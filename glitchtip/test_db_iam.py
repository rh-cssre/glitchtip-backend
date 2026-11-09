"""Tests for GlitchTip's RDS IAM auth policy (``glitchtip.db_iam``).

Token minting lives in the driver (``django_vpg.contrib.aws``) and is tested
there; what is tested here is the policy around it — which aliases get IAM
auth, and the refusal to configure a live credential onto an unencrypted or
unverified connection.
"""

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from glitchtip import db_iam


class ConfigureOptionsTestCase(SimpleTestCase):
    def test_without_iam_auth_only_tls_settings_are_applied(self):
        assert db_iam.configure_options({}, iam_auth=False) == {}
        # Including modes IAM auth would refuse: the TLS guards exist to
        # protect a live credential, and there is none here.
        options = db_iam.configure_options(
            {}, iam_auth=False, sslmode="prefer", sslrootcert="/certs/rds.pem"
        )
        assert options == {"sslmode": "prefer", "sslrootcert": "/certs/rds.pem"}

    def test_enabled_wires_the_provider(self):
        options = db_iam.configure_options(
            {}, iam_auth=True, sslrootcert="/certs/rds.pem"
        )
        assert options["credential_provider"] == db_iam.RDS_IAM_PROVIDER
        # Verified TLS by default: without it a token would be handed to
        # whatever answers on that hostname.
        assert options["sslmode"] == "verify-full"

    def test_verify_full_without_a_ca_bundle_is_refused(self):
        # RDS certificates do not chain to a CA in the system trust store, so
        # this would otherwise fail later as an opaque TLS error. It is also
        # the one message every operator sees, being on the default path, so
        # it must not name a mode that validates nothing — under deploy
        # pressure that is what gets copied.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            db_iam.configure_options({}, iam_auth=True)
        message = str(ctx.exception)
        assert "DATABASE_SSLROOTCERT" in message
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
                with self.assertWarns(UserWarning):
                    options = db_iam.configure_options(
                        {},
                        iam_auth=True,
                        sslmode=mode,
                        sslrootcert="/certs/rds.pem",
                        allow_unverified_tls=True,
                    )
                assert options["sslmode"] == mode
                # The opt-out waives server verification, not IAM auth itself:
                # an early return here would silently revert that deployment
                # to the password the feature exists to remove.
                assert options["credential_provider"] == db_iam.RDS_IAM_PROVIDER

    def test_the_opt_out_does_not_also_waive_the_ca_bundle(self):
        # verify-ca validates a chain, so without an anchor the driver falls
        # back to the system roots — and it does not check the hostname, so
        # any publicly trusted certificate for any name would be handed the
        # token. The opt-out is about hostname verification, not about
        # trusting the whole public WebPKI.
        with self.assertRaises(ImproperlyConfigured) as ctx:
            db_iam.configure_options(
                {}, iam_auth=True, sslmode="verify-ca", allow_unverified_tls=True
            )
        assert "DATABASE_SSLROOTCERT" in str(ctx.exception)

    def test_require_needs_no_ca_bundle(self):
        # require encrypts without validating any chain, so unlike verify-ca
        # there is no anchor for it to need.
        with self.assertWarns(UserWarning):
            options = db_iam.configure_options(
                {}, iam_auth=True, sslmode="require", allow_unverified_tls=True
            )
        assert options["sslmode"] == "require"

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
        # left with IAM half-wired onto a rejected sslmode. The inputs matter:
        # with sslmode already set, every pre-raise write is a setdefault
        # no-op, so an implementation with no copy at all would still pass.
        # Leaving it unset makes the writes visible if they escape.
        options = {}
        with self.assertRaises(ImproperlyConfigured):
            db_iam.configure_options(
                options, iam_auth=True, sslmode="prefer", sslrootcert="/certs/rds.pem"
            )
        assert options == {}

        options = {"sslmode": "prefer"}
        with self.assertRaises(ImproperlyConfigured):
            db_iam.configure_options(options, iam_auth=True)
        assert options == {"sslmode": "prefer"}

    def test_no_renewal_interval_is_configured(self):
        # The provider reports the token's own expiry and the driver renews
        # against that, so setting an interval would have had no observable
        # effect. Asserted so it is not reintroduced as dead configuration.
        assert "credential_ttl" not in db_iam.configure_options(
            {}, iam_auth=True, sslrootcert="/certs/rds.pem"
        )
