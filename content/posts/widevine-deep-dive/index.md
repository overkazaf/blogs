---
title: "一个 PSSH，为什么还拿不到 Key？ - Google Widevine 从 License Proxy 到 L1 的完整解剖"
slug: "widevine-pssh-license-l1-deep-dive"
date: 2026-08-22T14:00:00+08:00
lastmod: 2026-08-22T14:00:00+08:00
draft: false
tags: ["Widevine", "DRM", "CENC", "EME", "MediaDrm", "OEMCrypto", "L1", "PSSH", "protobuf", "security-research"]
categories: ["security-research"]
description: "从 Widevine SystemID 与 PSSH protobuf 开始，沿 CENC、License Proxy、设备 Provisioning、MediaDrm、OEMCrypto、L1/L2/L3 和安全输出还原完整信任链，并给出可复现的自有内容实验与安全评估。"
toc: true
math: false
---

> **读完本文，你将获得：**
> - 分清 MPD、PSSH、WidevinePsshData、KID、CK 和 License，不再把一段 Base64 当成内容密钥
> - 看懂客户端为什么不能直接访问 Widevine License Service，以及合作方 License Proxy 真正承担什么职责
> - 理解浏览器 EME 与 Android `MediaDrm` 两条接入路径，知道 CDM、OEMCrypto、TEE 和 Secure Decoder 各自守哪一段
> - 严格区分 Widevine L1/L2/L3、Android 五级安全枚举、分辨率授权和 HDCP 输出策略
> - 理解 Provisioning、设备身份、License 个性化、续租、离线授权、密钥轮换和撤销之间的关系
> - 用固定版本 Shaka Packager 对自有媒体做 CENC 实验，并用 Bento4、GPAC 与 PSSH parser 交叉检查结果
> - 从攻击面而不是产品宣传评估 Widevine：L1 把风险压到了哪里，License Server 又可能怎样把整条链主动放空

## 〇、摘要：我最初以为 protobuf 里总该有把 Key

笔者第一次拆 Widevine PSSH 时，心态和拆 PlayReady PRO 时差不多：Base64 很长，里面又是二进制，真正的 License 或内容密钥大概就藏在深处。

解开 ISO BMFF `pssh` box，先看到这个 SystemID：

```text
edef8ba9-79d6-4ace-a3c8-27dcd51d21ed
```

继续把 `Data` 当 protobuf 解码，字段开始变得很像答案：`key_id`、`provider`、`content_id`、`policy`、`crypto_period_index`、`protection_scheme`。名字一个比一个接近密钥，唯独没有 `key`。

折腾到最后才发现，这不是一个被藏得很深的保险箱，而是一张写得相当紧凑的挂号单。它告诉客户端该为哪个内容、哪些 KID、哪种加密方案建立 DRM 会话；至于客户端有没有资格拿到 License、License 能不能绑定到这台设备、内容密钥最终在哪里被解封，全部发生在 PSSH 之外。

Widevine 最容易制造的错觉就在这里：**协议里能被抓到的对象很多，真正可迁移的秘密很少。** MPD、PSSH、License Request、License Response 和加密分片都可能经过普通进程或网络，但系统的目标，是让这些对象离开原来的设备身份和执行环境后失去价值。

本文沿着这条边界从媒体字节一路走到 L1。为避免把公开事实、实验观察和推测混成一锅，先约定三种证据等级：

| 标记 | 含义 | 本文示例 |
|------|------|----------|
| **公开规范** | Google、AOSP、W3C 或官方项目明确说明 | SystemID、PSSH proto、License Proxy、`MediaDrm` 安全枚举 |
| **工程观察** | 可在公开测试内容、自有媒体或开源代码中重复验证 | box 布局、KID 对齐、EME 消息顺序、secure decoder 查询 |
| **安全推断** | 从威胁模型与系统约束推导，不声称是某家平台内部实现 | 多 DRM 的最弱路径效应、策略误配和 OEM 集成残余风险 |

> **研究边界**：本文只讨论公开资料、自有内容打包、官方测试服务和防御性分析。不会给出商业服务 License 复刻、设备秘密提取、生产密钥获取或受保护内容导出的操作链。

---

## 一、Widevine 不是“CDM 里做一次 AES”

从播放器看，Widevine 的入口非常窄。Web 端是一个 Key System String：

```text
com.widevine.alpha
```

Android 端则通常是一个 DRM UUID 加 `MediaDrm`。播放器创建 session，转发一段 opaque message，再把服务器响应送回去。API 刻意把细节藏起来，于是人很容易形成一个过度简化的模型：CDM 收到 Key，调用 AES，返回明文。

这个模型漏掉了 Widevine 最重要的部分。AES 只负责样本加解密，Widevine 要回答的是另外五个问题：

1. 这段媒体由哪个 DRM 和哪个 KID 管理？
2. 请求来自哪类设备、哪个实现和哪个安全环境？
3. 当前账号、地区、播放并发和业务状态是否允许签发？
4. 内容密钥能否只在被认可的客户端状态中使用？
5. 解密以后，压缩流、像素和显示输出还能经过哪些路径？

### 1.1 六层模型

笔者把 Widevine 拆成六层：

| 层 | 关键对象 | 解决的问题 |
|----|----------|------------|
| **媒体信令** | `ContentProtection`、SystemID、`pssh`、WidevinePsshData | 选哪个 DRM、请求哪些 KID、内容如何标识 |
| **媒体加密** | CENC、`cenc`、`cbcs`、IV、subsample、KID/CK | 分片如何被对称加密 |
| **授权控制面** | License Request、Partner Proxy、License Service、Policy | 谁能拿到什么授权，能用多久 |
| **设备身份** | Provisioning、Keybox/Device Credential、证书与撤销状态 | 请求来自谁，响应绑定给谁 |
| **执行环境** | CDM、`MediaDrm`、OEMCrypto、TEE、Secure Decoder | 密钥和明文在哪一侧出现 |
| **输出约束** | secure surface、HDCP、显示能力与服务策略 | 解密后的内容能从哪里出去 |

这六层是相互独立的失败门。一个 `LICENSE_REQUEST_REJECTED`、黑屏或降档，看起来都只是“Widevine 不能播”，背后却可能分别是 PSSH 不匹配、设备未 provision、License 策略不足、secure decoder 不可用、HDCP 不达标，或者服务端根本没给高价值轨道的 KID。

---

## 二、一张图看完 Widevine 的端到端信任链

下图按 Cocoon AI `architecture-diagram` 规范绘制。蓝色是可以公开分发的媒体和元数据，红色是内容密钥与设备秘密，紫色是策略和授权状态。图中特意把 Partner License Proxy 单独画出来，因为这是理解 Widevine 服务端架构最关键、也最常被省略的一跳。

{{< cocoon-diagram
  src="images/widevine-deep-dive/end-to-end-flow.html"
  title="Google Widevine End-to-End Trust Architecture"
  height="1120"
>}}

先看三条边界：

- CDN 可以缓存 MPD、PSSH 和加密 CMAF 分片，不需要也不应该持有 CK；
- Web App 或 Android App 可以转发 License Request/Response，但标准 API 不向应用导出内容密钥；
- Widevine 官方公开架构要求客户端请求先进入合作方 HTTPS Proxy，客户端不直接与 Widevine License Service 通信。

最后一条很重要。Widevine Cloud License Service 负责验证 Widevine 消息并生成已个性化的 License，合作方 Proxy 则负责账号、套餐、地区、并发、内容权利和风险控制。把两者画成一个“License Server”虽然省事，却会直接掩盖最常见的业务授权漏洞。

---

## 三、从 MPD 到 protobuf：PSSH 里到底有什么

### 3.1 DASH `ContentProtection`

一个最小化的 Widevine DASH 信令大致如下：

```xml
<ContentProtection
  schemeIdUri="urn:mpeg:dash:mp4protection:2011"
  value="cenc"
  cenc:default_KID="00112233-4455-6677-8899-aabbccddeeff" />

<ContentProtection
  schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed">
  <cenc:pssh>BASE64_COMPLETE_PSSH_BOX</cenc:pssh>
</ContentProtection>
```

这里至少有四个不同对象：

- `schemeIdUri` 里的 UUID 是 Widevine Protection System ID；
- `cenc:default_KID` 是默认密钥标识；
- `cenc:pssh` 的 Base64 解码结果是完整 ISO BMFF box；
- 初始化分片的 `moov` 中也可能带同一个或另一组 PSSH。

W3C 的 `cenc` Initialization Data 允许把一个或多个不同 SystemID 的 `pssh` box 串联起来交给 EME。这就是同一份 CENC 媒体能同时携带 Widevine、PlayReady 等多 DRM 初始化数据的基础。

### 3.2 ISO BMFF `pssh` box

PSSH v0 的核心布局：

```text
size        4 bytes, big-endian
type        4 bytes, "pssh"
version     1 byte
flags       3 bytes
system_id  16 bytes
data_size   4 bytes, big-endian
data        data_size bytes
```

PSSH v1 会在 `system_id` 后增加 `kid_count` 和 KID 数组。需要注意：v1 box 的 KID 列表与 Widevine protobuf 内部的 `key_id` 可以同时存在，它们属于不同层。实现 parser 时不能因为外层已有 KID 就跳过内层长度检查，也不能默认两处永远一致。

### 3.3 `WidevinePsshData` 不是 License

Shaka Packager 仓库公开了 Google 使用的 PSSH proto。略去注释后，结构可以概括为：

```protobuf
message WidevinePsshData {
  optional Algorithm algorithm = 1;          // legacy
  repeated bytes key_id = 2;
  optional string provider = 3;
  optional bytes content_id = 4;
  optional string policy = 6;
  optional uint32 crypto_period_index = 7;
  optional bytes grouped_license = 8;
  optional uint32 protection_scheme = 9;     // cenc/cbc1/cens/cbcs 4CC
}
```

这些字段的安全含义并不相同：

| 字段 | 是否秘密 | 作用 |
|------|----------|------|
| `key_id` | 否 | 定位 License 中对应的内容密钥 |
| `provider` | 否 | 内容提供方命名空间/路由信息 |
| `content_id` | 否 | 资产标识，供打包或授权控制面关联 |
| `policy` | 通常否 | 打包/Key Service 侧的策略名，不等于客户端已获权利 |
| `crypto_period_index` | 否 | 密钥轮换周期索引 |
| `protection_scheme` | 否 | `cenc`、`cbc1`、`cens`、`cbcs` 的 4CC |

`KID` 可以出现在 MPD、`tenc`、PSSH v1 和 protobuf 里。它的工作是让系统知道“找哪把钥匙”，并不要求隐藏。`CK` 才是实际 AES 内容密钥，应留在 KMS、受保护 License 和客户端安全边界中。

### 3.4 一个只读 metadata parser 应该检查什么

在自有样本上做检查时，顺序最好固定：

1. box size 是否覆盖完整输入，是否存在整数溢出或截断；
2. type 是否为 `pssh`，version 是否只取实现支持的值；
3. SystemID 是否准确匹配 Widevine UUID；
4. v1 的 KID count 乘 16 后是否越界；
5. `data_size` 是否与剩余字节一致；
6. protobuf wire type、长度和重复字段是否可被严格消费；
7. PSSH KID、`tenc.default_KID` 与 MPD 默认 KID 是否形成预期映射。

PSSH 来自媒体和清单，本质上是攻击者可控输入。解析器的首要目标不是“尽量解出内容”，而是拒绝畸形长度、深度和不一致状态。

---

## 四、CENC 数据面：加密相同，不等于安全相同

### 4.1 `cenc` 与 `cbcs`

Widevine 采用 ISO Common Encryption。现代工作流最常见的是：

| scheme | 核心模式 | 特征 |
|--------|----------|------|
| `cenc` | AES-CTR | 流式异或语义，常见于较早和广泛兼容的 DASH |
| `cbcs` | pattern AES-CBC | 按 crypt/skip pattern 保护，常见于 CMAF 多 DRM |

Google 的公开支持表明确显示，平台和系统版本对加密方案的支持不同。例如 Android 7+ 才进入官方 `cbcs` 支持范围，Media3 文档给出的 Widevine `cbcs` 最低要求是 Android 7.1 / API 25。打包时只看浏览器桌面测试通过，很容易在旧 Android、电视或嵌入式客户端上留下兼容性断层。

### 4.2 `tenc`、`senc`、`saiz`、`saio`

PSSH 只负责 DRM 初始化，真正的样本加密信息分散在 CENC box 中：

- `schm` 声明 protection scheme；
- `tenc` 提供默认 KID、IV 和 pattern 参数；
- `senc` 描述每个 sample 的 IV 与 subsample 加密区间；
- `saiz`/`saio` 帮助定位辅助加密信息；
- `pssh` 告诉 DRM 客户端如何为相关 KID 建立授权会话。

因此，“成功解析 PSSH”只证明你读懂了初始化数据，不证明你已经理解每个 sample 的加密布局，更不证明拿到了 CK。

### 4.3 多轨、多 KID 与密钥轮换

生产内容常见三种切分：

1. 音频和视频使用不同 KID；
2. SD、HD、UHD/HDR 轨道使用不同 KID；
3. Live 或长内容按 crypto period 轮换 KID/CK。

这种切分不仅是密码学 hygiene，也让 License Service 能按设备能力和业务权利只签发一部分 key。服务端即使允许音频和 SD，也可以不给 UHD KID。客户端宣称支持 4K，并不意味着 License 里会出现 4K 对应的 key。

### 4.4 多 DRM 的最弱路径效应

同一份 CMAF 资产可以让 Widevine 和 PlayReady 共用 CK，只放不同 PSSH/License 封装。这降低了存储和打包成本，也引入一个直接的安全后果：

> 如果多个 DRM 最终授权同一把 CK，内容的可复制性要按最弱的客户端、最宽的策略和最松的服务端路径重新评估。

单独把 Widevine L1 做得很强，并不能抵消另一 DRM 对同一 UHD KID 签发软件级、长周期、可离线的宽松授权。多 DRM 安全评审必须从 `asset -> track -> KID -> CK -> every license path` 建图，而不是分别审三份 SDK checklist。

---

## 五、浏览器路径：EME 只接线，不交出 Key

### 5.1 EME 的职责边界

浏览器侧典型流程：

```text
navigator.requestMediaKeySystemAccess("com.widevine.alpha", configs)
  -> createMediaKeys()
  -> createSession(type)
  -> session.generateRequest("cenc", initData)
  -> message event
  -> application forwards message to partner license endpoint
  -> session.update(response)
  -> keystatuseschange
```

W3C EME 定义的是会话、消息、状态与能力协商，不定义 Widevine License 的生产字段，也不提供 `getRawKey()` 之类的 API。JavaScript 能看到的是 opaque message 和 key status；内容密钥是否能被软件进程触达，取决于具体 CDM 与平台安全实现，而不是网页权限。

### 5.2 Chrome 里的进程边界

Chromium 公开了 CDM shared-library interface，但正式 Widevine CDM 不是 Chromium 仓库里的完整开源实现。概念上可以分成：

```text
Renderer / Web App
  -> EME implementation
  -> Mojo / CDM service boundary
  -> Widevine CDM adapter + proprietary CDM
  -> decrypt/decode path
  -> compositor / GPU / display
```

具体进程名、sandbox 归属和 decode 路径会随 Chromium 版本、OS 和硬件能力变化。安全分析应追踪“消息和 buffer 穿过了哪些 trust boundary”，不要把某一版 `ps` 输出当成永久架构。

桌面软件 CDM 的现实约束也很清楚：如果密钥使用和解密长期发生在通用 CPU/普通进程，强混淆与完整性校验只能提高逆向成本，不能制造硬件隔离。ChromeOS、Android 和特定平台可能提供不同的硬件路径，因此“Chrome = L3”同样是过度概括。

### 5.3 能力协商不是授权结果

EME configuration 里的 codec、robustness、persistent state 和 distinctive identifier 是客户端能力与隐私协商。服务端最终是否发 UHD/HDR key，还会结合设备状态、账号权利、内容策略和输出能力。

换句话说，改 JavaScript 让 `requestMediaKeySystemAccess` 返回成功，最多改变前端分支；它不能替代设备凭据，也不能命令 License Service 在响应里加入原本不该下发的 KID。

---

## 六、Android 路径：从 `MediaDrm` 到 OEMCrypto

### 6.1 Framework 与 vendor plugin

AOSP 的 DRM 框架刻意保持实现无关。应用通过 `MediaDrm` 和 `MediaCrypto` 操作，`mediadrmserver` 创建 `DrmHal`/`CryptoHal`，再通过 AIDL DRM HAL 进入 vendor plugin。Android 13 起的新设备要求 binderized AIDL DRM HAL；Widevine 服务在设备 manifest 中通常以 `IDrmFactory/widevine`、`ICryptoFactory/widevine` 暴露。

简化调用链：

```text
Media3 / App
  -> MediaDrm.openSession()
  -> getKeyRequest()
  -> Partner License Proxy
  -> provideKeyResponse()
  -> MediaCrypto
  -> MediaCodec.queueSecureInputBuffer()
  -> secure or non-secure decoder/output
```

加密 sample 可以一直保持密文，直到被送入 decoder。AOSP 还为 secure buffer 设计了跨 Binder 的 native handle 路径，目的就是避免高安全内容在普通应用地址空间里先变成明文再交给 codec。

### 6.2 `MediaDrm` 的公开安全枚举

Android API 公开的不是简单三个等级，而是五种具体能力：

| Android 枚举 | DRM key/crypto | decode | 完整媒体处理 |
|--------------|----------------|--------|--------------|
| `SW_SECURE_CRYPTO` | 软件 | 不承诺安全 | 不承诺安全 |
| `SW_SECURE_DECODE` | 软件保护 | 软件安全解码语义 | 不承诺硬件路径 |
| `HW_SECURE_CRYPTO` | 硬件/TEE | 可在普通路径 | 不保证完整硬件媒体链 |
| `HW_SECURE_DECODE` | 硬件/TEE | 硬件安全解码 | 未必覆盖全部媒体处理 |
| `HW_SECURE_ALL` | 硬件/TEE | 硬件安全解码 | 压缩与非压缩媒体都在硬件支持的可信环境处理 |

`getSecurityLevel(sessionId)` 返回 session 当前级别，`requiresSecureDecoder(mime, level)` 则回答指定 codec/级别是否要求 secure decoder。这比读一个厂商字符串更适合做能力判断。

### 6.3 L1/L2/L3 与 Android 枚举不能机械画等号

Widevine 业界常用的三层概念可以这样理解：

| Widevine 级别 | 核心边界 | 典型风险位置 |
|---------------|----------|--------------|
| **L1** | 密钥处理、解密、解码和关键媒体路径进入安全硬件/TEE | TEE、OEMCrypto、secure decoder、驱动与输出集成 |
| **L2** | 密钥和 crypto 在安全硬件，解码/像素路径可离开安全环境 | 解密后的压缩流或普通 decode 路径 |
| **L3** | 密钥处理与 crypto 主要在软件环境 | CDM 进程、混淆实现、内存与软件输出路径 |

这张表是安全模型，不是 Android API 的强制转换表。`HW_SECURE_CRYPTO` 很像 L2 的核心特征，`HW_SECURE_ALL` 很像 L1 目标，但具体认证、codec、SoC 和服务策略仍由 Widevine/OEM 集成决定。

最重要的一句是：**安全级别描述客户端执行能力，不直接定义内容分辨率。** `MediaDrm.openSession(level)` 的官方文档只说降低安全级别通常会被 License policy 限制到更低分辨率；“通常”不是“协议固定”。720p、1080p、4K 与 HDR 的门槛是服务方策略，不是 L1 字符串的数学函数。

### 6.4 OEMCrypto 守的是什么

Widevine 官方公开页只把 OEMCrypto、Keybox 和 Provisioning 标为设备集成组件，详细接口和合规要求属于授权资料。结合 AOSP 边界与公开研究，可以谨慎地描述其角色：

- 把设备凭据、License key derivation 和内容 key loading 留在受保护实现中；
- 为加密 sample 提供受约束的 decrypt/decode 接口；
- 维护 session、usage、nonce、时间或 rollback 相关安全状态；
- 把受保护 buffer 交给 secure decoder，而不是向 REE 返回裸 CK。

这里必须区分“公开接口角色”和“某个版本的逆向布局”。函数编号、key ladder 常量、trustlet 消息和内存结构都可能随 OEMCrypto/API 版本变化，不应拿一篇论文里的单个设备样本冒充所有 L1 实现。

---

## 七、License 控制面：为什么客户端不能直连 Google

### 7.1 两种签发方式

Google 公开提供两种 Widevine License 签发模式：

1. **Cloud License Service**：Google 托管，按组织凭据开放测试和生产环境；
2. **License Server SDK**：合作方在自己的基础设施中托管 License 服务。

无论哪种，客户端请求都应先到合作方控制的 License Proxy。官方描述的链路是：

```text
Client request
  -> Partner HTTPS Proxy validates request
  -> Partner evaluates entitlement/business rules
  -> Proxy appends rules
  -> Widevine License Service fulfills request
  -> individualized immutable license
  -> Proxy returns response to the originating client
```

“License 生成后不可修改且针对请求设备个性化”意味着 Proxy 不能在返回途中随便把 SD License 改成 UHD，也不能把 A 设备响应换个 KID 后发给 B 设备。业务规则必须在签发前做完。

### 7.2 Proxy 不是反向代理配置文件

把 Proxy 只实现成 `POST /license -> upstream`，等于主动放弃 Widevine 架构给出的业务控制点。一个合格的授权面至少要绑定：

| 上下文 | 需要验证的关系 |
|--------|----------------|
| account/session | token 是否有效、是否允许当前播放类型 |
| asset | 当前用户是否有这部内容的权利 |
| track/KID | 请求 KID 是否属于该资产与允许档位 |
| device/client | 设备状态、实现版本、安全能力、撤销状态 |
| geography/risk | 地区、代理风险、异常切换与速率 |
| concurrency | 同账号活跃 playback/renewal 是否超限 |
| output | HDCP、secure decoder 和目标档位是否匹配 |

最危险的实现不是“不懂 protobuf”，而是只验证 HTTP token，不验证 token、资产、KID 和设备证据是否属于同一授权上下文。

### 7.3 生产 License 协议为何不应靠猜

Widevine PSSH proto 和 Common Encryption Key API 的一部分在 Shaka Packager 中公开，但客户端生产 License message、设备证书、策略和 OEMCrypto 细节并没有形成一份等价于 PlayReady Header Specification 的完整公开规范。正式合作方应以 Widevine Portal、Proxy SDK 和 License Server SDK 随授权交付的文档为准。

社区项目可以帮助阅读 protobuf 名称和研究历史版本，却不能成为生产兼容性或安全合规的唯一依据。把某个非官方 `license_protocol.proto` 直接固化到服务端，未来最先遇到的不是“破解成功”，而是版本、字段语义、签名和设备状态判断全面漂移。

---

## 八、Provisioning 与设备身份：License 到底发给谁

### 8.1 Provisioning 是 License 之前的信任建立

Android `MediaDrm` 公开 API 明确区分 Provisioning 和 Key Request：

- `getProvisionRequest()` 产生 opaque provisioning request；
- 应用按返回的 default URL 交给 provisioning server；
- `provideProvisionResponse()` 安装响应；
- 之后才能正常 open session 或获取 key。

也就是说，Provisioning 不是“第一次 License 请求的别名”，而是向设备分发或更新设备唯一凭据的独立阶段。未 provision、凭据损坏或被撤销，都可能让后续授权直接失败。

### 8.2 Keybox、设备证书和 L1 不应混成一个词

公开 Widevine 概览把 Keybox 列为设备集成组件；历史 Android 研究又经常把设备身份根、keybox、WVD 文件和 provisioned certificate 混着说。更稳妥的边界是：

- 制造/设备根材料负责建立不可随意复制的设备身份；
- Provisioning 把设备注册到 Widevine 信任体系并获取可用于协议的凭据；
- License Request 证明或携带这些身份状态；
- License Service 根据身份、实现和策略生成绑定响应；
- OEMCrypto/CDM 在设备侧验证、派生并受约束地使用 key。

不同年代、平台和安全级别可能采用不同材料与封装。审计时应问“哪个 secret 是 root、存在哪里、如何更新和撤销”，而不是看到一个叫 keybox 的文件就断言掌握了整套设备信任。

### 8.3 隐私边界

设备身份越稳定，越容易成为跨站跟踪信号。Android 安全文档明确提到，浏览器中的 Widevine Client ID 会按应用包和 Web origin 返回不同值。EME 也把 distinctive identifier 与 persistent state 放进显式能力/权限模型。

这说明 DRM 的隐私目标不是“设备没有身份”，而是尽量避免把同一个可链接标识无条件暴露给所有站点。公开研究已经指出浏览器实现偏差可能重新引入可链接性，因此隐私评审要覆盖浏览器/CDM 实现，而不能只看 EME 规范的理想流程。

---

## 九、License 不只是一把 CK：时间、状态与输出

### 9.1 Streaming、Offline 与 Release

从 EME 和 `MediaDrm` 暴露的会话能力，可以把常见授权生命周期理解为：

| 类型 | 状态位置 | 典型动作 |
|------|----------|----------|
| streaming / temporary | session 内存 | 获取、播放、续租、关闭 |
| offline / persistent | 设备持久安全状态 | 下载、恢复 key set、续期、显式释放 |
| release | 客户端生成释放请求 | 服务端确认后清理离线授权 |

离线 License 不是“把 Response 存个文件”。它还要处理设备绑定、安全时间、播放窗口、存储回滚、renewal 和 release。Android 提供 `getOfflineLicenseState()`、`removeOfflineLicense()` 等 API，本身就说明持久 License 是受 DRM 状态机管理的对象。

### 9.2 Renewal 也是并发控制

旧 Android API 中的 Secure Stop 曾用于确认 key session 生命周期；相关接口在 API 33 已被标记 deprecated，官方建议通过周期性 License renewal 管理并发播放。

这揭示了一个实用设计：并发限制不必只靠“开始播放时计数 + 客户端退出时减一”。短租约和续租可以把掉线、进程崩溃和恶意不释放，转换成服务端可过期的 lease。代价是续租服务必须高可用，而且不能让网络抖动误伤正常播放。

### 9.3 HDCP 与输出保护

Android `MediaDrm` 公开了 `getConnectedHdcpLevel()`、`getMaxHdcpLevel()`，错误码中也明确区分 `ERROR_INSUFFICIENT_OUTPUT_PROTECTION` 与 `ERROR_INSUFFICIENT_SECURITY`。这两类失败不应混为一谈：

- security 不足：DRM key/crypto/decode 的执行环境不满足策略；
- output protection 不足：设备内部可以安全处理，但外接显示链路不满足 HDCP 等要求。

服务端策略还应考虑没有数字输出、显示热插拔、镜像、虚拟显示、远程桌面和 secure surface 变化。只在 License 签发瞬间检查一次 HDMI 状态，并不等于整个播放期间都满足输出保护。

---

## 十、密钥轮换与 Live：把一次授权变成持续协议

### 10.1 Crypto Period

Shaka Packager 公开的 Widevine Common Encryption API 支持：

- `first_crypto_period_index`；
- `crypto_period_count`；
- `crypto_period_seconds`；
- 每个 period 返回独立 KID、key、IV 与 PSSH 数据。

在 Live 中，播放器需要在新 period 到来前拿到对应 key。轮换间隔太长，单次泄露窗口扩大；间隔太短，License QPS、CDM session 数、manifest 更新和边缘网络抖动会同时上升。

### 10.2 轮换不等于自动撤销历史风险

密钥轮换解决的是 blast radius 和时间分段，不会自动完成：

- 吊销已经泄露的设备凭据；
- 阻止旧 License 在有效期内继续使用；
- 修复错误签发给低安全客户端的高价值 KID；
- 清除 CDN 或日志中误写的 CK；
- 弥补所有 period 使用可预测 key derivation 的 KMS 设计。

轮换策略必须和 License duration、renewal、revocation、KMS audit 一起设计。只把 period 从 24 小时改成 10 分钟，可能只是把服务端复杂度放大 144 倍。

---

## 十一、从安全角度评估 Widevine

### 11.1 它真正做强的地方

Widevine 的强项不是隐藏 MPD，而是把可规模化攻击拆成多个需要同时成立的条件：

1. CENC 让 CDN 与存储只处理密文；
2. 设备 Provisioning 和 License 个性化抑制跨设备响应重放；
3. Partner Proxy 把 DRM 证据与账号业务权利绑定；
4. L1/OEMCrypto 把 key use、decrypt、decode 和输出压进硬件信任路径；
5. 多 KID、短 License 和 renewal 缩小单点泄露窗口；
6. 撤销与设备状态允许服务方把已知失陷实现移出高价值授权集合。

它并不保证攻击不可能，而是努力把“复制一段响应即可无限扩散”变成高成本、设备相关、容易观测且可以撤销的对抗。

### 11.2 网络攻击者

网络侧可以看到域名、时序、包长和加密媒体流量，但 HTTPS 保护 License Proxy 传输。即使在受控客户端上看到完整 Request/Response，设备个性化和 session 状态也应阻止把响应搬到另一设备直接使用。

剩余风险主要在：TLS 终止点、代理日志、调试抓包配置、错误遥测、CDN token 和 License API 的认证授权。尤其要检查 APM 是否把 opaque License body 当“便于排障的 payload”持久化。

### 11.3 控制 Web App 或普通 Android 进程的攻击者

这类攻击者可以改 JavaScript、hook EME/`MediaDrm` 调用、观察 IPC 和网络，也可以伪造 UI 层 capability。对 L3，秘密和攻击者长期处于同一通用执行环境，安全性很大程度依赖实现复杂度、混淆、完整性与快速撤销。

对 L1，REE 原本就应被视为不可信。正确目标不是阻止 REE 看见所有消息，而是保证：

- Request/Response 离开设备安全状态后不能独立使用；
- CK 不通过普通 API 返回；
- 解密后的高价值 sample 不落入普通共享内存；
- secure decoder 和 protected surface 不能被替换成普通输出而保持同等授权；
- TEE 调用严格校验 handle、长度、session 和 nonce。

### 11.4 L1 把攻击面搬到了哪里

“进入 TEE”是边界变化，不是安全证明。L1 的残余风险包括：

- secure boot 与 anti-rollback 配置错误；
- DRM HAL、TEE driver 或 trustlet 消息解析漏洞；
- 共享 buffer 的 ownership、lifetime 和 bounds 错误；
- secure decoder、display compositor 或 HDCP 集成缺陷；
- debug fuse、工程固件和生产 provision 流程失控；
- OEM 把本应留在 secure world 的中间结果送回 REE；
- 设备证书批量泄露、错误签发或撤销传播过慢。

这也是为什么安全等级不能只靠客户端上报。License Service 需要基于受认证的设备/实现状态做决策，并保留按 OEM、model、build、CDM version 和 credential 批次快速降级的能力。

### 11.5 License Proxy 与 KMS 往往更脆

| 风险 | 后果 | 防御重点 |
|------|------|----------|
| token 未绑定 asset/KID | 低价值会话越权请求其他轨道 | 服务端从可信 catalog 反查 KID，不信客户端列表 |
| 只信 capability 字符串 | 软件客户端拿到高价值策略 | 验证受认证设备状态和安全能力 |
| License 过长、续租过松 | 失陷窗口扩大，并发控制失效 | 短 lease、风险自适应 renewal、幂等状态机 |
| key API 与 license API 共凭据 | 一处服务失陷直接暴露 CK | 分离身份、权限、网络与审计域 |
| CK 出现在日志/队列 | 密钥绕过所有客户端保护泄露 | KMS envelope、字段级脱敏、禁止 body logging |
| revocation 数据陈旧 | 已知失陷客户端继续获权 | 自动发布、版本门槛、灰度和紧急 kill switch |
| 多 DRM 策略不一致 | 从最弱路径得到同一 CK | 统一 entitlement graph 与 KID policy |

客户端投入再多，KMS 返回接口如果能被普通应用凭据调用，或者 Proxy 能为任意 KID 附加宽松规则，整条硬件信任链都会被服务端主动绕开。

### 11.6 公开研究给出的边界

近年来的 Widevine 研究大致分成四类：

1. 逆向 L3 软件实现与 key ladder；
2. 分析 Android L1/OEMCrypto 组件和信任边界；
3. 形式化验证 EME/Widevine 协议目标；
4. 研究浏览器实现造成的隐私和 replay 问题。

这些工作共同说明：协议设计、CDM 实现、浏览器 glue、OEM 固件和服务端策略缺一不可。拿一项论文结果宣布“Widevine 已破”不严谨；拿认证标签宣布“L1 不可破”同样不严谨。安全结论必须注明具体版本、平台、攻击权限、目标资产和是否可规模化。

---

## 十二、一个合法、可重复的 Widevine 实验室

### 12.1 实验目标

```text
自有 clear MP4
  -> Shaka Packager CENC/cbcs 打包
  -> 生成 Widevine SystemID + PSSH protobuf
  -> 检查 MPD / tenc / senc / PSSH / KID
  -> 使用自建测试授权或 Clear Key 验证通用 EME plumbing
  -> 使用官方/合作方测试环境验证 Widevine，不接生产 OTT endpoint
```

raw key 实验只能验证 CENC 数据面和 Widevine 信令。它不会凭空生成受 Google 信任的 Device Credential、Cloud License 权限或生产 CDM。

### 12.2 固定 Shaka Packager 版本

本文固定 `v3.9.3`，避免 main branch 继续变化后命令和产物无法对齐：

```bash
sudo apt-get update
sudo apt-get install -y \
  curl build-essential cmake git ninja-build python3

git clone --recurse-submodules \
  --branch v3.9.3 \
  https://github.com/shaka-project/shaka-packager.git
cd shaka-packager
git submodule update --init --recursive

cmake -B build -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF

cmake --build build --parallel
ctest --test-dir build --output-on-failure
./build/packager/packager --version
```

官方当前构建要求 CMake 3.24+，Linux/macOS 推荐 Ninja。几个参数分别意味着：

| 参数 | 作用 | 何时修改 |
|------|------|----------|
| `-B build` | out-of-tree build，源码目录保持干净 | 多配置可换成 `build-debug` 等 |
| `-G Ninja` | 使用 Ninja generator | Windows 可使用 Visual Studio generator |
| `CMAKE_BUILD_TYPE=Release` | 开启 release 优化和对应编译定义 | parser 调试时改 `Debug` |
| `BUILD_SHARED_LIBS=OFF` | 生成静态 `libpackager` 依赖形态 | 嵌入动态库时设 `ON` |
| `--parallel` | 并行编译 | 内存不足时加具体 job 数 |

`FULLY_STATIC=ON` 不是“更安全”的按钮。官方文档明确要求配合 musl 或 musl toolchain；在普通 glibc 环境硬开只会得到链接问题。需要可移植静态二进制时再使用：

```bash
cmake -B build-musl -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DBUILD_SHARED_LIBS=OFF \
  -DFULLY_STATIC=ON \
  -DCMAKE_C_COMPILER=/path/to/x86_64-linux-musl-gcc \
  -DCMAKE_CXX_COMPILER=/path/to/x86_64-linux-musl-g++
```

为了让实验可复现，至少记录：tag、完整 commit、submodule revision、编译器版本、CMake 版本、host OS 和最终 `packager --version` 输出。

### 12.3 打包自己的测试媒体

下面 KID/key 仅是公开文档占位值，只能用于自己生成的测试内容：

```bash
./build/packager/packager \
  'in=clear.mp4,stream=video,output=video_cenc.mp4,drm_label=VIDEO' \
  'in=clear.mp4,stream=audio,output=audio_cenc.mp4,drm_label=AUDIO' \
  --enable_raw_key_encryption \
  --keys \
label=VIDEO:key_id=00112233445566778899aabbccddeeff:key=000102030405060708090a0b0c0d0e0f,\
label=AUDIO:key_id=ffeeddccbbaa99887766554433221100:key=0f0e0d0c0b0a09080706050403020100 \
  --protection_scheme cenc \
  --protection_systems Widevine \
  --mpd_output manifest.mpd
```

关键参数：

- `drm_label` 把 stream 映射到 `--keys` 中同 label 的 KID/CK；
- `--enable_raw_key_encryption` 表示 key 来自命令行实验值，不连接 Widevine Key Service；
- `--protection_systems Widevine` 让 Packager 生成 Widevine PSSH；
- `--protection_scheme cenc` 选择 AES-CTR CENC；
- `--mpd_output` 生成可检查的 DASH manifest。

测试 `cbcs` 时只改 scheme 还不够，必须确认目标客户端版本、codec、pattern 和播放器支持：

```bash
--protection_scheme cbcs --crypt_byte_block 1 --skip_byte_block 9
```

不要把实验 raw key 放进 shell history、CI 日志或生产命令。生产打包应从 KMS/Key Service 取得 key，并将 packager 身份限制为只访问当前 asset/track 所需材料。

### 12.4 使用 Widevine Key Service 的参数边界

Shaka Packager 支持 `--enable_widevine_encryption`，配合：

```text
--key_server_url
--content_id
--policy
--signer
--aes_signing_key + --aes_signing_iv
或 --rsa_signing_key_path
```

这组参数属于获得 Widevine 权限的内容提供方工作流。`signer` 与签名 key 是组织凭据，不是播放器侧 License，也不应出现在前端、示例仓库或普通构建日志中。没有正式 Cloud Service/合作方测试权限时，停留在 raw key + 自有内容实验即可。

### 12.5 交叉检查产物

```bash
./build/packager/packager --dump_stream_info input=video_cenc.mp4
mp4dump video_cenc.mp4
MP4Box -info video_cenc.mp4
python3 build/packager/pssh-box.py \
  --from-base64 'PASTE_YOUR_OWN_CENC_PSSH' \
  --human
```

检查清单：

1. `schm` 是 `cenc` 还是 `cbcs`；
2. `tenc.default_KID` 是否对应 stream 的 label；
3. `pssh.system_id` 是否为 Widevine UUID；
4. PSSH v0/v1 的 box 长度是否正确；
5. protobuf 中 `key_id`、`provider/content_id` 与预期一致；
6. MPD、init segment 和 media sample 的 KID 是否形成同一映射；
7. audio/video 多 key 时，播放器是否建立了所需 session 或 multi-session。

同一个 packager 同时负责写和读，可能让同一实现缺陷互相“证明正确”。用 Bento4、GPAC 和 Shaka 自带 PSSH 工具交叉验证，价值远高于换三种 Base64 网站。

### 12.6 播放侧实验

浏览器可使用 Shaka Player，Android 可使用 Media3 ExoPlayer demo。实验应用只应负责：

```text
select key system / DRM UUID
create session
forward opaque request to an authorized test endpoint
provide opaque response
observe key status, renewal, HDCP and decoder errors
```

对纯 EME 状态机测试，可以使用 W3C Clear Key 和自有 key，避免把 Widevine 生产协议当成前端联调依赖。需要验证 Widevine 时，使用官方示例内容、组织 UAT 或自建合规测试服务，不要导入商业平台 Cookie、MSL token、设备私钥或来源不明的 WVD 文件。

---

## 十三、参考项目地图：谁能证明哪一层

### 13.1 Google、Android 与标准

| 项目/规范 | 层 | 用途 | 注意事项 |
|-----------|----|------|----------|
| [Widevine Overview](https://developers.google.com/widevine/drm/overview) | 全局 | 官方生态、平台支持、Proxy/Cloud/SDK 架构 | 公开资料入口，深入文档需 License |
| [Widevine Portal](https://developers.google.com/widevine) | 合作方 | 申请访问、产品与授权入口 | 不是公开 License 协议规范 |
| [AOSP DRM Architecture](https://source.android.com/docs/core/media/drm) | Android | `mediadrmserver`、AIDL HAL、vendor plugin | 描述平台边界，不公开 Widevine trustlet |
| [Android `MediaDrm`](https://developer.android.com/reference/android/media/MediaDrm) | App API | session、provision、offline、HDCP、安全枚举 | DRM 实现无关 API |
| [Media3 DRM Guide](https://developer.android.com/media/media3/exoplayer/drm) | Android Player | Widevine `cenc/cbcs`、offline、多 session | 推荐播放器接入层 |
| [Chromium CDM API](https://chromium.googlesource.com/chromium/cdm/) | Browser/CDM ABI | EME CDM shared-library interface | 不是 Widevine CDM 源码 |
| [W3C EME](https://www.w3.org/TR/encrypted-media-2/) | Browser API | Key System、session、message、key status | 不定义 Widevine License payload |
| [W3C CENC Init Data](https://www.w3.org/TR/eme-initdata-cenc/) | Init Data | 多 PSSH 串联与处理规则 | 连接 CENC 与 EME |

### 13.2 打包、解析与播放

| 项目 | 角色 | Widevine 相关能力 | 推荐用法 |
|------|------|--------------------|----------|
| [Shaka Packager](https://github.com/shaka-project/shaka-packager) | CENC Packager | Widevine PSSH、Cloud Key API、多 DRM、key rotation | 自有媒体打包与格式基准 |
| [`widevine_pssh_data.proto`](https://github.com/shaka-project/shaka-packager/blob/main/packager/media/base/widevine_pssh_data.proto) | PSSH schema | 官方开源 PSSH protobuf 字段 | 编写严格 metadata parser |
| [`widevine_common_encryption.proto`](https://github.com/shaka-project/shaka-packager/blob/main/packager/media/base/widevine_common_encryption.proto) | Packaging API | 内容 key 请求、track、crypto period | 是打包 Key API，不是客户端 License proto |
| [Shaka Player](https://github.com/shaka-project/shaka-player) | Web Player | MSE/EME、多 DRM、License routing | 浏览器互操作与错误观测 |
| [AndroidX Media3](https://github.com/androidx/media) | Android Player | ExoPlayer DRM session、offline、rotation | Android 推荐应用层实现 |
| [Bento4](https://github.com/axiomatic-systems/Bento4) | ISOBMFF 工具箱 | `mp4dump`、加密、DASH/CMAF 检查 | box 级交叉验证 |
| [GPAC](https://github.com/gpac/gpac) | 多媒体框架 | MP4Box、CENC、DASH、PSSH | 复杂打包和独立检查 |
| [pssh-box-rs](https://github.com/emarsden/pssh-box-rs) | PSSH parser | Widevine/PlayReady PSSH 解析与序列化 | 只读 init data 工具 |
| [dash.js](https://github.com/Dash-Industry-Forum/dash.js) | DASH Player | EME protection controller、多 DRM | DASH-IF 互操作观察 |

FFmpeg 可以做编码、demux、probe 和 clear 内容验证，但它不是 Widevine CDM、License Client 或 OEMCrypto 实现。`ffprobe` 能列出 encrypted track，不等于“FFmpeg 支持 Widevine”。

### 13.3 研究项目与论文

| 项目/论文 | 范围 | 研究价值 | 边界 |
|-----------|------|----------|------|
| [pywidevine](https://github.com/devine-dl/pywidevine) | 非官方 Python 协议研究实现 | 阅读 PSSH、消息签名与 service certificate 研究代码 | 不是 Google 认证 CDM；不要导入来源不明的设备秘密 |
| [WideXtractor / Exploring Widevine](https://arxiv.org/abs/2204.09298) | Android Widevine 动态分析 | 组件关系、key ladder 与 L3 研究方法 | 针对具体历史版本，不代表所有 OEM/L1 |
| [A First Look at DRM Systems](https://arxiv.org/abs/2308.00437) | Widevine/FairPlay/PlayReady 对比 | 移动 DRM 威胁模型与系统性比较 | 学术结论不替代最新合规要求 |
| [Formal Security Analysis of Widevine](https://www.usenix.org/system/files/usenixsecurity24-delaune.pdf) | 形式化协议分析 | 明确安全目标与攻击假设 | 抽象模型不覆盖所有实现漏洞 |
| [Narrowbeer](https://www.usenix.org/system/files/usenixsecurity25-roudot.pdf) | 浏览器/CDM replay 研究 | 观察 host-CDM 交互完整性边界 | 需要按厂商修复状态和版本复核 |
| [Widevine EME Privacy](https://arxiv.org/abs/2308.05416) | 浏览器隐私 | distinctive identifier 与跨站可链接性 | 关注隐私，不等同内容 key 攻击 |

笔者没有把通用下载器、商业服务插件、设备材料交易库或“自动取 key”脚本列为参考实现。它们既不能证明 Widevine 协议实现正确，也会把本来清晰的架构研究拖进授权与合规风险。

---

## 十四、和 PlayReady 放在一起看

| 维度 | Widevine | PlayReady |
|------|----------|-----------|
| SystemID | `edef8ba9-...-d51d21ed` | `9a04f079-...-e65be0885f95` |
| PSSH payload | protobuf `WidevinePsshData` | PRO records + UTF-16LE WRMHEADER |
| Web Key System | `com.widevine.alpha` | `com.microsoft.playready` 等 |
| Android 核心 | `MediaDrm` + Widevine vendor plugin/OEMCrypto | 平台/OEM PlayReady plugin |
| 高安全路径 | L1 + TEE + secure decoder/output | SL3000 + TEE + protected path |
| 服务端公开架构 | Partner Proxy + Cloud License Service 或 SDK | 自建 PlayReady License Server SDK |
| 公开协议密度 | PSSH/Key API 开源，生产细节多在合作方文档 | Header、PRO、License/Policy 概念公开更完整 |
| 共同数据面 | CENC/CMAF，可复用 KID/CK | CENC/CMAF，可复用 KID/CK |

二者最明显的表面差异，是 protobuf 对 UTF-16 XML；真正的共同点，则是都不把安全性押在 Header 隐藏上。媒体信令公开，内容分片公开缓存，安全性来自设备身份、License 绑定、可信执行、策略和输出路径。

---

## 十五、实现和审计时最值得记住的十二条

1. Widevine PSSH 的 SystemID 固定，但 PSSH 不是 License，更不是 CK。
2. `key_id` 可以公开；它是索引，不是 AES key。
3. PSSH protobuf 是公开信令，生产 License 协议与 OEMCrypto 细节仍以授权文档为准。
4. Widevine 客户端不直接访问 Cloud License Service，业务授权必须经过 Partner Proxy。
5. EME/`MediaDrm` 让应用转发 opaque message，不向应用提供 raw key API。
6. L1/L2/L3 是安全模型，Android 还公开了更细的五级 session 能力。
7. 安全级别不等于固定分辨率；最终档位由 License policy 和服务策略决定。
8. L1 保护的不只是 CK，还包括解密 sample、secure decode、surface 和输出路径。
9. 多 DRM 共用 CK 时，要按所有授权路径中的最弱项评估资产。
10. Key rotation 缩小时间窗口，但不能替代撤销、续租和 KMS 隔离。
11. License Proxy、KMS、日志和凭据域的错误，可以绕过再强的客户端保护。
12. 开源 research CDM 能帮助读协议，不等于合规、认证或生产安全实现。

---

## 十六、结语：真正的边界在 PSSH 之后

回到开头那段 protobuf。

它可以被复制、解码、改字段、重新封装，甚至可以由 Shaka Packager 为自有媒体重新生成。Widevine 从来没有指望靠隐藏 `key_id`、`provider` 或 `content_id` 守住内容。

真正的边界在 PSSH 之后：客户端先证明自己是谁、能做到什么；Partner Proxy 再把账号权利、资产和风险规则附加到请求；License Service 生成针对该设备的不可随意修改响应；CDM 或 OEMCrypto 在受约束的 session 中解封 key；高安全内容继续经过 secure decoder、protected surface 和满足策略的显示链。

所以，一个 PSSH 当然不够拿到 Key。

它只是把你带到了信任链的入口。后面那条从 KMS、License Proxy 一直延伸到 TEE 和 HDMI 的路径，才是 Widevine 真正想守住的东西。

---

## 参考资料

1. [Google Widevine Overview](https://developers.google.com/widevine/drm/overview)
2. [Android Open Source Project: DRM](https://source.android.com/docs/core/media/drm)
3. [Android Open Source Project: Media Framework Hardening](https://source.android.com/docs/core/media/framework-hardening)
4. [Android `MediaDrm` API](https://developer.android.com/reference/android/media/MediaDrm)
5. [Android `MediaDrm.ErrorCodes`](https://developer.android.com/reference/android/media/MediaDrm.ErrorCodes)
6. [Media3 ExoPlayer DRM Guide](https://developer.android.com/media/media3/exoplayer/drm)
7. [W3C Encrypted Media Extensions](https://www.w3.org/TR/encrypted-media-2/)
8. [W3C Common Encryption Initialization Data](https://www.w3.org/TR/eme-initdata-cenc/)
9. [Shaka Packager Build Instructions](https://shaka-project.github.io/shaka-packager/html/build_instructions.html)
10. [Shaka Packager: Using Widevine Key Server](https://shaka-project.github.io/shaka-packager/html/tutorials/widevine.html)
11. [Shaka Packager `widevine_pssh_data.proto`](https://github.com/shaka-project/shaka-packager/blob/main/packager/media/base/widevine_pssh_data.proto)
12. [Shaka Packager `widevine_common_encryption.proto`](https://github.com/shaka-project/shaka-packager/blob/main/packager/media/base/widevine_common_encryption.proto)
13. [Chromium CDM Interface](https://chromium.googlesource.com/chromium/cdm/)
14. [Patat, Sabt, Fouque: Exploring Widevine for Fun and Profit](https://arxiv.org/abs/2204.09298)
15. [Rafi, Shepherd, Markantonakis: A First Look at DRM Systems](https://arxiv.org/abs/2308.00437)
16. [Delaune et al.: Formal Security Analysis of Widevine](https://www.usenix.org/system/files/usenixsecurity24-delaune.pdf)
17. [Roudot et al.: Narrowbeer, A Practical Replay Attack Against Widevine DRM](https://www.usenix.org/system/files/usenixsecurity25-roudot.pdf)
18. [Patat et al.: Privacy Implications of Widevine EME](https://arxiv.org/abs/2308.05416)
