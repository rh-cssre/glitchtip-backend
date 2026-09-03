"""Tests for GlitchTip's RDS IAM auth policy (``glitchtip.db_iam``).

Token minting lives in the driver (``django_vpg.contrib.aws``); what is
tested here is the policy around it — which aliases get IAM auth, and the
refusal to configure a live credential onto an unencrypted or unverified
connection.
"""

from unittest import skipUnless

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.utils.module_loading import import_string

from glitchtip import db_iam


class ConfigureOptionsTestCase(SimpleTestCase):
    def test_disabled_leaves_options_untouched(self):
        assert db_iam.configure_options({}, iam_auth=False, token_ttl=600) == {}

    def test_tls_settings_apply_without_iam_auth(self):
        options = db_iam.configure_options(
            {},
            iam_auth=False,
            token_ttl=600,
            sslmode="require",
            sslrootcert="/certs/rds.pem",
        )
        assert options == {"sslmode": "require", "sslrootcert": "/certs/rds.pem"}

    def test_enabled_wires_the_provider(self):
        options = db_iam.configure_options(
            {},
            iam_auth=True,
            token_ttl=300,
            sslrootcert="/certs/rds.pem",
        )
        assert options["password_provider"] == db_iam.RDS_IAM_PROVIDER
        assert options["password_ttl"] == 300
        # Verified TLS by default: without it a token would be handed to
        # whatever answers on that hostname.
        assert options["sslmode"] == "verify-full"

    def test_verifying_modes_without_a_ca_bundle_are_refused(self):
        # RDS certificates do not chain to a CA in the system trust store, so
        # this would otherwise fail later as an opaque TLS error. verify-ca is
        # included: without the bundle it falls back to system roots AND skips
        # the hostname check, so any publicly-trusted cert would be accepted.
        for sslmode in ("verify-full", "verify-ca"):
            with self.subTest(sslmode=sslmode):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    db_iam.configure_options(
                        {}, iam_auth=True, token_ttl=600, sslmode=sslmode
                    )
                assert "DATABASE_SSLROOTCERT" in str(ctx.exception)

    def test_the_ca_bundle_error_does_not_recommend_the_unverified_mode(self):
        # This error is on the default path, so it is the one message every
        # operator sees. It must lead with the fix, and must not present
        # 'require' as an equivalent alternative — it validates nothing.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            db_iam.configure_options({}, iam_auth=True, token_ttl=600)
        message = str(ctx.exception)
        assert db_iam.RDS_CA_BUNDLE_URL in message
        assert "NO certificate or hostname validation" in message

    def test_explicit_require_opts_out_of_verification(self):
        options = db_iam.configure_options(
            {}, iam_auth=True, token_ttl=600, sslmode="require"
        )
        assert options["sslmode"] == "require"

    def test_unknown_sslmode_is_refused(self):
        # Allow-list, not deny-list: a mode the driver gains later must not
        # slip through here.
        with self.assertRaises(ImproperlyConfigured):
            db_iam.configure_options(
                {}, iam_auth=True, token_ttl=600, sslmode="prefer-verify"
            )

    def test_explicit_options_win_over_environment(self):
        options = db_iam.configure_options(
            {"sslmode": "verify-ca", "sslrootcert": "/explicit.pem"},
            iam_auth=True,
            token_ttl=600,
            sslmode="require",
            sslrootcert="/from-env.pem",
        )
        assert options["sslmode"] == "verify-ca"
        assert options["sslrootcert"] == "/explicit.pem"

    def test_unencrypted_modes_are_refused(self):
        # The driver rejects these too, but only at connect time and as an
        # OperationalError. Failing at settings load names the bad setting.
        for sslmode in ("disable", "allow", "prefer"):
            with self.subTest(sslmode=sslmode):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    db_iam.configure_options(
                        {}, iam_auth=True, token_ttl=600, sslmode=sslmode
                    )
                assert "DATABASE_IAM_AUTH" in str(ctx.exception)

    def test_unencrypted_mode_is_allowed_when_iam_auth_is_off(self):
        options = db_iam.configure_options(
            {}, iam_auth=False, sslmode="prefer", token_ttl=600
        )
        assert options["sslmode"] == "prefer"


def _provider_available():
    try:
        import_string(db_iam.RDS_IAM_PROVIDER)
    except ImportError:
        return False
    return True


class ProviderWiringTestCase(SimpleTestCase):
    @skipUnless(
        _provider_available(),
        f"{db_iam.RDS_IAM_PROVIDER} needs a django-vpg with rotating-credential "
        "support; this skip disappears once glitchtip-rust pins that release. "
        "Until then, DATABASE_IAM_AUTH cannot work at runtime.",
    )
    def test_the_configured_provider_is_importable_and_callable(self):
        # The dotted path is resolved by the driver at connect time, so a typo
        # would otherwise surface only on a deployment with IAM enabled.
        assert callable(import_string(db_iam.RDS_IAM_PROVIDER))
