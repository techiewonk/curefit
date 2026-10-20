from django import template

register = template.Library()


@register.filter
def inr(value):
    """18,75,475 - lakh / crore digit grouping, no decimals. Empty for None."""
    if value in (None, ""):
        return ""
    value = float(value)
    if abs(value) < 1000 and value != int(value):  # ratings, ratios, small deltas keep their decimals
        return f"{value:.2f}".rstrip("0").rstrip(".")
    n = int(round(value))
    sign, digits = ("-" if n < 0 else ""), str(abs(n))
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups + [tail])
