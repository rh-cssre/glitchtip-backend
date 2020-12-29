from django.contrib import admin
from .models import Issue


class IssueAdmin(admin.ModelAdmin):
    list_display = ("id", "type", "created", "project")
    list_filter = ("created", "type")
    raw_id_fields = ("project",)
    search_fields = ("id",)
    show_full_result_count = False


admin.site.register(Issue, IssueAdmin)
