from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_email_warn_quota(
    organization, subscription, event_count: int, plan_event_count: int
):
    template_html = "near-quota-drip.html"
    template_txt = "near-quota-drip.txt"

    subject = f"Nearing event quota for {organization.name}"

    base_url = settings.GLITCHTIP_URL.geturl()
    subscription_link = f"{base_url}/{organization.slug}/settings/subscription"

    context = {
        "organization": organization.name,
        "plan_event_count": plan_event_count,
        "event_count": event_count,
        "subscription_link": subscription_link,
    }

    text_content = render_to_string(template_txt, context)
    html_content = render_to_string(template_html, context)

    to = [organization.email]
    msg = EmailMultiAlternatives(subject, text_content, to=to)
    msg.attach_alternative(html_content, "text/html")
    msg.send()
