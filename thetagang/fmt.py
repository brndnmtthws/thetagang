from typing import Optional, Union


def redgreen(value: Union[int, float]) -> str:
    if value >= 0:
        return "green"
    return "red"


def dfmt(amount: Optional[Union[int, str, float]], precision: int = 2) -> str:
    if amount is not None:
        amount = float(amount)
        rg = redgreen(amount)
        return f"[{rg}]${amount:,.{precision}f}[/{rg}]"
    return ""


def pfmt(amount: Union[str, float], precision: int = 2) -> str:
    amount = float(amount) * 100.0
    rg = redgreen(amount)
    return f"[{rg}]{amount:.{precision}f}%[/{rg}]"


def ffmt(amount: Optional[float], precision: int = 2) -> str:
    if amount is not None:
        amount = float(amount)
        rg = redgreen(amount)
        return f"[{rg}]{amount:.{precision}f}[/{rg}]"
    return ""


def ifmt(amount: Optional[int]) -> str:
    if amount is not None:
        amount = int(amount)
        rg = redgreen(amount)
        return f"[{rg}]{amount:,d}[/{rg}]"
    return ""


def to_camel_case(snake_str: str) -> str:
    components = snake_str.split("_")
    # We capitalize the first letter of each component except the first one
    # with the 'title' method and join them together.
    return components[0] + "".join(x.title() for x in components[1:])
