from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html
from import_export import resources
from import_export.admin import ImportExportModelAdmin
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)

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


class OrganizationResource(resources.ModelResource):
    class Meta:
        model = Organization
        skip_unchanged = True
        fields = ("id", "slug", "name", "created", "organization")


class OrganizationAdmin(BaseOrganizationAdmin, ImportExportModelAdmin):
    list_per_page = 50
    list_display = [
        "name",
        "is_active",
        "is_accepting_events",
        "issue_events",
        "transaction_events",
        "uptime_check_events",
        "file_size",
        "total_events",
    ]
    readonly_fields = ("customers", "created")
    list_filter = ORGANIZATION_LIST_FILTER
    inlines = [OrganizationUserInline, OwnerInline]
    show_full_result_count = False
    resource_class = OrganizationResource

    def issue_events(self, obj):
        return obj.issue_event_count

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

    def file_size(self, obj):
        return obj.file_size

    def total_events(self, obj):
        return obj.total_event_count

    def get_queryset(self, request):
        qs = self.model.objects.with_event_counts()

        # From super
        ordering = self.ordering or ()
        if ordering:
            qs = qs.order_by(*ordering)

        return qs


class OrganizationUserResource(resources.ModelResource):
    class Meta:
        model = OrganizationUser
        skip_unchanged = True
        fields = (
            "id",
            "user",
            "organization",
            "role",
            "email",
        )
        import_id_fields = ("user", "email", "organization")


class OrganizationUserAdmin(BaseOrganizationUserAdmin, ImportExportModelAdmin):
    list_display = ["user", "organization", "role", "email"]
    resource_class = OrganizationUserResource


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
