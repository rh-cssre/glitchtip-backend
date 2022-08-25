from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from import_export import resources
from import_export.admin import ImportExportModelAdmin

from .models import User, UserProjectAlert


class UserResource(resources.ModelResource):
    class Meta:
        model = User
        skip_unchanged = True
        fields = (
            "id",
            # "password",
            "is_superuser",
            "email",
            "name",
            "is_staff",
            "is_active",
            "created",
        )


class UserAdmin(BaseUserAdmin, ImportExportModelAdmin):
    ordering = ("email",)
    list_display = (
        "email",
        "name",
        "organizations",
    )
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "email",
                    "password",
                )
            },
        ),
        (
            _("Personal info"),
            {"fields": ("name", "subscribe_by_default", "analytics", "options")},
        ),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                ),
            },
        ),
        (_("Important dates"), {"fields": ("last_login",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "password1", "password2"),
            },
        ),
    )
    search_fields = ("email", "name")
    readonly_fields = ("analytics",)
    resource_class = UserResource

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .prefetch_related("organizations_ext_organization")
        )

    def organizations(self, obj):
        return ", ".join([org.name for org in obj.organizations_ext_organization.all()])


class UserProjectAlertAdmin(admin.ModelAdmin):
    list_display = ("user", "project", "status")
    list_filter = ("status",)
    search_fields = ("project__name", "user__email")
    raw_id_fields = (
        "user",
        "project",
    )


admin.site.register(User, UserAdmin)
admin.site.register(UserProjectAlert, UserProjectAlertAdmin)
