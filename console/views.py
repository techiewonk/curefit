"""Console pages: thin class-based views. Rules live in scraper/, display in the templates."""
import datetime as dt
import json

import tablib

from django.contrib import admin, messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import FormView, TemplateView, View
from unfold.views import UnfoldSiteViewMixin

from centers import sync
from centers.models import Center, CenterGroup, CenterList, CenterSyncLog
from scraper import analysis, auth, coverage, jobs, perfwide, revenue, selectors, sparkline
from scraper.models import Job, Session

from . import charts
from .forms import CampaignForm, CompareForm, OtpForm, RunForm, SessionPickForm


class ConsolePage(UnfoldSiteViewMixin):
    """A full page inside the Unfold admin layout (sidebar, theme, dark mode)."""

    admin_site = admin.site
    permission_required = ()


class Partial(PermissionRequiredMixin):
    """A fragment that HTMX swaps into a page (progress panel, estimate, ...)."""

    permission_required = ()


# ------------------------------------------------------------------ run
class RunView(ConsolePage, FormView):
    template_name = "console/run.html"
    form_class = RunForm
    title = "Run a scrape"
    success_url = reverse_lazy("console:run")

    def get_initial(self):
        q = self.request.GET  # links from Home / coverage: ?list=test  ?session=<id>  ?start=&end=
        return {k: v for k, v in (("centers", q.get("list")), ("session", q.get("session")),
                                  ("start", q.get("start")), ("end", q.get("end"))) if v}

    def get_context_data(self, **kwargs):
        kwargs.setdefault("campaign", CampaignForm(prefix="c"))
        return super().get_context_data(**kwargs)

    def post(self, request, *args, **kwargs):
        if "save_campaign" not in request.POST:
            return super().post(request, *args, **kwargs)
        form, campaign = self.get_form(), CampaignForm(request.POST, prefix="c")
        if form.is_valid() and campaign.is_valid():
            auto = campaign.save_from(form)
            messages.success(request, f"Saved the campaign '{auto.name}'. It is {'on' if auto.enabled else 'off'}: "
                                      "switch it on, edit it or press Run now in Automatic runs.")
            return redirect("admin:scraper_autorun_change", auto.pk)
        return self.render_to_response(self.get_context_data(form=form, campaign=campaign))

    def form_valid(self, form):
        if not auth.logged_in(fresh=True):
            messages.error(self.request, "Not logged in - send and verify the OTP first.")
            return self.form_invalid(form)
        try:
            job = jobs.enqueue(form.to_spec())
        except jobs.JobError as e:
            form.add_error(None, str(e))
            return self.form_invalid(form)
        messages.success(self.request, f"Job {job.id} queued for session {job.session_id}. "
                                       "The job runner (qcluster) must be running.")
        return super().form_valid(form)


class LoginPanel(Partial, TemplateView):
    """Kronos login: shows the state, sends the OTP, checks the code."""

    template_name = "console/partials/login.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx.update(logged_in=auth.logged_in(), form=kwargs.get("form") or OtpForm())
        return ctx

    def post(self, request):
        if request.POST.get("action") == "send":
            sent = auth.send_otp()
            return self.render_to_response(self.get_context_data(
                notice="OTP sent - check the inbox." if sent else "Could not send the OTP.", ok=sent))
        form = OtpForm(request.POST)
        if form.is_valid():
            ok = auth.verify_otp(form.cleaned_data["otp"])
            return self.render_to_response(self.get_context_data(
                notice="Logged in." if ok else "The OTP was not accepted.", ok=ok))
        return self.render_to_response(self.get_context_data(form=form))


class StatusView(ConsolePage, TemplateView):
    """What is happening: the running job, what needs attention, the latest numbers, automatic runs."""

    template_name = "console/status.html"
    title = "Status"

    def get_context_data(self, **kwargs):
        return super().get_context_data(home=selectors.home(), **kwargs)


class JobPanel(Partial, TemplateView):
    template_name = "console/partials/job.html"

    def get_context_data(self, **kwargs):
        jobs.refresh_states()
        return super().get_context_data(job=Job.objects.first(), **kwargs)

    def post(self, request, action):
        job = jobs.active_job()
        if job and action == "stop":
            jobs.request_stop(job)
        elif job and action == "force":
            jobs.force_stop(job)
        elif job and action == "pause":
            jobs.set_pause(job, not job.pause_requested)
        target = request.POST.get("next", "")
        if not request.headers.get("HX-Request") and url_has_allowed_host_and_scheme(target, {request.get_host()}):
            return redirect(target)  # plain form post from Home / Monitor
        return self.get(request)


class EstimatePanel(Partial, TemplateView):
    template_name = "console/partials/estimate.html"

    def get_context_data(self, **kwargs):
        form = RunForm(self.request.GET)
        form.is_valid()  # only the fields the estimate needs have to be valid
        ctx = super().get_context_data(**kwargs)
        data = form.cleaned_data
        if all(data.get(f) for f in ("centers", "reports", "speed")):
            pieces = len(coverage.pieces(data["start"], data["end"], data.get("sales_chunk") or Session.WHOLE)) \
                if data.get("start") and data.get("end") and data["start"] <= data["end"] else 1
            ctx["estimate"] = selectors.estimate(form.center_count, data["reports"], form.workers, pieces)
        ctx["window_notice"] = selectors.sales_window_notice(data.get("end"), data.get("reports", []))
        return ctx


class ResumePanel(Partial, TemplateView):
    """Offers to continue an unfinished session for the chosen period."""

    template_name = "console/partials/resume.html"

    def get_context_data(self, **kwargs):
        form = RunForm(self.request.GET)
        form.is_valid()
        ctx = super().get_context_data(**kwargs)
        start, end = form.cleaned_data.get("start"), form.cleaned_data.get("end")
        ctx["sessions"] = Session.objects.for_period(start, end, form.cleaned_data.get("granularity")) \
            .unfinished() if start and end else []
        ctx["form"] = form
        return ctx


# ------------------------------------------------------------------ monitor
def _picked_session(request):
    """The session in ?session=, else the running job's, else the newest one."""
    form = SessionPickForm(request.GET)
    if form.is_valid():
        return form.cleaned_data["session"]
    job = Job.objects.active().first()
    return job.session if job else Session.objects.first()


class MonitorView(ConsolePage, TemplateView):
    template_name = "console/monitor.html"
    title = "Live monitor"

    def get_context_data(self, **kwargs):
        session = _picked_session(self.request)
        return super().get_context_data(session=session, picker=SessionPickForm(initial={"session": session}),
                                        **kwargs)


class MonitorGrid(Partial, TemplateView):
    template_name = "console/partials/monitor_grid.html"

    def get_context_data(self, **kwargs):
        jobs.refresh_states()
        session = _picked_session(self.request)
        return super().get_context_data(grid=selectors.monitor(session, Job.objects.first()) if session else None,
                                        **kwargs)


# ------------------------------------------------------------------ analytics / compare
class AnalyticsView(ConsolePage, TemplateView):
    template_name = "console/analytics.html"
    title = "Analytics"

    def get_context_data(self, **kwargs):
        session = _picked_session(self.request)
        ctx = super().get_context_data(session=session, picker=SessionPickForm(initial={"session": session}), **kwargs)
        if session:
            data = selectors.analytics(session)
            c = data["centers"]
            names = [x["name"] or str(x["id"]) for x in c]
            points = analysis.trend(session)
            trend_chart = json.dumps({"labels": [str(p["date"]) for p in points], "datasets": [
                {"label": label, "data": [p.get(key) for p in points], "borderColor": color}
                for key, label, color in (("total", "Total revenue", "#7c3aed"), ("new", "New", "#22c55e"),
                                          ("repeat", "Repeat", "#f97316"))]}) if len(points) > 1 else None
            ctx.update(data, revenue_options=charts.options("Total revenue by center", "Center", "Revenue (₹)", legend=False),
                       mix_options=charts.options("New and repeat revenue by center", "Center", "Revenue (₹)"),
                       pack_options=charts.options("Revenue by pack", "Pack", "Revenue (₹)", legend=False),
                       trend_options=charts.options("Revenue over time", "Date", "Revenue (₹)"),
                       trend_chart=trend_chart, revenue_chart=json.dumps({
                "labels": names, "datasets": [{"label": "Total revenue", "data": [x["total_revenue"] for x in c],
                                               "backgroundColor": "#7c3aed"}]}),
                mix_chart=json.dumps({"labels": names, "datasets": [
                    {"label": "New", "data": [x["new_revenue"] for x in c], "backgroundColor": "#22c55e"},
                    {"label": "Repeat", "data": [x["repeat_revenue"] for x in c], "backgroundColor": "#f97316"}]}),
                pack_chart=json.dumps({"labels": [p["pack_name"] for p in data["packs"][:8]], "datasets": [
                    {"data": [p["revenue"] or 0 for p in data["packs"][:8]],
                     "backgroundColor": ["#7c3aed", "#22c55e", "#f97316", "#ef4444", "#3b82f6", "#14b8a6",
                                         "#eab308", "#ec4899"]}]}),
                table={"headers": ["Center", "Sales", "Sales revenue", "Renewals", "Renewal %", "Total revenue",
                                   "New %", "Repeat %", "Active %"],
                       "rows": [[f"{x['id']} · {x['name']}", x["sales"], x["sales_revenue"], x["renewals"],
                                 x["renewal_pct"], x["total_revenue"], x["new_pct"], x["repeat_pct"],
                                 x["active_pct"]] for x in c]})
        return ctx


class CoverageView(ConsolePage, TemplateView):
    """Expired Pack User Details by day and by gym: what is stored, what is missing, and buttons to fetch it."""

    template_name = "console/coverage.html"
    title = "Missing days"

    def get_context_data(self, **kwargs):
        lists = CenterList.objects.all()
        chosen = lists.filter(name=self.request.GET.get("list")).first() or lists.filter(name="actual").first() \
            or lists.first()
        ids = list(chosen.centers.values_list("center_id", flat=True)) if chosen else []
        days = coverage.missing_by_day(ids)
        return super().get_context_data(
            lists=lists, chosen=chosen, total=len(ids), days=days, centers=coverage.missing_by_center(ids),
            gap_days=sum(1 for d in days if d["missing"]), window=coverage.WINDOW_DAYS, **kwargs)


class CoverageRun(PermissionRequiredMixin, View):
    """Start a run for the missing days: all of them, one day, or one gym."""

    permission_required = ()

    def post(self, request):
        scope, _, value = request.POST.get("run", "").partition(":")
        if scope not in ("all", "day", "center"):
            messages.error(request, "Nothing was started: press one of the Fetch buttons.")
            return redirect("console:coverage")
        list_name = request.POST.get("list") or "actual"
        try:
            if scope == "day":
                job = coverage.fetch_missing(list_name, day=dt.date.fromisoformat(value))
            elif scope == "center":
                job = coverage.fetch_missing(list_name, center_ids_=[int(value)])
            else:
                job = coverage.fetch_missing(list_name)
        except (coverage.CannotFetch, ValueError) as e:
            messages.error(request, str(e) or "Nothing to fetch.")
            return redirect(f"{reverse('console:coverage')}?list={list_name}")
        messages.success(request, f"Fetching the missing days: job {job.id}, session {job.session_id}. "
                                  "The job runner (qcluster) must be running.")
        return redirect("console:run")


class PerfAnalysisView(ConsolePage, TemplateView):
    """A chart for every metric (a line per group), and a comparison table for one metric."""

    template_name = "console/perf_analysis.html"
    title = "Analysis and compare"

    def get_context_data(self, **kwargs):
        g = self.request.GET
        names = perfwide.ordered_metrics(perfwide.metric_names())
        metric = g.get("metric") if g.get("metric") in names else (names[0] if names else "")
        mode = g.get("mode") if g.get("mode") in ("mom", "yoy", "range", "group") else "mom"
        agg = g.get("agg") if g.get("agg") in ("auto", "sum", "avg") else "auto"
        table_agg = ("avg" if perfwide.is_rate(metric) else "sum") if agg == "auto" else agg
        month = perfwide.parse_month(g.get("month"))
        a = (perfwide.parse_month(g.get("a_from")), perfwide.parse_month(g.get("a_to")))
        b = (perfwide.parse_month(g.get("b_from")), perfwide.parse_month(g.get("b_to")))
        ranges = (a, b) if all(a) and all(b) else (None, None)
        try:
            months_back = min(max(int(g.get("months", 12)), 3), 48)
        except ValueError:
            months_back = 12
        data = perfwide.analyse(metric, g.get("group", ""), mode, table_agg, month, *ranges, months_back=months_back) \
            if metric else None
        months, series = perfwide.all_series(g.get("group", ""), agg, months_back)
        labels = [revenue.label(m) for m in months]
        cards = [{
            "metric": s_["metric"], "how": s_["how"],
            "data": json.dumps({"labels": labels, "datasets": [
                ({"label": name, "data": [None if v is None else round(v, 2) for v in points],
                  "borderColor": "#9ca3af", "backgroundColor": "#9ca3af", "borderDash": [6, 4]}
                 if name == perfwide.AVERAGE else
                 {"label": name, "data": [None if v is None else round(v, 2) for v in points],
                  "borderColor": charts.colour(i), "backgroundColor": charts.colour(i)})
                for i, (name, points) in enumerate(s_["lines"].items())]}),
            "options": charts.options(s_["metric"], "Month", f"{'Average per gym' if s_['how'] == 'avg' else 'Total'}")}
            for s_ in series if s_["lines"]]
        available = perfwide.available_months()
        if data:
            for r in data["rows"]:
                r["spark"] = sparkline.svg(r["cells"][:-1][-6:], labels[:-1][-6:])  # full months only
        return super().get_context_data(
            labels=labels, data=data, cards=cards, metrics=names, metric=metric, mode=mode, agg=agg, months_back=months_back,
            group=g.get("group", ""), groups=list(CenterGroup.objects.values_list("name", flat=True)),
            month=(data["month"].strftime("%Y-%m") if data else ""), a_from=g.get("a_from", ""), a_to=g.get("a_to", ""),
            b_from=g.get("b_from", ""), b_to=g.get("b_to", ""),
            month_min=available[0].strftime("%Y-%m") if available else "",
            month_max=available[-1].strftime("%Y-%m") if available else "", **kwargs)


class GymTrendView(ConsolePage, TemplateView):
    """One gym: every metric over the last months, against the average of its group."""

    template_name = "console/gym.html"

    def get_context_data(self, center_id, **kwargs):
        center = get_object_or_404(Center, pk=center_id)
        try:
            months_back = min(max(int(self.request.GET.get("months", 6)), 3), 36)
        except ValueError:
            months_back = 6
        months, rows, group = perfwide.gym_series(center.pk, months_back)
        labels = [revenue.label(m) for m in months]
        cards = []
        for i, r in enumerate(rows):
            for_chart = [None if v is None else round(v, 2) for v in r["values"]]
            datasets = [{"label": center.name, "data": for_chart, "borderColor": charts.colour(0), "backgroundColor": charts.colour(0)}]
            if group and any(v is not None for v in r["group_average"]):
                datasets.append({"label": f"{group} average", "data": [None if v is None else round(v, 2) for v in r["group_average"]],
                                 "borderColor": "#9ca3af", "backgroundColor": "#9ca3af", "borderDash": [6, 4]})
            cards.append({"data": json.dumps({"labels": labels, "datasets": datasets}),
                          "options": charts.options(r["metric"], "Month", "Value")})
            r["spark"] = sparkline.svg(r["values"][:-1][-6:], labels[:-1][-6:])  # full months only
        self.title = center.name
        return super().get_context_data(center=center, group=group, rows=rows, labels=labels, cards=cards,
                                        months_back=months_back, month_options=(3, 6, 12, 24), **kwargs)


def _revenue_data(request):
    g = request.GET
    since, until = perfwide.parse_month(g.get("from")), perfwide.parse_month(g.get("to"))
    try:
        months = min(max(int(g.get("months", 0)), 0), 48)  # 0 = all months with data
    except ValueError:
        months = 0
    if since or until:
        months = 0
    return revenue.matrix(g.get("group", ""), months, since=since, until=until), months


class RevenueView(ConsolePage, TemplateView):
    """Gyms x months: revenue, average, ranking, growth and target, one table per group."""

    template_name = "console/revenue.html"
    title = "Revenue by group"

    def get_context_data(self, **kwargs):
        g = self.request.GET
        data, months = _revenue_data(self.request)
        chart = json.dumps({"labels": [revenue.label(m) for m in data["months"]], "datasets": [
            {"label": name, "data": [round(series[m]) if series[m] else None for m in data["months"]],
             "borderColor": charts.colour(i), "backgroundColor": charts.colour(i)}
            for i, (name, series) in enumerate(data["group_totals"].items())]})
        group_months = revenue.group_month_table(g.get("group", ""))
        recent = [m for m in data["months"] if m < data["current"]][-6:]  # the current month is partial
        sections = [{**sec, "rows": [{**r, "cells": [r["values"].get(m) for m in data["months"]],
                                      "spark": sparkline.svg([r["values"].get(m) for m in recent],
                                                             [revenue.label(m) for m in recent])}
                                     for r in sec["rows"]]} for sec in data["sections"]]
        available = perfwide.available_months()
        return super().get_context_data(
            data=data, rows=data["rows"], sections=sections, group_months=group_months, months_back=months,
            shown_months=len(data["months"]), group=g.get("group", ""),
            labels=[revenue.label(m) for m in data["months"]],
            rank_labels=[revenue.label(m) + (" *" if m == data["current"] else "") for m in data["rank_months"]],
            chart=chart if len(data["months"]) > 1 else None,
            chart_options=charts.options("Revenue by group", "Month", "Revenue (₹)"),
            month_choices=[(m.strftime("%Y-%m"), revenue.label(m)) for m in available],
            from_=g.get("from", ""), to=g.get("to", ""),
            latest_label=revenue.label(data["latest_month"]) if data["latest_month"] else "", **kwargs)


class RevenueCsv(Partial, View):
    def get(self, request):
        if request.GET.get("part") == "groups":  # the groups x last 12 months table
            headers, rows = revenue.group_month_download(revenue.group_month_table(request.GET.get("group", "")))
            return _download(request, "revenue_by_group_month", headers, rows)
        data, _ = _revenue_data(request)
        headers, rows = revenue.table(data)
        return _download(request, "revenue_by_gym", headers, rows)


class CompareView(ConsolePage, FormView):
    template_name = "console/compare.html"
    form_class = CompareForm
    title = "Compare sessions"

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        if self.request.GET:
            kwargs["data"] = self.request.GET
        return kwargs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        form = ctx["form"]
        if form.is_bound and form.is_valid():
            rows = selectors.compare(form.cleaned_data["a"], form.cleaned_data["b"])
            if form.cleaned_data["only_changed"]:
                rows = [r for r in rows if not r["diff"] or any(r["diff"].values())]
            ctx.update(rows=rows, totals={k: sum((r["diff"] or {}).get(k, 0) for r in rows)
                                          for k in ("sales", "sales_revenue", "total_revenue")})
        return ctx


def _download(request, name, headers, rows):
    """A tablib download: CSV by default, ?format=xlsx for Excel."""
    book = tablib.Dataset(*rows, headers=headers)
    if request.GET.get("format") == "xlsx":
        response = HttpResponse(book.export("xlsx"), content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))
        response["Content-Disposition"] = f'attachment; filename="{name}.xlsx"'
        return response
    response = HttpResponse(book.export("csv"), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{name}.csv"'
    return response


class AnalyticsCsv(Partial, View):
    """The per-center analysis table of the picked session."""

    def get(self, request):
        session = _picked_session(request)
        if not session:
            raise Http404("No session")
        data = selectors.analytics(session)
        headers = ["center_id", "center", "sales", "sales_revenue", "renewals", "renewal_pct", "total_revenue",
                   "new_revenue", "repeat_revenue", "new_pct", "repeat_pct", "active_pct"]
        rows = [[x["id"], x["name"], x["sales"], x["sales_revenue"], x["renewals"], x["renewal_pct"],
                 x["total_revenue"], x["new_revenue"], x["repeat_revenue"], x["new_pct"], x["repeat_pct"],
                 x["active_pct"]] for x in data["centers"]]
        return _download(request, f"analytics_{session.session_id}", headers, rows)


class CompareCsv(Partial, View):
    def get(self, request):
        form = CompareForm(request.GET)
        if not form.is_valid():
            raise Http404("Pick two sessions")
        rows = selectors.compare(form.cleaned_data["a"], form.cleaned_data["b"])
        keys = ("sales", "sales_revenue", "total_revenue")
        headers = ["center_id", "center"] + [f"{k}_{t}" for k in keys for t in ("a", "b", "change")]
        body = [[r["id"], r["name"]] + [v for k in keys for v in
                                        ((r.get("a") or {}).get(k), (r.get("b") or {}).get(k), (r["diff"] or {}).get(k))]
                for r in rows]
        return _download(request, f"compare_{form.cleaned_data['a'].session_id}_{form.cleaned_data['b'].session_id}",
                         headers, body)


# ------------------------------------------------------------------ centers
class CentersView(ConsolePage, TemplateView):
    template_name = "console/centers.html"
    title = "Update centers from Kronos"

    def get_context_data(self, **kwargs):
        return super().get_context_data(
            last=CenterSyncLog.objects.first(), total=Center.objects.count(),
            active=Center.objects.filter(state="ACTIVE").count(), lists=CenterList.objects.all(), **kwargs)

    def post(self, request):
        if not auth.logged_in(fresh=True):
            messages.error(request, "Not logged in - send and verify the OTP on the Run page first.")
            return redirect("console:centers")
        try:
            d = sync.sync_from_kronos()
        except sync.NotLoggedIn as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"Sync failed: {e}")
        else:
            messages.success(request, f"Updated {d['total']} centers ({d['active']} active): {len(d['added'])} new, "
                                      f"{len(d['removed'])} no longer in Kronos, {len(d['state_changed'])} changed "
                                      f"state, {len(d['renamed'])} renamed.")
        return redirect("console:centers")
