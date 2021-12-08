from django.contrib import admin
from django.db.models import Sum, Count, Subquery, OuterRef
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)
from projects.models import Project
from performance.models import TransactionEvent
from .models import Organization, OrganizationUser, OrganizationOwner


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
    list_filter = ("is_active", "is_accepting_events")
    inlines = [OrganizationUserInline, OwnerInline]
    show_full_result_count = False

    def issue_events(self, obj):
        return obj.issue_events

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
            total=Count("transactionevent")
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
