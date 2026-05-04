---
title: "十三次碰壁之后：当密钥不可提取时 - Chrome Widevine CDM 流捕获的工程突围"
slug: "chrome-cdm-stream-dump-widevine-vtable-hook"
date: 2026-04-29
lastmod: 2026-04-30
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
| **分析时间** | 2026-04-15 ~ 2026-04-25 |

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

### 5.4 /dev/shm：RAM 缓冲解决吞吐瓶颈

8x 播放速率下，720p 10-bit YUV 的写入吞吐需求约 **553 MB/s**——接近消费级 SSD 的极限。`/dev/shm`（tmpfs，纯内存）可以提供 10-20 GB/s，hook 永远不会因 I/O 阻塞。

代价：受 RAM 容量限制（96GB 主机约可缓存 30 分钟原始 YUV）。

### 5.5 CDP 持久 JS 注入

Netflix 的 SPA 在内部导航时会重置 `playbackRate`。通过 `Page.addScriptToEvaluateOnNewDocument` 注册持久脚本：

```javascript
const proto = HTMLMediaElement.prototype;
const origDesc = Object.getOwnPropertyDescriptor(proto, 'playbackRate');
Object.defineProperty(proto, 'playbackRate', {
    get: () => TARGET_RATE,
    set: (v) => { origDesc.set.call(this, TARGET_RATE); }
});
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

**验证要点**：
- 画面完整，无 block artifact，色彩正常
- 帧率稳定 24fps（Netflix 原始帧率）
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
| **笔者 (本文)** | 2026.04 | Chrome 4.10.2934 | 13 种方法 + vtable hook | **密钥：失败 / 流：成功** |

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
