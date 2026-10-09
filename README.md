# +5 Security Research

> DRM 安全研究 / 逆向工程与二进制安全 / 硬件安全 / AI Agent

个人安全研究博客的源码仓库，基于 [Hugo](https://gohugo.io/) + [PaperMod](https://github.com/adityatelange/hugo-PaperMod) 构建，通过 GitHub Actions 自动部署到 GitHub Pages。

**在线阅读：<https://overkazaf.github.io/blogs/>**

## 文章目录

### 🔐 DRM 安全研究

Widevine、FairPlay、PlayReady 等数字版权管理系统的协议分析、白盒密码学攻防与端到端信任链拆解。

| 日期 | 文章 |
| --- | --- |
| 2026-08-24 | [一段 UTF-16 XML 凭什么管住 4K？PlayReady 的套娃式架构](https://overkazaf.github.io/blogs/posts/playready-pro-license-sl3000-deep-dive/) |
| 2026-08-23 | [Cromite 源码在手，Widevine 还是过不了](https://overkazaf.github.io/blogs/posts/cromite-bromite-widevine-stream-export-failure/) |
| 2026-08-23 | [Chrome 里藏了个虚拟机，它到底在保护什么](https://overkazaf.github.io/blogs/posts/chrome-vmp-protection-vm-dispatch-whitebox/) |
| 2026-08-22 | [有了 PSSH 还是拿不到 Key，从 L3 到 L1 有多远](https://overkazaf.github.io/blogs/posts/widevine-pssh-license-l1-deep-dive/) |
| 2026-08-15 | [把 Netflix MSL 拆成字节 - 一次协议逆向的路线、踩坑与经验](https://overkazaf.github.io/blogs/posts/netflix-msl-protocol-reverse-engineering/) |
| 2026-05-13 | [3.4 秒：一条 TCP 选项改变的 57 倍 - FairPlay DRM 解密管线优化实录](https://overkazaf.github.io/blogs/posts/fairplay-drm-decrypt-pipeline-optimization/) |
| 2026-05-10 | [五个函数，一条链 - Apple FairPlay DRM 的 Frida 逆向全记录](https://overkazaf.github.io/blogs/posts/fairplay-drm-frida-reversing/) |
| 2026-05-08 | [谁在铸造破解白盒的武器？Quarkslab 的十年](https://overkazaf.github.io/blogs/posts/quarkslab-drm-whitebox-cryptanalysis-arsenal/) |
| 2026-05-04 | [13 种攻击全部失败之后，Chrome CDM 白盒 AES 到底怎么绕](https://overkazaf.github.io/blogs/posts/chrome-cdm-stream-dump-widevine-vtable-hook/) |
| 2026-04-29 | [学习拉马努金提高注意力的解题模式 - 谈谈基于DFA的Widevine L3 keybox量产技术](https://overkazaf.github.io/blogs/posts/widevine-l3-keybox-mass-production/) |

### 🔧 逆向工程与二进制安全

Android/iOS 应用逆向、签名算法分析、设备指纹、APK 加固体系、端风控架构与 TEE 攻击面研究。

| 日期 | 文章 |
| --- | --- |
| 2026-10-08 | [当 LLM 学会拆炸弹 — Quarkslab 四维框架下的 AI 反混淆能力全景](https://overkazaf.github.io/blogs/posts/llm-deobfuscation-quarkslab-four-dimensional-framework/) |
| 2026-10-03 | [右键点两下，密码就出来了 - Ponce4Ghidra 符号执行实战：从 crackme 到 license key 全流程](https://overkazaf.github.io/blogs/posts/ponce4ghidra-symbolic-execution-crackme/) |
| 2026-08-25 | [把 classes.dex 藏起来就安全了？主流加固方案的真实防线](https://overkazaf.github.io/blogs/posts/android-apk-hardening-packer-vmp-rasp-mainstream/) |
| 2026-08-25 | [一个 Canvas 当然认不出你：海内外主流 Web / Android 设备指纹与风控体系全景](https://overkazaf.github.io/blogs/posts/device-fingerprinting-web-android-mainstream-platforms/) |
| 2026-08-08 | [当 Hook 失效的那一刻：SVC 指令级系统调用保护的攻防](https://overkazaf.github.io/blogs/posts/android-svc-syscall-protection/) |
| 2026-07-23 | [拆开 anti-token 以后 - 拼多多历史样本中的环境记录与服务端盲区](https://overkazaf.github.io/blogs/posts/pinduoduo-libpdd-secure-antitoken-evidence-boundaries/) |
| 2026-07-10 | [当 sign() 函数变成了两万行伪代码](https://overkazaf.github.io/blogs/posts/android-native-vmp-analysis/) |
| 2026-06-18 | [签名过了，订单就安全吗 - 重读美团 mtgsig、DFP 与服务端风控](https://overkazaf.github.io/blogs/posts/meituan-mtgsig-dfp-risk-control-boundaries/) |
| 2026-06-05 | [所有分支都指向同一个 switch，然后呢](https://overkazaf.github.io/blogs/posts/ollvm-deobfuscation-engineering/) |
| 2026-05-26 | [Shield 之后还有什么 - 小红书请求签名与设备风控材料复核](https://overkazaf.github.io/blogs/posts/xiaohongshu-shield-device-risk-evidence-boundaries/) |
| 2026-05-09 | [四层特权，四条链，一个目标 - ARM TrustZone EL0→EL3 攻击实录](https://overkazaf.github.io/blogs/posts/arm-trustzone-el0-to-el3-attack-chain-anatomy/) |
| 2026-04-17 | [淘四神究竟守哪道门 - 重读 SecurityGuard 与 MTOP 的公开证据](https://overkazaf.github.io/blogs/posts/taobao-securityguard-mtop-four-signatures-boundaries/) |
| 2026-03-29 | [驯服六头蛇：驾驭希腊诸神 - 抖音六神签名算法的 unidbg 逆向全记录](https://overkazaf.github.io/blogs/posts/douyin-sixgod-metasec-unidbg-reverse-engineering/) |

### 🔩 硬件安全

嵌入式安全与硬件 DRM：MCU 固件逆向、安全启动、侧信道攻击、自制 DRM 系统与硬件攻防实验。

| 日期 | 文章 |
| --- | --- |
| 2026-09-30 | [用 RP2350 从零搭建一个 DRM 系统](https://overkazaf.github.io/blogs/posts/rp2350-diy-audio-drm-from-scratch/) |

### 🤖 AI Agent

AI Agent 在安全研究中的实践：自动化逆向、智能 fuzzing、LLM 辅助漏洞挖掘。*（筹备中）*

## 仓库结构

```text
.
├── content/
│   ├── posts/<post>/index.md   # 文章（Page Bundle，配图 / 代码 / 日志放在同目录）
│   ├── categories/             # 分类描述页
│   ├── about/  archives.md  search.md
├── static/images/              # 部分文章的共享图片资源
├── layouts/
│   ├── partials/               # 覆盖主题：giscus 评论、分享按钮、head/footer 扩展
│   └── shortcodes/             # 自定义 shortcode
├── assets/{css,js}/            # 自定义样式与脚本
├── themes/PaperMod/            # 主题（直接 vendored，非 submodule）
├── hugo.toml                   # 站点配置
└── .github/workflows/          # GitHub Pages 自动部署
```

## 本地预览

需要 [Hugo extended](https://gohugo.io/installation/)（CI 使用 `0.147.6`）。

```bash
git clone https://github.com/overkazaf/blogs.git
cd blogs
hugo server -D          # http://localhost:1313/blogs/ ，-D 同时显示草稿
```

## 写新文章

```bash
mkdir -p content/posts/my-new-post
$EDITOR content/posts/my-new-post/index.md
```

Front matter 约定：

```yaml
---
title: "文章标题"
slug: "english-url-slug"          # 决定最终 URL：/blogs/posts/<slug>/
date: 2026-10-09T10:00:00+08:00
draft: false
tags: ["reverse-engineering", "Android"]
categories: ["reverse-engineering"] # drm-security | reverse-engineering | hardware-security | ai-agent
---
```

- 配图放在文章目录下的 `images/`，用相对路径引用（如 `![](images/arch.png)`），避免依赖外部 URL。
- 新增文章后记得同步更新本 README 的文章目录。

## 部署

推送到 `main` 分支后，[`.github/workflows`](.github/workflows) 会自动执行 `hugo --minify` 并发布到 GitHub Pages。`public/` 为本地构建产物，已在 `.gitignore` 中忽略。

## 评论

文章评论基于 [giscus](https://giscus.app/)，数据存放在本仓库的 GitHub Discussions 中。

## 声明

文章内容仅用于安全研究与技术交流，请勿用于任何违法用途。涉及的第三方产品与协议分析均基于公开资料或个人授权环境下的研究。

## License

文章内容采用 [CC BY-NC-SA 4.0](LICENSE) 协议授权 —— 转载请注明出处，禁止商业用途，衍生作品需以相同协议发布。
