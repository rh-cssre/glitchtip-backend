from django.contrib import admin

from .models import IssueEvent


class IssueEventAdmin(admin.ModelAdmin):
    list_display = ("id", "created")
    list_filter = ("created",)
    raw_id_fields = ("issue",)
    search_fields = ("id",)
    show_full_result_count = False


admin.site.register(IssueEvent, IssueEventAdmin)
