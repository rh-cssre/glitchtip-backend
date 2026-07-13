from asgiref.sync import async_to_sync
from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html
from import_export.admin import ImportExportModelAdmin
from organizations.base_admin import (
    BaseOrganizationAdmin,
    BaseOrganizationUserAdmin,
    BaseOwnerInline,
)

from apps.stripe.models import StripeSubscription
from apps.stripe.utils import get_stripe_link

from .models import (
    Organization,
    OrganizationOwner,
    OrganizationSocialApp,
    OrganizationUser,
    get_current_period_dates,
    get_event_counts,
)
from .resources import OrganizationResource, OrganizationUserResource

ORGANIZATION_LIST_FILTER = (
    "is_active",
    "is_accepting_events",
)


class OwnerInline(BaseOwnerInline):
    model = OrganizationOwner


class OrganizationUserInline(admin.StackedInline):
    raw_id_fields = ("user",)
    model = OrganizationUser
    extra = 0


class OrganizationSubscriptionInline(admin.StackedInline):
    model = StripeSubscription
    extra = 0
    readonly_fields = [field.name for field in StripeSubscription._meta.fields]


class OrganizationAdmin(BaseOrganizationAdmin, ImportExportModelAdmin):
    list_display = [
        "name",
        "is_active",
        "is_accepting_events",
    ]
    readonly_fields = (
        "created",
        "issue_events",
        "transaction_events",
        "uptime_check_events",
        "log_events",
        "file_size",
        "total_events",
    )
    list_filter = ORGANIZATION_LIST_FILTER
    inlines = [OrganizationUserInline, OwnerInline]
    show_full_result_count = False
    resource_class = OrganizationResource

    def get_readonly_fields(self, request, obj=None):
        fields = list(super().get_readonly_fields(request, obj))
        fields = list(self.readonly_fields)
        if settings.BILLING_ENABLED:
            fields += ["customer_link", "subscription_link", "max_events"]
        return fields

    def get_inlines(self, request, obj=None):
        inlines = list(self.inlines)
        if settings.BILLING_ENABLED:
            inlines.append(OrganizationSubscriptionInline)
        return inlines

    def _get_event_counts(self, obj):
        """Cached per-request event counts for the detail page."""
        if not hasattr(obj, "_event_counts_cache"):
            period = async_to_sync(get_current_period_dates)(obj)
            start, end = period if period else (None, None)
            obj._event_counts_cache = async_to_sync(get_event_counts)(
                obj.id, start, end
            )
        return obj._event_counts_cache

    def issue_events(self, obj):
        return self._get_event_counts(obj).issue_event_count

    def transaction_events(self, obj):
        return self._get_event_counts(obj).transaction_count

    def uptime_check_events(self, obj):
        return self._get_event_counts(obj).uptime_check_event_count

    def log_events(self, obj):
        return self._get_event_counts(obj).log_count

    def file_size(self, obj):
        return f"{self._get_event_counts(obj).file_size} MB"

    def total_events(self, obj):
        return self._get_event_counts(obj).total_event_count

    def max_events(self, obj):
        if obj.stripe_primary_subscription:
            return obj.stripe_primary_subscription.price.product.events

    def customer_link(self, obj):
        if customer_id := obj.stripe_customer_id:
            return format_html(
                '<a href="{}" target="_blank">{}</a>',
                get_stripe_link(customer_id),
                customer_id,
            )

    def subscription_link(self, obj):
        if subscription_id := obj.stripe_primary_subscription_id:
            return format_html(
                '<a href="{}" target="_blank">{}</a>',
                get_stripe_link(subscription_id),
                subscription_id,
            )

    def get_queryset(self, request):
        qs = super().get_queryset(request)
        if settings.BILLING_ENABLED:
            qs = qs.select_related("stripe_primary_subscription__price__product")
        return qs


class OrganizationUserAdmin(BaseOrganizationUserAdmin, ImportExportModelAdmin):
    list_display = ["user", "organization", "role", "email"]
    search_fields = ("email", "user__email", "organization__name")
    list_filter = ("role",)
    resource_class = OrganizationUserResource


class OrganizationSocialAppAdmin(admin.ModelAdmin):
    list_display = ["organization", "social_app"]
    search_fields = ("organization__name", "social_app__name")


admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationUser, OrganizationUserAdmin)
admin.site.register(OrganizationSocialApp, OrganizationSocialAppAdmin)
