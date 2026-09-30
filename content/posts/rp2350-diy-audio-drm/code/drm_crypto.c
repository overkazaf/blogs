/*
 * Crypto helpers for the RP2350 DRM dongle lab.
 *
 * 全部只依赖 SHA-256:
 *   - HMAC-SHA256 用于密钥派生 / licence 签名 / 会话确认
 *   - HMAC-SHA256 计数模式 ("HMAC-CTR") 作为内容流加密
 *
 * SHA-256 直接用 RP2350 的硬件加速器 (pico/sha256.h, 底层是 hardware_sha256),
 * 所以密钥派生和 keystream 生成都不占 CPU。HMAC 的 ipad/opad 结构在这里手写,
 * 方便对照 RFC 2104 看每一步。
 */

#include "drm_crypto.h"

#include <stdio.h>
#include <string.h>

#include "pico/sha256.h"

#define SHA_BLOCK_LEN 64

static void sha_begin(pico_sha256_state_t *st) {
    pico_sha256_start_blocking(st, SHA256_BIG_ENDIAN, false);
}

static void sha_update(pico_sha256_state_t *st, const uint8_t *data, size_t len) {
    if (len) pico_sha256_update_blocking(st, data, len);
}

static void sha_end(pico_sha256_state_t *st, uint8_t out[DRM_HASH_LEN]) {
    sha256_result_t res;
    pico_sha256_finish(st, &res);
    memcpy(out, res.bytes, DRM_HASH_LEN);
}

int drm_sha256(const uint8_t *msg, size_t msg_len, uint8_t out[DRM_HASH_LEN]) {
    pico_sha256_state_t st;
    sha_begin(&st);
    sha_update(&st, msg, msg_len);
    sha_end(&st, out);
    return 0;
}

int drm_hmac_sha256(const uint8_t *key, size_t key_len,
                    const uint8_t *msg, size_t msg_len,
                    uint8_t out[DRM_HASH_LEN]) {
    uint8_t k[SHA_BLOCK_LEN];
    uint8_t ipad[SHA_BLOCK_LEN];
    uint8_t opad[SHA_BLOCK_LEN];
    uint8_t inner[DRM_HASH_LEN];
    pico_sha256_state_t st;
    size_t i;

    memset(k, 0, sizeof(k));
    if (key_len > SHA_BLOCK_LEN) {
        drm_sha256(key, key_len, k);          /* 长密钥先哈希, RFC 2104 4) */
    } else {
        memcpy(k, key, key_len);
    }
    for (i = 0; i < SHA_BLOCK_LEN; i++) {
        ipad[i] = (uint8_t)(k[i] ^ 0x36);
        opad[i] = (uint8_t)(k[i] ^ 0x5c);
    }

    sha_begin(&st);
    sha_update(&st, ipad, sizeof(ipad));
    sha_update(&st, msg, msg_len);
    sha_end(&st, inner);

    sha_begin(&st);
    sha_update(&st, opad, sizeof(opad));
    sha_update(&st, inner, sizeof(inner));
    sha_end(&st, out);
    return 0;
}

static int hex_to_byte(char c, uint8_t *out) {
    if (c >= '0' && c <= '9') { *out = (uint8_t)(c - '0'); return 0; }
    if (c >= 'a' && c <= 'f') { *out = (uint8_t)(c - 'a' + 10); return 0; }
    if (c >= 'A' && c <= 'F') { *out = (uint8_t)(c - 'A' + 10); return 0; }
    return -1;
}

size_t drm_hex_encode(const uint8_t *in, size_t in_len, char *out) {
    static const char digits[] = "0123456789abcdef";
    size_t i;
    for (i = 0; i < in_len; i++) {
        out[2 * i]     = digits[(in[i] >> 4) & 0x0f];
        out[2 * i + 1] = digits[in[i] & 0x0f];
    }
    out[2 * in_len] = '\0';
    return 2 * in_len;
}

int drm_hex_decode(const char *in, uint8_t *out, size_t out_len) {
    size_t len = strlen(in);
    size_t i;
    if (len % 2 != 0 || len / 2 > out_len) return -1;
    for (i = 0; i < len / 2; i++) {
        uint8_t hi, lo;
        if (hex_to_byte(in[2 * i], &hi) != 0) return -1;
        if (hex_to_byte(in[2 * i + 1], &lo) != 0) return -1;
        out[i] = (uint8_t)((hi << 4) | lo);
    }
    return (int)(len / 2);
}

int drm_const_time_eq(const uint8_t *a, const uint8_t *b, size_t len) {
    uint8_t diff = 0;
    size_t i;
    for (i = 0; i < len; i++) diff |= (uint8_t)(a[i] ^ b[i]);
    return diff == 0;
}

/* ------------------------------------------------------------------ */
/* Known answer tests                                                  */
/* ------------------------------------------------------------------ */

static int check_hmac(const uint8_t *key, size_t key_len,
                      const char *msg, const char *expected_hex,
                      const char *label, char *detail, size_t detail_len) {
    uint8_t out[DRM_HASH_LEN];
    uint8_t expected[DRM_HASH_LEN];
    if (drm_hmac_sha256(key, key_len, (const uint8_t *)msg, strlen(msg), out) != 0) {
        snprintf(detail, detail_len, "%s:hmac-error", label);
        return -1;
    }
    if (drm_hex_decode(expected_hex, expected, sizeof(expected)) != DRM_HASH_LEN) {
        snprintf(detail, detail_len, "%s:bad-vector", label);
        return -1;
    }
    if (!drm_const_time_eq(out, expected, DRM_HASH_LEN)) {
        char got[2 * DRM_HASH_LEN + 1];
        drm_hex_encode(out, DRM_HASH_LEN, got);
        snprintf(detail, detail_len, "%s:mismatch(got=%s)", label, got);
        return -1;
    }
    return 0;
}

int drm_kat_check(char *detail, size_t detail_len) {
    /* RFC 4231 test case 1 */
    uint8_t key1[20];
    /* RFC 4231 test case 2 */
    static const char key2[] = "Jefe";
    uint8_t digest[DRM_HASH_LEN];
    uint8_t expected_digest[DRM_HASH_LEN];
    static const char *sha_abc =
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

    memset(key1, 0x0b, sizeof(key1));

    if (check_hmac(key1, sizeof(key1), "Hi There",
                   "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7",
                   "rfc4231-1", detail, detail_len) != 0) {
        return -1;
    }
    if (check_hmac((const uint8_t *)key2, strlen(key2), "what do ya want for nothing?",
                   "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843",
                   "rfc4231-2", detail, detail_len) != 0) {
        return -1;
    }
    if (drm_sha256((const uint8_t *)"abc", 3, digest) != 0 ||
        drm_hex_decode(sha_abc, expected_digest, sizeof(expected_digest)) != DRM_HASH_LEN ||
        !drm_const_time_eq(digest, expected_digest, DRM_HASH_LEN)) {
        snprintf(detail, detail_len, "sha256-abc:mismatch");
        return -1;
    }
    snprintf(detail, detail_len, "rfc4231-1,rfc4231-2,sha256-abc");
    return 0;
}
