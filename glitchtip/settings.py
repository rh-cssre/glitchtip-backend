"""
Django settings for glitchtip project.

Generated by 'django-admin startproject' using Django 3.0rc1.

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
from django.core.exceptions import ImproperlyConfigured
from sentry_sdk.integrations.django import DjangoIntegration
from whitenoise.storage import CompressedManifestStaticFilesStorage

env = environ.Env(
    ALLOWED_HOSTS=(list, ["*"]),
    DEFAULT_FILE_STORAGE=(str, None),
    GS_BUCKET_NAME=(str, None),
    AWS_ACCESS_KEY_ID=(str, None),
    AWS_SECRET_ACCESS_KEY=(str, None),
    AWS_STORAGE_BUCKET_NAME=(str, None),
    AWS_S3_ENDPOINT_URL=(str, None),
    AWS_LOCATION=(str, ""),
    DEBUG=(bool, False),
    DEBUG_TOOLBAR=(bool, False),
    STATIC_URL=(str, "/"),
    STATICFILES_STORAGE=(
        str,
        "glitchtip.settings.NoSourceMapsStorage",
    ),
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

# Events and associated data older than this will be deleted from the database
GLITCHTIP_MAX_EVENT_LIFE_DAYS = env.int("GLITCHTIP_MAX_EVENT_LIFE_DAYS", default=90)

# For development purposes only, prints out inbound event store json
EVENT_STORE_DEBUG = env.bool("EVENT_STORE_DEBUG", False)

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/dev/howto/static-files/
STATIC_URL = "/static/"

# GlitchTip can track GlitchTip's own errors.
# If enabling this, use a different server to avoid infinite loops.
def before_send(event, hint):
    """Don't log django.DisallowedHost errors in Sentry."""
    if "log_record" in hint:
        if hint["log_record"].name == "django.security.DisallowedHost":
            return None

    return event


SENTRY_DSN = env.str("SENTRY_DSN", None)
# Optionally allow a different DSN for the frontend
SENTRY_FRONTEND_DSN = env.str("SENTRY_FRONTEND_DSN", SENTRY_DSN)
# Set traces_sample_rate to 1.0 to capture 100%. Recommended to keep this value low.
SENTRY_TRACES_SAMPLE_RATE = env.float("SENTRY_TRACES_SAMPLE_RATE", 0.1)

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
        traces_sample_rate=SENTRY_TRACES_SAMPLE_RATE,
        traces_sampler=traces_sampler,
    )


def show_toolbar(request):
    return env("DEBUG_TOOLBAR")


DEBUG_TOOLBAR_CONFIG = {"SHOW_TOOLBAR_CALLBACK": show_toolbar}
DEBUG_TOOLBAR_PANELS = [
    "debug_toolbar.panels.versions.VersionsPanel",
    "debug_toolbar.panels.timer.TimerPanel",
    "debug_toolbar.panels.settings.SettingsPanel",
    "debug_toolbar.panels.headers.HeadersPanel",
    "debug_toolbar.panels.request.RequestPanel",
    "debug_toolbar.panels.sql.SQLPanel",
]

# Application definition

INSTALLED_APPS = [
    "django_rest_mfa.mfa_admin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    "django.contrib.postgres",
    "django_prometheus",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.socialaccount.providers.digitalocean",
    "allauth.socialaccount.providers.gitea",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.gitlab",
    "allauth.socialaccount.providers.google",
    "allauth.socialaccount.providers.microsoft",
    "allauth.socialaccount.providers.nextcloud",
    "allauth.socialaccount.providers.keycloak",
    "anymail",
    "corsheaders",
    "django_celery_results",
    "django_filters",
    "django_extensions",
    "django_rest_mfa",
    "debug_toolbar",
    "rest_framework",
    "drf_yasg",
    "dj_rest_auth",
    "dj_rest_auth.registration",
    "storages",
    "glitchtip",
    "alerts",
    "api_tokens",
    "environments",
    "files",
    "organizations_ext",
    "events",
    "issues",
    "users",
    "user_reports",
    "glitchtip.uptime",
    "performance",
    "projects",
    "teams",
    "releases",
    "difs",
]

# Ensure no one uses runsslserver in production
if SECRET_KEY == "change_me" and DEBUG is True:
    INSTALLED_APPS += ["sslserver"]

ENABLE_OBSERVABILITY_API = env("ENABLE_OBSERVABILITY_API")
# Workaround https://github.com/korfuri/django-prometheus/issues/34
PROMETHEUS_EXPORT_MIGRATIONS = False

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "csp.middleware.CSPMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "debug_toolbar.middleware.DebugToolbarMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "sentry.middleware.proxy.DecompressBodyMiddleware",
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
]

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
# GlitchTip can record it's own errors
CSP_CONNECT_SRC = env.list(
    "CSP_CONNECT_SRC",
    str,
    ["'self'", "https://*.glitchtip.com", "https://app.chatwoot.com"],
)
# Needed for Analytics and Stripe for SaaS use cases. Both are disabled by default.
CSP_SCRIPT_SRC = env.list(
    "CSP_SCRIPT_SRC",
    str,
    ["'self'", "https://*.glitchtip.com", "https://js.stripe.com"],
)
CSP_IMG_SRC = env.list("CSP_IMG_SRC", str, ["'self'"])
CSP_FRAME_SRC = env.list("CSP_FRAME_SRC", str, ["'self'", "https://js.stripe.com"])
# Consider tracking CSP reports with GlitchTip itself
CSP_REPORT_URI = env.tuple("CSP_REPORT_URI", str, None)
CSP_REPORT_ONLY = env.bool("CSP_REPORT_ONLY", False)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", 0)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", False)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", False)
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", False)
SESSION_COOKIE_SAMESITE = env.str("SESSION_COOKIE_SAMESITE", "Lax")

DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", "webmaster@localhost")
ANYMAIL = {
    "MAILGUN_API_KEY": env.str("MAILGUN_API_KEY", None),
    "MAILGUN_SENDER_DOMAIN": env.str("MAILGUN_SENDER_DOMAIN", None),
    "MAILGUN_API_URL": env.str("MAILGUN_API_URL", "https://api.mailgun.net/v3"),
    "SENDGRID_API_KEY": env.str("SENDGRID_API_KEY", None),
}

ACCOUNT_EMAIL_SUBJECT_PREFIX = ""

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
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env.str("DATABASE_NAME", "postgres"),
        "USER": env.str("DATABASE_USER", "postgres"),
        "PASSWORD": DATABASE_PASSWORD,
        "HOST": DATABASE_HOST,
        "PORT": env.str("DATABASE_PORT", "5432"),
    }

DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

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
CELERY_BROKER_URL = REDIS_URL
CELERY_BROKER_TRANSPORT_OPTIONS = {
    "fanout_prefix": True,
    "fanout_patterns": True,
}
CELERY_RESULT_BACKEND = "django-db"
CELERY_CACHE_BACKEND = "django-cache"
CELERY_BEAT_SCHEDULE = {
    "send-alert-notifications": {
        "task": "alerts.tasks.process_event_alerts",
        "schedule": 60,
    },
    "cleanup-old-events": {
        "task": "issues.tasks.cleanup_old_events",
        "schedule": crontab(hour=6, minute=1),
    },
    "cleanup-old-transaction-events": {
        "task": "performance.tasks.cleanup_old_transaction_events",
        "schedule": crontab(hour=6, minute=10),
    },
    "cleanup-old-monitor-checks": {
        "task": "glitchtip.uptime.tasks.cleanup_old_monitor_checks",
        "schedule": crontab(hour=6, minute=20),
    },
    "uptime-dispatch-checks": {
        "task": "glitchtip.uptime.tasks.dispatch_checks",
        "schedule": timedelta(seconds=30),
    },
}
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

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

USE_L10N = True

USE_TZ = True

SITE_ID = 1

if env("DEFAULT_FILE_STORAGE"):
    DEFAULT_FILE_STORAGE = env("DEFAULT_FILE_STORAGE")
GS_BUCKET_NAME = env("GS_BUCKET_NAME")

AWS_ACCESS_KEY_ID = env("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = env("AWS_SECRET_ACCESS_KEY")
AWS_STORAGE_BUCKET_NAME = env("AWS_STORAGE_BUCKET_NAME")
AWS_S3_ENDPOINT_URL = env("AWS_S3_ENDPOINT_URL")
AWS_LOCATION = env("AWS_LOCATION")

if AWS_S3_ENDPOINT_URL:
    MEDIA_URL = env.str(
        "MEDIA_URL", "https://%s/%s/" % (AWS_S3_ENDPOINT_URL, AWS_LOCATION)
    )
    DEFAULT_FILE_STORAGE = "storages.backends.s3boto3.S3Boto3Storage"
else:
    MEDIA_URL = "media/"
MEDIA_ROOT = env.str("MEDIA_ROOT", "")

STATICFILES_DIRS = [
    "assets",
    "dist",
]
STATIC_ROOT = path("static/")
STATICFILES_STORAGE = env("STATICFILES_STORAGE")
EMAIL_BACKEND = env.str(
    "EMAIL_BACKEND", default="django.core.mail.backends.smtp.EmailBackend"
)
if os.getenv("EMAIL_URL"):
    EMAIL_CONFIG = env.email_url("EMAIL_URL")
    vars().update(EMAIL_CONFIG)

AUTH_USER_MODEL = "users.User"
ACCOUNT_AUTHENTICATION_METHOD = "email"
ACCOUNT_EMAIL_REQUIRED = True
ACCOUNT_USERNAME_REQUIRED = False
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_ADAPTER = "glitchtip.social.MFAAccountAdapter"
INVITATION_BACKEND = "organizations_ext.invitation_backend.InvitationBackend"
SOCIALACCOUNT_PROVIDERS = {}
GITLAB_URL = env.url("SOCIALACCOUNT_PROVIDERS_gitlab_GITLAB_URL", None)
if GITLAB_URL:
    SOCIALACCOUNT_PROVIDERS["gitlab"] = {"GITLAB_URL": GITLAB_URL.geturl()}
GITEA_URL = env.url("SOCIALACCOUNT_PROVIDERS_gitea_GITEA_URL", None)
if GITEA_URL:
    SOCIALACCOUNT_PROVIDERS["gitea"] = {"GITEA_URL": GITEA_URL.geturl()}
NEXTCLOUD_URL = env.url("SOCIALACCOUNT_PROVIDERS_nextcloud_SERVER", None)
if NEXTCLOUD_URL:
    SOCIALACCOUNT_PROVIDERS["nextcloud"] = {"SERVER": NEXTCLOUD_URL.geturl()}
KEYCLOAK_URL = env.url("SOCIALACCOUNT_PROVIDERS_keycloak_KEYCLOAK_URL", None)
if KEYCLOAK_URL:
    alt_url_env = env.url("SOCIALACCOUNT_PROVIDERS_keycloak_KEYCLOAK_URL_ALT", None)

    if alt_url_env:
        alt_url = alt_url_env.geturl()
    else:
        alt_url = None

    SOCIALACCOUNT_PROVIDERS["keycloak"] = {
        "KEYCLOAK_URL": KEYCLOAK_URL.geturl(),
        "KEYCLOAK_REALM": env.str(
            "SOCIALACCOUNT_PROVIDERS_keycloak_KEYCLOAK_REALM", None
        ),
        "KEYCLOAK_URL_ALT": alt_url,
    }

OLD_PASSWORD_FIELD_ENABLED = True
LOGOUT_ON_PASSWORD_CHANGE = False

REST_AUTH_SERIALIZERS = {
    "USER_DETAILS_SERIALIZER": "users.serializers.UserSerializer",
    "TOKEN_SERIALIZER": "users.serializers.NoopTokenSerializer",
    "PASSWORD_RESET_SERIALIZER": "users.serializers.PasswordSetResetSerializer",
}
REST_AUTH_REGISTER_SERIALIZERS = {
    "REGISTER_SERIALIZER": "users.serializers.RegisterSerializer",
}
REST_AUTH_TOKEN_MODEL = None
REST_AUTH_TOKEN_CREATOR = "users.utils.noop_token_creator"

# By default (False) only the first user, superuser, or organization owners may register
# and create an organization. Other users must be invited. Intended for private instances
ENABLE_OPEN_USER_REGISTRATION = env.bool("ENABLE_OPEN_USER_REGISTRATION", False)

AUTHENTICATION_BACKENDS = (
    # Needed to login by username in Django admin, regardless of `allauth`
    "django.contrib.auth.backends.ModelBackend",
    # `allauth` specific authentication methods, such as login by e-mail
    "allauth.account.auth_backends.AuthenticationBackend",
)

DEFAULT_RENDERER_CLASSES = ("rest_framework.renderers.JSONRenderer",)
if DEBUG:
    DEFAULT_RENDERER_CLASSES = DEFAULT_RENDERER_CLASSES + (
        "rest_framework.renderers.BrowsableAPIRenderer",
    )

REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "glitchtip.pagination.LinkHeaderPagination",
    "PAGE_SIZE": 50,
    "ORDERING_PARAM": "sort",
    "DEFAULT_FILTER_BACKENDS": ("django_filters.rest_framework.DjangoFilterBackend",),
    "DEFAULT_RENDERER_CLASSES": DEFAULT_RENDERER_CLASSES,
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "glitchtip.authentication.BearerTokenAuthentication",
    ],
    "DEFAULT_THROTTLE_RATES": {"anon": "100/minute"},
}

DRF_YASG_EXCLUDE_VIEWS = [
    "users.views.SocialAccountDisconnectView",
]
SWAGGER_SETTINGS = {
    "DEFAULT_AUTO_SCHEMA_CLASS": "glitchtip.yasg.SquadSwaggerAutoSchema",
}


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
    """Gets an organization instance from the id passed through ``request``"""
    user = request.user
    if user:
        return user.organizations_ext_organization.filter(
            owner__organization_user__user=user
        ).first()


# Set to track activity with Plausible
PLAUSIBLE_URL = env.str("PLAUSIBLE_URL", default=None)
PLAUSIBLE_DOMAIN = env.str("PLAUSIBLE_DOMAIN", default=None)

# Set to chatwoot website token to enable live help widget. Assumes app.chatwoot.com.
CHATWOOT_WEBSITE_TOKEN = env.str("CHATWOOT_WEBSITE_TOKEN", None)

# Is running unit test
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"

# See https://liberapay.com/GlitchTip/donate - suggested self-host donation is $5/month/user.
# Support plans available. Email info@burkesoftware.com for more info.
I_PAID_FOR_GLITCHTIP = env.bool("I_PAID_FOR_GLITCHTIP", False)

# Max events per month for free tier
BILLING_FREE_TIER_EVENTS = env.int("BILLING_FREE_TIER_EVENTS", 1000)
DJSTRIPE_SUBSCRIBER_MODEL = "organizations_ext.Organization"
DJSTRIPE_SUBSCRIBER_MODEL_REQUEST_CALLBACK = organization_request_callback
DJSTRIPE_USE_NATIVE_JSONFIELD = True
DJSTRIPE_FOREIGN_KEY_TO_FIELD = "djstripe_id"
STRIPE_AUTOMATIC_TAX = env.bool("STRIPE_AUTOMATIC_TAX", False)
BILLING_ENABLED = False
STRIPE_LIVE_MODE = env.bool("STRIPE_LIVE_MODE", False)
if env.str("STRIPE_TEST_PUBLIC_KEY", None) or env.str("STRIPE_LIVE_PUBLIC_KEY", None):
    BILLING_ENABLED = True
    I_PAID_FOR_GLITCHTIP = True
    INSTALLED_APPS.append("djstripe")
    INSTALLED_APPS.append("djstripe_ext")
    STRIPE_TEST_PUBLIC_KEY = env.str("STRIPE_TEST_PUBLIC_KEY", None)
    STRIPE_TEST_SECRET_KEY = env.str("STRIPE_TEST_SECRET_KEY", None)
    STRIPE_LIVE_PUBLIC_KEY = env.str("STRIPE_LIVE_PUBLIC_KEY", None)
    STRIPE_LIVE_SECRET_KEY = env.str("STRIPE_LIVE_SECRET_KEY", None)
    DJSTRIPE_WEBHOOK_SECRET = env.str("DJSTRIPE_WEBHOOK_SECRET", None)
    CELERY_BEAT_SCHEDULE["set-organization-throttle"] = {
        "task": "organizations_ext.tasks.set_organization_throttle",
        "schedule": crontab(hour=7, minute=1),
    }
    CELERY_BEAT_SCHEDULE["warn-organization-throttle"] = {
        "task": "djstripe_ext.tasks.warn_organization_throttle",
        "schedule": crontab(minute=30),
    }
elif TESTING:
    # Must run tests with djstripe enabled
    BILLING_ENABLED = True
    INSTALLED_APPS.append("djstripe")
    INSTALLED_APPS.append("djstripe_ext")
    STRIPE_TEST_PUBLIC_KEY = "fake"
    STRIPE_TEST_SECRET_KEY = "sk_test_fake"  # nosec
    DJSTRIPE_WEBHOOK_SECRET = "whsec_fake"  # nosec
    logging.disable(logging.WARNING)

CELERY_TASK_ALWAYS_EAGER = env.bool("CELERY_TASK_ALWAYS_EAGER", False)
if TESTING:
    CELERY_TASK_ALWAYS_EAGER = True
    STATICFILES_STORAGE = None
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

MFA_SERVER_NAME = "GlitchTip"
FIDO_SERVER_ID = GLITCHTIP_URL.hostname

# Workaround for error encountered at build time (source: https://github.com/axnsan12/drf-yasg/issues/761#issuecomment-1014530805)
class NoSourceMapsStorage(CompressedManifestStaticFilesStorage):
    patterns = (
        (
            "*.css",
            (
                "(?P<matched>url\\(['\"]{0,1}\\s*(?P<url>.*?)[\"']{0,1}\\))",
                (
                    "(?P<matched>@import\\s*[\"']\\s*(?P<url>.*?)[\"'])",
                    '@import url("%(url)s")',
                ),
            ),
        ),
    )
