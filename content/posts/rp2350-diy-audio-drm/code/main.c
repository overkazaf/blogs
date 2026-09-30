/*
 * Lab 14 - RP2350 音频 DRM 授权器 (license dongle)
 *
 * 目标: 用一块 Pico 2W 做"硬件授权器", 把内容密钥锁在芯片里, 让上位机
 * (Mac / Raspberry Pi) 只能通过 USB CDC 协议在授权范围内取密钥或取
 * 单块 keystream。
 *
 * Level 2: OPEN 直接返回内容密钥 (方便跑通全链路, 也是真实 dongle 的经典形态)
 * Level 3: license 里 allow_key=0, OPEN 不返回密钥, 只能按块取 keystream
 *
 * 协议是 ASCII 行协议, 每个请求一行, 每个响应一行, 见 README。
 * 上位机实现: tools/drm/{drm_common,pack_audio,dongle_sim,player,attack}.py
 */

#include <stdarg.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>

#include "pico/stdlib.h"
#include "pico/unique_id.h"
#include "pico/rand.h"

#include "drm_crypto.h"

#define FW_VERSION   "0.1"
#define LINE_MAX     4096
#define OUT_MAX      4096
#define CHUNK_SIZE   1024                 /* 明文块大小(字节), 必须是 32 的倍数 */
#define KS_BLOCKS    (CHUNK_SIZE / DRM_HASH_LEN)
#define NONCE_CACHE  8

/*
 * 教学用的"厂商根密钥"。
 *
 * 真系统里它应该待在 HSM / license server 里, 设备端只保存由它派生的、
 * 按设备隔离的密钥, 而且设备端往往还需要 debug lock + secure boot 才能
 * 真正保护住。这里为了实验方便把它同时放进固件和上位机工具里 —— 这正好
 * 是 Lab 14 的攻击实验之一: dump 固件 = 拿到根密钥 = 可以伪造 license。
 */
static const char K_ROOT_ASCII[] = "RP2350-DRM-LAB-ROOT-KEY-v1-00000";
_Static_assert(sizeof(K_ROOT_ASCII) - 1 == DRM_KEY_LEN, "K_ROOT must be 32 bytes");

/* ------------------------------------------------------------------ */
/* licence state                                                       */
/* ------------------------------------------------------------------ */

typedef struct {
    bool     loaded;
    char     track_id[64];
    char     iv[32];
    uint32_t chunks;
    uint32_t counter;
    uint32_t allow_key;
    uint8_t  kaudio[DRM_KEY_LEN];
} licence_t;

static licence_t g_lic;

static bool     g_open;                     /* 当前会话是否已经 OPEN 成功 */
static char     g_client_nonce[32];
static char     g_server_nonce[32];
static uint32_t g_session;
static uint32_t g_highest_counter;          /* 回滚保护(掉电丢失, 见 README) */
static char     g_nonce_cache[NONCE_CACHE][32];
static uint8_t  g_nonce_cache_idx;

static char     g_ks_hex[2 * CHUNK_SIZE + 1];
static char     g_out[OUT_MAX];
static char     g_line[LINE_MAX];

/* ------------------------------------------------------------------ */
/* small helpers                                                       */
/* ------------------------------------------------------------------ */

static void resp(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_out, sizeof(g_out), fmt, ap);
    va_end(ap);
    printf("%s\n", g_out);
}

/* HMAC(K_ROOT, label) */
static void kdf_root(const char *label, uint8_t out[DRM_KEY_LEN]) {
    drm_hmac_sha256((const uint8_t *)K_ROOT_ASCII, sizeof(K_ROOT_ASCII) - 1,
                    (const uint8_t *)label, strlen(label), out);
}

static void device_id_hex(char out[17]) {
    pico_unique_board_id_t id;
    pico_get_unique_board_id(&id);
    drm_hex_encode(id.id, PICO_UNIQUE_BOARD_ID_SIZE_BYTES, out);
}

static void derive_kdev(const char *device_id, uint8_t out[DRM_KEY_LEN]) {
    char buf[64];
    snprintf(buf, sizeof(buf), "device|%s", device_id);
    kdf_root(buf, out);
}

static void derive_klic(uint8_t out[DRM_KEY_LEN]) {
    kdf_root("license-signing", out);
}

static void derive_kaudio(const char *track_id, uint8_t out[DRM_KEY_LEN]) {
    char buf[96];
    snprintf(buf, sizeof(buf), "track|%s", track_id);
    kdf_root(buf, out);
}

/* keystream 的第 index 个 32 字节块: HMAC(K_audio, "stream|" + iv + "|" + %016llx) */
static void stream_block(const uint8_t key[DRM_KEY_LEN], const char *iv,
                         uint64_t index, uint8_t out[DRM_HASH_LEN]) {
    char buf[96];
    snprintf(buf, sizeof(buf), "stream|%s|%016llx", iv, (unsigned long long)index);
    drm_hmac_sha256(key, DRM_KEY_LEN, (const uint8_t *)buf, strlen(buf), out);
}

/* ------------------------------------------------------------------ */
/* minimal JSON field reader (licence 是小 JSON, 不需要完整解析器)      */
/* ------------------------------------------------------------------ */

static const char *json_find(const char *json, const char *key) {
    char pattern[64];
    const char *p;
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    p = strstr(json, pattern);
    if (!p) return NULL;
    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    if (*p != ':') return NULL;
    p++;
    while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') p++;
    return p;
}

static int json_get_str(const char *json, const char *key, char *out, size_t out_len) {
    const char *p = json_find(json, key);
    size_t n = 0;
    if (!p || *p != '"') return -1;
    p++;
    while (*p && *p != '"') {
        if (n + 1 >= out_len) return -1;
        out[n++] = *p++;
    }
    if (*p != '"') return -1;
    out[n] = '\0';
    return (int)n;
}

static int json_get_u32(const char *json, const char *key, uint32_t *out) {
    const char *p = json_find(json, key);
    if (!p) return -1;
    if (*p < '0' || *p > '9') return -1;
    *out = (uint32_t)strtoul(p, NULL, 10);
    return 0;
}

/* ------------------------------------------------------------------ */
/* licence handling                                                    */
/* ------------------------------------------------------------------ */

/* canonical string 必须与 tools/drm/drm_common.py 完全一致 */
static void licence_canonical(char *out, size_t out_len,
                              uint32_t v, const char *device_id, const char *track_id,
                              const char *iv, uint32_t chunks, uint32_t counter,
                              uint32_t allow_key, const char *wrapped_key) {
    snprintf(out, out_len,
             "license|v=%u|device_id=%s|track_id=%s|iv=%s|chunks=%u|counter=%u"
             "|allow_key=%u|wrapped_key=%s",
             (unsigned)v, device_id, track_id, iv, (unsigned)chunks,
             (unsigned)counter, (unsigned)allow_key, wrapped_key);
}

static void load_licence(const char *json) {
    char device_id[32], my_id[17], wrapped_hex[80], sig_hex[80], canonical[512];
    uint8_t wrapped[DRM_KEY_LEN], sig[DRM_HASH_LEN], expect[DRM_HASH_LEN];
    uint8_t kdev[DRM_KEY_LEN], klic[DRM_KEY_LEN], mask[DRM_HASH_LEN];
    uint32_t v = 0;
    size_t i;

    device_id_hex(my_id);

    if (json_get_u32(json, "v", &v) != 0) { resp("ERR code=parse field=v"); return; }
    if (json_get_str(json, "device_id", device_id, sizeof(device_id)) < 0) {
        resp("ERR code=parse field=device_id"); return;
    }
    if (json_get_str(json, "track_id", g_lic.track_id, sizeof(g_lic.track_id)) < 0) {
        resp("ERR code=parse field=track_id"); return;
    }
    if (json_get_str(json, "iv", g_lic.iv, sizeof(g_lic.iv)) < 0) {
        resp("ERR code=parse field=iv"); return;
    }
    if (json_get_u32(json, "chunks", &g_lic.chunks) != 0) {
        resp("ERR code=parse field=chunks"); return;
    }
    if (json_get_u32(json, "counter", &g_lic.counter) != 0) {
        resp("ERR code=parse field=counter"); return;
    }
    if (json_get_u32(json, "allow_key", &g_lic.allow_key) != 0) {
        resp("ERR code=parse field=allow_key"); return;
    }
    if (json_get_str(json, "wrapped_key", wrapped_hex, sizeof(wrapped_hex)) < 0) {
        resp("ERR code=parse field=wrapped_key"); return;
    }
    if (json_get_str(json, "sig", sig_hex, sizeof(sig_hex)) < 0) {
        resp("ERR code=parse field=sig"); return;
    }

    /* 1. licence 签名 (厂商侧签发) */
    licence_canonical(canonical, sizeof(canonical), v, device_id, g_lic.track_id,
                      g_lic.iv, g_lic.chunks, g_lic.counter, g_lic.allow_key,
                      wrapped_hex);
    derive_klic(klic);
    drm_hmac_sha256(klic, DRM_KEY_LEN, (const uint8_t *)canonical, strlen(canonical), expect);
    if (drm_hex_decode(sig_hex, sig, sizeof(sig)) != DRM_HASH_LEN ||
        !drm_const_time_eq(sig, expect, DRM_HASH_LEN)) {
        resp("ERR code=sig"); return;
    }

    /* 2. 设备绑定 */
    if (strcasecmp(device_id, my_id) != 0) {
        resp("ERR code=device expected=%s got=%s", my_id, device_id);
        return;
    }

    /* 3. 回滚保护: 新 licence 的 counter 不允许比本次上电见过的更小 */
    if (g_highest_counter && g_lic.counter < g_highest_counter) {
        resp("ERR code=rollback counter=%u highest=%u",
             (unsigned)g_lic.counter, (unsigned)g_highest_counter);
        return;
    }
    if (g_lic.counter > g_highest_counter) g_highest_counter = g_lic.counter;

    /* 4. 用设备密钥解出内容密钥 K_audio */
    if (drm_hex_decode(wrapped_hex, wrapped, sizeof(wrapped)) != DRM_KEY_LEN) {
        resp("ERR code=parse field=wrapped_key"); return;
    }
    derive_kdev(device_id, kdev);
    {
        char ctx[160];
        snprintf(ctx, sizeof(ctx), "wrap|%s|%s", device_id, g_lic.track_id);
        drm_hmac_sha256(kdev, DRM_KEY_LEN, (const uint8_t *)ctx, strlen(ctx), mask);
    }
    for (i = 0; i < DRM_KEY_LEN; i++) g_lic.kaudio[i] = wrapped[i] ^ mask[i];

    g_lic.loaded = true;
    g_open = false;

    resp("OK track=%s counter=%u chunks=%u allow_key=%u",
         g_lic.track_id, (unsigned)g_lic.counter, (unsigned)g_lic.chunks,
         (unsigned)g_lic.allow_key);
}

static bool nonce_seen(const char *nonce) {
    for (int i = 0; i < NONCE_CACHE; i++) {
        if (g_nonce_cache[i][0] && strcasecmp(g_nonce_cache[i], nonce) == 0) return true;
    }
    return false;
}

static void nonce_remember(const char *nonce) {
    snprintf(g_nonce_cache[g_nonce_cache_idx], sizeof(g_nonce_cache[0]), "%s", nonce);
    g_nonce_cache_idx = (uint8_t)((g_nonce_cache_idx + 1) % NONCE_CACHE);
}

static void cmd_open(const char *client_nonce) {
    char device_id[17], canonical[384], resp_hex[65];
    uint8_t confirm[DRM_HASH_LEN];
    uint32_t rnd;

    if (!g_lic.loaded) { resp("ERR code=nolicense"); return; }
    if (strlen(client_nonce) != 16) { resp("ERR code=hex field=client_nonce"); return; }
    if (nonce_seen(client_nonce)) { resp("ERR code=replay nonce=%s", client_nonce); return; }

    rnd = get_rand_32();
    snprintf(g_server_nonce, sizeof(g_server_nonce), "%08x%08x",
             (unsigned)rnd, (unsigned)get_rand_32());
    snprintf(g_client_nonce, sizeof(g_client_nonce), "%s", client_nonce);
    nonce_remember(client_nonce);
    g_session++;
    g_open = true;

    device_id_hex(device_id);
    snprintf(canonical, sizeof(canonical), "open|%s|%s|%s|%s|%u",
             device_id, g_lic.track_id, g_client_nonce, g_server_nonce,
             (unsigned)g_session);
    drm_hmac_sha256(g_lic.kaudio, DRM_KEY_LEN, (const uint8_t *)canonical,
                    strlen(canonical), confirm);
    drm_hex_encode(confirm, DRM_HASH_LEN, resp_hex);

    if (g_lic.allow_key) {
        char key_hex[2 * DRM_KEY_LEN + 1];
        drm_hex_encode(g_lic.kaudio, DRM_KEY_LEN, key_hex);
        resp("OK track=%s counter=%u session=%u server_nonce=%s key=%s resp=%s",
             g_lic.track_id, (unsigned)g_lic.counter, (unsigned)g_session,
             g_server_nonce, key_hex, resp_hex);
    } else {
        resp("OK track=%s counter=%u session=%u server_nonce=%s key=WITHHELD resp=%s",
             g_lic.track_id, (unsigned)g_lic.counter, (unsigned)g_session,
             g_server_nonce, resp_hex);
    }
}

static void cmd_chunk(uint32_t index) {
    uint8_t block[DRM_HASH_LEN];
    uint64_t first;
    size_t pos = 0;

    if (!g_lic.loaded) { resp("ERR code=nolicense"); return; }
    if (!g_open) { resp("ERR code=nosession"); return; }
    if (index >= g_lic.chunks) {
        resp("ERR code=range chunks=%u", (unsigned)g_lic.chunks); return;
    }

    first = ((uint64_t)index * CHUNK_SIZE) / DRM_HASH_LEN;
    for (size_t i = 0; i < KS_BLOCKS; i++) {
        char hex[2 * DRM_HASH_LEN + 1];
        stream_block(g_lic.kaudio, g_lic.iv, first + i, block);
        drm_hex_encode(block, DRM_HASH_LEN, hex);
        memcpy(g_ks_hex + pos, hex, 2 * DRM_HASH_LEN);
        pos += 2 * DRM_HASH_LEN;
    }
    g_ks_hex[pos] = '\0';

    resp("OK chunk=%u size=%u ks=%s", (unsigned)index, (unsigned)CHUNK_SIZE, g_ks_hex);
}

/* ------------------------------------------------------------------ */
/* command dispatch                                                    */
/* ------------------------------------------------------------------ */

static void handle_line(char *line) {
    char *cmd = line;
    char *arg1, *arg2;
    char device_id[17];

    while (*cmd == ' ' || *cmd == '\t') cmd++;
    if (*cmd == '\0') return;

    arg1 = strpbrk(cmd, " \t");
    if (arg1) { *arg1++ = '\0'; while (*arg1 == ' ' || *arg1 == '\t') arg1++; }
    arg2 = arg1 ? strpbrk(arg1, " \t") : NULL;
    if (arg2) { *arg2++ = '\0'; while (*arg2 == ' ' || *arg2 == '\t') arg2++; }

    /* 大小写不敏感的命令名 */
    for (char *p = cmd; *p; p++) {
        if (*p >= 'a' && *p <= 'z') *p = (char)(*p - 'a' + 'A');
    }

    if (strcmp(cmd, "PING") == 0) {
        resp("OK PONG");
    } else if (strcmp(cmd, "INFO") == 0) {
        device_id_hex(device_id);
        resp("OK fw=%s device_id=%s chunk_size=%u nonce_cache=%u",
             FW_VERSION, device_id, (unsigned)CHUNK_SIZE, (unsigned)NONCE_CACHE);
    } else if (strcmp(cmd, "KAT") == 0) {
        char detail[160];
        if (drm_kat_check(detail, sizeof(detail)) == 0) {
            resp("OK kat=pass vectors=%s", detail);
        } else {
            resp("ERR code=kat detail=%s", detail);
        }
    } else if (strcmp(cmd, "LOAD") == 0) {
        uint8_t raw[1024];
        int n;
        if (!arg1) { resp("ERR code=parse field=payload"); return; }
        n = drm_hex_decode(arg1, raw, sizeof(raw) - 1);
        if (n <= 0) { resp("ERR code=hex field=payload"); return; }
        raw[n] = '\0';
        load_licence((const char *)raw);
    } else if (strcmp(cmd, "OPEN") == 0) {
        if (!arg1) { resp("ERR code=parse field=client_nonce"); return; }
        cmd_open(arg1);
    } else if (strcmp(cmd, "CHUNK") == 0) {
        if (!arg1) { resp("ERR code=parse field=index"); return; }
        cmd_chunk((uint32_t)strtoul(arg1, NULL, 10));
    } else if (strcmp(cmd, "RESET") == 0) {
        memset(&g_lic, 0, sizeof(g_lic));
        memset(g_nonce_cache, 0, sizeof(g_nonce_cache));
        g_open = false;
        resp("OK reset");
    } else if (strcmp(cmd, "HELP") == 0) {
        resp("OK cmds=PING,INFO,KAT,LOAD <lic_hex>,OPEN <nonce>,CHUNK <i>,RESET,HELP");
    } else {
        resp("ERR code=unknown cmd=%s", cmd);
    }
}

/* ------------------------------------------------------------------ */
/* main loop                                                           */
/* ------------------------------------------------------------------ */

int main(void) {
    char device_id[17];
    size_t len = 0;

    stdio_init_all();
    sleep_ms(1500);           /* 等 USB CDC 枚举完成 */

    device_id_hex(device_id);

    printf("\n");
    printf("=== RP2350 DRM Dongle Lab (fw %s) ===\n", FW_VERSION);
    printf("device_id : %s\n", device_id);
    printf("chunk_size: %u bytes\n", (unsigned)CHUNK_SIZE);
    printf("levels    : license allow_key=1 -> Level 2 (返回内容密钥)\n");
    printf("            license allow_key=0 -> Level 3 (只按块给 keystream)\n");
    printf("type HELP for commands\n");
    printf("[READY]\n");

    while (true) {
        int c = getchar_timeout_us(0);
        if (c == PICO_ERROR_TIMEOUT) {
            sleep_us(200);
            continue;
        }
        if (c == '\r' || c == '\n') {
            if (len == 0) continue;
            g_line[len] = '\0';
            len = 0;
            handle_line(g_line);
        } else if (c == 127 || c == 8) {
            if (len > 0) len--;
        } else if (len + 1 < sizeof(g_line)) {
            g_line[len++] = (char)c;
        } else {
            len = 0;
            resp("ERR code=overflow");
        }
    }
}
