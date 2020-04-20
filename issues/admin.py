from django.contrib import admin
from .models import Event, Issue


class IssueAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "created", "project")
    list_filter = ("created", "type")
    raw_id_fields = ("project",)
    search_fields = ("id",)
    show_full_result_count = False


class EventAdmin(admin.ModelAdmin):
    list_display = ("event_id", "created")
    list_filter = ("created",)
    raw_id_fields = ("issue",)
    search_fields = ("event_id",)
    show_full_result_count = False


admin.site.register(Issue, IssueAdmin)
admin.site.register(Event, EventAdmin)
