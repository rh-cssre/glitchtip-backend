from django.contrib import admin

from .models import DebugSymbolBundle


@admin.register(DebugSymbolBundle)
class DebugSymbolBundleAdmin(admin.ModelAdmin):
    list_display = [
        "file__name",
        "debug_id",
        "release__version",
        "organization",
        "sourcemap_file__name",
    ]
