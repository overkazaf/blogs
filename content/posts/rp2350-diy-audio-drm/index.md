---
title: "用 RP2350 从零搭建一个 DRM 系统"
slug: "rp2350-diy-audio-drm-from-scratch"
date: 2026-09-30T10:00:00+08:00
draft: false
tags: ["RP2350", "Pico2", "embedded-security", "DRM", "HMAC-SHA256", "hardware-crypto", "dongle", "security-research"]
categories: ["hardware-security"]
description: "用 RP2350 微控制器做一个完整的音频 DRM 原型：内容加密、设备绑定、授权下发、密钥保护，然后自己攻击它。"
toc: true
math: false
---

> **读完本文，你将获得：**
> - 理解 DRM 系统的四个核心问题：内容加密、设备绑定、授权下发、密钥保护
> - 看到一个完整的、可运行的硬件 DRM 原型：从内容打包到设备授权到加密播放
> - 理解基于 HMAC-SHA256 的密钥层级设计，以及为什么每个公式是这样的
> - 对照 Widevine L1/L2/L3，理解「密钥是否离开芯片」这件事的实际含义
> - 亲手对自己造的 DRM 运行 8 条攻击，看到每一条防御挡住了什么、挡不住什么
> - 获得一张从教学原型到商业级 DRM 的加固路线图，以及与 Widevine/PlayReady 的差距对比

**阅读路线：**

| 目标 | 推荐章节 | 可以先跳过 |
|------|----------|------------|
| 建立 DRM 全局直觉 | 一～三 | 具体命令和代码 |
| 复现实验 | 四～五 | 加固路线和商业对比 |
| 做安全评估 | 六～八 | 密码学公式细节 |
| 评估与商业 DRM 差距 | 八～九 | 复现步骤 |

> **研究边界**：本文的所有实验仅针对**自有音频内容**和**自有硬件设备**。不涉及任何商业 DRM 系统的绕过或密钥提取。代码中的根密钥是故意写死的教学值 —— **不要拿这套代码保护任何真实内容**。

---

## 一、为什么要自己造一个 DRM

Widevine、PlayReady、FairPlay —— 商业 DRM 系统的架构文档可以读上万页，但它们的代码是闭源的，协议是加密的，密钥管理深藏在 TEE 和安全芯片里。站在外面看，你能看到协议握手的流程图；站在里面看，你还是不知道一条 licence 被篡改时到底哪一步会拒绝它、拒绝的原因是什么。

**理解攻击面的最好方式不是读文档，是自己造一个。**

协议自己写，密码学自己选，固件自己编译，然后自己攻击它 —— 每一条防御挡住了什么，没挡住什么，清清楚楚。

### 为什么用 RP2350

RP2350 是 Raspberry Pi 的第二代微控制器（Pico 2 上面那颗芯片），5 美元一块，但它带了几个对 DRM 实验很有用的特性：

| 特性 | DRM 实验中的作用 |
|------|-----------------|
| **硬件 SHA-256 加速器** | 密钥派生和 keystream 生成不占 CPU，性能有保障 |
| **OTP（一次性可编程存储）** | 可以把设备密钥烧进去，软件 dump 不出来 |
| **TrustZone-M** | Cortex-M33 的安全世界/非安全世界隔离 |
| **双架构（ARM / RISC-V）** | 同一份协议可以编译成两种 ISA，做跨架构逆向对比 |
| **SWD 调试口** | 攻击实验时可以用它 dump 内存、注入数据 |
| **USB CDC** | 板子直接作为 USB 串口设备，不需要额外硬件 |

关键点：这些特性在**实验阶段**可以选择性开关。先开着 debug 口方便做攻击实验，做完再 lock 掉做加固对比 —— 同一块板子走完攻防两面。

---

## 二、整体架构

系统分三个角色：

```
   ┌────────────┐  ① LOAD licence   ┌─────────────────────┐
   │  播放器     │ ────────────────▶ │  Pico 2W  (dongle)  │
   │  player.py │  ② OPEN nonce     │  K_ROOT / K_dev     │
   │            │ ◀──────────────── │  K_audio 永不出芯片  │
   │            │  ③ key 或 keystream└─────────────────────┘
   │ HMAC-CTR   │
   │ 解密+播放   │  ④ 解密 → 校验 SHA-256 → 播放
   └────────────┘
```

| 角色 | 实现 | 职责 |
|------|------|------|
| **打包工具** `pack_audio.py` | Python（Mac 或 Pi 上运行） | 加密音频、生成 manifest、签发 licence |
| **授权器** dongle 固件 | C（RP2350 Pico 2W） | 保存设备密钥、校验 licence、派生会话密钥、按块返回 keystream |
| **播放器** `player.py` | Python（Mac 或 Pi 上运行） | 读取加密音频、向 dongle 请求授权、解密、播放 |

此外有一个 **虚拟 dongle** `dongle_sim.py`，用纯 Python 模拟 RP2350 的行为。没有硬件也能把整套流程跑通。

### 威胁模型

实验假设攻击者能做到以下事情：

| 攻击者能力 | 是否防御 |
|-----------|---------|
| 拿到加密音频文件 | 有：内容加密 |
| 拿到播放器源码或二进制 | 有：密钥不在播放器里 |
| 复制 licence 文件 | 有：licence 绑定设备 |
| 抓 USB 串口通信 | 有：nonce 防重放 |
| 重放旧授权请求 | 有：会话 nonce + 缓存 |
| 分析 RP2350 固件（strings/Ghidra） | **部分**：根密钥在固件里（故意的，见攻击 #8） |
| 高成本物理攻击（芯片开盖） | 不防 |
| 模拟音频输出录制 | 不防 |

---

## 三、密码学设计：全 SHA-256 族

整个系统的密码学只用到一个原语：**HMAC-SHA256**。没有 AES，没有 RSA，没有椭圆曲线。

为什么？因为 RP2350 有硬件 SHA-256 加速器，HMAC-SHA256 的 ipad/opad 部分用软件做，核心的 SHA-256 压缩走硬件 —— 密钥派生和 keystream 生成几乎不占 CPU 时间。

### 密钥层级

```
K_ROOT（厂商根密钥，32 字节，教学版明文写在固件里）
├── K_dev  = HMAC(K_ROOT, "device|<device_id>")        设备密钥，每块板不同
├── K_lic  = HMAC(K_ROOT, "license-signing")            licence 签发密钥
└── K_audio = HMAC(K_ROOT, "track|<track_id>")          内容密钥，每首曲目不同
```

### 为什么这么设计

**K_dev 绑设备**：`device_id` 来自 RP2350 的 OTP 唯一 ID（`pico_get_unique_board_id()`）。同一份固件烧到两块板子，会得到两把不同的 `K_dev`。licence 里的 `wrapped_key` 是用 `K_dev` 包裹的，所以 licence 不能在设备之间互换。

**K_audio 绑内容**：每首曲目有独立的内容密钥。一首曲目被破解不影响其他曲目。

**K_lic 签名 licence**：licence 的所有授权字段（device_id、track_id、版本号、wrapped_key）都被 HMAC 签名覆盖，篡改任何字段都会导致签名校验失败。

### 完整公式表

| 用途 | 公式 |
|------|------|
| 设备密钥 | `K_dev = HMAC(K_ROOT, "device\|<device_id>")` |
| 签发密钥 | `K_lic = HMAC(K_ROOT, "license-signing")` |
| 内容密钥 | `K_audio = HMAC(K_ROOT, "track\|<track_id>")` |
| 密钥包裹 | `wrapped_key = K_audio XOR HMAC(K_dev, "wrap\|<device>\|<track>")` |
| licence 签名 | `sig = HMAC(K_lic, "license\|v=…\|device_id=…\|…\|wrapped_key=…")` |
| 流加密 | `keystream_block(i) = HMAC(K_audio, "stream\|<iv>\|<i:016x>")` |
| 会话确认 | `resp = HMAC(K_audio, "open\|<device>\|<track>\|<nonces>\|<session>")` |

### HMAC-CTR 流加密

密文 = 明文 XOR keystream。每块 keystream 是一次 HMAC 调用，块索引递增，等价于 CTR 模式。

> 这是教学用的流加密。生产环境请换 AES-GCM 或 ChaCha20-Poly1305，把完整性校验做进密文格式。本实验的完整性靠 manifest 里的 SHA-256 哈希事后校验。

---

## 四、Level 2 vs Level 3：密钥是否离开芯片

这是整个 DRM 最关键的分界线。本实验用同一套加密格式，通过 licence 里的一个字段 `allow_key` 控制：

| | Level 2 | Level 3 |
|---|---------|---------|
| `allow_key` | 1 | 0 |
| OPEN 响应 | 返回 `K_audio`（密钥） | 不返回密钥 |
| 解密方式 | 播放器拿到密钥，一次性解密 | 播放器按块请求 keystream |
| 密钥是否进入主机内存 | **是** | **否**（只有 keystream 进入） |
| 安全边界 | 密钥泄露 = 永久可解密 | 攻击者需要 dongle 在线才能解密 |

对照 Widevine 的安全级别（详见[《有了 PSSH 还是拿不到 Key》](/posts/widevine-pssh-license-l1-deep-dive/)）：

| Widevine | 密钥处理 | 解密位置 | 近似对应 |
|----------|---------|---------|---------|
| **L1** | 密钥在 TEE/SE 内 | 在安全硬件内解密 | 本实验没有等价物（需要 RP2350 自己解密音频输出） |
| **L2** | 密钥在 TEE 内 | CPU 处理解密后内容 | — |
| **L3** | 密钥在软件中 | 软件解密 | 本实验的 Level 2（密钥进主机内存） |

本实验的 Level 3（keystream-only）比 Widevine L3 更强一点：密钥本身不离开芯片，只有 keystream 流出去。但内容仍然可以被完整还原 —— 见攻击 #7。

---

## 五、跑通全流程

### 5.1 不需要硬件：虚拟 dongle

```sh
# 造 3 秒测试音频
tools/drm/pack_audio.py --make-demo out/demo.wav

# 打包（licence 绑定到虚拟设备）
tools/drm/pack_audio.py --wav out/demo.wav --device-id sim --out-dir out/bundle

# 播放（用 Python 虚拟 dongle）
tools/drm/player.py --bundle out/bundle --sim
```

三条命令走完打包 → 授权 → 解密 → 播放的全流程。虚拟 dongle 的密码学实现和固件逐字对齐（有一致性自检脚本 `parity_check.py` 保证）。

### 5.2 接真硬件

```sh
# 编译固件
scripts/build_lab14.sh

# 烧录
picotool load -f build/14_drm_dongle/rp2350_drm_dongle.uf2
picotool reboot -f

# 打包（--port 自动向 dongle 要 device_id，保证 licence 绑定正确）
tools/drm/pack_audio.py --wav out/demo.wav --port /dev/cu.usbmodem14101 --out-dir out/hw

# 播放
tools/drm/player.py --bundle out/hw --port /dev/cu.usbmodem14101 --play
```

烧录成功后串口会打印：

```
=== RP2350 DRM Dongle Lab (fw 0.1) ===
device_id : e66164XXXXXXXXXX
chunk_size: 1024 bytes
type HELP for commands
[READY]
```

### 5.3 Level 2 / Level 3 对比

```sh
# Level 2：密钥出芯片，上位机一次性解密
tools/drm/pack_audio.py --wav out/demo.wav --device-id sim --out-dir out/l2

# Level 3：密钥不出芯片，按块取 keystream
tools/drm/pack_audio.py --wav out/demo.wav --device-id sim --no-key --out-dir out/l3
tools/drm/player.py --bundle out/l3 --sim
```

两个 bundle 用的是同一套加密格式，可以直接对比「密钥是否离开芯片」这件事。

### 5.4 固件自检

串口敲 `KAT`，板子会用 RFC 4231 / FIPS 180-4 的标准测试向量验证 HMAC-SHA256 和硬件 SHA-256 通路：

```
> KAT
OK kat=pass vectors=rfc4231-1,rfc4231-2,sha256-abc
```

---

## 六、8 条攻击实验

这是整篇文章最重要的部分。一个没有攻击实验的 DRM 项目只是「加密播放 demo」。

```sh
tools/drm/attack.py --bundle out/bundle --sim     # 虚拟 dongle
tools/drm/attack.py --bundle out/hw --port /dev/cu.usbmodem14101  # 真硬件
```

| # | 攻击 | 期望结果 | 结论 |
|---|------|---------|------|
| 1 | 重放同一条 OPEN 请求 | `ERR code=replay` | nonce 缓存 + 会话 nonce 生效 |
| 2 | licence 复制到另一台设备 | `ERR code=device` | `wrapped_key` 绑定设备密钥 |
| 3 | 篡改 licence 中的授权字段 | `ERR code=sig` | HMAC 签名覆盖全部字段 |
| 4 | 伪造 licence 签名 | `ERR code=sig` | 但 K_lic 是对称密钥（见 #8） |
| 5 | 回滚到旧 licence（降版本号） | `ERR code=rollback` | 水位线检查生效（但只在 RAM，断电丢失） |
| 6 | 翻转密文中的 1 bit | 解密后 SHA-256 不符 | HMAC-CTR 只管机密性，完整性靠 manifest 兜底 |
| 7 | Level 3 逐块取全部 keystream | 密钥不外泄，但内容可完整还原 | dongle 是物理钥匙，要靠限流/计数/水印兜底 |
| 8 | dump 固件拿到 K_ROOT | 可以离线解密 + 伪造任意 licence | **真正的攻击面在密钥存放，不在算法** |

### 重点解读：攻击 #7 和 #8

**#7 —— Level 3 的天花板**

Level 3 做到了「密钥不离开芯片」，但 dongle 会老老实实地按块返回 keystream。攻击者拿着 dongle 把所有块要一遍，内容照样完整还原。

这不是 bug，这是架构的固有限制：只要授权器对合法请求必须响应，攻击者拿着合法设备就能导出内容。商业 DRM 用**播放计数、速率限制、水印和输出保护**来兜底，而不是靠协议本身阻止。

**#8 —— 根密钥泄露**

dump 固件（甚至只要 `strings rp2350_drm_dongle.elf`）就能拿到 K_ROOT。有了 K_ROOT，攻击者可以：

1. 派生任何设备的 K_dev → 解开任何设备的 wrapped_key
2. 生成 K_lic → 伪造 licence 签名
3. 生成 K_audio → 直接解密内容

算法没有被破解，协议没有被绕过，但 **一个密钥存放的问题让整条信任链归零**。

这就是为什么量产系统不能把 K_ROOT 放进固件 —— 它应该待在 HSM 或 license server 里。设备端只存设备级别的 K_dev（通过 OTP 或安全生产烧录注入），即使 dump 固件也只能拿到一台设备的密钥，无法伪造 licence。

---

## 七、加固路线图

第 #8 条攻击打开了加固的路径。以下 7 步按优先级排序，每一步都解决一个具体的攻击面：

| 步骤 | 加固措施 | 解决的攻击面 | 商业 DRM 中的等价物 |
|------|---------|-------------|-------------------|
| 1 | **密钥不进固件**：设备端只存 K_dev（OTP 派生），K_ROOT 放 HSM / license server | dump 固件拿根密钥（#8） | Widevine: keybox 在 TEE; PlayReady: 设备证书由 Microsoft 签发 |
| 2 | **换非对称签名**：licence 用 Ed25519/ECDSA，设备端只放公钥 | dump 固件也伪造不了 licence（#4 升级） | PlayReady: licence 用 RSA 签名; Widevine: license response 由 Google 签名 |
| 3 | **会话密钥替代内容密钥**：OPEN 返回 K_audio 的派生值 + 两层加密 | Level 2 的「密钥离开芯片」问题 | Widevine L1: 内容密钥在 TEE 内解封 |
| 4 | **counter 写入 flash/OTP** | 断电回滚（#5 的弱点） | PlayReady: secure stop; Widevine: 服务端计数 |
| 5 | **分块认证加密**：每块带 HMAC/GCM tag | 密文翻转（#6） | CENC: 每个 sample 独立加密; CMAF: 分片级完整性 |
| 6 | **debug lock + secure boot** | SWD dump 内存 | L1 设备: JTAG 禁用 + 安全启动链 |
| 7 | **速率限制 / 播放计数 / 水印** | 合法设备逐块导出（#7） | Netflix: 并发流限制; Spotify: 音频水印 |

> RP2350 SDK 里有 mbedtls，步骤 2 的 Ed25519/ECDSA 可以直接用。步骤 6 涉及 OTP 烧录，有不可逆风险，必须用可消耗的实验板。

---

## 八、与商业 DRM 还差多远

| 维度 | 本实验 | Widevine | PlayReady |
|------|--------|----------|-----------|
| **密钥保护** | K_ROOT 在固件里（可加固到 OTP） | Keybox 在 TEE / SE | 设备证书由 Microsoft 签发 |
| **licence 签名** | 对称 HMAC | Google 签名 | RSA 签名 |
| **内容加密** | HMAC-CTR（教学用） | AES-128-CTR / AES-128-CBC (CENC) | AES-128-CTR (CENC) |
| **认证加密** | 无（SHA-256 事后校验） | 可选 CBCS | 可选 CBCS |
| **设备身份** | OTP unique ID | Provisioning + 设备证书 | 设备证书 + domain |
| **密钥分发** | 本地打包，无 server | License Server（合作方代理） | License Server |
| **撤销** | 无 | CRL + server-side | CRL + domain management |
| **输出保护** | 无 | HDCP + secure decoder | HDCP + SL3000 |
| **生态认证** | 无 | Google 审核 + 合规测试 | Microsoft 审核 |
| **成本** | $5（一块 Pico 2W） | 设备端免费，server 端按量 | 同左 |

差距主要在三个地方：

1. **TEE / SE 硬件边界**：RP2350 的 TrustZone-M 是 MCU 级别的，和手机 SoC 里的 TEE（如 Trusty / OP-TEE）不在一个量级。但对于理解「安全世界 vs 非安全世界」的概念，它完全够用。

2. **密钥分发基础设施**：商业 DRM 有完整的 Provisioning、License Server、Certificate Authority、Revocation 体系。本实验是本地打包，没有 server。

3. **内容格式标准化**：Widevine/PlayReady 使用 CENC（Common Encryption）标准，可以在不同 DRM 之间切换同一份加密内容。本实验用自定义格式。

但核心的密码学思路是一样的：**密钥层级、设备绑定、licence 签名、keystream 派生**。理解了这套 $5 的原型，再去读 Widevine 的万页文档，每个设计选择都有了具体的对照物。

---

## 九、已知限制

直接列出来，免得踩坑：

- `K_ROOT` 明写在固件和 Python 工具里 —— 这是**故意的**，方便做攻击实验
- licence 的 `counter` 回滚保护只存在 RAM，断电即忘
- HMAC-CTR 没有认证加密，完整性靠 manifest 里的 SHA-256 事后校验
- Level 2 的密钥一定会进上位机内存
- Level 3 也挡不住「拿着 dongle 把所有块要一遍」
- USB CDC 是明文信道，不防 USB 抓包
- 全程只对**自有音频、自有设备**做实验

## 下一步

- **双板版**：Pico 2 当播放器（UART 向 Pico 2W 要 keystream，PWM/I2S 输出音频），Mac 只负责打包 —— 让明文音频不进入通用操作系统
- **SWD 注入**：用第二块板做 debugprobe，在 dongle 运行时改 `g_lic.allow_key` 或直接 dump `g_lic.kaudio`，对照攻击 #8
- **USB 抓包**：用 Wireshark + usbmon / USBPcap 抓 OPEN/CHUNK 协议帧，做重放与篡改实验
- **RISC-V 版固件**：RP2350 可以切 Hazard3（RISC-V 核），同一份协议换 ISA 再反汇编对比
- **白盒 AES 替换**：把 HMAC-CTR 换成白盒 AES，用 DFA 攻击自己的白盒实现 —— 打通硬件 DRM 和密码分析两条线

---

## 参考

- [RP2350 产品页](https://www.raspberrypi.com/products/rp2350/) — 芯片规格与安全特性
- [RP2350 Security Whitepaper](https://pip.raspberrypi.com/categories/1214-rp2350) — OTP、secure boot、TrustZone-M 的官方说明
- [有了 PSSH 还是拿不到 Key，从 L3 到 L1 有多远](/posts/widevine-pssh-license-l1-deep-dive/) — 本站 Widevine 深度分析
- [W3C Encrypted Media Extensions](https://www.w3.org/TR/encrypted-media-2/) — 浏览器端 DRM 标准
- [CENC ISO/IEC 23001-7](https://www.iso.org/standard/68042.html) — 通用加密标准
