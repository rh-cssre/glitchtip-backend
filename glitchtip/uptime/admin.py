from django.conf import settings
from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from django.urls import reverse
from django.utils import timezone

from .models import Monitor, MonitorCheck


class MonitorCheckInlineFormSet(BaseInlineFormSet):
    def get_queryset(self):
        if not hasattr(self, "_queryset"):
            # pylint: disable=attribute-defined-outside-init
            self._queryset = super().get_queryset()[:50]  # noqa
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
    readonly_fields = ["heartbeat_endpoint"]
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

    def heartbeat_endpoint(self, obj):
        if obj.endpoint_id:
            return settings.GLITCHTIP_URL.geturl() + reverse(
                "heartbeat-check",
                kwargs={
                    "organization_slug": obj.organization.slug,
                    "endpoint_id": obj.endpoint_id,
                },
            )


class MonitorCheckAdmin(admin.ModelAdmin):
    list_filter = ["is_up", "reason", "created"]
    list_display = ["monitor", "is_up", "reason", "created", "response_time"]


admin.site.register(Monitor, MonitorAdmin)
admin.site.register(MonitorCheck, MonitorCheckAdmin)
