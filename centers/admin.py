from django.contrib import admin, messages
from unfold.contrib.filters.admin import (AllValuesCheckboxFilter, AutocompleteSelectFilter, BooleanRadioFilter,
                                          RangeDateFilter, RangeDateTimeFilter, RangeNumericFilter)
from unfold.decorators import action, display

from config.admin_base import ExportModelAdmin

from .models import Center, CenterGroup, CenterList, CenterSyncLog, KnownEmpty


@admin.register(Center)
class CenterAdmin(ExportModelAdmin):
    list_display = ("center_id", "name", "code", "state_badge", "in_groups", "monthly_target", "center_type",
                    "cluster", "in_lists")
    list_editable = ("monthly_target",)
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


@admin.register(CenterGroup)
class CenterGroupAdmin(ExportModelAdmin):
    """Make, rename or delete a group, and add or remove its gyms (type in the box to find a center)."""

    list_display = ("name", "position", "center_count", "updated_at")
    list_editable = ("position",)
    search_fields = ("name",)
    autocomplete_fields = ("centers",)

    @display(description="Centers")
    def center_count(self, obj):
        return obj.centers.count()
