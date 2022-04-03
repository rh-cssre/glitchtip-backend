from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from .models import User, UserProjectAlert


class UserAdmin(BaseUserAdmin):
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
            {"fields": ("name", "subscribe_by_default", "analytics")},
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

    def organizations(self, obj):
        return ", ".join(
            obj.organizations_ext_organization.all().values_list("name", flat=True)
        )


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
