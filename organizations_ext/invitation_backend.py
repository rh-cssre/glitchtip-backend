from django.conf import settings
from django.contrib.auth.tokens import PasswordResetTokenGenerator
from django.urls import re_path
from django.utils.http import base36_to_int
from django.utils.crypto import constant_time_compare
from organizations.backends.defaults import InvitationBackend as BaseInvitationBackend
from .models import Organization
from .tasks import send_email_invite


REGISTRATION_TIMEOUT_DAYS = getattr(settings, "REGISTRATION_TIMEOUT_DAYS", 15)


class InvitationTokenGenerator(PasswordResetTokenGenerator):
    def _make_hash_value(self, user, timestamp):
        return str(user.pk) + str(timestamp)

    def check_token(self, user, token):
        """
        Check that a password reset token is correct for a given user.
        """
        # Parse the token
        try:
            ts_b36, hash = token.split("-")
        except ValueError:
            return False

        try:
            ts = base36_to_int(ts_b36)
        except ValueError:
            return False

        # Check that the timestamp/uid has not been tampered with
        if not constant_time_compare(self._make_token_with_timestamp(user, ts), token):
            return False

        # Check the timestamp is within limit
        if (self._num_seconds(self._now()) - ts) > REGISTRATION_TIMEOUT_DAYS * 86400:
            return False

        return True


class InvitationBackend(BaseInvitationBackend):
    """
    Based on django-organizations InvitationBackend but for org user instead of user
    """

    def __init__(self, org_model=None, namespace=None):
        self.user_model = None
        self.org_model = Organization
        self.namespace = namespace

    def get_urls(self):
        return [
            re_path(
                r"^(?P<user_id>[\d]+)/(?P<token>[0-9A-Za-z]{1,90}-[0-9A-Za-z]{1,90})/$",
                view=self.activate_view,
                name="invitations_register",
            ),
        ]

    def get_token(self, org_user, **kwargs):
        return InvitationTokenGenerator().make_token(org_user)

    def send_invitation(self, user, sender=None, **kwargs):
        kwargs.update(
            {"token": self.get_token(user), "organization": user.organization,}
        )
        send_email_invite.delay(user.pk, kwargs["token"])
        return True
