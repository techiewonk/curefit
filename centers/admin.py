from django import forms
from django.contrib import admin, messages
from django.templatetags.static import static
from django.utils.html import format_html
from unfold.contrib.filters.admin import (AllValuesCheckboxFilter, AutocompleteSelectFilter, BooleanRadioFilter,
                                          RangeDateFilter, RangeDateTimeFilter, RangeNumericFilter)
from unfold.decorators import action, display
from unfold.widgets import UnfoldAdminFileFieldWidget, UnfoldAdminRadioSelectWidget

from config.admin_base import ExportModelAdmin

from . import group_csv
from .models import Center, CenterGroup, CenterList, CenterSyncLog, KnownEmpty


@admin.register(Center)
class CenterAdmin(ExportModelAdmin):
    list_display = ("center_id", "name", "code", "state_badge", "in_groups", "monthly_target", "note",
                    "center_type", "cluster", "in_lists")
    list_editable = ("monthly_target", "note")
    list_filter = [("state", AllValuesCheckboxFilter), ("groups__name", AllValuesCheckboxFilter),
                   ("monthly_target", RangeNumericFilter), ("cluster", AllValuesCheckboxFilter),
                   ("center_type", AllValuesCheckboxFilter), ("capacity", RangeNumericFilter),
                   ("launch_date", RangeDateFilter), ("shutdown_date", RangeDateFilter),
                   ("upcoming", BooleanRadioFilter), ("is_published", BooleanRadioFilter),
                   ("lists__name", AllValuesCheckboxFilter)]
    search_fields = ("name", "code", "center_id", "locality", "cluster")
    list_per_page = 100
    actions = ["add_to_actual", "add_to_test", "remove_from_actual", "remove_from_test"]

    @display(description="Groups")
    def in_groups(self, obj):
        return ", ".join(g.name for g in obj.groups.all())

    def get_queryset(self, request):
        return super().get_queryset(request).prefetch_related("groups")

    def get_actions(self, request):
        """One 'add to' and one 'remove from' action for every group that exists."""
        actions = super().get_actions(request)
        for group in CenterGroup.objects.all():
            for verb, adding in (("Add to", True), ("Remove from", False)):
                name = f"{'add' if adding else 'remove'}_group_{group.pk}"
                actions[name] = (self._group_action(group.pk, adding), name, f"{verb} group: {group.name}")
        return actions

    @staticmethod
    def _group_action(group_id, adding):
        def run(modeladmin, request, queryset):
            group = CenterGroup.objects.get(pk=group_id)
            (group.centers.add if adding else group.centers.remove)(*queryset)
            messages.success(request, f"{queryset.count()} center(s) {'added to' if adding else 'removed from'} {group.name}.")
        return run

    @display(description="State", label={"ACTIVE": "success", "INACTIVE": "danger"})
    def state_badge(self, obj):
        return obj.state or "-"

    @display(description="Lists")
    def in_lists(self, obj):
        return ", ".join(obj.lists.values_list("name", flat=True))

    def _change_list(self, request, queryset, name, add):
        lst, _ = CenterList.objects.get_or_create(name=name)
        (lst.centers.add if add else lst.centers.remove)(*queryset)
        messages.success(request, f"{queryset.count()} center(s) {'added to' if add else 'removed from'} the {name} list.")

    @action(description="Add to Actual list")
    def add_to_actual(self, request, queryset):
        self._change_list(request, queryset, "actual", True)

    @action(description="Add to Test list")
    def add_to_test(self, request, queryset):
        self._change_list(request, queryset, "test", True)

    @action(description="Remove from Actual list")
    def remove_from_actual(self, request, queryset):
        self._change_list(request, queryset, "actual", False)

    @action(description="Remove from Test list")
    def remove_from_test(self, request, queryset):
        self._change_list(request, queryset, "test", False)

    def has_add_permission(self, request):
        return False  # centers come from Kronos


@admin.register(CenterList)
class CenterListAdmin(ExportModelAdmin):
    list_display = ("name", "description", "center_count", "updated_at")
    list_filter = [("updated_at", RangeDateTimeFilter)]
    autocomplete_fields = ("centers",)

    @display(description="Centers")
    def center_count(self, obj):
        return obj.centers.count()


@admin.register(CenterSyncLog)
class CenterSyncLogAdmin(ExportModelAdmin):
    list_display = ("synced_at", "total", "active", "added_count", "removed_count", "changed_count")
    list_filter = [("synced_at", RangeDateTimeFilter), ("total", RangeNumericFilter)]
    readonly_fields = [f.name for f in CenterSyncLog._meta.fields]

    @display(description="New")
    def added_count(self, obj):
        return len(obj.added)

    @display(description="Removed")
    def removed_count(self, obj):
        return len(obj.removed)

    @display(description="State changes")
    def changed_count(self, obj):
        return len(obj.state_changed)

    def has_add_permission(self, request):
        return False


@admin.register(KnownEmpty)
class KnownEmptyAdmin(ExportModelAdmin):
    list_display = ("center", "report", "note", "since", "last_checked")
    list_filter = [("center", AutocompleteSelectFilter), ("report", AllValuesCheckboxFilter),
                   ("since", RangeDateTimeFilter), ("last_checked", RangeDateTimeFilter)]
    search_fields = ("center__name", "report", "note")
    autocomplete_fields = ("center",)


class CenterGroupForm(forms.ModelForm):
    """The group form plus an optional CSV of its gyms (a column of center IDs)."""

    gyms_csv = forms.FileField(
        required=False, label="Gyms from a CSV file", widget=UnfoldAdminFileFieldWidget,
        help_text="Optional. A CSV with the center IDs in a column headed \"ID\"; a Name column beside it is "
                  "only for you. Unknown IDs stop the save and are listed.")
    csv_mode = forms.ChoiceField(
        label="The CSV's gyms", initial="add", widget=UnfoldAdminRadioSelectWidget,
        choices=[("add", "Add to the gyms below"), ("replace", "Replace the gyms: the group gets exactly the file's gyms")])

    class Meta:
        model = CenterGroup
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["gyms_csv"].help_text = format_html(
            '{} <a class="text-primary-600 underline" href="{}" download>Download a sample CSV</a>',
            self.fields["gyms_csv"].help_text, static("centers/group_sample.csv"))

    def clean_gyms_csv(self):
        upload = self.cleaned_data.get("gyms_csv")
        self.csv_ids, self.csv_problems = [], []
        if not upload:
            return upload
        try:
            ids, problems = group_csv.read_ids(upload)
        except ValueError as e:
            raise forms.ValidationError(str(e))
        known = set(Center.objects.filter(center_id__in=ids).values_list("center_id", flat=True))
        unknown = [i for i in ids if i not in known]
        if unknown:
            shown = ", ".join(map(str, unknown[:20])) + (f" and {len(unknown) - 20} more" if len(unknown) > 20 else "")
            raise forms.ValidationError(f"{len(unknown)} ID{'s' if len(unknown) > 1 else ''} match no center: {shown}. "
                                        "Fix the file (or run Centers → Update first) and upload it again.")
        self.csv_ids, self.csv_problems = ids, problems
        return upload


@admin.register(CenterGroup)
class CenterGroupAdmin(ExportModelAdmin):
    """Make, rename or delete a group, and add or remove its gyms (type in the box to find a center,
    or upload a CSV of center IDs)."""

    form = CenterGroupForm
    list_display = ("name", "position", "center_count", "updated_at")
    list_editable = ("position",)
    search_fields = ("name",)
    autocomplete_fields = ("centers",)
    fieldsets = [(None, {"fields": ["name", "position", "centers"]}),
                 ("Upload gyms (CSV)", {"fields": ["gyms_csv", "csv_mode"]})]

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)  # the gyms picked in the box first
        ids = getattr(form, "csv_ids", [])
        if not ids:
            return
        group = form.instance
        before = set(group.centers.values_list("center_id", flat=True))
        if form.cleaned_data.get("csv_mode") == "replace":
            group.centers.set(ids)
        else:
            group.centers.add(*ids)
        after = set(group.centers.values_list("center_id", flat=True))
        messages.success(request, f"{group.name}: {len(after - before)} gyms added and {len(before - after)} removed "
                                  f"from the CSV ({len(ids)} IDs); the group now has {len(after)} gyms.")
        for line in form.csv_problems[:10]:
            messages.warning(request, f"Skipped {line}.")

    @display(description="Centers")
    def center_count(self, obj):
        return obj.centers.count()
