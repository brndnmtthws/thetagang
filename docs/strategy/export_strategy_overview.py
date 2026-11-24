"""Export the reviewed strategy map as SVG and, optionally, PNG.

SVG: .venv/bin/python docs/strategy/export_strategy_overview.py
PNG: use a Python runtime with Pillow and append --png.
This is a document exporter; it imports no trading code.
"""

import argparse
import importlib
import math
from html import escape
from pathlib import Path
from typing import Any

WIDTH, HEIGHT = 1800, 2220
INK, MUTED, LINE = "#19364c", "#496174", "#8a9fab"
WHITE, BACK = "#ffffff", "#f7f9fb"
WHEEL, REGIME, POST, WARN = "#fff3dd", "#f0ebfa", "#e9f5ee", "#fff0eb"
OUT = Path(__file__).parent
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def export(png: bool) -> None:
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<title>ThetaGang 交易策略 · 代码复核版</title>",
        "<desc>两条互斥主策略路线与可选后置阶段。包含已复现的 Delta 回退、止盈优先级、VIX 分档和股票禁交易开关缺口。</desc>",
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{BACK}"/>',
    ]
    image: Any = None
    draw: Any = None
    font_module: Any = None
    if png:
        image_module = importlib.import_module("PIL.Image")
        draw_module = importlib.import_module("PIL.ImageDraw")
        font_module = importlib.import_module("PIL.ImageFont")
        image = image_module.new("RGB", (WIDTH * 2, HEIGHT * 2), BACK)
        draw = draw_module.Draw(image)

    def text(
        x: float,
        y: float,
        value: str,
        size: int = 22,
        color: str = INK,
        max_width: float = 1680,
    ) -> None:
        svg.append(
            f'<text x="{x}" y="{y}" dominant-baseline="text-before-edge" '
            f'font-family="Hiragino Sans GB, PingFang SC, Microsoft YaHei, sans-serif" '
            f'font-size="{size}" fill="{color}">{escape(value)}</text>'
        )
        if draw is not None:
            assert font_module is not None
            font = font_module.truetype(FONT, size * 2)
            bounds = draw.textbbox((0, 0), value, font=font)
            width = bounds[2] - bounds[0]
            if width > max_width * 2:
                raise ValueError(f"Text exceeds its column: {value}")
            draw.text(
                (x * 2 - bounds[0], y * 2 - bounds[1]), value, font=font, fill=color
            )

    def rect(x: float, y: float, w: float, h: float, fill: str) -> None:
        svg.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
            f'rx="14" fill="{fill}" stroke="#cfdae2" stroke-width="1.5"/>'
        )
        if draw is not None:
            draw.rounded_rectangle(
                (x * 2, y * 2, (x + w) * 2, (y + h) * 2),
                radius=28,
                fill=fill,
                outline="#cfdae2",
                width=3,
            )

    def box(
        x: float,
        y: float,
        w: float,
        h: float,
        title: str,
        lines: list[str],
        fill: str = WHITE,
    ) -> None:
        rect(x, y, w, h, fill)
        text(x + 24, y + 18, title, 25, max_width=w - 48)
        for index, line in enumerate(lines):
            top = y + 56 + index * 31
            if top + 25 > y + h - 12:
                raise ValueError(f"Text exceeds box height: {title}")
            text(x + 24, top, line, 21, MUTED, max_width=w - 48)

    def arrow(x1: float, y1: float, x2: float, y2: float) -> None:
        angle = math.atan2(y2 - y1, x2 - x1)
        points = [
            (x2, y2),
            (
                x2 - 12 * math.cos(angle) + 5 * math.sin(angle),
                y2 - 12 * math.sin(angle) - 5 * math.cos(angle),
            ),
            (
                x2 - 12 * math.cos(angle) - 5 * math.sin(angle),
                y2 - 12 * math.sin(angle) + 5 * math.cos(angle),
            ),
        ]
        svg.append(
            f'<path d="M{x1} {y1} L{x2} {y2}" fill="none" stroke="{LINE}" stroke-width="2"/>'
        )
        svg.append(
            '<polygon points="'
            + " ".join(f"{x},{y}" for x, y in points)
            + f'" fill="{LINE}"/>'
        )
        if draw is not None:
            draw.line((x1 * 2, y1 * 2, x2 * 2, y2 * 2), fill=LINE, width=4)
            draw.polygon([(x * 2, y * 2) for x, y in points], fill=LINE)

    text(60, 36, "ThetaGang 交易策略 · 代码复核版", 38)
    text(
        60,
        92,
        "2026-09-10  /  基于当前源码与隔离调用  /  图中参数描述的是现有行为",
        21,
        MUTED,
    )
    box(
        60,
        145,
        1680,
        133,
        "配置选路 → 持仓与挂单对账 → 规划订单",
        [
            "run.strategies 按规范顺序展开；显式 run.stages 按填写顺序执行。",
            "仓库示例：Wheel 已选；VIX 与现金阶段已选，但各自 enabled=false，实际跳过。",
        ],
    )
    text(60, 312, "A · Wheel", 30)
    text(930, 312, "B · Regime 股票再平衡", 30)
    text(740, 319, "两条主路线互斥", 20, MUTED, max_width=170)
    arrow(470, 278, 470, 354)
    arrow(1330, 278, 1330, 354)

    box(
        60,
        360,
        810,
        158,
        "1 目标仓位与覆盖数量",
        [
            "B = NLV × wheel_margin_usage；T = floor(权重 × B / 价格)",
            "Put 缺口扣除股票和已计入空头 Put（每张按100股）。",
            "Call 目标由 cap_factor / cap_target_floor / excess_only 决定。",
        ],
        WHEEL,
    )
    box(
        60,
        556,
        810,
        158,
        "2 先规划卖 Put，再规划卖 Call",
        [
            "标的允许交易 + green/red + 当日价格变化达到阈值。",
            "buy_only / both 抑制新 Put；sell_only / both 抑制新 Call。",
            "新增合约分批数量至少为1张；百分比并非名义敞口硬上限。",
        ],
        WHEEL,
    )
    box(
        60,
        752,
        810,
        190,
        "3 合约扫描 → 股票补仓 / 减仓",
        [
            "DTE 是下限；按到期、Delta、价格、行权价、未平仓量选约。",
            "Delta 无合格候选且最低价格非零时，可回退到超限候选。",
            "股票模式 off / buy_only / sell_only / both 决定直接买卖。",
            "新规划订单先入队；不会在这里立即成交。",
        ],
        WHEEL,
    )
    box(
        60,
        980,
        810,
        223,
        "4 管理已有空头期权（排除 VIX）",
        [
            "先判展期；不满足展期时，才检查 P&L > close_at_pnl。",
            "展期 = BUY 旧合约 + SELL 新合约；允许同到期换执行价。",
            "always_when_itm 优先于旧 DTE / P&L 等普通展期限制。",
            "展期失败转平仓还要求 roll.max_dte 非空且盈利。",
            "买回 Call 不会卖出股票；Put 卖方被指派后才取得股票。",
        ],
        WHEEL,
    )
    for a, b in [(518, 556), (714, 752), (942, 980)]:
        arrow(465, a, 465, b)

    box(
        930,
        360,
        810,
        158,
        "1 选择资金基准 B",
        [
            "net_liq：NLV × regime_margin_usage。",
            "net_liq_ex_options：扣除有符号期权市值，再乘资金比例。",
            "managed_stocks：受管股票市值合计，不再乘资金比例。",
        ],
        REGIME,
    )
    box(
        930,
        556,
        810,
        158,
        "2 有效权重直接决定目标金额",
        [
            "基础权重 × 目标波动率 / 实现波动率 → 限幅与平滑。",
            "有效权重不再归一到100%；总和下降可主动保留现金。",
            "归一化只用于代理指数、ratio basket 等辅助计算。",
        ],
        REGIME,
    )
    box(
        930,
        752,
        810,
        190,
        "3 优先级：Hard → Soft → Deficit → Flow",
        [
            "Hard：偏离越过硬带，可部分回归；Soft：偏离 + 软门控。",
            "软门控 = 震荡/效率指标 + 冷却期 + 可选 ratio gate。",
            "Deficit：优先卖超配，必要时低于目标卖出补缺口。",
            "Flow：现金盈余或较小缺口 + 软门控 + 股数方向一致性。",
        ],
        REGIME,
    )
    box(
        930,
        980,
        810,
        223,
        "4 过滤股票订单与预留现金",
        [
            "Hard/Soft 后还可能追加 Deficit 清理。",
            "所有模式仍受交易允许、买卖方向和最小阈值限制。",
            "硬带也需历史数据校验通过；核心数据不足会中止本轮。",
            "仅 Flow 产生买单后，才预留未使用的正现金余量。",
            "普通波动率降仓位留下的现金不自动获得 Flow 预留。",
        ],
        REGIME,
    )
    for a, b in [(518, 556), (714, 752), (942, 980)]:
        arrow(1335, a, 1335, b)

    arrow(465, 1203, 465, 1257)
    arrow(1335, 1203, 1335, 1257)
    text(
        60,
        1260,
        "汇合后的可选阶段：先 VIX Call，再现金管理（各自还需 enabled=true）",
        27,
    )
    box(
        60,
        1315,
        810,
        244,
        "VIX Call 对冲",
        [
            "已有净多 Call：VIX 超过平仓线才平仓，否则持有。",
            "净数量≤0且未过平仓线：读取 VIXMO，按 NLV 预算买入。",
            "实际分档有偏差：默认列表在 VIXMO≥15 时均命中1%。",
            "现货 VIX 的平仓门控与 VIXMO 分档是不同判断。",
            "平仓筛选也有缺口，可能对非 Call 持仓生成 SELL。",
        ],
        POST,
    )
    box(
        930,
        1315,
        810,
        244,
        "现金基金管理",
        [
            "有效现金 = TotalCashValue - 本轮队列待发生支出。",
            "可扫现金 = 有效现金 - Regime 预留。",
            "可扫现金 > 目标+买阈值：按 ask 规划买入。",
            "有效现金 < 目标-卖阈值：按 bid 规划卖出。",
            "预留只影响买入；未成交收入不提前作为可用现金。",
        ],
        POST,
    )
    arrow(870, 1437, 930, 1437)
    arrow(1335, 1559, 1335, 1610)
    box(
        60,
        1610,
        1680,
        160,
        "订单队列 → 整批检查 → 预览或提交",
        [
            "统一审核持仓覆盖、股票超卖、重复/反向订单和资金；实盘再做 what-if。",
            "模拟模式只预览；实盘先写恢复日志，再提交、调价和记录结果。",
            "批次风控不补上 Delta、VIX 分档或全策略 no_trading 的语义检查；提交成功不等于全部成交。",
        ],
    )
    text(60, 1804, "本轮复核中需特别区分的事实", 29)
    box(
        60,
        1860,
        530,
        222,
        "参数优选 ≠ 硬约束",
        [
            "Delta=0.3 时原方法可选中0.6。",
            "新增5%分批参数存在至少1张下限。",
            "roll=90%、close=99%时，",
            "盈利99.5%仍被归为展期。",
        ],
        WARN,
    )
    box(
        635,
        1860,
        530,
        222,
        "配置意图 ≠ 当前实现",
        [
            "VIXMO=40 的意图预算为0.5%，",
            "当前原方法却使用1%。",
            "Wheel 股票模式 both 加",
            "no_trading=true 仍能规划买入。",
        ],
        WARN,
    )
    box(
        1210,
        1860,
        530,
        222,
        "文档已修正 · 逻辑未变更",
        [
            "原方法调用全部使用本地模拟数据。",
            "本轮留存复核报告和原始观察。",
            "已确认的策略缺口仍待后续处理。",
            "不将规则复核等同于收益验证。",
        ],
        WARN,
    )
    text(
        60,
        2120,
        "经济循环：卖 Put → 卖方被指派取得股票 → 卖 Call → 被指派交付股票；指派由券商处理。",
        22,
        MUTED,
    )
    text(
        60,
        2161,
        "详见 docs/strategy/README.md 与 REVIEW.md；箭头表示规划顺序，不表示当轮已完成交易或指派。",
        20,
        MUTED,
    )
    svg.append("</svg>")
    (OUT / "strategy-overview.svg").write_text("\n".join(svg), encoding="utf-8")
    if image is not None:
        image.save(OUT / "strategy-overview.png")
    print(f"Generated SVG{' and 3600 × 4440 PNG' if png else ''} in {OUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--png", action="store_true", help="Also render PNG with Pillow"
    )
    export(parser.parse_args().png)
