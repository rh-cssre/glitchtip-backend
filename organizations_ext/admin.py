from django.contrib import admin
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)
from .models import Organization, OrganizationUser, OrganizationOwner


class OwnerInline(BaseOwnerInline):
    model = OrganizationOwner


class OrganizationUserInline(admin.StackedInline):
    raw_id_fields = ("user",)
    model = OrganizationUser
    extra = 0


class OrganizationAdmin(BaseOrganizationAdmin):
    inlines = [OrganizationUserInline, OwnerInline]


class OrganizationUserAdmin(BaseOrganizationUserAdmin):
    list_display = ["user", "organization", "role"]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
