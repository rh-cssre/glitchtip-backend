from django.conf import settings
from django.contrib import admin
from django.db.models import Count, OuterRef, Subquery, Sum
from django.utils.html import format_html
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)

from projects.models import Project

from .models import Organization, OrganizationOwner, OrganizationUser

ORGANIZATION_LIST_FILTER = (
    "is_active",
    "is_accepting_events",
)
if settings.BILLING_ENABLED:
    ORGANIZATION_LIST_FILTER += ("djstripe_customers__subscriptions__plan__product",)


class OwnerInline(BaseOwnerInline):
    model = OrganizationOwner


class OrganizationUserInline(admin.StackedInline):
    raw_id_fields = ("user",)
    model = OrganizationUser
    extra = 0


class OrganizationAdmin(BaseOrganizationAdmin):
    list_per_page = 50
    list_display = [
        "name",
        "is_active",
        "is_accepting_events",
        "issue_events",
        "transaction_events",
        "uptime_check_events",
        "total_events",
    ]
    readonly_fields = ("customers",)
    list_filter = ORGANIZATION_LIST_FILTER
    inlines = [OrganizationUserInline, OwnerInline]
    show_full_result_count = False

    def issue_events(self, obj):
        return obj.event_count

    def customers(self, obj):
        return format_html(
            " ".join(
                [
                    f'<a href="{customer.get_stripe_dashboard_url()}" target="_blank">{customer.id}</a>'
                    for customer in obj.djstripe_customers.all()
                ]
            )
        )

    def transaction_events(self, obj):
        return obj.transaction_count

    def uptime_check_events(self, obj):
        return obj.uptime_check_event_count

    def total_events(self, obj):
        return obj.event_count + obj.transaction_count + obj.uptime_check_event_count

    def get_queryset(self, request):
        queryset = self.model.objects.with_event_counts(False)

        # From super
        ordering = self.ordering or ()
        if ordering:
            qs = qs.order_by(*ordering)

        return queryset


class OrganizationUserAdmin(BaseOrganizationUserAdmin):
    list_display = ["user", "organization", "role"]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
