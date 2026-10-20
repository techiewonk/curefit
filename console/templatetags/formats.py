import math

from django import template

register = template.Library()


@register.filter
def inr(value):
    """18,75,475 - lakh / crore digit grouping, no decimals. Empty for None and NaN; text that is not a
    number ('12%') is shown as it is instead of breaking the page."""
    if value in (None, ""):
        return ""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(value):
        return ""
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


@register.simple_tag
def report_label(key):
    """The name Kronos gives a report ('perf' is 'Total Revenue and Split'), so pages never name one that does not exist."""
    from scraper.reports import REPORTS
    return REPORTS[key]["label"] if key in REPORTS else key
