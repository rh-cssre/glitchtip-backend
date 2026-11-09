from django.contrib import admin

from .models import Release


class ReleaseAdmin(admin.ModelAdmin):
    search_fields = ["organization__name", "projects__name"]
    list_display = ["version", "organization"]
    list_filter = ["created"]
    autocomplete_fields = ["organization", "projects"]


admin.site.register(Release, ReleaseAdmin)
