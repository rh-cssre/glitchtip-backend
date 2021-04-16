from django.contrib import admin
from .models import FileBlob


class FileBlobAdmin(admin.ModelAdmin):
    pass


admin.site.register(FileBlob, FileBlobAdmin)
