"""Behavior of "email disabled" mode (settings.EMAIL_ENABLED = False).

When no mail transport is configured GlitchTip must run with zero email sends,
turn off account email verification, surface invite links instead of emailing
them, and report the disabled state on /api/settings/. These tests pin that
behavior; the configured-email path stays covered by the existing per-app mail
tests (which run with EMAIL_ENABLED defaulting True under TESTING).
"""

from allauth.account.models import EmailAddress
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from model_bakery import baker

from apps.organizations_ext.constants import OrganizationUserRole
from glitchtip.email import GlitchTipEmail
from glitchtip.test_utils import generators  # noqa: F401  (registers baker gens)

SETTINGS_URL = "/api/settings/"
SIGNUP_URL = "/_allauth/browser/v1/auth/signup"
RESET_URL = "/_allauth/browser/v1/auth/password/request"


class SettingsEmailFlagTestCase(TestCase):
    def test_email_flag_present_when_enabled(self):
        with override_settings(EMAIL_ENABLED=True):
            res = self.client.get(SETTINGS_URL)
        self.assertIn("email", res.json()["enabledFeatures"])

    def test_email_flag_absent_when_disabled(self):
        with override_settings(EMAIL_ENABLED=False):
            res = self.client.get(SETTINGS_URL)
        self.assertNotIn("email", res.json()["enabledFeatures"])


class EmailChokepointTestCase(TestCase):
    """All GlitchTip-authored mail (invite, throttle, alert, uptime) funnels
    through GlitchTipEmail._send_email, so guarding it once disables them all."""

    @override_settings(EMAIL_ENABLED=False)
    def test_send_email_is_a_real_skip_not_a_swallowed_error(self):
        # Returns before rendering/sending -- no transport is touched.
        GlitchTipEmail()._send_email({}, ["someone@example.com"])
        self.assertEqual(len(mail.outbox), 0)


class InviteLinkTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = baker.make("users.user")
        cls.organization = baker.make("organizations_ext.Organization", name="Test Org")
        cls.org_user = cls.organization.add_user(cls.user)
        cls.members_url = reverse(
            "api:list_organization_members", args=[cls.organization.slug]
        )

    def setUp(self):
        self.client.force_login(self.user)

    def _invite(self, email="new@example.com"):
        data = {
            "email": email,
            "orgRole": OrganizationUserRole.MEMBER.label.lower(),
            "teamRoles": [],
        }
        return self.client.post(self.members_url, data, content_type="application/json")

    def test_invite_response_exposes_usable_link(self):
        res = self._invite()
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertTrue(body["pending"])
        invite_link = body["inviteLink"]
        self.assertIn("/accept/", invite_link)

        # The link carries a token the accept endpoint actually honors.
        org_user_id, token = invite_link.rstrip("/").split("/")[-2:]
        self.client.logout()
        accept_url = reverse("api:get_accept_invite", args=[org_user_id, token])
        accept_res = self.client.get(accept_url)
        self.assertContains(accept_res, self.organization.name)

    @override_settings(EMAIL_ENABLED=False)
    def test_invite_skips_send_but_still_returns_link(self):
        res = self._invite()
        self.assertEqual(res.status_code, 201)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn("/accept/", res.json()["inviteLink"])


@override_settings(EMAIL_ENABLED=False, ACCOUNT_EMAIL_VERIFICATION="none")
class FirstRunDisabledEmailTestCase(TestCase):
    def test_first_user_signup_sends_no_mail_and_stays_unverified(self):
        res = self.client.post(
            SIGNUP_URL,
            {"email": "first@example.com", "password": "hunter222"},
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(mail.outbox), 0)
        # Email is recorded but never trusted as verified.
        email_address = EmailAddress.objects.get(email="first@example.com")
        self.assertFalse(email_address.verified)

    def test_password_reset_is_sane_and_silent(self):
        user = baker.make("users.user")
        res = self.client.post(
            RESET_URL, {"email": user.email}, content_type="application/json"
        )
        # No 500 from a failed send, and nothing actually goes out.
        self.assertNotEqual(res.status_code, 500)
        self.assertEqual(len(mail.outbox), 0)
