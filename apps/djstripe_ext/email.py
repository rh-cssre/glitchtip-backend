from django.conf import settings
from djstripe.models import Subscription

from glitchtip.email import DetailEmail


class WarnQuotaEmail(DetailEmail):
    html_template_name = "djstripe_ext/near-quota-drip.html"
    text_template_name = "djstripe_ext/near-quota-drip.txt"
    subject_template_name = "djstripe_ext/near-quota-drip-subject.txt"
    model = Subscription
    event_count = None

    def get_email(self):
        return self.object.customer.subscriber.email

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        base_url = settings.GLITCHTIP_URL.geturl()
        subscription = context["object"]
        organization = subscription.customer.subscriber
        plan_event_count = subscription.plan.product.metadata.get("events")
        subscription_link = f"{base_url}/{organization.slug}/settings/subscription"
        context["organization"] = organization.name
        context["plan_event_count"] = plan_event_count
        context["event_count"] = self.event_count
        context["subscription_link"] = subscription_link
        return context
