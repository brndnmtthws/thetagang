# OPT-01 验证记录

- 日期：2026-09-10（Asia/Shanghai）。
- 修订：[REV-20260910-003](../REVISION_HISTORY.md)。
- 代码基线：970f5acd85a6b262c0ae16846ecaf32118bcb3d0 加本次工作区补丁；未提交 commit。
- 环境：macOS，Python 3.13.12，pytest 9.0.2；覆盖率检查使用 pytest-cov 7.1.0。
- 范围：模拟模式隔离、撤单归属筛选、订单标识及改价后的字段保留。

## 回归场景

新增 `tests/test_order_execution_safety.py`，共 28 个参数化场景：

- 在 IBKR 适配层直接调用下单、修改与撤单，dry_run 模式全部阻断，正常模式全部转发。
- dry_run 继续支持账户和持仓查询、行情类型设置。
- 初始化撤单同时要求当前账户、受管理标的、tg: 归属标识和未完成状态。
- 其他账户、空账户、手工标识、无标识旧订单、相似但非 tg: 的前缀、未管理标的、已成交/已取消订单均不被撤销。
- VIX/现金管理关闭时，非组合配置标的的对应订单不被撤销；cancel_orders=false 也不撤单。
- 新订单默认包含账户及 tg:order 标识，显式 Regime 标识保持不变。
- dry_run 的管理流程保留本地订单意图和预览；绕过预览分支直接提交队列也无法触及券商写接口。
- 改价保留原订单除价格以外的全部字段，且不修改原对象；改价后仍能正确识别订单归属；模拟模式的改价不会提交。

## 执行命令与结果

```text
uv run --frozen --no-sync pytest -q tests/test_order_execution_safety.py tests/test_trading_operations.py tests/test_portfolio_manager.py tests/test_trades.py tests/test_ibkr.py tests/test_main_cli.py
90 passed in 2.80s

uv run --frozen --no-sync --with pytest-cov pytest --cov=thetagang --cov-report=term-missing:skip-covered
306 passed, 43 warnings in 6.46s
TOTAL: 5180 statements, 1280 missed, 75% coverage

.venv/bin/ty check
All checks passed!

.venv/bin/ruff check .
All checks passed!

.venv/bin/ruff format --check thetagang/ibkr.py thetagang/orders.py thetagang/trading_operations.py thetagang/portfolio_manager.py thetagang/main.py thetagang/config_models.py thetagang/trades.py tests/test_order_execution_safety.py
8 files already formatted

git diff --check
Passed (no output)
```

使用 --frozen --no-sync 保留现有依赖环境及锁文件；--with pytest-cov 仅为本次覆盖率检查临时加载工具，没有修改 pyproject.toml 或 uv.lock。

全量测试有 43 条警告，包含 SQLite 连接未关闭的 ResourceWarning；测试均通过。这些警告不等于经过本次修复，也不应省略为“无警告通过”。

## 验证边界

券商接口均由模拟对象提供，本次未运行真实交易命令、未连接 IBKR，也未检查实际账户挂单。当前验证不能替代实盘集成验证。

未覆盖/未修复的独立事项：OPT-02 的错误结果传递；OPT-03 的调价循环提前返回及订单意图关联；OPT-04 的重连和多进程互斥。

旧版生成的无 tg: 标识挂单现在会保留。账户和标识筛选是订单归属约定，不提供账户级多进程隔离；后者属于 OPT-04。
