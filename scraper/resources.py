"""django-import-export resources: one definition of each table's export columns, used by both
the admin's Export button and the per-session report files."""
import re

from import_export import resources, widgets
from import_export.fields import Field
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from .numbers import trim
from .models import CenterMonth, GymMember, GymPack, PerfMetric, SalesPack, SalesRecord

_NUMBER = re.compile(r"^[+-]?\d[\d,]*(\.\d+)?%?$")


def safe_cell(value):
    """A cell that is safe to open in Excel: text starting with = + - @ (a formula, e.g. a member named
    =HYPERLINK(...)) gets a leading apostrophe, numbers written as text (-354.5) are left alone, and control
    characters that XLSX cannot hold are dropped instead of failing the whole download."""
    if not isinstance(value, str):
        return value
    value = ILLEGAL_CHARACTERS_RE.sub("", value)
    if value[:1] in ("=", "@", "\t", "\r") or (value[:1] in ("+", "-") and not _NUMBER.match(value)):
        return "'" + value
    return value


class TrimmedFloatWidget(widgets.FloatWidget):
    """Exports 354012.54, not 354012.54000000004."""

    def render(self, value, obj=None, **kwargs):
        return "" if value is None or value == "" else trim(value)


class ModelResource(resources.ModelResource):
    @classmethod
    def widget_from_django_field(cls, f, default=widgets.Widget):
        from django.db import models
        return TrimmedFloatWidget if isinstance(f, models.FloatField) else super().widget_from_django_field(f, default)

    def export_field(self, field, instance, **kwargs):
        return safe_cell(super().export_field(field, instance, **kwargs))

    def get_export_headers(self, *args, **kwargs):
        """Headers too: CenterMonth's metric columns are named after whatever the data holds."""
        return [safe_cell(h) for h in super().get_export_headers(*args, **kwargs)]


def _from(attribute, name):
    return Field(attribute=attribute, column_name=name, readonly=True)


class SessionRecordResource(ModelResource):
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


class MasterResource(ModelResource):
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


class CenterMonthResource(ModelResource):
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
            return safe_cell(", ".join(g.name for g in instance.center.groups.all()))
        if field.column_name == "month":
            return instance.month.strftime("%b-%y")
        if field.column_name in self.metric_names:
            value = instance.metrics.get(field.column_name)
            return trim(value) if isinstance(value, float) else safe_cell(value)
        return super().export_field(field, instance, **kwargs)
