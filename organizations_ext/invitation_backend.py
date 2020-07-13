from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.urls import re_path
from organizations.backends.tokens import RegistrationTokenGenerator
from organizations.backends.defaults import InvitationBackend as BaseInvitationBackend
from .models import Organization
from .email import send_invitation_email


class InvitationTokenGenerator(RegistrationTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return str(user.pk) + str(timestamp)


class InvitationBackend(BaseInvitationBackend):
    """
    Based on django-organizations InvitationBackend but for org user instead of user
    """

    def __init__(self, org_model=None):
        self.user_model = None
        self.org_model = Organization

    def get_urls(self):
        return [
            re_path(
                r"^(?P<user_id>[\d]+)/(?P<token>[0-9A-Za-z]{1,13}-[0-9A-Za-z]{1,20})/$",
                view=self.activate_view,
                name="invitations_register",
            ),
        ]

    def get_token(self, org_user, **kwargs):
        return InvitationTokenGenerator().make_token(org_user)

    def send_invitation(self, user, sender=None, **kwargs):
        kwargs.update({
            "token": self.get_token(user),
            "organization": user.organization,
        })
        send_invitation_email(self, user, **kwargs)
        return True
