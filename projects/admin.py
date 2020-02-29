from django.contrib import admin
from .models import Project, ProjectKey


class ProjectKeyInline(admin.StackedInline):
    model = ProjectKey
    extra = 0
    readonly_fields = ("dsn",)


class ProjectAdmin(admin.ModelAdmin):
    inlines = [ProjectKeyInline]


admin.site.register(Project, ProjectAdmin)
