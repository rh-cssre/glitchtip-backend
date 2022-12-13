from django.contrib import admin
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from .models import Project, ProjectKey, UserProjectAlert


class ProjectKeyResource(resources.ModelResource):
    class Meta:
        model = ProjectKey
        skip_unchanged = True
        fields = ("project", "label", "public_key")
        import_id_fields = (
            "project",
            "public_key",
        )


class ProjectKeyInline(admin.StackedInline):
    model = ProjectKey
    extra = 0
    readonly_fields = ("dsn",)


class ProjectResource(resources.ModelResource):
    class Meta:
        model = Project
        skip_unchanged = True
        fields = ("id", "created", "slug", "name", "organization", "platform")


class ProjectAdmin(ImportExportModelAdmin):
    search_fields = ("name",)
    list_display = ("name", "organization")
    raw_id_fields = ("organization",)
    inlines = [ProjectKeyInline]
    resource_class = ProjectResource


class UserProjectAlertAdmin(admin.ModelAdmin):
    list_display = ("user", "project", "status")
    list_filter = ("status",)
    search_fields = ("project__name", "user__email")
    raw_id_fields = (
        "user",
        "project",
    )


admin.site.register(Project, ProjectAdmin)
admin.site.register(UserProjectAlert, UserProjectAlertAdmin)
