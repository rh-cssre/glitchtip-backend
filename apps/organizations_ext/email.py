from typing import TYPE_CHECKING

from django.conf import settings

from apps.stripe.overage import overage_active, overage_eligible
from glitchtip.email import DetailEmail

from .models import Organization, OrganizationUser

if TYPE_CHECKING:
    from apps.stripe.models import StripeProduct


class ThrottleNoticeEmail(DetailEmail):
    html_template_name = "organizations/throttle-notice-drip.html"
    text_template_name = "organizations/throttle-notice-drip.txt"
    subject_template_name = "organizations/throttle-notice-drip-subject.txt"
    model = Organization

    def get_email(self):
        return self.object.email

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_url = settings.GLITCHTIP_URL.geturl()
        faq_link = (
            settings.MARKETING_URL
            + "/documentation/frequently-asked-questions"
            + "#how-can-i-reduce-the-number-of-events-my-organization-is-using-each-month"
        )
        organization = self.object
        subscription_link = f"{base_url}/{organization.slug}/settings/subscription"
        sub = organization.stripe_primary_subscription
        product: StripeProduct | None = sub.price.product if sub else None
        context.update(
            {
                "organization": organization,
                "product": product,
                # A paid org changes plan through us, not by upgrading itself.
                "paid": bool(sub and sub.price.price > 0),
                # On-demand is live, so the throttle means its spend cap is
                # reached; nothing past the cap is charged.
                "at_spend_cap": overage_active(organization),
                "on_demand_available": self._on_demand_available(organization),
                "event_limit": product.events if product else None,
                "total_event_count": getattr(self, "total_event_count", 0),
                "subscription_link": subscription_link,
                "faq_link": faq_link,
            }
        )
        return context

    @staticmethod
    def _on_demand_available(organization: Organization) -> bool:
        """Turning on-demand on would be accepted: the same plan and live
        price tests the configure endpoint applies."""
        from apps.stripe.models import StripePrice

        if organization.metered_billing_enabled:
            return False
        if not overage_eligible(organization.stripe_primary_subscription):
            return False
        return StripePrice.objects.filter(
            is_metered=True,
            active=True,
            product__is_overage=True,
            product__active=True,
        ).exists()


class InvitationEmail(DetailEmail):
    html_template_name = "organizations/invite-user-drip.html"
    text_template_name = "organizations/invite-user-drip.txt"
    subject_template_name = "organizations/invite-user-drip-subject.txt"
    model = OrganizationUser

    def get_email(self):
        return self.object.email

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        org_user = context["object"]
        context["token"] = self.kwargs["token"]
        context["user"] = org_user
        context["organization"] = org_user.organization
        return context
