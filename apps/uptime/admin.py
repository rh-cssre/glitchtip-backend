from django.conf import settings
from django.contrib import admin
from django.urls import reverse
from django.utils import timezone

from .models import Monitor, StatusPage


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
                "api:heartbeat_check",
                kwargs={
                    "organization_slug": obj.organization.slug,
                    "endpoint_id": obj.endpoint_id,
                },
            )


class StatusPageAdmin(admin.ModelAdmin):
    list_display = ["organization", "name", "is_public"]


admin.site.register(Monitor, MonitorAdmin)
admin.site.register(StatusPage, StatusPageAdmin)
