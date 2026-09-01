---
title: "当 sign() 函数变成了两万行伪代码"
slug: "android-native-vmp-analysis"
date: 2026-07-10T14:30:00+08:00
lastmod: 2026-07-10T14:30:00+08:00
draft: false
tags: ["VMP", "virtual-machine-protection", "Android", "native", "reverse-engineering", "binary-security", "obfuscation", "Unicorn", "unidbg", "Frida"]
categories: ["reverse-engineering"]
description: "从实战视角拆解 Android Native 层 VMP 保护的三代演进、dispatcher 识别、handler 枚举与语义恢复，讨论 VMP 的真实安全边界与工程误区。"
toc: true
math: false
---

> **读完本文，你将获得：**
> - 理解 Android Native VMP 保护的三代架构演进：从固定 opcode 到状态依赖调度
> - 掌握一套识别 dispatcher、枚举 handler、恢复 opcode 语义的工程方法
> - 经历一个 VMP 保护签名函数的完整分析过程，包括五次失败和三次策略转向
> - 看清 VMP 保护的真实安全边界：它提高了静态分析成本，但 unidbg/Unicorn 黑盒调用可以完全绕过
> - 区分 VMP 与 OLLVM 的适用场景，理解"VMP 了就安全了"为什么是危险的误解
> - 获得一组面向防御者的改进提案，从 P0 到 P3

## 〇、摘要

Android 生态里，VMP（Virtual Machine Protection / 代码虚拟化保护）是国内加固厂商和头部 App 保护关键 Native 逻辑的主要手段之一。它把原始 ARM/ARM64 指令编译为自定义字节码，由嵌入 SO 的解释器在运行时调度执行，使 IDA Pro 的反编译结果从"可读的 C 伪代码"退化为"两万行 switch-case 状态机"。

笔者在过去两年中分析过多个使用 VMP 保护签名算法的 Android App，积累了以下观察：

1. **VMP 的核心价值不是"防逆向"，而是"防低成本逆向"**：它把分析门槛从"F5 一下就能读"提高到"需要恢复 VM 指令集才能理解语义"，但不能阻止 unidbg/Unicorn 级别的黑盒调用。
2. **三代 VMP 的区分不在"有没有 VM"，而在 dispatcher 的状态依赖程度**：一代用固定 handler table，二代用编码 opcode + 动态 dispatch，三代把 dispatch 本身变成状态机输出。
3. **实战中击穿 VMP 保护签名函数的关键转向，往往不发生在 VM 内部**：笔者多次在 I/O 边界、JNI 桥接层或 memory pattern 上找到突破口，而不是靠完整还原 VM 指令集。
4. **VMP 与 OLLVM 是互补而非替代关系**：OLLVM 保护控制流，VMP 保护指令语义，两者组合才能同时抵御 symbolic execution 和静态反编译。
5. **"VMP 了就安全了"是当前最危险的工程误解**：侧信道、I/O 边界、timing pattern 和黑盒模拟器四条路径都不需要理解 VM 内部语义。

本文是一篇原创逆向研究文章（Type A），记录笔者分析 Android Native VMP 的实战过程、失败路径和方法论积累。与本博客的 [Chrome VMP 文章](/blogs/posts/chrome-vmp-protection-vm-dispatch-whitebox/) 不同，那篇讨论的是 DRM/Widevine 场景下的白盒密码学保护；本文聚焦 Android App 保护厂商（360加固、梆梆、爱加密等）和自研 VMP 实现，目标对象是签名算法、加密模块和反作弊逻辑，分析工具链以 IDA Pro + Frida + unidbg/Unicorn 为主。

---

## Research Evidence

### Environment

| Item | Detail |
|---|---|
| Device | Pixel 6 (oriole), Redmi Note 12 Pro |
| OS | Android 13 (TP1A.220624.014), Android 14 (AP2A.240805.005) |
| Target | 某电商 App v8.x.x `libsign.so` (arm64-v8a), 某社交 App v6.x.x `libcore.so` (arm64-v8a) |
| Binary SHA256 | `7f3a2b91e4c8******d5f6a0b3e7c912` / `a4d1c8f0e5b2******9e3f7a6d2b8c01` (partial) |
| IDA Pro | 9.0 SP1 |
| Frida | 16.5.2 → 16.5.9 (升级解决 Thread enumeration 问题) |
| unidbg | 0.9.7 |
| Unicorn | 2.0.1 (Python binding) |
| radare2 | 5.9.6 |

### Hypothesis

- **H1**: VMP dispatcher 可以通过 indirect branch 密度定位
- **H2**: Handler table 在 `.rodata` 段有固定布局
- **H3**: Opcode 编码方案可以通过差分输入恢复
- **H4**: 签名函数的输入输出边界在 JNI 层可直接观测
- **H5**: unidbg 可以在不理解 VM 语义的情况下正确调用签名函数

### Experiments

| ID | 实验 | 验证假设 | 结果 | 关键证据 |
|---|---|---|---|---|
| E01 | IDA F5 反编译 `sign()` 入口 | — | 观察 | 22,847 行伪代码，单函数 |
| E02 | 统计 indirect branch 密度 | H1 | ✓ PASS | 0x4a200-0x4a800 区间 br x8 占 67% |
| E03 | `.rodata` 扫描 handler table | H2 | ✗ FAIL | 无固定 8 字节对齐指针数组 |
| E04 | 固定输入 trace handler 序列 | H3 | ✗ PARTIAL | 前 120 个 handler 稳定，之后分叉 |
| E05 | 差分输入 trace 对比 | H3 | ✓ PASS | 第 47 个 handler 起 msg 依赖 |
| E06 | JNI `GetStringUTFChars` hook | H4 | ✓ PASS | 输入明文 / 输出 32 字节 hex |
| E07 | unidbg 直接调用 `sign()` | H5 | ✗ FAIL | 第一次：JNI env 初始化崩溃 |
| E08 | unidbg 补齐 JNI 环境 | H5 | ✓ PASS | 输出与真机一致 |
| E09 | Frida stalker trace 全量指令 | H1 | ✓ PASS | 89% 指令落在 0x4a000-0x51000 |
| E10 | handler 语义分类（手动 + AI 辅助） | H3 | ✓ PARTIAL | 识别 31/48 个 handler 语义 |

---

## 一、路线总览

> 笔者分析 Android Native VMP 的路线不是一开始就画好的。它是在五次碰壁之后逐渐成型的：先从 IDA 静态分析失败开始，经过 Frida 动态 trace、差分实验、handler 分类，最后发现 unidbg 黑盒调用才是工程上性价比最高的路径。以下流程图记录的是这条实际走过的路线，而不是事后理想化的方法论。

### 分析路线

```text
Phase 1: 静态识别
  IDA F5 → 发现巨型 switch-case → 定位 dispatcher 区间
  radare2 交叉引用 → handler 候选集 → .rodata 扫描失败

Phase 2: 动态 trace
  Frida stalker → 全量指令 trace → 热区确认
  固定输入重复 trace → handler 序列稳定性验证
  差分输入 trace → opcode 与输入依赖关系

Phase 3: 语义恢复（部分）
  handler 分类（寄存器操作/内存/算术/控制流）
  opcode 解码方案推断 → 编码密钥假设 → 部分确认
  AI 辅助 handler 语义标注 → 31/48 识别

Phase 4: 边界突破
  JNI 层 hook → 输入输出明文
  unidbg 黑盒调用 → 环境补齐 → 签名复现
  timing / memory pattern → 侧信道验证

Phase 5: 安全边界评估
  VMP 内部语义恢复成本 vs 黑盒绕过成本
  防御改进提案
```

### 阶段总结

| 阶段 | 目标 | 方法 | 产出 | 耗时 |
|------|------|------|------|------|
| 静态识别 | 定位 dispatcher | IDA + radare2 | dispatcher 地址范围、handler 候选 | ~4h |
| 动态 trace | 确认 handler 序列 | Frida stalker | 稳定/不稳定 handler 分界点 | ~6h |
| 语义恢复 | 理解 VM 指令集 | 手动 + AI | 31/48 handler 语义 | ~12h |
| 边界突破 | 复现签名结果 | JNI hook + unidbg | 签名输出一致 | ~3h |
| 安全评估 | 评估保护有效性 | 对比分析 | 改进提案表 | ~2h |

🧑‍🔬 回头看这张表，最讽刺的是：花了 12 小时做语义恢复，结果 unidbg 黑盒调用 3 小时就解决了签名复现问题。语义恢复的价值在于理解保护机制本身，但如果目标只是"调用签名函数"，VMP 内部的复杂性并没有阻止任何事。

---

## 二、引言

### 2.1 VMP 出现在哪里

🧑‍🔬 笔者第一次遇到 VMP 保护的 Android Native 代码，是在分析某电商 App 的请求签名时。IDA 打开 `libsign.so`，F5 反编译 `Java_com_xxx_Sign_sign` 函数，得到了 22,847 行伪代码。一个正常的 HMAC-SHA256 签名函数大概 200 行。笔者一开始以为是 IDA 出了 bug，重新分析了三次才接受这个现实。

在 Android 生态中，VMP 保护通常出现在以下位置：

| 保护对象 | 为什么用 VMP | 典型场景 |
|----------|-------------|---------|
| **请求签名算法** | 防止签名被逆向后批量伪造请求 | `sign()`、`getToken()`、`encrypt()` |
| **设备指纹生成** | 防止设备指纹算法被还原和模拟 | `getDeviceId()`、`collectInfo()` |
| **反作弊检测** | 防止检测逻辑被分析后定向绕过 | root 检测、hook 检测、环境校验 |
| **授权验证** | 防止 license 校验被 patch 跳过 | SDK 授权、VIP 功能解锁 |
| **白盒密钥** | 防止密钥被从内存中提取 | AES key、HMAC secret |

### 2.2 谁在做 Android VMP

国内加固市场的主要参与者及其公开声称的 VMP 能力：

| 厂商 | VMP 能力声明 | 目标层 | 证据等级 |
|------|-------------|--------|---------|
| ![360](https://img.shields.io/badge/360-00B850?style=flat&logoColor=white) 360加固保 | SO VMP 加固 | Native | B（产品页声明） |
| ![梆梆](https://img.shields.io/badge/Bangcle-2B5797?style=flat&logoColor=white) 梆梆安全 | DEX VMP + SO VMP | Java + Native | B（产品页声明） |
| ![爱加密](https://img.shields.io/badge/iJiami-FF6600?style=flat&logoColor=white) 爱加密 | SO 虚拟化保护 | Native | B（产品页声明） |
| 腾讯乐固 | SO 加固（VMP 细节不确认） | Native | C（历史文档） |
| 自研实现 | 各头部 App 自行实现 | Native | C（社区观察） |

**说明**：以上能力来自厂商公开产品页和帮助文档，不代表默认配置或最新版本的实际防护效果。笔者未对任何加固产品进行采购或红队测试。

### 2.3 VMP 的成本与收益

🧑‍🔬 笔者接触过的项目中，选择对 Native 代码做 VMP 保护的团队通常面临一个很现实的权衡。这不是理论分析——以下表格中的每一行"成本"都是笔者在实际项目中亲眼见过的问题：

| 维度 | VMP 的收益 | VMP 的成本（笔者见过的实际案例） |
|------|-----------|-------------------------------|
| **分析难度** | IDA 反编译结果不可读，symbolic execution 失效 | 保护部署和调试复杂度显著增加——某团队花了两周才把 VMP 工具链接入 CI |
| **性能** | — | 被保护函数执行速度下降 3x-50x。§7.3 中的某 App 签名延迟从 2ms 涨到 120ms |
| **包体积** | — | VM 解释器 + 字节码增加 SO 体积。某样本的 `libsign.so` 从 280KB 膨胀到 1.2MB |
| **兼容性** | — | 某样本在 Android 14 Beta 上 crash，原因是 ART 的 JNI 校验变严了 |
| **崩溃定位** | — | VM 内部崩溃的 stack trace 只显示 dispatcher 地址，没有任何语义信息。某 App 的线上 crash 率在上 VMP 后涨了 0.3%，排查花了三周 |
| **安全边界** | 提高静态逆向成本 | 不阻止黑盒调用、侧信道、I/O 边界分析——这正是本文要详细讨论的 |

这个权衡决定了一个工程原则：**VMP 应该只保护少量高价值、低频调用的关键路径**。笔者见过一个反面教材：某 App 把整个 SO（约 200 个函数）都做了 VMP，结果 App 启动时间增加了 2.3 秒（VM 解释器初始化 + 首次 dispatch 预热），QA 团队发现 12 个机型出现了兼容性问题，线上 crash 量翻倍。最终只保留了 3 个核心签名函数的 VMP 保护。

### 2.4 本文与 Chrome VMP 文章的区别

本博客此前发表的 [Chrome VMP 文章](/blogs/posts/chrome-vmp-protection-vm-dispatch-whitebox/) 讨论的是 DRM/Widevine 场景下的 VM-based Protection。两篇文章虽然都涉及 VMP，但分析对象、威胁模型和关注点完全不同：

| 维度 | Chrome VMP 文章 | 本文 |
|------|----------------|------|
| **保护对象** | DRM content key、白盒 AES | 签名算法、设备指纹、反作弊 |
| **平台** | 桌面 Chrome (x86-64) | Android (ARM64) |
| **保护来源** | Google/Widevine 团队 | 国内加固厂商 + App 自研 |
| **攻击者目标** | 提取可离线解密的 content key | 复现签名函数、绕过设备检测 |
| **关键差异** | 明文必须出现（播放需要） | 签名输出可通过 I/O 边界获取 |
| **分析方法** | 边界建模 + 差分实验 | dispatcher 识别 + handler 枚举 + 黑盒调用 |

Chrome 场景下，VMP 要解决的核心问题是"密钥不能被提取"。Android 签名场景下，VMP 要解决的核心问题是"算法不能被理解和独立复现"。但 unidbg 的存在使得后者的安全边界天然比前者低：攻击者不需要理解算法，只需要正确调用它。

---

## 三、知识准备

> 这一节记录笔者在分析第一个 VMP 样本时逐步搞清楚的五个核心概念。它们不是事先就知道的教科书定义，而是在 IDA 里看了三天"乱码"之后，回头查资料、对照二进制才拼出来的认知。如果你已经知道 dispatcher、handler、virtual register 和 opcode encoding 的含义，可以直接跳到 §四。

### 3.1 从 22,847 行伪代码里辨认出五个部件

🧑‍🔬 笔者在 IDA 里盯着那个巨型函数的反编译输出时，最初的感觉就是一团乱码——两万多行里到处是 `v312 = (v287 ^ (v301 >> 3)) & 0xFF`、`goto LABEL_4712` 这样的内容。直到笔者注意到一个模式：有一段大约 600 字节的代码反复出现在执行热区里，它做的事情永远是"读一个值、查一张表、跳到某个地址"。那就是 dispatcher。

从 dispatcher 出发，笔者逐步辨认出了 VM 的五个部件。以下不是教科书定义，而是"笔者在二进制中实际看到了什么"：

| 概念 | 笔者在二进制中看到的 | 怎么确认的 |
|------|---------------------|-----------|
| **Bytecode** | `.rodata` 段 0x52000 附近一片高熵字节，IDA 不识别为代码也不识别为字符串 | dispatcher 的 fetch 操作始终从这个区域读取 |
| **Dispatcher** | 0x4a200-0x4a800 区间：`br x8` 占比 67%，执行热区占全函数 65% 指令 | Frida stalker 热区统计（§5.2 详述） |
| **Handler** | dispatcher 跳转的 41 个不同目标地址，每个基本块 15-80 条指令 | 统计 `br x8` 的所有跳转目标并去重 |
| **Virtual registers** | 栈上偏移 `[sp, #0x40]` 起始的 128 字节（16 个 uint64），多个 handler 都读写这个区域 | 对比 4 个不同 handler 的内存访问模式，发现它们共享同一块栈空间 |
| **Opcode encoding** | 不是明文——同一个 bytecode 值在不同位置被 dispatch 到不同 handler | 差分实验：固定输入跑两次 trace，handler 序列完全一致；说明编码是确定性的但不是恒等映射 |

这五个部件的辨认前后花了约两天。最困难的部分不是找到 dispatcher（热区分析就够了），而是确认 virtual registers 的布局——因为 IDA 把栈上的 128 字节分解成了几十个独立的局部变量，笔者一开始根本没意识到它们是一个数组。直到用 Frida 在 handler 入口和出口分别 dump 这块内存，看到多个 handler 以 8 字节步长读写同一区域，才确认这就是虚拟寄存器文件。

### 3.2 笔者走过的一个弯路：把 VMP 当成"更强的混淆"

🧑‍🔬 笔者在遇到 VMP 之前，分析过几个 OLLVM 保护的 SO。OLLVM 的特征是控制流被打散，但原始指令仍然存在——你能在 IDA 里看到 `add`、`ldr`、`str` 这些正常的 ARM 指令，只是它们之间的跳转关系变得混乱。笔者最初以为 VMP 只是"更乱的 OLLVM"，所以拿着分析 OLLVM 的那套方法（用 angr 做 symbolic execution + D810 去混淆）去跑 VMP 样本。

结果 angr 跑了 40 分钟，内存占用飙到 32GB，状态空间爆炸后 OOM 退出。笔者以为是参数没调好，调整了 exploration strategy 重跑了三次，每次都以同样的方式失败。

后来笔者才理解根本原因：OLLVM 混淆的是控制流的外观，原始指令的语义对 symbolic engine 来说仍然是可解的；但 VMP 替换了指令本身，symbolic engine 面对的不是"被打乱的程序"，而是"一个解释器程序"——它看到的语义是 dispatcher 的语义（取指、解码、分发），不是原始算法的语义。两者的差别不在程度，在维度。

| 笔者踩的坑 | OLLVM 下可行 | VMP 下为什么不行 |
|-----------|-------------|-----------------|
| angr symbolic execution | 能穿透 FLA/BCF（数小时） | 状态空间爆炸：dispatcher 循环 + 间接跳转 |
| IDA F5 读伪代码 | 能读（虽然乱） | 输出是 dispatcher 主循环，不是原始算法 |
| 搜索算法常量（S-box、magic number） | 能找到（常量未被替换） | 常量被编码或在运行时计算，搜不到 |

这个弯路花了笔者约一天半。事后回想，如果一开始就注意到 IDA 输出的结构——一个大 while 循环里套着大量 indirect branch——就应该意识到这是解释器模式而不是混淆模式。但当时笔者的经验库里只有 OLLVM，没有 VMP。

### 3.3 Android 平台的约束：笔者踩过的几个坑

🧑‍🔬 与 x86 Windows 上的 VMProtect、Themida 不同，Android Native VMP 有几个平台特有的约束。以下不是从文档里列出来的，而是笔者在实际分析中每一条都踩过：

| 约束 | 笔者实际遇到的问题 | 怎么解决的 |
|------|-------------------|-----------|
| **ARM64 指令集** | handler 里大量 `ADRP + ADD` 对，IDA 有时无法正确解析 PC-relative 地址 | 手动计算：`(ADRP_page << 12) + ADD_offset` |
| **Android linker** | unidbg 加载 SO 时 GOT 表未正确初始化，导致 `strlen` 等 libc 调用崩溃 | 指定 `AndroidResolver(23)` 并手动 hook 缺失的 libc 函数 |
| **JNI 边界** | VM 入口函数的第一个参数是 `JNIEnv*`，但 VMP 把它当成普通指针传给了 virtual register | 在 unidbg 中必须正确初始化完整的 JNI function table |
| **多 ABI** | 目标 App 同时提供 armeabi-v7a 和 arm64-v8a，两个 SO 的 VMP bytecode 完全不同 | 选择 arm64-v8a 版本分析（handler 更规整，IDA 反编译质量更好） |
| **SELinux** | 某个样本的 handler 尝试 `mmap` 一块 RWX 内存（可能是 JIT 优化），在 Pixel 6 上被 SELinux 拒绝后静默走了 fallback 路径 | strace 看到 `mmap` 返回 EPERM 后 trace 出现分叉，排查了 2 小时才定位 |

最后一条是最意外的。笔者在 Pixel 6 上的 trace 和在一台 root 后关闭了 SELinux 的 Redmi 上的 trace 不一致，差异正是因为 SELinux 阻止了 RWX 映射。这意味着同一个 VMP 保护的 SO，在不同安全策略的设备上可能走不同的执行路径——一个做动态分析时很容易忽视的变量。

---

## 四、VMP 的三代演进

> 这一节不是教科书式的分类，而是笔者在分析不同 App 时实际遇到的三类 VMP 实现。分代标准是 dispatcher 的状态依赖程度——这直接决定了分析难度和可自动化程度。

### 4.1 一代：固定 opcode table，线性 dispatch

> 一代 VMP 的特征是：handler table 是固定的函数指针数组，opcode 到 handler 的映射关系在二进制生命周期内不变。dispatcher 就是一个简单的 `switch(opcode)` 或间接跳转。

🔬 笔者在某工具类 App 的 `libutil.so` 中遇到的第一个 VMP 实例就是这种类型。IDA 反编译后，dispatcher 的核心逻辑非常清晰：

```c
// IDA F5 伪代码（简化）
void vm_dispatch(vm_ctx *ctx) {
    while (ctx->running) {
        uint8_t opcode = ctx->bytecode[ctx->pc];
        ctx->pc++;
        switch (opcode) {
            case 0x01: handler_mov_reg(ctx); break;
            case 0x02: handler_add(ctx); break;
            case 0x03: handler_sub(ctx); break;
            case 0x04: handler_load(ctx); break;
            case 0x05: handler_store(ctx); break;
            case 0x06: handler_cmp(ctx); break;
            case 0x07: handler_jmp(ctx); break;
            // ... 24 个 handler
            case 0x1F: handler_ret(ctx); break;
            default:   ctx->running = 0; break;
        }
    }
}
```

🧑‍🔬 **假设**：如果 opcode 是固定映射，那么同样的输入应该产生完全相同的 handler 调用序列。

**实验**：用 Frida stalker trace 两次相同输入的 handler 序列，逐条比对。

**结果**：✓ 完全一致。2048 个 handler 调用，序列 diff 为 0。

**确认**：这是一个一代 VMP——opcode table 固定，dispatch 无状态依赖。

一代 VMP 的分析相对直接：

| 步骤 | 方法 | 难度 |
|------|------|------|
| 定位 dispatcher | 搜索大型 switch-case 或 indirect branch table | 低 |
| 枚举 handler | 提取 switch-case 的所有目标地址 | 低 |
| 恢复 opcode 映射 | 直接读取 switch-case 的 case 值 | 低 |
| 理解 handler 语义 | 逐个反编译 handler 函数 | 中 |
| 恢复原始算法 | 组合 handler 语义和 bytecode stream | 中到高 |

🧑‍🔬 一代 VMP 的主要价值在于阻止 IDA F5 直接输出可读伪代码，但它不能阻止有耐心的分析者恢复完整语义。笔者花了约 8 小时完成这个样本的全量恢复，发现底层是一个标准的 HMAC-SHA256。8 小时对于一个签名函数来说，成本已经远高于无保护情况下的 5 分钟，但对于高价值目标来说仍然不够。

### 4.2 二代：动态 dispatch，编码 opcode，handler 变异

> 二代 VMP 开始引入运行时状态：opcode 不再是明文，而是经过编码；handler table 不再是固定数组，而是通过某种变换间接索引；handler 本身也可能有多个变体。分析难度从"读 switch-case"跳到"恢复编码方案"。

🔬 笔者在某电商 App `libsign.so` 中遇到的 VMP 实例属于这一代。IDA F5 的输出不再是清晰的 switch-case，而是：

```c
// IDA F5 伪代码（简化，实际更混乱）
void vm_dispatch(vm_ctx *ctx) {
    while (ctx->running) {
        uint32_t raw = *(uint32_t*)(ctx->bytecode + ctx->pc);
        uint32_t key = ctx->decode_key;
        uint32_t decoded = (raw ^ key) & 0xFF;
        uint32_t idx = permute_table[decoded];
        
        void (*handler)(vm_ctx*) = (void*)(ctx->handler_base + offsets[idx]);
        handler(ctx);
        
        ctx->pc += 4;
        ctx->decode_key = rotate_left(key ^ raw, 7);
    }
}
```

关键变化：
1. **Opcode 编码**：`raw ^ key` —— opcode 不是明文，需要知道 `decode_key` 才能还原
2. **Key 滚动**：`decode_key` 在每次 dispatch 后更新，依赖上一条指令的 raw 值
3. **间接索引**：通过 `permute_table` 二次映射，不是直接用 decoded 值索引

🧑‍🔬 笔者最初的 **假设** 是：`decode_key` 的初始值是固定的，只要找到初始值就能解码整个 bytecode stream。

**实验**：在 `vm_dispatch` 入口处 hook，记录 `ctx->decode_key` 的初始值。连续调用 10 次 `sign()`，比较初始值。

**结果**：✗ **失败**。10 次调用中，初始值有 3 个不同的值。进一步排查发现，`decode_key` 的初始值来自 `vm_ctx` 的构造函数，而构造函数会读取一个 session counter。

**转向**：不能静态解码 bytecode，需要动态 trace。

🔬 笔者转向 Frida stalker，在 dispatcher 入口和 handler 跳转点插桩，记录每次 dispatch 的 `decoded` 值和 handler 地址。

```python
# Frida script（简化）
Interceptor.attach(dispatcher_addr, {
    onEnter: function(args) {
        var ctx = args[0];
        var raw = Memory.readU32(ctx.add(pc_offset).readPointer());
        var key = ctx.add(key_offset).readU32();
        var decoded = (raw ^ key) & 0xFF;
        send({type: 'dispatch', decoded: decoded, handler: '...'});
    }
});
```

**结果**：成功记录了完整的 handler 调用序列。同一输入的序列稳定，不同输入的序列在第 47 个 handler 处开始分叉（这正是输入数据被首次读取的位置）。

🧑‍🔬 二代 VMP 的 `permute_table` 恢复是另一个挑战。笔者尝试了两种方法：

| 方法 | 过程 | 结果 |
|------|------|------|
| 静态提取 | 在 `.rodata` 中搜索 256 字节的排列表 | ✗ 找到 4 个候选，但都不是真正的 permute_table——它们是 S-box 残留 |
| 动态记录 | hook handler 跳转指令，记录 (decoded, handler_addr) 对 | ✓ 跑了 500 次不同输入，覆盖了 48 个 handler 中的 45 个 |

剩下 3 个未触发的 handler 很可能是错误处理或反调试路径。笔者选择暂时忽略它们。

二代 VMP 的分析要点：

| 步骤 | 方法 | 难度 |
|------|------|------|
| 定位 dispatcher | indirect branch 密度 + 热区分析 | 低到中 |
| 恢复编码方案 | 动态 trace + decode_key 跟踪 | 中到高 |
| 枚举 handler | 动态 trace 覆盖 | 中 |
| 理解 handler 语义 | 逐个反编译 + AI 辅助分类 | 高 |
| 恢复原始算法 | handler 序列 + 数据流分析 | 高 |

### 4.3 三代：嵌套 VM，反模拟，状态依赖 dispatch

> 三代 VMP 把 dispatch 本身变成了状态机的一部分。opcode 的含义不仅依赖编码密钥，还依赖 VM 的当前执行状态——同一个 raw opcode 在不同状态下可能分发到不同的 handler。某些实现甚至嵌套多层 VM，或在 handler 内部检测执行环境。

🧑‍🔬 笔者在某社交 App `libcore.so` 中遇到的 VMP 实例表现出三代特征。最初的 **假设** 是：这只是一个编码方案更复杂的二代 VMP，多跑几轮 trace 就能覆盖。

**实验**：同一输入连续 trace 5 次 handler 序列。

**结果**：✗ **失败**。5 次 trace 中，handler 序列在前 80 次调用完全一致，但从第 81 次开始出现分叉。分叉不是随机的——每次分叉的位置都不同，但分叉后的序列长度大致相同。

这个现象让笔者困惑了整整两天。如果编码方案是确定性的（只依赖 bytecode 和初始 key），同一输入应该产生相同的序列。分叉意味着某个状态来自 bytecode 之外。

🧑‍🔬 **推断**：dispatcher 读取了某个随执行变化的外部状态。

**实验**：在 dispatcher 附近搜索 `mrs` 指令（读取系统寄存器）和内存读取模式。

**结果**：✓ 发现 dispatcher 内部有一条 `mrs x9, CNTVCT_EL0`（读取 CPU cycle counter），并且 `x9` 的低 2 位会影响后续的 handler 索引计算。

🔬 这意味着：**dispatch 路径依赖 CPU 时钟**。同一个 bytecode 在不同时刻执行可能走不同的 handler 路径，但最终语义等价（多条路径计算相同结果）。这是一种 **多态 dispatch**——增加 trace 分析的噪声，但不改变计算语义。

```text
                 ┌─ path A (CNTVCT[1:0] == 0b00) ─→ handler_add_v1
opcode 0x12 ───├─ path B (CNTVCT[1:0] == 0b01) ─→ handler_add_v2
                 ├─ path C (CNTVCT[1:0] == 0b10) ─→ handler_add_v3
                 └─ path D (CNTVCT[1:0] == 0b11) ─→ handler_add_v4
```

四个 handler 变体做相同的加法运算，但使用不同的临时寄存器、不同的指令序列、不同的常量编码。对于 trace 分析来说，需要识别这四个变体的等价性才能正确恢复 opcode 语义。

笔者还发现了另一个三代特征：**anti-emulation 检查**。

🔬 在第 23 个 handler 中，有一段代码读取 `/proc/self/maps` 并检查是否存在 `libunicorn` 或 `frida-agent` 的映射。如果检测到，不是立刻崩溃，而是静默修改 `decode_key`，导致后续所有 opcode 解码错误——输出结果看起来像一个有效的签名（32 字节 hex），但值是错误的。

🧑‍🔬 这个设计相当巧妙。传统反调试检测到 Frida 后直接 crash，攻击者可以 hook 检测函数返回 false 来绕过。但这里的实现是：检测后不报告、不崩溃，只是"悄悄出错"。笔者直到用真机结果和 unidbg 结果做 diff 时，才发现 unidbg 的输出全部不正确。

排查过程：

| 步骤 | 操作 | 结果 |
|------|------|------|
| 1 | 对比真机和 unidbg 输出 | 32 字节全部不同 |
| 2 | 怀疑 JNI 环境不完整 | 补齐后仍然不同 |
| 3 | 怀疑随机数种子不同 | 固定种子后仍然不同 |
| 4 | 在 unidbg 中 hook `/proc/self/maps` 读取 | 发现返回值包含 unicorn 相关映射 |
| 5 | 伪造 maps 内容 | ✓ 输出与真机一致 |

🧑‍🔬 从第 1 步到第 5 步，笔者花了约 6 小时。其中前 4 步都在错误的方向上。回头看，如果一开始就 diff 了 unidbg 和真机的 `/proc/self/maps` 输出，可能 30 分钟就能定位问题。但这就是实际分析的过程——你不知道哪个假设是对的，只能逐个排除。

### 4.4 三代对比

| 特性 | 一代 | 二代 | 三代 |
|------|------|------|------|
| **Opcode 编码** | 明文 | 异或/旋转 + 滚动密钥 | 状态依赖 + 多态 |
| **Handler table** | 固定数组 | 间接索引 + permute | 动态选择 + 变体 |
| **Dispatch 确定性** | 完全确定 | 依赖初始 key，序列确定 | 依赖时钟/环境，路径不确定但语义确定 |
| **反模拟** | 无 | 简单环境检测 | 静默错误输出 |
| **Trace 分析** | 直接有效 | 需要恢复编码方案 | 需要识别等价路径 |
| **静态恢复难度** | 中 | 高 | 极高 |
| **自动化工具** | angr/Triton 可能穿透 | 定制脚本可能穿透 | 目前无通用工具 |
| **笔者实际耗时** | ~8h（全量恢复） | ~22h（部分恢复） | ~40h（部分恢复，含排查反模拟） |

---

## 五、实战：分析一个 VMP 保护的签名函数

> 这一节记录笔者分析某电商 App `libsign.so` 中 VMP 保护的 `sign()` 函数的完整过程。目标不是展示成功，而是展示从困惑到理解的真实路径——包括每一次假设、每一次失败和每一次策略转向。这个样本属于二代 VMP。

### 5.1 初始困惑：22,847 行伪代码

🔬 IDA 打开 `libsign.so`（ARM64），定位到 JNI 导出函数 `Java_com_xxx_sign_Sign_generateSign`，F5 反编译。

```text
=== IDA 输出统计 ===
函数地址: 0x49E00
函数大小: 0x7200 bytes (29,184 bytes)
反编译行数: 22,847
局部变量: 312
基本块数: 1,847
indirect branch: 89
```

🧑‍🔬 第一印象：这不是一个正常的签名函数。正常的 HMAC-SHA256 实现大约 200 行反编译代码，3-5 个函数调用。22,847 行意味着要么是一个极度内联的实现，要么是 VMP。

**假设**：这是 VMP 保护，应该能找到 dispatcher 结构。

### 5.2 定位 dispatcher

🔬 笔者用 radare2 统计了函数内部的 indirect branch 分布：

```bash
$ r2 -q -c 'aa; pdf @ 0x49E00 | grep "br x"' libsign.so | wc -l
89

$ r2 -q -c 'aa; pdf @ 0x49E00 | grep "br x8"' libsign.so | wc -l
67
```

89 条 indirect branch 中，67 条使用 `x8` 寄存器。在 ARM64 中，`br x8` 是典型的 computed goto / switch-case 模式。

进一步分析这 67 条 `br x8` 的地址分布：

```text
0x4a200 - 0x4a800: 41 条 (61%)  ← 高密度区
0x4b000 - 0x4d000: 19 条
0x4d000 - 0x51000:  7 条
```

🧑‍🔬 **推断**：0x4a200-0x4a800 是 dispatcher 的核心区域。它占函数总大小的 2.1%，却包含 61% 的 indirect branch。

**验证**：用 Frida stalker 对整个函数做指令级 trace，统计每个 0x100 对齐区间的指令执行次数。

```text
=== Frida Stalker Trace (单次 sign() 调用) ===
Total instructions executed: 487,392
0x4a200-0x4a300: 142,847 (29.3%)
0x4a300-0x4a400:  89,234 (18.3%)
0x4a400-0x4a500:  51,023 (10.5%)
0x4a500-0x4a600:  34,118 ( 7.0%)
  ... (其余区间各 < 3%)
```

✓ **确认**：0x4a200 附近是执行热区，超过 65% 的指令在 0x4a200-0x4a600 范围内执行。这就是 dispatcher。

### 5.3 第一次失败：Handler table 不在预期位置

🧑‍🔬 定位到 dispatcher 后，笔者的下一个 **假设** 是：handler table 应该是 `.rodata` 段中一个连续的函数指针数组，IDA 应该能识别为 jump table。

**实验**：在 IDA 中检查 0x4a200 附近引用的 `.rodata` 地址。

**结果**：✗ **失败**。dispatcher 确实从 `.rodata` 读取数据，但读取的不是函数指针数组，而是一组 32-bit offset。这些 offset 经过一个位运算后才与 handler base 相加得到 handler 地址：

```c
// 反编译后的 handler 地址计算（简化）
uint32_t raw_offset = rodata_table[idx];
uint32_t decoded_offset = ((raw_offset >> 3) ^ (raw_offset << 29)) & 0xFFFFF;
void *handler = (void*)(handler_base + decoded_offset * 4);
```

🧑‍🔬 offset 编码意味着不能直接从 `.rodata` 提取 handler 地址。需要知道编码方案才能解码。但编码方案本身是从反编译 dispatcher 得到的，所以这个方向虽然多了一步，还是可行的。

笔者用 Python 脚本解码了 `.rodata` 中的 offset table：

```python
# 解码 handler offset table
import struct

with open('libsign.so', 'rb') as f:
    f.seek(rodata_table_offset)
    raw_offsets = struct.unpack('<64I', f.read(256))

handler_base = 0x4a800  # 从 IDA 确认
handlers = []
for raw in raw_offsets:
    decoded = ((raw >> 3) ^ (raw << 29)) & 0xFFFFF
    addr = handler_base + decoded * 4
    if 0x49E00 <= addr <= 0x51200:  # 函数范围内
        handlers.append(addr)

print(f"Valid handlers: {len(handlers)}")
# Output: Valid handlers: 48
```

🔬 48 个 handler。对于一个二代 VMP 来说，这个数量合理——比 ARM64 原始指令集少很多，说明 VM 指令集做了语义压缩。

### 5.4 Handler 语义分类

> 这是整个分析中最耗时的部分。48 个 handler，每个需要理解它对 VM 状态的影响。笔者用了三种方法组合推进。

🧑‍🔬 **方法一：手动反编译**

对每个 handler 地址，用 IDA 反编译并分析其行为。前 10 个 handler 花了约 4 小时，因为需要理解 vm_ctx 结构体的布局。

🔬 在分析 handler_0（地址 0x4a810）时，笔者发现 vm_ctx 的结构：

```c
struct vm_ctx {
    uint64_t vregs[16];      // offset 0x00: 虚拟寄存器
    uint8_t *bytecode;       // offset 0x80: 字节码指针
    uint32_t pc;             // offset 0x88: 虚拟 PC
    uint32_t decode_key;     // offset 0x8C: 解码密钥
    uint8_t *vmem;           // offset 0x90: 虚拟内存
    uint32_t vmem_size;      // offset 0x98: 虚拟内存大小
    uint32_t flags;          // offset 0x9C: 标志寄存器
    uint32_t running;        // offset 0xA0: 运行状态
    // ...
};
```

这个结构是通过观察多个 handler 的内存访问模式逐步还原的，不是一次性得到的。

🤖 **方法二：AI 辅助分类**

把 handler 的 IDA 伪代码交给 LLM，要求按功能分类。AI 在识别简单算术和寄存器操作时表现不错，但在识别编码后的内存访问和控制流 handler 时经常出错。

| AI 分类结果 | 正确 | 错误 | 不确定 |
|------------|------|------|--------|
| 寄存器操作（mov/xchg） | 6/6 | 0 | 0 |
| 算术运算（add/sub/mul） | 5/7 | 2 | 0 |
| 位运算（and/or/xor/shift） | 4/5 | 0 | 1 |
| 内存操作（load/store） | 3/8 | 3 | 2 |
| 控制流（jmp/cmp/branch） | 2/6 | 2 | 2 |
| 特殊操作（call/ret/env） | 2/4 | 1 | 1 |
| **合计** | **22/36** | **8** | **6** |

🧑‍🔬 AI 的 61% 正确率意味着不能直接信任其输出，但作为初筛工具节省了大量时间。笔者对 AI 标记为"正确"的结果逐个验证，对"错误"和"不确定"的手动重新分析。

**方法三：动态验证**

🔬 对于手动分析和 AI 辅助都无法确定的 handler，笔者设计了动态验证实验：构造特定输入，使 dispatcher 执行目标 handler，然后检查 vm_ctx 的状态变化。

```python
# Frida hook: 在目标 handler 前后记录 vm_ctx 状态
var handler_addr = ptr('0x4aXXX');
Interceptor.attach(handler_addr, {
    onEnter: function(args) {
        this.ctx_before = read_vm_ctx(args[0]);
    },
    onLeave: function(retval) {
        var ctx_after = read_vm_ctx(/* ... */);
        var diff = diff_ctx(this.ctx_before, ctx_after);
        send({handler: handler_addr, diff: diff});
    }
});
```

通过观察 handler 前后 `vregs`、`flags`、`pc` 和 `vmem` 的变化，可以推断 handler 的语义。例如：

```text
handler_12 (0x4ac40):
  before: vregs[3] = 0x12345678, vregs[7] = 0x0000000a
  after:  vregs[3] = 0x1234567a, flags = 0 (no overflow)
  → 语义: vregs[dst] = vregs[src1] + vregs[src2] (ADD)
```

最终分类结果：

| 类别 | Handler 数量 | 示例语义 |
|------|-------------|---------|
| 寄存器操作 | 8 | MOV, XCHG, PUSH, POP |
| 算术运算 | 7 | ADD, SUB, MUL, DIV, MOD |
| 位运算 | 6 | AND, OR, XOR, SHL, SHR, ROL |
| 内存操作 | 9 | LOAD8/16/32/64, STORE8/16/32/64, LEA |
| 控制流 | 8 | JMP, JE, JNE, JL, JG, CALL, RET, HALT |
| 特殊操作 | 5 | NOP, CPUID(环境检查), NATIVE_CALL, DECODE_KEY_UPDATE, FLAG_SET |
| **未识别** | **5** | 三个疑似反调试，两个疑似冗余 |
| **合计** | **48** | — |

🧑‍🔬 43/48 个 handler 的语义被确认。剩余 5 个中，3 个在正常执行流中从未被触发（笔者怀疑是反调试路径），2 个与已识别 handler 的行为高度相似（可能是冗余变体）。

### 5.5 第二次策略转向：从"完整恢复"到"边界突破"

> 到这一步，笔者已经理解了 VM 的指令集，理论上可以把整个 bytecode stream 反汇编为 VM 汇编语言，再进一步恢复原始算法。但笔者做了一个现实判断：这条路还需要至少 20 小时的 bytecode 分析工作。有没有更快的路？

🧑‍🔬 答案是：**看 I/O 边界**。

签名函数的本质是：输入一段消息，输出一个签名值。无论 VM 内部多复杂，JNI 层面的接口是明确的：

```java
// Java 层
public static native String generateSign(String message, long timestamp);
```

**假设**：在 JNI 边界做 hook，可以直接获取输入和输出，绕过 VM 内部复杂性。

🔬 **实验**：用 Frida hook JNI 层。

```javascript
// Hook JNI GetStringUTFChars / NewStringUTF
var GetStringUTFChars = /* JNIEnv 函数指针 */;
var NewStringUTF = /* JNIEnv 函数指针 */;

Interceptor.attach(GetStringUTFChars, {
    onLeave: function(retval) {
        console.log('[INPUT] ' + retval.readUtf8String());
    }
});

Interceptor.attach(NewStringUTF, {
    onEnter: function(args) {
        console.log('[OUTPUT] ' + args[1].readUtf8String());
    }
});
```

**结果**：✓ 成功捕获输入和输出。

```text
[INPUT] message=hello&timestamp=1720612345
[OUTPUT] 7f3a2b91e4c8d5f6a0b3e7c9******01
```

输出是 32 字节 hex（64 字符），与 HMAC-SHA256 或 MD5 的输出长度一致。但仅凭 I/O 不能判断具体算法。

### 5.6 unidbg 黑盒调用：绕过 VMP 的工程路径

🧑‍🔬 笔者在确认 I/O 边界后，决定尝试 unidbg 黑盒调用。如果 unidbg 能正确执行被 VMP 保护的 `sign()` 函数，那么对于"复现签名"这个工程目标，VMP 内部的复杂性就完全不重要了。

🔬 第一次尝试：

```java
// unidbg 调用（简化）
public class SignCaller extends AbstractJni {
    private final AndroidEmulator emulator;
    private final VM vm;
    private final Module module;
    
    public SignCaller() {
        emulator = AndroidEmulatorBuilder.for64Bit().build();
        Memory memory = emulator.getMemory();
        memory.setLibraryResolver(new AndroidResolver(23));
        vm = emulator.createDalvikVM();
        vm.setJni(this);
        DalvikModule dm = vm.loadLibrary(new File("libsign.so"), false);
        module = dm.getModule();
        dm.callJNI_OnLoad(emulator);
    }
    
    public String sign(String message, long timestamp) {
        // 调用 generateSign
        DvmObject<?> result = vm.callStaticJniMethod(
            emulator, "com/xxx/sign/Sign",
            "generateSign(Ljava/lang/String;J)Ljava/lang/String;",
            vm.addLocalObject(new StringObject(vm, message)),
            timestamp
        );
        return result.getValue().toString();
    }
}
```

**结果**：✗ 崩溃。

```text
com.github.unidbg.arm.backend.BackendException: 
  [Unicorn] Invalid memory read (UC_ERR_READ_UNMAPPED)
  at 0x4b234: ldr x0, [x19, #0x10]
```

🧑‍🔬 崩溃原因是 `x19` 指向一个未初始化的对象。追踪发现这是 `sign()` 内部调用的另一个 JNI 方法 `getAppKey()` 的返回值。VM 内部通过 `NATIVE_CALL` handler 调用了这个方法，但 unidbg 没有模拟它。

**解决**：重写 `callObjectMethod` 补齐 JNI 环境。

```java
@Override
public DvmObject<?> callObjectMethod(BaseVM vm, DvmObject<?> dvmObject, 
                                      String signature, VarArg varArg) {
    if (signature.equals("com/xxx/sign/Config->getAppKey()Ljava/lang/String;")) {
        return new StringObject(vm, "ak_******_redacted");
    }
    return super.callObjectMethod(vm, dvmObject, signature, varArg);
}
```

🔬 第二次尝试：

```text
unidbg output: 7f3a2b91e4c8d5f6a0b3e7c9******01
真机 output:   7f3a2b91e4c8d5f6a0b3e7c9******01
Match: ✓
```

✓ **验证通过**。unidbg 在不理解 VM 内部语义的情况下，正确执行了被 VMP 保护的签名函数。

🧑‍🔬 这个结果验证了一个重要判断：**对于 Android Native VMP 保护的签名函数，unidbg 黑盒调用是一条成本极低的绕过路径**。笔者花了 22 小时进行 VM 语义恢复，而 unidbg 路径从开始到成功只用了 3 小时（含排查 JNI 环境问题）。

### 5.7 但 unidbg 不是万能的

🧑‍🔬 在对三代 VMP 样本（§4.3 的 `libcore.so`）使用 unidbg 时，笔者遇到了反模拟检测（§4.3 中描述的 `/proc/self/maps` 检查）。即使绕过了这个检测，还遇到了另一个问题：

🔬 `libcore.so` 的签名函数在内部读取了 `android.os.Build` 的多个字段，并且签名结果与设备信息绑定。在 unidbg 中模拟这些字段需要大量的 JNI 环境补齐工作，而且缺少任何一个字段都可能导致签名结果不正确。

| 问题 | 一代/二代 VMP | 三代 VMP |
|------|-------------|---------|
| JNI 环境完整性 | 通常只需要几个关键方法 | 可能需要几十个 JNI 回调 |
| 反模拟检测 | 无或容易绕过 | 静默错误，难以发现 |
| 设备信息依赖 | 较少 | 签名可能绑定设备 |
| 网络依赖 | 通常无 | 可能内嵌远程校验 |

**结论**：unidbg 黑盒调用的有效性取决于目标函数对外部环境的依赖程度。环境依赖越多，补齐成本越高，unidbg 的优势越小。

---

## 六、VMP vs OLLVM：什么时候该用哪个

> 笔者在分析某出行 App 的 `libnetwork.so` 时，发现同一个 SO 里同时用了两种保护：外层的参数校验函数被 OLLVM 混淆了控制流，内层的签名核心被 VMP 保护。两层保护的分析体验完全不同——OLLVM 那层用 angr 跑了三个小时穿透了，但到了 VMP 层 angr 直接 OOM。这次经历让笔者开始认真思考两者的差异，以及在什么场景下该用哪个。

### 6.1 实际分析中的体感差异

🧑‍🔬 先说结论表，再用后面的段落解释每一条为什么这么写：

| 维度 | OLLVM | VMP | 组合使用 |
|------|-------|-----|---------|
| **保护目标** | 控制流不可读 | 指令语义不可读 | 控制流 + 语义双重不可读 |
| **IDA F5 效果** | 能反编译，CFG 混乱 | 能反编译，输出是 dispatcher | CFG 混乱 + dispatcher |
| **原始指令** | 存在（被重排） | 不存在（被替换） | 外层重排 + 内层替换 |
| **angr/Triton** | 可能穿透 | 通常失效 | 失效 |
| **Frida hook** | 可以 hook 原始函数 | 可以 hook dispatcher 入口 | 需要两层 hook |
| **unidbg 黑盒** | 直接有效 | 通常有效（需补齐环境） | 通常有效 |
| **性能开销** | 低（编译器优化后 5-20%） | 高（解释执行 3-50x） | 高 |
| **适用范围** | 可保护整个 SO | 只适合保护关键路径 | 全量 OLLVM + 关键路径 VMP |
| **分析者首选攻击** | symbolic execution | I/O 边界 + 黑盒调用 | I/O 边界 + 黑盒调用 |

### 6.2 每条判断背后的实验

🧑‍🔬 上面这张表不是从文档里抄的。每一条都有笔者踩坑的来源：

**"angr 可能穿透 OLLVM"**：笔者在分析某短视频 App 的 `libsecurity.so` 时，函数 `calc_token` 被 OLLVM -fla（控制流平坦化）保护。IDA 反编译后是一个巨型 switch-case，但原始 ARM 指令仍然存在。angr 配合手写的 state pruning，跑了约 2.5 小时穿透了控制流，恢复了可读的 C 伪代码。D810 插件可以进一步自动化这个过程。

**"angr 通常无法穿透 VMP"**：同一个 App 里，`sign_request` 函数被 VMP 保护。angr 面对的是 dispatcher 的 while 循环和间接跳转。笔者尝试了三种 exploration strategy（DFS、BFS、Veritesting），每次都在 15-20 分钟后因为状态空间爆炸而 OOM。根本原因是 dispatcher 的每次迭代都产生新的符号状态分支，而循环迭代次数取决于 bytecode 长度（本例约 3000 条虚拟指令），angr 需要探索 `3000 * handler_count` 量级的状态空间。

**"VMP 性能开销 3-50x"**：笔者用 Frida 在三个不同样本上对比了相同操作（SHA256 + HMAC）在保护和未保护版本下的执行时间：

```text
样本 A（一代 VMP）: 0.8ms → 2.4ms  (3x)
样本 B（二代 VMP）: 1.1ms → 18ms   (16x)
样本 C（三代 VMP）: 0.9ms → 47ms   (52x)
```

🔬 3x 到 52x 的差异主要来自 handler 粒度和编码复杂度。一代 VMP 的 handler 粒度较粗（一个 handler 可能对应多条原始指令），三代 VMP 的 handler 粒度细且包含大量状态更新开销。

**"unidbg 黑盒调用不受 OLLVM+VMP 组合影响"**：笔者在上述出行 App 的 `libnetwork.so` 上直接用 unidbg 调用签名函数，完全跳过了两层保护的分析。补齐 JNI 环境花了约 2 小时，之后输出与真机一致。unidbg 不做反编译，它只是执行 ARM 指令——不管这些指令是 dispatcher 的还是 handler 的，CPU 模拟器都照样执行。

### 6.3 选型建议（防御者视角）

🧑‍🔬 基于以上分析经验，笔者的建议：

| 场景 | 推荐方案 | 原因 | 笔者的经验佐证 |
|------|---------|------|---------------|
| 全量 SO 基线保护 | OLLVM（-fla + -bcf + -sub） | 性能可接受，全量覆盖 | 分析过的 8 个 OLLVM SO 中，6 个性能影响 < 15% |
| 签名/加密核心函数 | VMP | 阻止语义恢复 | 阻止了笔者在 §5.4 中的 angr 尝试 |
| 签名 + 想阻止 unidbg | VMP + 服务端绑定 | VMP 单独不能阻止黑盒调用 | §5.6 中 unidbg 3 小时就绕过了 VMP |
| 反作弊检测逻辑 | OLLVM + 内联检测 | VMP 性能开销不适合高频检测 | 见上面样本 C 的 52x 开销数据 |
| 白盒密钥存储 | VMP + 编码状态 + 硬件绑定 | 参考 Chrome VMP 文章的数据编码方案 | 笔者未在 Android 样本中遇到白盒级 VMP |

---

## 七、VMP 的工程边界与常见误区

> 这一节是笔者见过最多误解的领域。"VMP 了就安全了"这句话在甲方安全评审中出现的频率之高，令人担忧。以下每个误区都有对应的绕过路径。

### 7.1 误区一："VMP 了就安全了"

🧑‍🔬 笔者在一次安全评审中被甲方问过一个问题："我们的签名函数已经做了 VMP 保护，还需要服务端验证吗？"笔者的回答是："需要。"然后花了一个下午用以下四条路径证明了这一点——没有任何一条需要理解 VM 内部的一条虚拟指令。

**四条不需要理解 VM 内部语义的攻击路径**：

| 路径 | 方法 | VMP 是否阻止 | 笔者实际验证情况 |
|------|------|-------------|-----------------|
| **I/O 边界** | Hook JNI 层，直接获取输入输出 | 否 | hook `GetStringUTFChars` + `NewStringUTF`，20 分钟拿到完整 I/O |
| **黑盒调用** | unidbg/Unicorn 直接执行 SO | 否 | §5.6 中 3 小时完成，输出与真机一致 |
| **Timing pattern** | 侧信道观测执行时间 | 否 | 不同输入长度的执行时间呈线性关系，暗示内部有逐字节处理循环 |
| **重放/复用** | 捕获签名结果直接使用 | 否 | 抓到的签名在 5 分钟内可以直接重放给服务端并被接受 |

🔬 第四条路径让笔者意识到真正的安全问题不在 VMP 里。笔者在某个样本上抓取了一个签名结果，5 分钟后原封不动地发给服务端——请求被正常接受了。这意味着即使攻击者无法生成新签名，只要能抓到一个有效签名就能重放。VMP 保护了"如何生成签名"，但没有保护"一个签名能用多久"。

笔者接着做了一个更系统的实验。对某个 VMP 保护的签名函数，不做任何 VM 分析，只 hook JNI 层的输入输出，然后用 50 组不同输入观察签名算法的外部特征：

```text
输入长度 → 输出长度: 固定 64 字符 (32 bytes hex)
输入相同 → 输出相同: ✓ (确定性)
前缀相同 → 输出完全不同: ✓ (avalanche effect)
时间参数影响: ✓ (timestamp 参与签名)
空输入: 返回 64 字符 hex (不崩溃)
```

🧑‍🔬 这些观察花了不到一小时，足以缩小算法候选范围到 HMAC-SHA256 或 HMAC-MD5，然后用 unidbg 黑盒调用直接复现。VMP 内部那两万行伪代码完全没有增加这条攻击路径的成本。那个下午之后，甲方加了 60 秒 timestamp 窗口校验和 nonce 绑定。

### 7.2 误区二："分析者一定要理解 VM 指令集"

🧑‍🔬 笔者在分析 §五 的样本时，花了 22 小时做 VM 语义恢复。做完之后笔者知道了这个 VMP 有 48 个 handler、opcode 用异或 + 旋转编码、签名算法底层是 HMAC-SHA256。然后笔者去隔壁工位看了一眼同事的进度——他在笔者开始语义恢复的第二天就用 unidbg 跑通了签名调用，输出与真机一致，正在写测试用例了。

那一刻笔者清楚地意识到：22 小时的语义恢复对于理解 VMP 的保护机制非常有价值（也是本文能写出来的原因），但对于"复现签名"这个工程目标完全多余。

🔬 后来笔者在不同项目中统计了分析者的实际目标分布：

| 目标 | 是否需要理解 VM | 推荐路径 | 笔者见过的频率 |
|------|---------------|---------|--------------|
| 复现函数输出 | 否 | unidbg 黑盒调用 | ~70% 的项目 |
| 理解算法逻辑 | 是 | VM 语义恢复 + bytecode 反汇编 | ~15% |
| 修改函数行为 | 是 | handler patch 或 bytecode patch | ~10% |
| 提取内嵌密钥 | 视情况 | 内存扫描 + VM 状态观测，或差分分析 | ~5% |

大多数情况下，分析者的目标是第一种——而这恰恰是 VMP 防护效果最弱的目标。VMP 的投资回报在后三种目标上才真正体现。

### 7.3 误区三："性能不重要，安全第一"

🧑‍🔬 笔者见过的一个真实案例：某 App 对整个网络请求签名流程做了 VMP 保护，包括参数排序、URL 编码、拼接和最终哈希。结果签名计算从 2ms 增加到 120ms。团队一开始没注意到，因为开发环境网络快、数据少。上线后用户投诉接口慢，排查了一周才定位到是 VMP 的开销——弱网环境下，用户每次操作的感知延迟增加了 100ms+，而且这个延迟是 CPU-bound 的，跟网络优化毫无关系。最终团队不得不回退到只保护核心哈希计算部分（约 80 行代码），参数准备逻辑改回 OLLVM 保护。

🔬 笔者在自己的分析环境中也量化过这个问题。对三个不同样本，用 Frida 计时同一操作在 VMP 保护前后的开销差异：

| 保护范围 | 笔者实测性能影响 | 用户感知 | 笔者的判断 |
|----------|----------------|---------|-----------|
| 单个 SHA256（~80 行） | 0.8ms → 2.4ms (3x) | 不可感知 | 合理，值得做 |
| 完整签名流程（~600 行） | 1.1ms → 18ms (16x) | 列表页每个 item 都调签名时可感知 | 需要评估调用频率 |
| 带环境检测的签名（~1200 行） | 0.9ms → 47ms (52x) | 明确可感知 | 环境检测不该放在 VMP 里 |

最后一个样本的 52x 开销让笔者印象深刻。拆开看，47ms 中约 30ms 花在了 VMP handler 内嵌的环境检查上（读 `/proc/self/maps`、检查 `ro.debuggable` 等），只有 17ms 是真正的签名计算。把环境检查挪到 VMP 外面单独做，签名部分的 VMP 开销可以降到 17ms (19x)，仍然不低，但至少在可接受范围内。

### 7.4 误区四："反调试就够了"

🧑‍🔬 笔者曾花了一整天绕过某个 App 的反调试检测。这个 App 在 `JNI_OnLoad` 阶段开了一个线程，每 200ms 循环检测 `/proc/self/status` 中的 `TracerPid`、扫描 `/proc/self/maps` 中的 `frida` 字符串、检测 `27042` 端口是否被监听。检测到就直接 `kill(getpid(), SIGKILL)`。

笔者的绕过方案：hook `open()` 对 `/proc/self/status` 和 `/proc/self/maps` 的读取，返回伪造内容；hook `connect()` 跳过端口扫描。整个绕过脚本不到 50 行 JavaScript。绕过之后，Frida 正常工作，整个 SO 的所有函数都可以 hook。

🔬 然后笔者用 unidbg 跑了同一个 SO——根本不需要绕过反调试，因为 unidbg 不是调试器。它不触发 ptrace、不监听端口、不在 maps 里出现（除非像 §4.3 那样做了 `/proc/self/maps` 检测，但那是 VMP 内部的检测，不是常规反调试）。

| 措施 | 解决什么 | 笔者实际绕过成本 |
|------|---------|-----------------|
| 反调试（ptrace/maps/port） | 增加 Frida 挂载成本 | 50 行 JS + 2 小时调试 |
| VMP | 增加语义恢复成本 | 需要 20+ 小时做 handler 分析 |
| 两者组合 | 同时增加动态和静态成本 | unidbg 黑盒调用完全不受影响 |

反调试对 Frida 有效（但绕过成本可控），对 unidbg 无效（因为 unidbg 不是调试器），对静态分析无效（IDA 不需要运行目标进程）。将安全预算集中投入反调试而忽视协议层绑定，相当于给前门加了三把锁但后门开着。

### 7.5 真正有效的防御组合

🧑‍🔬 基于以上分析，笔者认为 VMP 必须与其他防御措施组合才能发挥价值：

```text
VMP (保护指令语义)
  + 服务端签名验证 (防止离线复用)
  + 时间戳/nonce 绑定 (防止重放)
  + 设备信息绑定 (防止跨设备使用)
  + 请求频率限制 (防止批量调用)
  + 定期更新签名方案 (防止长期有效)
= 有意义的安全边界
```

单独的 VMP 只是这条链上的一环。链的强度取决于最弱的那一环。

---

## 八、防御建议与改进提案

> 这一节面向使用或考虑使用 VMP 保护的开发团队。笔者从攻击者视角反推防御者应该优先加固的位置。

### 8.1 改进提案表

| 优先级 | 当前状态 | 建议改进 | 效果 | 成本 | 可行性 |
|--------|---------|---------|------|------|--------|
| **P0** | 签名结果可无限重放 | 服务端强制 timestamp/nonce 校验，窗口 < 60s | 重放攻击失效 | 低 | ★★★★★ |
| **P0** | unidbg 可直接调用签名函数 | 签名绑定设备证明（Play Integrity / 自研 attestation） | 黑盒调用需要真实设备 | 中 | ★★★★ |
| **P1** | handler table 在 SO 生命周期内固定 | 编译期随机化 handler table 和 opcode 映射 | 每个版本的分析结果不可复用 | 中 | ★★★★ |
| **P1** | VM 内部无环境校验 | 在关键 handler 中嵌入环境检查（非阻断式） | unidbg/Unicorn 产出错误结果 | 低 | ★★★★★ |
| **P2** | 同一 opcode 始终映射到同一 handler | 引入多态 dispatch（§4.3 三代特征） | Trace 分析噪声增加 | 高 | ★★★ |
| **P2** | 签名算法长期不变 | 服务端支持签名版本协商 + OTA 更新字节码 | 旧分析结果定期失效 | 高 | ★★★ |
| **P3** | VMP 字节码随 APK 固定分发 | 关键字节码片段从服务端按需下发 | 离线分析缺少完整字节码 | 极高 | ★★ |
| **P3** | VM 只在客户端执行 | 关键计算拆分到服务端（server-assisted signing） | 客户端无法独立完成签名 | 极高 | ★★ |

### 8.2 AI 时代的 VMP 有效性

🧑‍🔬 笔者注意到一个趋势：AI 工具在辅助逆向分析中的效果正在快速提升。这对 VMP 的安全边界有直接影响：

| 传统防御手段 | AI 辅助下的失效程度 | 说明 |
|-------------|-------------------|------|
| OLLVM 控制流混淆 | 高（~70-90% 可自动化去混淆） | 模式识别和 symbolic execution 是 AI 强项 |
| 字符串加密 | 高（~80% 可自动化解密） | 解密函数的识别已经很成熟 |
| VMP handler 语义分类 | 中（~60% 自动化，见 §5.4） | AI 对简单 handler 有效，复杂 handler 仍需人工 |
| VMP bytecode 反汇编 | 低（<30% 自动化） | 编码方案恢复仍高度依赖人工判断 |
| 服务端签名验证 | 无影响 | 不在客户端，AI 无法触及 |
| 设备绑定 + attestation | 无影响 | 需要真实硬件 |

🤖 结论：**AI 加速了客户端保护的失效曲线，使得服务端防御的相对价值更高**。这不是说 VMP 不该做，而是说 VMP 的预期有效期在缩短——从"可能挡住分析者几个月"变成"可能挡住几周"。防御者的资源应该优先投入服务端绑定和设备证明。

### 8.3 笔者不建议做的事情

基于上述分析的双重性，有几件事笔者认为应当明确避免：

- **不把 VMP 分析结果用于批量伪造请求或绕过业务限制**：签名复现能力是安全研究的副产品，不是业务攻击的工具
- **不公开完整的 opcode 映射表或 handler 语义表**：这会将单次研究的成本转嫁给所有使用同一加固方案的 App
- **不把 unidbg 调用封装成"签名服务"对外提供**：这本质上是一种欺诈基础设施
- **不低估 VMP 的工程价值**：它确实提高了分析门槛，这对于保护窗口期内的商业利益有现实意义

---

## 九、结论

回看整个分析过程，笔者的核心收获可以浓缩为以下几点：

1. 🧑‍🔬 **VMP 的三代演进反映了攻防双方的持续博弈**：从固定 opcode table 到状态依赖 dispatch，每一代的复杂性提升都是对上一代已知攻击的回应。但每一代也引入了新的工程复杂度和兼容性风险。

2. 🔬 **Dispatcher 是 VMP 的"心脏"，也是分析者的首要目标**：通过 indirect branch 密度分析和执行热区定位，dispatcher 通常可以在 1-2 小时内被识别。识别 dispatcher 不等于破解 VMP，但它是所有后续分析的起点。

3. 🧑‍🔬 **实战中最有效的突破路径往往不在 VM 内部**：I/O 边界观测 + unidbg 黑盒调用的组合，在工程上击败了 VMP 对签名函数的保护。笔者花 22 小时做语义恢复得到的"理解"，和花 3 小时做黑盒调用得到的"结果"，在签名复现这个目标上等价。

4. 🤖 **AI 正在加速 VMP 分析的自动化**：handler 语义分类的 61% 自动化率虽然不够用，但已经显著缩短了人工分析时间。随着 AI 能力提升，纯客户端保护的有效期将持续缩短。

5. 🧑‍🔬 **VMP 的真正价值在于组合防御中的"成本放大"角色**：单独部署的 VMP 无法阻止 unidbg、I/O 边界或侧信道攻击。但当它与服务端绑定、设备证明、请求频率限制和定期更新组合时，可以将攻击者从"一次分析，永久有效"推入"每个版本/设备/会话都需要适配"的持续成本模式。

6. 🔬 **从攻击者经济学看，VMP 的最佳投资位置是"少量高价值、低频调用的关键路径"**：全量 VMP 不是更高安全性的保证，而是性能和稳定性的灾难。

最后一个设计问题留给读者思考：

> **如果攻击者可以正确调用你的签名函数但不理解它的内部逻辑，你的系统应该在哪里、用什么方式区分"合法调用"和"黑盒调用"？** 这个问题的答案不在 VMP 里，而在签名函数之外的绑定、证明和验证链路中。VMP 保护的是"理解"，但防御的目标是"不可滥用"——两者之间的距离，正是服务端安全设计需要填补的空间。

---

## 附：工具与版本

| 工具 | 版本 | 用途 | 阶段 |
|------|------|------|------|
| ![IDA](https://img.shields.io/badge/IDA_Pro-9.0_SP1-4B0082?style=flat) | 9.0 SP1 | 静态反编译、handler 分析 | 全程 |
| ![Frida](https://img.shields.io/badge/Frida-16.5.9-FF6633?style=flat) | 16.5.2→16.5.9 | 动态 hook、stalker trace | Phase 2-4 |
| ![unidbg](https://img.shields.io/badge/unidbg-0.9.7-007ACC?style=flat) | 0.9.7 | 黑盒调用、JNI 模拟 | Phase 4 |
| ![Unicorn](https://img.shields.io/badge/Unicorn-2.0.1-333333?style=flat) | 2.0.1 | CPU 模拟、指令 trace | Phase 2 |
| ![radare2](https://img.shields.io/badge/radare2-5.9.6-yellow?style=flat) | 5.9.6 | 交叉引用、统计分析 | Phase 1 |
| ![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat&logo=python&logoColor=white) | 3.11 | 脚本、数据分析 | 全程 |

---

## 借鉴来源表

| 来源 | 借鉴内容 | 使用方式 |
|------|---------|---------|
| [看雪论坛 VMP 分析系列](https://bbs.kanxue.com/) | VM dispatcher 识别方法、handler 分类思路 | 方法论参考，非直接复用代码 |
| [unicorn-engine/unicorn](https://github.com/unicorn-engine/unicorn) | CPU 模拟框架 | 工具使用 |
| [zhkl0228/unidbg](https://github.com/zhkl0228/unidbg) | Android Native 模拟框架 | 工具使用，JNI 环境补齐 |
| 本博客 [Chrome VMP 文章](/blogs/posts/chrome-vmp-protection-vm-dispatch-whitebox/) | VMP 安全属性矩阵、边界建模方法 | 框架借鉴，场景不同 |
| 本博客 [APK 加固文章](/blogs/posts/android-apk-hardening-packer-vmp-rasp-mainstream/) | VMP 在加固体系中的定位 | 背景参考 |
| [NDSS 2018 DroidUnpack](https://www.ndss-symposium.org/) | Android 加壳/脱壳研究 | 学术背景 |

## 独立贡献表

| 工作 | 说明 |
|------|------|
| 三代 VMP 分类框架 | 基于 dispatcher 状态依赖程度的分代标准 |
| 二代 VMP 完整分析流程 | 从 dispatcher 到 handler 语义的实战路径 |
| anti-emulation 静默错误模式发现 | 三代 VMP 中 `/proc/self/maps` 检测导致的静默错误输出 |
| VMP vs unidbg 黑盒调用的成本对比 | 22h 语义恢复 vs 3h 黑盒调用的工程经济学 |
| AI 辅助 handler 分类的有效性评估 | 61% 正确率的量化数据 |
| 防御改进提案 P0-P3 | 从攻击者视角反推的优先级排序 |
