def contract_date_to_datetime(option):
    from datetime import datetime

    if len(option.lastTradeDateOrContractMonth) == 8:
        return datetime.strptime(option.lastTradeDateOrContractMonth, "%Y%m%d")
    else:
        return datetime.strptime(option.lastTradeDateOrContractMonth, "%Y%m")


def option_dte(option):
    from datetime import date

    dte = contract_date_to_datetime(option).date() - date.today()
    return dte.days
