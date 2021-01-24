from django.contrib import admin
from django.db.models import Sum, Count
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)
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
        "total_events",
    ]
    list_filter = ("is_active", "is_accepting_events")
    inlines = [OrganizationUserInline, OwnerInline]
    show_full_result_count = False

    def issue_events(self, obj):
        return obj.issue_events

    def transaction_events(self, obj):
        # Produces 1 query per row, but is fast enough and keeps queryset simple
        return TransactionEvent.objects.filter(project__organization=obj).count()

    def total_events(self, obj):
        total = 0
        issue = self.issue_events(obj)
        if issue:
            total += issue
        total += self.transaction_events(obj)
        return total

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        queryset = queryset.annotate(issue_events=Sum("projects__issue__count"),)
        return queryset


class OrganizationUserAdmin(BaseOrganizationUserAdmin):
    list_display = ["user", "organization", "role"]


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
