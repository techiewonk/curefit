"""Console pages: thin class-based views. Rules live in scraper/, display in the templates."""
import json

import tablib

from django.contrib import admin, messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.http import Http404, HttpResponse
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.generic import FormView, TemplateView, View
from unfold.views import UnfoldSiteViewMixin

from centers import sync
from centers.models import Center, CenterGroup, CenterList, CenterSyncLog
from scraper import analysis, auth, coverage, jobs, perfwide, revenue, selectors
from scraper.models import Job, Session

from .forms import CompareForm, OtpForm, RunForm, SessionPickForm


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
            ctx.update(data, trend_chart=trend_chart, revenue_chart=json.dumps({
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
    """Expired Pack User Details by day: how many centers of a list are stored, and which days are missing."""

    template_name = "console/coverage.html"
    title = "Sales day coverage"

    def get_context_data(self, **kwargs):
        from django.db.models import Count
        from django.utils import timezone

        from scraper import coverage
        from scraper.models import SalesCoverage
        lists = CenterList.objects.all()
        chosen = lists.filter(name=self.request.GET.get("list")).first() or lists.filter(name="actual").first() \
            or lists.first()
        ids = list(chosen.centers.values_list("center_id", flat=True)) if chosen else []
        today = timezone.localdate()
        first = coverage.window_start(today)
        stored = {}
        for i in range(0, len(ids), 900):
            for row in SalesCoverage.objects.filter(center_id__in=ids[i:i + 900], day__gte=first) \
                    .values("day").annotate(n=Count("center_id")):
                stored[row["day"]] = stored.get(row["day"], 0) + row["n"]
        days = [{"day": d, "stored": stored.get(d, 0), "missing": len(ids) - stored.get(d, 0)}
                for d in coverage.days(first, today - coverage.ONE_DAY)]
        gaps = [d for d in days if d["missing"]]
        return super().get_context_data(
            lists=lists, chosen=chosen, total=len(ids), days=days[::-1], gap_count=len(gaps),
            first_gap=gaps[0]["day"] if gaps else None, last_gap=gaps[-1]["day"] if gaps else None, **kwargs)


class PerfAnalysisView(ConsolePage, TemplateView):
    """One metric over months: chart per group, and a comparison (vs previous month, last year, two ranges, group)."""

    template_name = "console/perf_analysis.html"
    title = "Perf analysis"
    PALETTE = ["#7c3aed", "#22c55e", "#f97316", "#3b82f6", "#ef4444", "#14b8a6", "#eab308", "#ec4899"]

    def get_context_data(self, **kwargs):
        g = self.request.GET
        names = perfwide.ordered_metrics(perfwide.metric_names())
        metric = g.get("metric") if g.get("metric") in names else (names[0] if names else "")
        mode = g.get("mode") if g.get("mode") in ("mom", "yoy", "range", "group") else "mom"
        agg = "avg" if g.get("agg") == "avg" else "sum"
        month = perfwide.parse_month(g.get("month"))
        a = (perfwide.parse_month(g.get("a_from")), perfwide.parse_month(g.get("a_to")))
        b = (perfwide.parse_month(g.get("b_from")), perfwide.parse_month(g.get("b_to")))
        ranges = (a, b) if all(a) and all(b) else (None, None)
        try:
            months_back = min(max(int(g.get("months", 12)), 3), 48)
        except ValueError:
            months_back = 12
        data = perfwide.analyse(metric, g.get("group", ""), mode, agg, month, *ranges, months_back=months_back) \
            if metric else None
        chart = json.dumps({"labels": [revenue.label(m) for m in data["months"]], "datasets": [
            {"label": name, "data": [None if v is None else round(v, 2) for v in points],
             "borderColor": self.PALETTE[i % len(self.PALETTE)]} for i, (name, points) in enumerate(data["lines"].items())]}) \
            if data and data["lines"] else None
        return super().get_context_data(
            data=data, chart=chart, metrics=names, metric=metric, mode=mode, agg=agg, months_back=months_back,
            group=g.get("group", ""), groups=list(CenterGroup.objects.values_list("name", flat=True)),
            month=(data["month"].strftime("%Y-%m") if data else ""), a_from=g.get("a_from", ""), a_to=g.get("a_to", ""),
            b_from=g.get("b_from", ""), b_to=g.get("b_to", ""), **kwargs)


def _revenue_data(request):
    try:
        months = min(max(int(request.GET.get("months", 0)), 0), 48)  # 0 = all months with data
    except ValueError:
        months = 0
    return revenue.matrix(request.GET.get("group", ""), months), months


class RevenueView(ConsolePage, TemplateView):
    """Gyms x months: revenue, average, ranking, growth and target, filtered by group."""

    template_name = "console/revenue.html"
    title = "Revenue by gym"

    def get_context_data(self, **kwargs):
        data, months = _revenue_data(self.request)
        palette = ["#7c3aed", "#22c55e", "#f97316", "#3b82f6", "#ef4444", "#14b8a6"]
        chart = json.dumps({"labels": [revenue.label(m) for m in data["months"]], "datasets": [
            {"label": g, "data": [round(series[m]) if series[m] else None for m in data["months"]],
             "borderColor": palette[i % len(palette)]} for i, (g, series) in enumerate(data["group_totals"].items())]})
        rows = data["rows"]
        group_months = revenue.group_month_table(self.request.GET.get("group", ""))
        sections = [{**sec, "rows": [{**r, "cells": [r["values"].get(m) for m in data["months"]]} for r in sec["rows"]]}
                    for sec in data["sections"]]
        return super().get_context_data(
            data=data, rows=rows, sections=sections, group_months=group_months, months_back=months, shown_months=len(data["months"]), group=self.request.GET.get("group", ""),
            labels=[revenue.label(m) for m in data["months"]],
            rank_labels=[revenue.label(m) + (" *" if m == data["current"] else "") for m in data["rank_months"]], chart=chart if len(data["months"]) > 1 else None,
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
