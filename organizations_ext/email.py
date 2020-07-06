from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_email_met_quota(organization):
    template_html = "met-quota-drip.html"

    subject = f"Met event quota for {organization.name}"

    base_url = settings.GLITCHTIP_DOMAIN.geturl()
    event_limit = settings.BILLING_FREE_TIER_EVENTS
    subscription_link = f"{base_url}/settings/{organization.slug}/subscription"

    text_content = "You have reached your event quota\n\n"
    text_content += f"Organization: {organization.name} \n\n"
    text_content += f"Your plan allows for {event_limit} events per month, and "
    text_content += "your projects have exceeded that limit.\n\n"
    text_content += "You will no longer receive events until the monthly cycle "
    text_content += "resets, or you upgrade your plan.\n\n"
    text_content += f"Manage your subscription: {subscription_link}"

    html_content = render_to_string(
        template_html,
        {
            "organization_name": organization.name,
            "event_limit": event_limit,
            "subscription_link": subscription_link,
        },
    )

    to = [organization.email]
    msg = EmailMultiAlternatives(subject, text_content, to=to)
    msg.attach_alternative(html_content, "text/html")
    msg.send()
