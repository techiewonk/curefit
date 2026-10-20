"""Context for the Home dashboard (Unfold calls this for the admin index page): revenue by gym group, as charts."""
import json

from scraper import revenue

from . import charts


def home_charts():
    data = revenue.matrix(months=12)
    if not data["has_data"]:
        return {"has_data": False}
    months, latest = data["months"], data["latest_month"]
    labels = [revenue.label(m) for m in months]
    names = list(data["group_totals"])
    trend = {"labels": labels, "datasets": [
        {"label": g, "data": [round(v) if v else None for v in (data["group_totals"][g][m] for m in months)],
         "borderColor": charts.colour(i), "backgroundColor": charts.colour(i)} for i, g in enumerate(names)]}
    latest_label = revenue.label(latest) if latest else ""
    share = {"labels": names, "datasets": [{
        "label": f"Revenue {latest_label}", "data": [round(data["group_totals"][g].get(latest) or 0) for g in names],
        "backgroundColor": [charts.colour(i) for i in range(len(names))]}]}
    per_group = []
    for i, sec in enumerate(data["sections"]):
        rows = sorted((r for r in sec["rows"] if r["latest"]), key=lambda r: -r["latest"])[:15]
        if not rows:
            continue
        per_group.append({
            "group": sec["group"],
            "data": json.dumps({"labels": [r["name"] for r in rows], "datasets": [{
                "label": f"{sec['group']} - revenue {latest_label}", "data": [round(r["latest"]) for r in rows],
                "backgroundColor": charts.colour(i)}]}),
            "options": charts.options(f"{sec['group']}: gyms in {latest_label}", "Gym", "Revenue (₹)", legend=False)})
    return {
        "has_data": True, "latest_label": latest_label,
        "trend": json.dumps(trend),
        "trend_options": charts.options("Revenue by group, last 12 months (the last month is partial)", "Month", "Revenue (₹)"),
        "share": json.dumps(share),
        "share_options": charts.options(f"Revenue by group in {latest_label}", "Group", "Revenue (₹)", legend=False, aspect=1.2),
        "per_group": per_group,
    }


def callback(request, context):
    context.update(home_charts=home_charts())
    return context
