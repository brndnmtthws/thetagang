from datetime import date, datetime


def contract_date_to_datetime(expiration: str) -> datetime:
    if len(expiration) == 8:
        return datetime.strptime(expiration, "%Y%m%d")
    else:
        return datetime.strptime(expiration, "%Y%m")


def option_dte(expiration: str) -> int:
    dte = contract_date_to_datetime(expiration).date() - date.today()
    return dte.days
