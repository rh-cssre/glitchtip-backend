"""Tests for AWS RDS IAM database authentication (``glitchtip.db_iam``).

No AWS: the boto3 client is stubbed. What matters here is that the token is
minted for the connection actually being opened, and that a token can never
be configured onto an unencrypted or unverified connection.
"""

from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from glitchtip import db_iam


class RdsAuthTokenTestCase(SimpleTestCase):
    def setUp(self):
        # The module caches its client process-wide; keep tests independent.
        db_iam._client = None
        self.addCleanup(setattr, db_iam, "_client", None)

    def test_token_is_minted_for_the_connection_being_opened(self):
        # An RDS token signs host, port and user, so it is only valid for
        # that endpoint. Each alias has its own, which is why the driver
        # passes the connection rather than the provider reading settings.
        client = mock.Mock()
        client.generate_db_auth_token.return_value = "token-value"
        with mock.patch.object(db_iam, "_rds_client", return_value=client):
            token = db_iam.rds_auth_token(
                {
                    "host": "db.abc123.us-east-1.rds.amazonaws.com",
                    "port": 5432,
                    "user": "glitchtip_iam",
                    "dbname": "glitchtip",
                }
            )
        assert token == "token-value"
        client.generate_db_auth_token.assert_called_once_with(
            DBHostname="db.abc123.us-east-1.rds.amazonaws.com",
            Port=5432,
            DBUsername="glitchtip_iam",
        )

    def test_port_given_as_a_string_is_coerced(self):
        # DATABASE_URL parsing can leave PORT as a str.
        client = mock.Mock()
        with mock.patch.object(db_iam, "_rds_client", return_value=client):
            db_iam.rds_auth_token(
                {"host": "h", "port": "5432", "user": "u", "dbname": "d"}
            )
        assert client.generate_db_auth_token.call_args.kwargs["Port"] == 5432

    def test_client_is_built_once_and_reused(self):
        with mock.patch("boto3.client") as boto_client:
            first = db_iam._rds_client()
            second = db_iam._rds_client()
        assert first is second
        boto_client.assert_called_once()


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
        assert options["password_provider"] == "glitchtip.db_iam.rds_auth_token"
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
