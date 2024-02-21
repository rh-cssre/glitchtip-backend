from django.contrib import admin

from .models import Release, ReleaseFile


class ReleaseFileInlineAdmin(admin.TabularInline):
    model = ReleaseFile
    fields = ["file", "name"]
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class ReleaseAdmin(admin.ModelAdmin):
    search_fields = ["organization__name", "projects__name"]
    list_display = ["version", "organization"]
    list_filter = ["created"]
    autocomplete_fields = ["organization", "projects"]
    inlines = [ReleaseFileInlineAdmin]


admin.site.register(Release, ReleaseAdmin)
