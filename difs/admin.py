from django.contrib import admin
from .models import DebugInformationFile


class DebugInformationFileAdmin(admin.ModelAdmin):
    pass


admin.site.register(DebugInformationFile, DebugInformationFileAdmin)
