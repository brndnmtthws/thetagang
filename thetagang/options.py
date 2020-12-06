def contract_date_to_datetime(expiration):
    from datetime import datetime

    if len(expiration) == 8:
        return datetime.strptime(expiration, "%Y%m%d")
    else:
        return datetime.strptime(expiration, "%Y%m")


def option_dte(expiration):
    from datetime import date

    dte = contract_date_to_datetime(expiration).date() - date.today()
    return dte.days
