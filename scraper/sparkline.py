"""A small trend line (inline SVG) for a table row: no script, works inside admin lists and report tables."""
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from console.templatetags.formats import inr

UP, DOWN, FLAT = "#22c55e", "#ef4444", "#9ca3af"


def svg(values, labels=None, width=96, height=26):
    """values: numbers (None = no data) oldest first; labels: the month of each value (shown on hover)."""
    points = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(points) < 2:
        return "-"
    lo, hi = min(v for _, v in points), max(v for _, v in points)
    span = (hi - lo) or 1
    last = len(values) - 1 or 1
    x = lambda i: 2 + (width - 4) * i / last  # noqa: E731
    y = lambda v: height - 3 - (height - 6) * (v - lo) / span  # noqa: E731
    segments, current = [], []
    for i, v in enumerate(values):  # a gap in the data breaks the line
        if v is None:
            if current:
                segments.append(current)
            current = []
        else:
            current.append(f"{x(i):.1f},{y(v):.1f}")
    if current:
        segments.append(current)
    colour = UP if points[-1][1] > points[0][1] else DOWN if points[-1][1] < points[0][1] else FLAT
    lines = "".join(f'<polyline fill="none" stroke="{colour}" stroke-width="1.6" points="{" ".join(s)}"/>' for s in segments)
    i, v = points[-1]
    dot = f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="2.2" fill="{colour}"/>'
    names = labels or [str(n + 1) for n in range(len(values))]
    title = " · ".join(f"{n}: {inr(v) if v is not None else '-'}" for n, v in zip(names, values))
    return format_html('<svg width="{}" height="{}" viewBox="0 0 {} {}" role="img" aria-label="trend"><title>{}</title>{}</svg>',
                       width, height, width, height, title, mark_safe(lines + dot))
