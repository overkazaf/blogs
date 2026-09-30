#!/usr/bin/env python3
"""
Lab 14 攻击实验台: 对着 dongle 逐条验证"哪些攻击被挡住了, 哪些没挡住"。

每条攻击都有明确的期望结果, 脚本会给出 PASS/FAIL 表格, 方便验证
"加固 -> 再攻击" 的循环。

用法:
    tools/drm/attack.py --bundle out/bundle --sim
    tools/drm/attack.py --bundle out/bundle --port /dev/cu.usbmodem14101
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drm_common as dc            # noqa: E402
import pack_audio                  # noqa: E402
from player import SerialTransport, SimTransport, parse_reply   # noqa: E402


class Result:
    def __init__(self, name: str, expected: str, actual: str, blocked: bool, note: str = ""):
        self.name = name
        self.expected = expected
        self.actual = actual
        self.blocked = blocked
        self.note = note


def make_bundle(tmp: Path, device_id: str, *, track_id: str, allow_key: bool,
                counter: int = 1) -> dc.Bundle:
    """用一段临时 WAV 造一个 bundle(打包工具本身就是攻击者的对照物)。"""
    wav_path = tmp / f"{track_id}.wav"
    pack_audio.make_demo(wav_path, seconds=0.3, rate=8000)
    out_dir = tmp / f"bundle_{track_id}_{'k' if allow_key else 'nk'}_{counter}"
    out_dir.mkdir(parents=True, exist_ok=True)

    with wave.open(str(wav_path), "rb") as wav:
        params = wav.getparams()
        pcm = wav.readframes(params.nframes)

    iv = dc.random_nonce(8)
    k_audio = dc.derive_kaudio(track_id)
    ct = dc.encrypt(k_audio, iv, pcm)
    chunks = dc.chunk_count(len(ct))
    (out_dir / "track.enc").write_bytes(ct)
    manifest = {
        "version": dc.LICENCE_VERSION, "track_id": track_id,
        "cipher": "hmac-sha256-ctr", "chunk_size": dc.CHUNK_SIZE, "iv": iv,
        "chunks": chunks, "enc_file": "track.enc", "format": "wav",
        "channels": params.nchannels, "sampwidth": params.sampwidth,
        "sample_rate": params.framerate, "plaintext_bytes": len(pcm),
        "plaintext_sha256": hashlib.sha256(pcm).hexdigest(),
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    licence = dc.make_licence(device_id, track_id, iv, chunks,
                              counter=counter, allow_key=allow_key)
    (out_dir / "license.json").write_text(json.dumps(licence, indent=2))
    return dc.load_bundle(out_dir)


def load_licence(transport, licence: dict) -> dict:
    payload = json.dumps(licence, separators=(",", ":")).encode().hex()
    return parse_reply(transport.request(f"LOAD {payload}"))


def open_session(transport, licence: dict) -> tuple[dict, str]:
    load_licence(transport, licence)
    nonce = dc.random_nonce(8)
    return parse_reply(transport.request(f"OPEN {nonce}")), nonce


# --------------------------------------------------------------------------
# 攻击 1..8
# --------------------------------------------------------------------------

def attack_replay(transport, bundle) -> Result:
    opened, nonce = open_session(transport, bundle.licence)
    replay = parse_reply(transport.request(f"OPEN {nonce}"))
    blocked = replay.get("code") == "replay"
    return Result("1. 重放 OPEN 请求(同一个 nonce)",
                  "拒绝: code=replay",
                  f"{replay.get('status')} {replay.get('code', '')}",
                  blocked,
                  "nonce 缓存 + 服务器 nonce 让旧响应不可复用")


def attack_other_device(transport, bundle) -> Result:
    other = dc.make_licence("deadbeefdeadbeef", bundle.manifest["track_id"],
                            bundle.manifest["iv"], bundle.manifest["chunks"],
                            allow_key=True)
    reply = load_licence(transport, other)
    blocked = reply.get("code") == "device"
    return Result("2. 把 licence 复制到另一台设备",
                  "拒绝: code=device",
                  f"{reply.get('status')} {reply.get('code', '')}",
                  blocked,
                  "wrapped_key 是用本机 K_dev 派生的, 换机解不出密钥")


def attack_tamper_field(transport, bundle) -> Result:
    forged = dict(bundle.licence)
    forged["track_id"] = "another-track"
    reply = load_licence(transport, forged)
    blocked = reply.get("code") == "sig"
    return Result("3. 篡改 licence 字段(track_id)",
                  "拒绝: code=sig",
                  f"{reply.get('status')} {reply.get('code', '')}",
                  blocked,
                  "签名覆盖了全部授权字段")


def attack_forge_signature(transport, bundle) -> Result:
    forged = dict(bundle.licence)
    forged["sig"] = "00" * 32
    reply = load_licence(transport, forged)
    blocked = reply.get("code") == "sig"
    return Result("4. 伪造签名(没有 K_lic 的攻击者)",
                  "拒绝: code=sig",
                  f"{reply.get('status')} {reply.get('code', '')}",
                  blocked,
                  "K_lic 是 HMAC 对称密钥 —— 一旦固件被 dump, 这条防线就没了(见攻击 8)")


def attack_rollback(transport, bundle) -> Result:
    newer = dc.make_licence(bundle.licence["device_id"], bundle.manifest["track_id"],
                            bundle.manifest["iv"], bundle.manifest["chunks"],
                            counter=1000, allow_key=True)
    load_licence(transport, newer)
    older = dc.make_licence(bundle.licence["device_id"], bundle.manifest["track_id"],
                            bundle.manifest["iv"], bundle.manifest["chunks"],
                            counter=999, allow_key=True)
    reply = load_licence(transport, older)
    blocked = reply.get("code") == "rollback"
    return Result("5. 回滚到旧 licence(counter 变小)",
                  "拒绝: code=rollback",
                  f"{reply.get('status')} {reply.get('code', '')}",
                  blocked,
                  "counter 水位线只存 RAM: 断电后就能回滚 -> 真要防它得写 flash/OTP")


def attack_tamper_ciphertext(transport, bundle) -> Result:
    opened, _ = open_session(transport, bundle.licence)
    if opened.get("status") != "OK":
        return Result("6. 篡改加密音频(完整性)", "解密后校验失败",
                      f"OPEN 失败: {opened}", False,
                      "上一条攻击留下的回滚水位线把它挡住了, 先 RESET 或换新 counter")
    if opened.get("key", "WITHHELD") == "WITHHELD":
        return Result("6. 篡改加密音频(完整性)", "解密后校验失败",
                      "跳过(该 licence 不允许导出密钥)", False,
                      "用 allow_key=1 的 bundle 才测得到")
    key = bytes.fromhex(opened["key"])
    broken = bytearray(bundle.ciphertext)
    broken[100] ^= 0x01
    pcm = dc.decrypt(key, bundle.manifest["iv"], bytes(broken))
    dirty = hashlib.sha256(pcm).hexdigest() != bundle.manifest["plaintext_sha256"]
    return Result("6. 篡改加密音频(完整性)",
                  "解密后 SHA-256 不一致, 播放器拒绝输出",
                  "检测到篡改" if dirty else "没检测到!",
                  dirty,
                  "HMAC-CTR 只提供机密性, 完整性由 manifest 里的 SHA-256 提供")


def attack_level3_bulk(transport, tmp: Path, base: dc.Bundle) -> Result:
    bundle = make_bundle(tmp, transport.device_id, track_id="level3-track",
                         allow_key=False, counter=100)
    opened, _ = open_session(transport, bundle.licence)
    if opened.get("status") != "OK":
        return Result("7. Level 3 密钥不离开芯片", "key=WITHHELD + 分块可取",
                      f"OPEN 失败: {opened}", False,
                      "先 RESET 或把 counter 调高")
    if opened.get("key") != "WITHHELD":
        return Result("7. Level 3 密钥不离开芯片", "key=WITHHELD",
                      f"key={opened.get('key')}", False)
    ct = bundle.ciphertext
    size = bundle.manifest["chunk_size"]
    pcm = bytearray()
    for index in range(bundle.manifest["chunks"]):
        reply = parse_reply(transport.request(f"CHUNK {index}"))
        if reply["status"] != "OK":
            return Result("7. Level 3 密钥不离开芯片", "key=WITHHELD + 分块可取", str(reply), False)
        pcm += dc.xor_bytes(ct[index * size:(index + 1) * size], bytes.fromhex(reply["ks"])[:size])
    ok = hashlib.sha256(bytes(pcm)).hexdigest() == bundle.manifest["plaintext_sha256"]
    return Result("7. Level 3: 密钥不外泄, 但内容仍可被完整导出",
                  "key=WITHHELD 且 逐块取用可以还原出完整明文",
                  f"key=WITHHELD, 明文还原={'成功' if ok else '失败'}",
                  ok,
                  "结论: dongle 是物理钥匙, 谁拿着它谁就能放 —— 要靠限流/计数/水印兜底")


def attack_firmware_dump(bundle) -> Result:
    """离线攻击: 假设攻击者 dump 了固件拿到 K_ROOT。"""
    k_audio = dc.recover_kaudio(bundle.licence)
    pcm = dc.decrypt(k_audio, bundle.manifest["iv"], bundle.ciphertext)
    ok = hashlib.sha256(pcm).hexdigest() == bundle.manifest["plaintext_sha256"]

    other_device = dc.make_licence("cafebabecafebabe", bundle.manifest["track_id"],
                                   bundle.manifest["iv"], bundle.manifest["chunks"])
    forged_ok, _ = dc.verify_licence(other_device, "cafebabecafebabe")

    return Result("8. dump 固件拿到 K_ROOT 之后",
                  "可以直接离线解出内容, 并为任意设备伪造 licence",
                  f"离线解密={'成功' if ok else '失败'}, 通配伪造 licence={'成功' if forged_ok else '失败'}",
                  ok and forged_ok,
                  "这才是真正的攻击面: 所以量产时 K_ROOT 必须在 HSM, 设备端只放派生密钥 + debug lock")


def main() -> None:
    ap = argparse.ArgumentParser(description="Lab 14 攻击实验台")
    ap.add_argument("--bundle", required=True, help="pack_audio.py 的输出目录")
    ap.add_argument("--port", help="真实 dongle 串口")
    ap.add_argument("--sim", action="store_true", help="用虚拟 dongle")
    args = ap.parse_args()

    if bool(args.port) == bool(args.sim):
        ap.error("--port 和 --sim 必须二选一")

    bundle = dc.load_bundle(args.bundle)
    transport = SimTransport() if args.sim else SerialTransport(args.port)
    print(f"目标 dongle : {transport.name} (device_id={transport.device_id})")
    print(f"bundle      : {args.bundle} (device_id={bundle.licence['device_id']})")
    if transport.device_id and transport.device_id != bundle.licence["device_id"]:
        raise SystemExit("bundle 不是给这块 dongle 的, 先重新打包: --device-id "
                         f"{transport.device_id}")
    print()

    results: list[Result] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # 每条攻击都用干净状态, 避免互相污染
        transport.request("RESET")
        results.append(attack_replay(transport, bundle))
        transport.request("RESET")
        results.append(attack_other_device(transport, bundle))
        transport.request("RESET")
        results.append(attack_tamper_field(transport, bundle))
        transport.request("RESET")
        results.append(attack_forge_signature(transport, bundle))
        transport.request("RESET")
        results.append(attack_tamper_ciphertext(transport, bundle))
        transport.request("RESET")
        results.append(attack_level3_bulk(transport, tmp, bundle))
        transport.request("RESET")
        # 回滚实验放在最后: 它会把 counter 水位线推到 1000
        results.append(attack_rollback(transport, bundle))
    results.append(attack_firmware_dump(bundle))

    results.sort(key=lambda r: int(r.name.split(".", 1)[0]))   # 按编号打印
    width = max(len(r.name) for r in results)
    print(f"{'攻击'.ljust(width)}  结果")
    print("-" * (width + 30))
    for r in results:
        flag = "拦住 ✔" if r.blocked else "成功 ✘"
        print(f"{r.name.ljust(width)}  {flag}   (期望: {r.expected})")
        print(f"{' ' * width}  -> {r.actual}")
        if r.note:
            print(f"{' ' * width}     备注: {r.note}")
    print()
    blocked = sum(1 for r in results if r.blocked)
    print(f"小结: {blocked}/{len(results)} 条攻击按预期被挡住了。")
    print("注意 6/7/8 属于「信息可被复制」类攻击: 它们「成功」本身就是要教的东西。")


if __name__ == "__main__":
    main()
