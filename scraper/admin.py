import os
import shutil

from django.contrib import admin, messages
from django.utils.html import format_html
from import_export.admin import ExportActionMixin
from unfold.admin import ModelAdmin
from unfold.contrib.import_export.forms import ExportForm
from unfold.decorators import action, display

from centers.models import KnownEmpty

from . import files, jobs
from .models import (CardColumns, CardRow, GymMember, Job, PerfMetric, SalesRecord,
                     SectionResult, Session)

STATUS_LABELS = {"complete": "success", "ok": "success", "done": "success", "incomplete": "warning",
                 "slow": "warning", "empty": "info", "running": "info", "queued": "info",
                 "legacy": "info", "interrupted": "danger", "failed": "danger", "error": "danger",
                 "skipped": "danger"}


def _inside_runs(path):
    """Only ever delete folders that live under runs/."""
    root = os.path.realpath(files.RUNS_DIR)
    real = os.path.realpath(path) if path else ""
    return bool(real) and real.startswith(root + os.sep) and os.path.isdir(real)


@admin.register(Session)
class SessionAdmin(ModelAdmin):
    list_display = ("session_id", "period", "status_badge", "created_at", "centers", "results_link",
                    "missing_count")
    list_filter = ("status",)
    search_fields = ("session_id",)
    readonly_fields = ("session_id", "folder", "coverage", "missing", "created_at", "finished_at")
    actions = ["continue_session", "merge_files"]

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
            job = jobs.enqueue({"session": session.session_id, "workers": session.workers or 2})
        except jobs.JobError as e:
            return messages.error(request, str(e))
        messages.success(request, f"Job {job.id} queued for session {session.session_id}. "
                                  "Watch it on the Run page; the job runner (qcluster) must be running.")

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
class SectionResultAdmin(ModelAdmin):
    list_display = ("session", "center", "report", "status_badge", "row_count", "seconds", "scraped_at")
    list_filter = ("status", "report", "session")
    search_fields = ("center__name", "center__code", "report", "error")
    list_select_related = ("session", "center")
    list_per_page = 100
    show_full_result_count = False
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


class DataAdmin(ExportActionMixin, ModelAdmin):
    """Read-only browser for scraped rows, with CSV / XLSX export."""

    export_form_class = ExportForm
    list_per_page = 50
    show_full_result_count = False
    list_select_related = ("session", "center")
    list_filter = ("session",)

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
    list_display = ("center", "name", "phone", "pack_name", "member_end_date", "renewed", "revenue", "session")
    list_filter = ("session", "renewed", "active_last_30d")
    search_fields = ("name", "phone", "user_id", "center__name", "pack_name")


@admin.register(PerfMetric)
class PerfMetricAdmin(DataAdmin):
    list_display = ("center", "metric_date", "metric", "value", "session")
    list_filter = ("session", "metric")
    search_fields = ("center__name", "metric")


@admin.register(GymMember)
class GymMemberAdmin(DataAdmin):
    list_display = ("center", "username", "pack_name", "final_amount_paid", "status", "new_vs_repeat", "session")
    list_filter = ("session", "status", "new_vs_repeat", "offline_vs_online")
    search_fields = ("username", "user_id", "center__name", "pack_name")


@admin.register(CardRow)
class CardRowAdmin(DataAdmin):
    list_display = ("center", "report", "row_num", "data", "session")
    list_filter = ("session", "report")
    search_fields = ("center__name", "report")


@admin.register(CardColumns)
class CardColumnsAdmin(ModelAdmin):
    list_display = ("report", "columns")


@admin.register(Job)
class JobAdmin(ModelAdmin):
    list_display = ("id", "state_badge", "session", "progress", "started_at", "finished_at")
    list_filter = ("state",)
    readonly_fields = [f.name for f in Job._meta.fields]
    actions = ["request_stop"]

    @display(description="State", label=STATUS_LABELS)
    def state_badge(self, obj):
        return obj.state

    @display(description="Progress")
    def progress(self, obj):
        return f"{obj.done}/{obj.total}" if obj.total else "-"

    @action(description="Ask selected running jobs to stop")
    def request_stop(self, request, queryset):
        for j in queryset:
            jobs.request_stop(j)
        messages.success(request, "Stop requested.")

    def has_add_permission(self, request):
        return False
