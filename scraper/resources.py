"""django-import-export resources: one definition of each table's export columns, used by both
the admin's Export button and the per-session report files."""
from import_export import resources
from import_export.fields import Field

from .models import CenterMonth, GymMember, GymPack, PerfMetric, SalesPack, SalesRecord


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


class MasterResource(resources.ModelResource):
    """The all-runs tables: no single session, but when each pack was last seen."""

    center_id = _from("center_id", "center_id")
    center_name = _from("center__name", "center_name")
    center_code = _from("center__code", "center_code")
    last_seen = _from("scraped_at", "last_seen")
    last_session = _from("last_session_id", "last_session")

    def get_queryset(self):
        return super().get_queryset().select_related("center")


MASTER_HEAD = ("center_id", "center_name", "center_code")


class GymPackResource(MasterResource):
    class Meta:
        model = GymPack
        fields = MASTER_HEAD + ("user_id", "username", "pack_name", "final_amount_paid", "purchase_date",
                                "pack_start_date", "pack_end_date", "status", "offline_vs_online", "new_vs_repeat",
                                "class_level", "last_class_date", "last_seen", "last_session")
        export_order = fields


class SalesPackResource(MasterResource):
    class Meta:
        model = SalesPack
        fields = MASTER_HEAD + ("user_id", "name", "phone", "pack_name", "member_end_date", "active_last_30d",
                                "renewed", "revenue", "last_seen", "last_session")
        export_order = fields


class CenterMonthResource(resources.ModelResource):
    """Center x month with a column for every metric (the names come from the data, so they are added here)."""

    center_id = _from("center_id", "center_id")
    center_name = _from("center__name", "center")
    group = Field(column_name="group", readonly=True)
    month_label = Field(column_name="month", readonly=True)

    class Meta:
        model = CenterMonth
        fields = ("center_id", "center_name", "group", "month_label", "total_revenue", "new_revenue", "repeat_revenue")
        export_order = fields

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        from .perfwide import extra_metric_names
        self.metric_names = extra_metric_names()
        for name in self.metric_names:
            self.fields[name] = Field(column_name=name, readonly=True)

    def get_queryset(self):
        return super().get_queryset().select_related("center").prefetch_related("center__groups")

    def get_export_order(self):
        return list(self._meta.export_order) + self.metric_names

    def export_field(self, field, instance, **kwargs):
        if field.column_name == "group":
            return ", ".join(g.name for g in instance.center.groups.all())
        if field.column_name == "month":
            return instance.month.strftime("%b-%y")
        if field.column_name in self.metric_names:
            return instance.metrics.get(field.column_name)
        return super().export_field(field, instance, **kwargs)
