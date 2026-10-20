"""Console pages: thin class-based views. Rules live in scraper/, display in the templates."""
import json

from django.contrib import admin, messages
from django.contrib.auth.mixins import PermissionRequiredMixin
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.views.generic import FormView, TemplateView, View
from unfold.views import UnfoldSiteViewMixin

from centers import sync
from centers.models import Center, CenterList, CenterSyncLog
from scraper import auth, jobs, selectors
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
        q = self.request.GET  # links from Home: ?list=test  ?session=<id>
        return {k: v for k, v in (("centers", q.get("list")), ("session", q.get("session"))) if v}

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
        elif job and action == "pause":
            jobs.set_pause(job, not job.pause_requested)
        return self.get(request)


class EstimatePanel(Partial, TemplateView):
    template_name = "console/partials/estimate.html"

    def get_context_data(self, **kwargs):
        form = RunForm(self.request.GET)
        form.is_valid()  # only the fields the estimate needs have to be valid
        ctx = super().get_context_data(**kwargs)
        data = form.cleaned_data
        if all(data.get(f) for f in ("centers", "reports", "speed")):
            ctx["estimate"] = selectors.estimate(data["centers"].centers.count(), data["reports"], form.workers)
        return ctx


class ResumePanel(Partial, TemplateView):
    """Offers to continue an unfinished session for the chosen period."""

    template_name = "console/partials/resume.html"

    def get_context_data(self, **kwargs):
        form = RunForm(self.request.GET)
        form.is_valid()
        ctx = super().get_context_data(**kwargs)
        start, end = form.cleaned_data.get("start"), form.cleaned_data.get("end")
        ctx["sessions"] = Session.objects.for_period(start, end).unfinished() if start and end else []
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
            ctx.update(data, revenue_chart=json.dumps({
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
