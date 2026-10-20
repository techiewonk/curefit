"""Shared base for the admin lists: Unfold filters plus a CSV / XLSX download of exactly what is filtered."""
from import_export.admin import ExportActionMixin
from import_export.formats.base_formats import CSV, XLSX
from unfold.admin import ModelAdmin
from unfold.contrib.import_export.forms import ExportForm


class ExportModelAdmin(ExportActionMixin, ModelAdmin):
    """List with search and filters; the Export button downloads the rows currently shown
    (filters, search and ordering applied) as CSV or XLSX, "Export selected" only the ticked ones."""

    export_form_class = ExportForm
    formats = [CSV, XLSX]
    list_filter_submit = True      # a button applies the range filters
    show_full_result_count = False
