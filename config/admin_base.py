"""Shared base for the admin lists: Unfold filters plus a CSV / XLSX download of exactly what is filtered."""
from constance.forms import ConstanceForm
from django import forms
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
    list_fullwidth = True          # lists use the whole screen width
    list_horizontal_scrollbar_top = True
    show_full_result_count = False


class SettingsForm(ConstanceForm):
    """System → Settings. Each value has its own range (CONSTANCE_ADDITIONAL_FIELDS); this adds the checks
    that involve two values."""

    def clean(self):
        data = super().clean()
        low, high = data.get("DELAY_MIN"), data.get("DELAY_MAX")
        if low is not None and high is not None and low > high:
            self.add_error("DELAY_MIN", forms.ValidationError(
                "The shortest pause must not be longer than the longest pause."))
        return data
