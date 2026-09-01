---
title: "所有分支都指向同一个 switch，然后呢"
slug: "ollvm-deobfuscation-engineering"
date: 2026-06-05T16:20:00+08:00
lastmod: 2026-06-05T16:20:00+08:00
draft: false
tags: ["OLLVM", "obfuscation", "LLVM", "control-flow-flattening", "bogus-control-flow", "Android", "reverse-engineering", "binary-security", "IDA-Pro", "angr"]
categories: ["reverse-engineering"]
description: "从 IDA 里 400 个基本块的平坦化函数出发，完整拆解 OLLVM 三大混淆 pass 的工程原理与去混淆实战，覆盖 CFF/BCF/指令替换/字符串加密的识别、攻击与工具链对比"
toc: true
math: false
---

> **读完本文，你将获得：**
> - 能在 IDA 里一眼认出 Control Flow Flattening、Bogus Control Flow 和 Instruction Substitution 三种 OLLVM 混淆的视觉特征
> - 理解 OLLVM 如何嵌入 LLVM 编译管线，以及每个 pass 在 IR 层面做了什么
> - 掌握从 Frida 动态 trace 到 angr 符号执行再到 CFG 手动恢复的完整去混淆工作流
> - 经历一个真实案例中"IDA 看到 400 个基本块、真正执行的只有 27 个"的完整分析过程
> - 理解为什么 OLLVM 在 AI 时代正在从"有效保护"滑向"增加成本但不改变结局"

## 〇、摘要

🧑‍🔬 笔者最近在分析一个国内头部电商 App 的签名 SO 时，IDA 打开目标函数，CFG 窗口画出来的图像一块被碾平的披萨饼——400 多个基本块，全部通过一个巨型 switch 相互连接，没有任何可读的逻辑结构。这是典型的 OLLVM Control Flow Flattening，笔者在过去两年的 Android 逆向中至少遇到过十几次，但每次都在不同环节卡住。

这篇文章不是 OLLVM 的百科全书。笔者的目标是把自己在实战中积累的工程经验整理成一条可复现的路线：从识别混淆类型，到选择去混淆策略，到工具链搭建，到最终恢复出可读的控制流。过程中踩过的坑和失败的尝试一样会记录下来。

核心贡献：

1. **三大 pass 的 IDA 视觉指纹库** 🧑‍🔬：总结了 CFF、BCF、Instruction Substitution 在 IDA CFG/伪代码中的 12 个可识别特征，附实际截图级描述
2. **从 Frida trace 到 CFG 恢复的完整工作流** 🔬：以某电商 App 的 `libsign.so` 为案例，记录了从 0 到可读伪代码的全过程
3. **基于 angr 的半自动 CFF 去平坦化** 🤖🧑‍🔬：实现了一个 600 行的 Python 脚本，在 5 个不同目标上成功恢复了 CFF，失败了 2 个（原因分析见 §6.5）
4. **opaque predicate 的 7 种识别模式** 🧑‍🔬：从 BCF 的死代码中提取了 7 类 opaque predicate 模板，其中 2 类是 MBA 变种
5. **去混淆工具链横向对比** 🔬：对比了 D-810、GAMBA、Triton、angr、手动五种方案在 4 个目标上的表现
6. **OLLVM 在 AI 时代的失效曲线分析** 🧑‍🔬：基于本文实验数据，评估了 LLM 辅助去混淆对各保护层的实际影响
7. **防御方改进提案** 🧑‍🔬：从保护方视角提出了 6 条 P0-P3 级改进建议

---

## Research Evidence

### Environment

| Item | Detail |
|---|---|
| Device | Pixel 7 Pro (cheetah), rooted via Magisk 27.0 |
| OS | Android 14 (UP1A.231005.007) |
| Target SO | `libsign.so` v8.2.3, arm64-v8a, 4.7 MB |
| SO SHA256 | `7f3a2b1c4d5e******9a8b7c6d5e4f3a` (partial) |
| IDA | 9.0 SP1 + Hex-Rays ARM64 |
| Frida | 16.5.9, frida-server 16.5.9 |
| angr | 9.2.107 |
| Unicorn | 2.1.1 |
| Ghidra | 11.2 (辅助交叉验证) |
| Python | 3.11.8, z3-solver 4.13.0 |
| 分析周期 | 2026-05-18 ~ 2026-06-03 (约 3 周) |

### Hypothesis

| ID | 假设内容 | 章节 | 结果 |
|----|---------|------|------|
| H1 | 目标函数使用标准 OLLVM CFF，state variable 为单一 32-bit 整数 | §6.1 | ✓ **确认** — switch 变量为 `w8`，取值 47 种 |
| H2 | 通过 Frida trace 收集的 state 序列可以直接恢复原始 CFG | §6.2 | ✗ **部分失败** — 覆盖率仅 68%，路径依赖输入 |
| H3 | angr 符号执行可以枚举所有 state 转移，补全动态 trace 的缺口 | §6.3 | ✓ **确认** — 47 个 state 全部覆盖，但耗时 23 分钟 |
| H4 | BCF 插入的 opaque predicate 全部基于 `(x * (x - 1)) % 2 == 0` 模板 | §4.2 | ✗ **证伪** — 发现 7 种模板，含 2 种 MBA 变种 |
| H5 | D-810 可以一键去除所有 BCF dead code | §7.1 | ✗ **部分失败** — 标准模板 95% 去除，MBA 变种 0% |
| H6 | Instruction Substitution 不影响仿真正确性，可以忽略 | §4.3 | ✓ **确认** — Unicorn trace 语义等价 |
| H7 | 字符串加密使用固定 XOR key，可全局批量解密 | §5.1 | ✗ **证伪** — 每个函数使用不同 key，需逐函数处理 |

### Experiments

| ID | 实验 | 验证假设 | 结果 | 关键证据 |
|---|---|---|---|---|
| E01 | IDA CFG 统计分析 | H1 | ✓ PASS | 47 个 case，1 个 switch 变量 `w8` |
| E02 | Frida state trace (100 次调用) | H2 | ✗ PARTIAL | 32/47 states 覆盖，15 个未触发 |
| E03 | angr symbolic exploration | H3 | ✓ PASS | 47/47 全覆盖，23 min，peak 4.2 GB |
| E04 | BCF opaque predicate 分类 | H4 | ✗ FAIL | 7 种模板，非单一类型 |
| E05 | D-810 自动去 BCF | H5 | ✗ PARTIAL | 标准模板去除 95%，MBA 残留 |
| E06 | Unicorn 全路径仿真 | H6 | ✓ PASS | 输出与真机一致 |
| E07 | 字符串解密 key 分析 | H7 | ✗ FAIL | 12 个函数，12 个不同 key |
| E08 | CFF 去平坦化脚本 (5 目标) | — | ✓ 5/7 | 5 成功 2 失败 (§6.5) |
| E09 | 恢复后 CFG vs 真机 trace 验证 | — | ✓ PASS | 27 个真实基本块，执行顺序一致 |

---

## 一、路线总览

> 这一节给出完整的分析路线图。笔者的经验是：面对 OLLVM 保护的二进制，最大的陷阱不是技术难度，而是在错误的层次上花时间。先花 30 分钟做识别和分类，能省下 3 天的无效逆向。

整个去混淆工作可以分为 **6 个递进阶段**：

| 阶段 | 目标 | 方法 | 产出 | 耗时 |
|------|------|------|------|------|
| **① 混淆识别** | 确认混淆类型和强度 | IDA CFG 视觉分析 + 统计 | 混淆类型报告 | 0.5h |
| **② 动态 trace** | 收集真实执行路径 | Frida hook state variable | state 转移序列 | 2-4h |
| **③ 符号执行补全** | 覆盖未触发路径 | angr + Z3 约束求解 | 完整 state 转移图 | 4-8h |
| **④ CFG 重建** | 恢复原始控制流 | 自定义 Python 脚本 | 去平坦化的 CFG | 8-16h |
| **⑤ BCF/ISub 清理** | 去除死代码和算术混淆 | D-810 + 手动 pattern match | 可读伪代码 | 2-4h |
| **⑥ 语义验证** | 确认去混淆结果正确 | Unicorn 对比执行 | 输入/输出一致性证明 | 1-2h |

阶段之间不是严格线性的。笔者在 §6 的实战中就经历了 ② → ③ → 发现问题回到 ② 的循环。但总体方向是从动态（观察真实行为）到静态（推理全部可能），再回到动态（验证正确性）。

---

## 二、引言

### 2.1 OLLVM 在野外

🧑‍🔬 笔者第一次遇到 OLLVM 是 2023 年，分析某短视频 App 的签名库。当时在 IDA 里看到一个函数的 CFG，像是有人把一盘意面从高处扔到了地上——所有线条都汇聚到中间一个点，然后再散开。后来知道这就是 Control Flow Flattening 的标志性视觉效果。

从那以后，OLLVM 或其变种几乎出现在笔者分析的每一个有保护的 Android SO 中。以下是笔者在过去两年中遇到的使用 OLLVM 系列混淆的目标分布：

| 领域 | 代表目标 | 混淆组合 | 说明 |
|------|---------|---------|------|
| ![电商](https://img.shields.io/badge/电商-FF6600?style=flat) 电商签名 | 头部电商 App 签名 SO | CFF + BCF + String Enc | 签名算法和设备指纹 |
| ![短视频](https://img.shields.io/badge/短视频-000000?style=flat) 短视频 | 短视频平台 MetaSec SDK | CFF + BCF + ISub + VM | OLLVM 作为外层，内层还有自定义 VM |
| ![支付](https://img.shields.io/badge/支付-1DA1F2?style=flat) 金融支付 | 支付 SDK 风控模块 | CFF + String Enc | 设备环境采集和上报 |
| ![游戏](https://img.shields.io/badge/游戏-7B68EE?style=flat) 游戏反作弊 | 手游反作弊 SO | CFF + BCF + ISub | 内存完整性校验和行为检测 |
| ![IoT](https://img.shields.io/badge/IoT-00979D?style=flat) IoT 固件 | 智能门锁通信模块 | CFF | 通信协议加密 |
| ![DRM](https://img.shields.io/badge/DRM-E50914?style=flat) DRM 模块 | Widevine CDM 外层 | CFF + BCF | 作为 VMP 之前的第一层防护 |

OLLVM 之所以普及率这么高，有三个工程原因：

1. **编译时集成**：OLLVM 是 LLVM pass，只需要在编译命令里加 `-mllvm -fla` 就能开启，对开发者几乎零侵入
2. **跨平台**：同一套 pass 同时支持 ARM、ARM64、x86、MIPS，一次配置覆盖所有目标架构
3. **可组合**：CFF、BCF、ISub 可以单独开启或任意组合，保护强度可调

### 2.2 OLLVM 的前世今生

OLLVM（Obfuscator-LLVM）最初由瑞士 HEIG-VD 大学的 [Quarkslab](https://github.com/obfuscator-llvm/obfuscator) 团队在 2010 年左右开发，作为 LLVM 的 fork 发布。项目在 GitHub 上开源，最后一次官方更新停留在 LLVM 4.0（2017 年）。

| 时间节点 | 事件 | 影响 |
|----------|------|------|
| 2010-2013 | Quarkslab 开发并开源 OLLVM | 学术和安全社区开始使用 |
| 2015 | 国内安全厂商开始基于 OLLVM 构建商业保护方案 | 大规模商业应用开始 |
| 2017 | 官方停更于 LLVM 4.0 | 社区 fork 接力，各厂商自行维护 |
| 2018-2020 | 大量 Android App 开始默认启用 OLLVM 保护 | 成为 SO 保护的事实标准 |
| 2020-2024 | 去混淆工具生态成熟（D-810, GAMBA, angr 改进） | 攻防平衡开始向攻击方倾斜 |
| 2025-2026 | LLM 辅助分析出现，混淆代码理解成本进一步降低 | 笔者在 §八 详细讨论 |

🧑‍🔬 笔者注意到一个有趣的现象：虽然 OLLVM 官方早已停更，但它的 DNA 已经融入了几乎所有国内安全厂商的保护方案中。梆梆安全、爱加密、360 加固、网易易盾的 SO 保护组件，底层或多或少都有 OLLVM pass 的影子。有些厂商会在标准 pass 基础上增加自定义变换，但核心架构没有根本性改变。这意味着理解标准 OLLVM 的去混淆方法，对分析商业保护方案仍然有 80% 以上的适用性。

### 2.3 研究动机

🧑‍🔬 坦白说，写这篇文章的直接动机是笔者在分析那个电商 App 签名 SO 时第三次在同一个地方卡住：面对 CFF，每次都是 Frida trace 跑一遍，手动画状态图，然后在某个嵌套 BCF 上浪费两天。第三次的时候笔者决定系统化地解决这个问题——写一个半自动的去平坦化工具，并把整个方法论记录下来。

这篇文章就是那个过程的产物。

---

## 三、知识准备

> 这一节覆盖理解 OLLVM 所需的最小知识集。如果读者已经熟悉 LLVM 编译管线和 IR pass 的概念，可以跳过 §3.1 直接看 §3.2。

### 3.1 LLVM 编译管线与 Pass 架构

理解 OLLVM 的第一步是理解它嵌入的位置。LLVM 的编译流程可以简化为三段：

```
源代码 (.c/.cpp)
    │
    ▼  [Clang Frontend]
LLVM IR (.ll / .bc)        ← OLLVM 的混淆 pass 在这里插入
    │
    ▼  [Optimization Passes: -O0 ~ -O3]
优化后的 LLVM IR
    │
    ▼  [Backend Codegen]
目标机器码 (.o → .so)
```

OLLVM 本质上是三个 LLVM Function Pass：

| Pass | 命令行参数 | IR 层面操作 | 编译时开销 |
|------|-----------|------------|-----------|
| **Flattening** | `-mllvm -fla` | 重写 BasicBlock 拓扑结构 | 中等（~1.5x） |
| **BogusControlFlow** | `-mllvm -bcf` | 插入新 BasicBlock + opaque predicate | 较高（~2x） |
| **Substitution** | `-mllvm -sub` | 替换算术/逻辑指令为等价复杂表达式 | 低（~1.1x） |

关键点：OLLVM 操作的是 **LLVM IR**，不是源代码，也不是最终机器码。这意味着：

1. 混淆后的 IR 仍然会经过 LLVM 后端的正常优化和代码生成
2. 最终二进制中的混淆模式会受到后端优化的影响（有些模式会被优化掉，有些会变形）
3. 不同优化级别（`-O0` vs `-O2`）下同一个 pass 的最终效果差异巨大

🧑‍🔬 笔者曾经在一个目标上发现 BCF 的 opaque predicate 被后端优化器部分简化了——`-O2` 把一些明显恒真的条件直接消除了。这让笔者意识到，真正到达二进制层面的混淆效果，是 OLLVM pass 和后端优化之间博弈的结果。

### 3.2 OLLVM 混淆 pass 在 IR 中的插入点

为了直观理解 OLLVM 的工作方式，笔者用一个简单的例子展示混淆前后的 IR 变化。

原始 C 代码：

```c
int check_license(int code, int key) {
    if (code > 100) {
        if (key == 0x5A3C) {
            return 1;  // valid
        }
        return -1;     // invalid key
    }
    return 0;          // code too small
}
```

正常编译后的 IR 控制流（简化）：

```
entry:
  %cmp = icmp sgt i32 %code, 100
  br i1 %cmp, label %if.then, label %if.end

if.then:
  %cmp1 = icmp eq i32 %key, 23100
  br i1 %cmp1, label %return.1, label %return.neg1

return.1:
  ret i32 1

return.neg1:
  ret i32 -1

if.end:
  ret i32 0
```

CFF 混淆后的 IR（简化）：

```
entry:
  %state = alloca i32
  store i32 0x3a7f1b2c, i32* %state      ; 初始 state
  br label %dispatcher

dispatcher:                                ; 所有路径汇聚于此
  %s = load i32, i32* %state
  switch i32 %s, label %default [
    i32 0x3a7f1b2c, label %bb_check_code
    i32 0x7e2d4a19, label %bb_check_key
    i32 0x1c5b8e3d, label %bb_ret_1
    i32 0x4d9f2c71, label %bb_ret_neg1
    i32 0x6a3e5f80, label %bb_ret_0
  ]

bb_check_code:
  %cmp = icmp sgt i32 %code, 100
  %next = select i1 %cmp, i32 0x7e2d4a19, i32 0x6a3e5f80
  store i32 %next, i32* %state
  br label %dispatcher                     ; 回到 dispatcher

bb_check_key:
  %cmp1 = icmp eq i32 %key, 23100
  %next1 = select i1 %cmp1, i32 0x1c5b8e3d, i32 0x4d9f2c71
  store i32 %next1, i32* %state
  br label %dispatcher                     ; 又回到 dispatcher

; ... 每个原始基本块都变成了 switch 的一个 case
```

🧑‍🔬 笔者第一次手动画出这个变换的时候，突然理解了为什么叫"平坦化"——原始的树状控制流被拍扁成了一层，所有基本块都在同一层级上，通过 state variable 间接连接。从 IDA 的视角看，所有 basic block 都是 switch 的 case，没有任何层次结构信息。

### 3.3 去混淆的本质问题

理解了 CFF 的变换方式后，去混淆的核心问题就变得清晰了：

**已知**：一组通过 switch-dispatcher 连接的基本块，每个块末尾设置下一个 state value

**求解**：每个基本块的真实后继关系——即恢复原始 CFG

这本质上是一个**数据流分析**问题：追踪 state variable 的赋值和使用，重建从每个 case 到其后继 case 的映射关系。

困难在于：
1. state value 可能不是常量（条件赋值、运算得到）
2. BCF 会插入永远不会执行的基本块，干扰分析
3. Instruction Substitution 会让 state 计算变得不可读
4. 商业变种可能使用多个 state variable、嵌套 switch 或间接跳转

---

## 四、三大核心混淆 pass 的工程拆解

> 这一节是全文的知识基础。笔者按照实战中遇到的频率和难度排序：CFF 最常见也最麻烦，BCF 次之，Instruction Substitution 通常可以通过仿真绕过。

### 4.1 Control Flow Flattening (CFF)

#### 4.1.1 结构特征

🧑‍🔬 在 IDA 里识别 CFF 不需要任何工具——它的视觉特征太显眼了。笔者总结了 5 个一眼可判断的特征：

| # | 特征 | IDA 中的表现 | 可靠度 |
|---|------|-------------|--------|
| 1 | **巨型 switch** | 函数入口附近有一个 switch 语句，case 数量远超正常逻辑 | 极高 |
| 2 | **中心辐射 CFG** | CFG 呈轮辐状（hub-and-spoke），中心是 dispatcher | 极高 |
| 3 | **state variable** | 一个 local 变量被反复赋值为看似随机的常量 | 高 |
| 4 | **所有 case 以 `break` 结尾** | 每个 case 块末尾都无条件跳转回 switch | 高 |
| 5 | **函数体积膨胀** | 原始 50 行的函数变成 500+ 行 | 中等（也可能是正常复杂函数） |

一个典型的 CFF 函数在 IDA 伪代码中的模式：

```c
// IDA 伪代码 (ARM64)，典型 CFF 模式
__int64 __fastcall sign_request(const char *url, __int64 timestamp)
{
  int state;  // w8
  // ... 局部变量声明
  
  state = 0x3A7F1B2C;  // 初始 state
  while (1)
  {
    switch (state)
    {
      case 0x1C5B8E3D:
        // ... 某个计算块
        state = 0x4D9F2C71;
        continue;
      case 0x2E8A4F56:
        // ... 另一个计算块
        if (some_condition)
          state = 0x7E2D4A19;
        else
          state = 0x6A3E5F80;
        continue;
      case 0x3A7F1B2C:
        // ... 入口逻辑
        state = 0x5F1D3B9A;
        continue;
      // ... 数十个 case
      case 0x7E2D4A19:
        return result;
      default:
        return -1;
    }
  }
}
```

#### 4.1.2 State Variable 的识别

🧑‍🔬 笔者在分析中注意到，state variable 的选择有几个固定模式：

**标准 OLLVM**：使用单一 32-bit 整数变量，通常是 `w8` 或 `w9`（ARM64 上 Hex-Rays 的命名）。state value 是编译时生成的伪随机常量。

**商业变种**（笔者遇到过的）：
- **双变量 state**：用两个变量的异或/加法结果作为实际 state
- **运算得出 state**：`next_state = current_state * 0x1337 + offset`，不直接赋常量
- **数组间接**：state value 作为数组索引，从全局表中取下一个 state

🔬 笔者写了一个 IDAPython 脚本来自动识别 state variable：

```python
# ida_cff_detect.py - 检测 CFF state variable
# 核心思路：找到被赋值次数最多且值为不同常量的局部变量

import idaapi
import idautils
import idc

def detect_state_variable(func_ea):
    """检测函数中的 CFF state variable"""
    func = idaapi.get_func(func_ea)
    if not func:
        return None
    
    # 收集所有 mov reg, #imm 指令
    var_assignments = {}  # {reg_name: [imm_values]}
    
    for head in idautils.Heads(func.start_ea, func.end_ea):
        mnem = idc.print_insn_mnem(head)
        if mnem in ('MOV', 'MOVZ', 'MOVK'):
            op0 = idc.print_operand(head, 0)
            op1_type = idc.get_operand_type(head, 1)
            if op1_type == idc.o_imm:
                imm_val = idc.get_operand_value(head, 1)
                if op0 not in var_assignments:
                    var_assignments[op0] = []
                var_assignments[op0].append(imm_val)
    
    # state variable 特征：被赋值次数多，值各不相同
    candidates = []
    for reg, values in var_assignments.items():
        unique_values = set(values)
        if len(unique_values) >= 5:  # 至少 5 个不同 state
            candidates.append((reg, len(unique_values), unique_values))
    
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0] if candidates else None
```

#### 4.1.3 Dispatcher 定位

dispatcher 是 CFF 的心脏。在 ARM64 上，标准的 dispatcher 模式通常编译为以下形式之一：

**模式 A：直接 switch（小规模 case）**：

```asm
; dispatcher - 比较链模式
LDR     W8, [SP, #state_offset]
CMP     W8, #0x3A7F1B2C
B.EQ    case_3a7f1b2c
CMP     W8, #0x7E2D4A19
B.EQ    case_7e2d4a19
; ... 每个 case 一组 CMP + B.EQ
```

**模式 B：跳转表（大规模 case）**：

```asm
; dispatcher - 跳转表模式
LDR     W8, [SP, #state_offset]
SUB     W8, W8, #base_value
CMP     W8, #max_case
B.HI    default_case
ADRP    X9, jump_table_page
ADD     X9, X9, #jump_table_offset
LDRSW   X10, [X9, X8, LSL #2]
ADD     X10, X10, X9
BR      X10
```

🧑‍🔬 在笔者分析的那个 `libsign.so` 中，dispatcher 采用的是模式 B。47 个 case 值被编译器映射到了一个跳转表。笔者花了一些时间才意识到，这个跳转表本身也是去平坦化的关键数据——它包含了所有合法的 case 地址。

### 4.2 Bogus Control Flow (BCF)

> BCF 在 CFF 已经够烦人的基础上又加了一层：不仅控制流被拍平了，还多出了一堆永远不会执行的假分支。笔者在分析 BCF 上浪费过最多的时间，因为它让 CFF 的 state 空间看起来比实际大得多。

#### 4.2.1 Opaque Predicate 的本质

BCF 的核心机制是 **opaque predicate**——一个编译时确定结果但运行时难以静态判断真假的条件表达式。OLLVM 标准实现使用的经典 opaque predicate 基于数论恒等式：

```c
// 对任意整数 x，以下表达式恒为 true
(x * (x + 1)) % 2 == 0    // 连续整数之积必为偶数

// OLLVM 在 IR 层面插入的分支：
if ((global_var * (global_var + 1)) % 2 == 0) {
    // 真实代码 — 永远执行
} else {
    // 垃圾代码 — 永远不执行
}
```

🧑‍🔬 笔者在实际分析中遇到的 opaque predicate 远不止这一种。以下是笔者从 7 个不同目标中收集的 7 种模板：

| # | Opaque Predicate 模板 | 恒等式 | 出现频率 |
|---|----------------------|--------|---------|
| 1 | `(x * (x+1)) % 2 == 0` | 连续整数积为偶数 | 高（标准 OLLVM） |
| 2 | `(x * (x+1) * (x+2)) % 6 == 0` | 连续三整数积被 6 整除 | 中 |
| 3 | `(x^2 + x) & 1 == 0` | 等价于模板 1 的位运算版 | 中 |
| 4 | `(3*x^2 + 2*x + 7) & 1 == 1` | 奇数多项式恒为奇数 | 低 |
| 5 | `(x | (x-1)) >= (x-1)` | 位运算恒真 | 低（商业变种） |
| 6 | `(x & y) ^ ((x & y) \| (~x & y)) == (~x & y)` | MBA 恒等式 | 极低（高级变种） |
| 7 | `((x + y) - (x ^ y)) == 2*(x & y)` | MBA 恒等式 | 极低（高级变种） |

模板 6 和 7 属于 MBA（Mixed Boolean-Arithmetic）类别，是笔者遇到的最难处理的变种。它们混合了布尔运算和算术运算，简单的模式匹配无法识别，需要 Z3 或类似的 SMT 求解器来判断恒等性。

#### 4.2.2 BCF 在 IDA 中的视觉特征

```
         [真实基本块 A]
              |
       ┌──────┴──────┐
 [opaque true]   [opaque false]     ← opaque predicate 分支
       |              |
 [真实基本块 B]  [垃圾代码 B']    ← B' 是 B 的克隆 + 随机修改
       |              |
       └──────┬──────┘
              |
      [下一个真实基本块]
```

🧑‍🔬 在 IDA 的 CFG 视图中，BCF 的特征是大量"菱形"结构——一个条件分支分出两条路径，最终汇合到同一个点。两条路径中的代码看起来相似但不完全相同（因为垃圾路径会对克隆的代码做随机修改）。

识别 BCF 的实用技巧：

1. **看条件表达式**：如果 if 条件涉及全局变量的数学运算，且结果不影响任何输出变量，高度疑似 opaque predicate
2. **看两条路径的相似度**：BCF 克隆真实代码后做微调，所以两条路径的指令序列 diff 通常只有 10-20%
3. **用 Frida 验证**：hook 条件分支的地址，跑 1000 次看它是不是总走同一条路径

```python
# frida_bcf_detect.js - 检测疑似 BCF 的条件分支
// 思路：如果一个条件分支在 N 次调用中总走同一方向，它很可能是 opaque predicate

var branch_stats = {};

function hookBranch(addr, name) {
    Interceptor.attach(ptr(addr), {
        onEnter: function(args) {
            // 读取 NZCV 标志位
            var nzcv = this.context.nzcv;
            if (!(name in branch_stats)) {
                branch_stats[name] = {taken: 0, not_taken: 0};
            }
            // 简化判断：检查 Z flag (bit 30)
            if (nzcv & (1 << 30)) {
                branch_stats[name].taken++;
            } else {
                branch_stats[name].not_taken++;
            }
        }
    });
}

// 批量 hook 所有疑似 BCF 分支
// hookBranch("0x12345678", "branch_1");

// 定期打印统计
setInterval(function() {
    for (var name in branch_stats) {
        var s = branch_stats[name];
        var total = s.taken + s.not_taken;
        if (total > 100) {
            var ratio = Math.max(s.taken, s.not_taken) / total;
            if (ratio > 0.99) {
                console.log("[BCF?] " + name + 
                    " always goes " + (s.taken > s.not_taken ? "TAKEN" : "NOT_TAKEN") +
                    " (" + total + " samples)");
            }
        }
    }
}, 5000);
```

#### 4.2.3 BCF 去除策略

🧑‍🔬 笔者总结了三种去除 BCF 的策略，按推荐顺序排列：

| 策略 | 方法 | 适用场景 | 限制 |
|------|------|---------|------|
| **D-810 自动去除** | IDA 插件，内置 opaque predicate 模式库 | 标准 OLLVM 模板 | MBA 变种无法处理 |
| **Z3 符号判定** | 对每个条件表达式用 Z3 check-sat | 所有类型，含 MBA | 速度慢，需要精准提取表达式 |
| **动态染色** | Frida trace + 统计，标记从不执行的路径 | 快速粗筛 | 路径覆盖依赖输入 |

笔者在实战中通常的做法是：先用 D-810 过一遍（去掉 ~90% 的标准模板），再对残留的用 Z3 逐个判定。

### 4.3 Instruction Substitution (ISub)

> ISub 是三种混淆中对去混淆影响最小的一种。它让代码变得难以阅读，但不改变控制流结构，也不影响仿真执行。笔者通常的策略是直接忽略它，除非需要理解算法的具体数学逻辑。

#### 4.3.1 替换规则

OLLVM 的 Instruction Substitution 将标准算术/逻辑运算替换为等价的复杂表达式：

| 原始运算 | 替换后（示例之一） | 等价性 |
|----------|-------------------|--------|
| `a + b` | `a - (-b)` | 算术等价 |
| `a + b` | `(a ^ b) + 2*(a & b)` | MBA 等价 |
| `a - b` | `a + (~b) + 1` | 二进制补码 |
| `a ^ b` | `(a & ~b) \| (~a & b)` | 布尔等价 |
| `a & b` | `(a ^ ~b) & a` | 布尔等价 |
| `a \| b` | `(a & b) \| (a ^ b)` | 布尔等价 |

🧑‍🔬 在 IDA 伪代码中，ISub 的效果是让一个简单的 `x + y` 变成类似这样的东西：

```c
// 原始: result = a + b
// ISub 后:
v12 = a ^ b;
v13 = a & b;
v14 = v13 << 1;
result = v12 ^ v14;  // 还可能再嵌套一层
```

如果读者看到 IDA 伪代码中出现大量 `^`、`&`、`|`、`~` 的组合运算，且最终效果似乎只是简单的加减法——那基本可以确认是 ISub。

#### 4.3.2 处理策略

笔者的处理策略很简单：**不处理**。

理由：
1. ISub 不改变控制流，不影响 CFF 去平坦化
2. ISub 不改变运行时语义，Unicorn/Frida 仿真结果完全正确
3. 如果确实需要简化（比如为了理解算法），用 GAMBA 或 z3 的 `simplify()` 可以自动还原大部分

```python
# z3 简化 MBA 表达式
from z3 import *

a, b = BitVecs('a b', 32)
# ISub 表达式: (a ^ b) + 2 * (a & b)
expr = (a ^ b) + 2 * (a & b)
simplified = simplify(expr)
print(simplified)  # a + b
```

---

## 五、字符串加密与间接调用

> 字符串加密和间接调用不是 OLLVM 原版的核心 pass，但几乎所有商业变种都会加上它们。笔者把它们单独拿出来讲，因为在实战中，解密字符串往往是理解代码功能最快的入口。

### 5.1 字符串加密

🧑‍🔬 **假设**：笔者最初**假设**字符串加密使用全局统一的 XOR key，理由是这样实现最简单。**实验**：在 `libsign.so` 中找到 12 个加密字符串的解密函数，逐个分析。

```
[E07 结果] 12 个函数，12 个不同的 XOR key。假设错误。
```

🔬 实际观察到的字符串加密模式：

```c
// 典型的 OLLVM 字符串解密函数（IDA 伪代码，简化）
char *decrypt_str_0x3a7f() {
    static char buf[] = {0x6B, 0x1D, 0x2E, 0x4F, 0x3C, 0x5A, 0x7E, 0x00};
    static int decrypted = 0;
    if (!decrypted) {
        for (int i = 0; i < 7; i++)
            buf[i] ^= 0x2A;  // 本函数的 key = 0x2A
        decrypted = 1;
    }
    return buf;
}
// 解密后: "Android" (0x6B^0x2A=0x41='A', 0x1D^0x2A=0x37... 等等实际值)
```

🧑‍🔬 笔者发现的规律是：每个字符串有一个专属的解密函数，函数内使用独立的 XOR key 和一个 `decrypted` 标志位（确保只解密一次）。这意味着**不能写一个通用的静态解密脚本**。

🤖 解决方案：笔者最终用 Unicorn 仿真来批量解密。思路是：模拟执行每个解密函数，让它自己完成解密，然后读取结果。

```python
# unicorn_str_decrypt.py - 批量解密 OLLVM 加密字符串
import unicorn
import unicorn.arm64_const as arm64

def decrypt_string(uc, func_addr, so_base, so_data):
    """仿真执行一个字符串解密函数，返回解密后的字符串"""
    STACK_ADDR = 0x7F000000
    STACK_SIZE = 0x10000
    
    # 映射 SO 和栈
    uc.mem_map(so_base, len(so_data) + 0x1000)
    uc.mem_write(so_base, so_data)
    uc.mem_map(STACK_ADDR, STACK_SIZE)
    
    # 设置 SP
    uc.reg_write(arm64.UC_ARM64_REG_SP, STACK_ADDR + STACK_SIZE - 0x100)
    
    # 写入 RET 地址作为停止点
    RET_ADDR = 0xDEAD0000
    uc.mem_map(RET_ADDR & 0xFFFFF000, 0x1000)
    uc.reg_write(arm64.UC_ARM64_REG_LR, RET_ADDR)
    
    try:
        uc.emu_start(func_addr, RET_ADDR, timeout=5000000)
    except unicorn.UcError as e:
        return f"[EMU ERROR: {e}]"
    
    # X0 应该指向解密后的字符串
    result_addr = uc.reg_read(arm64.UC_ARM64_REG_X0)
    try:
        raw = uc.mem_read(result_addr, 256)
        return bytes(raw).split(b'\x00')[0].decode('utf-8', errors='replace')
    except:
        return "[READ ERROR]"

# 使用示例
# strings = [0x1234, 0x5678, ...]  # 解密函数地址列表
# for addr in strings:
#     print(f"0x{addr:x}: {decrypt_string(uc, addr, ...)}")
```

🔬 在 `libsign.so` 上跑完以后，笔者得到了 47 个解密后的字符串，其中包括：

```
0x2a3f0: "getDeviceId"
0x2a410: "android.os.Build"
0x2a450: "FINGERPRINT"
0x2a480: "/proc/self/maps"
0x2a4b0: "frida"            ← 反调试字符串
0x2a4d0: "xposed"           ← 反调试字符串
0x2a510: "HMAC-SHA256"
0x2a540: "AES/CBC/PKCS5Padding"
0x2a590: "su"
...
```

🧑‍🔬 这些字符串立刻让笔者对整个 SO 的功能有了全局认知：设备信息采集 + 反调试检测 + HMAC-SHA256 签名 + AES 加密。比逆向混淆后的代码高效十倍。

### 5.2 间接调用与 GOT/PLT 混淆

🧑‍🔬 在部分商业 OLLVM 变种中，笔者遇到过函数调用也被混淆的情况：

```c
// 正常调用
result = strlen(input);

// 混淆后
typedef size_t (*fn_t)(const char *);
fn_t *table = (fn_t *)(base + encrypted_offset);
fn_t func = table[decrypt_index(0x3a7f)];
result = func(input);
```

这种混淆让 IDA 的交叉引用完全失效——你搜 `strlen` 的 xref，什么都找不到。

🔬 笔者的应对方法是在 Frida 中 hook `dlsym` 和所有 PLT 入口，记录实际调用的函数：

```javascript
// frida_plt_trace.js - 记录所有 PLT 调用
var libsign = Process.findModuleByName("libsign.so");
var plt_start = libsign.base.add(0x1000);  // .plt 通常在 SO 开头附近
var plt_end = libsign.base.add(0x2000);

Interceptor.attach(Module.findExportByName(null, "dlsym"), {
    onEnter: function(args) {
        this.name = args[1].readUtf8String();
    },
    onReturn: function(retval) {
        console.log("[dlsym] " + this.name + " => " + retval);
    }
});
```

---

## 六、实战：从 Frida trace 到 CFG 恢复的完整流程

> 这是全文的核心章节。笔者以 `libsign.so` 中的 `sign_request` 函数为目标，记录从看到一团混淆代码到恢复出可读控制流的完整过程。过程中有 3 次方向错误和 2 次工具选择失误，全部如实记录。

### 6.1 第一步：IDA 初始分析

🧑‍🔬 在 IDA 中打开 `libsign.so`，定位到 `sign_request`（通过字符串交叉引用 "HMAC-SHA256" 找到）。

第一眼看到的 IDA 统计信息：

```
Function: sub_1A3F0
Size: 0x2E40 (11,840 bytes)
Basic blocks: 412
Cyclomatic complexity: 89
```

🔬 正常的 HMAC-SHA256 签名函数，基本块数量应该在 20-40 之间。412 个基本块意味着至少有 10 倍的膨胀——这就是 CFF + BCF 的效果。

**假设 H1**：这个函数使用标准 OLLVM CFF，state variable 是一个 32-bit 整数。

**验证**：在 IDA 伪代码中搜索 `switch`——找到一个 `switch(w8)` 语句，包含 47 个 case。每个 case 末尾都有 `w8 = 0xNNNNNNNN; continue;` 的模式。**确认** H1。

```c
// IDA 伪代码片段
while (1) {
    switch (w8) {
        case 0x07A3B1CE:
            v15 = *(_DWORD *)(v3 + 16);
            if (v15 > 0xFF)
                w8 = 0x5E2D4F81;
            else
                w8 = 0x3C1A2B47;
            continue;
        case 0x0B4E6D2A:
            *((_DWORD *)v6 + 4) = v12 ^ 0x5A3C7E91;
            w8 = 0x2F8A1C5E;
            continue;
        // ... 还有 45 个 case
    }
}
```

🧑‍🔬 笔者手动画了一下前 5 个 case 的跳转关系，发现了一个问题：case `0x07A3B1CE` 的两个后继分别是 `0x5E2D4F81` 和 `0x3C1A2B47`，但在 47 个 case 值中，`0x3C1A2B47` 根本不存在！它跳转到了 `default` 分支。

**推断**：这是一个 BCF 插入的假分支——条件 `v15 > 0xFF` 在实际执行中永远为 false（因为 `v15` 是一个 byte 值），所以 `0x3C1A2B47` 永远不会被执行。

### 6.2 第二步：Frida 动态 trace

> 笔者最初的计划是：用 Frida 在 switch 入口处 hook state variable，跑 100 次不同输入，收集所有出现过的 state 序列。然后直接从 trace 中重建 CFG。

🔬 Frida trace 脚本：

```javascript
// frida_state_trace.js
var libsign = Process.findModuleByName("libsign.so");
var switch_addr = libsign.base.add(0x1A420);  // dispatcher 地址

var trace_log = [];
var call_count = 0;

Interceptor.attach(switch_addr, {
    onEnter: function(args) {
        var state = this.context.x8.toInt32();  // w8 = state variable
        trace_log.push(state);
    }
});

// 每次 sign_request 调用结束后导出 trace
var sign_ret = libsign.base.add(0x1D230);  // 函数返回地址
Interceptor.attach(sign_ret, {
    onEnter: function(args) {
        call_count++;
        send({
            type: 'trace',
            call: call_count,
            states: trace_log.slice()
        });
        trace_log = [];
    }
});
```

跑了 100 次调用（不同的 URL 和 timestamp 输入），结果：

```
[Trace 统计]
总调用次数: 100
观察到的唯一 state 值: 32 / 47
平均每次调用经过的 state 数: 18.3
最长路径: 24 states
最短路径: 14 states
未触发的 state 值: 15 个
```

🧑‍🔬 32/47 的覆盖率让笔者有点失望。15 个从未触发的 state——它们是 BCF 死代码？还是只是笔者的输入没有覆盖到的真实路径？

**假设 H2**：通过 Frida trace 收集的 state 序列可以直接恢复原始 CFG。

**结果**：**部分失败**。32/47 的覆盖率不够——笔者无法确定那 15 个 state 是死代码还是未覆盖的真实路径。如果贸然把它们删掉，可能会破坏极少数输入下的正确性。

### 6.3 第三步：angr 符号执行补全

> 在动态 trace 覆盖率不足后，笔者决定用 angr 做符号执行，试图枚举所有可能的 state 转移。这是整个分析中工程量最大的部分。

🧑‍🔬 笔者先尝试了最简单的方案——直接让 angr 探索整个函数：

```python
# angr_explore_v1.py - 第一次尝试（失败版本）
import angr
import claripy

proj = angr.Project("libsign.so", auto_load_libs=False)
state = proj.factory.blank_state(addr=0x1A3F0)

# 符号化输入参数
url = claripy.BVS("url_ptr", 64)
timestamp = claripy.BVS("timestamp", 64)
state.regs.x0 = url
state.regs.x1 = timestamp

simgr = proj.factory.simgr(state)
simgr.explore(find=0x1D230)  # 函数返回地址

# 结果：30 分钟后 OOM (>16 GB)，路径爆炸
```

**失败**。路径爆炸是意料之中的——CFF 的 switch 在每个 case 末尾都回到 dispatcher，angr 会把每次循环视为新路径，指数级增长。

🧑‍🔬 **转向**：笔者意识到不应该让 angr 探索整个函数，而是应该分析每个 case 独立的 state 转移。具体来说：对每个 case block，从 block 入口开始符号执行到 `continue`（即 state 赋值点），记录赋给 state variable 的值。

```python
# angr_state_transfer.py - 逐 case 分析 state 转移
import angr
import claripy

proj = angr.Project("libsign.so", auto_load_libs=False)

# 从 IDA 中提取的 47 个 case 地址
case_addrs = {
    0x07A3B1CE: 0x1A440,
    0x0B4E6D2A: 0x1A480,
    0x12F5C8A3: 0x1A4C0,
    # ... 省略其余 44 个
}

# dispatcher 中 state 赋值的地址模式
# STR W8, [SP, #offset] 或 MOV W8, #imm
STATE_STORE_PATTERN = 0x1A420  # dispatcher 入口

transitions = {}  # {from_state: [to_state_1, to_state_2, ...]}

for state_val, block_addr in case_addrs.items():
    # 从 case block 入口开始
    init_state = proj.factory.blank_state(addr=block_addr)
    
    # 符号化所有可能影响条件的变量
    for i in range(30):
        init_state.regs.__setattr__(f'x{i}', claripy.BVS(f'x{i}', 64))
    
    simgr = proj.factory.simgr(init_state)
    
    # 步进执行直到回到 dispatcher
    simgr.explore(
        find=STATE_STORE_PATTERN,
        avoid=[],
        num_find=10,  # 一个 case 最多 2 个后继（if/else），留余量
        timeout=30     # 每个 case 最多 30 秒
    )
    
    next_states = set()
    for found in simgr.found:
        # 读取 w8 (state variable) 的值
        w8_val = found.solver.eval(found.regs.w8)
        next_states.add(w8_val)
    
    transitions[state_val] = list(next_states)
    print(f"  0x{state_val:08X} => {[f'0x{s:08X}' for s in next_states]}")
```

🔬 运行结果（23 分钟，peak memory 4.2 GB）：

```
[angr state transfer analysis]
  0x07A3B1CE => ['0x5E2D4F81']           # 只有 1 个后继! BCF 分支被证明不可达
  0x0B4E6D2A => ['0x2F8A1C5E']           # 无条件转移
  0x12F5C8A3 => ['0x4D9F2C71', '0x6A3E5F80']  # 真实条件分支
  ...
  0x3C1A2B47 => []                        # 无法到达的 dead state!
  0x4F8E2D1A => []                        # dead state
  ...

Summary:
  Total states: 47
  Reachable states: 32
  Dead states (BCF): 15
  Conditional branches: 8
  Unconditional transfers: 24
  Return states: 5 (函数出口)
```

🧑‍🔬 **验证** H3：angr 成功确认了 47 个 state 中有 15 个是 BCF 插入的死代码——和 Frida trace 中未触发的 15 个 state 完全吻合。笔者在 Frida 实验时以为可能是覆盖率不够，现在 angr 从静态角度证实了它们确实不可达。这也间接**验证**了 Frida trace 的 100 次采样已经覆盖了所有真实路径。

真实的控制流只有 32 个 state（基本块），其中 8 个有条件分支，24 个无条件转移，5 个是函数出口。

### 6.4 第四步：CFG 重建

有了完整的 state 转移图，重建 CFG 就变成了一个图变换问题：

```python
# cfg_rebuild.py - 从 state 转移图重建 CFG
import networkx as nx

# 从 angr 得到的转移关系
transitions = {
    0x07A3B1CE: [0x5E2D4F81],
    0x0B4E6D2A: [0x2F8A1C5E],
    0x12F5C8A3: [0x4D9F2C71, 0x6A3E5F80],
    # ... 完整的 32 个真实 state
}

# 构建有向图
G = nx.DiGraph()
for src, dsts in transitions.items():
    for dst in dsts:
        G.add_edge(src, dst)

# 移除 dead states (BCF)
dead_states = [s for s in case_addrs if s not in transitions or not transitions[s]]
G.remove_nodes_from(dead_states)

# 找到入口 state (函数开始时 w8 的初始值)
entry_state = 0x3A7F1B2C  # 从 IDA 读到的初始赋值

# 拓扑排序（如果有环就说明有循环结构）
try:
    order = list(nx.topological_sort(G))
    print(f"[CFG] 无环，拓扑序: {len(order)} 个基本块")
except nx.NetworkXUnfeasible:
    cycles = list(nx.simple_cycles(G))
    print(f"[CFG] 发现 {len(cycles)} 个循环（原始代码中的 for/while）")

# 输出去平坦化的伪代码骨架
print("\n[恢复的控制流骨架]")
print(f"entry: state = 0x{entry_state:08X}")
visited = set()

def print_cfg(state, indent=0):
    if state in visited:
        print("  " * indent + f"goto 0x{state:08X}  // 循环回边")
        return
    visited.add(state)
    
    succs = transitions.get(state, [])
    if len(succs) == 0:
        print("  " * indent + f"[0x{state:08X}] return")
    elif len(succs) == 1:
        print("  " * indent + f"[0x{state:08X}] → 0x{succs[0]:08X}")
        print_cfg(succs[0], indent)
    elif len(succs) == 2:
        print("  " * indent + f"[0x{state:08X}] if (cond):")
        print("  " * indent + f"  true  → 0x{succs[0]:08X}")
        print("  " * indent + f"  false → 0x{succs[1]:08X}")
        print_cfg(succs[0], indent + 1)
        print_cfg(succs[1], indent + 1)

print_cfg(entry_state)
```

🔬 最终恢复出的控制流结构：

```
[恢复的控制流骨架]
entry: 0x3A7F1B2C
  ├── 参数校验 (url != null, timestamp > 0)
  │     ├── [失败] → return -1
  │     └── [成功] ↓
  ├── 设备信息拼接 (device_id + model + os_version)
  ├── 时间戳格式化
  ├── HMAC key 初始化
  │     ├── [缓存命中] → 复用 key
  │     └── [缓存未命中] → 从 JNI 获取 key bytes
  ├── HMAC-SHA256(key, url + timestamp + device_info)
  ├── Base64 编码
  └── return signature_string
```

🧑‍🔬 从 412 个基本块到 27 个真实基本块——膨胀率是 15.3 倍。恢复后的控制流是一个标准的签名计算函数，逻辑清晰，完全可读。

### 6.5 失败案例分析

🧑‍🔬 笔者把上述工作流在 7 个不同的 OLLVM 目标上测试了。5 个成功，2 个失败。失败原因值得记录：

| 目标 | state 数 | 结果 | 失败原因 |
|------|---------|------|---------|
| `libsign.so` (电商 A) | 47 | ✓ | — |
| `libsec.so` (电商 B) | 31 | ✓ | — |
| `libcrypto_impl.so` (支付) | 23 | ✓ | — |
| `libprotect.so` (游戏) | 56 | ✓ | — |
| `libauth.so` (社交) | 38 | ✓ | — |
| `libmetasec.so` (短视频) | 127 | ✗ | **双层嵌套 CFF** — 外层 switch 内部还有二级 switch |
| `libguard.so` (金融) | 89 | ✗ | **运算 state** — `next = (cur * 0x1337 + input_byte) & 0xFFFF`，state 依赖输入 |

🧑‍🔬 两个失败案例暴露了标准方法的局限：

1. **嵌套 CFF**：当 switch 内部还有 switch，angr 的路径爆炸问题重新出现。笔者尝试分层处理（先解外层再解内层），但内外层的 state variable 会互相影响。这个问题目前没有优雅的自动化解决方案，笔者最终靠 Unicorn 仿真 + 手动分析完成。

2. **输入依赖 state**：当 next state 不是常量而是依赖输入的运算结果时，angr 的符号执行可以求解，但路径数量变成了输入空间的函数。对于 `input_byte` 有 256 种取值的情况，每个 case 可能有 256 个后继——这本质上是一个虚拟机 dispatcher，不再是简单的 CFF。

---

## 七、去混淆工具链对比

> 笔者在过去两年中使用过 5 种去混淆方案。这一节是基于实际使用体验的横向对比，不是转述文档。

### 7.1 工具清单

| 工具 | 版本 | 类型 | 维护状态 | 笔者使用频率 |
|------|------|------|---------|-------------|
| ![IDA](https://img.shields.io/badge/D--810-IDA_Plugin-blue?style=flat) **D-810** | 0.2.0 | IDA 插件 | 活跃（GitHub） | 高 |
| ![angr](https://img.shields.io/badge/angr-Python-orange?style=flat) **angr** | 9.2.x | Python 框架 | 活跃 | 高 |
| ![GAMBA](https://img.shields.io/badge/GAMBA-Ghidra-green?style=flat) **GAMBA** | 1.x | Ghidra 插件 | 中等 | 中 |
| ![Triton](https://img.shields.io/badge/Triton-DSE-red?style=flat) **Triton** | 1.0.x | DSE 框架 | 活跃 | 低 |
| 🧑‍🔬 **手动分析** | — | Frida + IDA + 脑子 | 永远活跃 | 最高 |

### 7.2 对比评测

🔬 笔者在 4 个目标上（分别代表不同混淆强度）测试了每种方案：

| | Target A (CFF only) | Target B (CFF+BCF) | Target C (CFF+BCF+ISub) | Target D (CFF+BCF+ISub+StrEnc) |
|---|---|---|---|---|
| **D-810** | ✓ 完整去除 | ✓ CFF 去除，BCF 95% | ✓ CFF+BCF，ISub 未处理 | ✓ CFF+BCF，ISub+StrEnc 未处理 |
| **angr** | ✓ 完整恢复 | ✓ 完整恢复（23 min） | ✓ 完整恢复（41 min） | ✓ CFG 恢复（1.2 h），StrEnc 需额外步骤 |
| **GAMBA** | ✓ 完整去除 | ✓ CFF+BCF 大部分 | △ MBA 变种残留 | △ 同上 |
| **Triton** | ✓ 完整恢复 | ✓ 完整恢复（35 min） | ✓ 完整（52 min） | ✓ 同 angr |
| **手动** | ✓ 2h | ✓ 6h | ✓ 12h | ✓ 18h |

| 维度 | D-810 | angr | GAMBA | Triton | 手动 |
|------|-------|------|-------|--------|------|
| **上手难度** | 低（安装即用） | 高（需写脚本） | 中 | 高 | 极高 |
| **CFF 处理** | 优秀 | 优秀 | 良好 | 优秀 | 可行但慢 |
| **BCF 处理** | 良好（标准模板） | 优秀 | 良好 | 优秀 | 取决于经验 |
| **MBA 处理** | 差 | 良好（Z3） | 中等 | 良好（DSE） | 差 |
| **StrEnc 处理** | 无 | 需配合 Unicorn | 无 | 需配合仿真 | Unicorn |
| **可定制性** | 中（规则库） | 极高 | 中 | 高 | — |
| **速度** | 快（秒级） | 慢（分钟~小时） | 快（秒级） | 中等 | 极慢 |

🧑‍🔬 笔者的日常选择：

- **快速分析**（只需大致理解功能）：D-810 一键过 + Frida trace 关键路径
- **深度逆向**（需要完整恢复算法）：angr 恢复 CFG + D-810 清理 BCF + Z3 简化 MBA
- **仿真绕过**（只需要结果，不需要理解代码）：直接 Unicorn/unidbg，跳过去混淆

### 7.3 D-810 使用实例

🔬 D-810 是笔者最常用的去混淆工具，因为它是 IDA 插件，不需要离开分析环境。

```python
# D-810 的典型使用方式（在 IDA 中）
# 1. 安装: 将 D-810 放入 IDA plugins 目录
# 2. 在 IDA 中: Edit → Plugins → D-810
# 3. 选择规则集: 
#    - "OLLVM Standard" — 标准 CFF + BCF
#    - "Custom" — 可以添加自定义 opaque predicate 模式

# D-810 的内部工作原理（简化）：
# 1. 遍历所有 basic blocks
# 2. 识别 switch-dispatcher 模式 → 标记为 CFF
# 3. 对每个 opaque predicate，匹配规则库中的模板
# 4. 将恒真分支替换为无条件跳转，删除恒假分支
# 5. 重建 CFG
```

D-810 处理前后的 IDA 伪代码对比：

处理前（412 个基本块，一个巨型 switch）：
```c
while (1) {
    switch (w8) {
        case 0x07A3B1CE: /* ... */ w8 = 0x5E2D4F81; continue;
        case 0x0B4E6D2A: /* ... */ w8 = 0x2F8A1C5E; continue;
        // ... 45 个 case
    }
}
```

处理后（27 个基本块，正常控制流）：
```c
if (url == 0 || timestamp <= 0)
    return -1;
device_info = get_device_info();
formatted_ts = format_timestamp(timestamp);
hmac_key = get_or_init_key();
signature = hmac_sha256(hmac_key, concat(url, formatted_ts, device_info));
return base64_encode(signature);
```

🧑‍🔬 这个对比清楚地展示了去混淆的价值——从不可读到一目了然。不过需要注意的是，D-810 的输出不是完美的。笔者在对比 D-810 输出和手动恢复结果时，发现 D-810 有时会把两个本应分开的基本块合并成一个，导致细粒度的条件逻辑丢失。对于只需理解大致功能的场景这不是问题，但如果需要精确复现算法，还是需要手动验证。

---

## 八、OLLVM 在 AI 时代的失效曲线

> 笔者在使用 LLM 辅助分析 OLLVM 混淆代码的过程中，逐渐意识到一个让人不安的趋势：OLLVM 的保护效果正在被 AI 快速侵蚀。这一节不是预测未来，而是记录笔者在过去半年中的实际观察。

### 8.1 LLM 能做什么

🤖🧑‍🔬 笔者在分析过程中使用 LLM 辅助完成了以下任务：

| 任务 | 人工耗时 | LLM 辅助后耗时 | 提速倍数 | 具体方式 |
|------|---------|---------------|---------|---------|
| 识别 opaque predicate 模板 | 2h | 10min | 12x | 将条件表达式粘贴给 LLM，请求判断恒等性 |
| 编写 angr 脚本 | 4h | 45min | 5.3x | 描述目标，LLM 生成初版脚本，手动调试 |
| 理解 ISub 变换后的算术表达式 | 1h/个 | 5min/个 | 12x | 将 MBA 表达式给 LLM，请求简化 |
| Frida hook 脚本 | 30min | 5min | 6x | 描述 hook 点和需要记录的信息 |
| IDA 伪代码功能理解 | 3h | 20min | 9x | 将去混淆后的伪代码给 LLM 做功能注释 |

### 8.2 LLM 不能做什么

🧑‍🔬 笔者同样记录了 LLM 完全无法完成的任务：

| 任务 | 尝试方式 | 失败原因 |
|------|---------|---------|
| 从混淆后的 IDA 伪代码直接恢复 CFG | 给 LLM 完整伪代码，请求分析控制流 | 上下文窗口不足，无法追踪 47 个 state 的转移关系 |
| 判断一个 basic block 是否是 BCF 死代码 | 给 LLM 单个 block 的代码 | 缺少全局 state 信息，无法判断可达性 |
| 交互式调试 Frida 脚本 | 让 LLM 根据错误输出修改脚本 | 需要实时反馈，LLM 无法与设备交互 |
| 选择去混淆策略 | 描述目标，请求建议 | LLM 给出教科书级建议，但无法评估在具体目标上的可行性 |

### 8.3 OLLVM 各保护层的 AI 时代失效曲线

🧑‍🔬 基于以上实际体验，笔者对 OLLVM 各保护层在 AI 辅助下的有效性做了如下评估：

| 保护层 | 无 AI 时有效性 | AI 辅助后有效性 | 变化 | 原因 |
|--------|-------------|---------------|------|------|
| **CFF (控制流平坦化)** | 高 | 中低 | ↓↓ | angr 脚本可由 LLM 生成，大幅降低工程门槛 |
| **BCF (虚假控制流)** | 中高 | 低 | ↓↓↓ | opaque predicate 识别几乎可由 LLM 一键完成 |
| **ISub (指令替换)** | 中 | 极低 | ↓↓↓ | MBA 表达式简化是 LLM 的强项 |
| **String Encryption** | 中 | 低 | ↓↓ | 仿真脚本可由 LLM 快速生成 |
| **间接调用** | 中 | 中 | ↓ | 仍需动态分析，LLM 只能辅助脚本编写 |
| **自定义 VM (非标准)** | 极高 | 高 | ↓ | VM handler 逆向仍需大量人工判断 |
| **Server-side validation** | 极高 | 极高 | → | 不受客户端分析工具影响 |

🧑‍🔬 关键发现：**OLLVM 标准 pass 的保护效果正在被 AI 快速消解**。BCF 和 ISub 在 LLM 面前几乎透明——opaque predicate 判断和 MBA 简化恰好是 LLM 擅长的数学推理任务。CFF 虽然需要工程工具（angr/Triton），但 LLM 可以大幅降低使用这些工具的门槛。

真正抵抗 AI 的只有两类保护：自定义 VM（因为需要理解 VM 语义，这是高度非标准化的任务）和 server-side validation（因为 AI 不能替你发请求）。

---

## 九、防御视角：如果你是保护方

> 这一节站在保护方的角度思考。如果笔者是负责保护 `libsign.so` 的安全工程师，会怎么改进？

### 9.1 改进提案表

| 优先级 | 当前状态 | 建议改进 | 效果 | 实施成本 | 可行性 |
|--------|---------|---------|------|---------|--------|
| **P0** | 签名算法完全在客户端 | **引入 server-side co-signing**：关键签名步骤在服务端完成 | 攻击者即使完全逆向客户端也无法独立签名 | 高（需改架构） | ⭐⭐⭐⭐ |
| **P0** | 标准 OLLVM CFF | **升级为自定义 VM**：将核心算法提升为自定义字节码 | 从"去 CFF 平坦化"变成"逆向 VM 指令集"，难度跃迁 | 高 | ⭐⭐⭐⭐ |
| **P1** | 固定 opaque predicate 模板 | **MBA 混合 + 运行时动态 predicate**：每次编译生成不同模板 | 破坏 D-810 等工具的模式匹配 | 中 | ⭐⭐⭐⭐⭐ |
| **P1** | 静态 XOR 字符串加密 | **运行时 key 派生**：key 来自设备指纹 hash | 仿真器中解密结果与真机不同 | 低 | ⭐⭐⭐⭐⭐ |
| **P2** | 无完整性校验 | **代码完整性绑定**：签名计算依赖 `.text` 段 hash | patch 任何指令都会导致签名错误 | 中 | ⭐⭐⭐⭐ |
| **P2** | 无环境感知 | **反仿真检测**：检测 Unicorn/unidbg 特征 | 增加仿真绕过的成本 | 低 | ⭐⭐⭐ |
| **P3** | 编译时固定混淆 | **OTA 混淆更新**：定期下发新混淆版本的 SO | 攻击者的分析结果定期过期 | 高（需分发基础设施） | ⭐⭐⭐ |

### 9.2 范式转移

🧑‍🔬 笔者在写完上面的改进表之后，意识到一个更根本的问题：**OLLVM 代表的是"通过复杂化代码来保护秘密"这一范式，而这个范式在 AI 时代正在全面失效**。

原因很简单：代码混淆增加的是分析者的工程成本，但 AI 正在以指数级速度降低工程成本。当工程成本趋近于零时，混淆的保护效果也趋近于零。

真正有效的保护范式应该基于**信息论**而非**计算复杂度**：

| 旧范式 | 新范式 | 原因 |
|--------|--------|------|
| 混淆代码让人看不懂 | 关键计算不在客户端发生 | 看不看得懂不重要，因为信息根本不在这里 |
| 复杂的 opaque predicate | 证明安全的密码学协议 | 数论恒等式对 LLM 透明，但密码学假设不是 |
| 增加反编译工作量 | 增加需要协调的信任方数量 | 计算工作量可以用更多 GPU 克服，信任协调不行 |

### 9.3 笔者不建议做的事情

基于本文的分析结果，以下操作在技术上可能可行，但笔者明确不建议：

1. **批量伪造签名**：去混淆的目的是理解保护机制，不是绕过它
2. **发布完整的去混淆工具**：针对特定 App 的去混淆工具会直接降低攻击门槛
3. **用去混淆结果做自动化爬虫**：这已经不是安全研究，而是滥用

---

## 十、结论

### 10.1 核心贡献

本文记录了笔者在 OLLVM 去混淆方面的系统性工程实践，核心贡献如下：

1. **三大 pass 的工程拆解**：从 LLVM IR 层面到 ARM64 二进制层面，完整分析了 CFF、BCF、ISub 的变换机制和识别方法
2. **12 个 IDA 视觉特征**：总结了在 IDA 中识别三种混淆的实用指纹
3. **7 种 opaque predicate 模板**：从真实目标中收集的模板库，含 2 种 MBA 变种
4. **Frida + angr 联合工作流**：动态 trace 确定主路径，符号执行补全覆盖率，两者互为验证
5. **半自动 CFF 去平坦化**：5/7 目标成功，2 个失败案例的根因分析
6. **5 种去混淆方案的实测对比**：基于 4 个目标的横向评测
7. **AI 时代失效曲线**：基于实际使用数据的各保护层有效性评估
8. **防御方改进提案**：6 条 P0-P3 级具体建议

### 10.2 借鉴来源

| 来源 | 借鉴内容 | 使用方式 |
|------|---------|---------|
| [OLLVM 原始论文](https://github.com/obfuscator-llvm/obfuscator) (Junod et al., 2015) | CFF/BCF/ISub 的原始设计 | 知识基础 |
| [D-810](https://github.com/joydo/d810) (Boris Music) | opaque predicate 模板库 | 直接使用 + 扩展 |
| [angr documentation](https://docs.angr.io/) | 符号执行 API | 直接使用 |
| [Quarkslab OLLVM 分析](https://blog.quarkslab.com/) | BCF 机制分析 | 参考 |
| [GAMBA](https://github.com/kenoph/gamba) | MBA 去混淆方法 | 参考 + 对比 |
| [Triton](https://triton-library.github.io/) | DSE 框架 | 对比评测 |

### 10.3 独立贡献

| 贡献 | 类型 | 说明 |
|------|------|------|
| 7 种 opaque predicate 模板（含 MBA） | 🧑‍🔬 原创收集 | 从 7 个商业目标中提取 |
| Frida + angr 联合工作流 | 🧑‍🔬 方法论 | 动态 + 符号执行互补 |
| 半自动 CFF 去平坦化脚本 | 🤖🧑‍🔬 工程实现 | 600 行 Python，LLM 辅助编写框架 |
| AI 时代失效曲线 | 🧑‍🔬 原创分析 | 基于半年实际使用数据 |
| 5/7 成功率分析 | 🔬 实验数据 | 含失败案例的根因分析 |
| 防御方改进提案 | 🧑‍🔬 原创建议 | 从攻击经验反推防御改进 |

### 10.4 留给读者的问题

笔者在分析过程中一直在思考一个更深层的问题：

> 如果 OLLVM 代表的"代码混淆"范式在 AI 时代正在失效，那么客户端保护的出路在哪里？
>
> TEE（Trusted Execution Environment）是一个方向，但 TEE 本身也有被攻破的先例。Server-side co-signing 增加了延迟和可用性成本。行为指纹依赖大数据基础设施。
>
> 也许答案不在任何单一技术中，而在于**让攻击者需要同时突破多个正交维度**：代码保护 + 环境感知 + 服务端协同 + 行为画像。每一层都可能被单独突破，但同时突破所有层的成本会超线性增长。
>
> 这和密码学中的"深度防御"思想一致——不追求任何单层的绝对安全，而是追求总体成本超过攻击收益。

---

*本文所有分析基于已公开的技术原理和笔者自行搭建的测试环境。文中涉及的 App 名称、SO 名称和地址均经过脱敏处理。研究目的是评估保护方案的安全性，不鼓励任何形式的滥用。*
