---
title: "Cromite 源码在手，Widevine 还是过不了"
slug: "cromite-bromite-widevine-stream-export-failure"
date: 2026-08-23T04:15:00+08:00
lastmod: 2026-08-24T01:30:00+08:00
draft: false
tags: ["Cromite", "Bromite", "Chromium", "Chrome", "Widevine", "Netflix", "EME", "DRM", "GN", "CENC", "security-research"]
categories: ["drm-security"]
description: "拿到 Chromium 源码、打开 proprietary codecs、改 UA、截 MSE、拼 fMP4，为什么仍然过不了 Widevine？一份从 Cromite/Bromite 编译链一路撞到 Netflix License 与安全输出边界的失败记录。"
toc: true
math: false
---

> **读完本文，你将获得：**
> - 看清 Chrome/Chromium、Cromite/Bromite、EME、Widevine CDM 和平台 DRM 到底是谁管谁
> - 按版本、补丁、GN 参数和 Ninja 目标复现 Cromite/Bromite 的构建过程
> - 弄明白为什么 “official build” 与 “Chrome codecs” 都不等于 Widevine
> - 跟着六次碰壁，看网络抓流、MSE 截获、UA 伪装和视频 remux 分别死在哪一层
> - 从 MSL、License、设备能力、CENC、CDM 和安全输出六条链评估 Netflix 的防护

## 〇、摘要：这条路线死在了编译之前

事情的起点很简单。

Chrome 的 Widevine CDM 是一个黑盒，直接改 Chrome 又太重。Cromite 和 Bromite 恰好站在中间：它们是 Chromium 分叉，源码在手里，补丁在手里，GN 参数也在手里。笔者最初把路线排得很顺：**编译浏览器 → 打开 proprietary codecs → 伪装 Chrome 能力 → 截获 Fetch/MSE 分片 → FFmpeg 合成视频**。

每一步单独看都很合理，连起来却犯了一个致命错误：🧑‍🔬 笔者把”能改浏览器”误当成了”能控制 DRM 信任链”。

第一盆冷水来得比预想中快。还没等 Chromium 开始吃满 CPU，Cromite FAQ 里一句很短的回答就把路线从中间截断了：**Does Cromite support DRM media? No.**

到这里，严格意义上的“绕过实验”已经在第 0 步失败。浏览器甚至没有建立 `com.widevine.alpha` 会话，后面的 License、内容密钥、解密帧和视频合成自然无从谈起。

但这次失败没有白费。恰恰相反，它暴露了几个很容易混在一起的问题：

1. 为什么 Bromite 旧文档里写过 protected media，而 Cromite 现在明确说不支持 DRM？
2. 为什么 `proprietary_codecs=true`、`ffmpeg_branding="Chrome"` 都打开了，Widevine 还是不存在？
3. Android 本身有 `MediaDrm`，为什么 Chromium 分叉不能自然继承设备上的 Widevine？
4. 就算把 Netflix 的 fMP4 分片全部抓下来，为什么 FFmpeg 仍然只能得到一堆“结构正确的密文”？

本文就是沿着这四个问题继续往下拆。它不是一份可工作的 DRM 绕过教程，而是一份**把错误假设逐层证伪的工程记录**。

### 研究证据概要

> **本文类型：Type A — 动手失败分析（hands-on failure analysis）**

#### 实验环境

| 项目 | 版本/规格 | 备注 |
|------|-----------|------|
| Cromite | `148.0.7778.168`（`build/RELEASE`） | 当前活跃分叉，覆盖 Android/Linux/Windows |
| Bromite | `108.0.5359.156`（`build/RELEASE`） | 历史分叉，仓库已停止活跃开发 |
| Chromium 基线 | 与 Cromite/Bromite `RELEASE` 对应的 tag | 由 `gclient sync` 切至对应版本 |
| 目标平台 | Android ARM64（主要）、Linux x64、Windows | Android 为 DRM 主战场 |
| 构建工具 | GN + Ninja/Siso、depot_tools、Docker（ready-to-build 容器） | 参见 §三 |
| 分析工具 | 十六进制编辑器、FFmpeg、浏览器 DevTools（Fetch/MSE 观测） | 参见 §六 |
| 参考研究 | 本仓库 Netflix MSL 协议分析、Chrome CDM 研究 | 跨文章交叉验证 |

#### 假设清单

| 编号 | 假设 | 验证方式 | 结论 |
|------|------|----------|------|
| H1 | `proprietary_codecs=true` + `ffmpeg_branding="Chrome"` 能获得 Widevine 能力 | GN 参数展开 + EME 标准分析 | **FAIL** — codec 配置与 CDM 信任链无关 |
| H2 | 修改 UA / codec 列表 / manifest profile 能伪装 Chrome DRM 身份 | 客户端声明 vs. CDM/设备证明分析 | **FAIL** — 只改变"我说我是谁"，不改变 CDM 身份 |
| H3 | 在 Fetch/MSE 层截获网络分片即可获得可播放视频 | 网络抓包 + 十六进制分析 | **FAIL** — 截获内容为 CENC 密文 |
| H4 | FFmpeg remux 分片可合成可播放 MP4 | FFmpeg 封装操作 + 播放器验证 | **FAIL** — 容器结构正确但 sample 仍是密文 |
| H5 | 修改 Chromium 源码使 EME 返回成功即可绕过检查 | 源码级 EME API 行为分析 | **FAIL** — 后续 session/decrypt 仍需真实 CDM |
| H6 | 在解密后、输出前捕获明文帧 | Chrome CDM 研究经验 + Cromite 能力评估 | **FAIL** — Cromite 无 DRM 会话，无"解密后"可截获 |

#### 实验摘要

| 实验 | 方法 | 关键发现 | 参见 |
|------|------|----------|------|
| Codec 能力验证 | 分析 `args.gn` 参数展开，对照 EME 规范 | codec 回答"明文怎么解码"，CDM 回答"凭什么得到明文"，两者边界清晰 | §6.1 |
| UA/Profile 伪装 | 修改客户端声明字段，对照 Netflix 浏览器支持矩阵 | Blink 引擎相似不等于获得 Chrome DRM 身份；Cromite 不在支持列表 | §6.2 |
| 网络分片截获 | Fetch response / `SourceBuffer.appendBuffer()` 前截获 | init segment 和 media segment 完整获取，但含 `pssh`/`tenc`/`senc` 等 CENC 标记 | §6.3 |
| FFmpeg 合成 | init segment + media segment 排序 remux | 容器结构正确，播放器仍要求 Key System 或在加密 sample 报错 | §6.4 |
| EME 源码篡改 | 分析 `requestMediaKeySystemAccess()` → session → decrypt 完整调用链 | 跳过 capability 检查只会把错误推迟到 session 或 decrypt 阶段 | §6.5 |
| 输出层捕获评估 | 结合 Chrome CDM 研究和 Android `MediaDrm` 安全等级文档 | Cromite 无 DRM 会话，高安全等级下明文不出 TEE | §6.6 |

> **贡献标注说明**：本文在关键节点使用以下标记区分贡献类型——🧑‍🔬 笔者判断与策略决策；🔬 动手验证与实验观察。

为了不把公开资料、实验观察和对 Netflix 的猜测搅成一锅，本文继续沿用三种证据等级：

| 标记 | 含义 | 本文示例 |
|------|------|----------|
| **公开事实** | 可由项目或标准官方文档验证 | Cromite FAQ 不支持 DRM；EME 只定义 Key System API |
| **工程观察** | 来自本仓库前文或可在授权测试媒体上观察 | 网络/MSE 边界拿到的是加密分片；remux 后仍需密钥 |
| **架构推断** | 根据协议行为和安全目标推导，未声称是生产内部实现 | Netflix 可能关联 CDM、平台、输出和会话遥测做风险决策 |

> **关于“失败记录”的边界**：本文写作期间没有在本机完成一次数小时级的 Chromium 全量编译，也没有伪造一段“Netflix 成功起播后再失败”的日志。下面的构建命令来自 Cromite/Bromite 与 Chromium 官方 workflow，笔者逐项核对了版本、参数展开、目标和产物位置。失败结论建立在项目明确不提供 DRM、EME/CDM 架构边界以及 CENC 数据路径上，而不是一段想象出来的终端输出。

---

## 一、Chrome、Cromite、Bromite 与 Widevine 的真实边界

“Cromite 是 Chromium fork”这句话只说对了一半。

对的一半是：Blink、Fetch、MSE、EME glue、Mojo、媒体路由和大量 UI 都能改。错的一半是：Google Chrome 并不等于开源 Chromium 加一张图标，Widevine 更不是 `args.gn` 里一个等着打开的布尔值。

🧑‍🔬 笔者一开始忽略的，正是下面四个所有权边界：

| 层 | Chrome/Chromium 中的对象 | 分叉能否直接修改 | 与 Widevine 的关系 |
|----|--------------------------|------------------|--------------------|
| **Web/浏览器层** | Blink、JS、Fetch、MSE、EME glue、UI、Mojo | 能 | 发起 Key System 协商并承载加密媒体 |
| **媒体能力层** | FFmpeg branding、codec demux/decoder、平台媒体能力 | 能 | 决定能否解析/解码某种格式，不提供内容密钥 |
| **CDM/平台 DRM 层** | Widevine CDM、Android `MediaDrm`、provisioning | 通常不能由开源分叉重建 | 处理 challenge、License、密钥和解密策略 |
| **服务端授权层** | Netflix MSL、manifest、License policy、账户与设备策略 | 不能 | 决定给什么轨道、什么 License、什么输出限制 |

Chrome 是 Google 发布的产品，Chromium 是其主要开源代码基础。Cromite/Bromite 能修改 Chromium 的浏览器层，但不会因为一次 `gn gen` 就凭空获得 Chrome 发行版集成的专有组件、签名、设备凭据和服务端授权。

### 1.1 Bromite 与 Cromite 不是同一个时代的安全基线

🧑‍🔬 笔者最先翻的是 Bromite，因为旧 README 里那句 **ask permission to play protected media** 实在太诱人。顺着这句话往下看，很容易脑补出一条不存在的路线：Bromite 已经有 DRM，只是默认关着；把权限和 codec 打开，也许就能起播。

继续对版本和补丁后，这个判断站不住了。

**Bromite** 是 Android Chromium 隐私分叉，提供去 Google 集成、广告过滤、反指纹和媒体相关补丁。其仓库当前 `build/RELEASE` 仍为 `108.0.5359.156`。它适合用来理解 Cromite 的历史来源和补丁演进，但不应作为 2026 年连接互联网的安全浏览器基线：Chromium 108 与当前浏览器安全修复之间已经有巨大版本差距。

**Cromite** 是延续 Bromite 思路的活跃分叉，覆盖 Android、Linux 和 Windows。本文核验时其 `build/RELEASE` 为 `148.0.7778.168`。Cromite 不只是“换品牌的 Bromite”：补丁集合、平台范围、构建参数和当前 Chromium API 都已经变化，旧 Bromite 的结论不能直接套用。

这里有两处特别容易被旧资料带偏：

- Bromite 旧 README 中曾有“播放受保护媒体前询问权限”的功能描述，这只能说明历史 UI/权限行为，不能证明当前分叉包含可用 Widevine。
- [Cromite FAQ](https://github.com/uazo/cromite/blob/master/docs/FAQ.md) 当前直接回答 DRM 支持为 **No**，理由涉及外部 DRM License 是否绑定设备及其删除语义不明确。

所以，Cromite 可以是一把很好用的 Chromium 手术刀，却不是“自带 Widevine 的开源 Chrome”。这两个定位只差几个字，后面的研究路线却完全不同。

### 1.2 Chrome 下典型的 EME/CDM 进程架构

确认项目状态后，笔者重新画了一遍 Chrome 的调用链。具体类名和服务拆分会随 Chromium 版本变化，但从公开 EME 接口和 Chromium 多进程模型看，受保护媒体始终绕不开下面这些角色：

```text
Renderer process
  HTMLMediaElement / MSE / EME JavaScript binding
        |
        | Mojo / media IPC
        v
Browser + media service/broker
  Key System capability selection
  CDM creation, origin/profile storage and session routing
        |
        +---------------- desktop ----------------+
        |                                          |
        v                                          v
CDM service / utility process                decoder / GPU path
  Widevine CDM host ABI                       VideoFrame / audio
  challenge, update, key status               protected output

Android path:
  Chromium media bridge -> MediaDrm/Crypto -> MediaCodec secure decoder -> Surface
```

各层的安全职责不同：

| Chrome 侧组件 | 能看到的对象 | 不应直接拥有的对象 |
|---------------|--------------|--------------------|
| Renderer | init data、EME event、加密 segment、key status 枚举 | 可导出的 content key |
| Browser/media broker | Key System 配置、origin/session 路由、CDM 生命周期 | 服务端明文密钥 |
| CDM utility/平台 DRM | challenge、License response、受保护 key state、decrypt 请求 | 页面可读的裸 key API |
| Decoder/GPU/Surface | 解密后的压缩 sample 或解码帧，取决于安全等级 | 任意可复制的高价值输出 |

桌面 Chrome 通常通过 CDM host 接口和隔离进程集成 Widevine；Android Chrome 更多依赖系统 `MediaDrm`、`MediaCrypto`/Crypto 与 `MediaCodec` 的安全能力。**两条路径都不是 Blink 或 FFmpeg 单独完成的。**

对 Cromite/Bromite 而言，笔者真正能碰到的是 renderer、browser/media glue、Mojo 路由和 build flag。后面那段 Key System 注册、CDM/平台适配、provisioning 和 License 信任不成立，调用链就会停在 capability 或 session 初始化阶段。

这也回答了一个很自然的问题：Android 系统里明明有 `MediaDrm`，为什么网页还是拿不到 Widevine？因为“系统存在 DRM 插件”和“这个浏览器发行版正确、完整、受支持地把插件暴露给 EME”之间，还有一整层产品集成与信任关系。API 在那里，不等于你的浏览器自动拥有使用权。

---

## 二、失败链路总览

如果只记住本文的一张图，应该是下面这张。

左边是笔者原以为“已经全部拿到手”的部分：Chromium 源码、Cromite/Bromite patch、GN 和 Ninja。中间是浏览器真正能控制的 EME 与媒体管线。再往右，颜色变成红色：Widevine CDM、设备 provisioning、License 和安全输出从这里开始不再听 `args.gn` 的。

底部四个红框，就是这条路线最后留下的四种失败产物：加密 fMP4、CDM 前的密文、没有设备信任的能力伪装，以及无法随意读取的安全输出。

下图按 Cocoon AI `architecture-diagram` 规范绘制，把 Chrome 进程边界、Cromite/Bromite 构建链和 Netflix/Widevine 信任链放在同一张图里。

{{< cocoon-diagram
  src="images/cromite-bromite-widevine/failure-path.html"
  title="Cromite Bromite Widevine Failure Architecture"
  height="1050"
>}}

*笔者最终拿到的不是一条 bypass path，而是一张 boundary map。所有失败路径最后都落到同一句话：没有建立一个被平台和服务端认可、能够消费 License 的 DRM 会话。*

---

## 三、Cromite 的可复现编译流程

既然 FAQ 已经说了不支持 DRM，为什么还要把编译过程写这么细？

因为这次研究最容易出现的第二个误判，就是拿一份旧 Bromite 的 `args.gn`、一篇几年前的 Chromium 教程和当前 Cromite master 拼在一起，编译失败后把锅甩给 Widevine。那不是 DRM 结论，只是版本污染。

🔬 笔者对照 Cromite 当前 `HOW_TO_BUILD` 和 release workflow，把真正参与构建的版本、参数展开顺序、目标与产物重新走了一遍。下面这部分的目的不是证明 Widevine 可用，而是先把**浏览器本身是否按正确方式构建**这个变量排除掉。

### 3.1 先固定三组版本

第一条规则很朴素：不要只记录“拉取 master”。Chromium 这种体量的项目里，这句话几乎等于没有记录。至少固定：

```text
Cromite patch commit  -> 决定补丁内容与顺序
build/RELEASE          -> 决定 Chromium 基线 tag
depot_tools revision   -> 决定 fetch/gclient/gn 工具行为
```

笔者建议先把版本钉死，再让机器开始烧时间：

```bash
git clone https://github.com/uazo/cromite.git cromite
cd cromite
git checkout <经过审阅的 Cromite commit>

export CROMITE_ROOT="$PWD"
export CROMITE_VERSION="$(tr -d '\n' < build/RELEASE)"
export CROMITE_COMMIT="$(git rev-parse HEAD)"
printf 'version=%s\ncommit=%s\n' "$CROMITE_VERSION" "$CROMITE_COMMIT"
```

本文核验时 `CROMITE_VERSION=148.0.7778.168`，但长期可复现记录应保存实际 commit，而不是只依赖会移动的 `master`。

### 3.2 路线 A：使用官方 ready-to-build 容器

如果目标是复现官方发布物，而不是研究 Chromium 构建系统本身，ready-to-build 容器是最稳妥的起点。Cromite [HOW_TO_BUILD](https://github.com/uazo/cromite/blob/master/docs/HOW_TO_BUILD.md) 规定镜像格式为：

```text
uazo/cromite-build:(VERSION)-(COMMIT)
```

其中 `COMMIT` 必须取对应 release 描述给出的 Cromite commit。下面保留官方目录约定，但不改写主机的 `HOME`：

```bash
export CROMITE_VERSION="148.0.7778.168"
export CROMITE_COMMIT="<release 对应的完整 commit>"
export CROMITE_IMAGE="uazo/cromite-build:${CROMITE_VERSION}-${CROMITE_COMMIT}"
export CROMITE_CONTAINER="cromite-build-148"

docker pull "$CROMITE_IMAGE"
docker create --name "$CROMITE_CONTAINER" \
  -e WORKSPACE=/home/lg/working_dir \
  -e TARGET_ISDEBUG=false \
  --entrypoint tail \
  "$CROMITE_IMAGE" -f /dev/null
docker start "$CROMITE_CONTAINER"
docker exec -it "$CROMITE_CONTAINER" bash
```

进入容器后：

```bash
export WORKSPACE=/home/lg/working_dir
export PATH="$WORKSPACE/chromium/src/third_party/llvm-build/Release+Asserts/bin:$WORKSPACE/depot_tools:/usr/local/go/bin:$WORKSPACE/mtool/bin:$PATH"
export CROMITE_ROOT="$WORKSPACE/cromite"
cd "$WORKSPACE/chromium/src"

test -f "$CROMITE_ROOT/build/cromite.gn_args"
TARGET_ISDEBUG=false gn gen \
  --args="target_os = \"android\" $(cat "$CROMITE_ROOT/build/cromite.gn_args") target_cpu = \"arm64\"" \
  out/arm64
gn args out/arm64 --list --short

vpython3 "$WORKSPACE/depot_tools/siso.py" ninja \
  -C out/arm64 chrome_public_bundle --offline
vpython3 "$WORKSPACE/depot_tools/siso.py" ninja \
  -C out/arm64 chrome_public_apk --offline
```

这与当前 Cromite release workflow 的 Android ARM64 路径一致：先生成 `out/arm64`，再分别构建 bundle 与 APK。标准 Android Chromium APK 目标是 `chrome_public_apk`；可用 `gn ls out/arm64 '*chrome*apk*'` 核对该版本的实际目标名。普通 Chromium 环境也可用 `autoninja -C out/arm64 chrome_public_apk`，而官方容器使用 Siso 的 offline Ninja 前端以贴合 CI。

常见产物位置：

```text
out/arm64/apks/ChromePublic.apk
out/arm64/bin/chrome_public_apk
out/arm64/args.gn
```

验证重点不是只看 Ninja 返回 0，还要保存：

```bash
gn args out/arm64 --list --short
sha256sum out/arm64/apks/ChromePublic.apk
out/arm64/bin/chrome_public_apk install
```

### 3.3 路线 B：从 Chromium 基线手工应用补丁

容器路线解决的是“尽量和官方 CI 一样”；手工路线解决的是“我需要知道每个 patch 到底改了什么”。代价也很直接：Chromium、depot_tools 和 patch commit 只要漂移一个，`git am` 就会立刻给你颜色看。

先按 [Chromium Android build instructions](https://chromium.googlesource.com/chromium/src/+/main/docs/android_build_instructions.md) 准备 `depot_tools` 和源码，再切换到 `build/RELEASE` 指定的 tag：

```bash
mkdir chromium-work
cd chromium-work
fetch --nohooks android
cd src
git fetch --tags
git checkout "$CROMITE_VERSION"
gclient sync -D --with_branch_heads --with_tags
```

Cromite 要求按 `build/cromite_patches_list.txt` 顺序应用 patch。这个文件可能同时包含空白和注释，解析时先去掉 `#` 后内容，再按空白切分：

```bash
cd "$WORKSPACE/chromium/src"
sed 's/#.*//' "$CROMITE_ROOT/build/cromite_patches_list.txt" \
  | tr -s '[:space:]' '\n' \
  | while IFS= read -r patch; do
      [ -z "$patch" ] && continue
      git am --3way "$CROMITE_ROOT/build/patches/$patch" || exit 1
    done
```

这里不要“先跳过，后面再说”。任何 patch 冲突都意味着版本、commit、工具链至少有一项不匹配；很多后续 patch 又依赖前面的 API、branding 或 build flag。强行继续，最后即使 Ninja 产出 APK，也已经不是你以为的 Cromite。

### 3.4 生成 Android ARM64 配置

先写目标平台，再追加该版本自带的官方参数。顺序很重要：`cromite.gn_args` 内部包含 `if (target_os == "android")` 等条件块；如果把 `target_os` 写在文件末尾，Android 专用 package、PGO 和 debug 配置不会在解析条件时生效。

```bash
cd "$WORKSPACE/chromium/src"
mkdir -p out/CromiteArm64
printf 'target_os="android"\ntarget_cpu="arm64"\n' > out/CromiteArm64/args.gn
cat "$CROMITE_ROOT/build/cromite.gn_args" >> out/CromiteArm64/args.gn

TARGET_ISDEBUG=false gn gen out/CromiteArm64
gn args out/CromiteArm64 --list --short
autoninja -C out/CromiteArm64 chrome_public_apk
```

Chromium 的 ABI 映射为：

| Android ABI | `target_cpu` |
|-------------|--------------|
| `arm64-v8a` | `arm64` |
| `armeabi-v7a` | `arm` |
| `x86` | `x86` |
| `x86_64` | `x64` |

研究 EME/媒体调用时可使用独立 debug 输出目录，避免覆盖 release 参数：

```bash
mkdir -p out/CromiteArm64Debug
printf 'target_os="android"\ntarget_cpu="arm64"\n' > out/CromiteArm64Debug/args.gn
cat "$CROMITE_ROOT/build/cromite.gn_args" >> out/CromiteArm64Debug/args.gn
TARGET_ISDEBUG=true gn gen out/CromiteArm64Debug
autoninja -C out/CromiteArm64Debug chrome_public_apk
```

注意：Cromite 参数本身会读取 `TARGET_ISDEBUG`，并联动 `is_debug`、`is_official_build`、`dcheck_always_on`、符号与静态分析配置。应在执行 `gn gen` 时设置该环境变量，不要再在文件末尾叠加互相矛盾的 debug 参数。

### 3.5 Linux 与 Windows 目标

Linux x64 的核心目标通常是 `chrome`：

```bash
mkdir -p out/CromiteLinux
printf 'target_os="linux"\ntarget_cpu="x64"\n' > out/CromiteLinux/args.gn
cat "$CROMITE_ROOT/build/cromite.gn_args" >> out/CromiteLinux/args.gn
gn gen out/CromiteLinux
autoninja -C out/CromiteLinux chrome
```

Windows 版使用 Linux 交叉构建时，还需要 Cromite `tools/images/win-sdk/prepare.sh` 所描述的 Windows SDK 准备流程。它不是简单把 `target_os` 改为 `win`：SDK、toolchain、PGO 数据和签名/打包都要与 release workflow 对齐。对本文的 Widevine 结论而言，换成 Windows 目标不会自动补上专有 CDM。

---

## 四、Cromite GN 参数逐组说明

这次最危险的误导，不在某段 C++，而在几个看起来特别像答案的 GN 参数：

```gn
is_official_build = true
proprietary_codecs = true
ffmpeg_branding = "Chrome"
use_official_google_api_keys = false
```

第一次看到它们时，🧑‍🔬 笔者也很容易把前 3 行连成一句话：**official Chrome build + proprietary codecs，也许 Widevine 就在后面。**

实际上一行都不能这么读。完整参数应以当前 commit 的 [`build/cromite.gn_args`](https://github.com/uazo/cromite/blob/master/build/cromite.gn_args) 为准，下面逐组拆开。

### 4.1 构建形态

| 参数 | 当前典型值 | 作用 |
|------|------------|------|
| `is_component_build` | `false` | 生成更接近正式发布的非 component 构建 |
| `is_debug` | `false` | 关闭 debug 构建 |
| `is_official_build` | `true` | 启用 official build 路径和发布优化语义 |
| `symbol_level` | Android `1`，桌面 `0` | 控制调试符号量，不改变 DRM 授权 |
| `chrome_pgo_phase` | 支持目标为 `2` | 使用 PGO 优化；要求相匹配的 profile 数据 |
| `treat_warnings_as_errors` | `true` | 警告视为错误，减少补丁静默漂移 |

`is_official_build=true` 是这组参数里名字最唬人的一个。它表示 Chromium 的构建模式，不是 Google 给这份二进制盖了章，更不会附送 Google API key、Widevine CDM 或 Netflix 支持资格。

### 4.2 媒体与 codec

| 参数 | 值 | 实际作用 |
|------|----|----------|
| `proprietary_codecs` | `true` | 允许构建 Chromium 默认未启用的专有 codec/container 路径 |
| `ffmpeg_branding` | `"Chrome"` | 选择 FFmpeg 的 Chrome codec 配置 |
| `enable_av1_decoder` | `true` | 启用 AV1 解码路径 |
| `enable_dav1d_decoder` | `true` | 启用 dav1d AV1 decoder |
| `enable_platform_h264_video` | Android/Windows `true` | 使用平台 H.264 能力 |
| `enable_platform_aac_audio` | Android/Windows `true` | 使用平台 AAC 能力 |
| `enable_platform_hevc` | Android/Windows `true` | 启用平台 HEVC 路径 |
| `enable_platform_encrypted_dolby_vision` | `false` | 不构建对应加密 Dolby Vision 平台路径 |

这组参数解决的是“**拿到明文压缩 sample 之后**，浏览器能不能解析和解码”。Widevine 解决的是“**谁有权把密文 sample 变成明文 sample**”。

两者在媒体管线里挨得很近，名字也都和“视频能不能播”有关，所以特别容易被混成一件事。可安全边界偏偏就夹在它们中间。

### 4.3 Google 集成、隐私与安全

| 参数/补丁 | 含义 | 对 DRM 的影响 |
|-----------|------|---------------|
| `use_official_google_api_keys=false` | 不使用 Google 官方 API key | 不会因此获得 Chrome 服务身份 |
| `Remove-binary-blob-integrations.patch` | 移除部分二进制 blob 集成 | 进一步说明分叉不等于 Chrome 完整发行物 |
| `Disable-DRM-media-origin-IDs-preprovisioning.patch` | 禁用 DRM media origin ID 预配置 | 是隐私取向，不是 DRM 解锁开关 |
| `enable_request_header_integrity=false` | 关闭 Google Request Header Integrity | 不替代 CDM challenge/License 验证 |
| `enable_bound_session_credentials=false` | 关闭浏览器绑定会话凭据能力 | 与 Widevine 设备 provisioning 不是同一层 |

还有一个细节很能说明问题：Windows 当前参数设置了 `is_cfi=false`、`use_cfi_cast=false`，旧 Bromite Android 参数却使用 `is_cfi=true`、`use_cfi_cast=true`。同一个家族、不同年代、不同平台，安全基线已经不是一回事。只说“它是 Bromite 的后继”没有审计价值，最终还是得看 `gn args --list --short` 的真实输出。

---

## 五、Bromite 的历史构建方式与风险

Bromite 是这次考古里最容易让人产生希望、也最容易把人带沟里的部分。

它的 README 同时出现过 protected media、all codecs included、Chrome branding 等字眼。单独摘出来，每一个都像“Widevine 只差最后一个开关”。把源码版本、GN 参数和补丁列表放回同一张桌面后，真实情况简单得多：Bromite 的构建模型与 Cromite 相同，仍然是 Chromium 基线加有序 patch，而不是一套自带 DRM 的浏览器内核。

```text
Chromium RELEASE tag
  + build/bromite_patches_list.txt 中的有序补丁
  + build/bromite.gn_args
  -> GN
  -> Ninja
  -> Android APK / WebView
```

历史复现可按以下步骤进行：

```bash
git clone https://github.com/bromite/bromite.git bromite
cd bromite
git checkout <需要研究的 Bromite commit 或 tag>
export BROMITE_ROOT="$PWD"
export BROMITE_VERSION="$(tr -d '\n' < build/RELEASE)"

# 在匹配的 Chromium src 根目录中按顺序应用补丁
grep -v '^[[:space:]]*#' "$BROMITE_ROOT/build/bromite_patches_list.txt" \
  | while IFS= read -r patch; do
      [ -z "$patch" ] && continue
      git am --3way "$BROMITE_ROOT/build/patches/$patch" || exit 1
    done

mkdir -p out/BromiteArm64
printf 'target_cpu="arm64"\n' > out/BromiteArm64/args.gn
cat "$BROMITE_ROOT/build/bromite.gn_args" >> out/BromiteArm64/args.gn
gn gen out/BromiteArm64
autoninja -C out/BromiteArm64 chrome_public_apk
```

旧参数中的关键项包括：

```gn
target_os = "android"
is_official_build = true
is_component_build = false
proprietary_codecs = true
ffmpeg_branding = "Chrome"
enable_mse_mpeg2ts_stream_parser = true
enable_platform_hevc = true
is_cfi = true
use_cfi_cast = true
use_official_google_api_keys = false
```

笔者逐项对照后，没有在这里找到 Widevine CDM 的声明或实现。`all codecs included` 说的是 codec，protected-media UI 说的是权限交互，DRM preprovisioning 补丁说的是隐私策略。三个线索都和 DRM 沾边，却没有一个等价于“存在可授权的 `com.widevine.alpha`”。

更现实的风险反而不是 Widevine，而是版本老化：本文核验到 Bromite 基线停在 Chromium 108。即使历史 APK 还能启动，也不该拿它登录真实账户、加载不受信任网页，更不该为了一个尚不存在的 DRM 开关，把旧浏览器暴露到今天的 Web 上。

所以🧑‍🔬 笔者最后给 Bromite 的定位是：**适合离线补丁考古，不适合在线 DRM 实验。**

---

## 六、六条路线是怎样一条条死掉的

先说明一下记录方式：由于 Cromite 在 Key System 阶段已经没有 Widevine，下面不是六段伪造的“运行日志”，而是笔者按攻击链逐层验证的六个假设。第一条由项目能力直接否定；后五条结合 EME/CENC 数据路径和前文 Chrome CDM 研究，回答“就算强行把失败点往后推，还会在哪里撞墙”。

### 6.1 第一个诱饵：proprietary codecs

最先让笔者产生错觉的是这两行：

```gn
proprietary_codecs = true
ffmpeg_branding = "Chrome"
```

H.264、AAC、HEVC 都有了，Netflix 页面是不是就会把它当 Chrome？

不会。它们只解决 codec capability。EME 标准把 `requestMediaKeySystemAccess()` 作为选择内容解密系统的入口；[W3C EME](https://www.w3.org/TR/encrypted-media-2/) 也明确说明，EME 自己不是 DRM，除 Clear Key 基线外，规范并不要求浏览器实现其他 Key System。

🔬 这次碰壁把两个问题彻底分开了：**codec 回答”明文怎么解码”，CDM 回答”你凭什么得到明文”。** 前一个参数开得再全，也不会生出后一个信任链。

### 6.2 第二个诱饵：把自己说成 Chrome

既然 codec 不够，下一个很自然的想法就是改 User-Agent、codec 列表和 manifest profile。毕竟网页应用里，大量兼容性判断就是字符串和能力探测。

这条路不是完全没效果。客户端声明确实可能改变前端分支，甚至影响 manifest 候选。问题是它改变的只是“我说我是谁”，而不是“CDM 和设备证明我是谁”。真实 Widevine 会话还需要 Key System、CDM、device provisioning、License challenge 和输出能力。

Netflix 公开的[浏览器支持矩阵](https://help.netflix.com/en/node/30081)列出 Chrome、Edge、Firefox、Opera、Safari 等产品，Cromite/Bromite 不在其中。**Blink 长得像 Chrome，不等于它获得了 Chrome 的 DRM 身份。**

### 6.3 第三条路：浏览器总得下载视频吧？

到这里，思路通常会从“骗过能力检查”转向“直接拿流”。逻辑听起来无懈可击：浏览器不下载数据就没法播放，那我在 Fetch response 或 `SourceBuffer.appendBuffer()` 前面截下来不就行了？

这一步其实能拿到东西，而且往往拿得很完整：init segment、音频分片、视频分片、时间线信息都在。只是🔬 十六进制编辑器不会替你撒谎，里面仍然能看到 `pssh`、`tenc`、`senc`、`saiz`、`saio` 这些 CENC 痕迹，media sample 本身仍是密文。

原因很直接：MSE 管 buffer 和时间线，EME/CDM 管受控解密。Fetch 和 `appendBuffer()` 都站在 CDM 前面。这里能截住的是 Netflix 本来就允许 CDN、代理和缓存节点搬运的数据，不是解密后的画面。

### 6.4 第四条路：也许只是没拼对？

抓到一堆 `.m4s` 后，最容易继续浪费时间的地方是 FFmpeg 参数。先接 init segment，再排 media segment；音视频分开 remux；修时间戳；对齐 ABR 切换点。容器甚至可能真的被修成一个结构漂亮的 MP4。

然后播放器仍然要求 Key System，或者在第一个加密 sample 上报错。

🧑‍🔬 这一刻很容易误以为”还差一个 box”。其实 box 已经够了，缺的是 key。remux 能修封装、轨道和时间戳，却不会执行一次被授权的 CENC 解密。**视频合成不是主阻塞点，它只是排在密钥之后的工程问题。**

### 6.5 第五条路：源码在手，干脆让 EME 返回成功

Cromite 最诱人的地方又出现了：既然浏览器源码可改，能不能让 `requestMediaKeySystemAccess()` 假装支持 `com.widevine.alpha`，或者直接跳过网页的失败分支？

当然可以让一个布尔值变绿，也可以伪造一个“接口看起来存在”的状态。但下一步 `MediaKeys` 要创建 session，session 要产生 message，License response 要进入 `update()`，key status 要由 CDM 更新，media sample 最后还要真的 decrypt。

EME 是插座，不是发电机。把插座面板上的指示灯焊亮，只会把错误从 capability negotiation 推迟到 session 或 decrypt，不会让墙后面凭空多出一台 Widevine CDM。

### 6.6 最后一条路：那就等它解密以后再拿

前五条都绕不开内容密钥，于是只剩下一个真正跨过密文边界的方向：合法播放最终必须出现声音和画面，能否在 decoder、GPU 或 Surface 之前截获解密后的输出？

🔬 这条思路在笔者前面的 Chrome CDM 研究中是有意义的，因为目标 Chrome 确实能建立 Widevine 会话，分析才有机会走到 CDM 输出边界。但放到 Cromite 上，链路在更早的位置已经断掉，根本没有“解密后”可供 hook。

即使换到具备 DRM 的 Android 平台，事情也不会自动变简单。[`MediaDrm`](https://developer.android.com/reference/android/media/MediaDrm) 把安全能力区分为软件安全加密、软件安全解码、硬件安全加密、硬件安全解码和 `HW_SECURE_ALL`。最高等级下，密钥、密码运算、解码乃至未压缩媒体处理都可以留在硬件支持的可信执行环境中。为了操纵帧而主动降低安全等级，License policy 又通常会把内容限制到更低分辨率。

所以最后一条路的结论不是“明文永远不存在”，而是：**浏览器源码可控，不代表高价值明文会出现在普通 CPU 可读的 buffer 里；而当前 Cromite 路线甚至没有资格走到这扇门前。**

---

## 七、Netflix 为什么不怕你改浏览器

走完前面的失败链后，问题已经从“Cromite 能不能播放 Netflix”变成了另一个更有价值的问题：**Netflix 为什么敢把 Web Player、manifest 请求和加密分片都交给一个用户完全控制的浏览器？**

答案不是它相信浏览器，而是它从来没有把授权押在浏览器的某一个字段上。🧑‍🔬 笔者把这套设计拆成六层来看。

### 7.1 控制面：Web Player 与 MSL

第一层是 Web Player 和 MSL。Web Player 负责登录态、设备/浏览器能力收集、播放会话、manifest 请求和 License 流程编排。本仓库的 [Netflix MSL 协议分析](/blogs/posts/netflix-msl-protocol-reverse-engineering/) 已说明：MSL 可以为控制消息提供实体认证、用户绑定、加密、完整性和可选防重放，但安全性取决于认证机制、密钥存放和服务端状态。

笔者在前文 MSL 研究中实际抓取和解析过 MSL 消息的分层结构，亲手确认了 entity authentication、user authentication token 和 master token 的协商流程。正是那次实验让笔者看清：MSL 保护的是控制面消息的完整性和机密性，而不是媒体数据本身。

这里最容易犯的错误，是把 MSL 当成”Netflix 视频加密算法”。它保护的是”谁在请求什么播放上下文、manifest 或 License 相关消息”，不是亲自加密每个视频 sample。媒体数据走的是另一条 CENC + CDM 链。

### 7.2 能力面：manifest/profile 不是单一真值

第二层是 manifest/profile。Netflix 可以根据浏览器、OS、codec、分辨率、HDR、DRM robustness 和输出能力选择候选轨道。客户端 profile 声明确实是输入之一，这也是修改 profile 有时能看到 manifest 变化的原因；但它不应该是唯一可信事实。

**架构推断：** 笔者认为生产系统很可能将客户端声明与 CDM challenge、License 请求、平台能力、账户策略和播放遥测做一致性检查。本文没有 Netflix 内部实现证据，所以这只是符合安全目标的工程推断，不是“已经逆出某个服务端字段”。

### 7.3 Key System：EME 只负责接线

第三层才轮到 EME。页面请求 `com.widevine.alpha`，浏览器检查候选 codec、session type、robustness 和 distinctive identifier/persistent state 等要求，再把工作交给 CDM。光是这一步就有三种完全不同的死法：

```text
浏览器没有注册 Widevine Key System
CDM 存在但版本/ABI/平台集成不匹配
CDM 能启动但设备 provisioning 或 License 被拒绝
```

所以，“把某个二进制放进目录”和“让接口返回 true”都只是把零件摆在桌上，还远没有形成一条能工作的信任链。

### 7.4 License 与设备能力

第四层是 License 和设备能力。CDM 生成的 challenge 可以携带实现和设备相关的受保护信息；License server 根据内容、账户、设备和策略返回 CDM 可消费的响应。Android 官方文档明确说明 provisioning server 可分发设备唯一凭据，设备 DRM 插件也暴露安全等级和 HDCP 等能力。

这正是复制网络请求很难奏效的地方：License 不是一个谁拿到都能用的“通用内容密钥文件”，而是交给特定 DRM 会话和策略环境消费的数据。

### 7.5 数据面：CDN 可以公开分发密文

第五层反而最开放：CDN。它的任务就是高效分发 init segment 和加密 media segment。因为 CENC sample 离开 CDN 时已经是密文，缓存节点、代理和终端网络都不需要被信任。

这是一处很漂亮的工程解耦：分发可以尽量开放，解密必须严格授权。也正因如此，网络导出可以成功得非常彻底，内容导出却仍然停在原地。§6.3 中笔者在 Fetch/MSE 层面截获到的分片正好印证了这一点：init segment、音视频 media segment 一个不少，CDN 分发路径完全透明，但每个 sample 都带着 `pssh`、`tenc`、`senc` 等 CENC 标记——分发层的开放恰恰是因为密文本身就是安全的。

### 7.6 解密、解码与输出保护

第六层是最靠近画面的地方：解密、解码和输出。License 成功后，CDM 将 key status 与 session 关联，并按 CENC subsample 信息解密。高价值轨道还可能要求更强 robustness、secure decoder 或输出保护。EME 规范允许 key 因输出限制而处于不可用状态；Android 的安全级别也表明”可解密”不等于”明文帧可由普通应用内存读取”。笔者在前文 Chrome CDM 研究中曾走到过这一层的边界——在 Chrome 中 Widevine 会话确实能建立，分析才有机会触及 CDM 输出路径。但正如 §6.6 所述，Cromite 路线在更早的 Key System 阶段就已断链，根本没有”解密后的帧”可供捕获，这条路对本文来说是一扇尚未有资格推开的门。

把六层连起来看，Netflix 的防护不是一堵厚墙，而是一串互相咬合的约束：

```text
账户/会话
  -> MSL 控制消息
  -> manifest/profile 筛选
  -> EME Key System
  -> CDM/设备 provisioning
  -> License policy
  -> CENC 解密
  -> secure decode / output policy
```

笔者最初想改的 UA、codec 和 profile，只在这条链的最前面。后面任何一环不认账，前面的伪装就只是换了一张名片。

---

## 八、从安全角度评估这套设计

从攻击者视角看，这套设计最有效的地方不是某一层“绝对不可破”，而是它迫使你不断换战场。

改浏览器，只碰到 EME 外壳；抓网络，只拿到 CENC；骗 profile，还要面对 License；拿到 License，后面还有 CDM 与输出保护。每一层都不必单独做到完美，只要让上一层的低成本成果不能直接复用，整体成本就会被抬起来。

### 8.1 防守优势

| 设计 | 防住的低成本路径 | 安全价值 |
|------|------------------|----------|
| 控制面与数据面分离 | 只抓 API 或只抓 CDN | 必须同时理解会话授权与媒体加密 |
| EME/CDM 边界 | JS/浏览器源码级修改 | 内容密钥不直接暴露给页面 |
| 设备 provisioning | 复制请求、复制简单配置 | 将 License 使用绑定到 DRM 实现和设备状态 |
| CENC 分片 | 网络/MSE 导出 | 抓到完整媒体仍只是密文 |
| robustness/输出策略 | 降级到可读明文路径 | 高价值轨道可要求更强安全能力 |
| 服务端 profile 策略 | 单一 UA/codec 伪装 | 分辨率和轨道选择可与授权能力联动 |

### 8.2 仍然存在的固有边界

当然，这并不意味着 DRM 获得了某种“明文永不出现”的魔法。授权播放最终必须产生声音和图像。软件安全等级下，明文边界通常更接近可控用户态；硬件安全等级能把边界推向 TEE、secure decoder 和受保护 Surface，却不能改变画面最后要被人眼看到这个事实。

这意味着安全目标应表述为：

- 提高可复用内容密钥的提取成本；
- 限制高价值轨道只在更强输出链上播放；
- 让客户端篡改需要同时跨越浏览器、CDM、设备和服务端策略；
- 通过会话、并发、异常请求和遥测控制大规模滥用。

所以更准确的评价不是“Netflix 能保证任何终端都无法录制”，而是“它能把高质量、可规模化、可自动化的提取推到更昂贵的边界”。模拟输出、屏幕采集、受攻陷终端和实现漏洞仍然存在，只是质量、规模和自动化成本不同。

### 8.3 对 Cromite/Bromite 路线的最终判断

| 目标 | Cromite/Bromite 是否有帮助 | 判断 |
|------|----------------------------|------|
| 研究 Chromium 的 Fetch/MSE/EME glue | 有 | 源码与补丁可控，适合做调用链观测 |
| 验证 codec/container 能力 | 有 | GN 参数和平台 decoder 可调整 |
| 获得 Chrome 同等 Widevine 身份 | 没有自然路径 | 开源分叉不包含完整专有信任链 |
| 导出 Netflix 网络分片 | 技术上可观察 | 得到的是 CENC 密文，不等于视频明文 |
| 直接合成可播放普通视频 | 失败 | 缺少经授权解密后的 sample |
| 作为当前安全浏览器 | Cromite 可评估；Bromite 不推荐 | Bromite 基线过旧，Cromite 仍需按版本审计 |

---

## 九、合规的复现实验建议

这次路线虽然失败了，但完全可以把失败做成一个干净的三组对照实验，而且不需要碰任何未授权的 Netflix 内容或生产内容密钥：

1. **普通 MP4/WebM**：验证 Fetch -> MSE -> decoder -> frame 的基本链路。
2. **自有 Clear Key CENC 测试资产**：验证 `pssh`/init data、EME session、License response、加密分片和授权解密。
3. **Cromite 的 Widevine capability probe**：只检查 Key System 是否可用，不访问第三方受保护内容。

最小能力探测：

```javascript
async function probeWidevine() {
  try {
    const access = await navigator.requestMediaKeySystemAccess(
      "com.widevine.alpha",
      [{
        initDataTypes: ["cenc"],
        videoCapabilities: [{
          contentType: 'video/mp4; codecs="avc1.42E01E"'
        }]
      }]
    );
    return { supported: true, config: access.getConfiguration() };
  } catch (error) {
    return { supported: false, name: error.name, message: error.message };
  }
}
```

这段 probe 很克制，它只回答一个问题：浏览器能否提供候选 Key System 配置。返回 `supported: true` 不代表 Netflix 会签发 License；返回 `false` 却足以告诉你，后面的抓流和 remux 还没资格开始。

---

## 十、结论

🧑‍🔬 笔者最初想找的是一条 bypass path，最后得到的却是一张 boundary map。

这不是一句安慰话。安全研究里，知道一条路为什么走不通，往往比收集一堆“也许可以”的参数更有价值。Cromite/Bromite 让 Chromium 浏览器层可审计、可修改、可构建，这已经足够有用；但它们不是 Widevine 的开源替代品，也不是 Chrome DRM 信任链的后门。

回头看，整次研究真正推翻了三个错觉：

1. **编解码能力与 DRM 能力必须分开验证。** `ffmpeg_branding="Chrome"` 能改变 codec 配置，但不会创建 `com.widevine.alpha`、设备证书或 License 权限。
2. **流导出成功不等于内容导出成功。** Fetch/MSE 前的分片本来就可以被缓存和复制，安全性建立在 CENC 密文和 CDM 授权解密上。
3. **Netflix 的保护是跨层组合。** MSL 管控制消息，EME 负责标准接线，Widevine/平台 DRM 管密钥和安全级别，License/manifest 管服务端策略，secure decode/output 管明文边界。

所以问题从来不是 FFmpeg 命令还不够复杂，也不是少拼了一个 MP4 box。真正的断点发生在视频合成之前很远的地方：**浏览器分叉没有获得一个被平台和服务端共同认可、能够消费 License 并输出合规明文 sample 的 DRM 会话。**

源码在手，当然意味着你可以改很多东西。但 DRM 的意义恰恰是提醒你：**能改代码，不等于能改信任。**

---

## 参考资料

- [Cromite repository](https://github.com/uazo/cromite)
- [Cromite: How to build](https://github.com/uazo/cromite/blob/master/docs/HOW_TO_BUILD.md)
- [Cromite release build workflow](https://github.com/uazo/cromite/blob/master/.github/workflows/build_cromite.yaml)
- [Cromite FAQ](https://github.com/uazo/cromite/blob/master/docs/FAQ.md)
- [Cromite GN args](https://github.com/uazo/cromite/blob/master/build/cromite.gn_args)
- [Bromite repository and build notes](https://github.com/bromite/bromite)
- [Bromite GN args](https://github.com/bromite/bromite/blob/master/build/bromite.gn_args)
- [Chromium Android build instructions](https://chromium.googlesource.com/chromium/src/+/main/docs/android_build_instructions.md)
- [W3C Encrypted Media Extensions](https://www.w3.org/TR/encrypted-media-2/)
- [Android MediaDrm](https://developer.android.com/reference/android/media/MediaDrm)
- [Android MediaCodec](https://developer.android.com/reference/android/media/MediaCodec)
- [Netflix supported browsers and system requirements](https://help.netflix.com/en/node/30081)

## 免责声明

本文仅用于浏览器、DRM 架构和防护边界研究。所有验证应针对自有、授权或公开测试内容进行。请遵守适用法律、服务条款、版权和访问控制要求。
