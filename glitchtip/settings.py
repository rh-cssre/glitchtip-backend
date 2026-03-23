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

DATA_UPLOAD_MAX_MEMORY_SIZE = 4294967295  # TMP REMOVE THIS
DATA_UPLOAD_MAX_NUMBER_FIELDS = env.int(
    "DATA_UPLOAD_MAX_NUMBER_FIELDS",
    default=global_settings.DATA_UPLOAD_MAX_NUMBER_FIELDS,
)
# Limits size (in bytes) of uncompressed event payloads. Mitigates DOS risk.
GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE = env.int(
    "GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE",
    5 * 1024 * 1024,  # 5 MB
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

# Enable/disable uptime monitoring. When False, uptime checks are not dispatched
# and uptime API endpoints are not registered.
GLITCHTIP_ENABLE_UPTIME = env.bool("GLITCHTIP_ENABLE_UPTIME", True)

# Allow uptime monitors to target private/internal IPs (RFC1918, loopback, link-local).
# Set to True if running GlitchTip on an internal network to monitor private services.
# Default False blocks SSRF attacks against internal infrastructure.
GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS = env.bool(
    "GLITCHTIP_UPTIME_ALLOW_PRIVATE_IPS", False
)


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


def _default_duckdb_memory_limit() -> str:
    """Auto-detect 25% of container/system memory for DuckDB.

    Reads the cgroup memory limit first (works in Docker/Kubernetes),
    then falls back to total system memory via os.sysconf.
    """
    try:
        total = None
        # cgroup v2 (Docker/Kubernetes)
        try:
            with open("/sys/fs/cgroup/memory.max") as f:
                val = f.read().strip()
                if val != "max":
                    total = int(val)
        except (FileNotFoundError, PermissionError):
            pass
        # Host memory fallback
        if total is None:
            total = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
        quarter = total // 4
        mb = quarter // (1024 * 1024)
        return f"{min(mb, 1024)}MB"
    except (ValueError, OSError, AttributeError):
        return "128MB"


# DuckDB memory limit — controls peak RAM for analytical reads.
# Also used as a proxy for the deployment's memory budget to size
# arro3 write batches during archival.
# Defaults to 25% of container/system memory.
# Set to empty string to disable (unbounded memory).
DUCKDB_MEMORY_LIMIT = env.str("DUCKDB_MEMORY_LIMIT", _default_duckdb_memory_limit())
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
    import sentry_sdk
    from django.http import UnreadablePostError
    from sentry_sdk.integrations.django import DjangoIntegration
    from sentry_sdk.integrations.modules import ModulesIntegration

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
        integrations=[DjangoIntegration()],
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
    "glitchtip.middleware.DecompressBodyMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

if "GRANIAN_STATIC_PATH_MOUNT" in os.environ:
    MIDDLEWARE.remove("whitenoise.middleware.WhiteNoiseMiddleware")

if ENABLE_OBSERVABILITY_API:
    MIDDLEWARE.insert(0, "django_prometheus.middleware.PrometheusBeforeMiddleware")
    MIDDLEWARE.append("django_prometheus.middleware.PrometheusAfterMiddleware")

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
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", False)
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
# Add other settings that apply to both methods.
for db_config in DATABASES.values():
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

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Make a best attempt to support both Valkey and Redis. Support full auth string or parts.
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
        "schedule": crontab(hour=3, minute=0),
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

if os.environ.get("CACHE_URL"):
    CACHES = {
        "default": env.cache(),
    }
    if "django_vtasks.db" not in INSTALLED_APPS:
        INSTALLED_APPS.append("django_vtasks.db")
elif VALKEY_URL:
    CACHES = {
        "default": {
            "BACKEND": "django_vcache.backend.ValkeyCache",
            "LOCATION": VALKEY_URL,
        }
    }
    TASKS = {
        "default": {
            "BACKEND": "django_vtasks.backends.valkey.ValkeyTaskBackend",
            "OPTIONS": {"cache_alias": "default"},
        }
    }
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
if cache_sentinel_url := env.str("CACHE_SENTINEL_URL", None):
    # Build a sentinel:// URL for django-vcache v2's Rust driver.
    # CACHE_SENTINEL_URL format: "host1:port,host2:port"
    # Becomes: sentinel://host1:port,host2:port/service_name/db
    cache_sentinel_service = env.str("CACHE_SENTINEL_SERVICE_NAME", "mymaster")
    cache_sentinel_db = env.int("CACHE_SENTINEL_DB", 0)
    CACHES["default"]["LOCATION"] = (
        f"sentinel://{cache_sentinel_url}/{cache_sentinel_service}/{cache_sentinel_db}"
    )
if "vcache" in CACHES["default"]["BACKEND"]:
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"

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

AUTH_USER_MODEL = "users.User"
ACCOUNT_ADAPTER = "glitchtip.adapters.CustomDefaultAccountAdapter"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
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
    # Needed to login by username in Django admin, regardless of `allauth`
    "django.contrib.auth.backends.ModelBackend",
    # `allauth` specific authentication methods, such as login by e-mail
    "allauth.account.auth_backends.AuthenticationBackend",
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
I_PAID_FOR_GLITCHTIP = env.bool(
    "I_PAID_FOR_GLITCHTIP", bool(GLITCHTIP_LICENSE_KEY)
)

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
CACHE_IS_VALKEY = "valkey" in CACHES["default"]["BACKEND"]

warnings.filterwarnings(
    "ignore", message="No directory at", module="django.core.handlers.base"
)
