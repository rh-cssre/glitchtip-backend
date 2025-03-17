from typing import Optional

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.views.generic.base import ContextMixin
from django.views.generic.detail import SingleObjectMixin

User = get_user_model()


class GlitchTipEmail(ContextMixin):
    html_template_name = None
    text_template_name = None
    subject_template_name = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        for key, value in kwargs.items():
            setattr(self, key, value)

    def get_html_template_name(self):
        return self.html_template_name

    def get_text_template_name(self):
        return self.text_template_name

    def get_subject_template_name(self):
        return self.subject_template_name

    def get_text_content(self, context):
        return render_to_string(self.get_text_template_name(), context)

    def get_html_content(self, context):
        return render_to_string(self.get_html_template_name(), context)

    def get_subject_content(self, context):
        return render_to_string(self.get_subject_template_name(), context).strip("\n")[
            :998
        ]

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["base_url"] = settings.GLITCHTIP_URL.geturl()
        return context

    def _send_email(self, context, to, users=None):
        subject = self.get_subject_content(context)
        msg = EmailMultiAlternatives(subject, self.get_text_content(context), to=to)
        if users:
            msg.merge_metadata = {user.email: {"unique_id": user.id} for user in users}
        msg.attach_alternative(self.get_html_content(context), "text/html")
        msg.send()

    def get_users(self):
        return User.objects.none()

    def send_users_email(self, users=None):
        """
        Send email to Django users, will set merge data to avoid exposing email addresses
        """
        context = self.get_context_data()
        if users is None:
            users = self.get_users()
        to = users.values_list("email", flat=True)
        self._send_email(context, to, users)

    def send_email(self, email: str):
        """Send just one email"""
        context = self.get_context_data()
        self._send_email(context, [email])


class DetailEmail(SingleObjectMixin, GlitchTipEmail):
    def get_email(self):
        raise NotImplementedError

    def send_users_email(self, users=None):
        self.object = self.get_object()
        super().send_users_email(users)

    def send_email(self, email: Optional[str] = None):
        """Send just one email"""
        self.object = self.get_object()
        context = self.get_context_data()
        if email is None:
            email = self.get_email()
        self._send_email(context, [email])
