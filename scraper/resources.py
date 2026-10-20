"""django-import-export resources: one definition of each table's export columns, used by both
the admin's Export button and the per-session report files."""
from import_export import resources
from import_export.fields import Field

from .models import GymMember, PerfMetric, SalesRecord


def _from(attribute, name):
    return Field(attribute=attribute, column_name=name, readonly=True)


class SessionRecordResource(resources.ModelResource):
    """Columns every scraped table shares: where the row came from."""

    center_id = _from("center_id", "center_id")
    center_name = _from("center__name", "center_name")
    center_code = _from("center__code", "center_code")
    start_date = _from("session__start_date", "start_date")
    end_date = _from("session__end_date", "end_date")
    scraped_at = _from("scraped_at", "scraped_at")

    def get_queryset(self):
        return super().get_queryset().select_related("center", "session")


HEAD = ("center_id", "center_name", "center_code", "start_date", "end_date")


class SalesRecordResource(SessionRecordResource):
    class Meta:
        model = SalesRecord
        fields = HEAD + ("user_id", "name", "phone", "pack_name", "member_end_date", "active_last_30d",
                         "renewed", "revenue", "scraped_at")
        export_order = fields


class PerfMetricResource(SessionRecordResource):
    class Meta:
        model = PerfMetric
        fields = HEAD + ("metric_date", "metric", "value", "scraped_at")
        export_order = fields


class GymMemberResource(SessionRecordResource):
    class Meta:
        model = GymMember
        fields = HEAD + ("user_id", "username", "pack_name", "final_amount_paid", "purchase_date",
                         "pack_start_date", "pack_end_date", "status", "offline_vs_online", "new_vs_repeat",
                         "class_level", "last_class_date", "scraped_at")
        export_order = fields
