"""Float noise: 354012.54000000004 is 354012.54. Four decimals keep ratios (0.8523) intact."""
DECIMALS = 4


def trim(value):
    """A float rounded to four decimals as text without trailing zeros ('354012.54', '1000', '0.8523')."""
    text = f"{round(float(value), DECIMALS):.{DECIMALS}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def tidy(value):
    """Round every float inside a list / dict (the rows of the generic cards)."""
    if isinstance(value, float):
        return round(value, DECIMALS)
    if isinstance(value, list):
        return [tidy(v) for v in value]
    if isinstance(value, dict):
        return {k: tidy(v) for k, v in value.items()}
    return value
