---
title: "把 Netflix MSL 拆成字节 - 一次协议逆向的路线、踩坑与经验"
slug: "netflix-msl-protocol-reverse-engineering"
date: 2026-08-15
lastmod: 2026-08-19
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
*HTTPS 负责传输；MSL 负责应用层加密、签名、token、重放保护；Widevine 在 Netflix 链路里有双重角色：先为 MSL Key Exchange 提供会话密钥，再通过 MSL 通道获取内容 license。*

既然已经有 TLS，为什么还要在里面重新做一遍加密、签名、防重放？要回答「为什么这么设计」，得先看 Netflix 面对的**威胁模型**，因为每一个设计选择都是对其中一条约束的回应：

- **客户端在攻击者手里。** 设备被 root / 越狱 / 魔改是常态，客户端里的任何静态密钥都应视为已泄露。
- **传输路径上普遍存在 TLS 拦截。** 企业、校园、运营商的透明代理装了受信根证书，对它们而言 TLS 就是明文；Open Connect CDN 边缘也会终止 TLS。**「有 HTTPS」不等于「Netflix 和设备之间没人能读」。**
- **服务端是无状态大集群。** 不可能为每个请求做一次有状态的会话查找。
- **设备形态极多。** TV、机顶盒、游戏机、手机、Web，网络栈千差万别，安全实现却要尽量只写一套。
- **内容价值高。** 需要 per-play 授权、可撤销、可灰度、可轮换密钥。

MSL 的每一条设计，都是在回答上面某一条：

1. **信道绑定到「这台被 provision 过的设备」，而不是「某个 TLS 客户端」。** TLS 只证明「对端持有某张 CA 签发的证书」。MSL 的 `MasterToken` 把信道绑定到设备的加密身份（ESN + 设备密钥，根植于 Widevine 的硬件信任根）。于是**偷到一个 bearer token 不够用**——你还得有那台设备的加密身份，才能让服务端认这条会话。这是对「客户端在攻击者手里」的回应。
2. **会话密钥来自 DRM / RSA，不来自 TLS 握手。** MSL 自带 Key Exchange（见 §五），`Kenc`/`Khmac` 经 Widevine CDM 或 RSA 建立，和 PKI / CA 信任根解耦。**这样一来，TLS 被拦截代理解密、甚至根证书被信任，MSL 这一层依旧是密文**——攻击者拿到的是一段自己解不开的 CBOR。这是对「TLS 拦截」的回应。
3. **加密、签名、防重放下沉到消息本身。** 每条消息自带 `messageid` / `sequencenumber`（§四），重放保护挂在消息上而非连接上。于是同一套信封能跨 HTTP、WebSocket、离线场景复用——一套安全实现覆盖所有设备形态。这是对「设备形态极多」的回应。
4. **token 是服务端签发的自包含凭证。** `MasterToken` 里的会话密钥用服务端主密钥封装（§六），任意一台服务器只要有主密钥就能校验、解封，**不需要粘性会话、不需要跨机查表**。这是对「无状态大集群」的回应。
5. **身份与会话分离，支持轮换。** 设备身份（ESN + 设备密钥）是长期根，`MasterToken` 带 `renewalwindow` / `expiration` 是短期会话；会话密钥可以频繁轮换而无需重新 provision 设备。这是对「密钥要可轮换」的回应。

一句话概括这套设计的本质：**Netflix 不信任 TLS、不信任客户端、也不想让服务端有状态，于是把「机密性 + 完整性 + 防重放 + 设备身份 + 用户身份」全部塞进了一个自包含、可跨机校验的应用层消息里。** 理解了「MSL 是自成一体的应用层信道」，就能理解它和 Widevine 之间那个容易看晕的**互相依赖**结构：

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

两条路线的本质差异就在第 4~5 步：`ASYMMETRIC_WRAPPED` 让你**持有密钥**，`WIDEVINE` 只让你**持有一个能调用密钥的 oracle**。

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
    sequencenumber,                 # 会话序号，重放保护
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

**服务端校验的核心等式是 `UserIdToken.mtserialnumber == MasterToken.serialnumber`。** 这一个等式把「用户身份」钉死在「某一次设备会话」上：换了会话（`MasterToken` 变），旧的 `UserIdToken` 立刻失配。这就是为什么「抓到 token」不等于「能重放请求」——一旦 `Kenc`/`Khmac`、`MasterToken`、`UserIdToken`、cookie、endpoint 来自不同上下文，就会冒出看似玄学、其实是绑定断裂的 `106039` / `205032` / `502`（§七）。

另一层绑定在密钥上：`sessiondata` 里的会话密钥由服务端加密，客户端拿不到明文（§5.2 的 `WIDEVINE` 路线甚至连 CDM 里也不暴露）。客户端能做的只是**用握手阶段拿到的 `Kenc`/`Khmac` 去证明自己「属于」这个 token**——token 是服务端签发的不透明凭证，密钥是证明持有权的手段，两者必须来自同一次会话。理解这一点，token 相关的错误就从「玄学」变成「查绑定」。

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

这条链路给了笔者一个清晰的验收标准：**只做成 Key Exchange 还不算真懂 MSL；只有 licensedManifest 能稳定解密，协议模型才算闭环。** 这里的「解析」是实验室验证——确认协议链路可解释、响应结构可解析，不涉及任何真实账号或内容密钥材料。

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

## 十、横向对比：MSL 强在哪，代价是什么

把 MSL 放回行业里对比，才看得清它到底买到了什么。下面依次对照四类：主流国外流媒体、国内长视频三家（爱奇艺 / 腾讯视频 / 优酷）、国内音乐 App，最后用一张谱系图和一张表收口。

贯穿全节的其实只有一根轴——**信任边界画在哪**。先把结论画出来，后面再逐类展开：

![内容保护的信任边界谱系](https://overkazaf.github.io/blogs/images/msl-protocol-analysis/trust-boundary-spectrum.png)
*同一根轴上，信任边界从「文件」一路移到「设备加密身份」。越往下，单点破解能撬动的杠杆越小。国内长视频三家沿这条线整体下移，但爬到的高度不同；MSL 独特在把 manifest / 授权也纳入了设备绑定的信封。*

### 10.1 vs 主流流媒体：差别在「信任边界画在哪」

绝大多数流媒体（HLS/DASH + Widevine/PlayReady/FairPlay 的组合，Disney+、Prime Video、YouTube TV 等大多如此）的安全架构是**两段式**：

- **控制面**（manifest、码率、CDN 地址、播放授权）走 **HTTPS + bearer token**（OAuth/cookie/JWT）；
- **密钥面**（内容密钥）走 **EME → CDM** 的标准 DRM license 交换。

这套组合对付网络攻击者完全够用——TLS 保证传输，DRM 保证密钥不落地。但它的**信任边界画在「token」上**：服务端信任持有合法 token 的人，而 token 往往是可从客户端会话里抠出来的 bearer 凭证。Netflix 的不同之处在于，它把控制面也塞进了设备绑定的 MSL 信道，于是信任边界从「token」下移到了「设备的加密身份」。

| 维度 | 主流方案（HLS/DASH + DRM） | Netflix MSL |
|------|---------------------------|-------------|
| 控制面（manifest/授权） | HTTPS + bearer token，明文 JSON | 塞进 MSL：加密 + 签名 + 设备绑定 |
| 信任边界 | 落在 token 上（bearer，可复制） | 落在设备加密身份上（ESN + 设备密钥） |
| 密钥来源 | 标准 EME/CDM license 交换 | 自带 Key Exchange，可由 CDM 驱动（§五） |
| 抗 TLS 拦截 | 依赖 TLS；代理装了根证书就能读控制面 | 控制面在 MSL 里仍是密文 |
| manifest 与 license | 两次独立请求 | `licensedManifest` 合并进一个授权信封（§七） |
| 单点破解后果 | 拿到 token 即可重放控制面 | 还需设备身份 + 活的 CDM，token 不够 |
| 代价 | 实现简单、标准化、生态成熟 | 协议复杂、需自研客户端栈、维护成本高 |

要公平地说：**多数厂商是「故意」选简单方案的**。HLS/DASH + 标准 DRM 有成熟的播放器、打包器、CDN 生态，接入成本低；MSL 那套是 Netflix 用工程复杂度换来的纵深防御，只有内容价值、设备规模、反滥用压力都到 Netflix 量级，这笔投入才划算。YouTube 走的是另一条路——用 JS VM 混淆流地址签名（`n` 参数、cipher），偏「反爬对抗」而非形式化的设备绑定协议，本质是 obfuscation，和 MSL 有 spec、可证明的路子不是一个范式。

### 10.2 vs 国内长视频三家：同一条演进路线，爬到了不同高度

国内长视频三家（爱奇艺、腾讯视频、优酷）既不能和音乐归一档，也不能三家归一档。为把这一节写准，笔者交叉核对了三家的公开资料与社区逆向工作——**凡无法一手证实的，都在下文明确标注为推断**（各平台官方从未公开逐清晰度的 DRM 矩阵，容器内部常量在中文资料里多互相矛盾，这里一律不引用）。看下来的图景是：三家走的是同一条演进路线，但爬到的高度不同。

**第一代（历史）：私有容器 + 弱加密/伪加密。** 这一代的"加密"主要是容器混淆 + 私有解码器封锁，挡普通用户、挡不住确定性攻击者：

- **优酷 `.kux`**：本质是分段重排的 FLV 容器（大头部区 + 256KB 对齐分段、内层仍是标准 FLV）。社区转换工具几乎都直接调用优酷自带的 `ffmpeg` 做 `-c copy` **流拷贝、零转码**——这等于证明底层码流**根本没做密码学加密**，只靠"标准播放器不认这个容器"设卡，强度≈0。
- **爱奇艺 `.qsv`**：文件头以 ASCII `QIYI VIDEO` 开头、约 90 字节头部 + 加密索引区，**每个分片只有前 1KB 被轻量加扰**，其余原样。社区工具离线 remux 即可还原。但要注意这是**历史方案**——爱奇艺客户端 v10+ 下载的 QSV 已换新加密，老工具失效。
- **腾讯 `.qlv`（早期缓存 `.tdl`）**：更早的分段缓存甚至是明文，`copy /b` 直接拼接就能还原；`.qlv` 是加密容器，但商业转换器只对特定旧客户端版本有效。保护随版本明显增强。

**第二代（现状）：服务端签名授权 + 商业/自研 DRM。** 三家都上了真 DRM，但侧重差别很大：

- **爱奇艺——投入最重的一家。** 据其 DRM 团队公开文章，架构是"两条腿"：自研 **iQIYI DRM-S**（Native Code 实现）+ **MultiDRM**（集成 Widevine / PlayReady / FairPlay / Intertrust）。设备侧**硬件级（TrustZone / TEE 里的 DRM TrustApp）与软件级（白盒密码 + 代码混淆）并存**，按设备能力选择。它 2018 年通过 ChinaDRM 实验室认证（国内首个），自研 DRM-S 还过了 Riscure、Farncombe 认证。"安卓只给 720P、iPhone 给更高清"的争议，技术根因正是硬件级 DRM（如需 TEE 的 Widevine L1）在不同设备上的可用性差异。
- **腾讯视频——ChinaDRM 自研派。** 消费端主线是自研 **ChinaDRM（遵循 ChinaDRM 2.0 参考实现）**，2020 年由其点播平台负责人公开介绍。Web 端的密钥链路值得单独看：客户端用 **WASM 生成 `cKey` 请求签名** → 服务端 `getvinfo` 接口鉴权后下发播放地址 → DRM license 服务器下发解密密钥。**注意 `cKey` 是请求防篡改签名，不是内容密钥**——这点后面对比 MSL 时是关键。（另外要区分：腾讯云对外卖的商业级 DRM 是 B2B 产品，≠ 腾讯视频 App 内部方案。）
- **优酷——更靠"签名 URL"的一家。** 自家内容大量走"服务端授权 + 时效签名 CDN URL"（UPS 接口 + `ckey` 签名 + 会员 cookie），多数分段是明文，所以 you-get 这类工具能长期抓到——它的护城河是那个反复加固、频繁失效的**签名算法**，而非内容加密本身。真·商业 DRM 走阿里云 VOD 能力（官方支持 HLS-AES-128 / 阿里私有加密 / Widevine + FairPlay），但优酷 App 具体用哪种、覆盖哪些清晰度**未公开**。阿里是 ChinaDRM 标准的参编方，但"优酷线上已部署 ChinaDRM"无一手证据。

有两点必须诚实说明，否则容易高估"国内视频已被破解"的程度：一是**社区那些 `you-get`/`iqiyi-parser`/`webvideo-downloader` 多是"走下发接口拿播放流"的下载器/解析器，而不是对已加密文件的离线解密器**；二是**硬件级 L1（TEE）路径至今是社区破不动的**，能破的基本都是软件级 L3 或纯签名授权那一层。

那么，爬到第二代最高处的国内长视频（爱奇艺 DRM-S、腾讯 ChinaDRM），和 MSL 还差在哪？差在**信任边界的覆盖范围**：它们把**密钥 / license 层**做成了设备绑定（TEE），但控制面（manifest、授权接口）仍是 **HTTPS + token + 客户端签名**。腾讯的 `cKey` 是国内最接近 MSL 的一例——客户端用 WASM 对请求做密码学签名——但它终究是**对一个明文 HTTPS 请求的防篡改签名**，不是把整条控制面塞进一个设备绑定、密钥来自 DRM 的**加密信封**。所以 §二 那句"偷 token 不够、控制面也要设备身份"，对国内长视频（哪怕用了 ChinaDRM / DRM-S 的那部分）依然是 MSL 独有的性质。

| 平台 | 历史容器 | 现状 DRM 主线 | 设备绑定 | 控制面签名/授权 |
|------|----------|---------------|----------|-----------------|
| **爱奇艺** | `.qsv`（每段前 1KB 弱加扰） | 自研 DRM-S + MultiDRM，过 ChinaDRM/Riscure/Farncombe 认证 | 硬件 TEE + 白盒并存 | HTTPS + token（+ 签名） |
| **腾讯视频** | `.qlv` / 早期 `.tdl`（明文可拼接） | 自研 ChinaDRM 2.0 | 分级：硬件 L1 / 软件 L3 | `cKey`(WASM 签名) + `getvinfo` 鉴权 |
| **优酷** | `.kux`（伪加密，`-c copy` 即还原） | 阿里云 VOD DRM（覆盖未公开），多数走签名 URL | 弱（token + 软设备指纹），L1 无证据 | UPS 接口 + `ckey` 签名 |
| **Netflix** | —（纯流式，不落文件） | Widevine + 自带 Key Exchange | 设备加密身份（硬件根） | 整条控制面塞进 MSL 信封 |

> 主要依据：爱奇艺 DRM 团队《修炼之路 / 探索之路》及其官方认证公告、ChinaDRM 实验室资料、腾讯 2020 ChinaDRM 研讨会公开表态，以及 [you-get](https://github.com/soimort/you-get)、[bbtsdecrypt](https://github.com/ReiDoBrega/bbtsdecrypt) 等社区逆向项目。凡涉及各平台"具体用哪种 DRM / 逐清晰度映射 / 是否强制 L1"处，均为行业推断而非官方定论。

### 10.3 vs 国内音乐加密：从「文件级静态加密」到「会话级活协议」

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
| 密钥归属 | 主密钥/算法**内置客户端**，可一次性提取 | **无内置内容密钥**，每会话现场 Key Exchange，Widevine 路线连 CDM 都不暴露明文 |
| 联网要求 | 解密**完全离线**，与服务端无关 | 每次播放要**活的、被授权的**服务端往返（`licensedManifest`） |
| 单次逆向的后果 | 得到**通用离线解密器**，一劳永逸 | 只得到「如何合法发起会话」，仍需授权账号 + 真实 CDM，拿不到万能钥匙 |
| 撤销 / 灰度 / 限速 | 无（文件发出去就管不了了） | 服务端可**实时拒绝、撤销、限速、指纹识别** |
| 设备绑定 | 无，文件 + App 即可 | 绑定到 provision 过的设备（硬件信任根） |
| 安全范式 | security through obscurity，逆一次即崩 | 有 spec 的协议 + 硬件根，可轮换、可升级 |

一句话：**破解国内音乐加密，是「拿到密钥后所有文件永久解密」；破解 MSL，是「学会如何像官方客户端一样合法发起一次会话」——后者每次都要服务端点头，且拿不到能离线解一切的万能钥匙。**

不过同样要说公平话：两者**在解不同的题**。国内音乐 App 的目标是「可下载、可离线播放的文件，加一层轻摩擦防随手拷贝」，它天然要把可播放文件交到用户设备上，UX 和离线可用性优先于强安全；Netflix 从不打算给你一个可播放文件，它是纯流式、按次授权。所以这不是「国内厂商不会做」，而是产品形态决定了能做多强——想做强的国内方案（例如 Apple Music 的 FairPlay，硬件级、逐轨密钥交换）就已经站在 MSL/Widevine 这一端了，笔者在 [FairPlay 逆向那篇](https://overkazaf.github.io/blogs/posts/fairplay-drm-frida-reversing/)里拆过它的 `decryptContext` 链路，和 `.ncm` 那种静态异或完全不是一个量级。

### 10.4 一张表看清整个谱系

把开头那张谱系图落成文字，就是下面这张表。真正在移动的只有一件事——**信任边界画在哪**：从「文件」到「token」再到「设备加密身份」，越往下，单点破解能撬动的杠杆越小：

| 方案 | 密钥归属 | 控制面信任边界 | 单次逆向的后果 |
|------|----------|----------------|----------------|
| 国内音乐 `.ncm`/`.qmc` | 客户端内置静态密钥 | 无（离线文件） | 通用离线解密器，一劳永逸 |
| 国内长视频自研 `.qsv`/`.qlv`/`.kux` | 服务端下发 + token 校验 | token（bearer） | 逆外壳 + 持 token 即可取流 |
| 国外 HLS/DASH + Widevine/FairPlay | 标准 CDM license 交换 | token（bearer） | 持 token 重放控制面，密钥仍需 CDM |
| ChinaDRM / 硬件 Widevine | license，TEE/CDM 保护 | token（bearer） | 需破 TEE + 有效授权 |
| **Netflix MSL** | 每会话 Key Exchange，CDM 内 | **设备加密身份** | 只学会「如何合法发起一次会话」，无万能钥匙 |

国内方案不是「没做」，而是分布在这条谱系的不同位置：音乐停在最上一档，长视频爬到了中间，ChinaDRM 已经到了硬件 DRM 这一档。**MSL 的独特不在某一层的算法多强，而在它把控制面的信任边界一路压到了「设备加密身份」——这一步，目前无论国内外的主流流媒体大多没走。**

---

## 十一、结语：这次逆向真正沉淀下来的东西

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
