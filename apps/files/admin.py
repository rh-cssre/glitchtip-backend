from django.contrib import admin

from .models import File, FileBlob


@admin.register(FileBlob)
class FileBlobAdmin(admin.ModelAdmin):
    search_fields = ("checksum",)
    list_display = ("checksum", "size", "created")


@admin.register(File)
class FileAdmin(admin.ModelAdmin):
    search_fields = ("name", "checksum")
    list_display = ("name", "type", "checksum", "blob")
    list_filter = ("type", "created")
