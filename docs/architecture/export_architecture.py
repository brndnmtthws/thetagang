"""Export the current ThetaGang system architecture as SVG and PNG.

SVG: .venv/bin/python docs/architecture/export_architecture.py
PNG: use a Python runtime with Pillow and append --png.
This exporter imports no trading code and performs no broker operations.
"""

from __future__ import annotations

import argparse
import importlib
import math
from html import escape
from pathlib import Path
from typing import Any

WIDTH, HEIGHT = 1800, 2080
INK, MUTED, LINE = "#18364d", "#4e6678", "#879aaa"
WHITE, BACK, PANEL = "#ffffff", "#f6f8fa", "#fbfcfd"
ANALYSIS, RUNTIME, STRATEGY, OUTPUT = "#f1edfb", "#edf5fa", "#fff3dd", "#eaf6ef"
WARN = "#fff0eb"
OUT = Path(__file__).parent
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"


def export(png: bool) -> None:
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        "<title>ThetaGang 系统架构 · 候选分析与中文交易仪表盘</title>",
        "<desc>新增只读候选标的分析与权重建议链路。数据采集、确定性评分、压力模拟、约束分配和报告输出与交易执行边界分离。</desc>",
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

    def font(size: int) -> Any:
        assert font_module is not None
        return font_module.truetype(FONT, size * 2)

    def text(
        x: float,
        y: float,
        value: str,
        size: int = 21,
        color: str = INK,
        *,
        center: bool = False,
        max_width: float = 1680,
    ) -> None:
        anchor = "middle" if center else "start"
        svg.append(
            f'<text x="{x}" y="{y}" dominant-baseline="text-before-edge" '
            f'text-anchor="{anchor}" fill="{color}" '
            'font-family="Hiragino Sans GB, PingFang SC, Microsoft YaHei, sans-serif" '
            f'font-size="{size}">{escape(value)}</text>'
        )
        if draw is not None:
            current_font = font(size)
            bounds = draw.textbbox((0, 0), value, font=current_font)
            value_width = bounds[2] - bounds[0]
            if value_width > max_width * 2:
                raise ValueError(f"Text exceeds its column: {value}")
            left = x * 2 - (value_width / 2 if center else bounds[0])
            draw.text((left, y * 2 - bounds[1]), value, font=current_font, fill=color)

    def rect(
        x: float,
        y: float,
        width: float,
        height: float,
        fill: str,
        *,
        radius: int = 14,
        stroke: str = "#cdd8e1",
        stroke_width: int = 2,
    ) -> None:
        svg.append(
            f'<rect x="{x}" y="{y}" width="{width}" height="{height}" '
            f'rx="{radius}" fill="{fill}" stroke="{stroke}" '
            f'stroke-width="{stroke_width}"/>'
        )
        if draw is not None:
            draw.rounded_rectangle(
                (x * 2, y * 2, (x + width) * 2, (y + height) * 2),
                radius=radius * 2,
                fill=fill,
                outline=stroke,
                width=stroke_width * 2,
            )

    def box(
        x: float,
        y: float,
        width: float,
        height: float,
        title: str,
        lines: list[str],
        fill: str,
        *,
        title_size: int = 23,
        body_size: int = 19,
    ) -> None:
        rect(x, y, width, height, fill)
        text(
            x + width / 2,
            y + 16,
            title,
            title_size,
            center=True,
            max_width=width - 34,
        )
        for index, line in enumerate(lines):
            top = y + 48 + index * 27
            if top + body_size > y + height - 10:
                raise ValueError(f"Text exceeds box height: {title}")
            text(
                x + width / 2,
                top,
                line,
                body_size,
                MUTED,
                center=True,
                max_width=width - 34,
            )

    def arrow(
        points: list[tuple[float, float]],
        *,
        dashed: bool = False,
        label: str | None = None,
        label_xy: tuple[float, float] | None = None,
    ) -> None:
        path = " ".join(
            ("M" if index == 0 else "L") + f"{x} {y}"
            for index, (x, y) in enumerate(points)
        )
        dash = ' stroke-dasharray="10 8"' if dashed else ""
        svg.append(
            f'<path d="{path}" fill="none" stroke="{LINE}" stroke-width="2"{dash}/>'
        )
        (x1, y1), (x2, y2) = points[-2], points[-1]
        angle = math.atan2(y2 - y1, x2 - x1)
        head = [
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
            '<polygon points="'
            + " ".join(f"{x},{y}" for x, y in head)
            + f'" fill="{LINE}"/>'
        )
        if draw is not None:
            draw.line(
                [(x * 2, y * 2) for x, y in points],
                fill=LINE,
                width=4,
                joint="curve",
            )
            draw.polygon([(x * 2, y * 2) for x, y in head], fill=LINE)
        if label and label_xy:
            text(
                label_xy[0],
                label_xy[1],
                label,
                17,
                MUTED,
                center=True,
                max_width=260,
            )

    text(60, 34, "ThetaGang 系统架构 · 交易分析与中文复盘", 38)
    text(
        60,
        90,
        "2026-09-12  /  分析链路只读并输出建议  /  交易链路继续执行统一风控",
        21,
        MUTED,
    )
    text(1570, 49, "代码结构快照", 19, MUTED, center=True, max_width=300)

    rect(40, 145, 1280, 540, PANEL, radius=18)
    text(64, 158, "PRE-TRADE ANALYSIS · REPORT-ONLY", 18, MUTED)
    box(
        75,
        205,
        330,
        105,
        "候选标的输入",
        ["JSON 文件 / --symbols", "交易员限定范围，不扫描全市场"],
        ANALYSIS,
    )
    box(
        465,
        205,
        350,
        105,
        "CandidateDataProvider",
        ["人工 / 智能体结构化数据", "只读 IBKRCandidateDataProvider"],
        ANALYSIS,
    )
    box(
        875,
        205,
        390,
        105,
        "候选观察快照",
        ["价差 · 成交量 · OI · DTE", "IV / RV · 财报状态 · 风险组"],
        ANALYSIS,
    )
    box(
        165,
        380,
        430,
        125,
        "硬过滤与可解释评分",
        ["流动性 / DTE / 事件窗口", "收益、风险分项与淘汰原因"],
        WHITE,
    )
    box(
        725,
        380,
        430,
        125,
        "压力模拟与约束分配",
        ["压力损失 · 单张合约规模", "单标的上限 · 风险组上限"],
        WHITE,
    )
    box(
        165,
        555,
        990,
        90,
        "推荐结果 · 人工审核入口",
        [
            "Markdown 审核报告 · JSON 证据 · TOML 权重片段；不会自动提交订单或覆盖正式配置"
        ],
        OUTPUT,
    )
    arrow([(405, 257), (465, 257)])
    arrow([(815, 257), (875, 257)])
    arrow([(1070, 310), (1070, 345), (380, 345), (380, 380)])
    arrow([(595, 442), (725, 442)])
    arrow([(940, 505), (940, 555)])

    rect(40, 725, 1280, 1045, PANEL, radius=18)
    text(64, 738, "TRADING RUNTIME · ORDER PATH", 18, MUTED)
    box(
        75,
        785,
        330,
        105,
        "交易命令行入口",
        ["entry.py → main.py", "dry-run / 配置迁移"],
        RUNTIME,
    )
    box(
        465,
        785,
        800,
        105,
        "正式 TOML 配置 → Config",
        ["迁移、校验、策略阶段、标的权重与执行参数"],
        RUNTIME,
    )
    box(
        75,
        945,
        1190,
        90,
        "启动与恢复 · thetagang.py",
        ["账户锁 · 交易时段 · 历史提交及券商对账 · 单轮执行 · 断线退出"],
        RUNTIME,
    )
    box(
        75,
        1090,
        1190,
        90,
        "PortfolioManager",
        ["实际持仓 + 工作挂单 + 本轮意图；按 run 配置顺序调度阶段"],
        RUNTIME,
    )
    rect(60, 1235, 1220, 210, WHITE)
    text(84, 1248, "策略层 · strategies/（Wheel 与 Regime 互斥）", 19, MUTED)
    box(
        80,
        1295,
        350,
        115,
        "OptionsStrategyEngine",
        ["卖 Put / Call", "展期 · 平仓"],
        STRATEGY,
    )
    box(
        485,
        1295,
        350,
        115,
        "Equity / Regime",
        ["股票再平衡", "状态门控 · 动态权重"],
        STRATEGY,
    )
    box(
        890,
        1295,
        350,
        115,
        "PostStrategyEngine",
        ["VIX Call 对冲", "现金管理"],
        STRATEGY,
    )
    box(
        75,
        1500,
        560,
        105,
        "OrderOperations → Orders → BatchRisk",
        ["订单意图 · 跨策略预留", "去重、敞口、支出与保证金检查"],
        OUTPUT,
    )
    box(
        705,
        1500,
        560,
        105,
        "Trades · 提交与跟踪",
        ["先落盘再提交 · 调价再校验", "失败与未知结果向上返回"],
        OUTPUT,
    )
    box(
        75,
        1650,
        560,
        80,
        "dry-run · 只输出订单摘要",
        ["交易写操作由 IBKR 适配层阻断"],
        WHITE,
        body_size=18,
    )
    box(
        705,
        1650,
        560,
        80,
        "批次结束",
        ["完成提交不等于全部订单已经成交"],
        WHITE,
        body_size=18,
    )
    arrow([(405, 837), (465, 837)])
    arrow([(865, 890), (865, 945)])
    arrow([(670, 1035), (670, 1090)])
    arrow([(670, 1180), (670, 1235)])
    arrow([(355, 1445), (355, 1500)])
    arrow([(635, 1552), (705, 1552)], label="实盘通过", label_xy=(670, 1514))
    arrow([(355, 1605), (355, 1650)], label="dry-run", label_xy=(402, 1614))
    arrow([(985, 1605), (985, 1650)])

    box(
        1400,
        175,
        340,
        125,
        "可选事件数据源",
        ["财报日历 · 分红日", "缺失时标记未验证"],
        WARN,
    )
    box(
        1400,
        360,
        340,
        150,
        "只读分析数据",
        ["目标 DTE 近 ATM Put / Call", "行情 · OI · 成交量 · Greeks", "历史日线"],
        ANALYSIS,
    )
    box(
        1400,
        570,
        340,
        145,
        "TWS / IB Gateway",
        ["分析命令要求网关已运行", "交易由 IBC / Watchdog 管理"],
        RUNTIME,
    )
    box(
        1400,
        775,
        340,
        100,
        "Interactive Brokers",
        ["账户 · 市场数据 · 交易执行"],
        RUNTIME,
    )
    box(
        1400,
        945,
        340,
        210,
        "持久化与恢复",
        [
            "ExecutionJournal · 原子 JSON",
            "DataStore · SQLite（可选）",
            "运行 / 快照 / 意图 / 成交",
            "账户锁阻止本机并发",
        ],
        WHITE,
    )
    box(
        1400,
        1235,
        340,
        115,
        "OptionChainScanner",
        ["DTE · Delta · 价格", "供交易策略选择合约"],
        RUNTIME,
    )
    box(
        1400,
        1430,
        340,
        160,
        "IBKR 适配层 · ibkr.py",
        ["账户 / 持仓 / 行情 / 日线", "合约验证 · what-if", "下单 / 撤单 / 状态回调"],
        RUNTIME,
    )
    box(
        1400,
        1650,
        340,
        115,
        "审计与说明",
        ["架构 / 策略 / 修订记录", "候选分析报告留痕"],
        WHITE,
    )

    arrow([(1400, 237), (1305, 237), (1305, 270), (1265, 270)], dashed=True)
    arrow([(1400, 435), (1325, 435), (1325, 285), (815, 285)], dashed=True)
    arrow([(1570, 510), (1570, 570)])
    arrow([(1570, 715), (1570, 775)])
    arrow(
        [(1155, 600), (1335, 600), (1335, 830), (465, 830)],
        dashed=True,
        label="审核后复制权重",
        label_xy=(1205, 610),
    )
    arrow([(1265, 980), (1360, 980), (1360, 1048), (1400, 1048)], dashed=True)
    arrow([(1280, 1275), (1360, 1275), (1400, 1290)], dashed=True)
    arrow([(1570, 1350), (1570, 1430)])
    arrow([(1265, 1552), (1340, 1552), (1400, 1510)])
    arrow(
        [(1570, 1430), (1570, 715)],
        dashed=True,
        label="ib_async",
        label_xy=(1612, 1160),
    )

    box(
        75,
        1810,
        1665,
        135,
        "本地中文交易仪表盘 · dashboard.py → 浏览器",
        [
            "SQLite 只读事务 → 运行历史 / 订单生命周期 / 持仓快照 / 策略决策 / JSON 导出",
            "Put / Call / VIX / 现金管理写入决策事件；界面仅本机读取，不连接券商",
        ],
        OUTPUT,
    )
    arrow([(1740, 1050), (1775, 1050), (1775, 1870), (1740, 1870)], dashed=True)
    text(
        60,
        1980,
        "实线：主要数据或订单路径    虚线：只读支撑、审核复制、持久化或共享适配调用",
        19,
        MUTED,
    )
    text(
        60,
        2015,
        "边界：候选分析生成建议但不交易；正式配置经交易运行加载后，订单仍必须通过恢复与整批风险检查。",
        19,
        MUTED,
    )
    svg.append("</svg>")
    (OUT / "thetagang-architecture.svg").write_text(
        "\n".join(svg) + "\n", encoding="utf-8"
    )
    if image is not None:
        image.save(OUT / "thetagang-architecture.png")
    print(f"Generated SVG{' and 3600 × 4160 PNG' if png else ''} in {OUT}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--png", action="store_true", help="Also render PNG with Pillow"
    )
    export(parser.parse_args().png)
