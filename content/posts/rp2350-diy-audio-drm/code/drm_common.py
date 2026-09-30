"""
Lab 14 音频 DRM —— 协议与密码学的"唯一真相"。

这份文件里的每个字符串拼接、每个密钥派生路径, 都必须和固件
labs/14_drm_dongle/src/main.c 完全一致, 否则 dongle 会拒绝你的 licence。

密码学全部只用到 SHA-256 / HMAC-SHA256:

    K_dev   = HMAC(K_ROOT, "device|<device_id>")            按设备隔离的设备密钥
    K_lic   = HMAC(K_ROOT, "license-signing")               厂商签发密钥(对称, 见 README 的攻击实验)
    K_audio = HMAC(K_ROOT, "track|<track_id>")              内容密钥

    wrapped_key = K_audio XOR HMAC(K_dev, "wrap|<device_id>|<track_id>")
                  ^^^ licence 只对"这台设备"有意义, 换设备解不出来

    sig = HMAC(K_lic, canonical_licence)                     防止篡改 licence 字段

    keystream_block(i) = HMAC(K_audio, "stream|<iv>|<i:016x>")   HMAC-CTR 流加密
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path

# 教学用根密钥, 必须与固件里的 K_ROOT_ASCII 一致
K_ROOT = b"RP2350-DRM-LAB-ROOT-KEY-v1-00000"
assert len(K_ROOT) == 32, "K_ROOT 必须 32 字节"

HASH_LEN = 32
KEY_LEN = 32
CHUNK_SIZE = 1024          # 必须是 32 的倍数, 且与固件 CHUNK_SIZE 一致
LICENCE_VERSION = 1


# --------------------------------------------------------------------------
# 基础密码学
# --------------------------------------------------------------------------

def hmac_sha256(key: bytes, msg: bytes) -> bytes:
    return hmac.new(key, msg, hashlib.sha256).digest()


def kdf_root(label: str) -> bytes:
    return hmac_sha256(K_ROOT, label.encode())


def derive_kdev(device_id: str) -> bytes:
    return kdf_root(f"device|{device_id.lower()}")


def derive_klic() -> bytes:
    return kdf_root("license-signing")


def derive_kaudio(track_id: str) -> bytes:
    return kdf_root(f"track|{track_id}")


def stream_block(key: bytes, iv: str, index: int) -> bytes:
    """HMAC-CTR 的一个 keystream 块, 与固件 stream_block() 对齐。"""
    return hmac_sha256(key, f"stream|{iv}|{index:016x}".encode())


def keystream(key: bytes, iv: str, nbytes: int, start_block: int = 0) -> bytes:
    blocks = (nbytes + HASH_LEN - 1) // HASH_LEN
    out = bytearray()
    for i in range(blocks):
        out += stream_block(key, iv, start_block + i)
    return bytes(out[:nbytes])


def chunk_keystream(key: bytes, iv: str, chunk_index: int) -> bytes:
    """某一整块的 keystream —— 固件 CHUNK 命令返回的就是它。"""
    return keystream(key, iv, CHUNK_SIZE, start_block=chunk_index * CHUNK_SIZE // HASH_LEN)


def xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def encrypt(key: bytes, iv: str, plaintext: bytes) -> bytes:
    return xor_bytes(plaintext, keystream(key, iv, len(plaintext)))


def decrypt(key: bytes, iv: str, ciphertext: bytes) -> bytes:
    return encrypt(key, iv, ciphertext)   # 流加密, 加解密同一操作


# --------------------------------------------------------------------------
# licence
# --------------------------------------------------------------------------

def licence_canonical(lic: dict) -> str:
    """签名覆盖的字段集合 —— 与固件 licence_canonical() 一字不差。"""
    return (
        f"license|v={lic['v']}"
        f"|device_id={lic['device_id']}"
        f"|track_id={lic['track_id']}"
        f"|iv={lic['iv']}"
        f"|chunks={lic['chunks']}"
        f"|counter={lic['counter']}"
        f"|allow_key={lic['allow_key']}"
        f"|wrapped_key={lic['wrapped_key']}"
    )


def wrap_key(k_audio: bytes, device_id: str, track_id: str) -> bytes:
    mask = hmac_sha256(derive_kdev(device_id), f"wrap|{device_id.lower()}|{track_id}".encode())
    return xor_bytes(k_audio, mask)


def unwrap_key(wrapped: bytes, device_id: str, track_id: str) -> bytes:
    return wrap_key(wrapped, device_id, track_id)   # XOR 自反


def make_licence(device_id: str, track_id: str, iv: str, chunks: int,
                 counter: int = 1, allow_key: bool = True) -> dict:
    device_id = device_id.lower()
    k_audio = derive_kaudio(track_id)
    lic = {
        "v": LICENCE_VERSION,
        "device_id": device_id,
        "track_id": track_id,
        "iv": iv,
        "chunks": chunks,
        "counter": counter,
        "allow_key": 1 if allow_key else 0,
        "wrapped_key": wrap_key(k_audio, device_id, track_id).hex(),
    }
    lic["sig"] = hmac_sha256(derive_klic(), licence_canonical(lic).encode()).hex()
    return lic


def verify_licence(lic: dict, device_id: str | None = None) -> tuple[bool, str]:
    """宿主侧的 licence 自检(和 dongle 的判断逻辑一致), 用于打包后验证。"""
    try:
        canonical = licence_canonical(lic)
    except KeyError as exc:
        return False, f"missing field {exc}"
    expected = hmac_sha256(derive_klic(), canonical.encode()).hex()
    if not hmac.compare_digest(expected, str(lic.get("sig", ""))):
        return False, "signature mismatch"
    if device_id is not None and lic["device_id"].lower() != device_id.lower():
        return False, f"device mismatch (licence={lic['device_id']} device={device_id})"
    return True, "ok"


def recover_kaudio(lic: dict) -> bytes:
    """只有拿到 K_ROOT 的人才能这么做 —— 这既是打包工具的能力, 也是攻击路径。"""
    return unwrap_key(bytes.fromhex(lic["wrapped_key"]), lic["device_id"], lic["track_id"])


def open_confirmation(k_audio: bytes, device_id: str, track_id: str,
                      client_nonce: str, server_nonce: str, session: int) -> str:
    msg = f"open|{device_id.lower()}|{track_id}|{client_nonce.lower()}|{server_nonce.lower()}|{session}"
    return hmac_sha256(k_audio, msg.encode()).hex()


# --------------------------------------------------------------------------
# manifest / licence 文件读写
# --------------------------------------------------------------------------

@dataclass
class Bundle:
    """一个打包好的"加密音频 + manifest + licence"集合。"""
    directory: Path
    manifest: dict
    licence: dict

    @property
    def ciphertext(self) -> bytes:
        return (self.directory / self.manifest["enc_file"]).read_bytes()

    def licence_bytes(self) -> bytes:
        return json.dumps(self.licence, separators=(",", ":")).encode()

    def xor_ciphertext_byte(self, offset: int) -> bytes:
        """攻击实验用: 翻转密文里的一个字节。"""
        data = bytearray(self.ciphertext)
        data[offset] ^= 0x01
        return bytes(data)


def load_bundle(directory: str | Path) -> Bundle:
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    licence = json.loads((directory / "license.json").read_text())
    return Bundle(directory=directory, manifest=manifest, licence=licence)


def random_nonce(nbytes: int = 8) -> str:
    import os
    return os.urandom(nbytes).hex()


def chunk_count(nbytes: int, chunk_size: int = CHUNK_SIZE) -> int:
    return (nbytes + chunk_size - 1) // chunk_size
