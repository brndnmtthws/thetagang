import time
from datetime import datetime, timezone

import exchange_calendars as xcals
import pandas as pd
from rich import box
from rich.table import Table

from thetagang import log
from thetagang.config import ExchangeHoursConfig


def determine_action(config: ExchangeHoursConfig, now: datetime) -> str:
    if config.action_when_closed == "continue":
        return "continue"

    calendar = xcals.get_calendar(config.exchange)
    today = now.date()

    if calendar.is_session(today):  # type: ignore
        open = calendar.session_open(today)  # type: ignore
        close = calendar.session_close(today)  # type: ignore

        start = open + pd.Timedelta(seconds=config.delay_after_open)
        end = close - pd.Timedelta(seconds=config.delay_before_close)

        table = Table(box=box.SIMPLE)
        table.add_column("Exchange Hours")
        table.add_column(config.exchange)
        table.add_row("Open", str(open))
        table.add_row("Close", str(close))
        table.add_row("Start", str(start))
        table.add_row("End", str(end))
        log.print(table)

        if start <= now <= end:
            # Exchange is open
            return "continue"
        elif config.action_when_closed == "exit":
            log.info("Exchange is closed")
            return "exit"
        elif config.action_when_closed == "wait":
            log.info("Exchange is closed")
            return "wait"
    elif config.action_when_closed == "wait":
        return "wait"

    log.info("Exchange is closed")
    return "exit"


def waited_for_open(config: ExchangeHoursConfig, now: datetime) -> bool:
    calendar = xcals.get_calendar(config.exchange)
    today = now.date()

    next_session = calendar.date_to_session(today, direction="next")  # type: ignore

    open = calendar.session_open(next_session)  # type: ignore
    start = open + pd.Timedelta(seconds=config.delay_after_open)

    seconds_until_start = (start - now).total_seconds()

    if seconds_until_start < config.max_wait_until_open:
        log.info(
            f"Waiting for exchange to open, start={start} seconds_until_start={seconds_until_start}"
        )
        time.sleep(seconds_until_start)
        return True
    else:
        log.info(
            f"Max wait time exceeded, exiting (seconds_until_start={seconds_until_start}, max_wait_until_open={config.max_wait_until_open})"
        )

    return False


def need_to_exit(config: ExchangeHoursConfig) -> bool:
    now = datetime.now(tz=timezone.utc)
    action = determine_action(config, now)
    if action == "exit":
        return True
    if action == "wait":
        return not waited_for_open(config, now)

    # action is "continue"
    return False
