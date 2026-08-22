---
title: "把 Netflix MSL 拆成字节 - 一次协议逆向的路线、踩坑与经验"
slug: "netflix-msl-protocol-reverse-engineering"
date: 2026-08-15
lastmod: 2026-08-23
draft: false
tags: ["Netflix", "MSL", "DRM", "Widevine", "reverse-engineering", "CBOR", "Frida", "Android", "MediaDrm", "protocol-analysis", "ChinaDRM"]
categories: ["security-research"]
description: "围绕 Netflix Message Security Layer (MSL) 的协议逆向记录：为什么 HTTPS 之上还要再套一层加密信道、encrypt-then-MAC 到底签的是什么、CBOR integer key 编码的字节细节、MasterToken/UserIdToken 的绑定关系，以及 MSL 相对国内外流媒体与音乐加密方案（爱奇艺/腾讯视频/优酷/ChinaDRM/网易云/QQ音乐）强在哪"
image: "https://overkazaf.github.io/blogs/images/msl-protocol-analysis/research-roadmap.png"
toc: true
math: false
---

> **读完本文，你将获得：**
> - 一个常被忽略的问题的答案：既然已经有 HTTPS，Netflix 为什么还要在里面再套一层自研的 MSL 加密信道？
> - MSL 消息的字节级结构：header envelope、encrypted envelope、payload chunk 三层是怎么嵌套的，`encrypt-then-MAC` 到底对哪段 bytes 签名
> - CBOR integer key 编码的真实字节，以及为什么 `bytes` 写成 `text`、`headerdata` 内联成 map 会让服务端直接 502
> - 一套不依赖“协议很复杂所以安全”的评估框架：MSL 能保护什么、哪些属性取决于部署配置，以及攻击者会转向哪些边界

## 〇、摘要

本文记录笔者对 **Netflix Message Security Layer (MSL)** 的一次协议逆向。

和前几篇 Widevine / FairPlay 文章不同，这次的目标不是攻破某个白盒 AES，而是**把一个真实商业客户端的应用层加密协议，拆成能对着字节解释、能复现、能被服务端接受或拒绝的结构**。产出是两个最小实现：

- **`nfmsl.py`**：单文件 Python 研究客户端，每一层中间结构都能打印，用来做字段 diff 和错误二分；
- **Android `MslClient`**：用真机 MediaDrm / CryptoSession 复现 Android 侧的 MSL 加密签名链路。

真正的突破点不是拿到某个密钥，而是把下面这些东西对齐到服务端能接受的 wire format：

- MSL 消息的三层信封结构，以及 `encrypt-then-MAC` 中「签名签的是密文而非明文」这一点；
- CBOR integer key 编码里 `bytes`/`text`、内联 map/加密 envelope 的字节差异；
- `MasterToken` 与 `UserIdToken` 通过 `mtserialnumber` 建立的字段级绑定；
- `ASYMMETRIC_WRAPPED` 与 `WIDEVINE` 两类 Key Exchange 的密钥归属差异；
- `licensedManifest` 把 Manifest 与 Widevine license challenge 合并进同一个 payload 的结构。

本文同时区分三个容易混淆的对象：**Netflix 开源 MSL 框架的能力、本文观测到的 Netflix 客户端协议实例、Netflix 当前生产环境的完整安全策略**。前两者可以由公开文档和实验字节支持；第三者还包含服务端风控、密钥托管、撤销策略和设备分级，不能仅凭客户端逆向完整证明。

**为避免误读，先说明本文所有 hex 均为按协议结构重新构造的示意字节（密钥/身份材料用占位值），不含任何真实账号、cookie、ESN、设备密钥或内容密钥。** 这里保留的是协议结构、判断依据和踩坑经验。

---

## 一、路线总览

这条路线不是一开始就瞄准 MSL 的。笔者最初仍沿着 DRM 研究的惯性去看 Chrome CDM 和内容密钥，走了一段才意识到那不是最高杠杆的地方。

![Netflix MSL 逆向路线总览](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/research-roadmap.png)
*从排除错误战场开始，逐步转向协议层：Chrome CDM 路线用于确认「不要继续硬磨白盒」；Rave / Hearo 对照用于拆分现成第三方客户端的架构；Netflix APK 动态抓包提供真实样本；CBOR wire format 对齐后，才进入 nfmsl.py / MslClient 的最小实现。*

整条路线分 6 个阶段：

| 阶段 | 目标 | 方法 | 结果 |
|------|------|------|------|
| **Phase 0** | 拆问题 | 区分 CDM 白盒、播放路径、服务端协议 | 确定先看协议层 |
| **Phase 1** | 排除 Chrome CDM 路线 | BoringSSL hook、S-box 扫描、`aesenc` trap、堆搜索 | 密钥提取成本过高，转向 MSL |
| **Phase 2** | 对照第三方路线 | Rave / Hearo / nfmsl.py 横向拆解 | 把链路拆成登录、MSL、CDM、播放四层 |
| **Phase 3** | 捕获真实客户端行为 | Frida hook MessageHeader、CryptoSession、SSL_write | 拿到真实 token、headerdata、payload bytes |
| **Phase 4** | 复原 wire format | CBOR decode、字段 diff、错误码归因 | 对齐 integer key、bytes 类型、签名输入 |
| **Phase 5** | 最小实现 | Python 与 Android 两条实现并行 | Key Exchange / Manifest / licensedManifest 可验证 |

一句话概括整条路线的方法：**协议逆向不是把 APK 反编译完就结束，而是把每一个假设都变成一段服务端会接受或拒绝的 bytes。** 后面所有章节都在做同一件事——把「我以为的结构」和「服务端真正接受的字节」对齐。

---

## 二、先回答一个本质问题：HTTPS 之上为什么还要 MSL

Netflix 播放链路常被一句话概括为「HTTPS 里发 Widevine license」。这句话只对了一半：HTTPS 只是传输层，Widevine 只管 DRM 密钥，夹在中间那层 Netflix 自研的 MSL 才是真正的应用层安全信封。

![HTTPS / MSL / Widevine 的嵌套关系](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/protocol-stack.png)
*HTTPS 负责传输；MSL 可提供应用层加密、认证、token 绑定与可选非重放语义；Widevine 在本文观测链路里有双重角色：先为 MSL Key Exchange 提供会话能力，再通过 MSL 通道获取内容 license。*

既然已经有 TLS，为什么还要在里面重新做一遍加密、签名、防重放？要回答「为什么这么设计」，得先看 Netflix 面对的**威胁模型**，因为每一个设计选择都是对其中一条约束的回应：

- **客户端在攻击者手里。** 设备被 root / 越狱 / 魔改是常态，客户端里的任何静态密钥都应视为已泄露。
- **传输路径可能存在 TLS 终止或受信代理。** 企业设备、调试代理和服务端边缘都可能在 TLS 终点看到应用数据。应用层信封可以让消息在离开 MSL 端点前仍保持机密性和完整性，但它不能替代 TLS 对传输端点、元数据和可用性的保护。
- **服务端是分布式大集群。** 将会话密钥封装进可验证 token 可以减少粘性会话和密钥查表，但撤销、防重放、风控和授权仍可能需要共享状态。
- **设备形态极多。** TV、机顶盒、游戏机、手机、Web，网络栈千差万别，安全实现却要尽量只写一套。
- **内容价值高。** 需要 per-play 授权、可撤销、可灰度、可轮换密钥。

MSL 的每一条设计，都是在回答上面某一条：

1. **信道可绑定到 MSL 实体身份，而不只依赖 Web bearer token。** 常规服务器 TLS 主要认证服务端，并不自动证明客户端设备身份。MSL 的实际绑定强度取决于 entity authentication 和 Key Exchange：在观测到的 Widevine 路线中，会话能力可绑定到 provision 过的 CDM；在 `ASYMMETRIC_WRAPPED` 等软件路线中，安全上限则由客户端私钥和执行环境决定。因而“偷到 bearer token 不够”是一个**有条件结论**，不能泛化为所有 MSL 配置都具备硬件设备绑定。
2. **会话密钥来自 DRM / RSA，不来自 TLS 握手。** MSL 自带 Key Exchange（见 §五），`Kenc`/`Khmac` 经 Widevine CDM 或 RSA 建立，和 PKI / CA 信任根解耦。**这样一来，TLS 被拦截代理解密、甚至根证书被信任，MSL 这一层依旧是密文**——攻击者拿到的是一段自己解不开的 CBOR。这是对「TLS 拦截」的回应。
3. **加密、认证与可选防重放下沉到消息本身。** `messageid` 绑定请求与响应，payload `sequencenumber` 约束消息内分块顺序；真正的非重放语义由可选 `nonreplayableid` 提供，接收端还必须按实体身份和 `MasterToken.serialnumber` 维护已接收窗口。也就是说，MSL 能跨传输复用，但严格防重放并非“消息里有序号”就自动成立。
4. **token 是服务端签发的自包含凭证。** `MasterToken` 里的会话密钥由签发方保护（§六），服务节点可以从 token 恢复会话密钥，从而减少粘性会话和密钥存储；但 token 撤销、最新 sequence、防重放窗口、账号策略和风控仍然可能查状态。这是对分布式部署成本的优化，不等于服务端完全无状态。
5. **身份与会话分离，支持轮换。** 在本文观测的 Widevine 路线中，provision 后的实体材料相对长期，而 `MasterToken` 带 `renewalwindow` / `expiration`，会话密钥可独立更新；其他 entity authentication 方案可能使用不同身份根。这是对“密钥要可轮换”的回应。

一句话概括这套设计的本质：**MSL 不把全部安全属性只押在 TLS 连接上，而是把实体/用户上下文、机密性、完整性以及可选的非重放语义绑定到应用消息。** 其中设备身份强度、前向保密、撤销和防重放效果由具体部署决定。理解这个边界，才能看清它和 Widevine 之间那个容易看晕的**互相依赖**结构：

```text
建立 MSL 会话时：
  Widevine CDM 生成 challenge
  Netflix 返回 MSL session keys 或 key ids
  => Widevine 为 MSL 服务（提供会话密钥）

获取内容 license 时：
  MSL 加密 payload，里面放 Widevine challenge
  Netflix 返回 MSL 加密响应，里面有 license
  => MSL 为 Widevine 服务（充当运输信道）
```

这个循环解释了后面很多调试现象：只盯 Widevine license，会漏掉 MSL header 的 token 绑定；只盯 MSL 加密，又会忽略 CDM challenge 的参数必须和服务端预期一致。三层各自的职责如下：

| 层 | 解决的问题 | 典型数据 |
|----|------------|----------|
| **HTTPS** | 网络传输安全 | HTTP POST body、headers、TLS |
| **MSL** | 应用层可信信道 | `MasterToken`、`UserIdToken`、encrypted headerdata、payload chunk、HMAC |
| **Widevine** | DRM 设备认证与内容密钥管理 | CDM challenge、license response、content key |

---

## 三、战场选择：为什么先排除 Chrome CDM

进入协议层之前，先交代一段被排除的路线，因为它决定了后面为什么不去硬磨白盒。

笔者最初的直觉和多数 DRM 研究一样：先看 Chrome CDM，尝试在解密或 license 处理处直接拿内容密钥。三个实验很快把这条路否掉了：

| 假设 | 实验 | 观察 | 结论 |
|------|------|------|------|
| CDM 里有可 hook 的标准 AES | Hook BoringSSL AES 入口 | 入口存在但不触发 | 更像链接残留 / dead code |
| 白盒是可 DFA 的 T-table 结构 | 扫描 S-box / T-table / key schedule | 没有标准 AES 结构 | 不是老版本 Android L3 那种可 DFA 的实现 |
| 存在稳定硬件 AES 路径 | `aesenc` trap + perf profile | 没有稳定硬件 AES 路径 | 软件白盒与 OLLVM 调度器占主导 |

这一步的价值不在「失败」，而在**尽早排除错误战场**。如果目标是写一篇白盒密码学论文，继续啃 CDM 也许合理；但如果目标是理解 Netflix 播放链路，收益更高的地方在 MSL——因为服务端最终关心的从来不是「你能否 dump 出密钥」，而是「你能否建立合法 MSL 会话、能否拿到 Manifest、能否把 Widevine challenge 放进正确的加密信封」。这个判断把后面的工作从「找密钥」转成了「复现协议」。

在动手写代码前，笔者还横向拆了三个现成样本，用来区分「协议要求」和「某客户端的实现习惯」：

| 方案 | MSL 线格式 | Key Exchange | 密钥在哪里 | 工程特点 |
|------|------------|--------------|------------|----------|
| **Rave** | JSON 字符串键 | `ASYMMETRIC_WRAPPED` | Java 内存里的明文 AES/HMAC | 简单成熟，但依赖后端 CDM |
| **Hearo** | 远程 extractors 管理 | `WIDEVINE` / MediaDrm | 设备 CDM 的 CryptoSession 内部 | 接近官方播放，客户端复杂 |
| **nfmsl.py** | CBOR integer key | 两种都支持 | Python 可见或 pywidevine 解析 | 协议理解最深，维护成本最高 |

三个样本各自回答一个问题：Rave 说明 Web JSON MSL 是低门槛路线；Hearo 说明真实设备 CDM 可以当加密签名 oracle（§5.3 展开）；nfmsl.py 适合把协议细节完全摊开。**只有一个样本时，很容易把某客户端的实现细节误当成协议必需项；有三个样本对照，才能把二者分开。**

---

## 四、把一条 MSL 消息拆成字节

这一节是全文的技术心脏。MSL 最容易被低估的地方就是：算法本身（AES-128-CBC + HMAC-SHA256）平平无奇，真正的坑全在**结构和编码边界**。下面从整体结构、签名构造、CBOR 编码三个角度，逐层拆到字节。

### 4.1 一条消息 = 一个 header + 若干 payload chunk

一条 MSL 消息在 HTTP body 里是连续拼接的若干 CBOR 对象：**1 个 header envelope**，后面跟 **1 个或多个 payload chunk**。

![MSL CBOR Wire Format 核心结构](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/cbor-wire-format.png)
*Header envelope 与 payload chunk 都是 CBOR map。被加密的 headerdata / payload 本身又是一层 encrypted envelope（含 ciphertext、iv、keyid）。真正难的不是算法，而是值类型和嵌套位置。*

- **Header envelope**：携带身份与元数据。三个顶层字段——`32 mastertoken`（会话身份）、`33 headerdata`（加密后的元数据）、`16 signature`（对 headerdata 的 HMAC）。
- **Payload chunk**：携带业务数据。两个顶层字段——`64 payload`（加密后的业务 bytes）、`16 signature`（对该 payload 的 HMAC）。业务数据分块传输，每块独立加密、独立签名，并各自带 `sequencenumber` 与 `endofmsg` 标志。

这里第一个关键点：**`33 headerdata` 和 `64 payload` 的值都不是明文 map，而是一段「加密 envelope 的字节」**。也就是说，结构是三层嵌套：

```text
Layer A  header envelope   { 32: mastertoken, 33: <bytes>, 16: <bytes> }
                                             │
Layer B                        33 的值 = encrypted envelope（一段 bytes，本身也是 CBOR）
                               { 8: keyid, 9: iv, 6: ciphertext }
                                             │
Layer C                        ciphertext 解密后 = headerdata 明文 map
                               { 20: sender/ESN, 22: messageid, 24: timestamp,
                                 14: sequencenumber, 18: useridtoken, 36: capabilities }
```

### 4.2 encrypt-then-MAC：签名签的是密文，不是明文

这是 MSL 最容易踩、也最能体现协议本质的一点。很多人第一反应是「对 headerdata 明文算 HMAC」，于是怎么调都对不上。实际构造是标准的 **encrypt-then-MAC**：

```text
1. plaintext  = CBOR(headerdata 明文 map)              # Layer C
2. ciphertext = AES-128-CBC(plaintext, Kenc, iv)        # CryptoSession.encrypt
3. envelope   = CBOR({8: keyid, 9: iv, 6: ciphertext})  # Layer B，一段 bytes
4. signature  = HMAC-SHA256(envelope, Khmac)            # 注意：签的是第 3 步的 bytes
5. header     = CBOR({32: mastertoken, 33: envelope, 16: signature})
```

**签名的输入是第 3 步整段 encrypted envelope 的字节，而不是第 1 步的明文。** 校验方向相反：服务端先用 `Khmac` 验 `signature` 覆盖的是不是 `33` 的那段 bytes，验过了才解密。这决定了两件在调试中反复咬人的事：

- 只要 envelope 的**任何一个字节**（包括 iv、keyid 的编码）和签名输入不一致，HMAC 就对不上，服务端根本不会尝试解密——你会得到一个「解析/认证失败」而不是「业务失败」，很容易误判成 payload 写错了。
- **handshake 首包没有会话密钥**，此时 `16 signature` 应当是**空 byte string**（CBOR `0x40`）而不是空 text（`0x60`），更不是省略字段。这个区别只有一个字节，却是首包能不能被接受的分水岭（§4.4 会看到这两个字节）。

payload chunk 同理：`64 payload` 是 payload 明文（`sequencenumber` + `messageid` + `endofmsg` + `compressionalgo` + 业务数据）经 GZIP、AES-CBC 后的 encrypted envelope bytes，`16 signature` 是对这段 bytes 的 HMAC。

### 4.3 CBOR integer key：不是为了省事，是为了省字节

业务层看，一个 licensedManifest 请求就是一个 JSON。但在线路上，Android MSL 用的是 **CBOR，且几乎所有字段名都用整数键**表达。为什么？拿真实字节比一下就清楚了——同一个 `{messageid, timestamp}`：

```python
>>> import cbor2, binascii
>>> h = lambda b: binascii.hexlify(b).decode()
>>> h(cbor2.dumps({22: 4212345678, 24: 1723600123}))          # 整数键
'a2161a fb134b4e 18181a 66bc0cfb'          # 14 字节
>>> h(cbor2.dumps({"messageid": 4212345678, "timestamp": 1723600123}))  # 字符串键
'a2 696d6573736167656964 1afb134b4e 6974696d657374616d70 1a66bc0cfb'   # 31 字节
```

字符串键把 `"messageid"`、`"timestamp"` 的键名（各占 10 字节：1 字节长度 + 9 字节 ASCII）原样塞进每条消息，两个键就是 20 字节；整数键把这两个键压成 3 字节（`16` 表示 22、`18 18` 表示 24）。仅这两个字段，header 就从 31 字节降到 14 字节——**对一个每次播放都要发大量消息、字段高度固定的协议，整数键把 header 体积砍掉一半以上。** 代价是可读性归零，这也是为什么静态看混淆类名意义不大，必须拿到线上字节反查含义。

笔者逆向出的整数键映射如下（按位置分组，与上面的结构图一致）：

| Integer key | 含义 | 所在位置 |
|-------------|------|----------|
| `32` | mastertoken | header envelope |
| `33` | headerdata | header envelope |
| `16` | signature | header envelope / payload chunk / token |
| `64` | payload | payload chunk |
| `15` | tokendata | mastertoken / useridtoken 内部 |
| `6` | ciphertext | encrypted envelope |
| `7` | sha256 | encrypted envelope |
| `8` | keyid | encrypted envelope |
| `9` | iv | encrypted envelope |
| `20` | sender / ESN | 解密后的 headerdata |
| `22` | messageid | 解密后的 headerdata / payload |
| `24` | timestamp | 解密后的 headerdata |
| `14` | sequencenumber | 解密后的 headerdata / payload |
| `18` | useridtoken | 解密后的 headerdata |
| `36` | capabilities | 解密后的 headerdata |

### 4.4 两个最贵的类型错误：map 当成 bytes、text 当成 bytes

CBOR 的类型信息编码在每个字节的高 3 位（major type）。MSL 逆向里最耗时间的两个 bug 都能落到某一个字节上，这里各看一次真实字节。

**错误一：把 `headerdata` 内联成 map，而不是加密 envelope 的 bytes。** `33` 键（CBOR `18 21`）后面那个字节决定了值的类型：

```text
正确：  ... 18 21  58 55  a3 08 60 09 50 ...    ← 58 = byte string(len 0x55)，值是「密文字节」
错误：  ... 18 21  a3 08 60 09 50 ...           ← a3 = map(3)，服务端把它当明文 map，无法解密 → 502
```

`0x58`（major type 2，byte string）和 `0xa3`（major type 5，map）就差在高位。**服务端拿到 `33` 的值，第一步是当密文去解密；如果你给的是一个 map，它连解密都进行不下去。**

**错误二：该用 `bytes` 的地方用了 `text`。** Widevine challenge、token 的 `tokendata`、各处 `signature`，在线路上都必须是 byte string；一旦 base64 化后当字符串塞进去，major type 就从 2 变成 3：

```text
b'ABCD'  ->  44 41424344     ← 44 = byte string(4)
 'ABCD'  ->  64 41424344     ← 64 = text string(4)
b''      ->  40              ← 空 byte string（handshake signature 应该长这样）
 ''      ->  60              ← 空 text string（写成这样首包就废了）
```

同样只差高位那一个 bit（`0x40` vs `0x60`）。§4.2 说的「handshake signature 应是空 byte string」，落到字节上就是**必须是 `0x40` 而不是 `0x60`**。

有了这些，笔者后面修字段时形成了一个固定习惯：**不看「代码像不像」，只看两件事**——(1) CBOR decode 后的结构类型是否和官方客户端逐字节一致；(2) 服务端错误码是否从「解析失败」前进到「认证失败」或「业务失败」。这比在混淆 Java 类名里猜字段含义可靠得多。

---

## 五、Key Exchange：两条路，两种密钥归属

MSL 会话建立的第一步是 Key Exchange——它决定了 `Kenc` / `Khmac` 这两把在 §4.2 反复出现的会话密钥从哪来、以什么形式存在。这里有两条性质完全不同的路线。

![MSL Key Exchange 两条路线对比](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/key-exchange-compare.png)
*Web 路线用 RSA 软件密钥交换，session key 明文可见、调试简单；Android 路线用 Widevine challenge，密钥留在 CDM 内，只暴露 key id 和 CryptoSession 的操作能力。*

### 5.1 ASYMMETRIC_WRAPPED：密钥在客户端内存

Web / Rave / Chrome JSON 风格常见的是 `ASYMMETRIC_WRAPPED`：

1. 客户端生成 RSA-2048 keypair；
2. handshake 里放 RSA public key 作为 `keyrequestdata`；
3. Netflix 返回 `MasterToken`，以及用该公钥 RSA-OAEP 包裹的 JWK；
4. 客户端用 RSA 私钥解出 AES encryption key（`Kenc`）与 HMAC key（`Khmac`）；
5. 后续 header / payload 全部用这两把 key 加密签名。

优点很直接：**`Kenc` / `Khmac` 就在客户端内存里，明文可读，调试和重写都简单。** 缺点也明确：这条路线更接近 Web/Cadmium，能力上容易被服务端策略限制，代表不了真实 Android 播放链路。

### 5.2 WIDEVINE：密钥留在 CDM 内

Android / Hearo / MslClient 路线是 `WIDEVINE`：

1. `MediaDrm.openSession()` 创建 Widevine session；
2. `getKeyRequest()` 生成 key exchange challenge；
3. handshake 里放 `WIDEVINE_APPID` entity auth 和 `WIDEVINE` keyrequestdata；
4. Netflix 返回 `cdmkeyresponse`、`encryptionkeyid`、`hmackeyid`；
5. 客户端 `provideKeyResponse()`，会话密钥被安装进 CDM——**客户端拿到的是 key id，而不是密钥明文**；
6. 后续加密 / 签名 / 解密全部通过 `CryptoSession.encrypt()` / `sign()` / `decrypt()` 完成。

两条路线的本质差异就在第 4~5 步：`ASYMMETRIC_WRAPPED` 让你**持有密钥**，`WIDEVINE` 只让你**持有一个能调用密钥的 oracle**。不过 oracle 本身就是活的协议能力：如果攻击者控制了合法客户端并能任意调用 `encrypt/sign/decrypt`，即使拿不到 key bytes，也可能借设备完成请求。硬件不可导出主要降低克隆和离线复用风险，不会让已被控制的授权端点自动可信。

### 5.3 把 CDM 当能力，而不是当靶子

上面那句话是整个 Android 路线的关键抽象，值得单独点一下。传统逆向习惯把 CDM 当成必须攻破的黑盒；但在 MSL 这条链路里，更省力的姿势是把它当成一个**加密签名能力提供者**：

```text
我给它 plaintext + key id + IV   →   它给我 ciphertext     （§4.2 第 2 步）
我给它 encrypted envelope bytes  →   它给我 HMAC signature  （§4.2 第 4 步）
我给它 ciphertext + key id + IV  →   它给我 plaintext      （解密响应）
```

这不是「破解 CDM」，而是**复现官方客户端如何调用 CDM**。很多时候协议逆向根本不需要把密钥导出来——只要把「官方客户端做的事」封装成可控接口，就足够构造合法 MSL 消息。Hearo 的工程价值正在于此。唯一要注意的坑：`CryptoSession` 是 `AES/CBC/NoPadding`，明文需要**手动 PKCS7 padding** 到 16 字节边界，CDM 不会替你补。

---

## 六、Token 绑定：`mtserialnumber` 是一个字段等式

`MasterToken` 和 `UserIdToken` 不是能复制粘贴的字符串，这句话如果只当口号就没用；落到字节上，它是一组**服务端会逐字段校验的等式关系**。

两个 token 的结构都是 `{15: tokendata, 16: signature}`，其中 `tokendata` 是一段 bytes，decode 后各含若干字段：

```text
MasterToken.tokendata = {
    renewalwindow, expiration,
    sequencenumber,                 # MasterToken 更新序号，主要约束克隆/回滚
    serialnumber      = S,          # ← 会话的唯一编号
    sessiondata                     # 服务端用 MSL 主密钥加密，客户端读不到
}                                   #   （Kenc/Khmac 本体在这里，但对客户端不透明）

UserIdToken.tokendata = {
    renewalwindow, expiration,
    mtserialnumber    = S,          # ← 必须等于上面的 serialnumber
    serialnumber,
    userdata                        # 加密的用户身份
}
```

**服务端校验的核心等式是 `UserIdToken.mtserialnumber == MasterToken.serialnumber`。** 这一个等式把用户 token 绑定到特定 master token 家族，能够阻止把不同上下文的 token 随意拼接。它本身并不阻止攻击者重放一整组仍有效的 `MasterToken + UserIdToken + 合法消息`；后者还依赖 `nonreplayableid` 的接收状态、token 时效、撤销和业务幂等性。一旦 `Kenc`/`Khmac`、`MasterToken`、`UserIdToken`、cookie、endpoint 来自不同上下文，就会出现看似玄学、其实是绑定断裂的 `106039` / `205032` / `502`（§七）。

另一层绑定在密钥上：`sessiondata` 由服务端加密，客户端不能从 `MasterToken` 直接读出会话密钥；在本文观测的 `WIDEVINE` 路线中，应用层只得到 key id 和 CryptoSession 能力。客户端通过对应会话密钥或 crypto capability 证明自己掌握该 token 的能力——token 是服务端签发的不透明凭证，证明能力与 token 必须来自同一次会话。理解这一点，token 相关的错误就从“玄学”变成“查绑定”。

---

## 七、licensedManifest：一次把前面全部串起来的验收点

Netflix 的 `licensedManifest` 很适合当协议逆向的验收点，因为它要求前面每一环都对：MSL session 已建立、`MasterToken` 可用、用户态 cookie 或 `UserIdToken` 可用、headerdata/payload 能正确加密签名、Widevine challenge 格式正确、返回响应能解密解压解析——**任何一环错，它都会失败在不同的错误码上**。

![licensedManifest: 清单与 License 合并链路](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/licensed-manifest-flow.png)
*licensedManifest 把 Manifest 参数与 Widevine license challenge 合进同一个 MSL payload。服务端返回加密响应，里面同时包含 tracks、CDN stream 和 license 信息。*

它的特别之处在于把两件事合并进**同一个 payload**：既要 Manifest（清单、码率、CDN 地址），又要 license（内容密钥授权）。业务层 payload 的结构大致是（占位值）：

```jsonc
{
  "method": "licensedManifest",
  "viewableId": ****,
  "profiles": ["playready-h264mpl30-dash", "hevc-hdr-main10-...", "..."],
  "drmType": "widevine",
  "challenges": { "default": "<base64: Widevine license challenge>" }  // 线上转 bytes
}
```

注意 `challenges.default` 在业务 JSON 里是 base64 文本，但打进 MSL payload 前必须还原成 **byte string**（§4.4 的错误二在这里最常犯）。整个实现分 5 步，正好复用前面所有结论：

1. 用 CDM 生成 Widevine license challenge；
2. 构造 `licensedManifest` payload（上面的结构）；
3. 构造 headerdata：`20 sender`、`22 messageid`、`24 timestamp`、`14 sequencenumber`、token、`36 capabilities`；
4. CBOR 编码 → GZIP → AES-CBC 加密 → HMAC 签名（§4.2 的 encrypt-then-MAC）；
5. 解密响应，解析 `video_tracks` / `audio_tracks` / license response。

这条链路给了笔者一个清晰的验收标准：**只做成 Key Exchange 还不算真懂 MSL；只有 licensedManifest 能稳定解密，协议兼容模型才算闭环。** 但这个成功只证明客户端在当时的账号、设备和服务端策略下完成了一次受授权交互，不证明防重放、撤销、硬件防克隆或内容输出保护已经被绕过。这里的「解析」是实验室验证——确认协议链路可解释、响应结构可解析，不涉及任何真实账号或内容密钥材料。

---

## 八、动态抓包与错误码定位

Netflix Android APK 混淆很重，静态看类名意义不大。笔者主要靠 Frida 在运行时抓三类对象，把「混淆类字段」映射回「MSL wire field」。

### 8.1 同一条请求，对象层和字节层一起抓

关键是**同一条请求要同时抓对象层和字节层**：只抓对象层，不知道编码时发生了什么；只抓 SSL bytes，又不知道哪个字段来自哪个 Java 对象。两者对齐，才能反查。

| Hook 点 | 目的 | 产出 |
|---------|------|------|
| MessageHeader serialize | 同时抓 `MasterToken` / `UserIdToken` / headerdata | token 结构和绑定关系 |
| `MediaDrm.CryptoSession` | 观察 encrypt / sign / decrypt 的输入输出 | 确认 padding、key id、签名输入 |
| OkHttp / Cronet / SSL_write | 抓 HTTP body 原始 bytes | 最终 wire format 样本 |

以 `CryptoSession` 为例，把它的输入输出打出来，就能直接验证 §4.2 的 encrypt-then-MAC——签名输入到底是明文还是那段 envelope bytes：

```javascript
// 观察官方客户端如何调用 CDM 做加密与签名（示意）
var CS = Java.use('android.media.MediaDrm$CryptoSession');
CS.encrypt.implementation = function (keyId, input, iv) {
    var out = this.encrypt(keyId, input, iv);
    console.log('[encrypt] in.len=' + input.length +
                ' iv=' + hex(iv) + ' out.len=' + out.length);  // 观察是否 NoPadding
    return out;
};
CS.sign.implementation = function (keyId, message) {
    // message 是 headerdata 明文？还是 encrypted envelope bytes？— 抓下来一比就知道
    console.log('[sign] msg[0..1]=' + hex(message).slice(0, 4) +
                ' len=' + message.length);
    return this.sign(keyId, message);
};
```

抓下来发现 `sign` 的入参前两字节正是 encrypted envelope 的 `a3 08…`（map 开头），而不是明文 headerdata——这就从字节上确认了「签的是密文」。

### 8.2 错误码是定位仪，不是终点

协议逆向里，错误码不是失败提示，而是告诉你「卡在哪一层」的定位仪：

| 现象 | 常见含义 | 下一步 |
|------|----------|--------|
| HTTP 502 | 服务端无法解析或会话状态不接受 | 看 CBOR 结构、endpoint、压缩、签名输入（§4） |
| `204035` | Widevine key request 不符合预期 | 检查 `getKeyRequest()` 的 mimeType / keyType / initData（§5.2） |
| `106039` | token 与 session key 不匹配 | 检查 MasterToken、UIT、AES/HMAC 是否来自同一会话（§六） |
| `205032` | cookie / entity session 绑定不匹配 | 不要把 Web cookie 与 Android MSL session 强行拼接（§六） |

一开始看到 502 会很挫败，因为它不像 JSON error 那样友好。但当手里有官方 bytes 对照时，502 反而在告诉你：服务端还没进入业务层，问题仍在 envelope、CBOR 或签名——也就是仍在 §四 的范围内。

### 8.3 一个固定的排查顺序

把上面所有结论压成一个调试闭环，就是笔者项目后期的标准工作流。

![MSL 逆向调试闭环](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/debug-loop.png)
*Hook 官方 App 得到真实对象和 wire bytes；自研客户端复现；对 CBOR 结构做 field diff；根据错误码修正类型、顺序、padding；直到服务端返回可解密响应。*

排查顺序**从外层往内层**走，避免在错误的层乱修（比如 token 不匹配却去改 profiles，或 payload 参数错却去怀疑 HMAC）：

```text
HTTP 层通了吗（§二）
  ↓
MSL 顶层对象能 decode 吗（§4.1）
  ↓
headerdata / payload 是 bytes 还是 text（§4.4）
  ↓
HMAC 输入是密文 envelope 还是明文（§4.2）
  ↓
AES padding 是否一致（§5.3）
  ↓
MasterToken / UIT / session key 是否来自同一会话（§六）
  ↓
业务 payload 参数是否正确（§七）
```

---

## 九、两个实现的分工

最终笔者保留了两个实现，因为它们解决的问题不同，互相校验。

| 实现 | 适合场景 | 优点 | 缺点 |
|------|----------|------|------|
| **nfmsl.py** | 协议研究、字段 diff、快速验证 | 单文件、可打印每层中间结构、适合二分错误 | 行为不像真实客户端，维护成本高 |
| **Android MslClient** | 复现真实 MediaDrm / CryptoSession 路径 | 接近官方链路，可验证 CDM oracle 模型 | 调试慢，需要设备和 Frida 环境 |

分工很清晰：`nfmsl.py` 负责**协议建模**（每一层都能打日志：CBOR bytes、HMAC 输入、payload JSON、response chunk，快速回答「字段是不是错了」）；`MslClient` 负责**设备能力验证**（用真实 MediaDrm，把加密签名交给 CryptoSession，回答「官方设备路径是不是这样工作」）；Frida 负责把官方客户端当 oracle 提供 ground truth。三者组合，比单独维护一个大而全的客户端更稳——因为任意一层出错时，另一层就是对照答案。

---

## 十、安全性评估：MSL 能保护什么，不能保护什么

前面的逆向工作回答了“消息怎样构造”；安全评估还要回答另一个问题：**在什么攻击者模型和部署配置下，这些结构能提供哪些保证？** Netflix 开源文档把 MSL 定义为可扩展框架，明确要求应用自行选择 entity authentication、user authentication、Key Exchange 以及每条消息是否加密、认证和不可重放。因此，不能脱离配置只给“MSL 协议”一个统一安全等级。

### 10.1 按安全属性拆解

| 安全属性 | MSL 能提供的机制 | 独立强度 | 关键条件与边界 |
|----------|------------------|----------|----------------|
| **应用数据机密性** | 对 headerdata 和 payload 加密 | 高（条件成立时） | 必须要求 encryption；密钥交换和端点不能被攻破；长度、时序、IP 等元数据仍可见 |
| **消息完整性与实体认证** | 对 encrypted envelope 做 HMAC/签名 | 高（条件成立时） | 必须先 verify 后 decrypt，并严格限制可接受的认证与签名方案 |
| **用户上下文绑定** | `UserIdToken` 绑定 `MasterToken.serialnumber` | 中到高 | 防止跨会话拼接，不等于用户授权永远有效；仍依赖登录、撤销和业务策略 |
| **不可重放性** | `messageid`、可选 `nonreplayableid`、接收窗口 | 中（有状态） | 高价值消息必须显式请求 non-replayable；服务端要保存窗口并处理并发、乱序和回绕 |
| **设备/实体不可克隆性** | entity authentication + Key Exchange + proof of possession | 低到高，取决于配置 | 软件 RSA 路线和硬件 Widevine 路线不是同一强度；L3 也不能等同于硬件根 |
| **会话密钥机密性** | key wrapping、CDM key id/CryptoSession | 中到高，取决于端点 | 不可导出可降低离线复制，但受控客户端仍可能滥用 crypto oracle |
| **前向保密** | 取决于所选 Key Exchange | 不保证 | MSL 框架支持多种方案，不能从 `MasterToken` 或 AES/HMAC 本身推出 PFS |
| **授权与反滥用** | token 可承载身份和上下文 | 低：只提供输入 | 账号权限、地区、并发、码率、风控和撤销都属于服务端策略 |
| **内容密钥与明文输出** | MSL 只保护 license/控制面运输 | 低 | content key 与 sample/frame 安全由 DRM、CDM、解码器和输出链负责 |
| **可用性** | 无直接保证 | 可能为负 | 多层解析、压缩、状态同步和续期会增加 DoS、误拒绝与恢复复杂度 |

最重要的结论是：**MSL 的强项是应用消息保护和上下文绑定，不是让受授权客户端失去协议能力。** 如果攻击者已控制一个合法端点，他可能不需要导出会话密钥，只需借该端点完成加密、签名和解密。此时防线会自然转移到服务端授权、频率控制、设备证明和结果可复用性上。

### 10.2 信任根与爆炸半径

一套 MSL 部署至少包含四个独立信任边界：

1. **客户端应用与 CDM**：应用可构造业务请求，CDM 可能只提供不可导出的 crypto capability。若调用权限没有绑定合法状态，“key 不可导出”仍挡不住 oracle abuse。
2. **MSL 端点与业务服务**：MSL 验证成功只说明消息来自掌握相应能力的实体，不代表业务请求一定被授权。业务服务仍须重新校验用户、内容、地区、设备等级和输出策略。
3. **token 签发方与 trusted services**：开源 MSL 的 trusted-services 模型允许多个服务共享保护 `MasterToken`/`UserIdToken` 的签发密钥。这样能减少会话存储，但也扩大密钥泄露的影响范围，必须依赖 HSM/KMS、用途分离、版本化和轮换。
4. **MSL 与 TLS**：MSL 可在 TLS 终止之后继续保护 payload，但 TLS 仍负责服务器身份、传输元数据保护、抗主动网络降级以及大量成熟的连接层防护。两者是叠加关系，不是替代关系。

因此，`MasterToken` 的“自包含”是性能和分布式架构优势，同时也是密钥治理责任：谁能解封 sessiondata，谁就进入了高价值信任域。生产评估必须记录签发密钥存放位置、访问主体、轮换周期、旧版本接受窗口和泄露后的吊销路径。

### 10.3 主要攻击面与失效模式

| 攻击面 | 典型失效 | 安全后果 | 评估重点 |
|--------|----------|----------|----------|
| **方案协商/配置** | 为兼容旧设备接受弱 entity auth 或 Key Exchange，错误后降级 | 强客户端被引导到弱路径 | 按实体类型做 allowlist；失败不得自动降低安全属性 |
| **端点与 oracle** | root、注入或恶意自动化调用合法 `CryptoSession` | 不导出 key 也能批量构造合法消息 | 调用是否绑定前台会话、账号、设备证明、速率和 nonce |
| **重放状态** | 只校 `messageid`，未要求/持久化 `nonreplayableid` | 有效操作被重复执行 | 窗口键是否含 entity + token serial；并发、乱序、跨机一致性 |
| **token 生命周期** | 过长有效期、撤销传播慢、允许旧 sequence 续期 | 被盗会话长期可用或可回滚 | renewal/expiration、anti-cloning 窗口、强制重新认证条件 |
| **签发密钥** | trusted services 共享密钥泄露或用途混用 | 可伪造/解封大批 token，爆炸半径大 | HSM/KMS、密钥分域、版本化、审计、应急轮换 |
| **CBOR/JSON 解析** | 深层嵌套、超大 byte string、整数边界、重复键 | 解析差异、内存耗尽、认证绕过 | canonical policy、大小/深度限制、跨语言差分 fuzzing |
| **压缩与分块** | 解压炸弹、chunk 重排/截断、提前消费部分业务对象 | DoS 或应用接受不完整数据 | 解压上限、逐块验签、messageid/sequence/endofmsg 完整校验 |
| **密码实现** | IV 重用、验签后置、密钥用途混用、非恒时比较 | 明文泄漏、padding oracle、伪造风险 | CSPRNG、key separation、verify-before-decrypt、统一失败语义 |
| **错误与遥测** | 细粒度错误码、时序和重试差异可稳定分类 | 为字段、token、账号状态提供 oracle | 对外错误收敛；内部日志脱敏并限制访问 |
| **流量元数据** | endpoint、消息大小、频率和时序仍可观察 | 行为识别、内容/操作推断 | padding/batching 的收益与成本，避免宣称“端到端完全不可见” |

这里最容易被低估的是解析器：MSL 消息需要经过外层对象、token、cipher envelope、压缩 payload 和业务 JSON/CBOR 多层处理。认证应尽可能在昂贵解析和解压前完成；所有认证后的业务数据仍然是不可信输入，不能因为“来自合法 MSL 会话”就跳过 schema、长度和权限校验。

### 10.4 `encrypt-then-MAC` 的保证与前提

本文观测到的 `AES-CBC + HMAC-SHA256` 采用 encrypt-then-MAC，方向是合理的：接收方先验证覆盖 envelope bytes 的认证标签，再进行 CBC 解密，可以避免把未认证密文直接送入 padding/parser 路径。Netflix 开源 MSL 接收流程也明确要求 **verify before decrypt**。

但算法名称本身仍不足以证明安全，至少要同时满足：

1. `Kenc` 与 `Khmac` 独立，不把同一 key 跨用途复用。
2. 每次 CBC 加密使用不可预测且不重复的随机 IV。
3. HMAC 覆盖实际传输的完整 envelope，包括 `keyid`、IV 与 ciphertext 的确切编码。
4. tag 比较不泄漏可利用的时间差，认证失败不进入解密和详细错误分支。
5. 接收端拒绝未知算法、未知 envelope 版本和不符合策略的空签名，而不是做宽松兼容。

MSL 开源文档还允许不同 cipher/authentication 方案，因此上面的结论只能描述本文观察到的 Netflix 实例，不能泛化为所有使用 MSL 框架的应用。

### 10.5 建议采用四级判定

| 等级 | 可验证条件 | 评价 |
|------|------------|------|
| **M0：线格式兼容** | 能编码、签名并得到服务端响应 | 只证明协议复现，不构成安全证明 |
| **M1：受保护消息** | 强制加密与完整性，严格验签后解密，弱方案不可协商 | 能抵抗网络观察和消息篡改 |
| **M2：上下文绑定会话** | entity/user/token 绑定、非重放状态、撤销、续期和业务授权均生效 | 能显著限制 token 拼接、重放和跨上下文滥用 |
| **M3：硬件协同实体** | 会话能力绑定硬件证明，具备 anti-rollback、不可克隆密钥与服务端风险控制 | 降低设备克隆和规模化自动化；仍不等于内容明文不可见 |

本文的实验已经充分达到 **M0**，并给出了 M1 的线格式证据和部分 M2 的结构/行为证据；Widevine API 路径只说明存在向 M3 设计的可能，不能证明当前测试环境达到硬件级绑定。准确评级还需验证：同一消息重放是否被拒、跨节点重放窗口是否一致、旧 token/sequence 能否续期、不同设备等级接受哪些 scheme、撤销传播时间，以及被控 oracle 能否跨账号或跨内容复用。

### 10.6 综合结论

从安全工程角度，MSL 是一套**配置驱动的应用层安全框架**。它相对“HTTPS + bearer token”的主要增益，是把业务 payload、实体能力、用户 token 和会话密钥放进同一个密码学上下文，降低 token 单独泄露后的利用价值，并让消息保护跨连接、跨服务节点延续。

它不能独立保证：

- 所有 MSL 配置都绑定硬件设备身份
- 所有消息都启用了严格不可重放语义
- 服务端无状态、无撤销和风控依赖
- 受控合法客户端无法滥用 CDM/CryptoSession oracle
- content key、解密 sample 或最终视频帧不可观察
- 使用了公开 spec 就自动获得形式化或可证明安全

所以，对本文所分析技术的严谨评价应是：**协议结构提供了可靠的纵深防御原语，实际强度由认证方案、Key Exchange、端点安全、状态管理和业务授权共同决定。** 它提高的是凭据拼接、跨设备复制和规模化重放的成本，而不是把本地受控端点变成可信环境。

> 一手资料：[Netflix MSL Framework](https://github.com/Netflix/msl)、[Application Security Requirements](https://github.com/Netflix/msl/wiki/Application-Security-Requirements)、[Messages / Non-Replayable ID](https://github.com/Netflix/msl/wiki/Messages)、[Regular Messages / Verify Before Decrypt](https://github.com/Netflix/msl/wiki/Regular-Messages)、[MSL Networks](https://github.com/Netflix/msl/wiki/MSL-Networks)。

---

## 十一、横向对比：MSL 强在哪，代价是什么

把 MSL 放回行业里对比，才看得清它到底买到了什么。下面依次对照四类：主流国外流媒体、国内长视频三家（爱奇艺 / 腾讯视频 / 优酷）、国内音乐 App，最后用一张谱系图和一张表收口。

贯穿全节的其实只有一根轴——**信任边界画在哪**。先把结论画出来，后面再逐类展开：

![内容保护的信任边界谱系](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/trust-boundary-spectrum.png)
*同一根轴上，信任边界从「文件」一路移到「实体密码学能力」。越往下，单点凭据泄露能撬动的杠杆通常越小。本文观测到的 Netflix Widevine 路径把 manifest / 授权纳入了可与 CDM 能力绑定的信封；绑定是否达到硬件级仍取决于设备和 Key Exchange 配置。*

### 11.1 vs 主流流媒体：差别在「信任边界画在哪」

许多基于 HLS/DASH + Widevine/PlayReady/FairPlay 的流媒体可以抽象为**两段式**；具体平台和客户端版本可能叠加额外的请求签名、设备证明或应用层信封，下面只做架构模型比较，不把它当作各厂商当前生产配置的实测结论：

- **控制面**（manifest、码率、CDN 地址、播放授权）走 **HTTPS + bearer token**（OAuth/cookie/JWT）；
- **密钥面**（内容密钥）走 **EME → CDM** 的标准 DRM license 交换。

这套组合能让 TLS 保护传输、DRM 保护内容密钥，但如果控制面只使用 bearer token，token 的复制风险就会成为独立攻击面。本文观测到的 Netflix 路径进一步把控制面放入 MSL 信道，使请求还需要证明持有对应会话能力；在 Widevine 硬件路径上，这种能力可以进一步绑定设备，但不能把所有 MSL 路线一概称为硬件身份。

| 维度 | 主流方案（HLS/DASH + DRM） | Netflix MSL |
|------|---------------------------|-------------|
| 控制面（manifest/授权） | HTTPS + bearer token，明文 JSON | 塞进 MSL：加密 + 认证 + 实体/session 绑定 |
| 信任边界 | 若仅用 bearer token，则主要落在 token 上 | 落在 MSL 实体 + 会话能力上；Widevine 路径可进一步设备绑定 |
| 密钥来源 | 标准 EME/CDM license 交换 | 自带 Key Exchange，可由 CDM 驱动（§五） |
| 抗 TLS 拦截 | 依赖 TLS；代理装了根证书就能读控制面 | 控制面在 MSL 里仍是密文 |
| manifest 与 license | 两次独立请求 | `licensedManifest` 合并进一个授权信封（§七） |
| 单点泄露后果 | 仅有 bearer token 的设计可能允许重放控制面 | 单独 token 通常不足；还需 session key 或可调用的实体能力，具体取决于配置 |
| 代价 | 实现简单、标准化、生态成熟 | 协议复杂、需自研客户端栈、维护成本高 |

要公平地说，采用标准 HLS/DASH + DRM 往往是生态、兼容性与维护成本的工程取舍。MSL 用额外协议复杂度换取应用层上下文绑定和纵深防御，这笔投入是否划算取决于内容价值、设备规模与反滥用压力。还应避免把“有公开 spec”写成“可证明安全”：MSL 文档明确说明实际安全属性由应用选择的认证、Key Exchange 和消息策略决定。

### 11.2 vs 国内长视频三家：同一条演进路线，爬到了不同高度

国内长视频三家（爱奇艺、腾讯视频、优酷）既不能和音乐归一档，也不能三家归一档。为把这一节写准，笔者交叉核对了三家的公开资料与社区逆向工作——**凡无法一手证实的，都在下文明确标注为推断**（各平台官方从未公开逐清晰度的 DRM 矩阵，容器内部常量在中文资料里多互相矛盾，这里一律不引用）。看下来的图景是：三家走的是同一条演进路线，但爬到的高度不同。

**第一代（历史）：私有容器 + 弱加密/伪加密。** 这一代的"加密"主要是容器混淆 + 私有解码器封锁，挡普通用户、挡不住确定性攻击者：

- **优酷 `.kux`**：本质是分段重排的 FLV 容器（大头部区 + 256KB 对齐分段、内层仍是标准 FLV）。社区转换工具几乎都直接调用优酷自带的 `ffmpeg` 做 `-c copy` **流拷贝、零转码**——这等于证明底层码流**根本没做密码学加密**，只靠"标准播放器不认这个容器"设卡，强度≈0。
- **爱奇艺 `.qsv`**：文件头以 ASCII `QIYI VIDEO` 开头、约 90 字节头部 + 加密索引区，**每个分片只有前 1KB 被轻量加扰**，其余原样。社区工具离线 remux 即可还原。但要注意这是**历史方案**——爱奇艺客户端 v10+ 下载的 QSV 已换新加密，老工具失效。
- **腾讯 `.qlv`（早期缓存 `.tdl`）**：更早的分段缓存甚至是明文，`copy /b` 直接拼接就能还原；`.qlv` 是加密容器，但商业转换器只对特定旧客户端版本有效。保护随版本明显增强。

**第二代（现状）：服务端签名授权 + 商业/自研 DRM。** 三家都上了真 DRM，但侧重差别很大：

- **爱奇艺——投入最重的一家。** 据其 DRM 团队公开文章，架构是"两条腿"：自研 **iQIYI DRM-S**（Native Code 实现）+ **MultiDRM**（集成 Widevine / PlayReady / FairPlay / Intertrust）。设备侧**硬件级（TrustZone / TEE 里的 DRM TrustApp）与软件级（白盒密码 + 代码混淆）并存**，按设备能力选择。它 2018 年通过 ChinaDRM 实验室认证（国内首个），自研 DRM-S 还过了 Riscure、Farncombe 认证。"安卓只给 720P、iPhone 给更高清"的争议，技术根因正是硬件级 DRM（如需 TEE 的 Widevine L1）在不同设备上的可用性差异。
- **腾讯视频——ChinaDRM 自研派。** 消费端主线是自研 **ChinaDRM（遵循 ChinaDRM 2.0 参考实现）**，2020 年由其点播平台负责人公开介绍。Web 端的密钥链路值得单独看：客户端用 **WASM 生成 `cKey` 请求签名** → 服务端 `getvinfo` 接口鉴权后下发播放地址 → DRM license 服务器下发解密密钥。**注意 `cKey` 是请求防篡改签名，不是内容密钥**——这条链路笔者在 §十二单独拆开讲。（另外要区分：腾讯云对外卖的商业级 DRM 是 B2B 产品，≠ 腾讯视频 App 内部方案。）
- **优酷——更靠"签名 URL"的一家。** 自家内容大量走"服务端授权 + 时效签名 CDN URL"（UPS 接口 + `ckey` 签名 + 会员 cookie），多数分段是明文，所以 you-get 这类工具能长期抓到——它的护城河是那个反复加固、频繁失效的**签名算法**，而非内容加密本身。真·商业 DRM 走阿里云 VOD 能力（官方支持 HLS-AES-128 / 阿里私有加密 / Widevine + FairPlay），但优酷 App 具体用哪种、覆盖哪些清晰度**未公开**。阿里是 ChinaDRM 标准的参编方，但"优酷线上已部署 ChinaDRM"无一手证据。

有两点必须诚实说明，否则容易高估“国内视频已被破解”的程度：一是**社区那些 `you-get`/`iqiyi-parser`/`webvideo-downloader` 多是“走下发接口拿播放流”的下载器/解析器，而不是对已加密文件的离线解密器**；二是拿到软件级 L3 或签名授权结果，不能推出硬件 L1/TEE 已失守，后者需要独立的 TEE、驱动或受保护输出链攻击证据。

那么，爬到第二代最高处的国内长视频（爱奇艺 DRM-S、腾讯 ChinaDRM），和 MSL 还差在哪？差在**信任边界的覆盖范围**：它们把**密钥 / license 层**做成了设备绑定（TEE），但控制面（manifest、授权接口）仍是 **HTTPS + token + 客户端签名**。腾讯的 `cKey` 是国内最接近 MSL 的一例——客户端用 WASM 对请求做密码学签名——但它终究是**对一个明文 HTTPS 请求的防篡改签名**，不是把整条控制面塞进一个设备绑定、密钥来自 DRM 的**加密信封**。所以 §二 那句"偷 token 不够、控制面也要设备身份"，对国内长视频（哪怕用了 ChinaDRM / DRM-S 的那部分）依然是 MSL 独有的性质。

| 平台 | 历史容器 | 现状 DRM 主线 | 设备绑定 | 控制面签名/授权 |
|------|----------|---------------|----------|-----------------|
| **爱奇艺** | `.qsv`（每段前 1KB 弱加扰） | 自研 DRM-S + MultiDRM，过 ChinaDRM/Riscure/Farncombe 认证 | 硬件 TEE + 白盒并存 | HTTPS + token（+ 签名） |
| **腾讯视频** | `.qlv` / 早期 `.tdl`（明文可拼接） | 自研 ChinaDRM 2.0 | 分级：硬件 L1 / 软件 L3 | `cKey`(WASM 签名) + `getvinfo` 鉴权 |
| **优酷** | `.kux`（伪加密，`-c copy` 即还原） | 阿里云 VOD DRM（覆盖未公开），多数走签名 URL | 弱（token + 软设备指纹），L1 无证据 | UPS 接口 + `ckey` 签名 |
| **Netflix** | —（本文讨论在线流式路径） | Widevine + MSL Key Exchange | 实体/会话能力；部分设备可硬件绑定 | 本文观测路径将控制面放入 MSL 信封 |

> 主要依据：爱奇艺 DRM 团队《修炼之路 / 探索之路》及其官方认证公告、ChinaDRM 实验室资料、腾讯 2020 ChinaDRM 研讨会公开表态，以及 [you-get](https://github.com/soimort/you-get)、[bbtsdecrypt](https://github.com/ReiDoBrega/bbtsdecrypt) 等社区逆向项目。凡涉及各平台"具体用哪种 DRM / 逐清晰度映射 / 是否强制 L1"处，均为行业推断而非官方定论。

### 11.3 vs 国内音乐加密：从「文件级静态加密」到「会话级活协议」

和国内主流音乐 App 的加密方案（QQ 音乐 `.qmc`/`.mflac`、网易云 `.ncm`、酷狗 `.kgm` 等）一比，差距就不是「架构取舍」而是「代际」了。核心区别只有一句：**国内方案是「带客户端内置密钥的文件级加密」，MSL 是「无内置密钥的会话级协议」。**

以文档最清楚的网易云 `.ncm` 为例，它的解密根本不需要联网：

```text
.ncm 文件结构（公开已知，unlock-music / ncmdump 均已实现）：
  magic "CTENFDAM"
  ├─ RC4 密钥块：每文件一个 key，但这个 key 用
  │   硬编码 AES-128-ECB key  "hzHRAmso5kInbaxW"  解出   ← 密钥在客户端里
  ├─ 元数据块：JSON，用另一个硬编码 AES key 解出
  └─ 音频块：用上面的 per-file key 生成 RC4 keystream 异或

=> 两把 AES 主密钥都是客户端里的固定常量。逆向一次，
   此后所有 .ncm 文件都能【离线、永久、批量】解密。
```

QQ 音乐的 `.qmc`（旧版固定 XOR mask 表）/`.mflac`（文件尾附 per-file key，但派生算法在 native 库里）、酷狗的 `.kgm`（固定掩码表）本质完全一样：**密钥或密钥派生算法内置在客户端**，威胁模型只到「提高翻拷门槛」，挡不住确定性攻击者。一旦有人把客户端逆一遍，整个格式对所有人失效。

MSL 在这几点上是结构性的强：

| 维度 | 国内音乐加密（.ncm/.qmc/.kgm） | Netflix MSL |
|------|-------------------------------|-------------|
| 密钥归属 | 主密钥/算法**内置客户端**，可一次性提取 | MSL 会话密钥动态建立；Widevine 路线中应用通常只持 key id/调用能力。内容密钥仍由 DRM 单独管理 |
| 联网要求 | 解密**完全离线**，与服务端无关 | 每次播放要**活的、被授权的**服务端往返（`licensedManifest`） |
| 单次逆向的后果 | 得到**通用离线解密器**，一劳永逸 | 通常得到协议复现或端点调用能力，仍受账号、session、设备和服务端策略约束；是否可规模化需实测 |
| 撤销 / 灰度 / 限速 | 无（文件发出去就管不了了） | 服务端可**实时拒绝、撤销、限速、指纹识别** |
| 设备绑定 | 无，文件 + App 即可 | Widevine 路线可绑定 provision 设备；强度随 L1/L3 和实现变化 |
| 安全范式 | security through obscurity，逆一次即崩 | 配置驱动的安全协议，可轮换、可升级；是否硬件绑定并非协议固定属性 |

一句话：**静态文件加密的典型失败是一次提取后形成通用离线解密器；MSL 路径的典型结果则是获得协议复现或活端点调用能力，后续仍需通过服务端授权。** 这提高了跨账号、跨设备和长期复用的成本，但是否存在可复用能力仍要按具体 Key Exchange 与端点安全实测，不能先验断言“绝无万能钥匙”。

不过同样要说公平话：两者**在解不同的题**。静态音乐文件强调下载和离线可用性；本文讨论的 Netflix 在线路径强调按次授权和持续服务端参与。Netflix 部分客户端也支持离线下载，但下载包仍由 DRM、设备和 license 生命周期约束，并不是直接交付无保护文件。所以这不是“谁会不会做”的问题，而是产品形态、离线需求与威胁模型决定了防护边界。

### 11.4 一张表看清整个谱系

把开头那张谱系图落成文字，就是下面这张表。真正变化的是**信任边界画在哪**：从文件、bearer token 到实体/session 能力，单一凭据泄露的可复用范围通常逐步缩小，但具体强度仍由部署配置决定：

| 方案 | 密钥归属 | 控制面信任边界 | 单次逆向的后果 |
|------|----------|----------------|----------------|
| 国内音乐 `.ncm`/`.qmc` | 客户端内置静态密钥 | 无（离线文件） | 通用离线解密器，一劳永逸 |
| 国内长视频自研 `.qsv`/`.qlv`/`.kux` | 服务端下发 + token 校验 | token（bearer） | 逆外壳 + 持 token 即可取流 |
| 国外 HLS/DASH + Widevine/FairPlay | 标准 CDM license 交换 | token（bearer） | 持 token 重放控制面，密钥仍需 CDM |
| ChinaDRM / 硬件 Widevine | license，TEE/CDM 保护 | token（bearer） | 需破 TEE + 有效授权 |
| **Netflix MSL（本文观测的 Widevine 路径）** | 动态 Key Exchange，能力可留在 CDM | **MSL 实体/会话能力，可设备绑定** | 通常仍需有效授权与活端点；可复用程度取决于设备等级和服务端策略 |

这些方案分布在谱系的不同位置，但不能只凭产品名称给真实部署定级。**本文能直接支持的 MSL 结论，不是“行业唯一”或“天然硬件级”，而是它能够把控制面放进与实体、用户和 session 绑定的密码学上下文；在 Widevine 硬件路径中，这个上下文还可以进一步绑定设备能力。**

---

## 十二、拆一个最接近 MSL 的国内例子：腾讯视频 cKey 的 WASM 链路

> 上一节说腾讯的 `cKey` 是「国内最接近 MSL 的一例」。这一节把它单独拆开——但先划清边界：**可观测的调用链和参数是清楚的（社区逆向有一手结论），WASM 内部那段混淆算法则是黑盒**，本文不臆造它的具体常量或密码结构。真正想让读者带走的，是它和 MSL 在「签什么」上的本质差异，以及为什么这里同样是「抓边界」比「读混淆」值。

### 12.1 可观测的调用链

腾讯视频 Web 端要拿到播放地址，得先过 `cKey` 这道请求签名。据社区逆向（onejane、ZSAIm 等，RE 观测，中-高置信），链路是这样的：

![腾讯视频 cKey 的 WASM 生成与校验链路](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/ckey-wasm-chain.png)
*JS 胶水层收集参数 → 调 WASM 里的 `getckey` 算出 cKey → 拼进 `getvinfo` 请求 → 服务端校验通过才下发播放地址；DRM 内容再单独走 license 拿解密密钥。cKey 只签「请求」，不碰「内容密钥」。*

拆成文字，关键是三段：

1. **参数收集（JS 层）**：JS 侧凑齐一组入参，社区观测到的 `getckey` 签名形如 `getckey(platform, appVer, vid, "", guid, tm)`：

   | 参数 | 含义 | 备注 |
   |------|------|------|
   | `platform` / `appVer` | 端标识、客户端版本 | 决定签名分支 |
   | `vid` | 视频 ID | 绑定具体内容 |
   | `guid` | 设备/用户标识 | 取不到时随机生成 32 位 |
   | `tm` | 时间戳 | 防重放，服务端校时效 |
   | `""` | 空占位 | 版本演进留下的坑位 |

2. **WASM 计算（黑盒）**：JS 通过 Emscripten 的 `cwrap("getckey","string",[...])` 把参数喂进 WASM 模块，里面做参数/时间戳校验、拼装、摘要/加密，吐出 `cKey` 字符串（当前 `encryptVer=9.1`；更早的 `8.1` 曾是纯 JS 的函数 `a()`，后来才搬进 WASM 加固）。**这段逻辑是混淆的，本文不展开其内部——因为对理解「它和 MSL 差在哪」没有必要。**

3. **服务端校验（getvinfo）**：`cKey` 连同 `guid`、`logintoken` 等拼进对 `vd.l.qq.com/proxyhttp` 的 `getvinfo` 请求；服务端先验签防篡改、再校会员态，通过才返回 `vinfo`（含播放地址 `url`+`pt`）。若是 DRM 内容，解密密钥再由 license 服务器单独下发、在 TEE 内解。

### 12.2 逆向它，为什么还是「抓边界」比「读混淆」值

`cKey` 从纯 JS 搬进 WASM，就是为了抬高静态阅读成本。但这恰恰印证了本文反复讲的方法论——**盯可控边界，而不是硬啃混淆内部**：

| 步骤 | 做法 | 对应本文思路 |
|------|------|--------------|
| 定位 | 在 JS 里搜 `getckey` / `cwrap` / `ccall` / `.wasm` 资源；Emscripten 产物有 `Module`、`asm` 等特征 | 先确定「卡在哪一层」（§12.1） |
| 抓边界 | 包住 `cwrap` 返回的函数或 `Module._getckey`，打印**入参→出参**；固定其它参数只动 `tm`，观察 cKey 变化 | 「信 bytes」的 Web 版——只认输入输出，不认混淆过程 |
| 反汇编（可选） | dump `.wasm`，用 `wasm2wat`（wabt）或支持 wasm 的 Ghidra 反汇编 | 只有当必须复现算法时才进黑盒 |

结论和 MSL 那条链路是一样的：**要复现一个签名，先把它当成一个「给定输入、产出输出」的 oracle**（§5.3 对 CryptoSession 是同一姿势）。多数时候你根本不需要读懂 WASM 里的混淆逻辑，只要能在边界上稳定地喂参数、拿签名，就够构造合法请求了。区别只在 oracle 的载体：MSL 是设备 CDM，cKey 是浏览器里的 WASM。

### 12.3 cKey 与 MSL：都在「客户端做密码学」，但签的东西完全不同

把两者摆到一起，就能看清为什么说「cKey 最接近、但仍不是 MSL」：

| | 腾讯视频 cKey | Netflix MSL |
|---|---------------|-------------|
| 保护对象 | 一个 **HTTPS 请求**（getvinfo） | 整条**控制面消息**（header + payload） |
| 密码学动作 | 对请求参数做**防篡改签名** | encrypt-then-MAC：**加密 + 签名**整个信封（§4.2） |
| 请求体本身 | 明文（TLS 之外可读） | 密文（TLS 之外仍不可读） |
| 密钥来源 | WASM 内置/派生（客户端侧） | DRM Key Exchange，会话现场协商（§五） |
| 绑定强度 | token + `guid` 软标识 | MSL 实体 + session；Widevine 路径可进一步绑定设备 |
| 一句话 | 给明文请求**盖个客户端签名章** | 把控制面装进与**实体/session 绑定**的加密信封 |

两者都体现了在 TLS 之外增加应用层请求保护的思路。但 `cKey` 主要给请求参数增加认证，MSL 还可以提供 payload 机密性，并把消息与 entity/user/session 上下文绑定。更准确的判断是：**单独获得 MSL token 通常不足以构造新消息，还需要对应 session key 或可调用的 crypto capability；该能力是否必须来自硬件设备，则取决于实际 entity authentication 与 Key Exchange。**

---

## 十三、结语：这次逆向真正沉淀下来的东西

这篇 MSL 分析和前面的 Widevine / Chrome CDM 文章是同一条研究线的不同层面：

| 文章 | 关注层 | 结论 |
|------|--------|------|
| Widevine L3 keybox 量产 | 白盒 AES / keybox / provisioning | 老 Android L3 可通过 DFA 拆解 |
| Chrome CDM 流捕获 | 现代桌面 CDM / 播放进程 | 直接提 key 不现实，转向流捕获 |
| 本文 | Netflix MSL 协议层 | MSL wire format 与 token 绑定是服务端通信核心 |

从研究路径看，本文是一次「向上走」：当底层白盒越来越硬，就去理解协议层；当协议层也加密签名，就回到真实客户端抓 bytes；当 bytes 对齐后，再用最小实现验证。

如果要把这次逆向压成几句能复用的东西，不是某个字段映射，而是几个能落到字节的判断：

- **先问在哪一层，再选工具。** 卡在 HTTPS、MSL、Widevine 还是播放层，决定了该抓 SSL bytes、该 decode CBOR、还是该看 CDM challenge——工具（Frida / Ghidra）只是这些问题的答案，不是起点。
- **业务 JSON 正确 ≠ 线路格式正确。** payload 参数对，不代表 header envelope 的类型、签名输入、token 绑定对（§4、§六）。
- **单点成功 ≠ 链路闭环。** Key Exchange 成功不代表 Manifest / License 也对，licensedManifest 能稳定解密才算闭环（§七）。

这次逆向里，几张流程图不是写完文章后的装饰，而是过程中的压缩工具：当一条链路画不清楚时，通常说明自己还没真正理解它。后续如果 Netflix 改协议，最先该更新的也是这些图和 §四 的字节结构——因为它们能最快暴露「哪一层变了」。

最后再强调一次：本文是安全研究与协议理解记录，所有字节均为按结构重构的示意值，不含真实账号、cookie、ESN、设备密钥或内容密钥。真正值得复用的不是某份凭据，而是这套「分层 → 对照样本 → 抓 bytes → 最小复现 → 错误码验证」的方法。
