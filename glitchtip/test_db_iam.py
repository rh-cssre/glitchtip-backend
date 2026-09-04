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
        assert db_iam.configure_options({}, iam_auth=False) == {}

    def test_tls_settings_apply_without_iam_auth(self):
        options = db_iam.configure_options(
            {}, iam_auth=False, sslmode="require", sslrootcert="/certs/rds.pem"
        )
        assert options == {"sslmode": "require", "sslrootcert": "/certs/rds.pem"}

    def test_enabled_wires_the_provider(self):
        options = db_iam.configure_options(
            {}, iam_auth=True, sslrootcert="/certs/rds.pem"
        )
        assert options["password_provider"] == db_iam.RDS_IAM_PROVIDER
        # Verified TLS by default: without it a token would be handed to
        # whatever answers on that hostname.
        assert options["sslmode"] == "verify-full"

    def test_verify_full_without_a_ca_bundle_is_refused(self):
        # RDS certificates do not chain to a CA in the system trust store, so
        # this would otherwise fail later as an opaque TLS error.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            db_iam.configure_options({}, iam_auth=True)
        assert "DATABASE_SSLROOTCERT" in str(ctx.exception)

    def test_the_error_does_not_offer_an_unverified_mode(self):
        # This error is on the default path, so it is the one message every
        # operator sees. It must not name a mode that validates nothing —
        # under deploy pressure that is what gets copied.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            db_iam.configure_options({}, iam_auth=True)
        message = str(ctx.exception)
        assert db_iam.RDS_CA_BUNDLE_URL in message
        for mode in db_iam.UNVERIFIED_SSLMODES:
            # Match the mode as a named value, not as a bare substring: the
            # message legitimately contains the word "requires".
            for spelling in (f"'{mode}'", f'"{mode}"', f"DATABASE_SSLMODE={mode}"):
                assert spelling not in message, (
                    f"the error offers {mode!r} as a way out"
                )

    def test_verify_ca_is_not_treated_as_verified(self):
        # verify-ca checks the chain but NOT the hostname, and the RDS global
        # bundle is the trust anchor for every RDS instance in every AWS
        # account — so it would accept an attacker's own RDS instance and hand
        # it a live, replayable token.
        assert "verify-ca" not in db_iam.VERIFIED_SSLMODES
        with self.assertRaises(ImproperlyConfigured) as ctx:
            db_iam.configure_options(
                {}, iam_auth=True, sslmode="verify-ca", sslrootcert="/certs/rds.pem"
            )
        assert "authenticates nothing" in str(ctx.exception)

    def test_unverified_tls_requires_an_explicit_opt_in(self):
        for mode in db_iam.UNVERIFIED_SSLMODES:
            with self.subTest(mode=mode):
                with self.assertRaises(ImproperlyConfigured):
                    db_iam.configure_options({}, iam_auth=True, sslmode=mode)
                options = db_iam.configure_options(
                    {}, iam_auth=True, sslmode=mode, allow_unverified_tls=True
                )
                assert options["sslmode"] == mode

    def test_unencrypted_modes_are_refused(self):
        # The driver rejects these too, but only at connect time and as an
        # OperationalError. Failing at settings load names the bad setting.
        for sslmode in ("disable", "allow", "prefer"):
            with self.subTest(sslmode=sslmode):
                with self.assertRaises(ImproperlyConfigured) as ctx:
                    db_iam.configure_options({}, iam_auth=True, sslmode=sslmode)
                assert "DATABASE_IAM_AUTH" in str(ctx.exception)

    def test_unencrypted_modes_stay_refused_even_with_the_opt_out(self):
        # The opt-out is about verifying the server's identity, not about
        # giving up encryption entirely.
        with self.assertRaises(ImproperlyConfigured):
            db_iam.configure_options(
                {}, iam_auth=True, sslmode="disable", allow_unverified_tls=True
            )

    def test_unencrypted_mode_is_allowed_when_iam_auth_is_off(self):
        options = db_iam.configure_options({}, iam_auth=False, sslmode="prefer")
        assert options["sslmode"] == "prefer"

    def test_explicit_options_win_over_environment(self):
        options = db_iam.configure_options(
            {"sslmode": "verify-full", "sslrootcert": "/explicit.pem"},
            iam_auth=True,
            sslmode="require",
            sslrootcert="/from-env.pem",
        )
        assert options["sslmode"] == "verify-full"
        assert options["sslrootcert"] == "/explicit.pem"

    def test_nothing_is_written_when_a_guard_rejects(self):
        # Validated before mutating, so a caller that catches the error is not
        # left with IAM half-wired onto a rejected sslmode.
        options = {"sslmode": "prefer"}
        with self.assertRaises(ImproperlyConfigured):
            db_iam.configure_options(options, iam_auth=True)
        assert options == {"sslmode": "prefer"}

    def test_token_ttl_is_only_a_fallback(self):
        # The RDS provider states the token's own expiry, which the driver
        # prefers, so this is absent unless explicitly configured.
        assert "password_ttl" not in db_iam.configure_options(
            {}, iam_auth=True, sslrootcert="/certs/rds.pem"
        )
        assert (
            db_iam.configure_options(
                {}, iam_auth=True, token_ttl=300, sslrootcert="/certs/rds.pem"
            )["password_ttl"]
            == 300
        )


def _provider_available():
    """Can the *loaded extension* actually honour password_provider?

    Checking that ``django_vpg.contrib.aws`` imports is not enough: gt_rust
    substitutes its own compiled module for ``django_vpg._vpg_driver``, and
    that substituted extension is what decides at runtime. The Python layer
    can be new while the extension is old.
    """
    try:
        import inspect

        import_string(db_iam.RDS_IAM_PROVIDER)
        from django_vpg._vpg_driver import RustPgDriver

        return "password_provider" in inspect.signature(RustPgDriver.connect).parameters
    except Exception:
        return False


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
