"""Generate the strategy overview SVG without external rendering dependencies."""

from html import escape
from pathlib import Path

OUT = Path(__file__).with_name("strategy-overview.svg")
WIDTH, HEIGHT = 3000, 1840

INK = "#173044"
MUTED = "#526579"
LINE = "#90a0ae"
BG = "#f6f8fb"
CORE = "#eaf3f8"
WHEEL = "#fff3df"
REGIME = "#f1ecfa"
POST = "#e8f6ee"
RISK = "#f8ecec"
WHITE = "#ffffff"

svg: list[str] = [
    f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
    "<title>ThetaGang 交易策略全景图</title>",
    "<desc>展示 Wheel、Regime-aware 再平衡、VIX Call 对冲、现金管理和统一订单风险边界的实际执行路径。</desc>",
    f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BG}"/>',
    """<defs>
      <marker id="arrow" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto">
        <path d="M0,0 L12,6 L0,12 z" fill="#90a0ae"/>
      </marker>
      <marker id="arrow-dark" markerWidth="12" markerHeight="12" refX="10" refY="6" orient="auto">
        <path d="M0,0 L12,6 L0,12 z" fill="#526579"/>
      </marker>
    </defs>""",
]


def text(
    x: float,
    y: float,
    value: str,
    size: int = 22,
    color: str = INK,
    anchor: str = "start",
    weight: int = 400,
) -> None:
    svg.append(
        f'<text x="{x}" y="{y}" text-anchor="{anchor}" dominant-baseline="middle" '
        f'fill="{color}" font-family="PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif" '
        f'font-size="{size}" font-weight="{weight}">{escape(value)}</text>'
    )


def box(
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    lines: list[str],
    fill: str = WHITE,
    stroke: str = "#cad5de",
    radius: int = 18,
    title_size: int = 24,
    line_size: int = 19,
) -> None:
    svg.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
    )
    text(x + 24, y + 32, title, title_size, INK, weight=500)
    for idx, line in enumerate(lines):
        text(x + 24, y + 72 + idx * 29, line, line_size, MUTED)


def section(
    x: float,
    y: float,
    w: float,
    h: float,
    title: str,
    subtitle: str,
    fill: str,
    stroke: str,
) -> None:
    svg.append(
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="24" fill="{fill}" stroke="{stroke}" stroke-width="3"/>'
    )
    text(x + 28, y + 35, title, 29, INK, weight=500)
    text(x + 28, y + 75, subtitle, 19, MUTED)


def arrow(
    x1: float, y1: float, x2: float, y2: float, dashed: bool = False, dark: bool = False
) -> None:
    stroke = "#526579" if dark else LINE
    dash = ' stroke-dasharray="10 8"' if dashed else ""
    marker = "arrow-dark" if dark else "arrow"
    svg.append(
        f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="{stroke}" stroke-width="3"{dash} marker-end="url(#{marker})"/>'
    )


text(70, 54, "ThetaGang 交易策略全景图", 40, INK, weight=500)
text(
    70,
    102,
    "配置驱动 · 一次运行按阶段规划 · 期权、股票、对冲和现金共享账户级风险边界",
    22,
    MUTED,
)
text(2930, 54, "代码行为快照 · 2026-09-10", 18, MUTED, anchor="end")

# Execution timeline.
section(
    70,
    150,
    2860,
    245,
    "一次运行的主路径",
    "阶段顺序由 CANONICAL_STAGE_ORDER 决定；策略配置只决定哪些阶段启用。",
    CORE,
    "#86a9be",
)
box(
    100,
    225,
    365,
    115,
    "① 快照",
    ["账户 / 持仓 / 工作挂单", "本轮订单意图进入预计组合"],
    WHITE,
)
box(
    510,
    225,
    500,
    115,
    "② 期权写入",
    ["卖 Put → 卖 Call", "目标权重、方向、波动与合约链门控"],
    WHEEL,
)
box(
    1055,
    225,
    615,
    115,
    "③ 股票再平衡",
    ["Regime / Buy-only / Sell-only", "软硬带、状态过滤、缺口与现金流"],
    REGIME,
)
box(
    1715,
    225,
    500,
    115,
    "④ 期权管理",
    ["展期 → 平仓", "DTE、ITM、P&L、信用与超额仓位"],
    WHEEL,
)
box(
    2260,
    225,
    620,
    115,
    "⑤ 后置与提交",
    ["VIX 对冲 → 现金管理 → 整批风控", "dry-run 预览或实盘提交 / 调价"],
    POST,
)
arrow(465, 282, 510, 282, dark=True)
arrow(1010, 282, 1055, 282, dark=True)
arrow(1670, 282, 1715, 282, dark=True)
arrow(2215, 282, 2260, 282, dark=True)

# Strategy cards.
section(
    70,
    445,
    1210,
    1035,
    "Wheel · 期权收益与仓位转换",
    "卖 Put 获得标的敞口，股票到位后卖 Call；空头合约按规则展期或止盈。",
    WHEEL,
    "#d7ae67",
)
box(
    105,
    540,
    500,
    150,
    "目标仓位",
    [
        "目标金额 = 权重 × 购买力",
        "目标股数 = floor(目标金额 / 价格)",
        "Put 股数还要扣除 100 × 净空头 Put",
    ],
    WHITE,
)
box(
    655,
    540,
    575 - 40,
    150,
    "共同写入门控",
    [
        "可交易 / 非 buy-only 或 sell-only",
        "green/red 方向与当日变动阈值",
        "新增张数受购买力百分比限制",
    ],
    WHITE,
)
arrow(605, 615, 655, 615)
box(
    105,
    755,
    1120,
    125,
    "OptionChainScanner",
    [
        "目标 DTE / 最大 DTE · Delta 上限 · 行权价方向 · 最低价格 · 未平仓量 · 排除旧合约"
    ],
    WHITE,
)
arrow(890, 690, 890, 755)
box(
    105,
    935,
    500,
    150,
    "卖 Put",
    ["按需卖出 Put", "低于目标时逐步获得持仓", "买方接 assignment 后转为股票"],
    WHITE,
)
box(
    655,
    935,
    570,
    150,
    "卖 Covered Call",
    [
        "cap_factor / cap_target_floor",
        "excess_only 与仓位阈值",
        "新行权价不低于成本线与现价",
    ],
    WHITE,
)
arrow(350, 880, 350, 935)
arrow(900, 880, 900, 935)
box(
    105,
    1135,
    1120,
    255,
    "空头期权管理",
    [
        "ITM、超额仓位、DTE、P&L 和 max_dte 决定是否动作",
        "展期：BAG 组合单 = BUY 旧合约 + SELL 新合约",
        "平仓：空头 BUY 平仓；配置 close_at_pnl 可直接止盈",
        "credit_only / 无合约时可按配置关闭或跳过",
    ],
    WHITE,
)
arrow(350, 1085, 350, 1135)
arrow(940, 1085, 940, 1135)
text(
    105,
    1445,
    "长期特征：卖 Put 承担下跌与 assignment 风险；卖 Call 换取权利金但限制上涨空间。",
    19,
    MUTED,
)

section(
    1320,
    445,
    1010,
    1035,
    "Regime-aware · 状态门控的股票再平衡",
    "Wheel 与 Regime 互斥；Regime 只规划股票订单，并可把 Flow 余量预留给现金管理。",
    REGIME,
    "#aa91ca",
)
box(
    1355,
    540,
    440,
    150,
    "资金基准",
    ["net_liq", "managed_stocks", "net_liq_ex_options", "再乘 regime_margin_usage"],
    WHITE,
)
box(
    1845,
    540,
    450,
    150,
    "有效权重",
    ["可按历史波动率调整", "归一化后计算目标股数", "逐标的得到 relative_drift"],
    WHITE,
)
arrow(1795, 615, 1845, 615)
box(
    1355,
    755,
    940,
    155,
    "市场状态过滤",
    [
        "软带：Choppiness ≥ min 且 Efficiency ≤ max，冷却期结束，Ratio gate 可选",
        "硬带：超过 hard_band，越过状态与冷却限制；可只回归 fraction",
    ],
    WHITE,
)
arrow(1815, 690, 1815, 755)
box(
    1355,
    975,
    280,
    175,
    "Soft",
    ["偏离 ≥ soft_band", "状态和冷却通过", "按目标买卖"],
    WHITE,
)
box(1685, 975, 280, 175, "Hard", ["偏离 ≥ hard_band", "安全护栏", "可部分回归"], WHITE)
box(
    2015,
    975,
    280,
    175,
    "Flow / Deficit",
    ["超额现金方向一致", "或缺口达到安全轨", "必要时跌破目标卖出"],
    WHITE,
)
arrow(1815, 910, 1495, 975)
arrow(1815, 910, 1825, 975)
arrow(1815, 910, 2155, 975)
box(
    1355,
    1210,
    940,
    175,
    "股票订单 + 资金预留",
    [
        "按每标的交易策略过滤买卖方向与最小阈值",
        "Flow 买入后仍需使用的现金由后置现金管理预留",
        "数据不足时该策略本轮停止生成订单",
    ],
    WHITE,
)
arrow(1495, 1150, 1495, 1210)
arrow(1825, 1150, 1825, 1210)
arrow(2155, 1150, 2155, 1210)

section(
    2370,
    445,
    560,
    1035,
    "后置策略",
    "VIX 与现金管理在股票/期权规划后执行。",
    POST,
    "#83b999",
)
box(
    2405,
    540,
    490,
    250,
    "VIX Call 对冲",
    [
        "已有净多 Call 且 VIX 过平仓线 → 卖出",
        "否则按 VIXMO 区间选择 allocation",
        "预算 = NLV × weight",
        "按目标 Delta / DTE 扫描并买入",
    ],
    WHITE,
)
box(
    2405,
    850,
    490,
    250,
    "Cash management",
    [
        "有效现金 = TotalCash + 待发生变动",
        "扣除 Regime 预留现金",
        "高于目标 + buy_threshold → 买现金基金",
        "低于目标 - sell_threshold → 卖现金基金",
    ],
    WHITE,
)
arrow(2650, 790, 2650, 850)
text(2405, 1175, "VIX Call 是尾部保护层，也会产生时间价值成本。", 18, MUTED)
text(2405, 1210, "现金基金是现金调度，不是收益保证。", 18, MUTED)

# Shared risk and boundaries.
section(
    70,
    1540,
    2860,
    245,
    "统一风险与互斥边界",
    "策略只生成意图；整批风险、执行状态和券商对账决定是否真的提交。",
    RISK,
    "#c68989",
)
box(
    100,
    1615,
    630,
    110,
    "整批 BatchRisk",
    ["重复/反向订单 · 股票超卖 · Call 覆盖", "合计支出 · 币种/乘数 · 实盘保证金预估"],
    WHITE,
)
box(
    770,
    1615,
    630,
    110,
    "执行与恢复",
    ["先写 ExecutionJournal，再提交", "未知结果、未解决工作单先阻断下一批"],
    WHITE,
)
box(
    1440,
    1615,
    630,
    110,
    "配置互斥",
    [
        "wheel ↔ regime_rebalance",
        "calls 写入依赖 puts 写入",
        "shares_only 跳过期权写入和展期",
    ],
    WHITE,
)
box(
    2110,
    1615,
    790,
    110,
    "共享账户视角",
    [
        "购买力、NLV、持仓、挂单和本轮意图共同影响后续阶段",
        "未成交卖出收入与期权贷记不会提前变成可用资金",
    ],
    WHITE,
)

svg.append("</svg>")
OUT.write_text("\n".join(svg), encoding="utf-8")
print(f"wrote {OUT}")
