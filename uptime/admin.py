from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.utils import timezone
from .models import Monitor, MonitorCheck


class MonitorCheckInlineFormSet(BaseInlineFormSet):
    def get_queryset(self):
        if not hasattr(self, "_queryset"):
            self._queryset = super().get_queryset()[:50]
        return self._queryset


class MonitorCheckInlineAdmin(admin.TabularInline):
    model = MonitorCheck
    formset = MonitorCheckInlineFormSet
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class MonitorAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "is_up",
        "time_since",
        "monitor_type",
        "organization",
        "interval",
    ]
    list_filter = ["monitor_type"]
    search_fields = ["name", "organization__name"]
    inlines = [MonitorCheckInlineAdmin]

    def get_queryset(self, request):
        qs = self.model.objects.with_check_annotations()
        ordering = self.get_ordering(request)
        if ordering:
            qs = qs.order_by(*ordering)
        return qs

    def is_up(self, obj):
        return obj.latest_is_up

    is_up.boolean = True

    def time_since(self, obj):
        if obj.last_change:
            now = timezone.now()
            return now - obj.last_change


class MonitorCheckAdmin(admin.ModelAdmin):
    list_filter = ["is_up", "reason", "created"]
    list_display = ["monitor", "is_up", "reason", "created", "response_time"]


admin.site.register(Monitor, MonitorAdmin)
admin.site.register(MonitorCheck, MonitorCheckAdmin)
