from django.contrib import admin
from django.db.models import Sum
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
    list_display = ["name", "is_active", "is_accepting_events", "event_count"]
    inlines = [OrganizationUserInline, OwnerInline]
    show_full_result_count = False

    def event_count(self, obj):
        return obj.event_count

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        queryset = queryset.annotate(event_count=Sum("projects__issue__count"))
        return queryset



class OrganizationUserAdmin(BaseOrganizationUserAdmin):
    list_display = ["user", "organization", "role"]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
