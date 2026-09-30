#ifndef DRM_CRYPTO_H
#define DRM_CRYPTO_H

#include <stddef.h>
#include <stdint.h>

#define DRM_HASH_LEN 32
#define DRM_KEY_LEN  32

/* HMAC-SHA256 */
int drm_hmac_sha256(const uint8_t *key, size_t key_len,
                    const uint8_t *msg, size_t msg_len,
                    uint8_t out[DRM_HASH_LEN]);

/* SHA-256 */
int drm_sha256(const uint8_t *msg, size_t msg_len, uint8_t out[DRM_HASH_LEN]);

/*
 * Known answer tests against RFC 4231 (HMAC-SHA256 test case 1 / 2) and
 * FIPS 180-4 (SHA-256("abc")).  Used by the on-device KAT command so you can
 * prove the crypto path of the firmware is correct before trusting it.
 * Returns 0 on success, -1 on failure, and fills "detail" with a short report.
 */
int drm_kat_check(char *detail, size_t detail_len);

/* out must have room for 2*in_len + 1 bytes */
size_t drm_hex_encode(const uint8_t *in, size_t in_len, char *out);

/* returns number of decoded bytes, or -1 on malformed input / overflow */
int drm_hex_decode(const char *in, uint8_t *out, size_t out_len);

/* compare without early exit (keeps timing uniform) */
int drm_const_time_eq(const uint8_t *a, const uint8_t *b, size_t len);

#endif /* DRM_CRYPTO_H */
