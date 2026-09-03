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

django-vpg's ``OPTIONS["password_provider"]`` exists for exactly this: the
driver calls back here whenever it needs a credential for a *physical*
connection. This module is the AWS-specific half — the driver itself knows
nothing about AWS, which is why the token minting lives in the application.

Enable with ``DATABASE_IAM_AUTH=true``; see ``glitchtip/settings.py``.

IAM auth is between the client and *RDS*. An alias that connects through a
connection pooler instead — GlitchTip's ``read_only`` alias points at a
read-only PgBouncer over the same database — authenticates against the
pooler, which cannot validate an IAM token; PgBouncer holds its own
credentials for the upstream. Use ``DATABASE_IAM_AUTH_ALIASES`` to limit IAM
auth to the aliases that really do connect straight to RDS.

Outside GlitchTip, RDS IAM auth also requires:

* an IAM policy granting ``rds-db:connect`` on
  ``arn:aws:rds-db:<region>:<account>:dbuser:<resource-id>/<db-user>``;
* the database role created with ``GRANT rds_iam TO <db-user>``;
* TLS, which the driver enforces — a token travels as a cleartext password.
"""

import threading

from django.core.exceptions import ImproperlyConfigured

#: Where to obtain the CA bundle RDS certificates chain to.
RDS_CA_BUNDLE_URL = "https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem"

#: sslmode values that actually authenticate the server. Anything outside
#: these plus UNVERIFIED_SSLMODE permits a plaintext session, which would put
#: a live credential on the wire in the clear.
VERIFIED_SSLMODES = ("verify-full", "verify-ca")
#: Encrypted but unauthenticated — see the warning in configure_options.
UNVERIFIED_SSLMODE = "require"

_client = None
_client_lock = threading.Lock()


def _rds_client():
    """Return a process-local boto3 RDS client, built on first use.

    Deliberately lazy rather than built at import: settings are imported in
    the parent of a prefork server, and a boto3 client created before
    ``fork()`` carries connection-pool state its children would then share.
    First use happens in the worker, after the fork.
    """
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                import boto3
                from django.conf import settings

                _client = boto3.client(
                    "rds", region_name=getattr(settings, "DATABASE_IAM_REGION", None)
                )
    return _client


def rds_auth_token(conn) -> str:
    """Mint an RDS IAM auth token for the connection being opened.

    ``conn`` carries the endpoint django-vpg is about to connect to
    (``host``, ``port``, ``user``, ``dbname``). The token signs host, port
    and user, so it is only valid for that endpoint — which is why this
    reads the connection rather than ``settings.DATABASES``: a writer and a
    read replica are different hosts and need different tokens, and one
    provider serves every alias.

    Called from whichever thread first notices the credential has aged past
    ``password_ttl``; the boto3 client is shared and its calls are
    thread-safe once constructed.
    """
    return _rds_client().generate_db_auth_token(
        DBHostname=conn["host"],
        Port=int(conn["port"]),
        DBUsername=conn["user"],
    )


def configure_options(
    options,
    *,
    iam_auth,
    token_ttl,
    sslmode=None,
    sslrootcert=None,
):
    """Apply TLS and IAM-auth settings to one alias's ``OPTIONS`` dict.

    Mutates and returns ``options``. An entry already present (set
    explicitly in a ``DATABASES`` override) wins over the environment,
    except ``password_provider``/``password_ttl``, which IAM auth owns.

    Kept here rather than inline in settings so the guard below is
    reachable from tests without re-importing settings.
    """
    if sslmode:
        options.setdefault("sslmode", sslmode)
    if sslrootcert:
        options.setdefault("sslrootcert", sslrootcert)

    if not iam_auth:
        return options

    # One provider serves every alias it is enabled for: it is handed the
    # host, port and user of the connection being opened, and an RDS token
    # signs exactly those, so each alias gets a token for its own endpoint.
    options["password_provider"] = "glitchtip.db_iam.rds_auth_token"
    options["password_ttl"] = token_ttl
    # An IAM token is sent as a cleartext password, so the connection must be
    # encrypted; the driver refuses the modes that permit a plaintext
    # session. Verify the server too unless the operator deliberately chose
    # otherwise — without verification a token would be handed to whatever
    # answers on that hostname.
    options.setdefault("sslmode", "verify-full")
    # Allow-list, mirroring the driver's own check, rather than a deny-list of
    # the three unencrypted modes: a mode name added to the driver later would
    # otherwise slip through here silently.
    if options["sslmode"] not in VERIFIED_SSLMODES + (UNVERIFIED_SSLMODE,):
        raise ImproperlyConfigured(
            f"DATABASE_SSLMODE={options['sslmode']!r} cannot be combined with "
            "DATABASE_IAM_AUTH: an IAM token is sent to PostgreSQL as a "
            "cleartext password, and this mode permits an unencrypted session "
            "('prefer' negotiates TLS but accepts plaintext if the server "
            f"declines). Use one of {', '.join(VERIFIED_SSLMODES)}."
        )
    # Both verifying modes need the bundle: RDS certificates do not chain to a
    # CA in the system trust store, and 'verify-ca' would otherwise fall back
    # to system roots *and* skip the hostname check — any publicly-trusted
    # certificate, for any name, would be accepted.
    if options["sslmode"] in VERIFIED_SSLMODES and not options.get("sslrootcert"):
        raise ImproperlyConfigured(
            "DATABASE_IAM_AUTH requires DATABASE_SSLROOTCERT to point at the "
            "Amazon RDS CA bundle, because RDS certificates are not signed by "
            f"a CA in the system trust store. Download {RDS_CA_BUNDLE_URL} and "
            "set DATABASE_SSLROOTCERT to its path.\n\n"
            f"DATABASE_SSLMODE={UNVERIFIED_SSLMODE!r} will also start, but it "
            "performs NO certificate or hostname validation: anything that can "
            "answer on the database hostname receives a live IAM token, "
            "replayable against RDS for 15 minutes as that role. Set the CA "
            "bundle instead unless you have a specific reason not to."
        )
    return options
