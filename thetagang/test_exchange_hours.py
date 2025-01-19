from datetime import datetime, timezone
from unittest.mock import patch

from thetagang.config import ActionWhenClosedEnum, ExchangeHoursConfig
from thetagang.exchange_hours import determine_action, waited_for_open


def test_determine_action_continue_when_closed():
    config = ExchangeHoursConfig(
        exchange="XNYS",
        delay_after_open=0,
        delay_before_close=0,
        action_when_closed=ActionWhenClosedEnum.continue_,
    )
    now = datetime(2025, 1, 21, 12, 0, tzinfo=timezone.utc)

    result = determine_action(config, now)
    assert result == "continue"


def test_determine_action_in_open_window():
    config = ExchangeHoursConfig(
        exchange="XNYS",
        delay_after_open=60,
        delay_before_close=60,
        action_when_closed=ActionWhenClosedEnum.continue_,
    )
    now = datetime(2025, 1, 21, 15, 0, tzinfo=timezone.utc)

    result = determine_action(config, now)
    assert result == "continue"


def test_determine_action_after_close():
    config = ExchangeHoursConfig(
        exchange="XNYS",
        delay_after_open=60,
        delay_before_close=60,
        action_when_closed=ActionWhenClosedEnum.exit,
    )
    now = datetime(2025, 1, 21, 21, 0, tzinfo=timezone.utc)

    result = determine_action(config, now)
    assert result == "exit"


def test_determine_action_session_closed_wait():
    config = ExchangeHoursConfig(
        exchange="XNYS",
        delay_after_open=60,
        delay_before_close=60,
        action_when_closed=ActionWhenClosedEnum.wait,
    )
    now = datetime(2025, 1, 21, 14, 29, tzinfo=timezone.utc)

    result = determine_action(config, now)
    assert result == "wait"


@patch("thetagang.exchange_hours.time.sleep")
def test_waited_for_open_under_max(mock_sleep):
    config = ExchangeHoursConfig(
        exchange="XNYS",
        delay_after_open=60,
        delay_before_close=60,
        action_when_closed=ActionWhenClosedEnum.wait,
        max_wait_until_open=600,
    )
    now = datetime(2025, 1, 21, 14, 29, tzinfo=timezone.utc)

    assert waited_for_open(config, now) is True
    mock_sleep.assert_called_once()


@patch("thetagang.exchange_hours.time.sleep")
def test_waited_for_open_exceeds_max(mock_sleep):
    config = ExchangeHoursConfig(
        exchange="XNYS",
        delay_after_open=60,
        delay_before_close=60,
        action_when_closed=ActionWhenClosedEnum.wait,
        max_wait_until_open=30,
    )
    now = datetime(2025, 1, 21, 14, 0, tzinfo=timezone.utc)

    assert waited_for_open(config, now) is False
    mock_sleep.assert_not_called()


@patch("thetagang.exchange_hours.time.sleep")
def test_waited_for_open_negative_difference(mock_sleep):
    config = ExchangeHoursConfig(
        exchange="XNYS",
        delay_after_open=60,
        delay_before_close=60,
        action_when_closed=ActionWhenClosedEnum.wait,
        max_wait_until_open=300,
    )
    # 'now' is already after the start time
    now = datetime(2025, 1, 21, 15, 0, tzinfo=timezone.utc)

    # seconds_until_start will be negative, but code checks if it's < max_wait_until_open
    assert waited_for_open(config, now) is True
    mock_sleep.assert_called_once()
