from django.contrib import admin
from .models import FileBlob, File


class FileBlobAdmin(admin.ModelAdmin):
    search_fields = ("checksum",)
    list_display = ("checksum", "size", "created")


class FileAdmin(admin.ModelAdmin):
    search_fields = ("name", "checksum")
    list_display = ("name", "type", "checksum", "blob")
    list_filter = ("type", "created")


admin.site.register(FileBlob, FileBlobAdmin)
admin.site.register(File, FileAdmin)
