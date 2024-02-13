from django.contrib import admin

from .models import DebugInformationFile


@admin.register(DebugInformationFile)
class DebugInformationFileAdmin(admin.ModelAdmin):
    pass
