from django.contrib import admin
from import_export.admin import ImportExportModelAdmin

from .models import Team
from .resources import TeamResource


class TeamAdmin(ImportExportModelAdmin):
    search_fields = ("slug",)
    list_display = ("slug", "organization")
    raw_id_fields = ("organization",)
    filter_horizontal = ("members", "projects")
    resource_class = TeamResource


admin.site.register(Team, TeamAdmin)
