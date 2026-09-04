"""AWS RDS IAM database authentication.

RDS can authenticate a PostgreSQL connection with a short-lived IAM auth
token instead of a stored password, which removes the database password
from the deployment entirely. The token is a SigV4-presigned string that
**expires 15 minutes** after it is minted, so it cannot be read once into
``DATABASES`` at startup: PostgreSQL only checks the password during the
startup handshake, so connections already open survive expiry, but the
driver's pool opens new ones on its own schedule — warmup, growth toward
``max_size``, ``max_lifetime`` recycling — and those fail to authenticate
once the token is stale.

django-vpg exists for exactly this: it keeps a credential current for the
pool and mints through ``OPTIONS["credential_provider"]``. The minting itself
is the driver's ``django_vpg.contrib.aws`` provider; what lives here is the
*policy* — which aliases use IAM, and what TLS we insist on before letting a
live credential onto the wire.

Enable with ``DATABASE_IAM_AUTH=true``; see ``glitchtip/settings.py``.

Not every alias can use it. GlitchTip's ``read_only`` alias points at a
read-only PgBouncer over the same database, and PgBouncer authenticates its
own clients while holding separate credentials upstream — it cannot validate
an IAM token. Use ``DATABASE_IAM_AUTH_ALIASES`` to limit IAM auth to the
aliases that reach an IAM-aware endpoint.

This is a PgBouncer limitation, not a pooler one: *RDS Proxy* accepts IAM
auth from clients and works fine. A token is signed for a specific host, port
and user, so it has to be minted for the endpoint actually being connected
to — which happens automatically, because the driver hands the provider the
connection it is opening rather than a configured hostname.

Outside GlitchTip, RDS IAM auth also requires:

* an IAM policy granting ``rds-db:connect`` on
  ``arn:aws:rds-db:<region>:<account>:dbuser:<resource-id>/<db-user>``;
* the database role created with ``GRANT rds_iam TO <db-user>``;
* TLS, which the driver enforces — a token travels as a cleartext password.
"""

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
#: verify-ca is deliberately NOT here. It validates the certificate chain but
#: skips the hostname check, and the trust anchor RDS deployments use is the
#: **global** bundle — the anchor for every RDS instance in every AWS account.
#: So verify-ca + that bundle trusts any RDS instance anywhere: anyone who can
#: answer on the database hostname (split-horizon DNS, a reclaimed CNAME, a
#: compromised sidecar) can point it at an instance in their own account,
#: complete the handshake, and be handed a live IAM token as a cleartext
#: password — replayable against the real database for 15 minutes.
VERIFIED_SSLMODES = ("verify-full",)

#: Encrypted, but authenticating nothing: no certificate chain and no
#: hostname. Reachable only by deliberately setting
#: DATABASE_IAM_ALLOW_UNVERIFIED_TLS, never by following an error message.
UNVERIFIED_SSLMODES = ("require", "verify-ca")


def configure_options(
    options,
    *,
    iam_auth,
    token_ttl=None,
    sslmode=None,
    sslrootcert=None,
    allow_unverified_tls=False,
):
    """Apply TLS and IAM-auth settings to one alias's ``OPTIONS`` dict.

    Mutates and returns ``options``, but only after every check has passed —
    a caller that catches ``ImproperlyConfigured`` is never left with IAM
    half-wired onto a rejected sslmode.

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
                raise ImproperlyConfigured(
                    f"DATABASE_SSLMODE={mode!r} authenticates nothing about the "
                    "server, so a live IAM token would be handed to whatever "
                    "answers on the database hostname and could be replayed "
                    "against the real database for 15 minutes. "
                    f"{'verify-ca validates the chain but not the hostname, and the RDS global bundle trusts every RDS instance in every AWS account. ' if mode == 'verify-ca' else ''}"
                    f"Use 'verify-full' with DATABASE_SSLROOTCERT set to the RDS "
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
        elif not resolved.get("sslrootcert"):
            # RDS certificates do not chain to a CA in the system trust store,
            # so verify-full without the bundle fails as an opaque TLS error.
            raise ImproperlyConfigured(
                "DATABASE_IAM_AUTH requires DATABASE_SSLROOTCERT to point at "
                "the Amazon RDS CA bundle, because RDS certificates are not "
                "signed by a CA in the system trust store. Download "
                f"{RDS_CA_BUNDLE_URL} and set DATABASE_SSLROOTCERT to its path."
            )
        # One provider serves every alias it is enabled for: it is handed the
        # host, port and user of the connection being opened, and an RDS token
        # signs exactly those, so each alias gets a token for its own endpoint.
        resolved["credential_provider"] = RDS_IAM_PROVIDER
        if token_ttl:
            # Only a fallback: the RDS provider states the token's own expiry,
            # which the driver prefers, so this is unused with it.
            resolved["credential_ttl"] = token_ttl

    options.clear()
    options.update(resolved)
    return options
