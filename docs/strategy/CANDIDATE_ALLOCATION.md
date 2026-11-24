# 候选标的评估与权重建议

## 目标

交易员输入有限的候选标的，工具对每个标的进行可审计的硬过滤和风险调整评分，再在单标的上限、风险组上限和合约规模约束下给出权重建议。它只生成建议和配置片段，不提交订单，也不会自动替换现有配置。

当前第一版将数据采集与计算解耦。候选数据可以来自人工、回测程序或未来的 IBKR 数据适配器；相同的评分和权重算法会得到相同结果，便于复核和留痕。

## 使用方式

准备 JSON 文件，格式可参考 [`data/candidate-analysis.example.json`](../../data/candidate-analysis.example.json)。示例数据是合成数据，不能直接用于交易。

```bash
uv run thetagang-analyze-candidates \
  --input data/candidate-analysis.example.json \
  --output data/candidate-analysis.md \
  --json-output data/candidate-analysis.json \
  --toml-output data/candidate-symbols.toml
```

如果 TWS/IB Gateway 已经运行，也可以让工具以只读模式采集候选标的：

```bash
uv run thetagang-analyze-candidates \
  --ibkr \
  --config thetagang.toml \
  --symbols SPY,QQQ,TLT \
  --risk-groups SPY=us_equity,QQQ=us_growth,TLT=rates \
  --market-data-type 3 \
  --capital 100000 \
  --output data/candidate-analysis.md
```

`--ibkr` 不启动 IBC，也不读取或提交交易订单；它使用配置中的
`runtime.watchdog` 连接参数，要求对应的 TWS/IB Gateway 已经接受 API 连接。
可以用 `--risk-groups SPY=us_equity,QQQ=us_growth,TLT=rates` 提供相关性分组；
省略时每个标的临时单独成组，并在报告中警告相关性上限未验证。
财报日期暂时没有默认的 IBKR 日历适配器，因此报告会把该项标成未验证。

候选分析默认读取 `runtime.account.market_data_type`，也可用
`--market-data-type` 只覆盖本次分析：`1` 为实时、`2` 为冻结、`3` 为延迟、
`4` 为延迟冻结。覆盖参数不会修改 `thetagang.toml`。分析报告用于盘前筛选时，
可以选择 `3`；如果 IBKR 对标的提供延迟行情，通常约有 15 分钟延迟，且个别
期权成交量、未平仓量或 Greeks 仍可能不可用，报告会按缺失数据处理。

### IBKR 10197 行情会话冲突

`No market data during competing live session` 表示同一 IBKR 用户同时在实盘和
模拟等会话请求实时行情，IBKR 把行情优先提供给实盘会话。处理方式按数据要求选择：

1. 需要实时数据：退出占用行情的另一个 TWS 会话，或为并行运行的 TWS/IB Gateway
   使用拥有独立行情权限的 IBKR 用户名，然后保持行情类型为 `1`。
2. 可接受延迟数据：在候选分析命令中加入 `--market-data-type 3`。如果仍返回
   10197，说明 IBKR 没有为该连接降级，仍需结束冲突会话。

参考 IBKR 官方的[行情请求说明](https://ibkrcampus.com/campus/trading-lessons/python-receiving-market-data/)，
行情权限按用户分配，行情类型 `1` 至 `4` 分别对应实时、冻结、延迟和延迟冻结。

如果在北京时间晚上约 20:00 运行，对应美东夏令时早上约 08:00，通常仍在美国
期权正常交易时段之前。此时使用 `1` 可能得到空的 bid/ask；有期权行情权限时，
可以改用 `--market-data-type 2` 读取上次收盘的冻结报价；没有相应实时权限时，
可以尝试 `--market-data-type 4`。冻结类型是否可用仍取决于 TWS/IB Gateway 版本
和账户的期权行情权限。

命令会捕获 10197 并立即给出以上提示，不再等待行情字段超时。期权字段的可选等待
上限默认为 10 秒，可通过 `--option-field-timeout 30` 延长。字段仍缺失时按数据不足
处理，不生成推荐权重。不要仅为候选分析改动正式交易所用的 `runtime.account.market_data_type`，
除非正式交易也明确要使用相同的数据类型。

### 正常交易时段仍然被拒绝

“处于交易时段”和“收到完整数据”是两个不同条件。不能仅凭成交量/OI低于阈值的
旧版错误文案判断盘前、权限不足或标的流动性差。当前版本会逐合约显示数值及数据状态：

- `eligible`（通过）：必需字段有效并通过硬过滤。
- `rejected`（不合格）：字段完整，但实际数值未满足门槛；真实成交量为 0 仍按 0 处理。
- `data_insufficient`（数据不足）：至少一张抽样合约缺少双边报价、成交量或未平仓量。
  若其他已知字段也不满足门槛，另列硬性拒绝原因；缺失值不会转换成 0。
- IV、Delta 和财报日历缺失属于警告，不混入硬性拒绝；重复财报提示合并。

成交量读取抽样合约的 `volume`（tick 8/74），不使用其他口径的
`putVolume/callVolume` 替代。未平仓量按合约方向读取 `putOpenInterest/callOpenInterest`
（generic tick 101）；不使用已废弃的通用 `openInterest` 字段回退。
期权 IV 依次读取 `modelGreeks/bidGreeks/askGreeks/lastGreeks.impliedVol` 中有效值，
不将标的 30 天 IV 字段 `ticker.impliedVolatility` 当成该合约 IV。
字段定义依据 [IBKR tick 类型](https://interactivebrokers.github.io/tws-api/tick_types.html)
和 [期权计算回调](https://interactivebrokers.github.io/tws-api/option_computations.html)，
并用本机 `ib_async` 的实际回调进行离线回归验证。

在原命令末尾添加以下参数即可保留诊断：

```bash
--option-field-timeout 30 \
--json-output data/candidate-diagnostics.json
```

筛选或权重分配失败时，命令仍退出为 2；指定的 JSON 文件保存
`status=analysis_failed`、逐标的评估和 `observations`，`recommendations` 为空。
不会覆盖旧的 Markdown/TOML，因此旧报告不能视为本次结果。
JSON 的 `option_samples` 包含合约标识、bid/ask、volume、OI、IV来源、Delta、
实际 `marketDataType` 和最后接收更新时间；接收时间不是交易所报价生成时间。
同时记录请求的行情类型，并保留已收到的常见行情权限/缺失错误码及消息。
缺失或非有限数值以标准 JSON `null` 保存；没有错误码并不能证明权限完整。
采集连接失败或 10197 提前中断时，尚无完整候选快照，不产生这份分析失败 JSON。

若实际收到 `volume=20` 或 `OI=0`，这是当前抽样合约的数值；分析器只抽样一个
目标到期日的 ATM Put/Call，不能据此声称整个 SPY/QQQ/TLT 期权市场流动性差。
若字段为“缺失”，再对照相同合约的 TWS 显示值和记录的错误码排查。切换冻结类型
不会自动补齐所有缺失字段，也不应通过降低阈值让数据不足的候选通过。

工具会输出：

- 默认生成 UTF-8 编码的中文 Markdown 报告；如需英文，可添加 `--language en`；
- 每个候选的是否通过、买卖价差、期权成交量、未平仓量、DTE、财报窗口和风险提示；
- 通过候选的风险调整评分；
- 总和为 100% 的推荐权重；
- 目标金额和“一张合约占可用资金基数比例”的提示；
- 可以复制到 `thetagang.toml` 的 `[symbols.<symbol>]` 片段。
- JSON 输出的 `observations` 保留每条观察的 `source`、`as_of` 和逐合约诊断（如果数据提供方有填写），用于复核数据时点。

## 计算规则

硬过滤默认检查：

1. 标的价格为正；
2. 买卖价差不超过 5%；
3. 期权成交量不少于 100；
4. 未平仓量不少于 500；
5. DTE 位于目标 DTE ± 15 天；
6. 已知财报日期时，距离财报至少 14 天。

评分由四部分组成：流动性 35%、DTE 匹配 15%、权利金收益 15%、压力风险 35%。权利金只占一部分，不能单独把高隐含波动率标的排到前面。缺失财报日期、隐含波动率或已实现波动率时会在报告中标记为未验证。

权重使用逐步分配算法：优先给风险调整评分高的候选，同时限制 `max_single_weight` 和 `max_group_weight`。如果约束条件无法把权重分配到 100%，工具会失败并说明原因，不会静默生成不完整配置。

默认 `max_single_weight=0.30`，因此上面的三个候选即使全部通过，合计最多也只有 90%，
不能生成总和 100% 的权重。这是独立的配置约束，与行情是否完整无关。按默认上限需至少
四个合格候选并满足风险组容量；若要改变上限，可在离线输入 JSON 的 `policy` 中明确
配置，当前 IBKR CLI 不提供该覆盖参数。工具不会为凑足 100% 自动提高上限。

如果提供 `capital`，工具还会计算：

```text
目标金额 = 推荐权重 × capital × margin_usage
一张合约名义金额 = 标的价格 × 100
```

当一张合约超过 `max_contract_notional_pct` 的可用资金基数时，报告会提示人工复核。ThetaGang 的实际下单数量还会受到现有持仓、已有期权和 `maximum_new_contracts_percent` 的影响，因此建议把报告和 dry-run 结果一起审核。

## 后续接入计划

当前已经实现 `CandidateDataProvider` 的只读 IBKR 适配器：

1. 对候选股票进行合约确认和现货行情采集；
2. 从期权链中选择目标 DTE 附近的 Put/Call，并读取买价、卖价、成交量、未平仓量和 Greeks；
3. 读取历史价格，计算已实现波动率；
4. 生成带时间戳和数据来源的分析报告，再由交易员确认后复制到正式配置。

下一步是接入财报日历、分红日和重大事件缓存，并将事件数据作为可配置的硬过滤或风险加分项；数据不可用时仍必须明确标记为“未验证”。

这一步不会把智能体的自由文本判断直接变成订单参数。智能体可以解释异常、补充风险提示和提出候选排序，但最终权重仍由确定性的约束优化器生成。
