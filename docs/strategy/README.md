# ThetaGang 交易策略分析

本文根据当前代码的实际执行路径整理，描述的是程序“会怎样做”，不是收益承诺或投资建议。分析范围包括 `wheel`、`regime_rebalance`、`vix_call_hedge` 和 `cash_management` 四类配置策略，以及它们在一次运行中的阶段顺序、互斥关系和共同资金约束。

## 一句话概览

ThetaGang 以配置中的目标权重和账户购买力为基准，先规划期权写入与股票仓位，再管理已有期权，最后处理 VIX 尾部对冲和现金基金扫款。所有阶段都读取同一份账户快照，并把工作挂单和本轮订单意图纳入预计组合；订单进入统一队列后，才经过整批风险检查和提交。

## 一次运行的实际顺序

`PortfolioManager.manage()` 按 `CANONICAL_STAGE_ORDER` 调度阶段。使用 `run.strategies` 时，配置策略只负责映射到这些阶段；使用 `run.stages` 时，可以显式选择阶段，但仍要满足依赖与互斥校验。

| 顺序 | 阶段 | 主要决策 | 订单方向 |
| ---: | --- | --- | --- |
| 1 | `options_write_puts` | 按目标权重计算需要的标的敞口，决定是否卖出 Put | 卖出 Put |
| 2 | `options_write_calls` | 按股票数量、目标数量和 Call 上限，决定是否卖出 Covered Call | 卖出 Call |
| 3 | `equity_regime_rebalance` | 计算动态权重、漂移和市场状态门控 | 买入/卖出股票 |
| 4 | `equity_buy_rebalance` | 对 buy-only 标的补足目标数量 | 买入股票 |
| 5 | `equity_sell_rebalance` | 对 sell-only 标的减至目标数量 | 卖出股票 |
| 6 | `options_roll_positions` | 按 ITM、DTE、P&L、超额仓位和信用条件展期 | 组合单：买旧卖新 |
| 7 | `options_close_positions` | 按止盈或展期失败规则平仓 | 买入平空 / 卖出平多 |
| 8 | `post_vix_call_hedge` | 依据 VIXMO 区间配置尾部对冲，或在 VIX 过高时平仓 | 买入/卖出 VIX Call |
| 9 | `post_cash_management` | 依据目标现金、阈值和 Regime 预留资金扫入或扫出现金基金 | 买入/卖出现金基金 |

若使用默认示例配置 `strategies = ["wheel", "vix_call_hedge", "cash_management"]`，实际启用的是 Wheel 的期权与股票阶段，加上两个后置阶段。`regime_rebalance` 不能和 Wheel 的期权阶段同时启用；Regime 的 `shares_only` 还会使期权写入和展期被跳过。

## 1. Wheel：用期权逐步获得或管理标的仓位

### 1.1 Put 写入：把“还需要的股票”转换成期权卖方敞口

对每个配置标的，程序先计算：

```text
目标金额 = 标的权重 × 账户购买力
目标股数 = floor(目标金额 / 当前价格)
需要的 Put 股数 = 目标股数 - 当前股票数 - 100 × 净空头 Put 数量
需要卖出的 Put 张数 = floor(需要的 Put 股数 / 100)
```

实际代码还会应用以下门控：

- 标的必须允许交易，且不能是 buy-only 再平衡标的。
- `write_when.puts.green/red` 决定上涨或下跌日是否允许卖 Put。默认示例为只在下跌日卖 Put。
- 当日绝对涨跌幅必须达到配置的写入阈值；阈值可以来自百分比或波动率计算。
- 新增张数受 `maximum_new_contracts_percent` 对购买力的上限约束。
- 期权扫描器按目标 DTE、最大 DTE、Delta、行权价方向、价格下限和最低未平仓量筛选合约。

Put 扫描的行权价通常要求不高于 `strike_limit`；如果没有显式限制，则使用接近现价的下方范围。最终生成卖出限价单，订单先入队，不在该方法内直接提交。

### 1.2 Call 写入：股票到位后限制覆盖比例

Call 规划使用当前股票数、此前 Put 阶段算出的目标股数以及 `get_target_calls()` 计算目标空头 Call 数量。目标会受到 `cap_factor`、`cap_target_floor` 和 `excess_only` 影响：

- `cap_factor` 控制最多覆盖当前股票的比例。
- `cap_target_floor` 保留一部分目标股票不被覆盖，以减少上涨时被全部锁定的可能。
- `excess_only` 只在股票数超过目标时卖 Call。

Call 写入还要求：标的可交易、不是 sell-only 再平衡标的、上涨/下跌方向门控通过、当日变动达到阈值，并可选地要求当前仓位达到 NLV 百分比或相对目标超额阈值。新 Call 的行权价至少不低于股票平均成本和当前价格，避免把普通覆盖仓位直接卖在成本线下方。

### 1.3 期权管理：展期优先，满足条件时止盈平仓

程序只自动管理空头期权；多头期权不会被 Wheel 管理。每次管理阶段先分别找出可展期和可平仓的 Put/Call：

- ITM 处理由 `roll_when.puts/calls.itm` 和 `always_when_itm` 控制。`always_when_itm` 会优先触发展期。
- 若当前 DTE 小于等于 `roll_when.dte`，且 P&L 不低于 `min_pnl`，可以展期。
- 即使 DTE 尚未达到阈值，只要 P&L 达到 `roll_when.pnl`，也可以展期。
- 超过 `roll_when.max_dte` 的合约不会继续触发普通展期。
- `close_at_pnl` 可以在达到指定收益后直接平仓。
- 配置 `credit_only` 时，新的展期组合必须满足最低净信用；找不到合约时，可按 `close_if_unable_to_roll` 及盈利条件平仓。

展期会构造 BAG 组合单：买回旧合约，同时卖出新合约。Call 新行权价会受平均成本和可选高水位约束；Put 新行权价会受到旧行权价、现价和信用条件约束。尚未达到展期 DTE 的仓位还会受新增张数上限限制，接近到期时才允许完整数量展期。

## 2. 股票再平衡：买入和卖出方向可以拆开

Wheel 模式下，`equity_buy_rebalance` 和 `equity_sell_rebalance` 只处理被标记为 buy-only 或 sell-only 的股票，并排除 Regime 标的。两者都用“标的权重 × 购买力”得到目标金额，再换算目标股数；实际订单还会经过最小股数、最小金额、NLV 百分比和相对偏离阈值过滤。

这种拆分的效果是：

- buy-only 标的只补仓，不会因为低于目标而卖出。
- sell-only 标的只减仓，不会因为低于目标而买入。
- 普通 Wheel 标的不在这两个阶段直接做股票再平衡，主要靠 Put 被接 assignment 或 Call 被平仓/行权改变仓位。

## 3. Regime-aware rebalancing：用市场状态决定是否做软再平衡

Regime 模式把目标股票组合单独作为一个再平衡策略。它首先选择资金基准：`net_liq`、`managed_stocks` 或 `net_liq_ex_options`，再结合 `regime_margin_usage` 得到目标总金额。之后可按历史波动率调整有效权重并归一化。

每个标的的相对偏离近似为：

```text
relative_drift = abs((当前权重 / 有效目标权重) - 1)
```

决策分为四类：

1. **软再平衡**：偏离达到 `soft_band`，同时历史代理组合满足 `choppiness >= choppiness_min`、`efficiency <= efficiency_max`，冷却期已结束，且可选 ratio gate 通过。
2. **硬再平衡**：偏离达到 `hard_band`，直接越过市场状态和冷却门控；可用 `hard_band_rebalance_fraction` 只回归一部分。
3. **Flow 模式**：存在超过 `flow_trade_min` 的超额现金，或已有 Flow 状态且超过 `flow_trade_stop`；只有净买入/净卖出方向占总缺口达到 `flow_imbalance_tau` 才下单。
4. **Deficit 安全轨**：投资额超过基准并达到 `deficit_rail_start`；优先卖出超配仓位，必要时允许跌破目标以补足资金缺口。

Regime 阶段会把正向 Flow 交易后仍需使用的现金预留给后置现金管理，避免现金基金阶段抢先使用这部分资金。它还可以通过每标的执行策略限制只买、只卖或关闭某一方向。

## 4. VIX Call 对冲：按 VIXMO 分层买入，极端时平仓

VIX 对冲是后置阶段，不参与普通 Wheel 的 Put/Call 计数。逻辑分两条：

- 如果已有净多 VIX Call，且现货 VIX 高于 `close_hedges_when_vix_exceeds`，则卖出多头 VIX Call；临近到期且小于等于 `ignore_dte` 的仓位可以不计入净持仓。
- 如果没有净多 VIX Call，读取 30 日 VIXMO，按 allocation 列表从上到下寻找第一个匹配区间，以 `NetLiquidation × weight` 作为预算，再按目标 Delta 和目标 DTE 扫描并买入 VIX Call。

示例配置的默认区间是 VIXMO 低于 15 不分配，15–30 分配 1%，30–50 分配 0.5%，50 以上分配 0%。这使它表现为成本可控但带有时间损耗的尾部保险层。

## 5. Cash management：保留现金阈值，剩余资金买现金基金

现金管理阶段计算：

```text
有效现金 = 账户 TotalCashValue + 本轮待发生现金变动
可扫现金 = 有效现金 - Regime 预留现金
```

当可扫现金高于目标现金加 `buy_threshold` 时，按卖价买入现金基金；当有效现金低于目标现金减 `sell_threshold` 时，按买价卖出现金基金。卖出数量不会超过实际持有数量。未成交卖单或未成交期权贷记不会被当作可用收入提前扫入。

## 6. 共同约束和策略交互

- **配置互斥**：`wheel` 与 `regime_rebalance` 不能同时启用；显式阶段配置还会检查 `options_write_calls` 对 `options_write_puts` 的前置依赖。
- **同一账户视角**：购买力、NLV、持仓、期权敞口和待执行订单来自同一账户，多个策略会共享资金池。
- **阶段顺序影响结果**：前面阶段生成的订单意图会进入后面阶段的预计组合；管理阶段不会把刚规划但尚未成交的新仓位当作已存在仓位去展期。
- **合约筛选集中**：Wheel 和 VIX 共用 `OptionChainScanner`，筛选条件包含 DTE、最大 DTE、Delta、价格、行权价、未平仓量和排除的旧合约。
- **统一风险边界**：所有策略只生成订单意图，随后由整批风险检查、模拟模式边界、实盘 what-if、恢复日志和订单状态跟踪统一处理。
- **单次运行语义**：连接后完成一轮 `manage`，提交/调价/记录后退出；券商仍可能保留未成交工作单，下一次运行会先对账。

## 7. 从代码角度看最关键的风险暴露

这些是策略本身的行为特征，不是本轮要实施的优化项：

1. 卖 Put 仍然承担标的下跌和被接 assignment 的风险；目标权重只是仓位计算基准，不是损失上限。
2. 卖 Call 会限制上涨空间；`cap_factor` 与 `cap_target_floor` 决定限制程度，ITM Call 的展期规则会进一步影响是否被行权。
3. 展期不是自动消除风险，而是把旧到期日和行权价替换成新合约；信用条件、最大 DTE 和可用链条可能导致无法展期。
4. Regime 的软再平衡依赖完整历史数据和市场状态指标；数据不足会中止该策略本轮决策，硬轨和资金缺口轨是独立的安全护栏。
5. VIX Call 是有成本的尾部保护；没有匹配的 allocation 或合约时，程序会跳过而不是强行下单。
6. 现金管理会改变账户持有的现金等价物，并依赖可用的卖出流动性；它不是收益保证，也不替代保证金管理。

## 图和源文件

- [策略全景图 SVG](strategy-overview.svg)：适合放大、打印和下载。
- [Mermaid 源文件](strategy-overview.mmd)：便于后续维护和二次渲染。
- [图与说明打包下载](thetagang-strategy-pack.zip)：包含本文、SVG 和 Mermaid 源文件。

这次新增只包含策略分析、图和文档留痕，没有修改交易逻辑、配置默认值或券商交互行为。
