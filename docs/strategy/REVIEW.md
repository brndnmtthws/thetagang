# 交易策略复核报告

日期：2026-09-10；复核基线 `5db001b261875d9e118f839dc7b2622243ae0633`。依据为当前源码、配置模型、仓库示例及实际方法的离线调用，未连接 IBKR。本轮修正图文并归档证据，生产策略代码没有修改。上一轮报告把若干配置意图直接描述成实现保证，以下按实际行为纠正。

## 一、影响策略理解的主要修正

| 编号 | 上一版描述或遗漏 | 当前代码与复核结论 | 代码依据（基线行号） |
| --- | --- | --- | --- |
| SR-01 | 阶段顺序固定；Call 的 Put 前置条件被完整保证 | 只有 strategies 模式规范排序；显式 stages 保留填写顺序。仅检查 Put 启用，不自动保证其排在 Call 前 | config.py:128、:207、:223；portfolio_manager.py:762 |
| SR-02 | 示例已启用 VIX 与现金管理 | 它们列入阶段，但各自 enabled=false；方法直接跳过。Wheel 股票模式默认 off | thetagang.toml:704、:756；config.py:524 |
| SR-03 | Delta 是合约硬上限 | 无合格候选且最低价格非零时，回退到 Delta 拒绝集合；目标0.3可选到0.6，Delta 缺失也在拒绝集合中 | trading_operations.py:254、:335 |
| SR-04 | 新增合约名义敞口不超过配置百分比 | 分批数量计算至少返回1张；B=10,000、股价=100、比例5%时仍返回1 | portfolio_manager.py:931 |
| SR-05 | 达到 close_at_pnl 可以直接止盈；展期失败可按开关关闭 | 先归类展期，else 才检查止盈；close-only 阶段也沿用此分类。失败转平仓另要求 roll_when.max_dte 非空且盈利。always_when_itm 越过普通展期限制 | options_engine.py:776、:798、:960、:999、:1260；strategies/options.py:93 |
| SR-06 | Regime 有效权重重新归一化 | 目标金额直接乘有效权重；总和可小于1或大于1。managed_stocks 基准也不乘 margin_usage | regime_engine.py:628、:960、:997、:1006 |
| SR-07 | Flow 只处理超额现金，硬带无条件执行 | Flow 可处理小额缺口且仍需软门控，方向一致性按股数判断；Hard/Deficit 仍需前置数据和最后执行过滤。核心历史异常终止本轮 | regime_engine.py:1037、:1094、:1121、:1150、:1303、:1560 |
| SR-08 | VIX 按 [15,30)、[30,50) 正确分档 | 双边区间失败后退到仅 lower_bound 判断；默认列表在 VIXMO≥15 时全部命中1%，30以上档位被遮蔽 | post_engine.py:127 |
| SR-09 | VIX 平仓只卖出多头 Call | 只排除空头 Call，而非仅保留多头 Call。混合 VIX 持仓可对空头 Put 生成 SELL，方向会增加空头 | post_engine.py:90、:97 |
| SR-10 | 股票只有 buy-only / sell-only，交易允许开关普遍有效 | both 同时进入买卖检查并抑制新 Put/Call；Wheel 股票方法不检查 no_trading，仍可规划订单 | config.py:549；equity_engine.py:118、:345 |
| SR-11 | shares_only 关闭全部期权、ignore_dte=0 排除到期日 | shares_only 屏蔽 Wheel 写入、展期、平仓，不屏蔽后置 VIX；ignore_dte=0 仍计入到期日合约 | portfolio_manager.py:297、:746、:763；util.py:148 |
| SR-12 | Put 是价外；平 Call 改变股票持仓；现金精确回到目标 | 无 Put 执行价上限时允许现价105%以内；买回 Call 不出售股票。现金买/卖使用不同余额，负数整除后再减1可多卖一股 | trading_operations.py:166；options_engine.py:1063；post_engine.py:191、:222 |

以上路径相对 `thetagang/`，其中 options_engine、regime_engine、post_engine、equity_engine 均位于 `strategies/` 子目录；行号用于定位复核基线。更新后的[策略说明](README.md)补充了公式、阶段组合和配置作用域，图中也避免把 Wheel 与 Regime 串成同一路线。

## 二、已确认但本次未修复的策略问题

以下是局部方法输出证据；它们不是发生过真实交易事故的证明。订单是否能通过最终风控和券商检查，仍取决于账户及合约数据。

| 事项 | 具体触发与影响 | 本轮状态 |
| --- | --- | --- |
| VIX 档位上界失效 | VIXMO=40，本应按配置0.5%预算，却进入1%预算 | 原方法复现；修正文档；交易代码未改 |
| VIX 平仓持仓筛选不完整 | 一张多 Call + 一张空 Put，超过 VIX 平仓线时生成 SELL Call 和 SELL Put；后者增加空头 | 原方法复现；修正文档；交易代码未改 |
| Wheel 股票忽略 no_trading | SPY.no_trading=true、股票模式both、目标40股且当前0股，仍规划买40股 | 原方法复现；修正文档；交易代码未改 |
| 止盈被展期分类截获 | 盈利99.5%、roll=90%、close=99%，close-only 也没有得到待平仓候选 | 原方法复现；修正文档；交易代码未改 |
| 失败转平仓额外依赖 roll.max_dte | 相同盈利合约、找不到替代合约：max_dte=None 不返平仓候选，180时返回 | 原方法复现；修正文档；交易代码未改 |
| Call 前置顺序缺少自动校验 | 无 depends_on 时可接受 Call 在 Put 前面，Call 目标计算依赖可能缺失 | Config 实例验证；修正文档；交易代码未改 |

Delta 回退和“至少1张”分批限制在代码中是明确写出的规则。本轮将其作为需要理解的风险边界，不擅自认定必须删除。Regime 有效权重不归一化是已有测试明确覆盖的设计行为，不能作为缺陷去“修正”。

## 三、离线验证与可重现证据

[verify_strategy_review.py](verify_strategy_review.py) 直接调用 Config、OptionsStrategyEngine、PostStrategyEngine、OptionChainScanner、RegimeRebalanceEngine、EquityRebalanceEngine 等现有实现；券商读写、行情和历史数据由本地 Mock 替代。未创建 IB 连接，未使用真实账户或下单。它记录**当前观察**，不是要让未来修复继续保留这些问题的回归标准；行为修复后应复核并更新观察断言。

执行方式（仓库根目录）：

```sh
.venv/bin/python -m docs.strategy.verify_strategy_review
.venv/bin/pytest -q tests/test_regime_rebalance.py tests/test_config_new.py tests/test_strategy_stage_runners.py tests/test_post_strategy_engine.py tests/test_trading_operations.py tests/test_util.py tests/test_portfolio_manager.py
```

- 12组观察结果及断言通过，原始数据见 [review-observations.json](review-observations.json)。其中 VIX 分档覆盖14、15、29、30、40、50、60；固定 NLV=100,000、期权价格2、乘数100，隔离测试关闭现货 VIX 平仓门控，以单独检查分档。
- 已有相关测试 **147 passed**。包括有效权重不归一化、Flow 需市场/ratio门控、缺口轨、显式阶段顺序和后置阶段。
- 现有测试通过不代表上述未覆盖边界没有问题；本轮没有把局部复现包装成全量回归或实盘验证。
- 图由同一生成脚本导出 SVG 与 PNG；PNG 渲染时检查文字宽度与框高，随后视觉检查中文和连线。下载包重建时逐字节比对归档与当前源文件。
- 导出/观察脚本的 Ruff、格式、类型检查以及 git diff --check 通过；命令与范围见 [review-validation.txt](review-validation.txt)。Mermaid 源文件按代码分支核对，本次未运行 Mermaid 渲染器。

## 四、策略层面的重新判断

Wheel 以持仓目标和空头期权管理为中心，不能仅用“赚时间价值”概括其收益和风险：数量按每张100股衡量而非 Delta 等效，Call 覆盖、指派以及展期的净借记都会改变持仓结果。Regime 同时有波动率调仓位和再平衡两层逻辑，并非固定权重的震荡套利器。VIX 是条件式单批保护，已有净多仓位时不会不断按目标百分比补足；现金基金是资金调度层。

因此，上一版的系统级分类基本成立，但不足以作为准确操作手册。复核版应以“配置作用域、真实分支和已确认缺口”为解释基础。规则分析不能替代手续费、滑点、指派、路径依赖和极端行情下的回测或实盘绩效验证；本轮没有对收益率或回撤作量化结论。
