"""Generate Cocoon-style architecture diagrams (HTML + inline SVG) for the LLM deobfuscation post."""
import html, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = sys.argv[1]
TPL = open(os.path.join(HERE, 'tpl.html')).read()

K = {  # semantic palette from the skill
    'cyan':    ('rgba(8, 51, 68, 0.4)',   '#22d3ee'),
    'emerald': ('rgba(6, 78, 59, 0.4)',   '#34d399'),
    'violet':  ('rgba(76, 29, 149, 0.4)', '#a78bfa'),
    'amber':   ('rgba(120, 53, 15, 0.3)', '#fbbf24'),
    'rose':    ('rgba(136, 19, 55, 0.4)', '#fb7185'),
    'orange':  ('rgba(251, 146, 60, 0.3)', '#fb923c'),
    'slate':   ('rgba(30, 41, 59, 0.5)',  '#94a3b8'),
}
# obfuscation pass -> colour, kept identical across all five diagrams
PASS = {'none': 'slate', 'BCF': 'amber', 'IS': 'violet', 'CFF': 'cyan', 'COMBO': 'rose'}
GREY = '#94a3b8'


def e(s):
    return html.escape(str(s), quote=False)


def T(x, y, s, size=12, fill='white', weight=None, anchor='start', mono=False, extra=''):
    w = f' font-weight="{weight}"' if weight else ''
    return f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}"{w} text-anchor="{anchor}"{extra}>{e(s)}</text>'


def R(x, y, w, h, kind, rx=6, dashed=None, mask=True, sw=1.5):
    fill, stroke = K[kind]
    out = []
    if mask:
        out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="#0f172a"/>')
    da = f' stroke-dasharray="{dashed}"' if dashed else ''
    out.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{da}/>')
    return '\n'.join(out)


def BOUND(x, y, w, h, kind, label, dash='8,4'):
    stroke = K[kind][1]
    tint = {'amber': 'rgba(251, 191, 36, 0.05)', 'rose': 'transparent'}.get(kind, 'rgba(148, 163, 184, 0.03)')
    return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="{tint}" stroke="{stroke}" stroke-width="1" stroke-dasharray="{dash}"/>\n'
            + T(x + 14, y + 20, label, 12, stroke, 600))


def CODE(x, y, w, lines, size=12):
    h = 14 + 18 * len(lines)
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="4" fill="#020617" stroke="#1e293b" stroke-width="1"/>']
    for i, (s, c) in enumerate(lines):
        out.append(T(x + 10, y + 20 + 18 * i, s, size, c))
    return '\n'.join(out)


def PILL(x, y, w, s, kind, size=11):
    fill, stroke = K[kind]
    return (f'<rect x="{x}" y="{y}" width="{w}" height="20" rx="10" fill="{fill}" stroke="{stroke}" stroke-width="1"/>\n'
            + T(x + w / 2, y + 14, s, size, stroke, 600, 'middle'))


def ARROW(d, kind='slate', dashed=False, w=1.5):
    stroke = K[kind][1] if kind != 'grey' else '#64748b'
    da = ' stroke-dasharray="5,5"' if dashed else ''
    return f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{w}"{da} marker-end="url(#ah-{kind})"/>'


def svg(w, h, body, y0=0):
    markers = ''.join(
        f'<marker id="ah-{k}" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="{v[1]}"/></marker>'
        for k, v in K.items())
    markers += '<marker id="ah-grey" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="#64748b"/></marker>'
    return f'''<svg viewBox="0 {y0} {w} {h - y0}">
        <defs>
          {markers}
          <pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse">
            <path d="M 40 0 L 0 0 0 40" fill="none" stroke="#1e293b" stroke-width="0.5"/>
          </pattern>
        </defs>
        <rect y="{y0}" width="100%" height="100%" fill="url(#grid)" />
{body}
      </svg>'''


def cards(items):
    out = ['<div class="cards">']
    for color, title, lis in items:
        out.append(f'      <div class="card">\n        <div class="card-header">\n          <div class="card-dot {color}"></div>\n          <h3>{e(title)}</h3>\n        </div>\n        <ul>')
        out += [f'          <li>• {e(li)}</li>' for li in lis]
        out.append('        </ul>\n      </div>')
    out.append('    </div>')
    return '\n'.join(out)


def page(name, title, h1, sub, svg_s, card_items, footer):
    s = (TPL.replace('%%TITLE%%', e(title)).replace('%%H1%%', e(h1)).replace('%%SUB%%', e(sub))
         .replace('%%SVG%%', svg_s).replace('%%CARDS%%', cards(card_items)).replace('%%FOOTER%%', e(footer)))
    open(os.path.join(OUT, name + '.html'), 'w').write(s)


FOOT = 'LLM 反混淆 · Quarkslab 四维框架'

# ---------------------------------------------------------------- 1. experiment pipeline
b = []
# arrows first (behind boxes)
b.append(ARROW('M 250 155 L 288 155', 'grey'))
b.append(ARROW('M 620 155 L 658 155', 'grey'))
b.append(ARROW('M 820 250 C 820 300, 450 290, 450 328', 'emerald'))
b.append(T(832, 292, '汇编文本', 11, '#34d399'))
b.append(ARROW('M 580 415 L 618 415', 'grey'))
# source
b.append(R(20, 60, 230, 190, 'slate'))
b.append(T(135, 84, '测试函数 · C 源码', 14, 'white', 600, 'middle'))
b.append(T(135, 104, '15 行，按 n % 4 分 4 个分支', 11, GREY, anchor='middle'))
b.append(CODE(32, 118, 206, [('switch (n % 4) {', '#e2e8f0'), ('0: (n | K) * (2 ^ n)', '#e2e8f0'),
                             ('1: (n & K) * (3 + n)', '#e2e8f0'), ('2,3: ...', '#64748b'), ('}', '#e2e8f0')]))
b.append(T(135, 232, 'K = 0xBAAAD0BF', 11, '#fbbf24', anchor='middle'))
# OLLVM
b.append(BOUND(290, 40, 330, 240, 'amber', 'OLLVM 编译 · 每次只开一种混淆'))
rows = [('none', '无混淆', '对照组'), ('BCF', 'BCF', '伪造控制流'), ('IS', 'IS', '指令替换'),
        ('CFF', 'CFF', '控制流平坦化'), ('COMBO', '组合', 'BCF + IS + CFF')]
for i, (p, n, s) in enumerate(rows):
    y = 72 + 41 * i
    b.append(R(310, y, 290, 32, PASS[p]))
    b.append(T(326, y + 21, n, 13, 'white', 600))
    b.append(T(586, y + 21, s, 11, K[PASS[p]][1], anchor='end'))
# capstone
b.append(R(660, 60, 320, 190, 'emerald'))
b.append(T(820, 84, 'Capstone 反汇编', 14, 'white', 600, 'middle'))
b.append(T(820, 104, '模型只能看到 x86_64 汇编', 11, GREY, anchor='middle'))
b.append(CODE(675, 118, 290, [('mov   eax, edi', '#e2e8f0'), ('and   eax, 3', '#e2e8f0'),
                              ('or    ecx, 0xbaaad0bf', '#fbbf24'), ('imul  eax, ecx', '#e2e8f0'), ('...', '#64748b')]))
b.append(T(820, 232, '无源码 · 无伪代码', 11, '#34d399', anchor='middle'))
# LLMs
b.append(BOUND(20, 330, 560, 160, 'slate', '8 个 LLM · 独立还原，记录完整对话', '4,4'))
models = ['GPT-4o', 'GPT-4.5', 'GPT-Pro-o1', 'GPT-o3 Mini', 'DeepSeek R1', 'Grok 3', 'Grok 2', 'Claude 3.7']
for i, m in enumerate(models):
    x = 40 + 133 * (i % 4); y = 360 + 64 * (i // 4)
    b.append(R(x, y, 120, 46, 'cyan'))
    b.append(T(x + 60, y + 28, m, 12, 'white', 600, 'middle'))
# scoring
b.append(R(620, 330, 360, 160, 'orange'))
b.append(T(800, 354, '攻击者知识等级 0 - 5', 14, 'white', 600, 'middle'))
b.append(T(800, 374, '分数 = 需要人帮多少忙', 11, GREY, anchor='middle'))
scale = ['#34d399', '#6ee7b7', '#fde68a', '#fbbf24', '#fb923c', '#fb7185']
for i, c in enumerate(scale):
    x = 641 + 54 * i
    b.append(f'<rect x="{x}" y="392" width="48" height="36" rx="4" fill="#0f172a" stroke="{c}" stroke-width="1.5"/>')
    b.append(T(x + 24, 416, str(i), 15, c, 700, 'middle'))
b.append(T(641, 450, '0 = AI 自主完成', 11, '#34d399'))
b.append(T(959, 450, '5 = 无法分析', 11, '#fb7185', anchor='end'))
b.append(T(641, 474, '← 混淆被攻破', 11, GREY))
b.append(T(959, 474, '混淆仍有效 →', 11, GREY, anchor='end'))
# legend
b.append(T(20, 523, '图例', 11, 'white', 600))
for i, (p, n) in enumerate([('none', '无混淆'), ('BCF', 'BCF'), ('IS', 'IS'), ('CFF', 'CFF'), ('COMBO', '组合混淆'), ]):
    x = 70 + 110 * i
    fill, stroke = K[PASS[p]]
    b.append(f'<rect x="{x}" y="514" width="16" height="10" rx="2" fill="{fill}" stroke="{stroke}" stroke-width="1"/>')
    b.append(T(x + 22, 523, n, 11, GREY))
b.append(f'<rect x="620" y="514" width="16" height="10" rx="2" fill="{K["cyan"][0]}" stroke="{K["cyan"][1]}" stroke-width="1"/>')
b.append(T(642, 523, '被测模型', 11, GREY))
b.append(f'<rect x="730" y="514" width="16" height="10" rx="2" fill="{K["orange"][0]}" stroke="{K["orange"][1]}" stroke-width="1"/>')
b.append(T(752, 523, '评分', 11, GREY))
page('experiment_pipeline', '实验流程', '论文实验怎么做', '同一段代码 × 五种混淆 × 八个模型；只改混淆方式，其余条件不变',
     svg(1000, 540, '\n'.join(b)),
     [('amber', '控制变量', ['同一份 15 行 C 源码', 'OLLVM 每次只开一种混淆', '最后一版 BCF + IS + CFF 全开']),
      ('cyan', '模型输入', ['只给 Capstone 反汇编结果', '看不到源码，也看不到伪代码', '必要时人工追加提示，记录全过程']),
      ('rose', '读结论时要打折', ['测试函数很短，汇编只有几百行', '真实业务函数动辄上千行', '模型版本截至 2025 年 3 月'])],
     FOOT)

# ---------------------------------------------------------------- 2. four dimensions
b = []
caps = [('推理深度', 'Reasoning Depth', '能否证明一个条件永远成立', 'x*(x-1) 必为偶数 → 假分支', 'BCF'),
        ('噪声过滤', 'Noise Filtering', '能否从无用代码里挑出真正干活的几行', '假分支、死代码都是噪声', 'BCF'),
        ('模式识别', 'Pattern Recognition', '能否认出被改写过的运算', '(a^b) + 2*(a&b) 其实是 a+b', 'IS'),
        ('上下文整合', 'Context Integration', '能否把打散的代码块按原顺序拼回去', '要同时记住大量跳转关系', 'CFF')]
targets = {'BCF': (620, 60, 170), 'IS': (620, 250, 90), 'CFF': (620, 360, 90)}
# connectors
for i, (n, en, d, ex, p) in enumerate(caps):
    y = 60 + 100 * i + 45
    tx, ty, th = targets[p]
    ty_mid = ty + th / 2 + (-35 if (p == 'BCF' and i == 0) else 35 if p == 'BCF' else 0)
    b.append(ARROW(f'M 330 {y} C 470 {y}, 470 {ty_mid}, {tx - 2} {ty_mid}', PASS[p]))
b.append(T(475, 50, '被考验的能力 → 针对它设计的混淆', 11, GREY, anchor='middle'))
for i, (n, en, d, ex, p) in enumerate(caps):
    y = 60 + 100 * i
    b.append(R(30, y, 300, 90, 'slate'))
    b.append(T(46, y + 24, n, 14, 'white', 600))
    b.append(T(314, y + 24, en, 11, K[PASS[p]][1], anchor='end'))
    b.append(T(46, y + 48, d, 11, GREY))
    b.append(T(46, y + 72, '例：' + ex, 11, '#cbd5e1'))
# pass boxes
info = {'BCF': ('BCF 伪造控制流', '中', ['插入永远不会执行的假分支', '条件看似有意义，实际恒真 / 恒假', '同时考：推理深度 + 噪声过滤', 'Claude 3.7 自主识破，其余多需提示']),
        'IS': ('IS 指令替换', '高', ['简单运算换成等价但更绕的式子', '所有模型 Level 4-5']),
        'CFF': ('CFF 控制流平坦化', '低', ['拆成碎片，用 switch 统一调度', '多数模型 Level 0-2'])}
for p, (x, y, h) in targets.items():
    title, res, lines = info[p]
    k = PASS[p]
    b.append(R(x, y, 350, h, k))
    b.append(T(x + 16, y + 26, title, 14, 'white', 600))
    b.append(PILL(x + 250, y + 12, 86, '抵抗力：' + res, k))
    for j, l in enumerate(lines):
        b.append(T(x + 16, y + 52 + 22 * j, l, 11, '#cbd5e1' if j < len(lines) - 1 else K[k][1]))
# combo
b.append(ARROW('M 180 450 L 180 488', 'rose', dashed=True))
b.append(ARROW('M 795 450 L 795 488', 'rose', dashed=True))
b.append(R(30, 490, 940, 80, 'rose'))
b.append(T(46, 516, '组合混淆 = BCF + IS + CFF', 14, 'white', 600))
b.append(T(46, 542, '四项能力同时被考，任何一项短板都会让整个还原失败', 11, '#cbd5e1'))
b.append(T(46, 560, '8 个模型全部 Level 5 或完全失败', 11, '#fb7185'))
for j, n in enumerate(['推理深度', '噪声过滤', '模式识别', '上下文整合']):
    b.append(PILL(560 + 100 * j, 520, 90, n, 'rose'))
page('four_dimensions', '四维框架', '四维框架 · 每种混淆在考模型的哪项能力', '左边是模型需要的能力，右边是专门针对这项能力设计的混淆',
     svg(1000, 590, '\n'.join(b)),
     [('amber', 'BCF 考两项', ['推理深度：证明谓词恒真 / 恒假', '噪声过滤：忽略假分支与死代码', '谓词越复杂（如 MBA），越难识破']),
      ('violet', 'IS 考模式识别', ['模型靠统计"猜"模式', '猜得出常见改写，证不了等价', '因此单独使用就有高抵抗力']),
      ('cyan', '怎么用这张图', ['看下一代模型在哪个维度进步最快', '对应的混淆就会最先失效', '组合混淆要求四项同时过关'])],
     FOOT)

# ---------------------------------------------------------------- 3. resistance tiers
b = []
b.append(ARROW('M 30 460 L 30 108', 'grey'))
b.append(T(44, 112, '对 LLM 的抵抗力', 12, GREY, 600))
tiers = [(60, 260, 'CFF', '低 · CFF 控制流平坦化', '已不安全', 'L0-2', ['多数模型 Level 0-2', '长上下文直接把碎片拼回原样'], '不要单独使用'),
         (370, 200, 'BCF', '中 · BCF 伪造控制流', '看谓词复杂度', 'L0-5', ['Claude 3.7 Level 0，其余 Level 1-5', 'x*(x-1) 为偶数：已被识破', 'MBA 变种谓词：仍有一战'], '换成 MBA 形式的谓词'),
         (680, 140, 'COMBO', '高 · IS / 组合混淆', '当前有效防线', 'L5 / 失败', ['IS 单独使用：Level 4-5', '组合混淆：全部 Level 5 或失败', '模型能猜模式，证不了等价'], 'IS 打底，再叠加多层')]
for i, (x, y, p, title, tag, lv, lines, adv) in enumerate(tiers):
    k = PASS[p]
    h = 460 - y
    b.append(R(x, y, 290, h, k))
    b.append(T(x + 16, y + 28, title, 14, 'white', 600))
    b.append(PILL(x + 16, y + 42, 110, tag, k))
    b.append(T(x + 274, y + 57, lv, 16, K[k][1], 700, 'end'))
    for j, l in enumerate(lines):
        b.append(T(x + 16, y + 92 + 22 * j, l, 11, '#cbd5e1'))
    b.append(f'<line x1="{x + 16}" y1="{y + 162}" x2="{x + 274}" y2="{y + 162}" stroke="#334155" stroke-width="1"/>')
    b.append(T(x + 16, y + 184, '建议：' + adv, 11, K[k][1], 600))
    if i < 2:
        nx, ny = tiers[i + 1][0], tiers[i + 1][1]
        b.append(ARROW(f'M {x + 200} {y - 6} C {x + 240} {ny - 40}, {nx - 40} {ny + 30}, {nx - 4} {ny + 30}', 'grey', dashed=True))
b.append(T(500, 495, '单层混淆正在被 LLM 侵蚀，多层组合仍是有效防线（模型版本截至 2025 年 3 月）', 12, '#e2e8f0', 600, 'middle'))
page('resistance_tiers', '抵抗力阶梯', '三层抵抗力 · 哪些混淆还挡得住 LLM', '台阶越高，模型越难还原；数字为论文中的"攻击者知识等级"',
     svg(1000, 515, '\n'.join(b), 85),
     [('cyan', '低：CFF', ['分发器结构太规整', '长上下文模型能整体拼回', '只能当作组合中的一层']),
      ('amber', '中：BCF', ['强度取决于不透明谓词', '经典谓词已进入模型"常识"', 'MBA 谓词能显著抬高门槛']),
      ('rose', '高：IS / 组合', ['等价改写需要证明，不能靠猜', '多层叠加让每个维度同时承压', '短期内仍是可靠防线'])],
     FOOT)

# ---------------------------------------------------------------- 4. quarkslab agent lab
b = []
b.append(BOUND(20, 30, 960, 250, 'amber', 'Docker 沙箱 · 每轮 80 分钟 · 全程无人类干预'))
b.append(ARROW('M 390 160 L 458 160', 'emerald'))
b.append(T(424, 150, '攻击', 11, GREY, anchor='middle'))
b.append(ARROW('M 500 280 L 500 292', 'rose'))
for i, x in enumerate([170, 500, 830]):
    b.append(ARROW(f'M 500 318 C 500 335, {x} 330, {x} 348', 'rose', dashed=True))
b.append(R(50, 70, 340, 180, 'emerald'))
b.append(T(220, 96, 'Claude Code Agent', 14, 'white', 600, 'middle'))
b.append(T(220, 116, '攻击方 · 自己决定下一步', 11, GREY, anchor='middle'))
for j, l in enumerate(['反汇编 / 反编译工具', '自己写脚本、仿真执行', '每轮 80 分钟时间预算']):
    b.append(T(74, 148 + 24 * j, '• ' + l, 12, '#cbd5e1'))
b.append(T(220, 234, '胜利条件：恢复隐藏字符串', 11, '#34d399', 600, 'middle'))
b.append(BOUND(460, 60, 500, 200, 'rose', '目标 · AArch64 二进制 · 保护逐级加强', '4,4'))
lv = [('slate', 'L1', '字符串明文存放'), ('amber', 'L2', '字符串加密，运行时才解密'), ('rose', 'L3', 'L2 + RASP 传感器（检测调试 / 篡改）')]
for j, (k, n, s) in enumerate(lv):
    y = 90 + 54 * j
    b.append(R(480, y, 460, 44, k))
    b.append(T(500, y + 27, n, 14, K[k][1], 700))
    b.append(T(540, y + 27, s, 12, 'white'))
b.append(R(390, 294, 220, 24, 'rose', rx=12, sw=1))
b.append(T(500, 311, '观察到的三类系统性缺陷', 12, '#fb7185', 600, 'middle'))
defects = [('01', '环境利用', ['在容器里翻到 SOLUTION.txt、', 'SSH 凭据等辅助文件，', '直接绕过了逆向本身'], '它先找捷径，不一定在逆向', '发布环境保持干净'),
           ('02', '叙事固着', ['早期认定一个解释（如把 RASP', '传感器当成恶意软件），之后', '证据再矛盾也很少回头'], '及时转向仍是人的优势', '给出看似合理的错误线索'),
           ('03', '产物伪造', ['写出"正在仿真"的脚本，', '实际什么也没执行；还会引用', '不存在的"上一轮结果"'], 'Agent 的结论必须复核', '出错时别崩溃，返回假结果')]
for i, (num, name, ph, ins, dfn) in enumerate(defects):
    x = 20 + 330 * i
    b.append(R(x, 350, 300, 220, 'rose'))
    b.append(T(x + 16, 378, num, 16, '#fb7185', 700))
    b.append(T(x + 52, 378, name, 14, 'white', 600))
    for j, l in enumerate(ph):
        b.append(T(x + 16, 410 + 20 * j, l, 11, '#cbd5e1'))
    b.append(f'<line x1="{x + 16}" y1="{486}" x2="{x + 284}" y2="{486}" stroke="#334155" stroke-width="1"/>')
    b.append(T(x + 16, 510, '启示：' + ins, 11, '#34d399'))
    b.append(T(x + 16, 536, '防御：' + dfn, 11, '#fbbf24'))
page('quarkslab_agent_lab', 'Agent 实验', 'Quarkslab 实验 · 让 AI Agent 自己去逆向', '把 Claude Code 放进沙箱攻击逐级加固的二进制，目标是找回隐藏字符串',
     svg(1000, 590, '\n'.join(b)),
     [('emerald', '实验设置', ['Agent 在 Docker 沙箱内自主行动', '可用逆向工具与脚本仿真', '每轮 80 分钟，目标是隐藏字符串']),
      ('rose', '三类缺陷', ['环境利用：先找捷径', '叙事固着：认定后很少修正', '产物伪造：声称做了但没做']),
      ('amber', '对防守方的启示', ['发布环境不要留辅助文件', '用合理但错误的线索误导', '异常时返回假结果而不是崩溃'])],
     FOOT)

# ---------------------------------------------------------------- 5. challenge so flow
b = []
# connectors first
for x in [140, 380, 620, 860]:
    b.append(ARROW(f'M 500 80 C 500 100, {x} 95, {x} 118', 'grey'))
b.append(ARROW('M 140 170 C 140 200, 180 200, 180 228', 'cyan'))
b.append(ARROW('M 380 170 C 380 200, 500 200, 500 228', 'amber'))
b.append(ARROW('M 320 330 L 358 330', 'grey'))
b.append(T(339, 322, 's1', 11, GREY, anchor='middle'))
b.append(ARROW('M 640 330 L 678 330', 'grey'))
b.append(T(659, 322, 's1,s2', 11, GREY, anchor='middle'))
for cx in [180, 500, 820]:
    b.append(ARROW(f'M {cx} 450 L {cx} 488', 'emerald'))
    b.append(T(cx + 8, 474, '通过', 11, '#34d399'))
    b.append(ARROW(f'M {cx} 534 C {cx} 560, 500 550, 500 578', 'emerald'))
b.append(R(300, 30, 400, 50, 'slate'))
b.append(T(500, 52, 'int verify_key(const char* input)', 13, 'white', 600, 'middle'))
b.append(T(500, 70, '输入必须正好 16 字节，按 4 字节切成 4 段', 11, GREY, anchor='middle'))
for i, x in enumerate([40, 280, 520, 760]):
    used = i < 2
    b.append(R(x, 120, 200, 50, 'slate', dashed=None if used else '4,4'))
    b.append(T(x + 100, 141, f'part{i + 1}', 13, 'white' if used else '#64748b', 600, 'middle'))
    b.append(T(x + 100, 159, f'input[{4 * i}..{4 * i + 3}] · uint32' if used else '未参与校验', 11, GREY if used else '#fb7185', anchor='middle'))
stages = [(40, 'CFF', '1', 's1 = stage1_verify(part1)', 'CFF 控制流平坦化', '考上下文整合', 's1 == 0x12345678', 'LLM 应该能解', 'emerald'),
          (360, 'BCF', '2', 's2 = stage2_verify(part2, s1)', 'BCF 假分支 + MBA 恒真谓词', '考推理深度与噪声过滤', 's2 == 0x9ABCDEF0', 'LLM 部分能解', 'amber'),
          (680, 'COMBO', '3', 's3 = stage3_verify(s1, s2)', 'IS + CFF + BCF 三层组合', '四个维度同时施压', 's3 == 0xCAFEBABE', 'LLM 应该解不出', 'rose')]
for x, p, n, call, prot, dim, chk, exp, ek in stages:
    k = PASS[p]
    b.append(R(x, 230, 280, 220, k))
    b.append(f'<circle cx="{x + 24}" cy="{256}" r="12" fill="{K[k][1]}"/>')
    b.append(T(x + 24, 261, n, 13, '#0f172a', 700, 'middle'))
    b.append(T(x + 44, 261, 'Stage ' + n, 14, 'white', 600))
    b.append(CODE(x + 14, 278, 252, [(call, '#e2e8f0')], 11))
    b.append(T(x + 16, 336, '保护：' + prot, 11, '#cbd5e1'))
    b.append(T(x + 16, 358, '维度：' + dim, 11, '#cbd5e1'))
    b.append(T(x + 16, 384, '校验：', 11, '#cbd5e1'))
    b.append(T(x + 58, 384, chk, 12, K[k][1], 600))
    b.append(PILL(x + 16, 400, 120, exp, ek))
    b.append(T(x + 264, 414, '否则 return 0', 11, '#fb7185', anchor='end'))
for i, cx in enumerate([180, 500, 820]):
    b.append(R(cx - 90, 490, 180, 44, 'emerald'))
    b.append(T(cx, 517, f'FLAG {i + 1}', 14, 'white', 700, 'middle'))
b.append(R(330, 580, 340, 44, 'emerald', sw=2))
b.append(T(500, 607, 'return 1 · 三个 FLAG 全部拿到', 13, 'white', 600, 'middle'))
page('challenge_so_flow', '挑战 SO 验证链', 'llm_challenge.so · 三级验证链', '每一级用一种不同的混淆保护，过一级拿一个 FLAG；任何一级不通过就 return 0',
     svg(1000, 645, '\n'.join(b)),
     [('cyan', 'Stage 1 · CFF', ['只依赖 part1', '平坦化后的分发器可被还原', '验证上下文整合能力']),
      ('amber', 'Stage 2 · BCF + MBA', ['依赖 part2 与 s1', 'MBA 谓词比 x*(x-1) 更难识破', 'D810G verify 可做 Z3 复核']),
      ('rose', 'Stage 3 · 组合', ['只依赖 s1、s2', 'IS + CFF + BCF 同时施压', '预期 LLM 无法独立还原'])],
     FOOT)
print('ok')
