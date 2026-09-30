#!/usr/bin/env python3
"""
Lab 14 打包工具: 把一个 WAV 变成 "加密音频 + manifest + licence"。

它扮演的是"内容方 / license server"的角色, 手里有 K_ROOT, 所以能:
  - 生成内容密钥 K_audio (按 track_id 派生)
  - 用 HMAC-CTR 加密音频
  - 针对某块板子的 device_id 签发 licence

用法:
    # 1. 先造一段测试音频(不用自己准备素材)
    tools/drm/pack_audio.py --make-demo out/demo.wav

    # 2. 打包(licence 绑定到 sim 设备, 可以先用虚拟 dongle 跑通)
    tools/drm/pack_audio.py --wav out/demo.wav --device-id sim --out-dir out/bundle

    # 3. 打包到真板子上(dongle 插着, 直接问它要 device_id)
    tools/drm/pack_audio.py --wav out/demo.wav --port /dev/cu.usbmodemXXXX --out-dir out/bundle

    # 4. Level 3: licence 不允许导出密钥, 上位机只能按块取 keystream
    tools/drm/pack_audio.py --wav out/demo.wav --device-id sim --no-key --out-dir out/bundle_l3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drm_common as dc  # noqa: E402


def make_demo(path: Path, seconds: float = 3.0, rate: int = 22050) -> None:
    """生成一段能听出"解密成功/失败"的测试音频: 先上行音阶, 再一段和弦。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = bytearray()
    total = int(seconds * rate)
    for n in range(total):
        t = n / rate
        if t < seconds * 0.6:
            # 渐进音阶 220Hz -> 880Hz
            freq = 220.0 * (2 ** (t / (seconds * 0.6) * 2))
            sample = 0.35 * math.sin(2 * math.pi * freq * t)
        else:
            # 小三和弦
            sample = 0.2 * (
                math.sin(2 * math.pi * 440.0 * t)
                + math.sin(2 * math.pi * 523.25 * t)
                + math.sin(2 * math.pi * 659.25 * t)
            ) / 1.5
        frames += struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32000))
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(frames))
    print(f"demo WAV  ->  {path} ({seconds:.1f}s, {rate}Hz, mono, 16-bit)")


def query_device_id(port: str, baud: int = 115200, timeout: float = 3.0) -> str:
    import serial   # pyserial
    with serial.Serial(port, baud, timeout=timeout) as ser:
        ser.reset_input_buffer()
        ser.write(b"INFO\n")
        for _ in range(10):
            line = ser.readline().decode(errors="replace").strip()
            if line.startswith("OK") and "device_id=" in line:
                for token in line.split():
                    if token.startswith("device_id="):
                        return token.split("=", 1)[1].lower()
        raise SystemExit(f"没有从 {port} 读到 device_id, 先手动跑一下 INFO 看看")


def main() -> None:
    ap = argparse.ArgumentParser(description="打包加密音频 + 签发 licence")
    ap.add_argument("--make-demo", metavar="WAV", help="只生成一段测试 WAV 然后退出")
    ap.add_argument("--wav", help="输入的明文 WAV")
    ap.add_argument("--out-dir", help="输出目录 (bundle)")
    ap.add_argument("--device-id", help="licence 绑定的 device_id (16 位 hex), 或 sim")
    ap.add_argument("--port", help="从真实 dongle 读取 device_id 的串口设备")
    ap.add_argument("--track-id", help="曲目 ID, 默认用 WAV 文件名")
    ap.add_argument("--counter", type=int, default=1, help="licence 版本号/回滚保护用计数器")
    ap.add_argument("--no-key", action="store_true",
                    help="签发 allow_key=0 的 licence -> Level 3, dongle 不再导出密钥")
    args = ap.parse_args()

    if args.make_demo:
        make_demo(Path(args.make_demo))
        return

    if not args.wav or not args.out_dir:
        ap.error("--wav 和 --out-dir 必须同时给出 (或者用 --make-demo)")

    device_id = args.device_id
    if args.port:
        device_id = query_device_id(args.port)
        print(f"device_id (来自 {args.port}) = {device_id}")
    if device_id == "sim":
        from dongle_sim import DEFAULT_SIM_DEVICE_ID
        device_id = DEFAULT_SIM_DEVICE_ID
    if not device_id or len(device_id) != 16:
        ap.error("--device-id 需要 16 位 hex (或者用 --port / 'sim')")

    wav_path = Path(args.wav)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with wave.open(str(wav_path), "rb") as wav:
        params = wav.getparams()
        pcm = wav.readframes(params.nframes)

    track_id = args.track_id or wav_path.stem
    iv = dc.random_nonce(8)                      # 8 字节 IV, 十六进制存放
    k_audio = dc.derive_kaudio(track_id)
    ciphertext = dc.encrypt(k_audio, iv, pcm)
    chunks = dc.chunk_count(len(ciphertext))

    enc_name = "track.enc"
    (out_dir / enc_name).write_bytes(ciphertext)

    manifest = {
        "version": dc.LICENCE_VERSION,
        "track_id": track_id,
        "cipher": "hmac-sha256-ctr",
        "chunk_size": dc.CHUNK_SIZE,
        "iv": iv,
        "chunks": chunks,
        "enc_file": enc_name,
        "format": "wav",
        "channels": params.nchannels,
        "sampwidth": params.sampwidth,
        "sample_rate": params.framerate,
        "plaintext_bytes": len(pcm),
        "plaintext_sha256": hashlib.sha256(pcm).hexdigest(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    licence = dc.make_licence(device_id, track_id, iv, chunks,
                              counter=args.counter, allow_key=not args.no_key)
    (out_dir / "license.json").write_text(json.dumps(licence, indent=2) + "\n")

    ok, why = dc.verify_licence(licence, device_id)
    print(f"WAV        : {wav_path} ({len(pcm)} 字节 PCM)")
    print(f"track_id   : {track_id}")
    print(f"device_id  : {device_id}")
    print(f"level      : {'Level 2 (allow_key=1, 密钥会离开芯片)' if not args.no_key else 'Level 3 (allow_key=0, 只给 keystream)'}")
    print(f"chunks     : {chunks} x {dc.CHUNK_SIZE} 字节")
    print(f"iv         : {iv}")
    print(f"自检       : {'OK' if ok else 'FAIL: ' + why}")
    print(f"输出       : {out_dir}/{{track.enc,manifest.json,license.json}}")
    print()
    print("播放:")
    print(f"  tools/drm/player.py --bundle {out_dir} --sim")
    print(f"  tools/drm/player.py --bundle {out_dir} --port /dev/cu.usbmodemXXXX --play")


if __name__ == "__main__":
    main()
