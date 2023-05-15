from django.conf import settings
from django.contrib import admin
from django.db.models import F, PositiveIntegerField
from django.db.models.fields.json import KeyTextTransform
from django.db.models.functions import Cast
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


class GlitchTipBaseOrganizationAdmin(BaseOrganizationAdmin):
    readonly_fields = ("customers", "created")
    list_filter = ORGANIZATION_LIST_FILTER
    inlines = [OrganizationUserInline, OwnerInline]
    show_full_result_count = False

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


class OrganizationAdmin(GlitchTipBaseOrganizationAdmin, ImportExportModelAdmin):
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
    resource_class = OrganizationResource

    def get_queryset(self, request):
        qs = self.model.objects.with_event_counts()

        # From super
        ordering = self.ordering or ()
        if ordering:
            qs = qs.order_by(*ordering)

        return qs


class OrganizationSubscription(Organization):
    class Meta:
        proxy = True


class IsOverListFilter(admin.SimpleListFilter):
    title = "Is over plan limit"
    parameter_name = "is_over"

    def lookups(self, request, model_admin):
        return (
            (True, "Yes"),
            (False, "No"),
        )

    def queryset(self, request, queryset):
        if self.value() is not None:
            queryset = queryset.filter(max_events__isnull=False)
        if self.value() is False:
            return queryset.filter(total_event_count__lte=F("max_events"))
        if self.value() is True:
            return queryset.filter(total_event_count__gt=F("max_events"))
        return queryset


class OrganizationSubscriptionAdmin(GlitchTipBaseOrganizationAdmin):
    list_display = [
        "name",
        "is_active",
        "is_accepting_events",
        "issue_events",
        "transaction_events",
        "uptime_check_events",
        "file_size",
        "total_events",
        "max_events",
    ]

    def max_events(self, obj):
        return obj.max_events

    def get_queryset(self, request):
        qs = self.model.objects.with_event_counts().annotate(
            max_events=Cast(
                KeyTextTransform(
                    "events",
                    "djstripe_customers__subscriptions__plan__product__metadata",
                ),
                output_field=PositiveIntegerField(),
            )
        )
        # From super
        ordering = self.ordering or ()
        if ordering:
            qs = qs.order_by(*ordering)

        return qs

    list_filter = GlitchTipBaseOrganizationAdmin.list_filter + (IsOverListFilter,)


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
    search_fields = ("email", "user__email", "organization__name")
    list_filter = ("role",)
    resource_class = OrganizationUserResource


admin.site.register(Organization, OrganizationAdmin)
if settings.BILLING_ENABLED:
    admin.site.register(OrganizationSubscription, OrganizationSubscriptionAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
