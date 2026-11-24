# ThetaGang 交易策略分析（复核版）

复核日期：2026-09-10。代码基线：`5db001b261875d9e118f839dc7b2622243ae0633`。本文按当前 Python 分支和离线调用结果说明行为，替代上一版的相关表述。完整差异、未修复事项与证据见[复核报告](REVIEW.md)。

## 1. 策略定位与当前配置

工程有两种主要路线：**改良 Wheel** 使用卖 Put、持有股票、卖 Call 和空头期权展期来围绕配置权重管理敞口；**Regime 再平衡** 根据权重偏离、市场状态、波动率和资金缺口买卖股票。两者是可选路线，不能画成一轮运行中必然相继执行的策略。VIX Call 对冲和现金基金管理可附加在后面。

| 仓库 thetagang.toml | 实际含义 |
| --- | --- |
| run.strategies 选择 wheel、vix_call_hedge、cash_management | 选择 Wheel 阶段及两个后置阶段 |
| VIX enabled=false、现金管理 enabled=false | 后置阶段进入方法后跳过，不产生相应订单 |
| Regime 未列入 run，模型默认 enabled=false | 当前示例不运行 Regime |
| Wheel 股票执行模式默认 off | 只有配置 buy_only / sell_only / both 的标的才直接再平衡股票 |
| margin_usage=0.5 | Wheel 目标资金基准是 NLV 的50%，不是券商实时 BuyingPower 的50% |

支持某策略不等于正在交易该策略；后置策略需要“阶段被选中”和“enabled=true”。以上是仓库示例的状态，不是对用户其他配置或实际账户的判断。

## 2. 执行顺序、组合与互斥

使用 `run.strategies` 时，所选策略按以下规范顺序展开，未选中的阶段略过：

| 规范序号 | 阶段 | 所属路线 |
| ---: | --- | --- |
| 1 | options_write_puts：计算目标股数，规划卖 Put | Wheel |
| 2 | options_write_calls：规划卖 Call | Wheel |
| 3 | equity_regime_rebalance：Regime 股票再平衡 | Regime |
| 4 | equity_buy_rebalance：股票补仓 | Wheel 股票模式 |
| 5 | equity_sell_rebalance：股票减仓 | Wheel 股票模式 |
| 6–7 | options_roll_positions / options_close_positions：管理已有空头期权 | Wheel |
| 8 | post_vix_call_hedge | 可选后置 |
| 9 | post_cash_management | 可选后置 |

**显式 run.stages 保留用户填写的顺序**，管理器按 run_stage_order 执行，并非再次强制排序。depends_on 校验已填写的依赖顺序；Call 阶段虽要求 Put 阶段也被启用，但缺省 depends_on 时，当前校验仍允许 Call 写在 Put 前面，届时 Call 可能拿不到当轮目标数量。

策略级 wheel 与 regime_rebalance 互斥；显式阶段禁止 Regime 与 Wheel 四个期权阶段共存，但允许它与股票买卖阶段共存，股票引擎会排除配置为 Regime 的标的。`regime.enabled && shares_only` 阻止管理器的期权写入、展期和普通平仓阶段，**不会阻止独立 VIX Call 对冲方法**。

同轮所有订单先规划、后统一提交。各阶段用实际持仓、工作挂单剩余量和已规划订单形成预计组合；管理阶段不把新计划仓位当作可以再次展期的实际仓位。规范顺序中同时启用 roll 和 close 时，管理器在 roll 阶段合并处理两者，之后跳过重复 close 阶段。

## 3. Wheel：仓位驱动的期权策略

### 3.1 目标敞口和卖 Put

```text
B = floor(NetLiquidation × wheel_margin_usage)
T = floor(round(标的权重 × B, 2) / 标的价格)
新增 Put 候选张数 = floor((T - 当前股票数 - 100 × 已计入空头 Put 数) / 100)
```

wheel_margin_usage 优先取策略 risk 配置，否则回退到账户配置。代码用每张100股估算 Wheel 数量，不按 Delta 折算等效股票敞口。默认只计空头合约；calculate_net_contracts=true 时，才按期限与行权价条件匹配多空腿。Wheel 不负责买入或维护这些保护性多头腿。

候选数必须为正、标的可交易、股票买入模式未启用，并通过 green/red 与当日价格变动阈值。默认 Put 允许下跌方向；价格等于前收盘时两个方向拦截均不触发，仍取决于变动阈值。阈值比较的是绝对价格变化，百分比阈值会先乘前收盘价，sigma 阈值会先换算为价格变化。

新增数量限制按 maximum_new_contracts_percent × B 和标的价格估算，**结果至少为1张**。5%是分批参数，不能理解为严格不超过5%的名义敞口上限。例如 B=10,000、股价=100，5%只有500，但函数仍返回最多1张，对应股票名义金额10,000；仓位缺口及批次风控仍可能阻止订单。

### 3.2 股票覆盖和卖 Call

Call 使用 Put 规划阶段记录的目标股数，普通模式目标空头张数为：

```text
保留不覆盖张数 = floor(目标股数 × cap_target_floor / 100)
覆盖比例上限 = floor(当前股数 × cap_factor / 100)
目标 Call 张数 = max(0, min(覆盖比例上限, floor(当前股数/100) - 保留不覆盖张数))
```

excess_only=true 时改用 `max(0, floor((当前股数 - 目标股数)/100))`，忽略上述两个 cap 参数。减去已有空头 Call 后得到新增量，再应用新增合约限制。

Call 写入要求交易允许、股票卖出模式未启用、方向与价格变动门控通过；还可要求仓位占 NLV 或超目标比例达到阈值。**新写入** Call 的行权价下限取配置下限、股票平均成本和现价的最大值并向上取整；展期 Call 不保证高于现价，另受成本和可选高水位限制。

### 3.3 合约筛选：Delta 是优选条件，存在回退

OptionChainScanner 的实际步骤：

1. 选择 DTE≥目标、不早于旧到期日、且不大于 max_dte 的最早若干到期日。DTE 是下限，不是“距离目标最近”。
2. 限制行权价并截取配置数量。无 Put strike_limit 时允许 K≤1.05×现价，因此不保证价外；无 Call 下限时允许 K≥0.95×现价。
3. 价格必须严格大于 minimum_price；Put 还检查 K≤期权价格+标的价格。过滤最低未平仓量。
4. 通常先选 abs(delta)≤target_delta，优先最近到期，同到期优先较大的绝对 Delta。
5. **没有合格 Delta 候选且 minimum_price 非零时，转用先前被 Delta 拒绝的候选**，包括 Delta 超限或缺失，再按到期和较小绝对 Delta 排序。离线原方法在目标0.3时选中了0.6。

Wheel 和 VIX 共用扫描器；最低未平仓量取 Wheel 全局配置，VIX 未设置 max_dte 时也会回退到公共目标配置。Delta 不能标为独立硬风控。

### 3.4 管理空头期权：先判展期，再判平仓

只检查空头 Put/Call，排除 VIX。P&L比例为 `unrealizedPNL / abs(averageCost × position)`，是单个持仓相对成本的盈亏，不是账户收益率。

实际判断顺序：

1. 标的允许交易；ITM 且 always_when_itm=true 时直接列为可展期，越过后续超额仓位、旧合约 max_dte 和 P&L 门控。
2. 否则按 itm、has_excess、旧合约 roll_when.max_dte 判断是否允许。
3. (DTE≤roll_when.dte 且 P&L≥min_pnl) 或 P&L≥roll_when.pnl，列为可展期。
4. **仅不属于可展期者**才检查 P&L>close_at_pnl，列为可平仓。模型默认 close_at_pnl=1.0，比较为严格大于。

设置 roll=90%、close=99%时，盈利99.5%的空头仍列为展期。即使只开 close 阶段，分类也先判展期，该持仓可能既不展期也不平仓。这是当前行为，并非“达到止盈直接平仓”。

展期为 BUY 旧合约 + SELL 新合约的 BAG 组合。新到期日不得早于旧到期日，但允许同到期换行权价；排除同到期同执行价的原合约。Call 受成本及可选旧执行价高水位限制；Put 上限结合配置、旧执行价、原权利金和回购价计算，ITM Put 再限制不高于旧执行价。

credit_only=true 要求净信用满足最低要求；false 时优先找覆盖回购成本的合约，找不到也可能付出净支出来展期。旧 DTE>roll_when.dte 时数量受分批限制，否则用完整旧数量。

找不到替代合约时，当前代码要求 **close_if_unable_to_roll=true、roll_when.max_dte 非空、旧 DTE≤它且 P&L>0**，才返回待平仓候选；最终还需启用 close 阶段。只设置 target.max_dte 不满足这个条件。

经济上的 Wheel 循环是“卖 Put → 被指派后买入股票 → 卖 Call → 被指派后交付股票 → 再考虑卖 Put”。指派由券商处理；买回 Call 本身不会卖出股票。默认 ITM Put 不展期，但仍可能符合平仓规则，也不保证一定被指派。

## 4. 直接股票再平衡

股票策略按同样的目标股数计算差额，支持 off / buy_only / sell_only / both。`both` 同时进入买卖检查，也同时抑制新卖 Put 和新卖 Call；已有空头期权仍可进入管理阶段。普通 off 标的不会在这两个阶段直接再平衡。

股票订单受最小股数、金额、占 NLV 比例及相对偏离阈值过滤。单标的买入使用目标资金 B 作粗略容量判断，最终由整批检查审查总体资金。

**已确认缺口**：Wheel 股票买卖方法没有调用 trading_is_allowed；symbol.no_trading=true、启用 both 后仍可规划买入。不能把它描述为全策略禁交易开关。本次仅记录，未修改代码。

## 5. Regime：市场状态、动态仓位与再平衡

### 5.1 资金基准与不归一化的有效权重

| weight_base | 实际资金基准 B |
| --- | --- |
| net_liq | floor(NLV × regime_margin_usage) |
| net_liq_ex_options（默认） | floor((NLV − 所有 Option 的有符号 marketValue 之和) × regime_margin_usage) |
| managed_stocks | 受管理标的的股票市值合计，不再乘 margin_usage |

空头期权市值为负，因此 net_liq_ex_options 扣除它会增大基准，不是扣除期权保证金。

可选波动率权重按 `基础权重 × target_vol / 实现年化波动率` 计算，应用最小/最大权重、调整带和增减仓平滑系数。上一轮权重来自可选数据库，缺失时从基础权重起算。权重历史不足或异常时回退静态权重。

**目标金额直接使用有效权重 × B，不再把权重归一到总和1。** 总和<1可留下现金，总和>1会增加目标敞口并与资金缺口处理相互作用。归一化用于代理组合指数、ratio basket 和部分缺口分摊，不用于重算目标金额。例如受管子集权重0.4、0.3，B=10,000、价格均100，目标40、30股，合计7,000，不会变为约57、42股。

### 5.2 门控和实际优先顺序

每标的 `relative_drift = abs((当前市值/B)/有效目标权重 − 1)`。软带10%指相对偏离：目标权重40%、当前44%，即达到10%相对偏离。

代理组合以当前已投资标的的市值比例建立历史收益指数，无投资时回退有效目标权重。默认历史40个交易日；震荡指标为 sqrt(sum(log_return²))/abs(sum(log_return))，效率指标为 abs(末值−初值)/sum(abs(每日变化))，分母有 eps 保护。默认要求前者≥3且后者≤0.30；并非直接调用标准技术指标 CHOP。

软/Flow 门控 = 市场状态通过 + 冷却期结束 + 可选 ratio gate 通过。默认冷却5个交易日，日历失败才回退自然日。ratio gate 判断其他篮子相对 anchor 的收益漂移 t 值和年化波动率；enabled=false 是计算并记录的影子模式。

| 优先级 | 条件与动作 |
| ---: | --- |
| 1 Hard | 任一标的相对偏离达到 hard_band，按 fraction 向目标回归；绕过软门控 |
| 2 Soft | 任一偏离达到 soft_band 且软门控通过，按目标再平衡 |
| 3 Deficit | 前两者未触发且投资超过基准达到 start；优先卖超配，必要时低于目标卖出，把缺口降至 stop |
| 4 Flow | 前述分支未触发，超额资金或较小缺口达到 Flow 阈值且软门控通过；股数净缺口方向一致性须严格大于 tau |

Hard/Soft 后可能追加 deficit 清理。默认 Flow start/stop=2.5%/1.25%，Deficit=6%/3%；有数据库状态时保留启动/停止的滞回。Flow 比较**股数缺口**而不是金额缺口，可买也可卖。

Hard/Deficit 绕过市场门控，但仍需前面价格、历史校验通过，也受 no_trading、买卖方向和最小订单阈值限制；不是无条件执行。核心历史不足向管理器抛错，会使本轮在提交前结束，不是只静默跳过 Regime。

仅最终模式为 flow、excess_cash>0 且过滤后确实有买单时，才把未用完余量预留给现金管理。普通降波动留下的现金不会自动获得这项预留。

## 6. VIX Call：配置意图与当前实现偏差

enabled=true 时才运行。已有净多 Call 时只检查现货 VIX 是否严格高于平仓线，没过线就持有，不持续按权重补足。净多数量≤0且未过平仓线时，才读 VIXMO 确定预算。预算按 NLV 计算，张数为 floor(预算/价格/乘数)。

| VIXMO | allocation 配置意图 | 当前默认列表命中权重（未被现货 VIX 门控拦截时） |
| --- | ---: | ---: |
| <15 | 0% | 0% |
| 15–<30 | 1% | 1% |
| 30–<50 | 0.5% | **1%** |
| ≥50 | 0% | **1%** |

原因：双边区间判断失败后又进入仅检查 lower_bound 的 elif，第二档 lower=15 截获全部≥15的值。现货 VIX 与 VIXMO 是不同输入，不能认为平仓线必然抵消错误。

另有平仓筛选问题：代码只排除“空头 Call”，随后对其他 VIX 持仓生成 SELL；同时持有空头 VIX Put 时也会生成 SELL Put，方向不是平仓。离线原方法已确认这种规划行为，未向券商发单。

ignore_dte>0 时不计 DTE≤该值的合约，可允许新旧保护重叠；为0时仍计入到期当日合约，仅排除已过期者。VIX 多头由本方法处理，普通 Wheel 管理排除 VIX。

## 7. 现金管理与共用执行边界

```text
C_effective = floor(TotalCashValue) − 本轮队列全部待发生支出
C_sweep = C_effective − Regime 预留现金
```

未成交卖出收入和期权贷记不抵扣支出。买入条件是 C_sweep>target+buy_threshold；卖出条件是 C_effective<target−sell_threshold。预留额只影响扫入，不阻止现金不足时卖基金。示例阈值10,000是金额，不是百分比。

买基金使用 ask，卖基金使用 bid，限价不保证成交；卖出受传入的预计股票持仓限制。当前卖出数量在负数整除后再减1，可能比简单向上取整多卖一股，因此不保证精确恢复目标现金。

批次检查审核重复/反向订单、数量价格、股票超卖、Call 覆盖与资金，实盘再做 what-if。它**没有补上 Delta 上限、VIX 区间或全策略 no_trading 的语义检查**。策略生成订单不等于必然提交，提交确认也不等于全部成交。

## 文件与依据

- [PNG](strategy-overview.png) / [SVG](strategy-overview.svg) / [Mermaid](strategy-overview.mmd)
- [完整下载包](thetagang-strategy-pack.zip)
- [复核报告与代码定位](REVIEW.md)
- [原方法离线观察](review-observations.json)及[可重复运行脚本](verify_strategy_review.py)

本文基于配置解析、管理器、策略引擎、扫描器及数量函数。未以项目 README 中的收益叙述作为实证，也未做回测或实盘绩效评估。本次修改范围为图文和复核证据。
