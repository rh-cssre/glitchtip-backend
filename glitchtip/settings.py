"""
Django settings for GlitchTip project.

For more information on this file, see
https://docs.djangoproject.com/en/dev/topics/settings/

For the full list of settings and their values, see
https://docs.djangoproject.com/en/dev/ref/settings/
"""

import logging
import os
import sys
import warnings
from datetime import timedelta

import environ
from corsheaders.defaults import default_headers
from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP
from django_vtasks.scheduler import crontab

env = environ.FileAwareEnv(
    ALLOWED_HOSTS=(list, ["*"]),
    DEFAULT_FILE_STORAGE=(str, global_settings.STORAGES["default"]["BACKEND"]),
    AWS_ACCESS_KEY_ID=(str, None),
    AWS_SECRET_ACCESS_KEY=(str, None),
    AWS_STORAGE_BUCKET_NAME=(str, None),
    AWS_S3_ENDPOINT_URL=(str, None),
    AWS_LOCATION=(str, ""),
    AWS_S3_USE_SSL=(bool, True),
    AWS_S3_VERIFY=(str, None),
    AZURE_ACCOUNT_NAME=(str, None),
    AZURE_ACCOUNT_KEY=(str, None),
    AZURE_CONTAINER=(str, None),
    AZURE_URL_EXPIRATION_SECS=(int, None),
    IS_LOAD_TEST=(bool, False),
    GS_BUCKET_NAME=(str, None),
    GS_PROJECT_ID=(str, None),
    DEBUG=(bool, False),
    DEBUG_TOOLBAR=(bool, False),
    STATIC_URL=(str, "/"),
    ENABLE_OBSERVABILITY_API=(bool, False),
    READ_ONLY_DATABASE_URL=(str, None),
    MAINTENANCE_DATABASE_URL=(str, None),
    GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL=(bool, False),
)
path = environ.Path()

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# Quick-start development settings - unsuitable for production
# See https://docs.djangoproject.com/en/dev/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env.str("SECRET_KEY", "change_me")

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env("DEBUG")

# Enable only for running end to end testing. Debug must be True to use.
ENABLE_TEST_API = env.bool("ENABLE_TEST_API", False)
if DEBUG is False:
    ENABLE_TEST_API = False
if DEBUG and ENABLE_TEST_API:
    ACCOUNT_RATE_LIMITS = False  # Disable for e2e tests

ALLOWED_HOSTS = env("ALLOWED_HOSTS")
# Necessary for kubernetes health checks
POD_IP = env.str("POD_IP", default=None)
if POD_IP:
    ALLOWED_HOSTS.append(POD_IP)


ENVIRONMENT = env.str("ENVIRONMENT", None)
GLITCHTIP_VERSION = env.str("GLITCHTIP_VERSION", "0.0.0-unknown")
# Multiline, markdown accepted. Example: "[Burke Software's](https://burkesoftware.com) GlitchTip Server"
GLITCHTIP_INSTANCE_NAME: str | None = None
if "GLITCHTIP_INSTANCE_NAME" in os.environ:
    GLITCHTIP_INSTANCE_NAME = env.str("GLITCHTIP_INSTANCE_NAME", None, multiline=True)

# Used in email and DSN generation. Set to full domain such as https://glitchtip.example.com
default_url = env.str(
    "APP_URL", env.str("GLITCHTIP_DOMAIN", "http://localhost:8000")
)  # DigitalOcean App Platform uses APP_URL
GLITCHTIP_URL = env.url("GLITCHTIP_URL", default_url)
if GLITCHTIP_URL.scheme not in ["http", "https"]:
    raise ImproperlyConfigured("GLITCHTIP_DOMAIN must start with http or https")

# If True, use a relative URL in chunk upload responses.
GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL = env.bool(
    "GLITCHTIP_CHUNK_UPLOAD_USE_RELATIVE_URL", False
)

# Is running unit test
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

# Warn (but don't refuse to boot) about unsafe defaults when running outside
# development. A forced failure here would break existing self-hosters on
# upgrade, so escalation to a hard check is deferred to a major release.
# Operators who miss these in logs end up with forgeable signed cookies /
# password-reset tokens and host-header attacks respectively.
if not DEBUG and not TESTING:
    if SECRET_KEY == "change_me":
        warnings.warn(
            "SECRET_KEY is still the placeholder default 'change_me'. "
            "Set SECRET_KEY to a unique random secret before running in "
            "production. Generate one with: "
            "python -c 'import secrets; print(secrets.token_urlsafe(50))'",
            RuntimeWarning,
            stacklevel=2,
        )
    if ALLOWED_HOSTS == ["*"]:
        warnings.warn(
            "ALLOWED_HOSTS is the wildcard default. Restrict to known "
            "hostnames via the ALLOWED_HOSTS env var (comma-separated) in "
            "production deployments.",
            RuntimeWarning,
            stacklevel=2,
        )

# Limits size (in bytes) of uncompressed event payloads. Mitigates DOS risk.
# Enforced at decompression time inside gt_rust (the ingest decompression
# primitive) and is the source of truth for ingest body size.
# DATA_UPLOAD_MAX_MEMORY_SIZE below sits just above this to give Django's own
# check a matching ceiling.
GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE = env.int(
    "GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE",
    5 * 1024 * 1024,  # 5 MB
)

# Mean seconds between in-process gc.collect() + malloc_trim() passes that
# return freed heap pages to the OS (see glitchtip/memory_trim.py). 0 disables.
GLITCHTIP_MALLOC_TRIM_INTERVAL = env.int("GLITCHTIP_MALLOC_TRIM_INTERVAL", 3600)

# Raw request body cap before view handling. For ingest endpoints gt_rust
# enforces GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE on the decompressed bytes, but
# the *compressed* body Django reads is smaller than that, so this only needs
# to cover uncompressed ingest bodies. Multipart file uploads (minidumps,
# source-map chunks) go through FILE_UPLOAD_MAX_MEMORY_SIZE and spill to disk,
# so this does not need to cover them.
#
# 15 MB default gives plenty of headroom over the 5 MB ingest cap for any
# non-ingest JSON bodies (webhooks, bulk invites, assemble manifests) while
# still killing the pre-existing 4 GB DoS vector. Operators can raise via env;
# if GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE is itself raised above 15 MB, this
# default scales with it so the two stay coherent.
DATA_UPLOAD_MAX_MEMORY_SIZE = env.int(
    "DATA_UPLOAD_MAX_MEMORY_SIZE",
    default=max(15 * 1024 * 1024, GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE + 1024 * 1024),
)
DATA_UPLOAD_MAX_NUMBER_FIELDS = env.int(
    "DATA_UPLOAD_MAX_NUMBER_FIELDS",
    default=global_settings.DATA_UPLOAD_MAX_NUMBER_FIELDS,
)

PARTITION_HASH_BUCKETS = env.int("PARTITION_HASH_BUCKETS", 4)

# ── Retention settings ──────────────────────────────────────────────
# Master default; falls back to legacy GLITCHTIP_MAX_EVENT_LIFE_DAYS
GLITCHTIP_RETENTION_DAYS = env.int(
    "GLITCHTIP_RETENTION_DAYS",
    default=env.int("GLITCHTIP_MAX_EVENT_LIFE_DAYS", default=90),
)

# Per-type total retention (hot + cold combined)
GLITCHTIP_EVENT_RETENTION_DAYS = env.int(
    "GLITCHTIP_EVENT_RETENTION_DAYS", default=GLITCHTIP_RETENTION_DAYS
)
GLITCHTIP_TRANSACTION_RETENTION_DAYS = env.int(
    "GLITCHTIP_TRANSACTION_RETENTION_DAYS",
    default=env.int(
        "GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS", default=GLITCHTIP_RETENTION_DAYS
    ),
)
# Reject transaction/span events whose timestamp is further in the future
# than this (clearly-broken clients) — bounds garbage so cold-storage
# hour-bucketing isn't polluted. Clients with badly skewed clocks that
# previously squeaked through now get HTTP 400 "Event time in the
# future." on transaction ingest. Not an operator knob.
GLITCHTIP_TRANSACTION_FUTURE_SKEW = timedelta(hours=1)

# Retention for raw span Parquet (the T1 hourly / T2 daily tiers). Most
# span data is never read; raw is kept only long enough for recent
# debugging and point lookups, then dropped. Trend rollups
# (performance_spans_rollup) are kept for the much longer
# GLITCHTIP_TRANSACTION_RETENTION_DAYS instead. Operator-tunable.
GLITCHTIP_SPAN_RAW_RETENTION_DAYS = env.int(
    "GLITCHTIP_SPAN_RAW_RETENTION_DAYS", default=30
)
GLITCHTIP_UPTIME_RETENTION_DAYS = env.int(
    "GLITCHTIP_UPTIME_RETENTION_DAYS",
    default=env.int(
        "GLITCHTIP_MAX_UPTIME_CHECK_LIFE_DAYS", default=GLITCHTIP_RETENTION_DAYS
    ),
)
GLITCHTIP_FILE_RETENTION_DAYS = env.int(
    "GLITCHTIP_FILE_RETENTION_DAYS",
    default=env.int("GLITCHTIP_MAX_FILE_LIFE_DAYS", default=GLITCHTIP_RETENTION_DAYS),
)
GLITCHTIP_LOG_RETENTION_DAYS = env.int(
    "GLITCHTIP_LOG_RETENTION_DAYS",
    default=env.int("GLITCHTIP_LOGS_COLD_DAYS", default=GLITCHTIP_RETENTION_DAYS),
)
GLITCHTIP_RELEASE_RETENTION_DAYS = env.int(
    "GLITCHTIP_RELEASE_RETENTION_DAYS", default=365
)

# Check if a throttle is needed 1 out of every 5000 event requests
GLITCHTIP_THROTTLE_CHECK_INTERVAL = env.int("GLITCHTIP_THROTTLE_CHECK_INTERVAL", 5000)
SEARCH_MAX_LEXEMES = 3800  # Postgres search vectors will truncate after

GLITCHTIP_FREE_TIER_EVENTS = env.int("GLITCHTIP_FREE_TIER_EVENTS", 1000)

# Enable/disable logs feature. When False, log events are rejected at ingest.
GLITCHTIP_ENABLE_LOGS = env.bool("GLITCHTIP_ENABLE_LOGS", True)

# Fleet-wide default for server-side PII scrubbing at ingest. A project's own
# `scrub_config` (JSON on the Project model) overrides this; when a project has
# none, this default applies. Set GLITCHTIP_PII_SCRUB_DEFAULT to a JSON object
# matching apps.event_ingest.pii_scrubber.ScrubConfig to turn scrubbing on for
# every project at once, e.g. '{"enabled": true, "scrub_emails": false}'.
# Default {} means scrubbing is off unless a project opts in.
GLITCHTIP_PII_SCRUB_DEFAULT = env.json("GLITCHTIP_PII_SCRUB_DEFAULT", {})

# Enable/disable uptime monitoring. When False, uptime checks are not dispatched
# and uptime API endpoints are not registered.
GLITCHTIP_ENABLE_UPTIME = env.bool("GLITCHTIP_ENABLE_UPTIME", True)

# Allow uptime monitors to target private/internal IPs (RFC1918, loopback, link-local).
# Set to True if running GlitchTip on an internal network to monitor private services.
# Default False blocks SSRF attacks against internal infrastructure.
GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS = env.bool(
    "GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS", False
)

# Allow alert webhooks (Discord, Slack-style, Teams, ntfy, Zulip, generic) to target
# private/internal IPs. Kept separate from the uptime flag: a user may legitimately
# want to monitor an internal service without also permitting webhook-triggered
# fetches to internal addresses, which are an exfiltration channel.
GLITCHTIP_ALLOW_PRIVATE_IPS = env.bool("GLITCHTIP_ALLOW_PRIVATE_IPS", False)


# Hot storage (PostgreSQL retention before archival to cold)
GLITCHTIP_EVENT_HOT_DAYS = env.int(
    "GLITCHTIP_EVENT_HOT_DAYS",
    default=env.int("GLITCHTIP_EVENTS_HOT_DAYS", default=30),
)
GLITCHTIP_LOG_HOT_DAYS = env.int(
    "GLITCHTIP_LOG_HOT_DAYS",
    default=env.int("GLITCHTIP_LOGS_HOT_DAYS", default=7),
)

# DuckDB extension directory (pre-installed in Docker image at /opt/duckdb/extensions)
DUCKDB_EXTENSION_DIRECTORY = env.str("DUCKDB_EXTENSION_DIRECTORY", None)


def _get_cgroup_memory_bytes() -> int | None:
    """Read cgroup v2 memory limit (Docker/Kubernetes). Returns None if unavailable."""
    try:
        with open("/sys/fs/cgroup/memory.max") as f:
            val = f.read().strip()
            if val != "max":
                return int(val)
    except (FileNotFoundError, PermissionError, ValueError):
        pass
    return None


def _get_cgroup_cpu_count() -> int | None:
    """Read cgroup v2 CPU quota (Docker/Kubernetes). Returns None if unavailable.

    Reads cpu.max which contains "quota period" (e.g. "200000 100000" = 2 CPUs).
    Returns the effective CPU count, minimum 1.
    """
    try:
        with open("/sys/fs/cgroup/cpu.max") as f:
            parts = f.read().strip().split()
            if parts[0] == "max":
                return None  # No CPU limit set
            quota = int(parts[0])
            period = int(parts[1])
            return max(1, quota // period)
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        pass
    return None


def _default_duckdb_memory_limit() -> str:
    """Auto-detect a safe DuckDB memory limit from container/system memory.

    Uses 25% of container memory (cgroup v2) or system memory, capped at
    256 MB. This is conservative because DuckDB's memory_limit only bounds
    its internal buffer pool — thread stacks, mmap'd file regions, and
    jemalloc overhead are all OUTSIDE this limit. A 256 MB buffer pool
    with 2 threads typically peaks at ~400-500 MB total process impact.
    """
    try:
        total = _get_cgroup_memory_bytes()
        if total is None:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        quarter = total // 4
        mb = quarter // (1024 * 1024)
        return f"{min(mb, 256)}MB"
    except (ValueError, OSError, AttributeError):
        return "128MB"


def _default_duckdb_threads() -> int:
    """Auto-detect DuckDB thread count from container CPU limit.

    Reads cgroup v2 cpu.max (Docker/Kubernetes) to get the pod's CPU
    quota. Falls back to min(os.cpu_count(), 4) for bare-metal.

    Without this, DuckDB defaults to os.cpu_count() which in Kubernetes
    returns the NODE's cores (e.g. 64), not the pod's limit (e.g. 2).
    Each thread allocates scan buffers outside memory_limit, so uncapped
    threads are the primary cause of VmPeak explosion.
    """
    cgroup_cpus = _get_cgroup_cpu_count()
    if cgroup_cpus is not None:
        return max(1, cgroup_cpus)
    # Bare-metal / no cgroup: cap at 4 to be safe
    return min(os.cpu_count() or 2, 4)


# DuckDB memory limit — controls peak RAM for analytical reads.
# Also used as a proxy for the deployment's memory budget to size
# arro3 write batches during archival.
# Auto-detected from cgroup memory limit (25%, capped at 256 MB).
# Set to empty string to disable (unbounded memory).
DUCKDB_MEMORY_LIMIT = env.str("DUCKDB_MEMORY_LIMIT", _default_duckdb_memory_limit())
# Max threads for DuckDB queries. Auto-detected from cgroup CPU quota
# (Kubernetes/Docker) or capped at 4 for bare-metal.
# DuckDB's default (host core count) causes VmPeak explosion in containers.
DUCKDB_THREADS = env.int("DUCKDB_THREADS", _default_duckdb_threads())
# Writable directory for DuckDB spill-to-disk. Set to empty string to disable
# the memory limit (needed for read-only root filesystems with no writable mount).
DUCKDB_TEMP_DIRECTORY = env.str("DUCKDB_TEMP_DIRECTORY", "/tmp")

# Cold storage bucket — defaults to AWS_STORAGE_BUCKET_NAME so users with S3
# configured get cold storage automatically. Files use cold_storage/ prefix.
GLITCHTIP_COLD_STORAGE_BUCKET = env.str(
    "GLITCHTIP_COLD_STORAGE_BUCKET",
    default=env.str("AWS_STORAGE_BUCKET_NAME", None),
)

# Local directory for cold storage (alternative to S3 for simple deployments)
GLITCHTIP_COLD_STORAGE_DIR = env.str("GLITCHTIP_COLD_STORAGE_DIR", None)

# Set to "true" to enable DuckDB cold storage archival.
# Requires a storage backend (GLITCHTIP_COLD_STORAGE_BUCKET, GLITCHTIP_COLD_STORAGE_DIR,
# or a "cold" STORAGES alias).
GLITCHTIP_ENABLE_DUCKDB = env.str("GLITCHTIP_ENABLE_DUCKDB", None)

# Cold storage cleanup: True = GT deletes old files, False = use S3 lifecycle policies
# High-scale deployments should disable this and configure lifecycle policies on the bucket
GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED = env.bool(
    "GLITCHTIP_COLD_STORAGE_CLEANUP_ENABLED", True
)

# Freezes acceptance of new events, for use during db maintenance
MAINTENANCE_EVENT_FREEZE = env.bool("MAINTENANCE_EVENT_FREEZE", False)

GLITCHTIP_ENABLE_MCP = env.bool("GLITCHTIP_ENABLE_MCP", False)

# For development purposes only, prints out inbound event store json
EVENT_STORE_DEBUG = env.bool("EVENT_STORE_DEBUG", False)


STATIC_URL = "static/"
# Base HREF, such as example.com/glitchtip/ where BASE_PATH would be "/glitchtip"
if "BASE_PATH" in os.environ or "FORCE_SCRIPT_NAME" in os.environ:
    FORCE_SCRIPT_NAME = env.str("BASE_PATH", env.str("FORCE_SCRIPT_NAME", ""))


# GlitchTip can track GlitchTip's own errors.
# When SENTRY_DSN points back to the same instance, InternalTransport
# bypasses HTTP to prevent feedback loops while still capturing errors.
SENTRY_DSN = env.str("SENTRY_DSN", None)
# Optionally allow a different DSN for the frontend
SENTRY_FRONTEND_DSN = env.str("SENTRY_FRONTEND_DSN", SENTRY_DSN)
# Set sample_rate to 1.0 to capture 100%.
SENTRY_SAMPLE_RATE = env.float("SENTRY_SAMPLE_RATE", 1.0)
# Set traces_sample_rate to 1.0 to capture 100%. Recommended to keep this value low.
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", 0.01)
# Enable sentry-sdk logs feature to send logs to the configured DSN
SENTRY_ENABLE_LOGS = env.bool("SENTRY_ENABLE_LOGS", False)


def _is_self_referencing_dsn(sentry_dsn, glitchtip_url):
    """Check if SENTRY_DSN points back to this GlitchTip instance."""
    from urllib.parse import urlparse

    dsn = urlparse(sentry_dsn)
    dsn_port = dsn.port or (443 if dsn.scheme == "https" else 80)
    gt_port = glitchtip_url.port or (443 if glitchtip_url.scheme == "https" else 80)
    return dsn.hostname == glitchtip_url.hostname and dsn_port == gt_port


if SENTRY_DSN:
    import inspect

    import sentry_sdk
    from django.http import UnreadablePostError
    from sentry_sdk.integrations.asyncio import AsyncioIntegration
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.django import views as _sentry_django_views
    from sentry_sdk.integrations.modules import ModulesIntegration

    # Sentry's Django integration binds the deprecated asyncio.iscoroutinefunction
    # alias in django/views.py and calls it once per request inside
    # sentry_patched_make_view_atomic. On Python 3.14 that alias runs the warnings
    # machinery (frame inspection + message formatting) on every call even when the
    # DeprecationWarning is filtered out, churning the heap on every request. Point
    # that one module-level name at the non-deprecated inspect.iscoroutinefunction
    # instead of mutating the asyncio module globally. Sentry already guards
    # django/asgi.py the same way but missed views.py; see
    # https://github.com/getsentry/sentry-python/issues/6085
    _sentry_django_views.iscoroutinefunction = inspect.iscoroutinefunction

    from glitchtip.internal_transport import InternalTransport

    _is_self_referencing = env.bool(
        "SENTRY_SELF_REFERENCING",
        _is_self_referencing_dsn(SENTRY_DSN, GLITCHTIP_URL),
    )

    def before_send(event, hint):
        """Don't log useless, inactionable errors in Sentry."""
        if "log_record" in hint:
            if hint["log_record"].name == "django.security.DisallowedHost":
                return None
        if "exc_info" in hint:
            _, exc_value, _ = hint["exc_info"]
            if isinstance(exc_value, UnreadablePostError):
                return None

        # --- Self-referencing loop protection ---
        # Synchronous recursion guard: drops events generated while
        # InternalTransport is processing an envelope. Async queue loops
        # are handled by the transport's rate limiter instead.
        if _is_self_referencing:
            from glitchtip.internal_transport import _processing_internal

            if _processing_internal.get():
                return None

        return event

    # Ignore whitenoise served static routes
    def traces_sampler(sampling_context):
        if (
            sampling_context.get("wsgi_environ", {})
            .get("PATH_INFO", "")
            .startswith(STATIC_URL)
        ):
            return 0.0
        return SENTRY_TRACES_SAMPLE_RATE

    release = "glitchtip@" + GLITCHTIP_VERSION if GLITCHTIP_VERSION else None

    _is_worker = env.bool("IS_WORKER", False)
    _embed_worker = os.environ.get("GLITCHTIP_EMBED_WORKER") == "true"
    if _is_worker:
        _default_service = "glitchtip-worker"
    elif _embed_worker:
        _default_service = "glitchtip"
    else:
        _default_service = "glitchtip-web"

    SENTRY_SERVICE_NAME = env.str("SENTRY_SERVICE_NAME", _default_service)

    # Disable auto-discovered integrations that add overhead without value:
    # - ModulesIntegration: serializes all ~2600 sys.modules on every error event
    # - Starlette: auto-detected but GlitchTip uses Django, not Starlette
    # - AioHttp: instruments aiohttp server; we only use aiohttp as an HTTP client
    # - MCP: instruments MCP server calls; minimal value vs overhead
    _disabled_integrations = [ModulesIntegration()]
    _optional_disable = [
        ("sentry_sdk.integrations.starlette", "StarletteIntegration"),
        ("sentry_sdk.integrations.aiohttp", "AioHttpIntegration"),
        ("sentry_sdk.integrations.mcp", "MCPIntegration"),
    ]
    for _mod_path, _cls_name in _optional_disable:
        try:
            _mod = __import__(_mod_path, fromlist=[_cls_name])
            _disabled_integrations.append(getattr(_mod, _cls_name)())
        except Exception:
            pass

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        transport=InternalTransport if _is_self_referencing else None,
        integrations=[DjangoIntegration(), AsyncioIntegration()],
        disabled_integrations=_disabled_integrations,
        before_send=before_send,
        release=release,
        environment=ENVIRONMENT,
        auto_session_tracking=False,
        send_client_reports=False,
        sample_rate=SENTRY_SAMPLE_RATE,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        traces_sampler=traces_sampler,
        max_value_length=2048,
        max_breadcrumbs=20,
        enable_logs=SENTRY_ENABLE_LOGS,
    )
    sentry_sdk.get_global_scope().set_attribute("service.name", SENTRY_SERVICE_NAME)


def show_toolbar(request):
    return env("DEBUG_TOOLBAR")


DEBUG_TOOLBAR = env("DEBUG_TOOLBAR")
DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": show_toolbar}
DEBUG_TOOLBAR_PANELS = [
    "debug_toolbar.panels.versions.VersionsPanel",
    "debug_toolbar.panels.timer.TimerPanel",
    "debug_toolbar.panels.settings.SettingsPanel",
    "debug_toolbar.panels.headers.HeadersPanel",
    "debug_toolbar.panels.request.RequestPanel",
    "debug_toolbar.panels.sql.SQLPanel",
    # "debug_toolbar.panels.history.HistoryPanel",
    # "debug_toolbar.panels.profiling.ProfilingPanel",
]


# Should GlitchTip trust and use proxy settings from environment variables (HTTP_PROXY, HTTPS_PROXY, NO_PROXY)
PROXY_ENV = env.bool("PROXY_ENV", False)
AIOHTTP_CONFIG = {
    "headers": {"User-Agent": "GlitchTip/" + GLITCHTIP_VERSION},
    "trust_env": PROXY_ENV,
    "max_field_size": 16380,  # 2x default
}

# Application definition
ENABLE_ADMIN = env.bool("ENABLE_ADMIN", True)
ENABLE_OPENAPI = env.bool("ENABLE_OPENAPI", True)

WEB_INSTALLED_APPS = [
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ninja",
]
if ENABLE_ADMIN:
    WEB_INSTALLED_APPS.insert(0, "django.contrib.admin")


INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "django.contrib.postgres",
    "django_prometheus",
    "allauth",
    "allauth.account",
    "allauth.headless",
    "allauth.mfa",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.digitalocean",
    "allauth.socialaccount.providers.gitea",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.gitlab",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.microsoft",
    "allauth.socialaccount.providers.nextcloud",
    "allauth.socialaccount.providers.openid_connect",
    "allauth.socialaccount.providers.okta",
    "anymail",
    "corsheaders",
    "django_extensions",
]
if DEBUG_TOOLBAR:
    INSTALLED_APPS.append("debug_toolbar")
INSTALLED_APPS += [
    "storages",
    "django_vtasks",
    "glitchtip",
    "apps.alerts",
    "apps.environments",
    "apps.organizations_ext",
    "apps.users",
    "apps.importer",
    "apps.uptime",
    "apps.performance",
    "apps.projects",
    "apps.teams",
    "apps.releases",
    "apps.stripe",
    "apps.sourcecode",
    "apps.difs",
    "apps.api_tokens",
    "apps.files",
    "apps.issue_events",
    "apps.logs",
    "apps.event_ingest",
    "apps.mcp",
    "apps.oauth",
    "import_export",  # Contains import management command, keep under apps.importer
]

IS_WORKER = env.bool("IS_WORKER", False)
if not IS_WORKER:
    INSTALLED_APPS = WEB_INSTALLED_APPS + INSTALLED_APPS

# Ensure no one uses runsslserver in production
if SECRET_KEY == "change_me" and DEBUG is True:
    INSTALLED_APPS += ["sslserver"]

ENABLE_OBSERVABILITY_API = env("ENABLE_OBSERVABILITY_API")
if ENABLE_OBSERVABILITY_API:
    INSTALLED_APPS.append("apps.observability")

# Workaround https://github.com/korfuri/django-prometheus/issues/34
PROMETHEUS_EXPORT_MIGRATIONS = False

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
]
if DEBUG_TOOLBAR:
    MIDDLEWARE.append("debug_toolbar.middleware.DebugToolbarMiddleware")
MIDDLEWARE += [
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "allauth_async.account.middleware.AsyncAccountMiddleware",
]

if "GRANIAN_STATIC_PATH_MOUNT" in os.environ:
    MIDDLEWARE.remove("whitenoise.middleware.WhiteNoiseMiddleware")

if ENABLE_OBSERVABILITY_API:
    MIDDLEWARE.insert(0, "django_prometheus.middleware.PrometheusBeforeMiddleware")
    MIDDLEWARE.append("django_prometheus.middleware.PrometheusAfterMiddleware")

# DB I/O is routed through django-async-backend (the only DB engine; see
# the ENGINE assignment in the DATABASES loop below). Inserted at the head
# of the chain so async cursors are returned to the pool before any other
# middleware finalises the response.
MIDDLEWARE.insert(0, "django_async_backend.middleware.close_async_connections")

ROOT_URLCONF = "glitchtip.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [path("dist"), path("templates")],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.csp",
            ],
        },
    },
]

WSGI_APPLICATION = "glitchtip.wsgi.application"

# Defaults to True because Sentry SDKs send events from arbitrary user domains.
# Set to False and configure CORS_ORIGIN_WHITELIST only if you control all client origins.
CORS_ORIGIN_ALLOW_ALL = env.bool("CORS_ORIGIN_ALLOW_ALL", True)
CORS_ORIGIN_WHITELIST = env.tuple("CORS_ORIGIN_WHITELIST", str, default=())
CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-sentry-auth",
    "baggage",
    "sentry-trace",
]

BILLING_ENABLED = False
STRIPE_PUBLIC_KEY = env.str("STRIPE_PUBLIC_KEY", None)
STRIPE_SECRET_KEY = env.str("STRIPE_SECRET_KEY", None)
STRIPE_WEBHOOK_SECRET = env.str("STRIPE_WEBHOOK_SECRET", None)
STRIPE_WEBHOOK_SECRET_SUBSCRIPTION = env.str(
    "STRIPE_WEBHOOK_SECRET_SUBSCRIPTION", STRIPE_WEBHOOK_SECRET
)
STRIPE_REGION = env.str("STRIPE_REGION", "")  # Sets stripe customer metadata
STRIPE_REGION_DOMAINS = env.dict(
    "STRIPE_REGION_DOMAINS", default={}
)  # Forward webhooks to appropriate domain
if STRIPE_PUBLIC_KEY and STRIPE_SECRET_KEY:
    BILLING_ENABLED = True

# Set to chatwoot website token to enable live help widget. Assumes app.chatwoot.com.
CHATWOOT_WEBSITE_TOKEN = env.str("CHATWOOT_WEBSITE_TOKEN", None)
CHATWOOT_IDENTITY_TOKEN = env.str("CHATWOOT_IDENTITY_TOKEN", None)

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", str, [])
SECURE_BROWSER_XSS_FILTER = True

# Consider tracking CSP reports with GlitchTip itself
# Enable Chatwoot only when configured
default_connect_src = [CSP.SELF, "https://*.glitchtip.com"]
if CHATWOOT_WEBSITE_TOKEN:
    default_connect_src.append("https://app.chatwoot.com")
# Enable stripe by default only when configured
stripe_domain = "https://js.stripe.com"
default_script_src = [
    CSP.SELF,
    "https://*.glitchtip.com",
    "'sha256-iRcDQ27XiXX4k+jbJ8nGeQFBnBOjmII7FdMlixb6QE4='",  # Theme picker inline JS
]
default_frame_src = [CSP.SELF]
if BILLING_ENABLED:
    default_script_src.append(stripe_domain)
    default_frame_src.append(stripe_domain)
SECURE_CSP_DIRECTIVES = {
    "default-src": env.list("CSP_DEFAULT_SRC", str, [CSP.SELF]) + [CSP.NONCE],
    "style-src": env.list("CSP_STYLE_SRC", str, [CSP.SELF]) + [CSP.NONCE],
    "font-src": env.list("CSP_FONT_SRC", str, [CSP.SELF, "data:"]),
    "connect-src": env.list("CSP_CONNECT_SRC", str, default_connect_src),
    "script-src": env.list("CSP_SCRIPT_SRC", str, default_script_src) + [CSP.NONCE],
    "img-src": env.list("CSP_IMG_SRC", str, [CSP.SELF]),
    "frame-src": env.list("CSP_FRAME_SRC", str, default_frame_src),
}
if report_uri := env.tuple("CSP_REPORT_URI", str, None):
    SECURE_CSP_DIRECTIVES["report-uri"] = report_uri

if "CSP_STYLE_SRC_ELEM" in os.environ:
    SECURE_CSP_DIRECTIVES["style-src-elem"] = env.list("CSP_STYLE_SRC_ELEM", str)
if "CSP_WORKER_SRC" in os.environ:
    SECURE_CSP_DIRECTIVES["worker-src"] = env.list("CSP_WORKER_SRC", str)

csp_report_only = env.bool("CSP_REPORT_ONLY", False)
if csp_report_only:
    SECURE_CSP_REPORT_ONLY = SECURE_CSP_DIRECTIVES
    SECURE_CSP = {}
else:
    SECURE_CSP = SECURE_CSP_DIRECTIVES
    SECURE_CSP_REPORT_ONLY = {}


SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", False)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
# Cookie Secure flags default to True when GLITCHTIP_URL is https so typical
# production deploys get secure cookies out of the box. Keep False for http
# so a local/internal-LAN deploy (no TLS) still lets users log in without
# manual config. Both are env-overridable in either direction.
_cookie_secure_default = GLITCHTIP_URL.scheme == "https"
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", _cookie_secure_default)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", _cookie_secure_default)
SESSION_COOKIE_SAMESITE = env.str("SESSION_COOKIE_SAMESITE", "Lax")

DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", "webmaster@localhost")

ANYMAIL_SETTINGS = [
    "MAILGUN_API_KEY",
    "MAILGUN_SENDER_DOMAIN",
    "MAILGUN_API_URL",
    "MAILGUN_WEBHOOK_SIGNING_KEY",
    "SENDGRID_API_KEY",
    "SENDGRID_API_URL",
    "POSTMARK_SERVER_TOKEN",
    "POSTMARK_API_URL",
    "MANDRILL_API_KEY",
    "MANDRILL_WEBHOOK_KEY",
    "MANDRILL_WEBHOOK_URL",
    "MANDRILL_API_URL",
    "SENDINBLUE_API_KEY",
    "SENDINBLUE_API_URL",
    "MAILJET_API_KEY",
    "MAILJET_SECRET_KEY",
    "MAILJET_API_URL",
    "POSTAL_API_KEY",
    "POSTAL_API_URL",
    "POSTAL_WEBHOOK_KEY",
    "SPARKPOST_API_KEY",
    "SPARKPOST_API_URL",
    "SPARKPOST_TRACK_INITIAL_OPEN_AS_OPENED",
]

ANYMAIL = {
    anymail_var: env.str(anymail_var)
    for anymail_var in ANYMAIL_SETTINGS
    if anymail_var in os.environ
}

ACCOUNT_EMAIL_SUBJECT_PREFIX = env.str("ACCOUNT_EMAIL_SUBJECT_PREFIX", "")

# Database
# https://docs.djangoproject.com/en/dev/ref/settings/#databases
# Use either DATABASE_URL or individual components
DATABASES = {
    "default": env.db(
        "DATABASE_URL", default="postgres://postgres:postgres@postgres:5432/postgres"
    )
}
if env("READ_ONLY_DATABASE_URL"):
    DATABASES["read_only"] = env.db("READ_ONLY_DATABASE_URL")
if env("MAINTENANCE_DATABASE_URL"):
    DATABASES["maintenance"] = env.db("MAINTENANCE_DATABASE_URL")

MAINTENANCE_DATABASE_ALIAS = "maintenance" if "maintenance" in DATABASES else "default"

# If component variables like DATABASE_HOST are provided, update the base config
if env.str("DATABASE_HOST", None):
    DATABASES["default"].update(
        {
            "NAME": env.str("DATABASE_NAME", "postgres"),
            "USER": env.str("DATABASE_USER", "postgres"),
            "PASSWORD": env.str("DATABASE_PASSWORD"),
            "HOST": env.str("DATABASE_HOST"),
            "PORT": env.str("DATABASE_PORT", "5432"),
        }
    )
# Database engine. Defaults to the async-backend (psycopg) used in production.
# Opt-in: set DATABASE_ENGINE=gt_rust.django_backend to run the ORM on the Rust
# Postgres driver instead — one shared tokio pool serving sync + async, on the
# same runtime as the valkey driver. Behavior is unchanged unless switched.
DATABASE_ENGINE = env.str(
    "DATABASE_ENGINE", "django_async_backend.db.backends.postgresql"
)
# Add other settings that apply to both methods.
for db_config in DATABASES.values():
    # async-backend's postgresql backend extends Django's stock postgresql
    # and adds an AsyncDatabaseWrapper that ``async_connections`` discovers
    # via load_backend. Sync paths (ORM, migrations, admin) still go through
    # psycopg unchanged. gt_rust.django_backend is the drop-in Rust equivalent.
    db_config["ENGINE"] = DATABASE_ENGINE
    db_config.setdefault("CONN_MAX_AGE", env.int("DATABASE_CONN_MAX_AGE", 0))
    db_config.setdefault(
        "CONN_HEALTH_CHECKS", env.bool("DATABASE_CONN_HEALTH_CHECKS", False)
    )
    db_config.setdefault("DISABLE_SERVER_SIDE_CURSORS", True)

    # Check if 'OPTIONS' exists and if 'pool' is already defined (e.g. via specific dict config)
    options = db_config.setdefault("OPTIONS", {})
    pooling_already_configured = "pool" in options

    # Django Connection Pooling requires CONN_MAX_AGE = 0
    if not pooling_already_configured and db_config["CONN_MAX_AGE"] == 0:
        options["pool"] = {
            "min_size": env.int("DATABASE_POOL_MIN_SIZE", 2),
            "max_size": env.int("DATABASE_POOL_MAX_SIZE", 20),
            "timeout": env.int("DATABASE_POOL_TIMEOUT", 30),
        }
        # Optional pooled-connection recycling horizon. A connection retains
        # buffer memory sized to the largest message it ever carried;
        # recycling bounds how long that lasts. Unset keeps the driver's
        # default lifetime; drivers jitter the deadline to avoid reconnect
        # stampedes.
        _pool_max_lifetime = env.int("DATABASE_POOL_MAX_LIFETIME", 0)
        if _pool_max_lifetime > 0:
            options["pool"]["max_lifetime"] = _pool_max_lifetime

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Valkey/Redis connection. VALKEY_URL is the source of truth — supports any scheme
# that django-vcache understands: redis://, rediss://, valkey://, valkeys://, sentinel://
# Component env vars (VALKEY_HOST, etc.) are a convenience for simple single-node setups.
VALKEY_HOST = env.str("VALKEY_HOST", env.str("REDIS_HOST", None))
if VALKEY_HOST:
    VALKEY_PORT = env.str("VALKEY_PORT", env.str("REDIS_PORT", "6379"))
    VALKEY_DATABASE = env.str("VALKEY_DATABASE", env.str("REDIS_DATABASE", "0"))
    VALKEY_PASSWORD = env.str("VALKEY_PASSWORD", env.str("REDIS_PASSWORD", None))
    if VALKEY_PASSWORD:
        VALKEY_URL = (
            f"redis://:{VALKEY_PASSWORD}@{VALKEY_HOST}:{VALKEY_PORT}/{VALKEY_DATABASE}"
        )
    else:
        VALKEY_URL = f"redis://{VALKEY_HOST}:{VALKEY_PORT}/{VALKEY_DATABASE}"
else:
    VALKEY_URL = env.str("VALKEY_URL", env.str("REDIS_URL", "redis://redis:6379/0"))
db = DATABASES["default"]
# Use Specified broker url, valkey url, or fallback to postgresql
IS_LOAD_TEST = env("IS_LOAD_TEST")

# Time in seconds to debounce some frequently run tasks
TASK_DEBOUNCE_DELAY = env.int("TASK_DEBOUNCE_DELAY", 30)
UPTIME_CHECK_INTERVAL = 1
ALERT_NOTIFICATION_INTERVAL = env.int("ALERT_NOTIFICATION_INTERVAL", 60)
VTASKS_SCHEDULE = {
    "send-alert-notifications": {
        "task": "apps.alerts.tasks.process_event_alerts",
        "schedule": ALERT_NOTIFICATION_INTERVAL,
    },
    "perform-maintenance": {
        "task": "glitchtip.tasks.perform_maintenance",
        "schedule": crontab(hour=5, minute=0),
    },
}

if str(GLITCHTIP_ENABLE_DUCKDB or "").lower() == "true":
    VTASKS_SCHEDULE["promote-spans"] = {
        "task": "apps.performance.tasks.promote_spans",
        "schedule": 300,
    }
    VTASKS_SCHEDULE["compact-span-chunks"] = {
        "task": "apps.performance.tasks.compact_span_chunks",
        # Collapse each day shortly after it seals (now - MAX_AGE - margin)
        # rather than once daily. Cheap when nothing is newly sealed.
        "schedule": 15 * 60,
    }

if GLITCHTIP_ENABLE_UPTIME:
    VTASKS_SCHEDULE["uptime-dispatch-checks"] = {
        "task": "apps.uptime.tasks.dispatch_checks",
        "schedule": UPTIME_CHECK_INTERVAL,
    }

TASKS = {
    "default": {
        "BACKEND": "django_vtasks.backends.db.DatabaseTaskBackend",
    }
}

VTASKS_QUEUES = ["default", "ingest"]

# Batch queues configuration - must be defined before tasks are imported
VTASKS_BATCH_QUEUES = {
    "ingest": {
        "count": 100,
        "timeout": 2.0,
    },
}

# Maximum number of issues send in a single alert payload
MAX_ISSUES_PER_ALERT = env.int("MAX_ISSUES_PER_ALERT", 3)

# Support running in WSGI mode (uWSGI or Granian WSGI)
# We need to use a different cache backend for WSGI to avoid async loop issues
try:
    import uwsgi  # noqa

    HAS_UWSGI = True
except ImportError:
    HAS_UWSGI = False

# Default to True for now, but if running under uWSGI or Granian WSGI, we might need to switch
USE_ASYNC_SERVER = env.bool("USE_ASYNC_SERVER", True)

# VALKEY_URL drives both cache and task broker. Empty string disables valkey.
# TLS: use rediss:// or valkeys:// URL scheme. For custom CA or mTLS, set
# VALKEY_SSL_CA_CERTS, VALKEY_SSL_CERTFILE, VALKEY_SSL_KEYFILE env vars.
# Set VALKEY_SSL_CERT_REQS=none to skip certificate verification.
if VALKEY_URL:
    _valkey_options = {}
    if _ssl_ca := env.str("VALKEY_SSL_CA_CERTS", None):
        _valkey_options["ssl_ca_certs"] = _ssl_ca
    if _ssl_cert := env.str("VALKEY_SSL_CERTFILE", None):
        _valkey_options["ssl_certfile"] = _ssl_cert
    if _ssl_key := env.str("VALKEY_SSL_KEYFILE", None):
        _valkey_options["ssl_keyfile"] = _ssl_key
    if _ssl_reqs := env.str("VALKEY_SSL_CERT_REQS", None):
        _valkey_options["ssl_cert_reqs"] = _ssl_reqs
    CACHES = {
        "default": {
            "BACKEND": "django_vcache.backend.ValkeyCache",
            "LOCATION": VALKEY_URL,
            **({"OPTIONS": _valkey_options} if _valkey_options else {}),
        }
    }
    TASKS = {
        "default": {
            "BACKEND": "django_vtasks.backends.valkey.ValkeyTaskBackend",
            "OPTIONS": {"cache_alias": "default"},
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
else:  # Fallback to database cache
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.db.DatabaseCache",
            "LOCATION": "django_cache",
        }
    }
    INSTALLED_APPS.append("django.contrib.sessions")
    if "django_vtasks.db" not in INSTALLED_APPS:
        INSTALLED_APPS.append("django_vtasks.db")

SESSION_COOKIE_AGE = env.int("SESSION_COOKIE_AGE", global_settings.SESSION_COOKIE_AGE)

# Password validation
# https://docs.djangoproject.com/en/dev/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]


# Internationalization
# https://docs.djangoproject.com/en/dev/topics/i18n/

LANGUAGE_CODE = "en-us"

TIME_ZONE = env.str("TIME_ZONE", "UTC")

USE_I18N = True

USE_TZ = True

STORAGES = {
    "default": {
        "BACKEND": env("DEFAULT_FILE_STORAGE"),
    },
    "staticfiles": {
        "BACKEND": env.str(
            "STATICFILES_STORAGE",
            "whitenoise.storage.CompressedManifestStaticFilesStorage",
        )
    },
}

AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL")
AWS_LOCATION = env("AWS_LOCATION")
AWS_S3_USE_SSL = env("AWS_S3_USE_SSL")
AWS_S3_VERIFY = env("AWS_S3_VERIFY")

AZURE_ACCOUNT_NAME = env("AZURE_ACCOUNT_NAME")
AZURE_ACCOUNT_KEY = env("AZURE_ACCOUNT_KEY")
AZURE_CONTAINER = env("AZURE_CONTAINER")
AZURE_URL_EXPIRATION_SECS = env("AZURE_URL_EXPIRATION_SECS")

GS_BUCKET_NAME = env("GS_BUCKET_NAME")
GS_PROJECT_ID = env("GS_PROJECT_ID")

if AWS_S3_ENDPOINT_URL:
    MEDIA_URL = env.str(
        "MEDIA_URL", "https://%s/%s/" % (AWS_S3_ENDPOINT_URL, AWS_LOCATION)
    )
    STORAGES["default"] = {"BACKEND": "storages.backends.s3boto3.S3Boto3Storage"}
else:
    MEDIA_URL = "media/"
MEDIA_ROOT = env.str("MEDIA_ROOT", "")

STATICFILES_DIRS = [
    "assets",
    "dist",
]
STATIC_ROOT = path("static/")

EMAIL_BACKEND = env.str(
    "EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend"
)
if os.getenv("EMAIL_HOST_USER"):
    EMAIL_HOST_USER = env.str("EMAIL_HOST_USER")
if os.getenv("EMAIL_HOST_PASSWORD"):
    EMAIL_HOST_PASSWORD = env.str("EMAIL_HOST_PASSWORD")
if os.getenv("EMAIL_HOST"):
    EMAIL_HOST = env.str("EMAIL_HOST")
if os.getenv("EMAIL_PORT"):
    EMAIL_PORT = env.str("EMAIL_PORT")
if os.getenv("EMAIL_USE_TLS"):
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS")
if os.getenv("EMAIL_USE_SSL"):
    EMAIL_USE_SSL = env.bool("EMAIL_USE_SSL")
if os.getenv("EMAIL_TIMEOUT"):
    EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT")
if os.getenv("EMAIL_FILE_PATH"):
    EMAIL_FILE_PATH = env.str("EMAIL_FILE_PATH")
if os.getenv(
    "EMAIL_URL"
):  # Careful, this will override most EMAIL_*** settings. Set them all individually, or use EMAIL_URL to set them all at once, but don't do both.
    EMAIL_CONFIG = env.email_url("EMAIL_URL")
    vars().update(EMAIL_CONFIG)
EMAIL_INVITE_THROTTLE_COUNT = env.int("EMAIL_THROTTLE_COUNT", 50)
EMAIL_INVITE_THROTTLE_INTERVAL = env.int("EMAIL_THROTTLE_INTERVAL", 300)  # 5 minutes
EMAIL_INVITE_REQUIRE_VERIFICATION = env.bool("EMAIL_INVITE_REQUIRE_VERIFICATION", False)

# Email is optional. With no transport configured, email is disabled: nothing
# is sent, account verification and password reset are off, and /api/settings/
# omits "email" so the frontend hides email-only UI. Disabling is implicit so a
# bare install with no MTA doesn't crash on the default smtp -> localhost:25.
#
# Auto-enabled by any explicit transport: an EMAIL_* var below, an Anymail
# provider, or a non-default EMAIL_BACKEND. So pointing at localhost:25 yourself
# enables it (and fails loudly if broken) -- only the untouched default is
# "unconfigured". Set EMAIL_ENABLED to override the auto-detection either way.
_EMAIL_TRANSPORT_ENV_VARS = (
    "EMAIL_URL",
    "EMAIL_HOST",
    "EMAIL_PORT",
    "EMAIL_HOST_USER",
    "EMAIL_HOST_PASSWORD",
    "EMAIL_USE_TLS",
    "EMAIL_USE_SSL",
    "EMAIL_TIMEOUT",
    "EMAIL_FILE_PATH",
)
EMAIL_ENABLED = env.bool(
    "EMAIL_ENABLED",
    default=TESTING
    or bool(ANYMAIL)
    or any(var in os.environ for var in _EMAIL_TRANSPORT_ENV_VARS)
    or EMAIL_BACKEND != "django.core.mail.backends.smtp.EmailBackend",
)

AUTH_USER_MODEL = "users.User"
ACCOUNT_ADAPTER = "glitchtip.adapters.CustomDefaultAccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
# Without a mail transport an account's email can never be confirmed, so treat
# it as unverified/untrusted: skip verification entirely rather than minting
# confirmations that can't be delivered. With email configured, keep allauth's
# default "optional" behavior (login allowed, confirmation sent in background).
ACCOUNT_EMAIL_VERIFICATION = "optional" if EMAIL_ENABLED else "none"
ACCOUNT_REAUTHENTICATION_TIMEOUT = SESSION_COOKIE_AGE  # Disabled for now
LOGIN_REDIRECT_URL = "/"
LOGIN_URL = "/login"
HEADLESS_ONLY = True
HEADLESS_FRONTEND_URLS = {
    "account_signup": "/login",
    "account_reset_password": "/reset-password",
    "account_confirm_email": "/profile/confirm-email/{key}/",
    "account_reset_password_from_key": "/reset-password/set-new-password/{key}",
    "socialaccount_login_error": "/login?socialLoginError=true",
}
HEADLESS_CLIENTS = ("browser",)
HEADLESS_SERVE_SPECIFICATION = True
MFA_ADAPTER = "allauth_async.mfa.adapter.AsyncDefaultMFAAdapter"
HEADLESS_ADAPTER = "allauth_async.headless.adapter.AsyncDefaultHeadlessAdapter"
MFA_TOTP_ISSUER = GLITCHTIP_URL.hostname
MFA_TOTP_TOLERANCE = 1
MFA_SUPPORTED_TYPES = ["totp", "webauthn", "recovery_codes"]
MFA_PASSKEY_LOGIN_ENABLED = True
MFA_WEBAUTHN_ALLOW_INSECURE_ORIGIN = DEBUG
SOCIALACCOUNT_ADAPTER = "glitchtip.adapters.CustomSocialAccountAdapter"
INVITATION_BACKEND = "apps.organizations_ext.invitation_backend.InvitationBackend"
SOCIALACCOUNT_PROVIDERS = {}
if GITLAB_URL := env.url("SOCIALACCOUNT_PROVIDERS_gitlab_GITLAB_URL", None):
    SOCIALACCOUNT_PROVIDERS["gitlab"] = {"GITLAB_URL": GITLAB_URL.geturl()}
if GITEA_URL := env.url("SOCIALACCOUNT_PROVIDERS_gitea_GITEA_URL", None):
    SOCIALACCOUNT_PROVIDERS["gitea"] = {"GITEA_URL": GITEA_URL.geturl()}
if NEXTCLOUD_URL := env.url("SOCIALACCOUNT_PROVIDERS_nextcloud_SERVER", None):
    SOCIALACCOUNT_PROVIDERS["nextcloud"] = {"SERVER": NEXTCLOUD_URL.geturl()}
if MICROSOFT_TENANT := env.str("SOCIALACCOUNT_PROVIDERS_microsoft_TENANT", None):
    SOCIALACCOUNT_PROVIDERS["microsoft"] = {"TENANT": MICROSOFT_TENANT}

ENABLE_USER_REGISTRATION = env.bool("ENABLE_USER_REGISTRATION", True)
ENABLE_SOCIAL_APPS_USER_REGISTRATION = env.bool(
    "ENABLE_SOCIAL_APPS_USER_REGISTRATION", ENABLE_USER_REGISTRATION
)
ENABLE_ORGANIZATION_CREATION = env.bool(
    "ENABLE_OPEN_USER_REGISTRATION", env.bool("ENABLE_ORGANIZATION_CREATION", False)
)

AUTHENTICATION_BACKENDS = (
    # Needed to login by username in Django admin, regardless of `allauth`.
    # Subclassed to keep the unknown-user dummy hash off the event loop;
    # revert to django.contrib.auth.backends.ModelBackend on Django 6.1+.
    "apps.users.auth_backends.ModelBackend",
    # `allauth` authentication methods (login by e-mail), with a native
    # aauthenticate() so async login paths never fall back to thread offload
    "allauth_async.account.auth_backends.AsyncAuthenticationBackend",
)

NINJA_PAGINATION_CLASS = "glitchtip.api.pagination.AsyncLinkHeaderPagination"

NINJA_PAGINATION_PER_PAGE = 50

LOGGING_HANDLER_CLASS = env.str("DJANGO_LOGGING_HANDLER_CLASS", "logging.StreamHandler")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "null": {
            "class": "logging.NullHandler",
        },
        "console": {
            "class": LOGGING_HANDLER_CLASS,
        },
    },
    "loggers": {
        "django.security.DisallowedHost": {
            "handlers": ["null"],
            "propagate": False,
        },
    },
    "root": {"handlers": ["console"], "level": env.str("LOG_LEVEL", "WARNING")},
}


# Set to track activity with Plausible
PLAUSIBLE_URL = env.str("PLAUSIBLE_URL", default=None)
PLAUSIBLE_DOMAIN = env.str("PLAUSIBLE_DOMAIN", default=None)

# Support license key — typically a Stripe subscription ID.
# Hides the "Support GlitchTip" banner and enables future support features.
GLITCHTIP_LICENSE_KEY = env.str("GLITCHTIP_LICENSE_KEY", None)

# Legacy setting — still accepted. New deployments should use GLITCHTIP_LICENSE_KEY.
I_PAID_FOR_GLITCHTIP = env.bool("I_PAID_FOR_GLITCHTIP", bool(GLITCHTIP_LICENSE_KEY))

MARKETING_URL = "https://glitchtip.com"
if BILLING_ENABLED:
    I_PAID_FOR_GLITCHTIP = True
    VTASKS_SCHEDULE["check-all-organizations-throttle"] = {
        "task": "apps.organizations_ext.tasks.check_all_organizations_throttle",
        "schedule": timedelta(hours=4),
    }
elif TESTING:
    # Must run tests with billing enabled
    BILLING_ENABLED = True
    logging.disable(logging.WARNING)


if TESTING:
    TEST_RUNNER = "glitchtip.test_runner.TimedTestRunner"
    # Optimization
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    for db_config in DATABASES.values():
        db_config["CONN_MAX_AGE"] = None
        if "OPTIONS" in db_config:
            db_config["OPTIONS"]["pool"] = False
    TASKS = {
        "default": {
            "BACKEND": "django_vtasks.backends.immediate.ImmediateBackend",
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    STORAGES = global_settings.STORAGES
    # https://github.com/evansd/whitenoise/issues/215
    warnings.filterwarnings(
        "ignore", message="No directory at", module="whitenoise.base"
    )
if TESTING:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
CACHE_IS_VALKEY = "valkey" in CACHES["default"]["BACKEND"].lower()

warnings.filterwarnings(
    "ignore", message="No directory at", module="django.core.handlers.base"
)
