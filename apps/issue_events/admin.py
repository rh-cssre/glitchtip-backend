from django.contrib import admin

from .models import IssueEvent, Issue


@admin.register(IssueEvent)
class IssueEventAdmin(admin.ModelAdmin):
    list_display = ("id", "date_created")
    list_filter = ("date_created",)
    raw_id_fields = ("issue",)
    search_fields = ("id",)
    show_full_result_count = False


@admin.register(Issue)
class IssueAdmin(admin.ModelAdmin):
    list_display = ("id", "created")
