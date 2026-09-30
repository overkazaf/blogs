#!/usr/bin/env python3
"""
固件 / 上位机 协议一致性自检。

Lab 14 最容易被忽略、也最容易出错的点: 协议里的字符串拼接两边必须完全一样。
比如固件签的是 "license|v=1|device_id=..." 而上位机签的是
"license|v=1|device_id=..." 多一个空格, 结果就是所有 licence 都验签失败。

这个脚本做两件事:
  1. 把 labs/14_drm_dongle/src/main.c 里的字符串字面量抽出来, 检查格式串是否存在
  2. 用同一组测试值分别跑 "C 格式串" 和 "drm_common.py 的拼接", 比较结果

用法:
    tools/drm/parity_check.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drm_common as dc  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
MAIN_C = ROOT / "labs" / "14_drm_dongle" / "src" / "main.c"
CRYPTO_C = ROOT / "labs" / "14_drm_dongle" / "src" / "drm_crypto.c"

TEMPLATES = {
    "licence 签名串": "license|v=%u|device_id=%s|track_id=%s|iv=%s|chunks=%u|counter=%u|allow_key=%u|wrapped_key=%s",
    "设备密钥派生": "device|%s",
    "内容密钥派生": "track|%s",
    "密钥包裹上下文": "wrap|%s|%s",
    "keystream 块上下文": "stream|%s|%016llx",
    "会话确认串": "open|%s|%s|%s|%s|%u",
}


def c_string_literals(path: Path) -> str:
    """
    抽取 C 字符串字面量(跳过注释和字符常量); 相邻字面量按 C 的规则拼接,
    不相邻的用 \\x00 隔开, 免得不同语句的字面量被连成假匹配。
    """
    text = path.read_text()
    out: list[str] = []
    last_end: int | None = None
    i, n = 0, len(text)

    while i < n:
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if text[i] == "'":                     # 字符常量, 里面的引号不能当字符串开始
            i += 1
            while i < n and text[i] != "'":
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if text[i] == '"':
            j = i + 1
            buf: list[str] = []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    buf.append(text[j])
                    j += 1
                    if j < n:
                        buf.append(text[j])
                        j += 1
                    continue
                buf.append(text[j])
                j += 1
            literal = "".join(buf).replace('\\"', '"').replace("\\\\", "\\")
            if last_end is not None and not re.fullmatch(r"[ \t\r\n]*", text[last_end:i]):
                out.append("\x00")
            out.append(literal)
            last_end = j + 1
            i = j + 1
            continue
        i += 1
    return "".join(out)


def cprintf(fmt: str, *args) -> str:
    """把 %u / %s / %016llx / %08x 这些 C 格式按顺序替换掉。"""
    it = iter(args)

    def repl(m: re.Match) -> str:
        width = m.group(2)
        kind = m.group(4)
        value = next(it)
        if kind == "s":
            return str(value)
        if kind == "u":
            return str(int(value))
        w = int(width) if width else 0
        num = f"{int(value):0{w}x}"
        return num.upper() if kind == "X" else num

    return re.sub(r"%([0 ]?)(\d*)(ll|l)?([usxX])", repl, fmt)


def main() -> int:
    if not MAIN_C.exists():
        print(f"找不到 {MAIN_C}")
        return 2

    blob = c_string_literals(MAIN_C)
    crypto_src = CRYPTO_C.read_text() if CRYPTO_C.exists() else ""
    failures: list[str] = []

    print("== 1. 固件里的格式串 ==")
    for label, tmpl in TEMPLATES.items():
        ok = tmpl in blob
        print(f"  {'✔' if ok else '✘'} {label:16} {tmpl[:60]}{'...' if len(tmpl) > 60 else ''}")
        if not ok:
            failures.append(f"main.c 里找不到格式串: {label} -> {tmpl}")

    print("== 2. 同一组输入下的字符串比对 ==")
    device_id = "0123456789abcdef"
    track_id = "demo-track"
    iv = "aabbccddeeff0011"
    chunks, counter, allow_key = 12, 3, 1
    wrapped = "00" * 32

    lic = {"v": dc.LICENCE_VERSION, "device_id": device_id, "track_id": track_id,
           "iv": iv, "chunks": chunks, "counter": counter, "allow_key": allow_key,
           "wrapped_key": wrapped}
    pairs = [
        ("licence 签名串",
         cprintf(TEMPLATES["licence 签名串"], lic["v"], device_id, track_id, iv,
                 chunks, counter, allow_key, wrapped),
         dc.licence_canonical(lic)),
        ("设备密钥派生",
         cprintf(TEMPLATES["设备密钥派生"], device_id),
         f"device|{device_id.lower()}"),
        ("内容密钥派生",
         cprintf(TEMPLATES["内容密钥派生"], track_id),
         f"track|{track_id}"),
        ("密钥包裹上下文",
         cprintf(TEMPLATES["密钥包裹上下文"], device_id, track_id),
         f"wrap|{device_id.lower()}|{track_id}"),
        ("keystream 块上下文",
         cprintf(TEMPLATES["keystream 块上下文"], iv, 0x1234),
         f"stream|{iv}|{0x1234:016x}"),
        ("会话确认串",
         cprintf(TEMPLATES["会话确认串"], device_id, track_id, "0011223344556677",
                 "8899aabbccddeeff", 7),
         f"open|{device_id.lower()}|{track_id}|0011223344556677|8899aabbccddeeff|7"),
    ]
    for label, from_c, from_py in pairs:
        ok = from_c == from_py
        print(f"  {'✔' if ok else '✘'} {label:16}")
        if not ok:
            print(f"      C : {from_c}")
            print(f"      PY: {from_py}")
            failures.append(f"{label} 两边拼接结果不同")

    print("== 3. 固件常量与上位机常量 ==")
    checks = [
        ("K_ROOT (32 字节 ASCII)", dc.K_ROOT.decode() in blob,
         f'固件里没有 {dc.K_ROOT.decode()!r}'),
        ("CHUNK_SIZE 一致", f"#define CHUNK_SIZE   {dc.CHUNK_SIZE}" in MAIN_C.read_text(),
         f"main.c 里的 CHUNK_SIZE 不是 {dc.CHUNK_SIZE}"),
        ("nonce 长度 16 hex", "strlen(client_nonce) != 16" in MAIN_C.read_text(),
         "固件没有检查 client_nonce 长度"),
        ("HMAC ipad/opad", "0x36" in crypto_src and "0x5c" in crypto_src,
         "drm_crypto.c 里没有看到 ipad/opad 常数"),
        ("SHA-256 用硬件加速器", "pico/sha256.h" in crypto_src,
         "drm_crypto.c 没有用 pico/sha256.h (硬件 SHA-256)"),
    ]
    for label, ok, why in checks:
        print(f"  {'✔' if ok else '✘'} {label}")
        if not ok:
            failures.append(why)

    print("== 4. 应答字段名 (虚拟 dongle 实测 vs 固件格式串) ==")
    from dongle_sim import VirtualDongle  # 延迟导入, 避免无谓依赖
    sim = VirtualDongle()
    ok_lic = dc.make_licence(sim.device_id, "parity-track", "0011223344556677",
                             chunks=2, counter=1, allow_key=True)
    import json
    payload = json.dumps(ok_lic, separators=(",", ":")).encode().hex()
    trial = {
        "INFO": sim.request("INFO"),
        "LOAD 拒绝": sim.request("LOAD zz"),
        "OPEN 无 licence": sim.request("OPEN 0011223344556677"),
        "CHUNK 无会话": sim.request("CHUNK 0"),
        "未知命令": sim.request("NOPE"),
        "LOAD 成功": sim.request(f"LOAD {payload}"),
        "OPEN 成功": sim.request(f"OPEN {dc.random_nonce(8)}"),
        "CHUNK 成功": sim.request("CHUNK 0"),
    }
    sim.request("RESET")
    fields = sorted({tok.split("=", 1)[0] for reply in trial.values()
                     for tok in reply.split() if "=" in tok and tok.split("=", 1)[0].isidentifier()})
    for name in fields:
        ok = f"{name}=" in blob
        print(f"  {'✔' if ok else '✘'} {name}=")
        if not ok:
            failures.append(f"固件里没有回应字段 {name}=")

    print()
    if failures:
        print(f"不一致 {len(failures)} 处:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("固件与上位机的协议拼接完全一致 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
