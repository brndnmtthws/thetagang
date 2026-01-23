import time
from datetime import date, datetime, timezone

import exchange_calendars as xcals
import pandas as pd
from rich import box
from rich.table import Table

from thetagang import log
from thetagang.config import ExchangeHoursConfig


def _session_times_from_schedule(
    calendar: xcals.ExchangeCalendar, today: date
) -> tuple[bool, pd.Timestamp | None, pd.Timestamp | None]:
    session = pd.Timestamp(today)
    if session in calendar.sessions:
        schedule = calendar.schedule.loc[session]
        return True, schedule["open"], schedule["close"]
    return False, None, None


def _next_session_open_from_schedule(
    calendar: xcals.ExchangeCalendar, now: datetime
) -> pd.Timestamp | None:
    session = pd.Timestamp(now.date())
    sessions = calendar.sessions
    idx = int(sessions.searchsorted(session.to_datetime64(), side="left"))
    if idx >= len(sessions):
        return None
    candidate = sessions[idx]
    schedule = calendar.schedule.loc[candidate]
    if candidate == session and schedule["close"] <= pd.Timestamp(now):
        idx += 1
        if idx >= len(sessions):
            return None
        candidate = sessions[idx]
        schedule = calendar.schedule.loc[candidate]
    return schedule["open"]


def determine_action(config: ExchangeHoursConfig, now: datetime) -> str:
    if config.action_when_closed == "continue":
        return "continue"

    calendar = xcals.get_calendar(config.exchange)
    today = now.date()

    is_session, session_open, session_close = _session_times_from_schedule(
        calendar, today
    )

    if is_session:
        if session_open is None or session_close is None:
            log.warning(f"Exchange schedule missing open/close for {config.exchange}.")
            return "wait" if config.action_when_closed == "wait" else "exit"
        start = session_open + pd.Timedelta(seconds=config.delay_after_open)
        end = session_close - pd.Timedelta(seconds=config.delay_before_close)

        table = Table(box=box.SIMPLE)
        table.add_column("Exchange Hours")
        table.add_column(config.exchange)
        table.add_row("Open", str(session_open))
        table.add_row("Close", str(session_close))
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
    next_open = _next_session_open_from_schedule(calendar, now)
    if next_open is None:
        log.warning(f"No upcoming exchange session found for {config.exchange}.")
        return False

    start = next_open + pd.Timedelta(seconds=config.delay_after_open)

    seconds_until_start = (start - now).total_seconds()

    if seconds_until_start <= 0:
        return True
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
