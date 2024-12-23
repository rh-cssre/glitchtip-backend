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
import sentry_sdk
from celery.schedules import crontab
from corsheaders.defaults import default_headers
from django.conf import global_settings
from django.core.exceptions import ImproperlyConfigured
from django.http import UnreadablePostError
from sentry_sdk.integrations.django import DjangoIntegration

env = environ.FileAwareEnv(
    ALLOWED_HOSTS=(list, ["*"]),
    DEFAULT_FILE_STORAGE=(str, global_settings.STORAGES["default"]["BACKEND"]),
    AWS_ACCESS_KEY_ID=(str, None),
    AWS_SECRET_ACCESS_KEY=(str, None),
    AWS_STORAGE_BUCKET_NAME=(str, None),
    AWS_S3_ENDPOINT_URL=(str, None),
    AWS_LOCATION=(str, ""),
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
    ACCOUNT_RATE_LIMITS = False  # Disable for e2e tests

ALLOWED_HOSTS = env("ALLOWED_HOSTS")
# Necessary for kubernetes health checks
POD_IP = env.str("POD_IP", default=None)
if POD_IP:
    ALLOWED_HOSTS.append(POD_IP)


ENVIRONMENT = env.str("ENVIRONMENT", None)
GLITCHTIP_VERSION = env.str("GLITCHTIP_VERSION", "0.0.0-unknown")

# Used in email and DSN generation. Set to full domain such as https://glitchtip.example.com
default_url = env.str(
    "APP_URL", env.str("GLITCHTIP_DOMAIN", "http://localhost:8000")
)  # DigitalOcean App Platform uses APP_URL
GLITCHTIP_URL = env.url("GLITCHTIP_URL", default_url)
if GLITCHTIP_URL.scheme not in ["http", "https"]:
    raise ImproperlyConfigured("GLITCHTIP_DOMAIN must start with http or https")


# Is running unit test
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

DATA_UPLOAD_MAX_MEMORY_SIZE = 4294967295  # TMP REMOVE THIS
DATA_UPLOAD_MAX_NUMBER_FIELDS = env.int(
    "DATA_UPLOAD_MAX_NUMBER_FIELDS",
    default=global_settings.DATA_UPLOAD_MAX_NUMBER_FIELDS,
)
# Limits size (in bytes) of uncompressed event payloads. Mitigates DOS risk.
GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE = env.int(
    "GLITCHTIP_MAX_UNZIPPED_PAYLOAD_SIZE", global_settings.DATA_UPLOAD_MAX_MEMORY_SIZE
)

# Events and associated data older than this will be deleted from the database
GLITCHTIP_MAX_EVENT_LIFE_DAYS = env.int("GLITCHTIP_MAX_EVENT_LIFE_DAYS", default=90)
GLITCHTIP_MAX_UPTIME_CHECK_LIFE_DAYS = env.int(
    "GLITCHTIP_MAX_UPTIME_CHECK_LIFE_DAYS", default=GLITCHTIP_MAX_EVENT_LIFE_DAYS
)
GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS = env.int(
    "GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS", default=GLITCHTIP_MAX_EVENT_LIFE_DAYS
)
GLITCHTIP_MAX_FILE_LIFE_DAYS = env.int(
    "GLITCHTIP_MAX_EVENT_LIFE_DAYS", default=GLITCHTIP_MAX_EVENT_LIFE_DAYS
)

# Check if a throttle is needed 1 out of every 5000 event requests
GLITCHTIP_THROTTLE_CHECK_INTERVAL = env.int("GLITCHTIP_THROTTLE_CHECK_INTERVAL", 5000)

# Freezes acceptance of new events, for use during db maintenance
MAINTENANCE_EVENT_FREEZE = env.bool("MAINTENANCE_EVENT_FREEZE", False)

# For development purposes only, prints out inbound event store json
EVENT_STORE_DEBUG = env.bool("EVENT_STORE_DEBUG", False)

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/dev/howto/static-files/
STATIC_URL = "/static/"


# GlitchTip can track GlitchTip's own errors.
# If enabling this, use a different server to avoid infinite loops.
def before_send(event, hint):
    """Don't log useless, inactionable errors in Sentry."""
    if "log_record" in hint:
        if hint["log_record"].name == "django.security.DisallowedHost":
            return None
    if "exc_info" in hint:
        _, exc_value, _ = hint["exc_info"]
        if isinstance(exc_value, UnreadablePostError):
            return None

    return event


SENTRY_DSN = env.str("SENTRY_DSN", None)
# Optionally allow a different DSN for the frontend
SENTRY_FRONTEND_DSN = env.str("SENTRY_FRONTEND_DSN", SENTRY_DSN)
# Set sample_rate to 1.0 to capture 100%.
SENTRY_SAMPLE_RATE = env.float("SENTRY_SAMPLE_RATE", 1.0)
# Set traces_sample_rate to 1.0 to capture 100%. Recommended to keep this value low.
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", 0.01)


# Ignore whitenoise served static routes
def traces_sampler(sampling_context):
    if (
        sampling_context.get("wsgi_environ", {})
        .get("PATH_INFO", "")
        .startswith(STATIC_URL)
    ):
        return 0.0
    return SENTRY_TRACES_SAMPLE_RATE


if SENTRY_DSN:
    release = "glitchtip@" + GLITCHTIP_VERSION if GLITCHTIP_VERSION else None
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        before_send=before_send,
        release=release,
        environment=ENVIRONMENT,
        auto_session_tracking=False,
        send_client_reports=False,
        sample_rate=SENTRY_SAMPLE_RATE,
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        traces_sampler=traces_sampler,
    )


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

# Application definition
# Conditionally load to workaround unnecessary memory usage in celery/beat
WEB_INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "ninja",
]


INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.humanize",
    "psqlextra",
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
    "django_rest_mfa",
    "corsheaders",
    "django_extensions",
]
if DEBUG_TOOLBAR:
    INSTALLED_APPS.append("debug_toolbar")
INSTALLED_APPS += [
    "storages",
    "glitchtip",
    "apps.alerts",
    "apps.environments",
    "apps.organizations_ext",
    "events",
    "issues",
    "apps.users",
    "apps.importer",
    "apps.uptime",
    "apps.performance",
    "apps.projects",
    "apps.teams",
    "apps.releases",
    "apps.sourcecode",
    "apps.difs",
    "apps.api_tokens",
    "apps.files",
    "apps.issue_events",
    "apps.event_ingest",
    "import_export",  # Contains import management command, keep under apps.importer
]


IS_CELERY = env.bool("IS_CELERY", False)
if not IS_CELERY:
    INSTALLED_APPS = WEB_INSTALLED_APPS + INSTALLED_APPS

# Ensure no one uses runsslserver in production
if SECRET_KEY == "change_me" and DEBUG is True:
    INSTALLED_APPS += ["sslserver"]

ENABLE_OBSERVABILITY_API = env("ENABLE_OBSERVABILITY_API")
# Workaround https://github.com/korfuri/django-prometheus/issues/34
PROMETHEUS_EXPORT_MIGRATIONS = False
# https://github.com/korfuri/django-prometheus/blob/master/documentation/exports.md#exporting-metrics-in-a-wsgi-application-with-multiple-processes-per-process
if start_port := env.int("METRICS_START_PORT", None):
    PROMETHEUS_METRICS_EXPORT_PORT_RANGE = range(
        start_port, start_port + env.int("UWSGI_WORKERS", 1)
    )

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "csp.middleware.CSPMiddleware",
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
    "sentry.middleware.proxy.DecompressBodyMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]

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
            ],
        },
    },
]

WSGI_APPLICATION = "glitchtip.wsgi.application"

CORS_ORIGIN_ALLOW_ALL = env.bool("CORS_ORIGIN_ALLOW_ALL", True)
CORS_ORIGIN_WHITELIST = env.tuple("CORS_ORIGIN_WHITELIST", str, default=())
CORS_ALLOW_HEADERS = list(default_headers) + [
    "x-sentry-auth",
    "baggage",
    "sentry-trace",
]

BILLING_ENABLED = False
if env.str("STRIPE_TEST_PUBLIC_KEY", None) or env.str("STRIPE_LIVE_PUBLIC_KEY", None):
    BILLING_ENABLED = True

# Set to chatwoot website token to enable live help widget. Assumes app.chatwoot.com.
CHATWOOT_WEBSITE_TOKEN = env.str("CHATWOOT_WEBSITE_TOKEN", None)
CHATWOOT_IDENTITY_TOKEN = env.str("CHATWOOT_IDENTITY_TOKEN", None)

CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", str, [])
SECURE_BROWSER_XSS_FILTER = True
CSP_DEFAULT_SRC = env.list("CSP_DEFAULT_SRC", str, ["'self'"])
CSP_STYLE_SRC = env.list(
    "CSP_STYLE_SRC", str, ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"]
)
CSP_STYLE_SRC_ELEM = env.list(
    "CSP_STYLE_SRC_ELEM",
    str,
    ["'self'", "'unsafe-inline'", "https://fonts.googleapis.com"],
)
CSP_FONT_SRC = env.list(
    "CSP_FONT_SRC", str, ["'self'", "https://fonts.gstatic.com", "data:"]
)
# Redoc requires blob
CSP_WORKER_SRC = env.list("CSP_WORKER_SRC", str, ["'self'", "blob:"])

# Enable Chatwoot only when configured
default_connect_src = ["'self'", "https://*.glitchtip.com"]
if CHATWOOT_WEBSITE_TOKEN:
    default_connect_src.append("https://app.chatwoot.com")
CSP_CONNECT_SRC = env.list("CSP_CONNECT_SRC", str, default_connect_src)

# Enable stripe by default only when configured
stripe_domain = "https://js.stripe.com"
default_script_src = [
    "'self'",
    "https://*.glitchtip.com",
    "'sha256-0Mfn7rrvFFIfp4wc7eyyIWdGty6Fhc4qVG7t12eqtio='",  # Theme picker inline JS
]
default_frame_src = ["'self'"]
if BILLING_ENABLED:
    default_script_src.append(stripe_domain)
    default_frame_src.append(stripe_domain)

CSP_SCRIPT_SRC = env.list("CSP_SCRIPT_SRC", str, default_script_src)
CSP_IMG_SRC = env.list("CSP_IMG_SRC", str, ["'self'"])
CSP_FRAME_SRC = env.list("CSP_FRAME_SRC", str, default_frame_src)
# Consider tracking CSP reports with GlitchTip itself
CSP_REPORT_URI = env.tuple("CSP_REPORT_URI", str, None)
CSP_REPORT_ONLY = env.bool("CSP_REPORT_ONLY", False)
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

DATABASES = {
    "default": env.db(default="postgres://postgres:postgres@postgres:5432/postgres")
}
# Support setting DATABASES in parts in order to get values from the postgresql helm chart
DATABASE_HOST = env.str("DATABASE_HOST", None)
DATABASE_PASSWORD = env.str("DATABASE_PASSWORD", None)
if DATABASE_HOST and DATABASE_PASSWORD:
    DATABASES["default"] = {
        "NAME": env.str("DATABASE_NAME", "postgres"),
        "USER": env.str("DATABASE_USER", "postgres"),
        "PASSWORD": DATABASE_PASSWORD,
        "HOST": DATABASE_HOST,
        "PORT": env.str("DATABASE_PORT", "5432"),
        "CONN_MAX_AGE": env.int("DATABASE_CONN_MAX_AGE", 0),
        "CONN_HEALTH_CHECKS": env.bool("DATABASE_CONN_HEALTH_CHECKS", False),
    }
DATABASES["default"]["ENGINE"] = "psqlextra.backend"
if not DATABASES["default"].get("OPTIONS"):
    DATABASES["default"]["OPTIONS"] = {}
# Enable pool by default, if there is no conn_max_age
if DATABASES["default"].get("CONN_MAX_AGE", 0) == 0:
    if env.bool("DATABASE_POOL", True):
        pool_options = {}
        min_size = env.int("DATABASE_POOL_MIN_SIZE", 2)
        max_size = env.int("DATABASE_POOL_MAX_SIZE", 6)

        if min_size:
            pool_options["min_size"] = min_size
        if max_size:
            pool_options["max_size"] = max_size

        if pool_options:
            DATABASES["default"]["OPTIONS"]["pool"] = pool_options
        else:
            DATABASES["default"]["OPTIONS"]["pool"] = True

PSQLEXTRA_PARTITIONING_MANAGER = "glitchtip.partitioning.manager"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# We need to support both url and broken out host to support helm redis chart
REDIS_HOST = env.str("REDIS_HOST", None)
if REDIS_HOST:
    REDIS_PORT = env.str("REDIS_PORT", "6379")
    REDIS_DATABASE = env.str("REDIS_DATABASE", "0")
    REDIS_PASSWORD = env.str("REDIS_PASSWORD", None)
    if REDIS_PASSWORD:
        REDIS_URL = (
            f"redis://:{REDIS_PASSWORD}@{REDIS_HOST}:{REDIS_PORT}/{REDIS_DATABASE}"
        )
    else:
        REDIS_URL = f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DATABASE}"
else:
    REDIS_URL = env.str("REDIS_URL", "redis://redis:6379/0")
REDIS_RETRY = env.bool("REDIS_RETRY", True)
REDIS_MAX_CONNECTIONS = env.int("REDIS_MAX_CONNECTIONS", 100)
CELERY_BROKER_URL = env.str("CELERY_BROKER_URL", REDIS_URL)
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "fanout_prefix": True,
    "fanout_patterns": True,
    "retry_on_timeout": REDIS_RETRY,
    "max_connections": REDIS_MAX_CONNECTIONS,
}
CELERY_REDIS_RETRY_ON_TIMEOUT = REDIS_RETRY
CELERY_REDIS_MAX_CONNECTIONS = REDIS_MAX_CONNECTIONS
if CELERY_BROKER_URL.startswith("sentinel"):
    CELERY_BROKER_TRANSPORT_OPTIONS["master_name"] = env.str(
        "CELERY_BROKER_MASTER_NAME", "mymaster"
    )
IS_LOAD_TEST = env("IS_LOAD_TEST")
# GlitchTip doesn't require a celery result backend
if IS_LOAD_TEST:
    CELERY_RESULT_BACKEND = REDIS_URL
if socket_timeout := env.int("CELERY_BROKER_SOCKET_TIMEOUT", None):
    CELERY_BROKER_TRANSPORT_OPTIONS["socket_timeout"] = socket_timeout
if broker_sentinel_password := env.str("CELERY_BROKER_SENTINEL_KWARGS_PASSWORD", None):
    CELERY_BROKER_TRANSPORT_OPTIONS["sentinel_kwargs"] = {
        "password": broker_sentinel_password
    }

# Time in seconds to debounce some frequently run tasks
TASK_DEBOUNCE_DELAY = env.int("TASK_DEBOUNCE_DELAY", 30)
UPTIME_CHECK_INTERVAL = 10
ALERT_NOTIFICATION_INTERVAL = env.int("ALERT_NOTIFICATION_INTERVAL", 60)
CELERY_BEAT_SCHEDULE = {
    "send-alert-notifications": {
        "task": "apps.alerts.tasks.process_event_alerts",
        "schedule": ALERT_NOTIFICATION_INTERVAL,
    },
    "perform-maintenance": {
        "task": "glitchtip.tasks.perform_maintenance",
        "schedule": crontab(hour=5, minute=0),
    },
    "uptime-dispatch-checks": {
        "task": "apps.uptime.tasks.dispatch_checks",
        "schedule": UPTIME_CHECK_INTERVAL,
    },
}
# Maximum number of issues send in a single alert payload
MAX_ISSUES_PER_ALERT = env.int("MAX_ISSUES_PER_ALERT", 3)

if os.environ.get("CACHE_URL"):
    CACHES = {
        "default": env.cache(),
    }
else:  # Default to REDIS when unset
    CACHES = {
        "default": {
            "BACKEND": "django_redis.cache.RedisCache",
            "LOCATION": REDIS_URL,
            "PARSER_CLASS": "redis.connection.HiredisParser",
            "OPTIONS": {
                "CONNECTION_POOL_KWARGS": {
                    "retry_on_timeout": REDIS_RETRY,
                    "max_connections": REDIS_MAX_CONNECTIONS,
                }
            },
        }
    }
if cache_sentinel_url := env.str("CACHE_SENTINEL_URL", None):
    try:
        cache_sentinel_host, cache_sentinel_port = cache_sentinel_url.split(":")
        SENTINELS = [(cache_sentinel_host, int(cache_sentinel_port))]
    except ValueError as err:
        raise ImproperlyConfigured(
            "Invalid cache redis sentinel url, format is host:port"
        ) from err
    DJANGO_REDIS_CONNECTION_FACTORY = "django_redis.pool.SentinelConnectionFactory"
    CACHES["default"]["OPTIONS"]["SENTINELS"] = SENTINELS
if cache_sentinel_password := env.str("CACHE_SENTINEL_PASSWORD", None):
    CACHES["default"]["OPTIONS"]["SENTINEL_KWARGS"] = {
        "password": cache_sentinel_password
    }


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

TIME_ZONE = "UTC"

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

AUTH_USER_MODEL = "users.User"
ACCOUNT_ADAPTER = "glitchtip.adapters.CustomDefaultAccountAdapter"
ACCOUNT_AUTHENTICATION_METHOD = "email"
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_REAUTHENTICATION_TIMEOUT = SESSION_COOKIE_AGE  # Disabled for now
LOGIN_REDIRECT_URL = "/"
LOGIN_URL = "/login"
# This config will later default to True and then be removed
USE_NEW_SOCIAL_CALLBACKS = env.bool("USE_NEW_SOCIAL_CALLBACKS", False)
HEADLESS_ONLY = True
HEADLESS_FRONTEND_URLS = {
    "account_signup": "/login",
    "account_reset_password": "/reset-password",
    "account_confirm_email": "/profile/confirm-email/{key}/",
    "account_reset_password_from_key": "/reset-password/set-new-password/{key}",
    "socialaccount_login_error": "/login?socialLoginError=true",
}
MFA_TOTP_ISSUER = GLITCHTIP_URL.hostname
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
    "root": {"handlers": ["console"]},
}

if LOGGING_HANDLER_CLASS is not logging.StreamHandler:
    from celery.signals import after_setup_logger, after_setup_task_logger

    @after_setup_logger.connect
    @after_setup_task_logger.connect
    def setup_celery_logging(logger, **kwargs):
        from django.utils.module_loading import import_string

        handler = import_string(LOGGING_HANDLER_CLASS)

        for h in logger.handlers:
            logger.removeHandler(h)
        logger.addHandler(handler())


def organization_request_callback(request):
    raise ImproperlyConfigured(
        "Organization request callback required by dj-stripe but not used."
    )


# Set to track activity with Plausible
PLAUSIBLE_URL = env.str("PLAUSIBLE_URL", default=None)
PLAUSIBLE_DOMAIN = env.str("PLAUSIBLE_DOMAIN", default=None)

# See https://liberapay.com/GlitchTip/donate - suggested self-host donation is $5/month/user.
# Support plans available. Email info@burkesoftware.com for more info.
I_PAID_FOR_GLITCHTIP = env.bool("I_PAID_FOR_GLITCHTIP", False)

DJSTRIPE_SUBSCRIBER_MODEL = "organizations_ext.Organization"
DJSTRIPE_SUBSCRIBER_MODEL_REQUEST_CALLBACK = organization_request_callback
DJSTRIPE_USE_NATIVE_JSONFIELD = True
DJSTRIPE_FOREIGN_KEY_TO_FIELD = "djstripe_id"
MARKETING_URL = "https://glitchtip.com"
STRIPE_AUTOMATIC_TAX = env.bool("STRIPE_AUTOMATIC_TAX", False)
STRIPE_LIVE_MODE = env.bool("STRIPE_LIVE_MODE", False)
if BILLING_ENABLED:
    I_PAID_FOR_GLITCHTIP = True
    INSTALLED_APPS.append("djstripe")
    INSTALLED_APPS.append("apps.djstripe_ext")
    STRIPE_TEST_PUBLIC_KEY = env.str("STRIPE_TEST_PUBLIC_KEY", None)
    STRIPE_TEST_SECRET_KEY = env.str("STRIPE_TEST_SECRET_KEY", None)
    STRIPE_LIVE_PUBLIC_KEY = env.str("STRIPE_LIVE_PUBLIC_KEY", None)
    STRIPE_LIVE_SECRET_KEY = env.str("STRIPE_LIVE_SECRET_KEY", None)
    DJSTRIPE_WEBHOOK_SECRET = env.str("DJSTRIPE_WEBHOOK_SECRET", None)
    CELERY_BEAT_SCHEDULE["check-all-organizations-throttle"] = {
        "task": "apps.organizations_ext.tasks.check_all_organizations_throttle",
        "schedule": timedelta(hours=4),
    }
elif TESTING:
    # Must run tests with djstripe enabled
    BILLING_ENABLED = True
    INSTALLED_APPS.append("djstripe")
    INSTALLED_APPS.append("apps.djstripe_ext")
    STRIPE_TEST_PUBLIC_KEY = "fake"
    STRIPE_TEST_SECRET_KEY = "sk_test_fake"  # nosec
    DJSTRIPE_WEBHOOK_SECRET = "whsec_fake"  # nosec
    logging.disable(logging.WARNING)

CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", False)
if TESTING:
    TEST_RUNNER = "glitchtip.test_runner.TimedTestRunner"
    # Optimization
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
    DATABASES["default"]["CONN_MAX_AGE"] = None
    DATABASES["default"]["OPTIONS"]["pool"] = False
    CELERY_TASK_ALWAYS_EAGER = True
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
    STORAGES = global_settings.STORAGES
    # https://github.com/evansd/whitenoise/issues/215
    warnings.filterwarnings(
        "ignore", message="No directory at", module="whitenoise.base"
    )
if CELERY_TASK_ALWAYS_EAGER:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        }
    }
CACHE_IS_REDIS = CACHES["default"]["BACKEND"] == "django_redis.cache.RedisCache"

warnings.filterwarnings(
    "ignore", message="No directory at", module="django.core.handlers.base"
)
