from django.conf import settings
from organizations.backends.tokens import RegistrationTokenGenerator
from organizations.backends.defaults import InvitationBackend as BaseInvitationBackend
from .models import Organization


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

    def get_token(self, org_user, **kwargs):
        return InvitationTokenGenerator().make_token(org_user)

    def send_invitation(self, user, sender=None, **kwargs):
        kwargs["domain"] = {
            "domain": settings.GLITCHTIP_DOMAIN.geturl(),
        }
        return super().send_invitation(user, sender, **kwargs)
