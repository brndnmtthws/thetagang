from datetime import UTC, datetime

from thetagang.options import contract_date_to_datetime


def _naive_utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC).replace(tzinfo=None)


def test_contract_date_to_datetime_preserves_naive_datetime_contract() -> None:
    assert contract_date_to_datetime("20270115") == _naive_utc(2027, 1, 15)
    assert contract_date_to_datetime("202701") == _naive_utc(2027, 1, 1)
    assert contract_date_to_datetime("20270115").tzinfo is None
