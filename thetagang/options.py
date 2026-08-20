from datetime import date, datetime


def contract_date_to_datetime(expiration: str) -> datetime:
    if len(expiration) == 8:
        return datetime.strptime(expiration, "%Y%m%d")  # noqa: DTZ007
    else:
        return datetime.strptime(expiration, "%Y%m")  # noqa: DTZ007


def option_dte(expiration: str) -> int:
    dte = contract_date_to_datetime(expiration).date() - date.today()  # noqa: DTZ011
    return dte.days
