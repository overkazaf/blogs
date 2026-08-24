---
title: "一个 PSSH，为什么还拿不到 Key？ - Google Widevine 从 License Proxy 到 L1 的完整解剖"
slug: "widevine-pssh-license-l1-deep-dive"
date: 2026-08-22T14:00:00+08:00
lastmod: 2026-08-24T17:18:00+08:00
draft: false
tags: ["Widevine", "DRM", "CENC", "CMAF", "ISOBMFF", "EME", "MediaDrm", "OEMCrypto", "L1", "PSSH", "protobuf", "security-research"]
categories: ["security-research"]
description: "从 Widevine SystemID、PSSH protobuf 与 CENC box 结构开始，沿 Chrome/Android 通信架构、License Proxy、设备 Provisioning、MediaDrm、OEMCrypto、L1/L2/L3 和安全输出还原完整信任链。"
toc: true
math: false
---

> **读完本文，你将获得：**
> - 分清 MPD、PSSH、WidevinePsshData、KID、CK 和 License，不再把一段 Base64 当成内容密钥
> - 看懂 `moov/trak/stsd/sinf/tenc` 与 `moof/traf/saiz/saio/senc/mdat` 的完整层级和引用关系
> - 分清 CMAF Header、Segment、Fragment 与 Chunk，理解低延迟发布、ABR 切换和 CENC 元数据如何落到同一条时间轴
> - 看懂客户端为什么不能直接访问 Widevine License Service，以及合作方 License Proxy 真正承担什么职责
> - 对照 Chrome 与 Android 双泳道通信图，理解 EME/Mojo/CDM 与 MediaDrm/AIDL HAL/OEMCrypto 两条接入路径
> - 严格区分 Widevine L1/L2/L3、Android 五级安全枚举、分辨率授权和 HDCP 输出策略
> - 理解 Provisioning、设备身份、License 个性化、续租、离线授权、密钥轮换和撤销之间的关系
> - 对照十类主流 OTT 应用，理解 Widevine 在订阅、租购、离线、直播和 UHD 场景中到底负责哪一段
> - 用固定版本 Shaka Packager 对自有媒体做 CENC 实验，并用 Bento4、GPAC 与 PSSH parser 交叉检查结果
> - 从攻击面而不是产品宣传评估 Widevine：L1 把风险压到了哪里，License Server 又可能怎样把整条链主动放空

**阅读路线不必从头走到尾：**

| 目标 | 推荐章节 | 可以先跳过 |
|------|----------|------------|
| 只想建立 Widevine 全局模型 | 一～三、九～十三 | Box 字段和实验命令 |
| 正在实现 packager/parser | 四～八 | OTT 产品案例 |
| 正在接 Chrome/Android 播放器 | 九～十三 | 研究论文和完整 Box 清单 |
| 正在做 OTT 安全评审 | 十一～十五、十九 | 编译过程 |
| 想搭建合法实验环境 | 十六～十九 | 产品业务对照 |

## Part I：信令、容器与媒体数据

从 PSSH 入口走到 CMAF/CENC 的逐 sample 字节映射。

### 〇、摘要：我最初以为 protobuf 里总该有把 Key

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

### 一、Widevine 不是“CDM 里做一次 AES”

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

#### 1.1 六层模型

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

### 二、一张图看完 Widevine 的端到端信任链

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

### 三、从 MPD 到 protobuf：PSSH 里到底有什么

#### 3.1 DASH `ContentProtection`

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

#### 3.2 ISO BMFF `pssh` box

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

#### 3.3 `WidevinePsshData` 不是 License

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

#### 3.4 一个只读 metadata parser 应该检查什么

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

### 四、CENC 与 ISO BMFF：先建立坐标系

#### 4.1 `cenc` 与 `cbcs`

Widevine 采用 ISO Common Encryption。现代工作流最常见的是：

| scheme | 核心模式 | 特征 |
|--------|----------|------|
| `cenc` | AES-CTR | 流式异或语义，常见于较早和广泛兼容的 DASH |
| `cbcs` | pattern AES-CBC | 按 crypt/skip pattern 保护，常见于 CMAF 多 DRM |

Google 的公开支持表明确显示，平台和系统版本对加密方案的支持不同。例如 Android 7+ 才进入官方 `cbcs` 支持范围，Media3 文档给出的 Widevine `cbcs` 最低要求是 Android 7.1 / API 25。打包时只看浏览器桌面测试通过，很容易在旧 Android、电视或嵌入式客户端上留下兼容性断层。

#### 4.2 一张图看清 Init Segment 与 Media Segment

如果只按四字符名称背 box，`tenc`、`senc`、`saiz`、`saio` 很快就会混在一起。更有效的办法，是先把它们放回 ISO BMFF 的容器树，再沿引用关系找出“默认值、逐 sample 元数据和真正密文”分别位于哪里。

{{< cocoon-diagram
  src="images/widevine-deep-dive/isobmff-box-map.html"
  title="Widevine CENC ISO BMFF Box Structure"
  height="1120"
>}}

这张图表达了三条互相独立的链：

1. `pssh` 把 SystemID 和 DRM init data 送给 CDM，负责建立 License session；
2. `stsd -> encv/enca -> sinf -> schm/tenc` 定义这条 track 的默认加密上下文；
3. `moof -> traf -> trun/saiz/saio/senc -> mdat` 把每个 fragment 的 sample、IV、subsample 区间和密文字节对应起来。

因此，`pssh` 不描述每个 sample 的字节布局，`senc` 也不负责告诉客户端去哪个 License Server。两者都叫“加密相关 box”，但处在完全不同的控制面。

#### 4.3 先理解 Box 与 FullBox

ISO BMFF 是一棵带长度的 box 树。最普通的 box 头是：

```text
size      4 bytes, big-endian
type      4 bytes, FourCC
payload   size - header_size bytes
```

当 `size == 1` 时，后面还有 64-bit `largesize`；当 `size == 0` 时，box 延伸到当前文件或父容器结束。`FullBox` 在普通头后再增加：

```text
version   1 byte
flags     3 bytes
```

`pssh`、`tenc`、`tfhd`、`trun`、`saiz`、`saio`、`senc`、`sgpd`、`sbgp` 都是 FullBox。解析器必须先根据 version/flags 决定后续字段是否存在，不能拿一个固定 C struct 直接覆盖输入字节。

安全审计时至少要防六类问题：

| 问题 | 典型错误 |
|------|----------|
| box size 小于 header | offset 回退或死循环 |
| 32/64-bit 长度加法溢出 | 边界检查被绕过 |
| child size 超出 parent | 跨容器读取下一段数据 |
| count 乘 entry size 溢出 | KID/sample 数组越界 |
| version/flags 未校验 | 按错误布局解释字段 |
| unknown box 被错误拒绝 | 兼容性失败；正确做法通常是按长度安全跳过 |

---

### 五、Init Segment：稳定默认值与 DRM 入口

#### 5.1 `ftyp + moov` 定义稳定上下文

典型 fragmented MP4/CMAF 初始化段可以抽象成：

```text
ftyp
moov
├── mvhd
├── pssh [0..N]
├── mvex
│   └── trex [per track]
└── trak [per track]
    ├── tkhd
    └── mdia
        ├── mdhd
        ├── hdlr
        └── minf
            └── stbl
                └── stsd
                    └── encv / enca
                        ├── codec configuration
                        └── sinf
                            ├── frma
                            ├── schm
                            └── schi
                                └── tenc
```

主要 box 的职责：

| Box | 所在位置 | 关键字段/作用 | DRM 语义 |
|-----|----------|---------------|----------|
| `ftyp` | top-level | major brand、compatible brands | 声明文件兼容族，不含授权信息 |
| `moov` | top-level | movie/track 元数据容器 | 初始化段主体，通常不含 sample payload |
| `mvhd` | `moov` | movie timescale、duration | 全局时间基准，不决定加密 |
| `trak` | `moov` | 一条音频、视频或文本 track | 不同 track 可以使用不同 KID |
| `tkhd` | `trak` | track ID、尺寸、duration | track identity，供 fragment 的 `tfhd` 对应 |
| `mdhd` | `mdia` | media timescale、duration、language | sample 时间轴 |
| `hdlr` | `mdia` | handler type，例如 video/audio | 轨道类型 |
| `stsd` | `stbl` | sample entry 列表 | 选择 codec 与 protected sample entry |
| `mvex` | `moov` | fragmented movie 扩展容器 | 表示后续由 `moof` 提供 sample 元数据 |
| `trex` | `mvex` | 默认 duration、size、flags、description index | 被 `tfhd/trun` 覆盖前的 fragment 默认值 |
| `pssh` | `moov` 或允许的 fragment 位置 | SystemID、KID 列表、system data | 向 CDM 提供 init data；不含 sample IV |

`ftyp` 通过、codec 能识别、`pssh` 能解码，只说明容器和初始化信令基本成立。播放器还必须找到受保护 sample entry、有效 `tenc`，并在后续 fragment 中建立 sample 到 `mdat` 的映射。

#### 5.2 Protected Sample Entry：`encv/enca -> sinf -> tenc`

加密视频通常把普通 codec sample entry 包在 `encv` 中，加密音频则使用 `enca`。真正的原始 codec 没有消失，而是由 `sinf/frma` 保存：

```text
encv
├── avcC / hvcC / av1C / ...
└── sinf
    ├── frma = avc1 / hvc1 / av01 / ...
    ├── schm = cenc / cbcs / cbc1 / cens
    └── schi
        └── tenc
```

四个对象需要分开：

| 对象 | 回答的问题 |
|------|------------|
| `encv/enca` | 这是一条受保护的视频/音频 sample entry 吗？ |
| `frma` | 去掉保护包装后，原始 codec FourCC 是什么？ |
| `schm` | 使用哪种 protection scheme？ |
| `tenc` | 这条 track 默认使用哪个 KID、IV 规则和 pattern？ |

`tenc` 的核心字段包括：

```text
default_is_protected
default_per_sample_iv_size
default_kid[16]
default_crypt_byte_block      // pattern scheme
default_skip_byte_block       // pattern scheme
default_constant_iv           // when per-sample IV size is 0
```

字段是否出现取决于 `tenc` version 和前置值。`cenc` 常见逐 sample IV；`cbcs` 还需要 crypt/skip pattern，并可能使用 constant IV。不能只读取 `default_kid` 就跳过剩余字段，否则下一 child box 的边界很容易被误判。

这里的 `default` 很关键。它不是“永远使用这组参数”，而是没有 sample group 覆盖时的 track 默认值。发生 key rotation 时，某些 sample 的有效 KID 可能来自 `sgpd(seig)`，而不是 `tenc.default_KID`。

---

### 六、Media Segment：`moof` 描述，`mdat` 存密文

#### 6.1 Fragment 的基础 Box 结构

一个典型 fragmented media segment：

```text
styp              // optional segment brands
sidx              // optional byte/time index
emsg / prft       // optional timed metadata / producer time
moof
├── mfhd
└── traf [per track fragment]
    ├── tfhd
    ├── tfdt
    ├── trun [1..N]
    ├── sgpd / sbgp       // optional encryption groups
    ├── saiz
    ├── saio
    └── senc
mdat
```

| Box | 关键作用 | 与加密的关系 |
|-----|----------|--------------|
| `styp` | segment brand/compatibility | 无 key 语义 |
| `sidx` | 时间、字节范围和 SAP 索引 | 帮助随机访问，不描述 AES |
| `emsg` | timed event | 可触发业务事件，但不是 License |
| `prft` | producer reference time | 用于时钟/低延迟关联，不是安全时钟证明 |
| `moof` | fragment metadata 容器 | 每段 sample mapping 的入口 |
| `mfhd` | fragment sequence number | 顺序标识，不应被当作 crypto period |
| `traf` | 单 track fragment | 把 timing、sample run 和 aux info 聚合起来 |
| `tfhd` | track ID 与 fragment 默认字段 | 可覆盖 `trex` 默认值 |
| `tfdt` | base media decode time | 定位 decode timeline |
| `trun` | sample count、duration、size、flags、composition offset、data offset | 定位 `mdat` 中每个 sample 的字节范围 |
| `mdat` | media payload | 对受保护 track 而言，这里才是加密 sample bytes |

`mdat` 不知道自己属于哪个 KID，也不知道哪些 NALU 字节保持 clear。它只是一段 payload。播放器必须先从 `tfhd/trun` 算出 sample 边界，再结合有效 encryption context 和 auxiliary info 才能正确解释。

---

### 七、CMAF Fragment：从可寻址对象到低延迟 Chunk

CMAF 经常被一句“DASH 和 HLS 共用的 fMP4”带过，这句话方向没错，却省略了最关键的层级。CMAF 不是 codec、传输协议或 DRM；它是 ISO/IEC 23000-19 定义的**受约束 fragmented ISO BMFF 媒体格式**，让相同的媒体对象可以被 DASH、HLS 或其他交付协议引用。

先看完整结构：

{{< cocoon-diagram
  src="images/widevine-deep-dive/cmaf-fragment-anatomy.html"
  title="CMAF Fragment, Chunk and CENC Anatomy"
  height="1160"
>}}

#### 7.1 六个名字分别处在哪一层

| CMAF 对象 | 核心含义 | 常见物理结构/映射 |
|-----------|----------|-------------------|
| **Presentation** | 一组在 presentation timeline 上同步的音频、视频和字幕选择 | DASH MPD 或 HLS Master/Media Playlist 组织 |
| **Switching Set** | 同一内容的替代编码，允许在约束满足时无缝切换 | 常映射到 DASH Adaptation Set；不同码率/分辨率各自仍是独立 track |
| **Track** | 一条编码 rendition 的连续 sample 时间轴 | 一个 CMAF Header 加连续 Fragment；不把多个 representation 混进同一 track file |
| **Header** | 初始化 track、codec、timescale、sample entry 和 fragment defaults | 实际播放路径通常对应 `ftyp + moov`，即 DASH/MSE 的 initialization segment |
| **Segment** | 一个可寻址媒体对象，包含同一 track 的一个或多个连续 Fragment | 可以是独立 URL，也可以是大文件中的 byte range |
| **Fragment** | 独立可解码、可作为切换/随机访问边界的时间区间 | 包含一个或多个 Chunk |
| **Chunk** | 一个 Fragment 中连续 sample 的子集，也是低延迟逐步发布单位 | 通常以一组 fragment metadata 和 media data 形成可消费单元 |

最容易混淆的是最后三项。可以把包含关系写成：

```text
CMAF Segment (addressable object)
├── CMAF Fragment 0 (independent / switching boundary)
│   ├── CMAF Chunk 0.0 (first sample subset)
│   └── CMAF Chunk 0.1 (next sample subset)
└── CMAF Fragment 1
    ├── CMAF Chunk 1.0
    └── CMAF Chunk 1.1
```

在最简单的单 Chunk Fragment 中，Segment、Fragment、Chunk 甚至可能落到同一组字节上；在低延迟直播中，一个较长 Segment 通常包含多个 Fragment 或 Chunk。DASH、HLS、MSE 和 CMAF 对“segment”的可寻址语义也不完全相同，所以看到 URL 名为 `segment_123.m4s`，不能反推里面必然只有一个 `moof`。

#### 7.2 `moof + mdat` 是物理基础，不是完整语义判据

DASH-IF 的公开 CMAF Ingest 文档专门提醒：`moof` 描述 `mdat` 中 sample 的播放和解码属性，一组 `moof/mdat` 根据对象结构和包含关系，可能被称为 CMAF Fragment，也可能是 CMAF Chunk。

对一组可独立追加的媒体数据，最低限度的逻辑是：

```text
[styp] [emsg/prft] moof mdat
                    │    └── encoded/encrypted sample bytes
                    └── track, decode time, sample count/size/flags/offset
```

W3C 的 MSE ISO BMFF byte-stream 约束进一步要求媒体段中的 `moof` 至少包含一个 `traf`，每个相关 `traf` 要有 `tfdt`，`trun` 引用的全部 sample 必须能在后续 `mdat` 中找到，并使用 movie-fragment relative addressing。浏览器不是“收到 `mdat` 就开始猜帧”，而是先消费 `moof` 建立 sample map，再按 offset 和 size 读取 payload。

#### 7.3 Fragment 为什么是 ABR 切换边界

同一 Switching Set 内的 360p、720p、1080p Track 各自有独立 sample 和字节流，但 Fragment 边界需要在共同时间轴上对齐。典型切换过程是：

```text
720p Fragment N  --decode-->  boundary T
                                │
                                └── switch
1080p Fragment N+1 --starts at boundary T with suitable random access sample
```

这里有四个容易被忽略的条件：

1. `tfdt.baseMediaDecodeTime` 必须把各 track fragment 放到正确 decode timeline；
2. `trun` 中的 duration 和 composition offset 必须让 sample 时间连续；
3. 替代 Track 的 Fragment 边界要时间对齐，并满足 codec/config/profile 的 switching constraints；
4. Fragment 起点要具备所需随机访问能力，而普通 Chunk 边界不自动等于新的 IDR/SAP。

因此，Chunk 可以比 GOP 更短。Fragment 的第一个 Chunk 从随机访问 sample 开始，后续 Chunk 可以继续携带依赖同一 Fragment 早先参考帧的 sample。把每个 Chunk 都强行做成 IDR 会增加码率和编码损失，并不是低延迟的必要条件。

#### 7.4 Chunk 为什么能降低直播延迟

传统 Segment 发布模型要等整个媒体段编码和封装完成后，才把对象暴露给播放器。Chunk 模型允许 packager 在一个短 sample 子集完成后就发布：

```text
encode samples
  -> write moof for current run
  -> write matching mdat bytes
  -> publish completed Chunk
  -> player parses and queues it
  -> parent Segment continues growing
```

Apple 的 Low-Latency HLS 文档给出的说明性例子，是把 6 秒 parent segment 拆成约 200 ms 的 Partial Segments/CMAF Chunks。数字本身不是固定标准值，真正的收益是播放器无需等待完整 parent segment。代价则是 box/header 开销、playlist 更新、HTTP/CDN 调度和播放器 append 次数上升。

更小的 Chunk 也不会自动消除所有延迟。端到端延迟仍包含 encoder lookahead、GOP、packager flush、origin/CDN、manifest 可见性、网络抖动和播放器 buffer。只把 `segment_duration` 调小，却不改变发布、缓存和播放策略，常常只会制造更多小文件。

#### 7.5 Widevine/CENC 如何跨越 Header 和 Fragment

CMAF 允许 sample 使用 MPEG Common Encryption。Widevine 在这条结构中的位置不是增加一个“Widevine Fragment”，而是把初始化与逐 sample 状态分散在两处：

```text
CMAF Header
  pssh                  -> DRM init data / KID hints
  encv|enca/sinf/tenc   -> default KID, IV and pattern context

CMAF Fragment / Chunk
  sgpd(seig) + sbgp     -> optional KID/context override
  saiz + saio + senc    -> per-sample IV and subsample metadata
  trun                  -> sample byte ranges
  mdat                  -> encrypted media bytes
```

License Request/Response、设备 Provisioning 和 raw CK 都不属于 CMAF Fragment。即使每 200 ms 发布一个 Chunk，也不意味着每个 Chunk 都要请求一次 License；License 频率由 session、KID 集合、crypto period、renewal policy 和客户端缓存状态决定。工程上常把 key rotation 与 Segment/Fragment 边界对齐以降低状态复杂度，但仍应以实际 `tenc/seig/sbgp` 映射为准。

#### 7.6 一个 Fragment parser 应验证什么

在前文 Box 检查之外，CMAF 层还应增加这些跨对象约束：

1. Header 的 track ID、timescale、sample entry 和 `trex` 能否被每个 `tfhd` 正确引用；
2. 同一 Track 的 `mfhd.sequence_number` 与 `tfdt` 是否按预期前进，是否出现重复、倒退或未声明 discontinuity；
3. `trun` 的 sample duration 累计是否与下一 Chunk/Fragment 的 decode start 连续；
4. `trun.data_offset`、sample size 总和与 `mdat` 边界是否一致；
5. Fragment 起点是否满足 manifest 和 `sidx` 声明的 SAP/random-access 属性；
6. Switching Set 中各 Track 的 Fragment boundary 是否在共同时间线上对齐；
7. Segment/Fragment/Chunk 的 `styp` brands 是否与目标 CMAF profile 和 Header brands 兼容；
8. `saiz/saio/senc` entry 数是否与 Chunk 中相关 sample 对齐；
9. `sbgp/sgpd(seig)` 覆盖是否跨越错误 sample run，effective KID 是否能被当前 License 满足；
10. manifest 声明的 Segment time/duration、实际 `tfdt/trun` 时间和 HTTP byte range 是否三方一致。

最后一条尤其重要。很多“偶发卡顿”“切清晰度黑屏”或“License 明明 usable 仍 decode error”的根因，不在 DRM，而在 manifest、Fragment timeline 和字节范围之间出现了一个 sample 的偏差。

---

### 八、样本加密元数据：从 IV 到 Key Rotation

#### 8.1 `saiz + saio + senc`：三个 box 如何拼成一条记录

这三个名称经常被一句“存 IV”带过，实际分工更精确：

- `senc`：承载或表示每个 sample 的 encryption entry，包含 IV，以及可选的 subsample pairs；
- `saiz`：给出每个 sample 对应 auxiliary encryption entry 的字节长度，长度相同时可用一个 default size；
- `saio`：给出 auxiliary information 相对相应基准的 offset，帮助 parser 找到实际记录。

一个启用 subsample encryption 的 `senc` entry 可以抽象成：

```text
sample_iv[iv_size]
subsample_count
repeat subsample_count times:
  bytes_of_clear_data
  bytes_of_protected_data
```

对视频来说，保留部分 codec framing/NAL header 为 clear 很常见。`bytes_of_clear_data + bytes_of_protected_data` 的累计结果必须与 sample size 对齐；对 CBC/pattern scheme，还要继续验证受保护区间的 block/pattern 约束。

三者的读取顺序不是“先看到谁就信谁”，而是：

```text
effective tenc/seig context
  -> 得到 IV size / constant IV / pattern / KID
saio
  -> 找到 auxiliary data
saiz
  -> 切分每个 sample 的 auxiliary entry
senc semantics
  -> 解析 IV 与 subsample clear/protected ranges
trun
  -> 找到 mdat sample bytes
```

Shaka Packager 当前的 fragmented MP4 写法让 `saio` 指向 `senc` 内的 sample encryption data，并把 `senc` 放在 `traf` 的末尾。但这是具体 muxer 的稳定实现行为，不应被 parser 偷换成“所有合法文件中 `senc` 必须永远位于最后”的无条件假设。

#### 8.2 `sgpd(seig) + sbgp`：key rotation 如何覆盖 `tenc`

当一条 track 在不同 sample 范围使用不同 KID 时，重新写一个 `tenc` 不现实，因为 `tenc` 位于初始化段。CENC 使用 sample group 解决：

- `sgpd` 的 grouping type 为 `seig` 时，存放一个或多个 CENC Sample Encryption Information Group Entry；
- 每个 `seig` entry 可以携带 `is_protected`、per-sample IV size、KID、pattern 和 constant IV；
- `sbgp` 把连续 sample run 映射到某个 group description index；
- 没有映射到覆盖项的 sample 继续使用 `tenc` 默认上下文。

可以把有效参数写成：

```text
effective_encryption(sample) =
  mapped seig entry, if sbgp selects one
  otherwise tenc defaults
```

因此，看到 MPD 或 `tenc` 中只有一个 default KID，不代表整个 segment 只使用一个 KID。要正确审计轮换，必须同时遍历 track/fragment level 的 `sgpd`、`sbgp`，并处理 group description index 的作用域。

#### 8.3 三组默认值的优先级

fragmented MP4 的难点不仅是 box 多，还在于字段可以继承和覆盖。

**Sample timing/size/flags：**

```text
trun per-sample field
  > tfhd fragment default
  > trex track-fragment default
```

**Encryption context：**

```text
sbgp selected sgpd(seig) entry
  > tenc track default
```

**Codec identity：**

```text
encrypted sample entry encv/enca
  -> sinf/frma original format
  -> corresponding codec configuration box
```

把这些继承关系写成明确的数据结构，比在解密循环里到处写 fallback 判断可靠得多。否则 parser 很容易在某个 optional flag 缺失时使用未初始化的 size、IV 或 KID。

#### 8.4 Box 级一致性检查清单

对自己打包的文件，推荐按以下顺序检查：

1. `ftyp/styp` brand 是否与目标 CMAF/DASH profile 一致；
2. 每条 `trak` 的 `tkhd.track_ID` 是否能被对应 `tfhd.track_ID` 找到；
3. `stsd` 的 active sample description 是否为预期 `encv/enca`；
4. `frma` 与 codec configuration 是否一致，例如 `frma=avc1` 对应 `avcC`；
5. `schm.scheme_type` 是否为目标 `cenc/cbcs`；
6. `tenc.default_KID` 是否与 MPD/打包配置一致；
7. `trun` 的 sample count、size 和 data offset 是否落在 `mdat` 边界内；
8. `saiz.sample_count`、`senc` entry count 与相关 sample count 是否一致；
9. 每个 subsample pair 的 clear/protected bytes 累计是否等于 sample size；
10. `sbgp` 覆盖的 sample run 是否越界，group index 是否能在对应 `sgpd` 中解析；
11. 有效 KID 是否出现在预期 PSSH/License 授权集合中；
12. 任意未知 box 是否能按声明长度安全跳过，不破坏 sibling 边界。

因此，“成功解析 PSSH”只证明你读懂了 DRM 初始化数据；“成功解析 `moov`”也只证明你得到了默认上下文。只有把 `moof` 的逐 sample mapping 和 `mdat` 字节范围一起验证，才算真正读懂了 CENC 数据面。

#### 8.5 多轨、多 KID 与密钥轮换

生产内容常见三种切分：

1. 音频和视频使用不同 KID；
2. SD、HD、UHD/HDR 轨道使用不同 KID；
3. Live 或长内容按 crypto period 轮换 KID/CK。

这种切分不仅是密码学 hygiene，也让 License Service 能按设备能力和业务权利只签发一部分 key。服务端即使允许音频和 SD，也可以不给 UHD KID。客户端宣称支持 4K，并不意味着 License 里会出现 4K 对应的 key。

#### 8.6 多 DRM 的最弱路径效应

同一份 CMAF 资产可以让 Widevine 和 PlayReady 共用 CK，只放不同 PSSH/License 封装。这降低了存储和打包成本，也引入一个直接的安全后果：

> 如果多个 DRM 最终授权同一把 CK，内容的可复制性要按最弱的客户端、最宽的策略和最松的服务端路径重新评估。

单独把 Widevine L1 做得很强，并不能抵消另一 DRM 对同一 UHD KID 签发软件级、长周期、可离线的宽松授权。多 DRM 安全评审必须从 `asset -> track -> KID -> CK -> every license path` 建图，而不是分别审三份 SDK checklist。

---

## Part II：客户端、设备身份与授权生命周期

对照 Chrome、Android、Partner Proxy 和 Provisioning 四条状态线。

### 九、浏览器路径：EME 只接线，不交出 Key

在分别下钻 API 之前，先把 Chrome desktop 与原生 Android 放到同一张通信图里。两边的服务端 License loop 几乎同构，但本地调用会穿过完全不同的进程、HAL 与硬件边界。

{{< cocoon-diagram
  src="images/widevine-deep-dive/client-communication-architecture.html"
  title="Chrome and Android Widevine Communication Architecture"
  height="1160"
>}}

这张图有意把“授权消息”和“媒体数据”分成两条线：

- 红/紫虚线是 License Request/Response。应用负责 HTTPS transport，但拿到的是 opaque bytes；
- 蓝色实线是 MPD、PSSH、加密 sample 和 decode 后的 frame/buffer；
- Chrome desktop 通过 Mojo 把 library CDM 放进独立 CDM utility process；
- Android 通过 `MediaDrm`、Binder、`DrmHal/CryptoHal` 和 AIDL vendor plugin 进入 Widevine/OEMCrypto；
- 两边都先访问 Partner License Proxy，而不是从客户端直连 Widevine Cloud License Service。

#### 9.1 同一个 License Loop，两套本地 ABI

| 阶段 | Chrome / EME | Android / MediaDrm |
|------|--------------|--------------------|
| init data 入口 | `encrypted` event / `generateRequest("cenc", initData)` | app/player 从 manifest/init segment 取得 init data |
| 创建 session | `MediaKeys.createSession()` | `MediaDrm.openSession()` |
| 生成请求 | CDM callback 触发 `MediaKeyMessageEvent` | `getKeyRequest()` 返回 opaque `byte[]` |
| 网络发送 | Web App 用 Fetch/XHR 发给 Partner Proxy | App/Media3 的 DRM callback 发给 Partner Proxy |
| 安装响应 | `MediaKeySession.update(response)` | `provideKeyResponse(scope, response)` |
| 本地系统边界 | Renderer -> Browser broker -> Mojo `CdmService` -> `CdmAdapter`/library CDM | App -> Binder -> `mediadrmserver` -> AIDL `IDrmPlugin`/`ICryptoPlugin` |
| 媒体路径 | MSE/demux -> CDM-backed decryptor/decoder -> GPU/OS media path | extractor -> `MediaCrypto/CryptoHal` -> OEMCrypto/TEE -> `MediaCodec` secure decoder |

这张对照表也解释了一个常见误区：网络请求通常由应用层代码发出，不代表 License 是“发给 JavaScript”或“发给 Android App”的。应用只是协议搬运工；真正消费 response、建立 key status 和绑定 session 的是 CDM/DRM plugin。

#### 9.2 EME 的职责边界

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

#### 9.3 Chrome 里的进程边界

Chromium 公开了 CDM shared-library interface，但正式 Widevine CDM 不是 Chromium 仓库里的完整开源实现。概念上可以分成：

```text
Renderer process
  Web App / MediaKeySession
    -> Blink EME / MediaKeys
    -> MojoCdm client

Browser process
  MediaInterfaceProxy / InterfaceFactory broker

CDM utility process (desktop library-CDM path)
  CdmService
    -> MojoCdmService
    -> CdmAdapter
    -> proprietary Widevine library CDM

Media pipeline
  MSE / demuxed encrypted buffer
    -> CDM-backed decryptor or decoder
    -> platform decoder / GPU / compositor / display
```

Chromium 的公开 media/mojo 文档把 `MediaInterfaceProxy` 描述为媒体接口请求的 central hub；桌面启用 library CDM 时，`ContentDecryptionModule` 请求会被转发到运行在 CDM utility process 中的 `CdmService`。`CdmService` 只承载 CDM 接口，不等于整个播放器和 decoder 都进入同一个进程。

session 通信是双向的：`CreateSessionAndGenerateRequest()` 把 init data 交给 CDM；CDM 的 session message、keys change、expiration update 再经 Mojo callback 回到 Renderer，最终表现为 EME event。网页随后自行完成 HTTPS 请求，并用 `session.update()` 把 opaque response 原路送回。

具体进程名、sandbox 归属和 decode 路径会随 Chromium 版本、OS 和硬件能力变化。安全分析应追踪“消息和 buffer 穿过了哪些 trust boundary”，不要把某一版 `ps` 输出当成永久架构。

桌面软件 CDM 的现实约束也很清楚：如果密钥使用和解密长期发生在通用 CPU/普通进程，强混淆与完整性校验只能提高逆向成本，不能制造硬件隔离。ChromeOS、Android 和特定平台可能提供不同的硬件路径，因此“Chrome = L3”同样是过度概括。

#### 9.4 能力协商不是授权结果

EME configuration 里的 codec、robustness、persistent state 和 distinctive identifier 是客户端能力与隐私协商。服务端最终是否发 UHD/HDR key，还会结合设备状态、账号权利、内容策略和输出能力。

换句话说，改 JavaScript 让 `requestMediaKeySystemAccess` 返回成功，最多改变前端分支；它不能替代设备凭据，也不能命令 License Service 在响应里加入原本不该下发的 KID。

---

### 十、Android 路径：从 `MediaDrm` 到 OEMCrypto

#### 10.1 Framework 与 vendor plugin

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

#### 10.2 `IDrmPlugin` 与 `ICryptoPlugin` 为什么要分开

AOSP 把控制面和数据面拆成两类 vendor interface：

| Interface | 控制对象 | 典型动作 |
|-----------|----------|----------|
| `IDrmPlugin` | session、Provisioning、License、key status、secure state | open/close session、get/provide key response、query status |
| `ICryptoPlugin` | 绑定 session 的 sample crypto context | 接收 subsample layout、secure buffer handle，执行受约束 decrypt |

`DrmHal` 负责前一类，`CryptoHal` 负责后一类；它们由 `mediadrmserver` 创建，再通过 AIDL 调用 vendor/SoC 的实现。这样做不是为了让应用更方便，而是为了让 License state 与高吞吐 sample path 各自拥有清晰 ABI 和权限边界。

一轮 streaming key acquisition 可以展开为：

```text
1. app -> MediaDrm.openSession()
2. app -> MediaDrm.getKeyRequest(sessionId, initData, mimeType, STREAMING, params)
3. MediaDrm -> DrmHal -> IDrmPlugin -> Widevine implementation
4. opaque KeyRequest returns to app
5. app -> Partner License Proxy -> Widevine License Service
6. opaque response returns to app
7. app -> MediaDrm.provideKeyResponse(sessionId, response)
8. plugin verifies/binds keys and publishes key status
9. MediaCrypto(sessionId) is attached to MediaCodec
10. encrypted samples enter queueSecureInputBuffer()
```

第 4、6 步经过 app 并不等于 app 获得 CK。第 9、10 步也不保证一定是 L1：是否要求 secure decoder、buffer 是否进入 protected memory、最终安全级别是什么，都要查询实际 session 和 codec 能力。

Provisioning 是另一条状态机。`NotProvisionedException` 或 provision-required event 出现时，应用取得 opaque `ProvisionRequest`，发送到其推荐 provisioning endpoint，再把 response 交回 `provideProvisionResponse()`。它解决设备凭据，不应和内容 License API 合并成一个“拿 key 请求”。

#### 10.3 `MediaDrm` 的公开安全枚举

Android API 公开的不是简单三个等级，而是五种具体能力：

| Android 枚举 | DRM key/crypto | decode | 完整媒体处理 |
|--------------|----------------|--------|--------------|
| `SW_SECURE_CRYPTO` | 软件 | 不承诺安全 | 不承诺安全 |
| `SW_SECURE_DECODE` | 软件保护 | 软件安全解码语义 | 不承诺硬件路径 |
| `HW_SECURE_CRYPTO` | 硬件/TEE | 可在普通路径 | 不保证完整硬件媒体链 |
| `HW_SECURE_DECODE` | 硬件/TEE | 硬件安全解码 | 未必覆盖全部媒体处理 |
| `HW_SECURE_ALL` | 硬件/TEE | 硬件安全解码 | 压缩与非压缩媒体都在硬件支持的可信环境处理 |

`getSecurityLevel(sessionId)` 返回 session 当前级别，`requiresSecureDecoder(mime, level)` 则回答指定 codec/级别是否要求 secure decoder。这比读一个厂商字符串更适合做能力判断。

#### 10.4 L1/L2/L3 与 Android 枚举不能机械画等号

Widevine 业界常用的三层概念可以这样理解：

| Widevine 级别 | 核心边界 | 典型风险位置 |
|---------------|----------|--------------|
| **L1** | 密钥处理、解密、解码和关键媒体路径进入安全硬件/TEE | TEE、OEMCrypto、secure decoder、驱动与输出集成 |
| **L2** | 密钥和 crypto 在安全硬件，解码/像素路径可离开安全环境 | 解密后的压缩流或普通 decode 路径 |
| **L3** | 密钥处理与 crypto 主要在软件环境 | CDM 进程、混淆实现、内存与软件输出路径 |

这张表是安全模型，不是 Android API 的强制转换表。`HW_SECURE_CRYPTO` 很像 L2 的核心特征，`HW_SECURE_ALL` 很像 L1 目标，但具体认证、codec、SoC 和服务策略仍由 Widevine/OEM 集成决定。

最重要的一句是：**安全级别描述客户端执行能力，不直接定义内容分辨率。** `MediaDrm.openSession(level)` 的官方文档只说降低安全级别通常会被 License policy 限制到更低分辨率；“通常”不是“协议固定”。720p、1080p、4K 与 HDR 的门槛是服务方策略，不是 L1 字符串的数学函数。

#### 10.5 OEMCrypto 守的是什么

Widevine 官方公开页只把 OEMCrypto、Keybox 和 Provisioning 标为设备集成组件，详细接口和合规要求属于授权资料。结合 AOSP 边界与公开研究，可以谨慎地描述其角色：

- 把设备凭据、License key derivation 和内容 key loading 留在受保护实现中；
- 为加密 sample 提供受约束的 decrypt/decode 接口；
- 维护 session、usage、nonce、时间或 rollback 相关安全状态；
- 把受保护 buffer 交给 secure decoder，而不是向 REE 返回裸 CK。

这里必须区分“公开接口角色”和“某个版本的逆向布局”。函数编号、key ladder 常量、trustlet 消息和内存结构都可能随 OEMCrypto/API 版本变化，不应拿一篇论文里的单个设备样本冒充所有 L1 实现。

---

### 十一、License 控制面：为什么客户端不能直连 Google

#### 11.1 两种签发方式

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

#### 11.2 Proxy 不是反向代理配置文件

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

#### 11.3 生产 License 协议为何不应靠猜

Widevine PSSH proto 和 Common Encryption Key API 的一部分在 Shaka Packager 中公开，但客户端生产 License message、设备证书、策略和 OEMCrypto 细节并没有形成一份等价于 PlayReady Header Specification 的完整公开规范。正式合作方应以 Widevine Portal、Proxy SDK 和 License Server SDK 随授权交付的文档为准。

社区项目可以帮助阅读 protobuf 名称和研究历史版本，却不能成为生产兼容性或安全合规的唯一依据。把某个非官方 `license_protocol.proto` 直接固化到服务端，未来最先遇到的不是“破解成功”，而是版本、字段语义、签名和设备状态判断全面漂移。

---

### 十二、Provisioning 与设备身份：License 到底发给谁

#### 12.1 Provisioning 是 License 之前的信任建立

Android `MediaDrm` 公开 API 明确区分 Provisioning 和 Key Request：

- `getProvisionRequest()` 产生 opaque provisioning request；
- 应用按返回的 default URL 交给 provisioning server；
- `provideProvisionResponse()` 安装响应；
- 之后才能正常 open session 或获取 key。

也就是说，Provisioning 不是“第一次 License 请求的别名”，而是向设备分发或更新设备唯一凭据的独立阶段。未 provision、凭据损坏或被撤销，都可能让后续授权直接失败。

#### 12.2 Keybox、设备证书和 L1 不应混成一个词

公开 Widevine 概览把 Keybox 列为设备集成组件；历史 Android 研究又经常把设备身份根、keybox、WVD 文件和 provisioned certificate 混着说。更稳妥的边界是：

- 制造/设备根材料负责建立不可随意复制的设备身份；
- Provisioning 把设备注册到 Widevine 信任体系并获取可用于协议的凭据；
- License Request 证明或携带这些身份状态；
- License Service 根据身份、实现和策略生成绑定响应；
- OEMCrypto/CDM 在设备侧验证、派生并受约束地使用 key。

不同年代、平台和安全级别可能采用不同材料与封装。审计时应问“哪个 secret 是 root、存在哪里、如何更新和撤销”，而不是看到一个叫 keybox 的文件就断言掌握了整套设备信任。

#### 12.3 隐私边界

设备身份越稳定，越容易成为跨站跟踪信号。Android 安全文档明确提到，浏览器中的 Widevine Client ID 会按应用包和 Web origin 返回不同值。EME 也把 distinctive identifier 与 persistent state 放进显式能力/权限模型。

这说明 DRM 的隐私目标不是“设备没有身份”，而是尽量避免把同一个可链接标识无条件暴露给所有站点。公开研究已经指出浏览器实现偏差可能重新引入可链接性，因此隐私评审要覆盖浏览器/CDM 实现，而不能只看 EME 规范的理想流程。

#### 12.4 从 Provisioning 到 Secure Playback 的完整时序

把 Provisioning、License 和播放混在一张抓包时序里，很容易误判“第一次请求为什么没有 PSSH”，或者把设备证书响应当成内容 License。下面这张图把三段状态机拆开：

{{< cocoon-diagram
  src="images/widevine-deep-dive/provision-license-playback-flow.html"
  title="Widevine Provisioning, License and Secure Playback Flow"
  height="1160"
>}}

图里的参与者是逻辑边界，不承诺所有平台都以相同进程和 HTTP 栈实现。尤其要区分 Android 与浏览器：

1. 播放器先创建 DRM/CDM 上下文并尝试打开 session；
2. Android 若没有可用设备证书，相关操作会抛出 `NotProvisionedException`；
3. 应用调用 `getProvisionRequest()`，取得 opaque request 和目标 URL；
4. 应用只负责经 HTTPS 搬运 request/response，再调用 `provideProvisionResponse()` 安装凭据；
5. 浏览器侧的 Individualization 可能由 User Agent/CDM 直接完成，网页 JavaScript 不一定能观察到与 Android 等价的 provisioning API；
6. Provisioning 成功后，客户端才根据 MPD/PSSH、KID 和 session type 生成内容 License Request；
7. App/Web App 把 opaque request 连同账号 token、asset 和播放上下文送到 Partner License Proxy；
8. Proxy 校验套餐、地区、并发、设备风险与 KID 映射，再附加业务规则请求 Widevine fulfillment；
9. 生成后的个性化 License 作为 opaque response 原路返回，并由 `update()` 或 `provideKeyResponse()` 消费；
10. CDM/OEMCrypto 建立 key handle 与 policy state，公开应用 API 不返回 raw CK；
11. 加密 sample 携带 IV、subsample 和 KID 元数据进入 crypto/decode path；
12. 续租、key rotation、offline restore/release、过期和输出限制继续驱动后续状态变化。

因此，Provisioning 回答的是“这台客户端能否获得一个受信的设备身份”，License 回答的是“这个身份在当前业务上下文里能否使用哪些 KID”，Secure Playback 回答的才是“这些 key 能在哪里、以什么输出条件被使用”。三者缺一不可，也不能互相代替。

---

### 十三、License Policy：时间、状态、输出与轮换

#### 13.1 Streaming、Offline 与 Release

从 EME 和 `MediaDrm` 暴露的会话能力，可以把常见授权生命周期理解为：

| 类型 | 状态位置 | 典型动作 |
|------|----------|----------|
| streaming / temporary | session 内存 | 获取、播放、续租、关闭 |
| offline / persistent | 设备持久安全状态 | 下载、恢复 key set、续期、显式释放 |
| release | 客户端生成释放请求 | 服务端确认后清理离线授权 |

离线 License 不是“把 Response 存个文件”。它还要处理设备绑定、安全时间、播放窗口、存储回滚、renewal 和 release。Android 提供 `getOfflineLicenseState()`、`removeOfflineLicense()` 等 API，本身就说明持久 License 是受 DRM 状态机管理的对象。

#### 13.2 Renewal 也是并发控制

旧 Android API 中的 Secure Stop 曾用于确认 key session 生命周期；相关接口在 API 33 已被标记 deprecated，官方建议通过周期性 License renewal 管理并发播放。

这揭示了一个实用设计：并发限制不必只靠“开始播放时计数 + 客户端退出时减一”。短租约和续租可以把掉线、进程崩溃和恶意不释放，转换成服务端可过期的 lease。代价是续租服务必须高可用，而且不能让网络抖动误伤正常播放。

#### 13.3 HDCP 与输出保护

Android `MediaDrm` 公开了 `getConnectedHdcpLevel()`、`getMaxHdcpLevel()`，错误码中也明确区分 `ERROR_INSUFFICIENT_OUTPUT_PROTECTION` 与 `ERROR_INSUFFICIENT_SECURITY`。这两类失败不应混为一谈：

- security 不足：DRM key/crypto/decode 的执行环境不满足策略；
- output protection 不足：设备内部可以安全处理，但外接显示链路不满足 HDCP 等要求。

服务端策略还应考虑没有数字输出、显示热插拔、镜像、虚拟显示、远程桌面和 secure surface 变化。只在 License 签发瞬间检查一次 HDMI 状态，并不等于整个播放期间都满足输出保护。

#### 13.4 Crypto Period：Live 把一次授权变成持续协议

Shaka Packager 公开的 Widevine Common Encryption API 支持：

- `first_crypto_period_index`；
- `crypto_period_count`；
- `crypto_period_seconds`；
- 每个 period 返回独立 KID、key、IV 与 PSSH 数据。

在 Live 中，播放器需要在新 period 到来前拿到对应 key。轮换间隔太长，单次泄露窗口扩大；间隔太短，License QPS、CDM session 数、manifest 更新和边缘网络抖动会同时上升。

#### 13.5 轮换不等于自动撤销历史风险

密钥轮换解决的是 blast radius 和时间分段，不会自动完成：

- 吊销已经泄露的设备凭据；
- 阻止旧 License 在有效期内继续使用；
- 修复错误签发给低安全客户端的高价值 KID；
- 清除 CDN 或日志中误写的 CK；
- 弥补所有 period 使用可预测 key derivation 的 KMS 设计。

轮换策略必须和 License duration、renewal、revocation、KMS audit 一起设计。只把 period 从 24 小时改成 10 分钟，可能只是把服务端复杂度放大 144 倍。

---

## Part III：OTT 场景与安全评估

先看主流服务如何使用，再按攻击面判断 Widevine 的真实边界。

### 十四、哪些 OTT App 在使用 Widevine，它们实际保护什么

#### 14.1 先纠正“基于 Widevine 构建”这个说法

Google 的 [Widevine 官方概览](https://developers.google.com/widevine/drm/overview) 直接列出了 Google Play、YouTube、Netflix、Disney+、Amazon Prime Video、HBO Max、Hulu、Peacock、Discovery+ 和 Paramount+。这足以证明它们是 Widevine 生态的公开合作方，但不能推出三个过度结论：

1. 这些 App 的全部安全体系都由 Widevine 提供；
2. 它们在每个平台、每个标题和每个清晰度上都使用 Widevine；
3. 它们采用相同 License policy、L1 门槛或服务端实现。

更准确的表述是：**这些 OTT 服务在 Chrome、Android、Android TV、Fire OS、部分智能电视等 Widevine 平台上，把 Widevine 作为多 DRM 交付矩阵中的一条内容保护路径。** 同一服务在 Apple 设备上通常需要 FairPlay，在 Windows、Xbox 或部分电视生态中还可能走 PlayReady。媒体资产可以共用 CENC/CMAF，License 和设备信任路径则按平台分开。

#### 14.2 十类公开确认的服务与典型场景

下表中的“Widevine 承载点”是根据公开架构做的边界映射，不代表服务商公开了每个内部 License 字段。具体功能会随地区、套餐、标题和设备变化。

| OTT App / 服务 | 典型业务场景 | Widevine 在适用平台可承载的保护 | 仍由服务自身负责的部分 |
|----------------|--------------|--------------------------------|--------------------------|
| **Google Play / Google TV** | 电影租赁、购买、跨设备播放、移动端离线 | persistent/offline License、租期与播放窗口、设备绑定、输出策略 | 订单、所有权账本、退款、地区版权 |
| **YouTube** | 付费电影、高价值内容、Living Room 与直播频道 | L1 secure decode、HDCP、key rotation、多 KID 与分辨率策略 | 频道权利、广告、账号、直播授权与风控 |
| **Netflix** | 订阅 VOD、UHD/HDR、移动端下载 | 设备安全等级、offline License、续租、HDCP 与高价值轨道隔离 | MSL/账号会话、套餐、Household、并发、推荐和 CDN token |
| **Disney+** | 影视订阅、4K/HDR、移动端离线 | 高清轨道策略、设备绑定、离线有效期与 protected output | 套餐、地区 catalog、儿童档案、并发和账号风控 |
| **Amazon Prime Video** | 订阅、租赁、购买、离线与直播活动 | streaming/offline/release 生命周期、租购窗口、轮换与输出限制 | 订单、频道订阅、PIN、并发和地区规则 |
| **HBO Max / Max** | 精品影视、UHD 与部分地区直播体育 | 高价值 KID、L1/secure path、HDCP、短期 License/renewal | 套餐、赛事权利、账号共享策略与区域授权 |
| **Hulu** | 订阅 VOD、Live TV、移动端下载 | VOD License、Live key rotation、离线恢复与到期状态 | 直播频道包、地理位置、广告和并发限制 |
| **Peacock** | 广告/订阅 VOD、线性频道、体育直播 | CENC、直播轮换、设备策略、输出保护 | 广告决策、赛事 blackout、账号和播放并发 |
| **Discovery+** | 纪实内容、订阅 VOD、部分市场体育 | 多设备 License、UHD/HD 档位、续租和输出约束 | 区域 catalog、套餐、赛事/频道 entitlement |
| **Paramount+** | 影视 VOD、线性频道与体育直播 | VOD 与 Live License、crypto period、secure decode/HDCP | CBS/赛事地区权、套餐、广告、并发和风控 |

这里最值得观察的不是品牌数量，而是**同一个 DRM 状态机如何承载不同商业模型**。

#### 14.3 五种业务场景，五组不同的 License 重点

**订阅 VOD。** Netflix、Disney+、Max 一类服务更关心套餐是否覆盖当前标题、设备是否允许目标分辨率、License 多久续一次，以及外接显示链是否满足 HDCP。Widevine 可以执行 key usage 与输出策略，但“用户有没有订阅”仍由 Partner Proxy 判断。

**租赁与购买。** Google Play/Google TV、Prime Video 的租购场景需要区分“订单长期存在”和“本次播放 License 有效”。购买记录可以长期保留，License 仍可短期签发并周期更新；租赁还需要未开始窗口、首次播放后的倒计时和设备数量限制。不能把支付数据库的一行订单直接等同于永不过期的 DRM License。

**离线下载。** Netflix、Disney+、Prime Video 等移动应用需要把 encrypted media 与 persistent/offline License 一起管理。License 可绑定设备、账号、下载槽位和安全时间，并支持 renewal/release。把 `.mp4` 缓存到磁盘只是数据面；真正决定断网后还能否播放的是 DRM 持久状态。

**Live TV 与体育。** Hulu Live TV、Peacock、Paramount+ 和部分地区的 Max/Discovery+ 需要短 License、持续 renewal、crypto-period key rotation 和快速 revocation。赛事 blackout、地区限制和订阅包由 Proxy 决策，Widevine 负责让对应 KID 的授权在设备上按时生效或失效。

**UHD/HDR 与 Living Room。** 高价值档位通常要求更强的 device robustness、secure decoder 和输出保护。YouTube 的公开 Living Room 设备要求把 1080p 以上 High Value Content 与 Widevine L1、secure hardware decode 和 HDCP 关联；Netflix 的帮助文档也把 4K 外接显示链与 HDCP 2.2 放在同一组前提中。这说明“拿到 License”与“允许输出目标画质”是两个独立门槛。

#### 14.4 Widevine 明确不负责什么

一次 OTT 安全评审如果只画 DRM，会漏掉更大的业务面：

- 登录、支付、订阅和家庭成员关系；
- geo-block、VPN/代理判断、赛事 blackout 和版权窗口；
- CDN signed URL、manifest token 与防盗链；
- 并发播放、设备槽位和异常账号共享检测；
- App 完整性、root/debug 风险、设备 attestation；
- 服务端 watermarking、泄露溯源和盗版监测；
- 广告决策、反作弊、遥测和风控模型。

这些控制可以影响 Partner Proxy 是否签发、签发哪些 KID、License 多长、是否要求 L1/HDCP，却不是 Widevine CDM 自动提供的功能。反过来，业务 token 做得再复杂，也不能替代 secure key use 和 protected media path。

#### 14.5 评估一个 OTT App 时应该问什么

1. 当前平台实际选择了 Widevine、PlayReady 还是 FairPlay，是否存在降级路径？
2. SD、HD、UHD/HDR、音频是否使用不同 KID 与不同安全策略？
3. Provisioning 失败、凭据撤销和 CDM 版本过旧时，是拒播、降档还是切换 DRM？
4. Streaming、offline、renewal、release 的状态是否都绑定账号、asset、KID 和设备？
5. Live rotation 的提前量、session 数和 License QPS 能否承受边缘网络抖动？
6. Partner Proxy 是否从可信 catalog 反查 KID，而不是接受客户端任意指定？
7. L1、secure decoder、HDCP 和 protected surface 是否在播放全过程持续验证？
8. 多 DRM 共用 CK 时，另一条路径是否能获得更宽松的同一把 key？

这套问题比“App 有没有 Widevine”更接近真实安全结论。前者是在查一个 SDK，后者是在审一条从订单、License 到屏幕的授权链。

---

### 十五、从安全角度评估 Widevine

#### 15.1 它真正做强的地方

Widevine 的强项不是隐藏 MPD，而是把可规模化攻击拆成多个需要同时成立的条件：

1. CENC 让 CDN 与存储只处理密文；
2. 设备 Provisioning 和 License 个性化抑制跨设备响应重放；
3. Partner Proxy 把 DRM 证据与账号业务权利绑定；
4. L1/OEMCrypto 把 key use、decrypt、decode 和输出压进硬件信任路径；
5. 多 KID、短 License 和 renewal 缩小单点泄露窗口；
6. 撤销与设备状态允许服务方把已知失陷实现移出高价值授权集合。

它并不保证攻击不可能，而是努力把“复制一段响应即可无限扩散”变成高成本、设备相关、容易观测且可以撤销的对抗。

#### 15.2 网络攻击者

网络侧可以看到域名、时序、包长和加密媒体流量，但 HTTPS 保护 License Proxy 传输。即使在受控客户端上看到完整 Request/Response，设备个性化和 session 状态也应阻止把响应搬到另一设备直接使用。

剩余风险主要在：TLS 终止点、代理日志、调试抓包配置、错误遥测、CDN token 和 License API 的认证授权。尤其要检查 APM 是否把 opaque License body 当“便于排障的 payload”持久化。

#### 15.3 控制 Web App 或普通 Android 进程的攻击者

这类攻击者可以改 JavaScript、hook EME/`MediaDrm` 调用、观察 IPC 和网络，也可以伪造 UI 层 capability。对 L3，秘密和攻击者长期处于同一通用执行环境，安全性很大程度依赖实现复杂度、混淆、完整性与快速撤销。

对 L1，REE 原本就应被视为不可信。正确目标不是阻止 REE 看见所有消息，而是保证：

- Request/Response 离开设备安全状态后不能独立使用；
- CK 不通过普通 API 返回；
- 解密后的高价值 sample 不落入普通共享内存；
- secure decoder 和 protected surface 不能被替换成普通输出而保持同等授权；
- TEE 调用严格校验 handle、长度、session 和 nonce。

#### 15.4 L1 把攻击面搬到了哪里

“进入 TEE”是边界变化，不是安全证明。L1 的残余风险包括：

- secure boot 与 anti-rollback 配置错误；
- DRM HAL、TEE driver 或 trustlet 消息解析漏洞；
- 共享 buffer 的 ownership、lifetime 和 bounds 错误；
- secure decoder、display compositor 或 HDCP 集成缺陷；
- debug fuse、工程固件和生产 provision 流程失控；
- OEM 把本应留在 secure world 的中间结果送回 REE；
- 设备证书批量泄露、错误签发或撤销传播过慢。

这也是为什么安全等级不能只靠客户端上报。License Service 需要基于受认证的设备/实现状态做决策，并保留按 OEM、model、build、CDM version 和 credential 批次快速降级的能力。

#### 15.5 License Proxy 与 KMS 往往更脆

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

#### 15.6 公开研究给出的边界

近年来的 Widevine 研究大致分成四类：

1. 逆向 L3 软件实现与 key ladder；
2. 分析 Android L1/OEMCrypto 组件和信任边界；
3. 形式化验证 EME/Widevine 协议目标；
4. 研究浏览器实现造成的隐私和 replay 问题。

这些工作共同说明：协议设计、CDM 实现、浏览器 glue、OEM 固件和服务端策略缺一不可。拿一项论文结果宣布“Widevine 已破”不严谨；拿认证标签宣布“L1 不可破”同样不严谨。安全结论必须注明具体版本、平台、攻击权限、目标资产和是否可规模化。

---

## Part IV：实验、参考实现与审计清单

最后处理可复现实验、项目选型、多 DRM 对照与落地检查项。

### 十六、一个合法、可重复的 Widevine 实验室

#### 16.1 实验目标

```text
自有 clear MP4
  -> Shaka Packager CENC/cbcs 打包
  -> 生成 Widevine SystemID + PSSH protobuf
  -> 检查 MPD / tenc / senc / PSSH / KID
  -> 使用自建测试授权或 Clear Key 验证通用 EME plumbing
  -> 使用官方/合作方测试环境验证 Widevine，不接生产 OTT endpoint
```

raw key 实验只能验证 CENC 数据面和 Widevine 信令。它不会凭空生成受 Google 信任的 Device Credential、Cloud License 权限或生产 CDM。

#### 16.2 固定 Shaka Packager 版本

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

#### 16.3 打包自己的测试媒体

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

#### 16.4 使用 Widevine Key Service 的参数边界

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

#### 16.5 交叉检查产物

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

#### 16.6 播放侧实验

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

### 十七、参考项目地图：谁能证明哪一层

#### 17.1 Google、Android 与标准

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

#### 17.2 打包、解析与播放

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

#### 17.3 研究项目与论文

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

### 十八、和 PlayReady 放在一起看

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

### 十九、实现和审计时最值得记住的十二条

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

### 二十、结语：真正的边界在 PSSH 之后

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
19. [Shaka Packager MP4 Box Definitions](https://github.com/shaka-project/shaka-packager/blob/main/packager/media/formats/mp4/box_definitions.h)
20. [Shaka Packager MP4 Segmenter](https://github.com/shaka-project/shaka-packager/blob/main/packager/media/formats/mp4/segmenter.cc)
21. [Chromium Media Mojo Architecture](https://chromium.googlesource.com/chromium/src/+/main/media/mojo/)
22. [Chromium MP4 Box Definitions](https://chromium.googlesource.com/chromium/src/+/main/media/formats/mp4/box_definitions.h)
23. [Android `NotProvisionedException`](https://developer.android.com/reference/android/media/NotProvisionedException)
24. [Android NDK `AMediaDrm_getProvisionRequest`](https://developer.android.com/ndk/reference/group/media#amediadrm_getprovisionrequest)
25. [YouTube Living Room Hardware and Media Format Requirements](https://developers.google.com/youtube/devices/living-room/files/pdf-guides/Revised_YouTube_Hardware_And_Media_Format_Requirements_for_CE_and_Operator_Devices_2020.pdf)
26. [Netflix: Ultra HD and HDCP Requirements on Windows](https://help.netflix.com/en/node/23931)
27. [Prime Video Usage Rules](https://www.primevideo.com/help/?language=en_US&nodeId=G202095500)
28. [Disney+ Plans, 4K and Downloads](https://help.disneyplus.com/article/disneyplus-price)
29. [ISO/IEC 23000-19:2024: CMAF for Segmented Media](https://www.iso.org/standard/85623.html)
30. [Apple: About CMAF with HTTP Live Streaming](https://developer.apple.com/documentation/http-live-streaming/about-the-common-media-application-format-with-http-live-streaming-hls)
31. [W3C ISO BMFF Byte Stream Format](https://www.w3.org/TR/mse-byte-stream-format-isobmff/)
32. [DASH-IF Live Media Ingest Protocol](https://dashif.org/Ingest/)
33. [DASH-IF Restricted Timing Model](https://dashif.org/Guidelines-TimingModel/)
34. [Apple: Enabling Low-Latency HLS](https://developer.apple.com/documentation/http-live-streaming/enabling-low-latency-http-live-streaming-hls)
