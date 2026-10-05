from django.contrib import admin

from .models import DebugSymbolBundle, Repository


@admin.register(DebugSymbolBundle)
class DebugSymbolBundleAdmin(admin.ModelAdmin):
    list_display = [
        "file__name",
        "debug_id",
        "release__version",
        "organization",
        "sourcemap_file__name",
    ]


@admin.register(Repository)
class RepositoryAdmin(admin.ModelAdmin):
    list_display = ["name", "organization", "status", "created"]
    list_filter = ["status"]
