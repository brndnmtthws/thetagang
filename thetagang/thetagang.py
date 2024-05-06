from asyncio import Future

from ib_insync import IB, IBC, Watchdog, util
from ib_insync.contract import Contract
from rich import box
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from thetagang.config import normalize_config, validate_config
from thetagang.fmt import dfmt, ffmt, pfmt
from thetagang.util import (
    get_strike_limit,
    get_target_delta,
    get_write_threshold_perc,
    get_write_threshold_sigma,
    maintain_high_water_mark,
)

from .portfolio_manager import PortfolioManager

util.patchAsyncio()

console = Console()


def start(config_path: str, without_ibc: bool = False) -> None:
    import toml

    with open(config_path, "r", encoding="utf8") as file:
        config = toml.load(file)

    config = normalize_config(config)

    validate_config(config)

    config_table = Table(box=box.SIMPLE_HEAVY)
    config_table.add_column("Section")
    config_table.add_column("Setting")
    config_table.add_column("")
    config_table.add_column("Value")

    config_table.add_row("[spring_green1]Account details")
    config_table.add_row("", "Account number", "=", config["account"]["number"])
    config_table.add_row(
        "", "Cancel existing orders", "=", f'{config["account"]["cancel_orders"]}'
    )
    config_table.add_row(
        "",
        "Margin usage",
        "=",
        f"{config['account']['margin_usage']} ({pfmt(config['account']['margin_usage'],0)})",
    )
    config_table.add_row(
        "", "Market data type", "=", f'{config["account"]["market_data_type"]}'
    )

    config_table.add_section()
    config_table.add_row("[spring_green1]Constants")
    config_table.add_row(
        "",
        "Daily stddev window",
        "=",
        f"{config['constants']['daily_stddev_window']}",
    )

    c_write_thresh = (
        f"{ffmt(get_write_threshold_sigma(config, None, 'C'))}σ"
        if get_write_threshold_sigma(config, None, "C")
        else pfmt(get_write_threshold_perc(config, None, "C"))
    )
    p_write_thresh = (
        f"{ffmt(get_write_threshold_sigma(config, None, 'P'))}σ"
        if get_write_threshold_sigma(config, None, "P")
        else pfmt(get_write_threshold_perc(config, None, "P"))
    )

    config_table.add_row("", "Write threshold for puts", "=", p_write_thresh)
    config_table.add_row("", "Write threshold for calls", "=", c_write_thresh)

    config_table.add_section()
    config_table.add_row("[spring_green1]Order settings")
    config_table.add_row(
        "",
        "Exchange",
        "=",
        f"{config['orders']['exchange']}",
    )
    config_table.add_row(
        "",
        "Params",
        "=",
        f"{config['orders']['algo']['params']}",
    )
    config_table.add_row(
        "",
        "Price update delay",
        "=",
        f"{config['orders']['price_update_delay']}",
    )
    config_table.add_row(
        "",
        "Minimum credit",
        "=",
        f"{dfmt(config['orders']['minimum_credit'])}",
    )

    config_table.add_section()
    config_table.add_row("[spring_green1]Close option positions")
    config_table.add_row(
        "",
        "When P&L",
        ">=",
        f"{pfmt(config['roll_when']['close_at_pnl'],0)}",
    )

    config_table.add_section()
    config_table.add_row("[spring_green1]Roll options when either condition is true")
    config_table.add_row(
        "",
        "Days to expiry",
        "<=",
        f"{config['roll_when']['dte']} and P&L >= {config['roll_when']['min_pnl']} ({pfmt(config['roll_when']['min_pnl'],0)})",
    )
    if "max_dte" in config["roll_when"]:
        config_table.add_row(
            "",
            "P&L",
            ">=",
            f"{config['roll_when']['pnl']} ({pfmt(config['roll_when']['pnl'],0)}) and DTE < {config['roll_when']['max_dte']}",
        )
    else:
        config_table.add_row(
            "",
            "P&L",
            ">=",
            f"{config['roll_when']['pnl']} ({pfmt(config['roll_when']['pnl'],0)})",
        )

    config_table.add_row(
        "",
        "Puts: credit only",
        "=",
        f"{config['roll_when']['puts']['credit_only']}",
    )
    config_table.add_row(
        "",
        "Puts: roll excess",
        "=",
        f"{config['roll_when']['puts']['has_excess']}",
    )
    config_table.add_row(
        "",
        "Calls: credit only",
        "=",
        f"{config['roll_when']['calls']['credit_only']}",
    )
    config_table.add_row(
        "",
        "Calls: roll excess",
        "=",
        f"{config['roll_when']['calls']['has_excess']}",
    )
    config_table.add_row(
        "",
        "Calls: maintain high water mark",
        "=",
        f"{config['roll_when']['calls']['maintain_high_water_mark']}",
    )
    config_table.add_section()
    config_table.add_row("[spring_green1]When writing new contracts")
    config_table.add_row(
        "",
        "Calculate net contract positions",
        "=",
        f"{config['write_when']['calculate_net_contracts']}",
    )
    config_table.add_row(
        "",
        "Puts, write when red",
        "=",
        f"{config['write_when']['puts']['red']}",
    )
    config_table.add_row(
        "",
        "Puts, write when green",
        "=",
        f"{config['write_when']['puts']['green']}",
    )
    config_table.add_row(
        "",
        "Calls, write when green",
        "=",
        f"{config['write_when']['calls']['green']}",
    )
    config_table.add_row(
        "",
        "Calls, write when red",
        "=",
        f"{config['write_when']['calls']['red']}",
    )
    config_table.add_row(
        "",
        "Call cap factor",
        "=",
        f"{pfmt(config['write_when']['calls']['cap_factor'])}",
    )
    config_table.add_row(
        "",
        "Call cap target floor",
        "=",
        f"{pfmt(config['write_when']['calls']['cap_target_floor'])}",
    )

    config_table.add_section()
    config_table.add_row("[spring_green1]When contracts are ITM")
    config_table.add_row(
        "",
        "Roll puts",
        "=",
        f"{config['roll_when']['puts']['itm']}",
    )
    config_table.add_row(
        "",
        "Roll calls",
        "=",
        f"{config['roll_when']['calls']['itm']}",
    )

    config_table.add_section()
    config_table.add_row("[spring_green1]Write options with targets of")
    config_table.add_row("", "Days to expiry", ">=", f"{config['target']['dte']}")
    if "max_dte" in config["target"]:
        config_table.add_row(
            "", "Days to expiry", "<=", f"{config['target']['max_dte']}"
        )
    config_table.add_row("", "Default delta", "<=", f"{config['target']['delta']}")
    if "puts" in config["target"]:
        config_table.add_row(
            "",
            "Delta for puts",
            "<=",
            f"{config['target']['puts']['delta']}",
        )
    if "calls" in config["target"]:
        config_table.add_row(
            "",
            "Delta for calls",
            "<=",
            f"{config['target']['calls']['delta']}",
        )
    config_table.add_row(
        "",
        "Maximum new contracts",
        "=",
        f"{pfmt(config['target']['maximum_new_contracts_percent'],0)} of buying power",
    )
    config_table.add_row(
        "",
        "Minimum open interest",
        "=",
        f"{config['target']['minimum_open_interest']}",
    )

    config_table.add_section()
    config_table.add_row("[spring_green1]Cash management")
    config_table.add_row("", "Enabled", "=", f"{config['cash_management']['enabled']}")
    config_table.add_row(
        "", "Cash fund", "=", f"{config['cash_management']['cash_fund']}"
    )
    config_table.add_row(
        "",
        "Target cash",
        "=",
        f"{dfmt(config['cash_management']['target_cash_balance'])}",
    )
    config_table.add_row(
        "",
        "Buy threshold",
        "=",
        f"{dfmt(config['cash_management']['buy_threshold'])}",
    )
    config_table.add_row(
        "",
        "Sell threshold",
        "=",
        f"{dfmt(config['cash_management']['sell_threshold'])}",
    )

    config_table.add_section()
    config_table.add_row("[spring_green1]Hedging with VIX calls")
    config_table.add_row("", "Enabled", "=", f"{config['vix_call_hedge']['enabled']}")
    config_table.add_row(
        "",
        "Target delta",
        "<=",
        f"{config['vix_call_hedge']['delta']}",
    )
    config_table.add_row(
        "",
        "Target DTE",
        ">=",
        f"{config['vix_call_hedge']['target_dte']}",
    )
    config_table.add_row(
        "",
        "Ignore DTE",
        "<=",
        f"{config['vix_call_hedge']['ignore_dte']}",
    )
    config_table.add_row(
        "",
        "Ignore DTE",
        "<=",
        f"{config['vix_call_hedge']['ignore_dte']}",
    )
    config_table.add_row(
        "",
        "Close hedges when VIX",
        ">=",
        f"{config['vix_call_hedge']['close_hedges_when_vix_exceeds']}",
    )
    for alloc in config["vix_call_hedge"]["allocation"]:
        config_table.add_row()
        if "lower_bound" in alloc:
            config_table.add_row(
                "",
                f"Allocate {pfmt(alloc['weight'])} when VIXMO",
                ">=",
                f"{alloc['lower_bound']}",
            )
        if "upper_bound" in alloc:
            config_table.add_row(
                "",
                f"Allocate {pfmt(alloc['weight'])} when VIXMO",
                "<=",
                f"{alloc['upper_bound']}",
            )

    symbols_table = Table(
        title="Configured symbols and target weights",
        box=box.SIMPLE_HEAVY,
        show_lines=True,
    )
    symbols_table.add_column("Symbol")
    symbols_table.add_column("Weight", justify="right")
    symbols_table.add_column("Call delta", justify="right")
    symbols_table.add_column("Call strike limit", justify="right")
    symbols_table.add_column("Call threshold", justify="right")
    symbols_table.add_column("HWM", justify="right")
    symbols_table.add_column("Put delta", justify="right")
    symbols_table.add_column("Put strike limit", justify="right")
    symbols_table.add_column("Put threshold", justify="right")
    for symbol, sconfig in config["symbols"].items():
        symbols_table.add_row(
            symbol,
            pfmt(sconfig["weight"]),
            ffmt(get_target_delta(config, symbol, "C")),
            dfmt(get_strike_limit(config, symbol, "C")),
            (
                f"{ffmt(get_write_threshold_sigma(config, symbol, 'C'))}σ"
                if get_write_threshold_sigma(config, symbol, "C")
                else pfmt(get_write_threshold_perc(config, symbol, "C"))
            ),
            str(maintain_high_water_mark(config, symbol)),
            ffmt(get_target_delta(config, symbol, "P")),
            dfmt(get_strike_limit(config, symbol, "P")),
            (
                f"{ffmt(get_write_threshold_sigma(config, symbol, 'P'))}σ"
                if get_write_threshold_sigma(config, symbol, "P")
                else pfmt(get_write_threshold_perc(config, symbol, "P"))
            ),
        )
    assert (
        round(
            sum([config["symbols"][s]["weight"] for s in config["symbols"].keys()]), 5
        )
        == 1.00000
    )

    tree = Tree(":control_knobs:")
    tree.add(Group(f":file_cabinet: Loaded from {config_path}", config_table))
    tree.add(Group(":yin_yang: Symbology", symbols_table))
    console.print(Panel(tree, title="Config"))

    if config.get("ib_insync", {}).get("logfile"):
        util.logToFile(config["ib_insync"]["logfile"])  # type: ignore

    def onConnected() -> None:
        portfolio_manager.manage()

    ib = IB()
    ib.connectedEvent += onConnected

    completion_future: Future[bool] = Future()
    portfolio_manager = PortfolioManager(config, ib, completion_future)

    probeContractConfig = config["watchdog"]["probeContract"]
    watchdogConfig = config.get("watchdog", {})
    del watchdogConfig["probeContract"]
    probeContract = Contract(
        secType=probeContractConfig["secType"],
        symbol=probeContractConfig["symbol"],
        currency=probeContractConfig["currency"],
        exchange=probeContractConfig["exchange"],
    )

    if not without_ibc:
        # TWS version is pinned to current stable
        ibc_config = config.get("ibc", {})
        # Remove any config params that aren't valid keywords for IBC
        ibc_keywords = {
            k: ibc_config[k] for k in ibc_config if k not in ["RaiseRequestErrors"]
        }
        ibc = IBC(1019, **ibc_keywords)

        ib.RaiseRequestErrors = ibc_config.get("RaiseRequestErrors", False)

        watchdog = Watchdog(ibc, ib, probeContract=probeContract, **watchdogConfig)
        watchdog.start()

        ib.run(completion_future)  # type: ignore
        watchdog.stop()
        ibc.terminate()
    else:
        ib.connect(
            watchdogConfig["host"],
            watchdogConfig["port"],
            clientId=watchdogConfig["clientId"],
            timeout=watchdogConfig["probeTimeout"],
            account=config["account"]["number"],
        )
        ib.run(completion_future)  # type: ignore
        ib.disconnect()
