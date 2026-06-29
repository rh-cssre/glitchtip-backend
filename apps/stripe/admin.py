from django.conf import settings
from django.contrib import admin
from django.utils.html import format_html

from .models import StripePrice, StripeProduct, StripeSubscription, SupportLicense
from .utils import get_stripe_link


class StripeBaseAdmin(admin.ModelAdmin):
    def has_add_permission(self, request, obj=None):
        return False

    def stripe_link(self, obj):
        return format_html(
            '<a href="{}" target="_blank">{}</a>',
            get_stripe_link(obj.stripe_id),
            obj.stripe_id,
        )

    def get_readonly_fields(self, request, obj=None):
        return (
            list(self.readonly_fields)
            + [field.name for field in obj._meta.fields]
            + [field.name for field in obj._meta.many_to_many]
            + ["stripe_link"]
        )


class StripePriceInline(admin.StackedInline):
    model = StripePrice
    extra = 0
    readonly_fields = ["stripe_id", "nickname", "no_throttle", "price"]


class StripeProductAdmin(StripeBaseAdmin):
    list_display = ["stripe_id", "name", "events", "default_price", "is_public"]
    inlines = [StripePriceInline]


class StripeSubscriptionAdmin(StripeBaseAdmin):
    list_display = [
        "stripe_id",
        "organization",
        "price__product",
        "current_period_start",
        "current_period_end",
        "status",
    ]
    list_filter = ["status", "price__product"]


class SupportLicenseAdmin(admin.ModelAdmin):
    list_display = ["license_key", "billing_email", "updated"]
    fields = ["license_key", "billing_email", "updated"]
    readonly_fields = ["updated"]

    def has_add_permission(self, request):
        return not SupportLicense.objects.exists() and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        # License key is a credential; non-superuser staff must not see it.
        return request.user.is_superuser

    def has_change_permission(self, request, obj=None):
        return request.user.is_superuser

    def has_delete_permission(self, request, obj=None):
        return False


if settings.BILLING_ENABLED:
    admin.site.register(StripeSubscription, StripeSubscriptionAdmin)
    admin.site.register(StripeProduct, StripeProductAdmin)
else:
    # Support license config only makes sense on self-hosted (no Stripe creds).
    admin.site.register(SupportLicense, SupportLicenseAdmin)
