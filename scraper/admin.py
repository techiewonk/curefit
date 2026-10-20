import os
import shutil

from django import forms
from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.template.response import TemplateResponse
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (AllValuesCheckboxFilter, AutocompleteSelectFilter, BooleanRadioFilter,
                                          ChoicesDropdownFilter, FieldTextFilter, RangeDateFilter, RangeNumericListFilter,
                                          RangeDateTimeFilter, RangeNumericFilter, RelatedDropdownFilter)
from unfold.admin import TabularInline
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminCheckboxSelectMultipleWidget, UnfoldAdminSelectWidget

from centers.models import KnownEmpty
from config.admin_base import ExportModelAdmin

import datetime as dt

from django.urls import reverse

from . import autorun, coverage, customers, files, jobs, perfwide, sparkline
from .numbers import tidy
from console.templatetags.formats import inr
from .conf import SPEED_CHOICES
from .models import (AutoRun, CardColumns, CenterMonth, CardRow, Customer, CustomerPhone, GymMember, GymPack, Job, PerfMetric,
                     SalesCoverage, SalesPack, SalesRecord, SectionResult, Session)
from .reports import TYPED, report_choices
from .resources import (GymMemberResource, CenterMonthResource, GymPackResource, SalesPackResource,
                        SalesRecordResource)

STATUS_LABELS = {"complete": "success", "ok": "success", "done": "success", "finished": "success",
                 "incomplete": "warning", "slow": "warning", "cancelled": "warning", "empty": "info",
                 "running": "info", "queued": "info", "started": "info", "interrupted": "danger",
                 "failed": "danger", "error": "danger", "skipped": "danger"}


def confirmed(model_admin, request, queryset, what):
    """Django's delete-confirmation pattern for a bulk action that cannot be undone: the first post shows what
    will happen (`what`) and the rows; only the second post, from that page, does it. Returns None when confirmed,
    else the confirmation page to return from the action."""
    if request.POST.get("confirmed") == "yes":
        return None
    opts = model_admin.model._meta
    return TemplateResponse(request, "admin/confirm_action.html", {
        **model_admin.admin_site.each_context(request), "opts": opts, "title": "Are you sure?",
        "what": what, "objects": list(queryset[:50]), "count": queryset.count(),
        "action": request.POST.get("action"), "selected": request.POST.getlist(ACTION_CHECKBOX_NAME),
        "select_across": request.POST.get("select_across", "0"), "checkbox_name": ACTION_CHECKBOX_NAME})


def done(request, url_name):
    """After a dialog action (posted by HTMX from Unfold's dialog): go to `url_name` with the messages shown."""
    url = reverse(url_name)
    if request.headers.get("HX-Request"):
        response = HttpResponse()
        response["HX-Redirect"] = url
        return response
    return redirect(url)


def _inside_runs(path):
    """Only ever delete folders that live under runs/."""
    root = os.path.realpath(files.RUNS_DIR)
    real = os.path.realpath(path) if path else ""
    return bool(real) and real.startswith(root + os.sep) and os.path.isdir(real)


@admin.register(Session)
class SessionAdmin(ExportModelAdmin):
    list_display = ("session_id", "period", "granularity", "status_badge", "created_at", "centers", "results_link",
                    "missing_count")
    list_filter = [("status", ChoicesDropdownFilter), ("granularity", ChoicesDropdownFilter),
                   ("start_date", RangeDateFilter), ("end_date", RangeDateFilter),
                   ("created_at", RangeDateTimeFilter), ("finished_at", RangeDateTimeFilter)]
    search_fields = ("session_id",)
    readonly_fields = ("session_id", "folder", "coverage", "missing", "checks", "created_at", "finished_at",
                       "run_rows")
    actions = ["continue_session", "stop_running", "merge_files"]

    @display(description="Per-run rows")
    def run_rows(self, obj):
        from console.views import rows_pruned
        if rows_pruned(obj):
            return "Removed to save space (Keep run rows in Settings): this session's sales and gym rows are only " \
                   "in the all-runs tables now, so its per-session pages show less than was scraped."
        return "Kept"

    @display(description="Period")
    def period(self, obj):
        return f"{obj.start_date} → {obj.end_date}"

    @display(description="Status", label=STATUS_LABELS)
    def status_badge(self, obj):
        return obj.status

    @display(description="Centers")
    def centers(self, obj):
        return len(obj.center_ids or [])

    @display(description="Results")
    def results_link(self, obj):
        return format_html('<a href="/admin/scraper/sectionresult/?session__session_id__exact={}">'
                           "{} sections</a>", obj.session_id, obj.results.count())

    @action(description="Continue selected session (fetch what is missing)")
    def continue_session(self, request, queryset):
        if queryset.count() != 1:
            return messages.error(request, "Pick exactly one session to continue.")
        session = queryset.get()
        try:
            job = jobs.enqueue({"session": session.session_id, "workers": session.workers or 2,
                                "speed": session.speed})
        except jobs.JobError as e:
            return messages.error(request, str(e))
        messages.success(request, f"Job {job.id} queued for session {session.session_id}. "
                                  "Watch it on the Run page; the job runner (qcluster) must be running.")

    @action(description="Stop the running job of the selected session")
    def stop_running(self, request, queryset):
        stopped = 0
        for job in Job.objects.active().filter(session__in=queryset):
            jobs.request_stop(job)
            stopped += 1
        (messages.success if stopped else messages.info)(
            request, "Stop requested." if stopped else "None of the selected sessions has a running job.")

    @action(description="Rebuild merged files (one file per report)")
    def merge_files(self, request, queryset):
        for s in queryset:
            if _inside_runs(s.folder):
                files.merge_session(s)
        messages.success(request, f"Merged files rebuilt for {queryset.count()} session(s).")

    # deleting a session also removes its folder (rows go through the cascade)
    def delete_model(self, request, obj):
        folder = obj.folder
        super().delete_model(request, obj)
        if _inside_runs(folder):
            shutil.rmtree(folder, ignore_errors=True)

    def delete_queryset(self, request, queryset):
        folders = list(queryset.values_list("folder", flat=True))
        super().delete_queryset(request, queryset)
        for folder in folders:
            if _inside_runs(folder):
                shutil.rmtree(folder, ignore_errors=True)


@admin.register(SectionResult)
class SectionResultAdmin(ExportModelAdmin):
    list_display = ("session", "center", "report", "status_badge", "row_count", "seconds", "scraped_at")
    list_filter = [("session", RelatedDropdownFilter), ("center", AutocompleteSelectFilter),
                   ("status", ChoicesDropdownFilter), ("report", AllValuesCheckboxFilter),
                   ("row_count", RangeNumericFilter), ("seconds", RangeNumericFilter),
                   ("scraped_at", RangeDateTimeFilter)]
    search_fields = ("center__name", "center__code", "report", "error")
    list_select_related = ("session", "center")
    list_per_page = 100
    actions = ["mark_known_empty", "retry_selected"]

    @display(description="Status", label=STATUS_LABELS)
    def status_badge(self, obj):
        return obj.status

    @action(description="Mark selected empty results as known empty (skipped by later runs)")
    def mark_known_empty(self, request, queryset):
        """Only results that came back empty: a known-empty center is skipped by every later run, so marking an
        OK, failed or slow result would silently stop fetching data that exists."""
        empty = queryset.filter(status=SectionResult.EMPTY)
        skipped = queryset.count() - empty.count()
        n = 0
        for r in empty:
            _, created = KnownEmpty.objects.get_or_create(
                center_id=r.center_id, report=r.report, defaults={"note": f"seen empty in session {r.session_id}"})
            n += created
        messages.success(request, f"{n} new known-empty entr{'y' if n == 1 else 'ies'}.")
        if skipped:
            messages.warning(request, f"{skipped} selected result{'s' if skipped != 1 else ''} not marked: only "
                                      "results with status 'empty' can be known empty.")

    @action(description="Retry selected (deletes their results, then continues the session to fetch them again)")
    def retry_selected(self, request, queryset):
        sessions = set(queryset.values_list("session_id", flat=True))
        if len(sessions) != 1:
            return messages.error(request, "Pick results from one session.")
        if jobs.active_job():  # checked before anything is deleted: the results and their errors stay
            return messages.error(request, "A scrape is running: retry when it has finished. Nothing was deleted.")
        page = confirmed(self, request, queryset, "These results and their error messages are deleted, then the "
                                                  "session is continued so that they are fetched again.")
        if page:
            return page
        sid = sessions.pop()
        try:
            with transaction.atomic():  # the results come back if the job cannot be queued
                queryset.delete()  # no result = still missing, so the continue fetches it again
                job = jobs.enqueue({"session": sid, "workers": 1})
        except jobs.JobError as e:
            return messages.error(request, f"{e}. Nothing was deleted.")
        messages.success(request, f"Job {job.id} queued to retry the selected sections of {sid}.")

    def has_add_permission(self, request):
        return False


class DataAdmin(ExportModelAdmin):
    """Read-only browser for scraped rows. Filter, then Export to download exactly those rows."""

    list_per_page = 50
    list_select_related = ("session", "center")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        """Never deleted by hand: one click would lose scraped (or merged, all-runs) history. Deleting a session or a
        center still removes its rows (the admin checks this permission for every row the delete cascades into)."""
        match = getattr(request, "resolver_match", None)
        return bool(match) and match.url_name in CASCADE_PAGES

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


# the pages whose delete may cascade into the scraped rows: a session's and a center's delete
CASCADE_PAGES = {"scraper_session_delete", "scraper_session_changelist", "centers_center_delete",
                 "centers_center_changelist"}


@admin.register(SalesRecord)
class SalesRecordAdmin(DataAdmin):
    resource_classes = [SalesRecordResource]
    list_display = ("center", "name", "phone", "pack_name", "member_end_date", "renewed", "revenue", "session")
    list_filter = [("session", RelatedDropdownFilter), ("center", AutocompleteSelectFilter),
                   ("revenue", RangeNumericFilter), ("member_end_date", RangeDateFilter),
                   ("renewed", AllValuesCheckboxFilter), ("active_last_30d", AllValuesCheckboxFilter),
                   ("pack_name", FieldTextFilter), ("name", FieldTextFilter), ("phone", FieldTextFilter),
                   ("scraped_at", RangeDateTimeFilter)]
    search_fields = ("name", "phone", "user_id", "center__name", "pack_name")
    raw_id_fields = ("customer",)


@admin.register(GymMember)
class GymMemberAdmin(DataAdmin):
    resource_classes = [GymMemberResource]
    list_display = ("center", "username", "pack_name", "final_amount_paid", "status", "new_vs_repeat", "session")
    list_filter = [("session", RelatedDropdownFilter), ("center", AutocompleteSelectFilter),
                   ("final_amount_paid", RangeNumericFilter), ("purchase_date", RangeDateFilter),
                   ("pack_start_date", RangeDateFilter), ("pack_end_date", RangeDateFilter),
                   ("last_class_date", RangeDateFilter), ("status", AllValuesCheckboxFilter),
                   ("new_vs_repeat", AllValuesCheckboxFilter), ("offline_vs_online", AllValuesCheckboxFilter),
                   ("class_level", AllValuesCheckboxFilter), ("pack_name", FieldTextFilter),
                   ("username", FieldTextFilter), ("scraped_at", RangeDateTimeFilter)]
    search_fields = ("username", "user_id", "center__name", "pack_name")
    raw_id_fields = ("customer",)


@admin.register(CardRow)
class CardRowAdmin(DataAdmin):
    list_display = ("center", "report", "row_num", "values", "session")

    @display(description="Row")
    def values(self, obj):
        return ", ".join("" if v is None else str(v) for v in tidy(obj.data))

    list_filter = [("session", RelatedDropdownFilter), ("center", AutocompleteSelectFilter),
                   ("report", AllValuesCheckboxFilter), ("row_num", RangeNumericFilter),
                   ("scraped_at", RangeDateTimeFilter)]
    search_fields = ("center__name", "report")


@admin.register(CardColumns)
class CardColumnsAdmin(ExportModelAdmin):
    list_display = ("report", "columns")


@admin.register(Job)
class JobAdmin(ExportModelAdmin):
    list_display = ("id", "state_badge", "session", "progress", "started_at", "finished_at")
    list_filter = [("state", ChoicesDropdownFilter), ("session", RelatedDropdownFilter),
                   ("created_at", RangeDateTimeFilter), ("started_at", RangeDateTimeFilter),
                   ("finished_at", RangeDateTimeFilter)]
    readonly_fields = [f.name for f in Job._meta.fields]
    actions = ["request_stop", "force_stop"]

    @display(description="State", label=STATUS_LABELS)
    def state_badge(self, obj):
        return obj.state

    @display(description="Progress")
    def progress(self, obj):
        return f"{obj.done}/{obj.total}" if obj.total else "-"

    @action(description="Stop selected jobs (after the requests in flight; a queued job is cancelled)")
    def request_stop(self, request, queryset):
        for j in queryset:
            jobs.request_stop(j)
        messages.success(request, "Stop requested.")

    @action(description="Stop selected jobs NOW (abandons the request in progress)")
    def force_stop(self, request, queryset):
        page = confirmed(self, request, queryset, "The job runner's worker is ended at once: the request in "
                                                  "progress is lost and the session is marked interrupted. Everything "
                                                  "saved so far is kept; continue the session later to finish it.")
        if page:
            return page
        for j in queryset:
            jobs.force_stop(j)
        messages.success(request, "Stopped. Continue the session later to finish it.")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        """A queued or running job cannot be deleted (the runner would lose track of it): stop it first."""
        return not (obj and obj.is_active) and super().has_delete_permission(request, obj)

    def delete_queryset(self, request, queryset):
        active = queryset.filter(state__in=jobs.ACTIVE)
        if active.exists():
            messages.warning(request, f"{active.count()} queued or running job(s) were kept: stop them first.")
        super().delete_queryset(request, queryset.exclude(state__in=jobs.ACTIVE))


class AutoRunForm(forms.ModelForm):
    reports = forms.MultipleChoiceField(choices=report_choices, initial=list(TYPED), widget=UnfoldAdminCheckboxSelectMultipleWidget,
                                        help_text="The three core reports are ticked by default; tick the extra cards you want.")
    speed = forms.ChoiceField(choices=SPEED_CHOICES, widget=UnfoldAdminSelectWidget)

    class Meta:
        model = AutoRun
        fields = "__all__"

    def clean(self):
        data = super().clean()
        if self.errors.get("centers") or self.errors.get("groups") or self.errors.get("gyms"):
            return data
        if not (data.get("centers") or data.get("groups") or data.get("gyms")):
            self.add_error("centers", "Pick a center list, groups or gyms: a campaign needs something to run on.")
        if data.get("period_rule") == AutoRun.MISSING_SALES and "sales" not in data.get("reports", ["sales"]):
            data["reports"] = list(data.get("reports") or []) + ["sales"]
        return data


@admin.register(AutoRun)
class AutoRunAdmin(ExportModelAdmin):
    """Campaigns: the Run-page setup saved under a name and attached to a schedule.

    Switch one on and the job runner starts it on schedule; Run now tests it with the period it would report on."""

    form = AutoRunForm
    list_display = ("name", "enabled", "schedule_text", "period_text", "gyms_text", "granularity", "next_run_at",
                    "last_run_at", "last_status_badge", "last_message")
    list_editable = ("enabled",)
    list_filter = [("enabled", BooleanRadioFilter), ("frequency", ChoicesDropdownFilter),
                   ("period_rule", ChoicesDropdownFilter), ("granularity", ChoicesDropdownFilter),
                   ("centers", RelatedDropdownFilter), ("last_run_at", RangeDateTimeFilter)]
    search_fields = ("name",)
    autocomplete_fields = ("gyms", "groups")
    readonly_fields = ("last_run_at", "last_session", "last_status", "last_message")
    fieldsets = (
        ("Campaign", {"fields": ("name", "enabled")}),
        ("Schedule: when it runs", {"fields": ("frequency", "run_at", "weekday", "month_day", "cron")}),
        ("Period: what it reports on", {"fields": ("period_rule", "days", "start_date", "end_date")}),
        ("Gyms: who it runs for", {"fields": ("centers", "groups", "gyms")}),
        ("Reports and speed", {"fields": ("reports", "speed", "granularity", "sales_chunk", "fill_missing")}),
        ("Options", {"fields": ("retry_empty", "recheck_known", "continue_unfinished")}),
        ("Last run", {"fields": ("last_run_at", "last_session", "last_status", "last_message")}),
    )
    actions = ["run_selected"]
    actions_row = ["run_now"]
    actions_detail = ["run_now"]

    @display(description="Schedule")
    def schedule_text(self, obj):
        if obj.frequency == AutoRun.CRON:
            return f"cron {obj.cron}"
        extra = f" {dict(AutoRun.WEEKDAYS)[obj.weekday]}" if obj.frequency == AutoRun.WEEKLY else \
            f" day {obj.month_day}" if obj.frequency == AutoRun.MONTHLY else ""
        return f"{obj.get_frequency_display().split(',')[0].lower()}{extra} at {obj.run_at:%H:%M}"

    @display(description="Period")
    def period_text(self, obj):
        return autorun.summary(obj)

    @display(description="Gyms")
    def gyms_text(self, obj):
        return len(autorun.center_ids(obj))

    @display(description="Next run")
    def next_run_at(self, obj):
        when = autorun.next_run(obj)
        return when.strftime("%d %b %H:%M") if when else "-"

    @display(description="Last result", label=STATUS_LABELS)
    def last_status_badge(self, obj):
        return obj.last_status or "-"

    def _run(self, request, auto):
        status, message = autorun.run(auto, force=True)
        (messages.success if status == "started" else messages.warning)(request, f"{auto.name}: {message}")

    # a dialog: opening the link only asks, the scrape starts on the dialog's (POST, CSRF-checked) button
    @action(description="Run now (test it with the period it would report on)", url_path="run-now",
            dialog={"title": "Run this campaign now?",
                    "description": "Starts a real scrape on Curefit's servers now, for the period this campaign would "
                                   "report on today, even if it is switched off. It is refused while another scrape runs.",
                    "form_submit_text": "Start the scrape"})
    def run_now(self, request, form, object_id):
        self._run(request, get_object_or_404(AutoRun, pk=object_id))
        return done(request, "admin:scraper_autorun_changelist")

    @action(description="Run the selected automatic runs now (starts real scrapes)")
    def run_selected(self, request, queryset):
        page = confirmed(self, request, queryset, "Each selected campaign starts a real scrape on Curefit's servers "
                                                  "now, for the period it would report on today. Only one scrape runs at "
                                                  "a time, so the others are skipped with a reason.")
        if page:
            return page
        for auto in queryset:
            self._run(request, auto)

    def delete_queryset(self, request, queryset):
        """Bulk delete skips each object's delete(): remove the job runner's schedules here too."""
        for auto in queryset:
            autorun.remove_schedule(auto)
        super().delete_queryset(request, queryset)


class PhoneInline(TabularInline):
    model = CustomerPhone
    extra = 0
    can_delete = False
    readonly_fields = ("phone", "shared")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Customer)
class CustomerAdmin(ExportModelAdmin):
    """Everyone seen in sales or gym data, one row per person, with the newest status and a lead label."""

    list_display = ("name", "user_id", "phone_numbers", "center", "pack_name", "pack_end_date", "lead_badge",
                    "member_status", "last_class_date", "purchases_link")
    list_filter = [("lead_status", AllValuesCheckboxFilter), ("pack_end_date", RangeDateFilter),
                   ("last_class_date", RangeDateFilter), ("last_amount", RangeNumericFilter),
                   ("center", AutocompleteSelectFilter), ("renewed", AllValuesCheckboxFilter),
                   ("active_last_30d", AllValuesCheckboxFilter), ("member_status", AllValuesCheckboxFilter),
                   ("new_vs_repeat", AllValuesCheckboxFilter), ("pack_name", FieldTextFilter),
                   ("name", FieldTextFilter), ("phones__phone", FieldTextFilter),
                   ("phones__shared", BooleanRadioFilter), ("updated_at", RangeDateTimeFilter)]
    search_fields = ("name", "user_id", "phones__phone", "pack_name")
    list_select_related = ("center",)
    list_per_page = 50
    inlines = [PhoneInline]
    autocomplete_fields = ("center",)
    readonly_fields = ("user_id", "last_session", "as_of", "first_seen", "updated_at", "lead_status")
    actions = ["refresh_leads"]

    @display(description="Phones")
    def phone_numbers(self, obj):
        return ", ".join(p.phone for p in obj.phones.all())

    @display(description="Lead", label={"active": "success", "expiring": "warning", "expired": "info",
                                        "lapsed": "danger", "unknown": "info"})
    def lead_badge(self, obj):
        return obj.lead_status

    @display(description="Purchases")
    def purchases_link(self, obj):
        return format_html('<a href="/admin/scraper/salesrecord/?customer={0}">sales</a> · '
                           '<a href="/admin/scraper/gymmember/?customer={0}">gym</a>', obj.pk)

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("phones")

    @action(description="Recompute lead status from the pack end dates")
    def refresh_leads(self, request, queryset):
        messages.success(request, f"Lead status refreshed for {customers.refresh_leads()} customers.")

    def has_add_permission(self, request):
        return False


@admin.register(GymPack)
class GymPackAdmin(DataAdmin):
    """My Gym Member merged over every run: one row per member pack."""

    list_display = ("center", "username", "user_id", "pack_name", "pack_start_date", "pack_end_date", "status",
                    "last_class_date", "final_amount_paid", "scraped_at")
    list_filter = [("center", AutocompleteSelectFilter), ("pack_end_date", RangeDateFilter),
                   ("pack_start_date", RangeDateFilter), ("purchase_date", RangeDateFilter),
                   ("last_class_date", RangeDateFilter), ("final_amount_paid", RangeNumericFilter),
                   ("status", AllValuesCheckboxFilter), ("new_vs_repeat", AllValuesCheckboxFilter),
                   ("offline_vs_online", AllValuesCheckboxFilter), ("class_level", AllValuesCheckboxFilter),
                   ("pack_name", FieldTextFilter), ("username", FieldTextFilter),
                   ("scraped_at", RangeDateTimeFilter), ("first_seen", RangeDateTimeFilter)]
    search_fields = ("username", "user_id", "center__name", "pack_name")
    list_select_related = ("center",)
    resource_classes = [GymPackResource]


@admin.register(SalesPack)
class SalesPackAdmin(DataAdmin):
    """Expired Pack User Details merged over every run and day."""

    list_display = ("center", "name", "phone", "pack_name", "member_end_date", "renewed", "active_last_30d",
                    "revenue", "scraped_at")
    list_filter = [("center", AutocompleteSelectFilter), ("member_end_date", RangeDateFilter),
                   ("revenue", RangeNumericFilter), ("renewed", AllValuesCheckboxFilter),
                   ("active_last_30d", AllValuesCheckboxFilter), ("pack_name", FieldTextFilter),
                   ("name", FieldTextFilter), ("phone", FieldTextFilter),
                   ("scraped_at", RangeDateTimeFilter), ("first_seen", RangeDateTimeFilter)]
    search_fields = ("name", "phone", "user_id", "center__name", "pack_name")
    list_select_related = ("center",)
    resource_classes = [SalesPackResource]


@admin.register(SalesCoverage)
class SalesCoverageAdmin(DataAdmin):
    """Which days of Expired Pack User Details are stored for which gym. A day missing here is a gap to fill."""

    list_display = ("center", "groups_column", "day", "session", "fetched_at")
    list_filter = [("center", AutocompleteSelectFilter), ("center__groups__name", AllValuesCheckboxFilter),
                   ("day", RangeDateFilter), ("fetched_at", RangeDateTimeFilter)]
    search_fields = ("center__name", "center__code")
    list_select_related = ("center", "session")
    ordering = ("-day", "center_id")
    actions_list = ["fetch_missing_days", "open_missing_days"]

    @action(description="Fetch the missing days", url_path="fetch-missing-days",
            dialog={"title": "Fetch the missing days?",
                    "description": "Starts a sales scrape on Curefit's servers for the actual list: one request per "
                                   "day, only for the days of the last 90 that are not stored yet.",
                    "form_submit_text": "Start fetching"})
    def fetch_missing_days(self, request, form):
        """Starts a sales run, one request per day, for the days of the last 90 that the actual list is missing."""
        try:
            job = coverage.fetch_missing("actual")
        except coverage.CannotFetch as e:
            messages.error(request, str(e))
            return done(request, "admin:scraper_salescoverage_changelist")
        messages.success(request, f"Fetching the missing days: job {job.id}. The job runner (qcluster) must be running.")
        return done(request, "console:run")

    @action(description="Missing days", url_path="missing-days")
    def open_missing_days(self, request):
        return redirect("console:coverage")

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("center__groups")

    @display(description="Groups")
    def groups_column(self, obj):
        return ", ".join(g.name for g in obj.center.groups.all())


def metric_filter(metric):
    """A from/to filter on one metric stored in CenterMonth.metrics."""
    slug = "m_" + "".join(ch if ch.isalnum() else "_" for ch in metric.lower())

    class MetricRangeFilter(RangeNumericListFilter):
        title = metric
        parameter_name = slug

        def queryset(self, request, queryset):
            filters = {}
            low = self.used_parameters.get(f"{slug}_from")
            high = self.used_parameters.get(f"{slug}_to")
            try:
                if low not in (None, ""):
                    filters[f"metrics__{metric}__gte"] = float(low)
                if high not in (None, ""):
                    filters[f"metrics__{metric}__lte"] = float(high)
            except ValueError:
                return queryset.none()
            return queryset.filter(**filters)

    MetricRangeFilter.__name__ = f"MetricRangeFilter_{slug}"
    return MetricRangeFilter


@admin.register(CenterMonth)
class CenterMonthAdmin(DataAdmin):
    """Performance metrics by month: a row per gym and month, a column per metric. Filter, sort, search, export."""

    resource_classes = [CenterMonthResource]
    list_select_related = ("center",)
    ordering = ("-month", "center_id")
    list_filter = [("center", AutocompleteSelectFilter), ("center__groups__name", AllValuesCheckboxFilter),
                   ("month", RangeDateFilter), ("total_revenue", RangeNumericFilter),
                   ("new_revenue", RangeNumericFilter), ("repeat_revenue", RangeNumericFilter)]
    search_fields = ("center__name", "center__code")

    def get_list_filter(self, request):
        """The fixed filters, then a from/to range for every other metric the data has (the list follows the data)."""
        return [*super().get_list_filter(request), *[metric_filter(name) for name in perfwide.extra_metric_names()]]

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("center__groups")

    def get_list_display(self, request):
        trend = self._trend_columns()
        return ["center", "groups_column", "month_column", "total_column", trend[0], "new_column", trend[1],
                "repeat_column", trend[2], "gym_page",
                *[self._metric_column(i, name) for i, name in enumerate(perfwide.extra_metric_names())]]

    @staticmethod
    def _trend_columns():
        """A small trend line (the six months up to the row's month) for total, new and repeat revenue.
        One query per page load: the revenue of every gym and month is read once and shared by all rows."""
        cache = {}

        def series(obj, which):
            if not cache:
                for cid, month, *vals in CenterMonth.objects.values_list(
                        "center_id", "month", "total_revenue", "new_revenue", "repeat_revenue"):
                    cache[(cid, month)] = vals
            months, m = [], obj.month
            for _ in range(6):
                months.append(m)
                m = (m - dt.timedelta(days=1)).replace(day=1)
            months.reverse()
            return [(cache.get((obj.center_id, mm)) or [None] * 3)[which] for mm in months], [mm.strftime("%b-%y") for mm in months]

        def make(which, title):
            def column(obj):
                values, labels = series(obj, which)
                return sparkline.svg(values, labels)
            column.__name__ = f"trend_{which}"
            return display(description=title)(column)

        return [make(0, "Total, 6 mo"), make(1, "New, 6 mo"), make(2, "Repeat, 6 mo")]

    @display(description="All metrics")
    def gym_page(self, obj):
        return format_html('<a href="{}">trends</a>', reverse("console:gym", args=[obj.center_id]))

    @display(description="Groups")
    def groups_column(self, obj):
        return ", ".join(g.name for g in obj.center.groups.all())

    @display(description="Month", ordering="month")
    def month_column(self, obj):
        return obj.month.strftime("%b-%y")

    @display(description="Total revenue", ordering="total_revenue")
    def total_column(self, obj):
        return inr(obj.total_revenue)

    @display(description="New revenue", ordering="new_revenue")
    def new_column(self, obj):
        return inr(obj.new_revenue)

    @display(description="Repeat revenue", ordering="repeat_revenue")
    def repeat_column(self, obj):
        return inr(obj.repeat_revenue)

    @staticmethod
    def _metric_column(index, name):
        def column(obj):
            value = obj.metrics.get(name)
            return "" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")
        column.__name__ = f"metric_{index}"
        return display(description=name)(column)


def _use_settings_form():
    """System -> Settings: constance's admin with the cross-field checks of config.admin_base.SettingsForm."""
    from constance.admin import Config
    from config.admin_base import SettingsForm
    if Config in admin.site._registry:
        admin.site._registry[Config].change_list_form = SettingsForm


_use_settings_form()
