from datetime import datetime, timezone


def contract_date_to_datetime(expiration: str) -> datetime:
    if len(expiration) == 8:
        return datetime.strptime(expiration, "%Y%m%d").replace(tzinfo=timezone.utc)
    else:
        return datetime.strptime(expiration, "%Y%m").replace(tzinfo=timezone.utc)


def option_dte(expiration: str) -> int:
    dte = (
        contract_date_to_datetime(expiration).date()
        - datetime.now().astimezone().date()
    )
    return dte.days
