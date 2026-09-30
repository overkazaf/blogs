#!/usr/bin/env python3
"""
Lab 14 播放器: 播放"加密音频", 密钥必须向 RP2350 授权器要。

两种工作模式:

  Level 2 (licence allow_key=1)
      OPEN  -> dongle 返回内容密钥 -> 本地一次性解密
      优点: 简单, 兼容任意播放器
      缺点: 密钥进了主机内存, 攻击者可以顺手抄走

  Level 3 (licence allow_key=0)
      OPEN  -> dongle 只回确认值
      CHUNK -> 按块要 keystream, 逐块解密
      优点: 密钥不离开芯片
      缺点: 协议调用变多, 而且拿到 dongle 的人依然能把所有块要一遍

用法:
    tools/drm/player.py --bundle out/bundle --sim                 # 虚拟 dongle
    tools/drm/player.py --bundle out/bundle --port /dev/cu.usbmodem14101
    tools/drm/player.py --bundle out/bundle --port ... --level 3
    tools/drm/player.py --bundle out/bundle --sim --max-chunks 4  # 只解密前 4 块(试听)
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drm_common as dc  # noqa: E402


# --------------------------------------------------------------------------
# 传输层: 真串口 / 虚拟 dongle 共用同一个接口
# --------------------------------------------------------------------------

class SimTransport:
    name = "virtual dongle"

    def __init__(self, device_id: str | None = None):
        from dongle_sim import VirtualDongle, DEFAULT_SIM_DEVICE_ID
        self.dongle = VirtualDongle(device_id or DEFAULT_SIM_DEVICE_ID)
        self.device_id = self.dongle.device_id

    def request(self, line: str) -> str:
        return self.dongle.request(line)


class SerialTransport:
    def __init__(self, port: str, baud: int = 115200, timeout: float = 10.0):
        import serial
        self.ser = serial.Serial(port, baud, timeout=0.5)
        self.timeout = timeout
        self.name = f"serial {port}"
        self.device_id = None

        # 1) 先把板子刚启动时打印的 banner 读出来(如果它已经启动完了, 这里就什么也没有)
        for _ in range(6):
            line = self.ser.readline().decode(errors="replace").strip()
            if line:
                print(f"[dongle] {line}")

        # 2) 用 PING 确认对面在听 —— 无论是刚上电还是已经跑了一会儿都能对上
        for _ in range(10):
            self.ser.write(b"PING\n")
            reply = self.ser.readline().decode(errors="replace").strip()
            if reply.startswith("OK"):
                return
        raise SystemExit(
            f"{port} 没有响应 PING。检查:\n"
            f"  1) ls /dev/cu.usbmodem* 确认设备名\n"
            f"  2) 是否已经烧入 Lab 14 固件\n"
            f"  3) 有没有别的程序(比如 minicom) 占着这个串口"
        )

    def request(self, line: str) -> str:
        self.ser.reset_input_buffer()
        self.ser.write((line + "\n").encode())
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            reply = self.ser.readline().decode(errors="replace").strip()
            if reply.startswith(("OK", "ERR")):
                return reply
        return "ERR code=timeout"


def parse_reply(reply: str) -> dict:
    """把 "OK a=1 b=2" / "ERR code=x detail=y" 变成 dict。"""
    fields: dict[str, str] = {}
    if not reply:
        return {"status": "empty"}
    parts = reply.split()
    fields["status"] = parts[0]
    if parts[0] == "OK" and len(parts) > 1 and "=" not in parts[1] and parts[1] == "PONG":
        fields["cmd"] = "PONG"
        return fields
    for token in parts[1:]:
        if "=" in token:
            k, v = token.split("=", 1)
            fields[k] = v
        elif "cmd" not in fields and parts[0] == "ERR":
            fields["cmd"] = token
    return fields


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def open_session(transport, licence: dict, verbose: bool = True) -> dict:
    """INFO -> LOAD -> OPEN, 返回 dongle 的应答字段。"""
    info = parse_reply(transport.request("INFO"))
    dev = info.get("device_id")
    if verbose:
        print(f"[1] INFO  : device_id={dev} fw={info.get('fw')}")
    if dev and dev.lower() != licence["device_id"].lower():
        raise SystemExit(
            f"licence 绑定的是 {licence['device_id']}, 但这块 dongle 是 {dev}\n"
            f"-> 这正是设备绑定要挡住的情况(重新用 --device-id {dev} 打包一份就能过)")

    payload = dc.json.dumps(licence, separators=(",", ":")).encode().hex()
    loaded = parse_reply(transport.request(f"LOAD {payload}"))
    if loaded["status"] != "OK":
        raise SystemExit(f"LOAD 被拒绝: {loaded}")
    if verbose:
        print(f"[2] LOAD  : track={loaded.get('track')} counter={loaded.get('counter')} "
              f"chunks={loaded.get('chunks')} allow_key={loaded.get('allow_key')}")

    client_nonce = dc.random_nonce(8)
    opened = parse_reply(transport.request(f"OPEN {client_nonce}"))
    if opened["status"] != "OK":
        raise SystemExit(f"OPEN 被拒绝: {opened}")
    if verbose:
        print(f"[3] OPEN  : session={opened.get('session')} server_nonce={opened.get('server_nonce')} "
              f"key={'返回' if opened.get('key') != 'WITHHELD' else 'WITHHELD(拒绝导出)'}")

    opened["client_nonce"] = client_nonce
    return opened


def decrypt_level2(transport, bundle, opened, verbose=True) -> tuple[bytes, bytes]:
    key_hex = opened.get("key", "")
    if key_hex == "WITHHELD" or len(key_hex) != 64:
        raise SystemExit("licence 的 allow_key=0, dongle 不会给你密钥 -> 请用 --level 3")
    key = bytes.fromhex(key_hex)

    expect = dc.open_confirmation(key, bundle.licence["device_id"],
                                  bundle.licence["track_id"], opened["client_nonce"],
                                  opened["server_nonce"], int(opened["session"]))
    if expect != opened["resp"]:
        raise SystemExit("dongle 的会话确认值不对 —— 协议实现和固件不一致?")
    if verbose:
        print(f"[4] 确认  : resp 校验通过 (dongle 和播放器派生出同一个 K_audio)")
        print(f"            key = {key_hex}   <-- 密钥已经离开芯片, 攻击者可在此处截获")

    plaintext = dc.decrypt(key, bundle.manifest["iv"], bundle.ciphertext)
    return plaintext, key


def decrypt_level3(transport, bundle, opened, max_chunks: int | None = None,
                   verbose=True) -> tuple[bytes, None]:
    ct = bundle.ciphertext
    chunk_size = bundle.manifest["chunk_size"]
    chunks = bundle.manifest["chunks"]
    limit = chunks if not max_chunks else min(chunks, max_chunks)
    out = bytearray()
    for index in range(limit):
        reply = parse_reply(transport.request(f"CHUNK {index}"))
        if reply["status"] != "OK":
            raise SystemExit(f"CHUNK {index} 被拒绝: {reply}")
        ks = bytes.fromhex(reply["ks"])
        block = ct[index * chunk_size:(index + 1) * chunk_size]
        out += dc.xor_bytes(block, ks[:len(block)])
        if verbose and (index < 3 or index == limit - 1):
            print(f"[4] CHUNK : {index}/{chunks - 1} 解密 {len(block)} 字节 "
                  f"(keys 始终留在芯片里)")
        elif verbose:
            print(f"    CHUNK : {index}/{chunks - 1}")
    return bytes(out), None


def write_wav(path: Path, manifest: dict, pcm: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(manifest["channels"])
        wav.setsampwidth(manifest["sampwidth"])
        wav.setframerate(manifest["sample_rate"])
        wav.writeframes(pcm)


def main() -> None:
    ap = argparse.ArgumentParser(description="向 RP2350 dongle 要密钥并解密播放")
    ap.add_argument("--bundle", required=True, help="pack_audio.py 的输出目录")
    ap.add_argument("--port", help="dongle 的串口设备")
    ap.add_argument("--sim", action="store_true", help="用虚拟 dongle, 不接硬件")
    ap.add_argument("--level", type=int, choices=(2, 3), help="2=要密钥, 3=只按块要 keystream")
    ap.add_argument("--out-wav", help="解密结果写到哪里, 默认 <bundle>/decrypted.wav")
    ap.add_argument("--max-chunks", type=int, help="只解密前 N 块(做试听/限流实验)")
    ap.add_argument("--play", action="store_true", help="解密完用 afplay 播放")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if bool(args.port) == bool(args.sim):
        ap.error("--port 和 --sim 必须二选一")

    bundle = dc.load_bundle(args.bundle)
    manifest, licence = bundle.manifest, bundle.licence

    level = args.level or (2 if licence["allow_key"] else 3)
    if level == 2 and not licence["allow_key"]:
        ap.error("licence 的 allow_key=0, Level 2 拿不到密钥; 请用 --level 3 或重新打包")

    transport = SimTransport() if args.sim else SerialTransport(args.port)
    verbose = not args.quiet
    if verbose:
        print(f"dongle : {transport.name}")
        print(f"track  : {manifest['track_id']} ({manifest['plaintext_bytes']} 字节, "
              f"{manifest['chunks']} 块)")
        print(f"level  : {level} ({'返回内容密钥' if level == 2 else '只返回 keystream'})")

    opened = open_session(transport, licence, verbose)

    if level == 2:
        pcm, key = decrypt_level2(transport, bundle, opened, verbose)
    else:
        pcm, key = decrypt_level3(transport, bundle, opened, args.max_chunks, verbose)

    digest = hashlib.sha256(pcm).hexdigest()
    expected = manifest["plaintext_sha256"]
    if args.max_chunks:
        if verbose:
            print(f"[5] 校验  : 只解密了 {args.max_chunks} 块, 跳过整体校验")
    elif digest != expected:
        raise SystemExit(f"解密结果校验失败!\n  期望 {expected}\n  实际 {digest}")
    elif verbose:
        print(f"[5] 校验  : SHA-256 一致 {digest[:16]}... ✔")

    out_wav = Path(args.out_wav) if args.out_wav else Path(args.bundle) / "decrypted.wav"
    if not args.max_chunks:
        write_wav(out_wav, manifest, pcm)
        if verbose:
            print(f"[6] 输出  : {out_wav}")
    if args.play:
        play_wav = out_wav if not args.max_chunks else Path(args.bundle) / "preview.wav"
        if args.max_chunks:
            manifest_slice = dict(manifest)
            write_wav(play_wav, manifest_slice, pcm)
        print(f"[7] 播放  : afplay {play_wav}")
        subprocess.run(["afplay", str(play_wav)], check=False)


if __name__ == "__main__":
    main()
