from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from .models import Project, ProjectKey


class ProjectKeyInline(admin.StackedInline):
    model = ProjectKey
    extra = 0
    readonly_fields = ("dsn",)


class ProjectResource(resources.ModelResource):
    class Meta:
        model = Project
        fields = ("id", "created", "slug", "name", "organization", "platform")


class ProjectAdmin(ImportExportModelAdmin):
    search_fields = ("name",)
    list_display = ("name", "organization")
    raw_id_fields = ("organization",)
    inlines = [ProjectKeyInline]
    resource_class = ProjectResource


admin.site.register(Project, ProjectAdmin)
