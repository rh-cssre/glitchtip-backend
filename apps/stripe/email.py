from urllib.parse import urlencode

from glitchtip.email import GlitchTipEmail


class SupportLicenseWelcomeEmail(GlitchTipEmail):
    """Sent once when a support-plan license is purchased.

    `license_key` is the Stripe subscription id (sub_xxx). The deep link
    pre-fills the key on the marketing support form; no email in the URL.
    """

    html_template_name = "stripe/support-welcome.html"
    text_template_name = "stripe/support-welcome.txt"
    subject_template_name = "stripe/support-welcome-subject.txt"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        license_key = self.kwargs["license_key"]
        context["license_key"] = license_key
        context["support_url"] = "https://glitchtip.com/support#" + urlencode(
            {"sub": license_key}
        )
        return context
