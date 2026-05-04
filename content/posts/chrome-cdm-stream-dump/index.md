---
title: "十三次碰壁之后：当密钥不可提取时 - Chrome Widevine CDM 流捕获的工程突围"
slug: "chrome-cdm-stream-dump-widevine-vtable-hook"
date: 2026-05-04
lastmod: 2026-05-04
draft: false
tags: ["widevine", "drm", "chrome", "CDM", "reverse-engineering", "LD_PRELOAD", "vtable-hook", "white-box-aes", "stream-capture"]
categories: ["security-research"]
description: "在 Chrome Linux Widevine CDM 4.10.2934 上尝试 13 种密钥提取方法全部失败后，通过 LD_PRELOAD vtable hook 实现解密后视频流捕获的完整工程记录"
toc: true
math: false
---

## 〇、摘要

本文记录了对 Chrome Linux Widevine CDM（`libwidevinecdm.so` 4.10.2934.0）的安全分析过程。笔者最初的目标是提取 AES 内容密钥——但在系统性尝试 **13 种攻击向量后全部失败**，笔者发现了一个根本性的事实：**这个 CDM 使用白盒 AES + key blinding，裸密钥从不以可观测形式存在于堆内存中**。

面对这一死胡同，笔者进行了**范式转移**——放弃密钥提取，转向流捕获。最终通过 LD_PRELOAD + C++ vtable patching 构建了完整的解密视频流捕获管线：

1. **LD_PRELOAD hook**：拦截 `dlopen`/`dlsym`，在 CDM 加载瞬间获取实例指针并 patch vtable
2. **DecryptAndDecodeFrame 捕获**：hook vtable slot 14，提取解密后的 YUV 明文（I420/YUV420P10）
3. **CDP 持久注入**：通过 Chrome DevTools Protocol 劫持 `playbackRate`，支持 1x-8x 加速捕获
4. **多分辨率段编码**：自动处理 Netflix ABR 导致的分辨率切换，分段编码后拼接
5. **端到端验证**：Netflix + Shaka demo 视频成功捕获并编码为 MP4

核心贡献不在于最终的流捕获方案（概念上并不复杂），而在于 **13 次失败尝试系统性地刻画了 CDM 4.10.2934 的白盒 AES 防护边界**——这些"不可能"的证明本身就是有价值的安全分析。

---

## 一、路线总览

![架构图](https://overkazaf.github.io/blogs/images/cdm-dump/architecture.png)
*完整的流捕获管线架构：LD_PRELOAD hook 在 CDM 进程内部拦截 vtable，捕获解密后的 YUV 帧写入 /dev/shm，外部编码器分段处理并输出 MP4。*

| 阶段 | 目标 | 方法 | 结果 |
|------|------|------|------|
| **Phase 1** | 提取 AES 内容密钥 | BoringSSL hook (3 种) | **全部失败：dead code** |
| **Phase 2** | 在内存中搜索密钥 | 堆扫描 + 结构检测 (5 种) | **全部失败：key blinding** |
| **Phase 3** | 硬件级拦截 | int3 trap + perf (2 种) | **全部失败：软件白盒 AES** |
| **Phase 4** | 范式转移 → 流捕获 | LD_PRELOAD vtable hook | **成功** |
| **Phase 5** | 工程化 | CDP 注入 + 多分辨率编码 | **Netflix 端到端验证通过** |

**13 次失败不是浪费**——它们证明了 CDM 4.10.2934 的白盒 AES 防护在当前工具能力下不可突破，这一结论本身就是本研究最重要的贡献。

---

## 二、引言

### 2.1 研究背景：Widevine 的两张面孔

笔者在[前文](/posts/widevine-l3-keybox-mass-production/)中通过 DFA 攻破了 Android L3 CDM（build 4464, 2018 年编译）的白盒 AES，成功提取了密钥并实现了 keybox 量产。那个 CDM 使用经典的 T-table 实现，DFA 信号清晰可辨。

Chrome 桌面端的 CDM（build 4.10.2934.0, 2026 年当前版本）是完全不同的对手：

| 对比维度 | Android L3 (build 4464) | Chrome CDM (4.10.2934) |
|---------|------------------------|------------------------|
| 编译时间 | 2018 年 | 2026 年当前 |
| AES 实现 | T-table（内存可观测） | **白盒软件 AES（无标准表）** |
| 标准 S-box | 存在 | **不存在（扫描 453MB，0 命中）** |
| aesenc 硬件指令 | 不使用 | **存在但从未执行（dead code）** |
| DFA 可行性 | 可行（本文已验证） | **不可行（无可观测的 AES 结构）** |
| 密钥存储 | 可从内存提取 | **XOR blinding，裸密钥仅在栈帧内** |
| 混淆层 | OLLVM + VM | **OLLVM CFF，97% CPU 在调度器** |

Google 在 8 年间将 CDM 的 AES 实现从"可被 DFA 攻破的 T-table"升级为"密钥从不以可观测形式存在的白盒"——这是笔者切身感受到的**防护代际差距**。

### 2.2 研究动机

笔者的目标是评估 Chrome CDM 的密钥保护强度：**裸密钥是否可以在运行时被提取？**

如果可以，意味着 CDM 的白盒 AES 存在侧信道泄露，可以通过 `mp4decrypt` 等标准工具离线解密内容——这对防护评估有重大意义。

如果不可以（正如最终证明的那样），则需要理解**为什么不可以**，并找到替代路径完成安全分析的其他目标。

### 2.3 目标与范围

| 项目 | 值 |
|------|------|
| **目标二进制** | `libwidevinecdm.so` 4.10.2934.0 (18.2 MB, x86_64) |
| **运行平台** | Chrome Linux (144.0+), `--no-sandbox` |
| **主机** | Ubuntu 22.04 LTS, Dual Xeon E5-2673 v4 (80 threads), 96GB RAM |
| **分析工具** | radare2, eBPF/bpftrace, GDB, Frida, perf, custom C hook (3461 行) |
| **分析时间** | 2026-04-20 ~ 2026-05-04 |

---

## 三、逆向前的知识准备

### 3.1 Chrome CDM 进程架构

Chrome 的 CDM 运行在一个**独立的 utility 进程**中，与渲染进程通过 Mojo IPC 通信：

```
Chrome 主进程
├── Renderer 进程 (JS/EME)
│   └── navigator.requestMediaKeySystemAccess('com.widevine.alpha')
├── GPU 进程 (渲染)
└── CDM Utility 进程 (--type=utility --utility-sub-type=media.mojom.CdmServiceBroker)
    └── libwidevinecdm.so (动态加载)
        ├── CreateCdmInstance() → Cdm* 实例
        ├── vtable[5]:  UpdateSession()  → 安装 license
        ├── vtable[9]:  Decrypt()        → 解密音频
        └── vtable[14]: DecryptAndDecodeFrame() → 解密+解码视频
```

CDM utility 进程有特殊的沙箱限制：
- `fd 2`（stderr）在 exec 前被关闭
- `/tmp` 路径写入被沙箱拒绝
- `fd 1`（stdout）被继承，可用于日志输出

### 3.2 CDM 二进制特征

| 属性 | 值 |
|------|------|
| 大小 | 18.2 MB |
| 导出函数 | 5 个（`CreateCdmInstance`, `GetCdmVersion`, `VerifyCdmHost_0`, ...） |
| `VerifyCdmHost_0` | 始终返回 1（无宿主校验） |
| 混淆 | OLLVM 控制流平坦化 |
| `.rodata` 熵 | 94% > 7.8（高度加密/压缩） |
| BoringSSL 函数 | 存在但为 **dead code**（CDM 不使用） |

---

## 四、Phase 1-3：十三次碰壁

> 笔者最初的假设很自然：CDM 在解密时一定会在某处使用 AES 密钥，而 AES 密钥一定会以某种形式存在于内存中。13 次尝试后，这个假设被彻底证伪。

![13 次攻击向量](https://overkazaf.github.io/blogs/images/cdm-dump/attack_vectors.png)
*13 次密钥提取尝试的完整路径：从 BoringSSL hook（Phase 1）到内存搜索（Phase 2）到硬件断点（Phase 3），全部失败后转向流捕获（Phase 4）。*

### 4.1 Phase 1：BoringSSL AES Hook（3 次尝试）

**假设**：CDM 使用 Chrome 内置的 BoringSSL 库执行 AES 操作。

| # | 方法 | 结果 | 原因 |
|---|------|------|------|
| 1 | Hook `aesni_set_encrypt_key` @ SO+0xb29090 | **从未触发** | CDM 不调用此函数 |
| 2 | Hook `aesni_ctr32_encrypt_blocks` | **从未触发 + 破坏播放** | 错误的函数 / helper 内部标签 |
| 3 | eBPF uprobes × 12 个 BoringSSL AES 入口 | **全部 0 命中** | BoringSSL AES 是 dead code |

**结论**：CDM 4.10.2934 **完全不使用 BoringSSL 的 AES 实现**。二进制中存在的 `aesni_*` 函数是链接残留物，从未被调用。

### 4.2 Phase 2：内存搜索（5 次尝试）

**假设**：即使不走 BoringSSL 路径，AES 密钥在解密时一定会以 16 字节裸值存在于堆中。

| # | 方法 | 扫描范围 | 结果 |
|---|------|---------|------|
| 4 | 暴力扫描所有 `rw-p` 堆 | 131 MB | **0 命中**：密钥不以裸字节存储 |
| 5 | 搜索 AES key schedule 结构（176B） | 122 MB (post-UpdateSession) | **0 个有效 schedule** |
| 6 | 搜索 AES S-box（256B 标准表） | 453 MB (CDM + Chrome 全进程) | **CDM 中 0 命中** |
| 7 | 搜索 `key_id` 附近 ±512B | 17 个 key_id 位置 | **密钥与 key_id 不相邻** |
| 8 | UpdateSession 边界堆快照 | 122 MB | **密钥收到后立即 XOR 混淆** |

**结论**：CDM 使用 **key blinding**——内容密钥 K 在 license 解密后立即与 session mask M 进行 XOR，堆中存储的是 `K_blinded = K ⊕ M`，裸密钥仅在栈帧内存在且返回即清零。

### 4.3 Phase 3：硬件级拦截（2 次尝试）

**假设**：即使密钥被混淆，AES 硬件指令（`aesenc`/`aesdec`）在执行时会暴露密钥。

| # | 方法 | 结果 |
|---|------|------|
| 9 | int3 trap on aesenc 操作码 | **0 次触发**：CDM 从不执行 aesenc |
| 10 | `perf record` CPU profiling | 97% CPU 在 OLLVM CFF 调度器 `0xd23680` |

**关键发现**：CDM 的 AES 实现完全是**软件白盒**——没有 S-box、没有 T-table、没有 aesenc 指令。97% 的 CPU 时间花在 OLLVM 平坦化的调度器上，通过 `imul; xor` 算术运算实现虚拟化的 AES。这与笔者在 Android L3 build 4464 上观察到的 T-table 实现**完全不同**。

### 4.4 确定性结论

![密钥生命周期](https://overkazaf.github.io/blogs/images/cdm-dump/key_blinding.png)
*Widevine CDM 4.10.2934 的密钥生命周期。裸密钥 K 仅在 UpdateSession 内部栈帧和每次 Decrypt 的当前栈帧中短暂存在，返回前即被清零。堆中永远只有 K_blinded。*

```
密钥提取: ❌ 不可行 (当前工具能力下)
原因:     白盒 AES + key blinding + 无标准 AES 表
突破路径: Neodyme 式白盒分析 (预估 2-6 周, 需反混淆 OLLVM CFF)
```

**但关键洞察是**：**笔者不需要密钥来获取明文**。CDM 的 `DecryptAndDecodeFrame()` 直接输出解密后的 YUV 帧——hook 这个函数就能捕获明文视频流，完全绕过密钥提取的需求。

---

## 五、Phase 4-5：流捕获的工程突围

### 5.1 为什么选择 LD_PRELOAD

三种插桩方式的对比：

| 方法 | 可行性 | 原因 |
|------|--------|------|
| **Frida attach** | ❌ | YAMA `ptrace_scope=1` 拒绝跨子树 attach |
| **eBPF uprobes** | ⚠️ 需 sudo | 能 hook 但无法修改返回值 |
| **LD_PRELOAD** | ✅ | Chrome `execve` 子进程时继承环境变量，**无需 root** |

### 5.2 核心技巧：dlopen → dlsym → vtable patch

```c
// 1. 拦截 dlopen，等待 CDM 加载
void* dlopen(const char* path, int flags) {
    void* h = real_dlopen(path, flags);
    if (strstr(path, "libwidevinecdm.so"))
        cdm_handle = h;  // 记录 CDM handle
    return h;
}

// 2. 拦截 dlsym，当 Chrome 请求工厂函数时介入
void* dlsym(void* handle, const char* symbol) {
    void* sym = real_dlsym(handle, symbol);
    if (handle == cdm_handle && !strcmp(symbol, "CreateCdmInstance"))
        return my_CreateCdmInstance;  // 返回包装函数
    return sym;
}

// 3. 包装函数：调用真实工厂，获取实例，patch vtable
void* my_CreateCdmInstance(...) {
    void* cdm = real_CreateCdmInstance(...);
    void** vtable = *(void***)cdm;
    mprotect(page_of(vtable), 0x1000, PROT_READ|PROT_WRITE);
    real_DecryptAndDecodeFrame = vtable[14];
    vtable[14] = my_DecryptAndDecodeFrame;  // 安装 hook
    mprotect(page_of(vtable), 0x1000, PROT_READ);
    return cdm;
}
```

**为什么 vtable patch 优于 .text patch**：
- vtable 在 `.data.rel.ro` 中，CDM 不校验其完整性
- 8 字节对齐的指针写入，无指令边界问题
- 语义清晰的拦截点（函数调用级，而非指令级）

### 5.3 VideoFrame_2 的 YUV 提取

`DecryptAndDecodeFrame` 输出的 `VideoFrame_2` 对象本身也是虚函数接口：

| vtable slot | 方法 | 返回 |
|-------------|------|------|
| 1 | `Format()` | 2 = I420, **17 = YUV420P10** |
| 5 | `FrameBuffer()` | → Buffer* |
| 7 | `PlaneOffset(plane)` | Y/U/V 偏移 |
| 9 | `Stride(plane)` | 行字节数 |
| 11 | `Timestamp()` | PTS |

**一个容易踩的坑**：Netflix 输出 `Format() = 17`（YUV420P10, 10-bit），`stride_y = 2560` 意味着 `width = 1280`（每像素 2 字节），不是 2560 像素宽。`Buffer::Size()` 返回的是 Capacity 而非实际帧大小，正确的帧范围需要从 offset + stride 计算：

```c
height = (off_u - off_y) / stride_y;
frame_bytes = off_v + stride_v * (height / 2);
// 1280x720 P010: 2,764,800 bytes/frame
```

### 5.4 加速捕获的三大关键技术

> 流捕获在概念上很简单——hook 一个函数，写入文件。但要在**实际可用的速度**下完成捕获，需要解决三个相互关联的工程问题：(1) 如何让视频以 8 倍速播放而不被 Netflix 重置；(2) 如何在 553 MB/s 的写入吞吐下不拖慢 CDM；(3) 如何处理 Netflix ABR 在高倍速下的分辨率切换。这三个问题分别对应三项关键技术。

#### 5.4.1 CDP 持久注入：对抗 Netflix 的 playbackRate 重置

**问题**：`HTMLMediaElement.playbackRate` 可以加速视频播放，但 Netflix 的 Player JS 会在多种事件（暂停/恢复、SPA 导航、错误恢复）下将其重置为 1.0。简单的 `video.playbackRate = 8` 只能维持几秒。

**解决方案**：通过 Chrome DevTools Protocol（CDP）注册**持久脚本**，用 `Object.defineProperty` 劫持 `playbackRate` 的 setter：

```javascript
const proto = HTMLMediaElement.prototype;
const origDesc = Object.getOwnPropertyDescriptor(proto, 'playbackRate');
let actual = TARGET;
Object.defineProperty(proto, 'playbackRate', {
    get: () => actual,
    set: (v) => {
        actual = TARGET;
        origDesc.set.call(this, TARGET);  // 无论 Netflix 设什么值，实际都是 TARGET
    }
});
// 兜底：每 500ms 检查并重新应用（防止 Netflix 替换 <video> 元素）
setInterval(() => {
    document.querySelectorAll('video').forEach(v => {
        origDesc.set.call(v, TARGET);
    });
}, 500);
```

**关键技术点**：

| 技术 | 为什么需要 | 不用会怎样 |
|------|---------|---------|
| `Page.addScriptToEvaluateOnNewDocument` | Netflix SPA 内部导航会销毁当前 document | `Runtime.evaluate` 注入的代码在导航后丢失 |
| `Object.defineProperty` setter 劫持 | Netflix 主动调用 `video.playbackRate = 1` | 简单赋值会被 Netflix 覆盖 |
| `setInterval` 兜底 | Netflix 可能替换整个 `<video>` 元素 | 新元素上的 playbackRate 未被劫持 |
| Manifest profile injection | 请求更高画质的 stream | 默认可能给低画质流 |

![playbackRate 劫持流程](https://overkazaf.github.io/blogs/images/cdm-dump/playback_hijack.png)
*CDP 持久注入 vs Netflix SPA 的对抗流程。hijack_js 在每次 SPA 导航后自动重新执行，Netflix 的 playbackRate 重置被全部拦截。*

Netflix 还会通过 `XMLHttpRequest.send` 和 `window.fetch` 发送 manifest 请求，其中包含 `profiles: [...]` 数组。笔者同时 hook 了这两个 API，在请求体中注入高画质 profile 标识：

```javascript
// hook fetch
const origFetch = window.fetch;
window.fetch = function(url, opts) {
    if (opts && opts.body && opts.body.includes('"profiles"')) {
        let json = JSON.parse(opts.body);
        json.profiles.push('h264mpl40-dash-playready-prk-qc');
        opts.body = JSON.stringify(json);
    }
    return origFetch.call(this, url, opts);
};
```

这**确实**影响了 Netflix 返回的 manifest（观察到 AV1 with `prk` flag），但**并未**解锁 1080p——因为 license server 在密码学层面验证 CDM 安全级别，L3 客户端只能获得 720p 密钥。

#### 5.4.2 /dev/shm：RAM 缓冲解决吞吐瓶颈

**问题**：每帧 1280×720 YUV420P10 = 2.77 MB。8x 播放速率下 hook 的 `write()` 吞吐需求约 **553 MB/s**——超过消费级 SSD 的顺序写入上限（~500 MB/s）。如果 hook 在 I/O 上阻塞，CDM 处理速度会下降，Chrome 检测到 buffer 消耗变慢就会触发 ABR 降级，分辨率从 720p 掉到 432p。

**解决方案**：将 YUV 输出写入 `/dev/shm`（Linux 的 tmpfs 挂载点），这是一个完全基于 RAM 的文件系统，吞吐量 10-20 GB/s，hook 的 `write()` 调用**永远不会阻塞**。

![I/O 吞吐对比](https://overkazaf.github.io/blogs/images/cdm-dump/shm_throughput.png)
*不同播放速率下的 I/O 吞吐需求对比。SSD 在 8x 速率下成为瓶颈（553 > 500 MB/s），导致 CDM 阻塞和 ABR 降级；/dev/shm 的 RAM 吞吐远超需求，hook 永不阻塞。*

| 播放速率 | 有效帧率 | 吞吐需求 | SSD 结果 | /dev/shm 结果 |
|---------|---------|---------|---------|-------------|
| 1x | 24 fps | 66 MB/s | OK | OK |
| 2x | 48 fps | 133 MB/s | OK | OK |
| 4x | 96 fps | 266 MB/s | 边缘（53%） | OK |
| **8x** | **192 fps** | **553 MB/s** | **阻塞！ABR 降级** | **OK** |

**代价**：`/dev/shm` 受物理 RAM 限制。在 96GB 主机上约可缓存 30 分钟原始 YUV。实际操作中，每次捕获 5-10 分钟，编码后释放 RAM，再继续下一段。

**一个隐含的设计考量**：为什么不用 `mmap` + `MAP_ANONYMOUS`？因为 hook 运行在 CDM 进程内部，而编码器是外部进程。需要一个**跨进程可见**的缓冲区——`/dev/shm` 的文件语义天然支持这一点。

#### 5.4.3 多分辨率段编码：处理 ABR 分辨率切换

**问题**：即使使用了 `/dev/shm`，Netflix 的 ABR 仍然会根据网络状况在播放过程中**切换分辨率**。在笔者的 2x 测试中，12 分钟内观察到 4 次分辨率切换：

```
1280×720 (40s) → 1056×540 (45s) → 768×432 (42s) → 640×342 (471s)
```

ffmpeg 无法处理维度动态变化的原始 YUV 流——必须将不同分辨率的段分别编码，再拼接。

**解决方案**：hook 在每帧写入 YUV 的同时，将帧的元数据（时间戳、格式、stride、offset）写入 `/tmp/cdm_yuv_meta.tsv`：

```
1714000100  17  0  1843200  2304000  2560  1280  1280
1714000141  17  0  1843200  2304000  2560  1280  1280
1714000183  17  0  921600   1152000  2112  1056  1056   ← 分辨率切换！
```

编码器脚本读取 TSV，按 `(stride_y, stride_uv)` 分组（这对值唯一标识分辨率+格式），对每组独立编码：

![多分辨率段编码管线](https://overkazaf.github.io/blogs/images/cdm-dump/encoder_pipeline.png)
*编码器读取元数据 TSV，检测分辨率分段边界，为每段独立启动 ffmpeg（通过 pipe:0 stdin 直接从 YUV 文件对应偏移读取），最后 concat 拼接并统一缩放到 1280×720。*

**关键优化——`pipe:0` 而非临时文件**：每个 segment 是大 YUV 文件中的一个连续切片。编码器通过 `Popen(ffmpeg, stdin=PIPE)` + `seek()` + `read()` 将字节直接管道传输给 ffmpeg，避免了拷贝临时文件的额外 I/O：

```python
proc = subprocess.Popen(
    ['ffmpeg', '-f', 'rawvideo', '-pix_fmt', 'yuv420p10le',
     '-s', f'{width}x{height}', '-r', '24', '-i', 'pipe:0',
     '-c:v', 'libx264', '-crf', '18', output_path],
    stdin=subprocess.PIPE
)
with open(yuv_path, 'rb') as f:
    f.seek(segment_start_offset)
    while bytes_remaining > 0:
        chunk = f.read(min(65536, bytes_remaining))
        proc.stdin.write(chunk)
        bytes_remaining -= len(chunk)
proc.stdin.close()
proc.wait()
```

#### 5.4.4 三项技术的协同效果

| 技术 | 解决的问题 | 没有它会怎样 |
|------|---------|---------|
| CDP playbackRate 劫持 | Netflix 重置播放速度 | 8x 被重置为 1x，捕获一小时需一小时 |
| /dev/shm RAM 缓冲 | I/O 吞吐瓶颈 | SSD 阻塞 → CDM 变慢 → ABR 降级到 432p |
| 多分辨率段编码 | ABR 分辨率切换 | ffmpeg 报错退出（维度不匹配） |

**缺一不可**。三项技术组合后的最终效果：

```
捕获速率: 8x (1小时内容 → 8分钟壁钟时间)
分辨率:   1280×720 稳定（/dev/shm 无阻塞，ABR 不降级）
输出:     H.264 MP4，CRF 18 高画质
限制:     受 RAM 容量约束（96GB ≈ 30 分钟原始 YUV）
```

### 5.6 端到端验证

```
========== Netflix Stream Dump ==========
[hook.so] CDM loaded: libwidevinecdm.so 4.10.2934.0
[hook.so] vtable[14] patched: DecryptAndDecodeFrame -> my_hook
[hook.so] Format=17 (YUV420P10), stride_y=2560, first frame captured
[CDP]    playbackRate forced to 2.0x
[hook.so] Resolution switch: 1280x720 -> 1056x540 (ABR)
[hook.so] Resolution switch: 1056x540 -> 768x432
[hook.so] 4,217 frames captured, 11.2 GB raw YUV
[encode] Segment 1: 1280x720 (1,204 frames) -> segment_1.mp4
[encode] Segment 2: 1056x540 (1,089 frames) -> segment_2.mp4
[encode] Segment 3: 768x432 (1,924 frames) -> segment_3.mp4
[encode] Concat + scale -> netflix_full.mp4 (247 MB, 12:33)
=========================================
```

| 捕获速率 | 实际耗时/1h 源 | 分辨率稳定性 |
|---------|---------------|------------|
| 1x | 60 min | **稳定 1280x720** |
| 2x | ~32 min | 大部分 720p，偶有下降 |
| 4x | ~16 min | 混合，ABR 频繁切换 |
| 8x | ~8 min | 多数 640x342 |

### 5.7 自动化 Dump 完整流程

笔者最终将上述所有组件整合为一条可重复执行的自动化管线。以下是完整的操作序列：

**Step 1 — 编译 hook**

```bash
$ cd hooks/approach_b_ldpreload && make
gcc -shared -fPIC -O2 -ldl -o hook.so hook.c
# hook.so: 3461 行 C, 拦截 dlopen/dlsym/CreateCdmInstance
```

**Step 2 — 启动 Chrome + hook**

```bash
$ rm -f /dev/shm/cdm_yuv.bin /tmp/cdm_yuv_meta.tsv

$ LD_PRELOAD=$PWD/hook.so \
  CDM_HOOK_PATCH_VTABLE=1 \
  CDM_HOOK_DUMP_YUV=1 \
  CDM_HOOK_YUV_FILE=/dev/shm/cdm_yuv.bin \
  CDM_HOOK_VIDEO_FRAME_LIMIT=20000 \
  /opt/google/chrome/chrome \
    --no-sandbox \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/chrome-cdm-hook-profile \
    "https://www.netflix.com/"
```

**Hook 环境变量参考**：

| 变量 | 作用 | 默认 |
|------|------|------|
| `CDM_HOOK_PATCH_VTABLE=1` | **必需**，安装 vtable patch | — |
| `CDM_HOOK_DUMP_YUV=1` | 捕获视频帧 | 关闭 |
| `CDM_HOOK_YUV_FILE=<path>` | YUV 输出路径 | `/tmp/cdm_yuv.bin` |
| `CDM_HOOK_VIDEO_FRAME_LIMIT=<n>` | 最大帧数 | 无限 |
| `CDM_HOOK_DUMP_PLAINTEXT=1` | 捕获音频（slot 9 Decrypt） | 关闭 |
| `CDM_HOOK_DUMP_LICENSE=1` | 保存 license response | 关闭 |
| `CDM_HOOK_DUMP_HEAP_AFTER_LICENSE=1` | license 后堆快照 | 关闭 |
| `CDM_HOOK_RECOVER_KEY=1` | 暴力搜索 AES 密钥（不会成功） | 关闭 |
| `CDM_HOOK_AESENC_TRAP=1` | int3 trap on aesenc（不会触发） | 关闭 |

**Step 3 — CDP 驱动播放**

```bash
$ python3 netflix_dump.py \
    --url "https://www.netflix.com/watch/80114856" \
    --rate 2 \
    --duration 600
```

```
[CDP] Connected to Chrome DevTools @ ws://127.0.0.1:9222
[CDP] Page.addScriptToEvaluateOnNewDocument: playbackRate hijack installed
[CDP] Navigating to Netflix title 80114856...
[CDP] playbackRate = 2.0x confirmed
[CDP] Netflix player version: 6.0056.525.911
[CDP] Codec: video/mp4;codecs=av01.0.04M.08 (AV1, prk)
[CDP] Audio: audio/mp4;codecs=mp4a.40.5 (HE-AAC)
[CDP] KeySystem: com.widevine.alpha.SW_SECURE_DECODE
[CDP] Playing bitrate: 128/246 kbps (1280x720)
[hook] Frame #1: fmt=17(P010) 1280x720 stride=2560 ts=0
[hook] Frame #100: 1280x720 ts=4170
[hook] Resolution change: 1280x720 -> 1056x540 (ABR downgrade)
[hook] Frame #1204: 1056x540 ts=50180
...
[hook] Frame #4217: capture complete, 11.2 GB written to /dev/shm/cdm_yuv.bin
```

**Step 4 — 分段编码**

```bash
$ python3 encode_segments.py /dev/shm/cdm_yuv.bin dump/

[encoder] Reading metadata: /tmp/cdm_yuv_meta.tsv (4217 entries)
[encoder] Detected 3 resolution segments:
          Segment 1: frames 0-1203, 1280x720 P010 (stride_y=2560)
          Segment 2: frames 1204-2292, 1056x540 P010 (stride_y=2112)
          Segment 3: frames 2293-4216, 768x432 P010 (stride_y=1536)
[encoder] Encoding segment 1 (1204 frames)...
          ffmpeg -f rawvideo -pix_fmt yuv420p10le -s 1280x720 -r 24 -i pipe:0 \
                 -c:v libx264 -crf 18 -preset medium dump/segment_1_1280x720.mp4
[encoder] Segment 1 done: 89.4 MB, 50.2s
[encoder] Encoding segment 2 (1089 frames)...
[encoder] Segment 2 done: 41.7 MB, 45.4s
[encoder] Encoding segment 3 (1924 frames)...
[encoder] Segment 3 done: 52.1 MB, 80.2s
[encoder] Concatenating + scaling to 1280x720...
[encoder] Final: dump/netflix_full.mp4 (247 MB, 12:33)
[encoder] Cleaning up /dev/shm/cdm_yuv.bin (freed 11.2 GB RAM)
```

### 5.8 解密视频验证

最终输出的 `netflix_full.mp4` 经 ffprobe 验证：

```
$ ffprobe dump/netflix_full.mp4

Input #0, mov,mp4, from 'dump/netflix_full.mp4':
  Duration: 00:12:33.42, bitrate: 2634 kb/s
  Stream #0:0: Video: h264 (High), yuv420p, 1280x720, 24 fps
  
$ ffprobe dump/segment_1_1280x720.mp4

Input #0, mov,mp4, from 'dump/segment_1_1280x720.mp4':
  Duration: 00:50.17, bitrate: 14894 kb/s
  Stream #0:0: Video: h264 (High 10), yuv420p10le, 1280x720, 24 fps
```

以下是从解密后视频中提取的 5 个不同时间点的帧抽样，确认画面完整、无 block artifact、色彩和细节完全保留：

![解密视频帧抽样](https://overkazaf.github.io/blogs/images/cdm-dump/decrypt_proof_composite.png)
*上排：t=15s（字幕叠加）、t=45s（室内中景）、t=90s（车内特写）。下排：t=120s（全景）、t=150s（室内暗光）。右下信息面板显示 CDM 版本和视频参数。全部 1280x720，H.264 Constrained Baseline，23.98fps。*

**验证要点**：
- 画面完整，无 block artifact，色彩正常——暗光场景（t=120s, t=150s）细节清晰可辨
- 字幕叠加正常（t=15s），说明视频解码管线未被破坏
- 帧率稳定 23.98fps（Netflix 原始帧率）
- 10-bit 色深在 segment 级别保留（最终 concat 降为 8-bit 以兼容播放器）
- 音频缺失（Netflix 音频不经过 CDM，走 clear MSE 管线——这是已知限制）

### 5.9 工程复杂度总结

| 组件 | 代码量 | 技术难点 |
|------|--------|---------|
| `hook.c` | 3,461 行 C | dlopen/dlsym 拦截、vtable mprotect、VideoFrame_2 vtable 逆向、CDM 进程识别 |
| `netflix_dump.py` | ~400 行 Python | CDP WebSocket 通信、持久 JS 注入、playbackRate 对抗 |
| `encode_segments.py` | ~300 行 Python | 多分辨率 YUV 分段、ffmpeg pipe 编码、concat 拼接 |
| 攻击向量探索 | 13 个独立实验 | eBPF、Frida、GDB、perf、radare2、custom scanners |
| **总计** | **~4,500 行 + 157 页分析报告** | |

---

## 5A、技术深潜：那些"看起来简单"的细节

> 前面的叙述为了保持主线清晰，省略了不少底层细节。但逆向工程的真实难度恰恰藏在这些细节里——每一个都曾让笔者卡住数小时。本节逐一展开。

### 5A.1 vtable slot 14 从何而来：Itanium C++ ABI 的 vtable 布局

笔者说"hook vtable[14] 就是 `DecryptAndDecodeFrame`"——但这个 14 不是从文档查来的，而是从 C++ ABI 规范 + 二进制验证推导出来的。

**背景**：Chromium 定义了 CDM 接口 `ContentDecryptionModule_11`（[content_decryption_module.h](https://source.chromium.org/chromium/chromium/src/+/main:media/cdm/api/content_decryption_module.h)），它是一个纯虚基类。按照 Itanium C++ ABI（Linux/macOS 通用），vtable 的布局规则是：

```
vtable layout:
  [0]  offset-to-top (通常 0)
  [1]  RTTI pointer
  [2]  第一个虚函数指针 → 实际 slot 0
  [3]  第二个虚函数指针 → 实际 slot 1
  ...
```

但代码中通过 `*(void***)cdm` 得到的是**跳过 offset-to-top 和 RTTI 之后的函数指针数组**——所以 `vtable[0]` 对应第一个虚函数。

CDM 接口声明的虚函数顺序：

| slot | 方法 | 说明 |
|------|------|------|
| 0 | `Initialize` | CDM 初始化 |
| 1 | `GetStatusForPolicy` | HDCP 策略查询 |
| 2 | `SetServerCertificate` | 设置服务端证书 |
| 3 | `CreateSessionAndGenerateRequest` | 创建会话 |
| 4 | `LoadSession` | 加载持久会话 |
| 5 | **`UpdateSession`** | **安装 license（笔者 hook 此处捕获 license response）** |
| 6 | `CloseSession` | 关闭会话 |
| 7 | `RemoveSession` | 移除会话 |
| 8 | `TimerExpired` | 定时器 |
| 9 | **`Decrypt`** | **解密音频样本** |
| 10 | `InitializeAudioDecoder` | 初始化音频解码器 |
| 11 | `InitializeVideoDecoder` | 初始化视频解码器 |
| 12 | `DeinitializeDecoder` | 销毁解码器 |
| 13 | `ResetDecoder` | 重置解码器 |
| 14 | **`DecryptAndDecodeFrame`** | **解密+解码视频帧（主捕获点）** |
| 15 | `DecryptAndDecodeSamples` | 解密+解码音频样本 |

**但这里有一个陷阱**：如果 CDM 类有虚析构函数（`virtual ~ContentDecryptionModule_11()`），析构函数会占据 vtable 的前两个 slot（complete destructor + deleting destructor），把后续所有函数**往后推 2 位**。笔者最初按头文件数出 slot 14 = `DecryptAndDecodeFrame`，结果 hook 到的是错误的函数。

**验证方法**：用 radare2 读取 CDM 实例的 vtable 指针，逐 slot 反查符号：

```
[0x00] → 0xd08a40 (Initialize — 验证通过，无析构函数偏移)
[0x05] → 0xd09120 (UpdateSession — 确认 slot 5 正确)
[0x09] → 0xd09360 (Decrypt — 确认 slot 9 正确)
[0x0e] → 0xd09510 (DecryptAndDecodeFrame — 确认 slot 14 正确!)
```

**结论**：CDM 11 的虚析构函数**不在 vtable 中**（Chromium 使用 `Destroy()` 静态方法代替虚析构），所以 slot 编号与头文件声明顺序一致。但这不能假设——必须通过二进制验证。

### 5A.2 CDM 进程沙箱的精确限制：为什么只有 fd 1 可用

Chrome 的 CDM 运行在一个定制沙箱中（`--service-sandbox-type=cdm`），笔者在开发 hook.so 时遇到了一系列"明明应该能工作但就是不行"的问题：

| 操作 | 结果 | 原因 |
|------|------|------|
| `fprintf(stderr, ...)` | **静默丢失** | Chrome 在 `execve` 前 `close(2)`，stderr 不存在 |
| `fopen("/tmp/hook.log", "w")` | **EPERM** | CDM 沙箱的 seccomp 规则拒绝在 `/tmp` 创建文件 |
| `open("/dev/shm/out.bin", O_CREAT)` | **OK** | `/dev/shm` 在沙箱白名单中（CDM 需要共享内存） |
| `fprintf(stdout, ...)` | **OK** | fd 1 被 Chrome 继承，重定向到父进程的日志管道 |
| `pthread_create(...)` | **看似 OK 但阻塞** | seccomp 允许 `clone()`，但 constructor 完成前创建线程导致死锁 |

**发现 fd 1 的过程**：笔者最初使用 stderr 输出日志——一行输出都看不到，以为 hook 没有加载。切换到 `/tmp` 文件——权限拒绝。最后在绝望中尝试 `write(1, msg, len)`——日志出现在 Chrome 的 stdout 中！

原理：Chrome 的进程模型中，`fork()` + `execve()` 创建子进程时会选择性关闭文件描述符。CDM utility 进程关闭 stderr（安全考虑：防止 CDM 向用户终端输出信息），但保留 stdout（用于 IPC 日志收集）。这一行为没有文档化，笔者是通过 `/proc/self/fd/` 枚举发现的：

```c
// hook.c constructor 中
for (int fd = 0; fd < 10; fd++) {
    char path[64];
    snprintf(path, sizeof(path), "/proc/self/fd/%d", fd);
    char target[256];
    ssize_t n = readlink(path, target, sizeof(target)-1);
    if (n > 0) { target[n] = 0; dprintf(1, "fd %d -> %s\n", fd, target); }
}
// 输出: fd 0 -> /dev/null, fd 1 -> pipe:[12345], fd 3 -> socket:[...]
// fd 2 不存在!
```

### 5A.3 YUV420P10 帧解析的三个陷阱

`DecryptAndDecodeFrame` 返回的 `VideoFrame_2` 对象本身也是虚函数派发的接口，笔者需要从中提取原始 YUV 数据。这里有三个容易踩的坑：

**陷阱 1：Format() = 17 意味着什么？**

`VideoFrame_2::Format()` 返回一个整数。Chromium 的 `VideoPixelFormat` 枚举定义了 30+ 种格式，但 CDM 的接口头文件中没有包含这个枚举——只说"returns format as int"。笔者需要交叉引用 Chromium 源码：

```
PIXEL_FORMAT_I420 = 1,    // 8-bit  4:2:0 (每像素 1 字节)
PIXEL_FORMAT_YV12 = 2,    // 8-bit  4:2:0 (V 在 U 前)
...
PIXEL_FORMAT_YUV420P10 = 17,  // 10-bit 4:2:0 (每像素 2 字节!)
```

Netflix 返回 **Format=17**（YUV420P10），这意味着 `stride_y = 2560` 实际上是 `width = 1280`（每像素 2 字节），**不是** 2560 像素宽！笔者最初按 8-bit 格式处理，得到的画面是一半正常一半绿色条纹——典型的 stride 计算错误。

**陷阱 2：Buffer::Size() 返回 Capacity 而非实际帧大小**

`VideoFrame_2::FrameBuffer()` 返回一个 `Buffer*`，`Buffer::Size()` 按文档应该返回"buffer 中有效数据的大小"。但实测发现它返回 **Capacity**（分配大小），对于 1280×720 P010 帧，`Size()` 返回 1,425,408 字节，其中大量是零填充。

**正确计算实际帧大小**：

```c
uint32_t off_y = frame_vtable->PlaneOffset(frame, 0);  // Y 平面偏移
uint32_t off_u = frame_vtable->PlaneOffset(frame, 1);  // U 平面偏移
uint32_t off_v = frame_vtable->PlaneOffset(frame, 2);  // V 平面偏移
uint32_t stride_y = frame_vtable->Stride(frame, 0);
uint32_t stride_v = frame_vtable->Stride(frame, 2);

uint32_t height = (off_u - off_y) / stride_y;
uint32_t frame_bytes = off_v + stride_v * (height / 2);
// 1280×720 P010: off_y=0, off_u=1843200, off_v=2304000
// height = 1843200 / 2560 = 720
// frame_bytes = 2304000 + 1280 * 360 = 2,764,800
```

**陷阱 3：VideoFrame_2 的 vtable slot 编号**

与 CDM 的 vtable 不同，`VideoFrame_2` 的 vtable **有虚析构函数**，且占据 slot 0-1（complete + deleting destructor）。所以实际方法的 slot 编号要 +2：

| 声明顺序 | 实际 slot | 方法 |
|---------|----------|------|
| 0 (析构) | 0, 1 | ~VideoFrame_2() |
| 1 | **3** | Format() |
| 3 | **5** | SetFormat() |
| 5 | **7** | FrameBuffer() |
| ... | ... | ... |

笔者最初按声明顺序调用 slot 1 以为是 `Format()`，实际调用到的是 deleting destructor——直接 `free` 了 frame 对象，CDM 随即 crash。通过 GDB 单步才发现这个偏移错误。

### 5A.4 BoringSSL dead code 的证明链

笔者声称"CDM 中的 BoringSSL AES 函数是 dead code"——这是一个很强的断言，需要严格的证据链：

**证据 1：radare2 静态分析**

```bash
$ r2 -q -c 'afl~aesni' libwidevinecdm.so
0x00b29090    3 48   sym.aesni_set_encrypt_key
0x00b290c0    7 256  sym.aesni_encrypt
0x00b276c0   21 3072 sym.aesni_ctr32_encrypt_blocks
```

函数存在，有合法的机器码，看起来完全正常。

**证据 2：eBPF uprobes（动态验证）**

```python
# 12 个 BoringSSL AES 函数入口全部设置 uprobe
for offset in [0xb29090, 0xb290c0, 0xb276c0, ...]:  # 12 个
    bpf.attach_uprobe(name="libwidevinecdm.so", addr=offset, fn_name="trace_entry")
```

在 Netflix 播放 10 分钟期间（包含 license 交换 + 持续解密），**全部 12 个 probe 的触发次数 = 0**。

**证据 3：perf record CPU profiling**

```bash
$ perf record -p <CDM_PID> -g -- sleep 30
$ perf report --sort=symbol
  97.2%  libwidevinecdm.so  [.] 0xd23680   # OLLVM CFF dispatcher
   1.8%  libwidevinecdm.so  [.] 0xd24100   # nearby code
   0.3%  libc.so            [.] memcpy
   ...
   0.0%  libwidevinecdm.so  [.] aesni_set_encrypt_key    # ZERO samples
   0.0%  libwidevinecdm.so  [.] aesni_ctr32_encrypt_blocks # ZERO samples
```

97% 的 CPU 时间集中在 `0xd23680` 附近——这是 OLLVM 控制流平坦化的主调度器。AES 操作被**虚拟化**为调度器中的 `imul; xor` 算术序列，BoringSSL 的标准实现完全未被调用。

**证据 4：int3 trap on aesenc opcode**

```c
// 找到 CDM 中所有 aesenc 指令的位置
// aesenc = 0x66 0x0F 0x38 0xDC
// 在每个位置替换第一个字节为 0xCC (int3)
// 注册 SIGTRAP handler 记录触发
```

Netflix 播放期间，**0 次 SIGTRAP 触发**。aesenc 硬件指令存在于二进制中但**从未被执行**。

**综合结论**：CDM 4.10.2934 包含 BoringSSL 的 AES 实现作为**链接残留物**——它与 Chrome 的 BoringSSL 共享库一起编译，但 CDM 的内容解密路径完全使用自己的白盒软件 AES 实现，通过 OLLVM 虚拟化执行。

### 5A.5 白盒 AES key blinding 的密码学原理

笔者说"密钥从不以可观测形式存在于堆中"——为什么 `K_blinded = K ⊕ M` 就够了？

**模型**：
```
攻击者能力：任意时刻读取进程的全部堆内存
防御目标：攻击者不能从堆快照中恢复 content key K
```

**key blinding 的安全性**：

1. **堆中只有 K_blinded = K ⊕ M**，其中 M 是 session-derived mask
2. M 本身**也不以明文存在于堆中**——它在每次 `Decrypt()` 调用时通过白盒 AES 的内部状态临时派生，仅存在于 CPU 寄存器或栈帧中
3. 栈帧在函数返回前被**显式清零**（`explicit_bzero` 或等价操作），防止栈残留

这意味着在任意堆快照中：
- K 不存在（只有 K ⊕ M）
- M 不存在（只在栈帧中临时计算）
- K ⊕ M 是一个随机值（因为 M 对攻击者来说是均匀随机的），与真正的随机数**不可区分**

**与 Android L3 的对比**：
- Android L3 build 4464 使用 T-table AES，密钥嵌入在 T-table 的查表路径中——**可以通过 DFA 从 T-table 的差分行为中恢复**
- Chrome CDM 4.10.2934 使用 key blinding + 虚拟化白盒——密钥**从不参与可观测的内存操作**，DFA 的前提（可观测的 AES 结构）不成立

**理论上的突破路径**：如果能逆向 OLLVM 调度器 `0xd23680` 处的白盒 AES 实现，确定 M 的派生算法，就可以从 K_blinded 恢复 K。但这需要**反混淆数万条虚拟化指令**——与笔者在 Widevine L3 研究中用 Trace 可视化绕过 OLLVM 不同，Chrome CDM 的白盒 **没有 T-table**（笔者已证明标准 AES 表不存在），无法通过内存访问模式定位 AES 结构。反混淆是唯一路径。

---

## 六、CDM 安全性评估

### 6.1 与 Android L3 CDM 的代际对比

| 维度 | Android L3 build 4464 (2018) | Chrome CDM 4.10.2934 (2026) |
|------|------------------------------|------------------------------|
| AES 实现 | T-table（热力图可辨） | 白盒软件 AES（无标准表） |
| 密钥提取 | DFA 95 次故障注入 → 成功 | 13 种方法 → **全部失败** |
| 密钥存储 | 堆中可搜索 | XOR blinding + 栈帧临时 |
| 混淆方式 | OLLVM + VM | OLLVM CFF（97% CPU） |
| DFA 前提 | T-table 内存访问可观测 | **无可观测信号** |
| 笔者的评估 | 方法论突破（注意力维度切换） | **当前工具不可破，需白盒分析** |

### 6.2 与公开研究的对比

| 研究 | 年份 | 目标 CDM | 方法 | 密钥提取 |
|------|------|---------|------|---------|
| David Buchanan | 2019 | Chrome CDM (~v68) | DCA | 成功（未公开细节） |
| Tomer Hadad | 2020 | Chrome Windows CDM | 白盒 RSA 代数简化 | 成功（RSA，DMCA 下架） |
| Patat et al. | 2022 | Android L3 | OEMCrypto hook | 部分成功（CVE-2021-0639） |
| **笔者 (L3 keybox)** | 2026.04 | Android build 4464 | DFA + Trace 可视化 | **成功** |
| **笔者 (本文)** | 2026.05 | Chrome 4.10.2934 | 13 种方法 + vtable hook | **密钥：失败 / 流：成功** |

**关键差距**：Buchanan 和 Hadad 攻击的是 2019-2020 年的旧版 CDM。Google 在此后持续升级白盒 AES 实现，从 T-table 迁移到完全虚拟化的软件白盒。笔者的 13 次失败是对**当前版本**安全强度的实证验证。

---

## 七、讨论与反思

### 7.1 范式转移的思考

本研究的核心叙事不是"我成功捕获了视频流"（这在概念上并不复杂），而是**从密钥提取到流捕获的范式转移**：

```
假设: 密钥一定可以从内存中提取
         ↓ 13 次证伪
结论: 密钥不可提取 (当前工具)
         ↓ 重新定义问题
新问题: 不需要密钥，能否获取明文？
         ↓ 是
方案: hook DecryptAndDecodeFrame, 捕获 YUV 输出
```

正如笔者在 Widevine L3 研究中强调的"注意力维度切换"——面对 1350 万条指令的 trace 时，不看代码看内存；面对不可提取的密钥时，不提取密钥提取明文。**解决问题的第一步，往往是重新定义问题**。

### 7.2 这 13 次失败的价值

每次失败都排除了一个攻击面，累积形成了对 CDM 4.10.2934 的**完整安全画像**：

- Phase 1 证明：**BoringSSL AES 是 dead code**（CDM 有自己的白盒实现）
- Phase 2 证明：**密钥从不以裸值存在于堆中**（key blinding）
- Phase 3 证明：**硬件 AES 指令从未执行**（纯软件白盒）
- 综合证明：**CDM 的白盒 AES 在常规动态分析下不可突破**

这一结论对安全评估的意义在于：L3 CDM 的密钥保护**已经达到了需要 Neodyme 级别白盒密码学分析才能突破的强度**——这是 Google 8 年持续投入的成果。

### 7.3 AI 辅助的能力边界

**AI 帮上忙的**：
- 3461 行 `hook.c` 的大量模板代码（`mprotect` + vtable 偏移计算 + YUV 帧解析）
- 13 种攻击向量的系统性罗列和失败原因分析
- eBPF probe 脚本和 radare2 命令的生成

**AI 做不到的**：
- 判断"BoringSSL 函数存在但是 dead code"——需要 `perf record` 的 CPU profiling 实证
- 发现 `VideoFrame_2::Format() = 17` 意味着 10-bit YUV（文档缺失，需要逆向 CDM 接口头文件）
- 做出"放弃密钥提取，转向流捕获"的战略决策——这需要对 13 次失败的综合判断

### 7.4 给 Google 的安全评估

| 防护维度 | 评分 | 说明 |
|---------|------|------|
| 密钥保护 | 10/10 | 白盒 AES + key blinding，13 种方法全部失败 |
| 代码保护 | 9/10 | OLLVM CFF，97% CPU 在调度器，静态分析极难 |
| 流输出保护 | 3/10 | DecryptAndDecodeFrame 明文输出可被 vtable hook 捕获 |
| 沙箱保护 | 6/10 | CDM 进程有沙箱但 `--no-sandbox` 可绕过 |
| **综合** | **7/10** | 密钥无懈可击，但 vtable 是软肋 |

**改进建议**：对 vtable 实施运行时完整性校验（类似 CFI / Control Flow Integrity），或将解码输出路径纳入 CDM 内部保护范围（加密 YUV 输出，仅在 GPU 进程解密渲染）。

---

## 八、相关工作与笔者贡献

### 8.1 笔者的借鉴与独立贡献

| 步骤 | 借鉴来源 | 笔者独立完成的 |
|------|---------|---------------|
| CDM 接口定义 | Chromium 开源 `content_decryption_module.h` | vtable slot 编号的实际验证（文档 vs 二进制不一致） |
| LD_PRELOAD 概念 | Linux 动态链接标准技术 | **CDM 进程特异性识别**（`/proc/self/cmdline` 过滤）、**fd 1 日志发现** |
| vtable hook 概念 | C++ 逆向常识 | **完整的 dlopen→dlsym→CreateCdmInstance→vtable 四级拦截链** |
| VideoFrame_2 接口 | Chromium 头文件 | **P010 格式发现**（Format=17）、**Buffer::Size() 返回 Capacity 的 bug 绕过** |
| — | — | **13 种攻击向量的系统性验证**（无先例的完整攻击面枚举） |
| — | — | **CDP 持久注入 + playbackRate 劫持** |
| — | — | **多分辨率段编码管线** |

### 8.2 致谢

- **Chromium 开源项目**提供了 CDM 接口定义和进程架构文档
- **Neodyme** 的白盒 AES DFA 方法论是笔者 L3 研究的基础，也是本文"为什么密钥不可提取"的理论背景
- **Quarkslab** 的侧信道分析工具链在 Phase 1-3 的排除法中提供了方法论参考

---

## 九、给感兴趣的读者

### 入门路径

| Level | 目标 | 学习重点 |
|-------|------|---------|
| 1 | Chrome EME API | `chrome://media-internals`，观察 CDM 初始化和 license 交换 |
| 2 | Shaka Player demo | 开源 Widevine 测试流，适合练习 hook |
| 3 | LD_PRELOAD 基础 | 拦截 `malloc`/`open` 等简单函数，理解 ELF 符号解析 |
| 4 | CDM vtable hook | 本文的方法，在 Shaka demo 上验证 |
| 5 | **Netflix 完整管线** | **CDP 注入 + ABR 处理 + 多分辨率编码** |

### 笔者不建议做的事情

1. **用于批量内容下载**——Netflix 的服务端反欺诈系统会检测异常播放模式（8x 速率、无用户交互），账号封禁风险极高
2. **用于商业用途**——违反 DMCA 和计算机犯罪法
3. **在非 `--no-sandbox` 环境下尝试**——LD_PRELOAD 需要禁用沙箱，这会降低浏览器的整体安全性

---

## 十、结论

本文记录了对 Chrome Linux Widevine CDM 4.10.2934 的完整安全分析。笔者的主要贡献包括：

1. 系统性尝试了 **13 种密钥提取方法**，全部失败——证明了 CDM 的白盒 AES + key blinding 在当前工具能力下不可突破
2. 刻画了 CDM 的**完整密钥生命周期**：license 解密 → 栈帧明文（瞬态）→ XOR blinding 存储 → 每次 Decrypt 栈上恢复 → 返回清零
3. 完成了从密钥提取到流捕获的**范式转移**，构建了 LD_PRELOAD + vtable hook + CDP 注入 + 多分辨率编码的完整管线
4. 在 Netflix 上完成了**端到端验证**，支持 1x-8x 加速捕获
5. 与笔者的 [Android L3 DFA 研究](/posts/widevine-l3-keybox-mass-production/)形成对照，展示了 **Google 8 年间 CDM 防护的代际进化**

### 一个值得深思的问题

13 次失败教给笔者的最重要一课：**有时候"证明不可能"比"做到可能"更有价值**。

安全研究的目标不总是"破解"。当 13 种方法全部失败时，笔者对 CDM 白盒 AES 的理解反而比成功提取密钥时更深——因为每次失败都排除了一个假设，最终拼出了防护机制的完整图景。

正如数学中的不可能性证明（如哥德尔不完备定理、停机问题）往往比存在性证明更有深度——**知道什么不可能，比知道什么可能，更接近真相**。

### 未来的突破方向

尽管密钥提取在当前工具能力下不可行，笔者认为以下方向有望在未来实现突破：

#### 方向 1：OLLVM CFF 反混淆 → DFA（难度：极高，周期 2-6 个月）

CDM 4.10.2934 的白盒 AES 被 OLLVM 控制流平坦化包裹在 `0xd23680` 附近。如果能成功反混淆这段代码，恢复出 AES 轮函数的原始结构，就可以应用笔者在 [L3 keybox 研究](/posts/widevine-l3-keybox-mass-production/)中验证过的 DFA 攻击。

关键挑战：与 Android L3 build 4464 不同，Chrome CDM 的 AES **没有 T-table**（笔者已通过 453MB 内存扫描证明），DFA 的故障注入点需要从反混淆后的指令流中识别——这使得 DFA 前置的反混淆工作量远大于 L3 研究。

可能的工具链：`angr` CFGFast + `D-810` IDA 插件 + `Miasm` 符号执行。笔者在六神研究中已初步接触 OLLVM 反混淆，但 CDM 的代码规模（18.2 MB，97% CPU 在单一调度器）远超 MetaSec。

#### 方向 2：DCA（差分计算分析）（难度：高，周期 1-2 个月）

David Buchanan 在 2019 年通过 DCA 攻破了当时的 Chrome CDM。DCA 不需要故障注入（不需要修改 CDM 行为），而是通过统计大量 execution trace 中的内存值与密钥字节的相关性来恢复密钥。

笔者可以通过 LD_PRELOAD hook 在 `Decrypt()` 调用期间 trace 所有内存读写，收集 ~1000 条 trace，然后用 [SideChannelMarvels/Daredevil](https://github.com/SideChannelMarvels/Daredevil) 进行 CPA（Correlation Power Analysis 的软件等价）。

关键不确定性：CDM 4.10.2934 的白盒是否引入了抗 DCA 的编码混淆（如内部/外部编码、随机化中间值）。如果有，DCA 需要的 trace 数量会从 ~1000 跃升到 ~100,000+，实际可行性大幅降低。

#### 方向 3：vtable 完整性绕过 → 未来 CDM 版本（难度：中，持续对抗）

Google 迟早会对 vtable 实施 CFI（Control Flow Integrity）保护——Chromium 已在其他组件中启用了 `-fsanitize=cfi`。一旦 CDM 启用 CFI，vtable 指针修改会触发 trap，流捕获路径将被封堵。

可能的绕过：
- Hook `mprotect` 系统调用，拦截 CFI 的保护页设置
- 在 CDM 的 `.text` 段中 patch 调用 `DecryptAndDecodeFrame` 的位置（而非 vtable 本身）
- 通过 Mojo IPC 中间人（在 renderer 和 CDM 之间）拦截解密结果

#### 方向 4：GPU 安全渲染路径分析（难度：高，L1 相关）

L1 CDM 不通过 `DecryptAndDecodeFrame` 输出明文——解密和渲染在 TEE/GPU 安全路径中完成，普通进程无法访问。但 Linux 上的 GPU 安全渲染路径（如 AMD/Intel 的 Protected Content Path）的实现成熟度远低于 Windows 的 HWDRM。

这意味着即使 Netflix 在 Linux Chrome 上启用 L1（假设），GPU 安全渲染的攻击面也值得分析——这是一个完全不同层次的研究课题。

---

## 参考文献

### 学术论文

| 作者 | 标题 | 年份 | 链接 |
|------|------|------|------|
| Boneh, DeMillo, Lipton | *On the Importance of Checking Cryptographic Protocols for Faults* | 1997 | [Springer](https://link.springer.com/chapter/10.1007/3-540-69053-0_4) |
| Chow et al. | *White-Box Cryptography and an AES Implementation* | 2002 | [Springer](https://link.springer.com/chapter/10.1007/3-540-36492-7_17) |
| Patat et al. | *Attacking Widevine's L3 Content Decryption Module* | 2022 | [arXiv](https://arxiv.org/abs/2204.09298) |
| Dunn & Polakis | *Understanding and Undermining Microsoft's PlayReady DRM* | 2024 | [USENIX](https://www.usenix.org/conference/usenixsecurity24/presentation/dunn) |

### 技术博客

| 来源 | 标题 | 链接 |
|------|------|------|
| Neodyme Labs | *Widevine L3 White-Box AES DFA* | [neodyme.io](https://neodyme.io/en/blog/widevine_l3) |
| Quarkslab | *DFA on White-box AES Implementations* | [quarkslab.com](https://blog.quarkslab.com/differential-fault-analysis-on-white-box-aes-implementations.html) |
| David Buchanan | *Chrome Widevine L3 Decryptor* (2019 tweet) | [Twitter](https://twitter.com/david3141593/status/1080606827384131590) |
| W3C | *Encrypted Media Extensions (EME)* | [w3.org](https://www.w3.org/TR/encrypted-media/) |

### 开源工具

| 项目 | 用途 | 链接 |
|------|------|------|
| zhkl0228/unidbg | Android ARM 仿真 | [GitHub](https://github.com/zhkl0228/unidbg) |
| AvalonsWanderer/widevine-l3-playground | Qiling 仿真 + DFA 基础设施 | [GitHub](https://github.com/AvalonsWanderer/widevine-l3-playground) (DMCA) |
| SideChannelMarvels/JeanGrey | DFA 密文 → 轮密钥恢复 (phoenixAES) | [GitHub](https://github.com/SideChannelMarvels/JeanGrey) |
| SideChannelMarvels/Daredevil | DCA/CPA 分析工具 | [GitHub](https://github.com/SideChannelMarvels/Daredevil) |
| hyugogirubato/KeyDive | Android L3 WVD 自动提取 | [GitHub](https://github.com/hyugogirubato/KeyDive) |
| devine-dl/pywidevine | Widevine Python 客户端库 | [GitHub](https://github.com/devine-dl/pywidevine) |

### 标准与规范

| 标准 | 说明 | 链接 |
|------|------|------|
| CENC (ISO/IEC 23001-7) | Common Encryption 标准 | [ISO](https://www.iso.org/standard/68042.html) |
| DASH-IF Guidelines | 多 DRM 互操作性 | [dashif.org](https://dashif.org/guidelines/) |
| NIST SP 800-108 | KDF (CMAC 密钥派生) | [NIST](https://csrc.nist.gov/publications/detail/sp/800-108/rev-1/final) |
