from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string


def send_email_warn_quota(
    organization, subscription, event_count: int, plan_event_count: int
):
    template_html = "met-quota-drip.html"
    template_txt = "met-quota-drip.txt"

    subject = f"CHANGE ME {organization.name}"

    base_url = settings.GLITCHTIP_DOMAIN.geturl()
    subscription_link = f"{base_url}/{organization.slug}/settings/subscription"

    context = {
        "organization": organization,
        "subscription": subscription,
        "product": subscription.plan.product,
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
