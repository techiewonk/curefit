from django import template

register = template.Library()


@register.filter
def inr(value):
    """18,75,475 - lakh / crore digit grouping, no decimals. Empty for None."""
    if value in (None, ""):
        return ""
    n = int(round(float(value)))
    sign, digits = ("-" if n < 0 else ""), str(abs(n))
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return sign + ",".join(groups + [tail])
