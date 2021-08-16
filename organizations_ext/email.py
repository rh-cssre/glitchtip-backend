from django.conf import settings
from glitchtip.email import DetailEmail
from .models import Organization, OrganizationUser


class MetQuotaEmail(DetailEmail):
    html_template_name = "organizations/met-quota-drip.html"
    text_template_name = "organizations/met-quota-drip.txt"
    subject_template_name = "organizations/met-quota-drip-subject.txt"
    model = Organization

    def get_email(self):
        return self.object.email

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_url = settings.GLITCHTIP_URL.geturl()
        event_limit = settings.BILLING_FREE_TIER_EVENTS
        organization = self.object
        subscription_link = f"{base_url}/{organization.slug}/settings/subscription"
        context["organization_name"] = organization.name
        context["event_limit"] = event_limit
        context["subscription_link"] = subscription_link
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
