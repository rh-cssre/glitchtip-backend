from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from .models import Team


class TeamResource(resources.ModelResource):
    class Meta:
        model = Team
        skip_unchanged = True
        fields = ("id", "slug", "created", "organization", "projects", "members")


class TeamAdmin(ImportExportModelAdmin):
    search_fields = ("slug",)
    list_display = ("slug", "organization")
    raw_id_fields = ("organization",)
    filter_horizontal = ("members", "projects")
    resource_class = TeamResource


admin.site.register(Team, TeamAdmin)
