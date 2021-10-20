from django.contrib import admin
from .models import FileBlob, File


class FileBlobAdmin(admin.ModelAdmin):
    pass

class FileAdmin(admin.ModelAdmin):
    pass


admin.site.register(FileBlob, FileBlobAdmin)
admin.site.register(File, FileAdmin)
