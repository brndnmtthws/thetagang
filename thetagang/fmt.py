def redgreen(value):
    if value >= 0:
        return "green"
    return "red"


def dfmt(amount, precision=2):
    if amount is not None:
        amount = float(amount)
        rg = redgreen(amount)
        return f"[{rg}]${amount:,.{precision}f}[/{rg}]"
    return ""


def pfmt(amount, precision=2):
    if amount is not None:
        amount = float(amount) * 100.0
        rg = redgreen(amount)
        return f"[{rg}]{amount:.{precision}f}%[/{rg}]"
    return ""


def ffmt(amount, precision=2):
    if amount is not None:
        amount = float(amount)
        rg = redgreen(amount)
        return f"[{rg}]{amount:.{precision}f}[/{rg}]"
    return ""


def ifmt(amount):
    if amount is not None:
        amount = int(amount)
        rg = redgreen(amount)
        return f"[{rg}]{amount:,d}[/{rg}]"
    return ""


def to_camel_case(snake_str):
    components = snake_str.split("_")
    # We capitalize the first letter of each component except the first one
    # with the 'title' method and join them together.
    return components[0] + "".join(x.title() for x in components[1:])
