import os
import shutil

from django import forms
from django.contrib import admin, messages
from django.shortcuts import redirect
from django.utils.html import format_html
from unfold.admin import ModelAdmin
from unfold.contrib.filters.admin import (AllValuesCheckboxFilter, AutocompleteSelectFilter, BooleanRadioFilter,
                                          ChoicesDropdownFilter, FieldTextFilter, RangeDateFilter,
                                          RangeDateTimeFilter, RangeNumericFilter, RelatedDropdownFilter)
from unfold.admin import TabularInline
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminCheckboxSelectMultipleWidget, UnfoldAdminSelectWidget

from centers.models import KnownEmpty
from config.admin_base import ExportModelAdmin

from . import autorun, customers, files, jobs, perfwide
from .conf import SPEED_CHOICES
from .models import (AutoRun, CardColumns, CenterMonth, CardRow, Customer, CustomerPhone, GymMember, GymPack, Job, PerfMetric,
                     SalesCoverage, SalesPack, SalesRecord, SectionResult, Session)
from .reports import report_choices
from .resources import (GymMemberResource, CenterMonthResource, GymPackResource, PerfMetricResource, SalesPackResource,
                        SalesRecordResource)

STATUS_LABELS = {"complete": "success", "ok": "success", "done": "success", "finished": "success",
                 "incomplete": "warning", "slow": "warning", "cancelled": "warning", "empty": "info",
                 "running": "info", "queued": "info", "started": "info", "interrupted": "danger",
                 "failed": "danger", "error": "danger", "skipped": "danger"}


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
    readonly_fields = ("session_id", "folder", "coverage", "missing", "checks", "created_at", "finished_at")
    actions = ["continue_session", "stop_running", "merge_files"]

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

    @action(description="Mark selected as known empty (stop counting them as problems)")
    def mark_known_empty(self, request, queryset):
        n = 0
        for r in queryset:
            _, created = KnownEmpty.objects.get_or_create(
                center_id=r.center_id, report=r.report, defaults={"note": f"seen empty in session {r.session_id}"})
            n += created
        messages.success(request, f"{n} new known-empty entr{'y' if n == 1 else 'ies'}.")

    @action(description="Retry selected (continue their session)")
    def retry_selected(self, request, queryset):
        sessions = set(queryset.values_list("session_id", flat=True))
        if len(sessions) != 1:
            return messages.error(request, "Pick results from one session.")
        sid = sessions.pop()
        queryset.delete()  # no result = still missing, so the continue fetches it again
        try:
            job = jobs.enqueue({"session": sid, "workers": 1})
        except jobs.JobError as e:
            return messages.error(request, str(e))
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
        # allowed so that deleting a Session can cascade into these rows; the manual
        # bulk-delete is removed below, so they stay browse-and-export only
        return True

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions


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


@admin.register(PerfMetric)
class PerfMetricAdmin(DataAdmin):
    resource_classes = [PerfMetricResource]
    list_display = ("center", "metric_date", "metric", "value", "session")
    list_filter = [("session", RelatedDropdownFilter), ("center", AutocompleteSelectFilter),
                   ("metric_date", RangeDateFilter), ("value", RangeNumericFilter),
                   ("metric", AllValuesCheckboxFilter), ("scraped_at", RangeDateTimeFilter)]
    search_fields = ("center__name", "metric")
    ordering = ("-metric_date", "center_id")


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
    list_display = ("center", "report", "row_num", "data", "session")
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
        for j in queryset:
            jobs.force_stop(j)
        messages.success(request, "Stopped. Continue the session later to finish it.")

    def has_add_permission(self, request):
        return False


class AutoRunForm(forms.ModelForm):
    reports = forms.MultipleChoiceField(choices=report_choices, widget=UnfoldAdminCheckboxSelectMultipleWidget)
    speed = forms.ChoiceField(choices=SPEED_CHOICES, widget=UnfoldAdminSelectWidget)

    class Meta:
        model = AutoRun
        fields = "__all__"


@admin.register(AutoRun)
class AutoRunAdmin(ExportModelAdmin):
    """Runs that start by themselves. Switch one on, or press Run now to test it with yesterday's data."""

    form = AutoRunForm
    list_display = ("name", "enabled", "frequency", "centers", "granularity", "next_run_at", "last_run_at",
                    "last_status_badge", "last_message")
    list_editable = ("enabled",)
    list_filter = [("enabled", BooleanRadioFilter), ("frequency", ChoicesDropdownFilter),
                   ("granularity", ChoicesDropdownFilter), ("centers", RelatedDropdownFilter),
                   ("last_run_at", RangeDateTimeFilter)]
    readonly_fields = ("last_run_at", "last_session", "last_status", "last_message")
    actions = ["run_selected"]
    actions_row = ["run_now"]
    actions_detail = ["run_now"]

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

    @action(description="Run now (test it with the period it would report on)", url_path="run-now")
    def run_now(self, request, object_id):
        self._run(request, AutoRun.objects.get(pk=object_id))
        return redirect(request.META.get("HTTP_REFERER") or "admin:scraper_autorun_changelist")

    @action(description="Run the selected automatic runs now")
    def run_selected(self, request, queryset):
        for auto in queryset:
            self._run(request, auto)


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
    list_display = ("center", "day", "session", "fetched_at")
    list_filter = [("center", AutocompleteSelectFilter), ("day", RangeDateFilter),
                   ("fetched_at", RangeDateTimeFilter)]
    list_select_related = ("center", "session")


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

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("center__groups")

    def get_list_display(self, request):
        return ["center", "groups_column", "month_column", "total_revenue", "new_revenue", "repeat_revenue",
                *[self._metric_column(i, name) for i, name in enumerate(perfwide.extra_metric_names())]]

    @display(description="Groups")
    def groups_column(self, obj):
        return ", ".join(g.name for g in obj.center.groups.all())

    @display(description="Month", ordering="month")
    def month_column(self, obj):
        return obj.month.strftime("%b-%y")

    @staticmethod
    def _metric_column(index, name):
        def column(obj):
            value = obj.metrics.get(name)
            return "" if value is None else f"{value:,.2f}".rstrip("0").rstrip(".")
        column.__name__ = f"metric_{index}"
        return display(description=name)(column)
