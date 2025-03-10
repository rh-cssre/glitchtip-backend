from typing import TYPE_CHECKING

from django.conf import settings

from glitchtip.email import DetailEmail

from .models import Organization, OrganizationUser

if TYPE_CHECKING:
    from apps.stripe.models import StripeProduct


class ThrottleNoticeEmail(DetailEmail):
    html_template_name = "organizations/throttle-notice-drip.html"
    text_template_name = "organizations/throttle-notice-drip.txt"
    subject_template_name = "organizations/throttle-notice-drip-subject.txt"
    model = Organization

    def get_object(self, *args, **kwargs):
        return super().get_object(queryset=Organization.objects.with_event_counts())

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
        product: StripeProduct | None = None
        if organization.stripe_primary_subscription:
            product = organization.stripe_primary_subscription.price.product
        context.update(
            {
                "organization": organization,
                "product": product,
                "event_limit": product.events if product else None,
                "subscription_link": subscription_link,
                "faq_link": faq_link,
            }
        )
        return context


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
