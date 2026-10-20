"""Chart.js options for every chart: a legend, a title and axis titles.

Unfold hides legends by default and gives charts no axis titles, so a chart with several lines (one per
group) could not be read. Passing these options replaces Unfold's defaults, so the colours that suit both the
light and the dark theme are repeated here.
"""
import json
import math

TEXT = "#9ca3af"


def _finite(value):
    """NaN and infinity become null: json.dumps would write NaN, which is not JSON, and Chart.js would not draw."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    return value


def dumps(data):
    """Chart data as strict JSON."""
    return json.dumps(_finite(data), allow_nan=False)
PALETTE = ["#7c3aed", "#22c55e", "#f97316", "#3b82f6", "#ef4444", "#14b8a6", "#eab308", "#ec4899", "#8b5cf6", "#06b6d4"]


def colour(i):
    return PALETTE[i % len(PALETTE)]


def group_colour(name, order):
    """The same group gets the same colour on every chart (its place in the groups' order)."""
    return colour(order.index(name) if name in order else len(order))


def options(title="", x="", y="", legend=True, stacked=False, aspect=2.2):
    """JSON for the `options` argument of Unfold's chart components."""

    def axis(text, begin_at_zero=False):
        a = {"ticks": {"color": TEXT}, "grid": {"color": "#374151"},
             "title": {"display": bool(text), "text": text, "color": TEXT, "font": {"weight": "600"}}}
        if begin_at_zero:
            a["beginAtZero"] = True
        if stacked:
            a["stacked"] = True
        return a

    config = {
        "responsive": True,
        "maintainAspectRatio": True,
        "aspectRatio": aspect,   # width / height: keeps a chart from growing as tall as the page
        "interaction": {"mode": "index", "intersect": False},
        "plugins": {
            "legend": {"display": legend, "position": "bottom",
                       "labels": {"color": TEXT, "usePointStyle": True, "pointStyle": "circle", "boxWidth": 8, "boxHeight": 8}},
            "title": {"display": bool(title), "text": title, "color": TEXT, "font": {"size": 13, "weight": "600"}},
            "tooltip": {"enabled": True},
        },
        "elements": {"line": {"borderWidth": 2, "tension": 0.25}, "point": {"radius": 3, "hoverRadius": 5},
                     "bar": {"borderRadius": 2}},
        "scales": {"x": axis(x), "y": axis(y, begin_at_zero=True)},
    }
    return dumps(config)
