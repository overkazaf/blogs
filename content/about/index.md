---
title: "About"
layout: "about"
summary: "about"
---

## +5

Reverse engineer & security researcher currently at **NetEase**, previously at **Alibaba Cloud**, **Acxiom**, and several startups. Passionate about reverse engineering and vulnerability research, with a primary focus on overseas streaming media and DRM reverse engineering.

Currently diving deep into **Widevine L1 Keybox dumping** — exploring trusted execution environments, hardware-backed key protection, and the attack surfaces around modern DRM implementations.

### Research Directions

| Direction | Keywords | Highlights |
|-----------|----------|------------|
| 🔐 **DRM Security** | Widevine · FairPlay · PlayReady · White-box Crypto · CENC · EME | L3 keybox DFA mass production, Chrome CDM white-box AES bypass, Netflix MSL protocol RE, FairPlay decryption chain |
| 🔧 **Reverse Engineering & Binary Security** | Android Native · TEE · Signature Algorithms · Device Fingerprinting · APK Hardening | Douyin MetaSec signature RE, ARM TrustZone EL0→EL3 attack surface, mainstream hardening analysis |
| 🤖 **AI Agent** | LLM-assisted RE · Automated Fuzzing · AI in Offense/Defense | Exploring — boundaries and practices of AI in security research |

### Education

- **Beihang University (BUAA)**
- **East China University of Science and Technology (ECUST)**

### Experience

10+ years in the industry as a backend and reverse engineer. Built large-scale systems serving tens of millions of users. Recently focused on **reverse engineering & security research**: DRM analysis, binary exploitation, TEE attack surfaces, and protocol-level cryptography.

Not a full-stack engineer — more of a *full-f\*ck* engineer. If it breaks, I fix it. If it doesn't exist, I build it.

### Languages & Tools

C/C++, Go, JavaScript, Python, Java

### Interests

- DRM internals & content protection systems
- Reverse engineering (Android native, TEE, embedded)
- Vulnerability research & exploit development
- Cryptography in real-world protocols
- AI Agent practices in security research

### Projects

| Project | Stack | Description |
|---------|-------|-------------|
| [D810G](https://github.com/overkazaf/D810G) | Python · Java | Ghidra deobfuscation framework — 6 rule sets with 96 Z3-verified MBA simplification rules, CFF deflattening (Unicorn emulation, OLLVM + Tigress), opaque predicate elimination, BCF removal, string decryption, VM devirtualization. 7-pass pipeline with fixpoint convergence. Standalone CLI + headless mode. x86_64 / ARM64 / ARM32. 398 tests. |
| [Ponce4Ghidra](https://github.com/overkazaf/Ponce4Ghidra) | Python · Java | Interactive symbolic execution plugin for Ghidra via angr + Z3. 4 symbolization methods (argv, function args, registers, memory). Find/Avoid targets from Listing or Decompiler views. Up to 5 alternative solutions per query. Veritesting + optional Unicorn fast concrete execution. Platforms: Mach-O x86_64, ELF x86_64, Android .so ARM/ARM64. |
| [unpack](https://github.com/overkazaf/unpack) | Python | Automated Android unpacking — 24 packer vendors (360, Tencent Legu, Bangcle, Ijiami, NetEase Yidun, Baidu, Alibaba, DexProtector, etc.). 3 engines: Memory (`/proc/pid/maps`+`mem`), Frida (4 strategies, 7 anti-detection bypasses), eBPF (bpftrace uprobes on `libart.so`). 7-step DEX repair. Packer scanner with 6-dimension weighted scoring. LLM integration: `unpack analyze \| claude -p`. |
| [cap](https://github.com/overkazaf/cap) | Go · Svelte | MITM proxy for reverse engineering — LLM-ready JSONL output (sign params auto-tagged), signature algorithm inference (MD5/SHA1/SHA256/HMAC), jadx source correlation (Retrofit annotations + OkHttp patterns → file/class/method/line), 5-language codegen (cURL, Python, Go, Java, JS). Native desktop GUI (Wails + Svelte). ADB cert install (3 strategies). HTTP/HTTPS/WebSocket/SSE/schema-less protobuf. |
| [re-agent](https://github.com/overkazaf/re-agent) | Go | Terminal RE/CTF agent — 3 AI model seats (planner / executor / researcher), 9 workflow modes, 24 built-in tools (`/scan`, `/hex`, `/decode`, `/entropy`, `/r2`, etc.). Live dashboard (dataflow, tools, plan, reasoning, telemetry). Retool integration: radare2, JADX, Ghidra, Burp, mitmproxy, angr, Unicorn, unidbg, Frida. Context compaction (snapcompact). Remote mode with encrypted SSH. |

🔬 [Reverse Engineering Blog](https://overkazaf.github.io/reverse_engineering) — Writeups, tools, and methodology for binary analysis, DRM research, and protocol reverse engineering.

### Contact

📧 overkazaf@gmail.com
💬 WeChat: `_0xAF_` (please mention your purpose when adding)

### Disclaimer

All research published on this site is strictly for **academic and security research purposes**. No functional exploit code, working keys, or usable tools are provided. Case studies are presented solely to illustrate reverse engineering methodology and cryptographic analysis techniques.

If any content on this site raises concerns regarding intellectual property or infringes on your rights, please contact me at overkazaf@gmail.com — I will promptly revise or remove the material in question.
