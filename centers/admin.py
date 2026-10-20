from django.contrib import admin, messages
from unfold.admin import ModelAdmin
from unfold.decorators import action, display

from .models import Center, CenterList, CenterSyncLog, KnownEmpty


@admin.register(Center)
class CenterAdmin(ModelAdmin):
    list_display = ("center_id", "name", "code", "state_badge", "center_type", "cluster", "in_lists")
    list_filter = ("state", "cluster", "center_type", "upcoming")
    search_fields = ("name", "code", "center_id", "locality", "cluster")
    list_per_page = 100
    show_full_result_count = False
    actions = ["add_to_actual", "add_to_test", "remove_from_actual", "remove_from_test"]

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
class CenterListAdmin(ModelAdmin):
    list_display = ("name", "description", "center_count", "updated_at")
    autocomplete_fields = ("centers",)

    @display(description="Centers")
    def center_count(self, obj):
        return obj.centers.count()


@admin.register(CenterSyncLog)
class CenterSyncLogAdmin(ModelAdmin):
    list_display = ("synced_at", "total", "active", "added_count", "removed_count", "changed_count")
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
class KnownEmptyAdmin(ModelAdmin):
    list_display = ("center", "report", "note", "since", "last_checked")
    list_filter = ("report",)
    search_fields = ("center__name", "report", "note")
    autocomplete_fields = ("center",)
