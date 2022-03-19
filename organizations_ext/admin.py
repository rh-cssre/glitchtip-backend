from django.contrib import admin
from django.conf import settings
from django.db.models import Count, OuterRef, Subquery, Sum
from django.utils.html import format_html
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)

from performance.models import TransactionEvent
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
        return obj.issue_events

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
        return obj.transaction_events

    def uptime_check_events(self, obj):
        return obj.uptime_check_events

    def total_events(self, obj):
        total = 0
        if obj.issue_events:
            total += obj.issue_events
        if obj.transaction_events:
            total += obj.transaction_events
        if obj.uptime_check_events:
            total += obj.uptime_check_events
        return total

    def get_queryset(self, request):
        queryset = super().get_queryset(request)

        projects = Project.objects.filter(organization=OuterRef("pk")).values(
            "organization"
        )
        total_issue_events = projects.annotate(total=Sum("issue__count")).values(
            "total"
        )
        total_transaction_events = projects.annotate(
            total=Count("transactiongroup__transactionevent")
        ).values("total")

        queryset = queryset.annotate(
            issue_events=Subquery(total_issue_events),
            transaction_events=Subquery(total_transaction_events),
            uptime_check_events=Count("monitor__checks"),
        )
        return queryset


class OrganizationUserAdmin(BaseOrganizationUserAdmin):
    list_display = ["user", "organization", "role"]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
