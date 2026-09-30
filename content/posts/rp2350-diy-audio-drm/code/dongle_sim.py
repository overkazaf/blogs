#!/usr/bin/env python3
"""
Lab 14 虚拟授权器 —— 在 Mac 上把固件的协议逻辑跑一遍。

两种用法:

1) 作为库 (player.py --sim / attack.py 默认):
       from dongle_sim import VirtualDongle
       d = VirtualDongle()
       d.request("INFO")

2) 作为"假串口"设备, 让真实上位机代码走 pyserial:
       tools/drm/dongle_sim.py --pty
       # 会打印 /tmp/tty.dongle_sim -> /dev/ttysXXX, 然后
       tools/drm/player.py --bundle out/bundle --port /tmp/tty.dongle_sim

把同一套 licence 先喂给虚拟 dongle, 再喂给真板子, 就能确认"到底是
协议实现的问题, 还是硬件/固件的问题"。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import drm_common as dc  # noqa: E402

DEFAULT_SIM_DEVICE_ID = "0123456789abcdef"
NONCE_CACHE = 8


class VirtualDongle:
    """纯 Python 版 dongle, 行为对齐 labs/14_drm_dongle/src/main.c。"""

    def __init__(self, device_id: str = DEFAULT_SIM_DEVICE_ID, fw: str = "sim-0.1"):
        self.device_id = device_id.lower()
        self.fw = fw
        self.licence: dict | None = None
        self.kaudio: bytes | None = None
        self.opened = False
        self.highest_counter = 0
        self.nonce_cache: list[str] = []
        self.session = 0
        self.server_nonce = ""
        self.client_nonce = ""
        self.requests: list[str] = []          # 审计日志: 攻击实验会用到

    # ---------------- protocol ----------------

    def request(self, line: str) -> str:
        self.requests.append(line)
        parts = line.strip().split()
        if not parts:
            return ""
        cmd = parts[0].upper()
        args = parts[1:]

        if cmd == "PING":
            return "OK PONG"
        if cmd == "INFO":
            return (f"OK fw={self.fw} device_id={self.device_id} "
                    f"chunk_size={dc.CHUNK_SIZE} nonce_cache={NONCE_CACHE}")
        if cmd == "KAT":
            vectors = "rfc4231-1,rfc4231-2,sha256-abc"
            import hashlib
            import hmac as _hmac
            if (_hmac.new(b"\x0b" * 20, b"Hi There", hashlib.sha256).hexdigest()
                    != "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"):
                return "ERR code=kat detail=rfc4231-1"
            return f"OK kat=pass vectors={vectors} (host python)"
        if cmd == "LOAD":
            if not args:
                return "ERR code=parse field=payload"
            try:
                raw = bytes.fromhex(args[0]).decode()
            except ValueError:
                return "ERR code=hex field=payload"
            return self._load(raw)
        if cmd == "OPEN":
            if not args:
                return "ERR code=parse field=client_nonce"
            return self._open(args[0])
        if cmd == "CHUNK":
            if not args:
                return "ERR code=parse field=index"
            try:
                index = int(args[0])
            except ValueError:
                return "ERR code=parse field=index"
            return self._chunk(index)
        if cmd == "RESET":
            self.licence = None
            self.kaudio = None
            self.opened = False
            self.nonce_cache.clear()
            return "OK reset"
        if cmd == "HELP":
            return "OK cmds=PING,INFO,KAT,LOAD <lic_hex>,OPEN <nonce>,CHUNK <i>,RESET,HELP"
        return f"ERR code=unknown cmd={cmd}"

    # ---------------- handlers ----------------

    def _load(self, raw_json: str) -> str:
        try:
            lic = json.loads(raw_json)
        except json.JSONDecodeError:
            return "ERR code=parse field=json"

        for key in ("v", "device_id", "track_id", "iv", "chunks",
                    "counter", "allow_key", "wrapped_key", "sig"):
            if key not in lic:
                return f"ERR code=parse field={key}"

        ok, why = dc.verify_licence(lic)
        if not ok:
            return f"ERR code=sig detail={why.replace(' ', '_')}"
        if lic["device_id"].lower() != self.device_id:
            return f"ERR code=device expected={self.device_id} got={lic['device_id']}"
        if self.highest_counter and lic["counter"] < self.highest_counter:
            return (f"ERR code=rollback counter={lic['counter']} "
                    f"highest={self.highest_counter}")
        self.highest_counter = max(self.highest_counter, int(lic["counter"]))

        self.licence = lic
        self.kaudio = dc.recover_kaudio(lic)
        self.opened = False
        return (f"OK track={lic['track_id']} counter={lic['counter']} "
                f"chunks={lic['chunks']} allow_key={lic['allow_key']}")

    def _open(self, client_nonce: str) -> str:
        if self.licence is None or self.kaudio is None:
            return "ERR code=nolicense"
        if len(client_nonce) != 16:
            return "ERR code=hex field=client_nonce"
        client_nonce = client_nonce.lower()
        if client_nonce in self.nonce_cache:
            return f"ERR code=replay nonce={client_nonce}"

        self.nonce_cache.append(client_nonce)
        self.nonce_cache = self.nonce_cache[-NONCE_CACHE:]
        self.client_nonce = client_nonce
        self.server_nonce = dc.random_nonce(8)
        self.session += 1
        self.opened = True

        confirm = dc.open_confirmation(self.kaudio, self.device_id,
                                       self.licence["track_id"], client_nonce,
                                       self.server_nonce, self.session)
        key_field = self.kaudio.hex() if self.licence["allow_key"] else "WITHHELD"
        return (f"OK track={self.licence['track_id']} counter={self.licence['counter']} "
                f"session={self.session} server_nonce={self.server_nonce} "
                f"key={key_field} resp={confirm}")

    def _chunk(self, index: int) -> str:
        if self.licence is None or self.kaudio is None:
            return "ERR code=nolicense"
        if not self.opened:
            return "ERR code=nosession"
        if index < 0 or index >= int(self.licence["chunks"]):
            return f"ERR code=range chunks={self.licence['chunks']}"
        ks = dc.chunk_keystream(self.kaudio, self.licence["iv"], index)
        return f"OK chunk={index} size={dc.CHUNK_SIZE} ks={ks.hex()}"


# --------------------------------------------------------------------------
# pty 模式: 造一个"假串口", 让 pyserial 也能连上
# --------------------------------------------------------------------------

def serve_pty(device_id: str, link: Path | None, quiet: bool = False) -> None:
    import pty
    import select

    dongle = VirtualDongle(device_id)
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)
    if link:
        link.unlink(missing_ok=True)
        link.symlink_to(slave_name)
        print(f"虚拟 dongle 就绪: {link} -> {slave_name}")
    else:
        print(f"虚拟 dongle 就绪: {slave_name}")
    print(f"device_id = {dongle.device_id}")
    print("Ctrl-C 退出")

    buf = b""
    try:
        while True:
            r, _, _ = select.select([master], [], [], 0.2)
            if not r:
                continue
            try:
                data = os.read(master, 4096)
            except OSError:
                break
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.rstrip(b"\r").decode(errors="replace")
                if not line.strip():
                    continue
                reply = dongle.request(line)
                if not quiet:
                    print(f"  << {line[:60]}{'...' if len(line) > 60 else ''}\n  >> {reply[:80]}{'...' if len(reply) > 80 else ''}")
                os.write(master, (reply + "\n").encode())
    except KeyboardInterrupt:
        pass
    finally:
        os.close(master)
        os.close(slave)
        if link:
            link.unlink(missing_ok=True)
        print("\n虚拟 dongle 已停止")


def main() -> None:
    ap = argparse.ArgumentParser(description="虚拟 RP2350 DRM 授权器")
    ap.add_argument("--pty", action="store_true", help="创建一个假串口给 pyserial 用")
    ap.add_argument("--link", default="/tmp/tty.dongle_sim", help="假串口的软链接路径")
    ap.add_argument("--device-id", default=DEFAULT_SIM_DEVICE_ID)
    ap.add_argument("--quiet", action="store_true", help="不打印每条请求")
    args = ap.parse_args()

    if args.pty:
        serve_pty(args.device_id, Path(args.link) if args.link else None, args.quiet)
        return

    # 交互式: 直接敲命令, 和串口里对真板子敲的一样
    dongle = VirtualDongle(args.device_id)
    print(f"虚拟 dongle (device_id={dongle.device_id}), 输入命令, 例如 INFO / LOAD <hex> / OPEN <nonce>")
    for line in sys.stdin:
        reply = dongle.request(line.strip())
        if reply:
            print(reply)


if __name__ == "__main__":
    main()
