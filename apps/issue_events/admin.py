from django.contrib import admin

from .models import Issue, IssueEvent


@admin.register(IssueEvent)
class IssueEventAdmin(admin.ModelAdmin):
    list_display = ("id", "timestamp")
    list_filter = ("timestamp",)
    raw_id_fields = ("issue",)
    search_fields = ("id",)
    show_full_result_count = False


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("id", "first_seen")
