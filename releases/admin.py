from django.contrib import admin
from django.forms.models import BaseInlineFormSet
from .models import Release, ReleaseFile


class ReleaseFileInlineFormSet(BaseInlineFormSet):
    def get_queryset(self):
        if not hasattr(self, "_queryset"):
            # pylint: disable=attribute-defined-outside-init
            self._queryset = super().get_queryset()  # noqa
        return self._queryset


class ReleaseFileInlineAdmin(admin.TabularInline):
    model = ReleaseFile
    formset = ReleaseFileInlineFormSet
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
    inlines = [ReleaseFileInlineAdmin]


admin.site.register(Release, ReleaseAdmin)
