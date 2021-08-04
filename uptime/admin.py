from django.contrib import admin
from django.db.models import Subquery, OuterRef
from django.forms.models import BaseInlineFormSet
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
    list_display = ["name", "is_up", "monitor_type", "organization", "interval"]
    list_filter = ["monitor_type"]
    search_fields = ["name", "organization__name"]
    inlines = [MonitorCheckInlineAdmin]

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.annotate(
            is_up=Subquery(
                MonitorCheck.objects.filter(monitor_id=OuterRef("id"))
                .order_by("-start_check")
                .values("is_up")[:1]
            )
        )

    def is_up(self, obj):
        return obj.is_up

    is_up.boolean = True


class MonitorCheckAdmin(admin.ModelAdmin):
    list_filter = ["is_up", "reason", "created"]
    list_display = ["monitor", "is_up", "reason", "created", "response_time"]


admin.site.register(Monitor, MonitorAdmin)
admin.site.register(MonitorCheck, MonitorCheckAdmin)
