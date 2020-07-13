from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_email_met_quota(organization):
    template_html = "met-quota-drip.html"
    template_txt = "met-quota-drip.txt"

    subject = f"Met event quota for {organization.name}"

    base_url = settings.GLITCHTIP_DOMAIN.geturl()
    event_limit = settings.BILLING_FREE_TIER_EVENTS
    subscription_link = f"{base_url}/settings/{organization.slug}/subscription"

    context = {
        "organization_name": organization.name,
        "event_limit": event_limit,
        "subscription_link": subscription_link,
    }

    text_content = render_to_string(template_txt, context)
    html_content = render_to_string(template_html, context)

    to = [organization.email]
    msg = EmailMultiAlternatives(subject, text_content, to=to)
    msg.attach_alternative(html_content, "text/html")
    msg.send()


def send_invitation_email(self, user, **kwargs):
    template_html = "organizations/email/invite-user-drip.html"
    template_txt = "organizations/email/invite-user-drip.txt"

    context = {
        "token": kwargs["token"],
        "user": user,
        "organization": kwargs["organization"],
    }

    html_content = render_to_string(template_html, context)
    text_content = render_to_string(template_txt, context)
    subject = "You are invited to GlitchTip"

    msg = EmailMultiAlternatives(subject, text_content, to=[user])
    msg.attach_alternative(html_content, "text/html")
    msg.send()
