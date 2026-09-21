"""AWS RDS IAM database authentication.

RDS can authenticate a PostgreSQL connection with a short-lived IAM auth
token instead of a stored password, which removes the database password from
the deployment entirely. The token expires 15 minutes after it is minted, so
it cannot be read once into ``DATABASES`` at startup: PostgreSQL checks the
password only during the startup handshake, so connections already open
survive expiry, but every new one the pool opens — warmup, growth toward
``max_size``, ``max_lifetime`` recycling — would fail once it went stale.

django-vpg exists for exactly this. It keeps the credential current for the
pool and mints through ``OPTIONS["credential_provider"]``, off the request
path, refreshing ahead of the credential's own expiry. The minting itself is
the driver's ``django_vpg.contrib.aws.rds_iam_token``, whose docstring
documents the AWS prerequisites; what lives here is the *policy* — which
aliases use IAM, and what TLS we insist on before letting a live credential
onto the wire.

Enable with ``DATABASE_IAM_AUTH=true``; see ``glitchtip/settings.py``.

Not every alias can use it. GlitchTip's ``read_only`` alias points at a
read-only PgBouncer over the same database, and PgBouncer authenticates its
own clients while holding separate credentials upstream — it cannot validate
an IAM token. (A PgBouncer limitation, not a pooler one: RDS Proxy accepts
IAM auth from clients.) Use ``DATABASE_IAM_AUTH_ALIASES`` to limit IAM auth
to the aliases that reach an IAM-aware endpoint.

``GRANT rds_iam`` is one-way for a role, and it applies through role
*membership* too. Granting an IAM role to the master user — say, so it can
``CREATE DATABASE ... OWNER`` that role — makes the master inherit
``rds_iam`` and lose password authentication immediately, and resetting the
master password does not undo it; only revoking the membership does. Grant
nothing that carries ``rds_iam`` to a role that still needs a password.

boto3's default credential chain signs the token, and
``AWS_ACCESS_KEY_ID``/``AWS_SECRET_ACCESS_KEY`` in the environment outrank
the workload's own role. GlitchTip documents those same variables for
S3-compatible file storage, so supply storage credentials as
``AWS_ACCESS_KEY_ID_FILE``/``AWS_SECRET_ACCESS_KEY_FILE`` instead:
django-storages passes those to its own session explicitly, leaving the
chain free to resolve the pod's role.
"""

import warnings

from django.core.exceptions import ImproperlyConfigured

#: The driver's own RDS provider. It derives the region from the endpoint
#: hostname and reads the renewal deadline out of the token, so neither is
#: configuration here. boto3 is already a hard dependency of GlitchTip, so
#: django-vpg's ``[aws]`` extra adds nothing we do not already have.
RDS_IAM_PROVIDER = "django_vpg.contrib.aws.rds_iam_token"

#: Where to obtain the CA bundle RDS certificates chain to.
RDS_CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

#: sslmode values that authenticate the *server*, not merely encrypt.
#:
#: verify-ca is deliberately NOT here, and this is the reason the rest of the
#: module refers back to. It validates the certificate chain but skips the
#: hostname check, and the trust anchor RDS deployments use is the **global**
#: bundle — the anchor for every RDS instance in every AWS account. So
#: verify-ca + that bundle trusts any RDS instance anywhere: anyone who can
#: answer on the database hostname (split-horizon DNS, a reclaimed CNAME, a
#: compromised sidecar) can point it at an instance in their own account,
#: complete the handshake, and be handed a live IAM token as a cleartext
#: password — replayable against the real database for 15 minutes.
VERIFIED_SSLMODES = ("verify-full",)

#: Encrypted, but authenticating nothing: no certificate chain and no
#: hostname. Reachable only by deliberately setting
#: DATABASE_IAM_ALLOW_UNVERIFIED_TLS, never by following an error message.
UNVERIFIED_SSLMODES = ("require", "verify-ca")

#: Modes that validate a certificate chain, and so need a trust anchor.
#: Without one the driver falls back to the system roots — which, for the
#: verify-ca case described above, trusts the whole public WebPKI.
CHAIN_VERIFIED_SSLMODES = ("verify-ca", "verify-full")


def configure_options(
    options: dict,
    *,
    iam_auth: bool,
    sslmode: str | None = None,
    sslrootcert: str | None = None,
    allow_unverified_tls: bool = False,
) -> dict:
    """Apply TLS and IAM-auth settings to one alias's ``OPTIONS`` dict.

    Builds the result in a copy and merges it back only once every check has
    passed, so a caller that catches ``ImproperlyConfigured`` is never left
    with IAM half-wired onto a rejected sslmode.

    An entry already present (set explicitly in a ``DATABASES`` override)
    wins over the environment, except ``credential_provider``, which IAM auth
    owns. Kept here rather than inline in settings so the guards are reachable
    from tests without re-importing settings.
    """
    resolved = dict(options)
    if sslmode:
        resolved.setdefault("sslmode", sslmode)
    if sslrootcert:
        resolved.setdefault("sslrootcert", sslrootcert)

    if iam_auth:
        # An IAM token reaches PostgreSQL as a cleartext password, so the
        # connection must both be encrypted and prove who it is talking to.
        resolved.setdefault("sslmode", "verify-full")
        mode = resolved["sslmode"]
        if mode in UNVERIFIED_SSLMODES:
            if not allow_unverified_tls:
                why_not_verify_ca = (
                    "verify-ca validates the chain but not the hostname, and "
                    "the RDS global bundle trusts every RDS instance in every "
                    "AWS account. "
                    if mode == "verify-ca"
                    else ""
                )
                raise ImproperlyConfigured(
                    f"DATABASE_SSLMODE={mode!r} authenticates nothing about the "
                    "server, so a live IAM token would be handed to whatever "
                    "answers on the database hostname and could be replayed "
                    f"against the real database for 15 minutes. {why_not_verify_ca}"
                    "Use 'verify-full' with DATABASE_SSLROOTCERT set to the RDS "
                    f"CA bundle ({RDS_CA_BUNDLE_URL}). If you genuinely accept "
                    "an unauthenticated server, set "
                    "DATABASE_IAM_ALLOW_UNVERIFIED_TLS=true."
                )
        elif mode not in VERIFIED_SSLMODES:
            raise ImproperlyConfigured(
                f"DATABASE_SSLMODE={mode!r} cannot be combined with "
                "DATABASE_IAM_AUTH: an IAM token is sent as a cleartext "
                "password, and this mode permits an unencrypted session "
                "('prefer' negotiates TLS but accepts plaintext if the server "
                f"declines). Use {', '.join(VERIFIED_SSLMODES)}."
            )
        # Deliberately not an ``elif``: a mode reached through the opt-out
        # above still needs the anchor.
        if mode in CHAIN_VERIFIED_SSLMODES and not resolved.get("sslrootcert"):
            # RDS certificates do not chain to a CA in the system trust store,
            # so this would otherwise surface as an opaque TLS error.
            raise ImproperlyConfigured(
                "DATABASE_IAM_AUTH requires DATABASE_SSLROOTCERT to point at "
                "the Amazon RDS CA bundle, because RDS certificates are not "
                "signed by a CA in the system trust store. Download "
                f"{RDS_CA_BUNDLE_URL} and set DATABASE_SSLROOTCERT to its path."
            )
        if mode in UNVERIFIED_SSLMODES:
            # Leaves a trace on every boot: once the opt-out reaches a values
            # file it is otherwise invisible for the life of the deployment.
            warnings.warn(
                f"DATABASE_IAM_ALLOW_UNVERIFIED_TLS is set, so IAM tokens are "
                f"sent over sslmode={mode!r}, which does not verify the "
                "database server's identity.",
                stacklevel=2,
            )
        # One provider serves every alias it is enabled for: it is handed the
        # host, port and user of the connection being opened, and an RDS token
        # signs exactly those, so each alias gets a token for its own endpoint.
        resolved["credential_provider"] = RDS_IAM_PROVIDER

    options.update(resolved)
    return options
