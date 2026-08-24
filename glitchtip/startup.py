"""
GlitchTip startup banner display.
"""

import logging
import os

logger = logging.getLogger(__name__)

LOGO = """\
   ██████╗
  ██╔════╝
  ██║  ███╗
  ██║   ██║
  ╚██████╔╝
   ╚═════╝ """


def describe_email(settings) -> str:
    """One-line email transport descriptor for the banner.

    States what's *configured*, never whether it's reachable, and never any
    secret (host/credentials are left out). See settings.EMAIL_ENABLED.
    """
    if not settings.EMAIL_ENABLED:
        return "disabled (no transport configured)"
    backend = settings.EMAIL_BACKEND
    if "anymail" in backend:
        # anymail.backends.<provider>.EmailBackend
        parts = backend.split(".")
        provider = parts[2] if len(parts) > 2 else "anymail"
        return f"Anymail ({provider})"
    for needle, label in (
        ("smtp", "SMTP"),
        ("console", "console"),
        ("filebased", "file"),
        ("locmem", "in-memory"),
        ("dummy", "dummy"),
    ):
        if needle in backend:
            return label
    return "enabled"


def get_startup_info() -> dict:
    """Gather startup configuration info."""
    from django.conf import settings

    # Determine cache/queue backend
    cache_backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if "valkey" in cache_backend.lower() or "redis" in cache_backend.lower():
        backend = "Valkey"
    elif "database" in cache_backend.lower() or "db" in cache_backend.lower():
        backend = "Database"
    else:
        backend = "Local memory"

    return {
        "version": settings.GLITCHTIP_VERSION,
        "url": settings.GLITCHTIP_URL.geturl(),
        "email": describe_email(settings),
        "backend": backend,
        "retention_days": settings.GLITCHTIP_RETENTION_DAYS,
    }


def format_banner(info: dict) -> str:
    """Format the startup banner with logo and info side by side."""
    logo_lines = LOGO.split("\n")

    # Info lines to display next to logo
    info_lines = [
        f"GlitchTip v{info['version']}",
        "───────────────────────────────",
        f"URL:              {info['url']}",
        f"Email:            {info['email']}",
        f"Cache/Queue:      {info['backend']}",
        f"Event retention:  {info['retention_days']} days",
    ]

    # Combine logo and info side by side
    output_lines = []
    max_lines = max(len(logo_lines), len(info_lines))

    for i in range(max_lines):
        logo_part = logo_lines[i] if i < len(logo_lines) else " " * 10
        info_part = info_lines[i] if i < len(info_lines) else ""
        output_lines.append(f"{logo_part}  {info_part}")

    return "\n".join(output_lines)


# Environment variables that no longer do anything. Setting one used to
# change behavior, so staying silent would turn an upgrade into a silent
# behavior swap for the operator who deliberately set it.
#
# A removed name is ignored, never fatal: an install that still sets one keeps
# booting on the default. Where that default is destructive the warning has to
# say so, because the warning is the only thing the operator gets.
RETIRED_ENV_VARS = {
    "GLITCHTIP_RUST_INGEST": (
        "envelope ingest is always served by the Rust pipeline; the variable "
        "is ignored, including when set to false"
    ),
    "USE_ASYNC_SERVER": (
        "WSGI serving is removed; GlitchTip only runs as an ASGI application, "
        "so there is no synchronous mode to switch to"
    ),
    "GLITCHTIP_EMBED_WORKER": (
        "the task worker and scheduler always run in this process; the "
        "variable is ignored, including when set to false"
    ),
    "IS_WORKER": (
        "there is no worker-only mode; every process serves HTTP and consumes "
        "the task queue"
    ),
    "SERVER_ROLE": (
        "there is only one role; bin/start.sh refuses anything but web or "
        "all_in_one rather than starting the wrong thing"
    ),
    "GLITCHTIP_MAX_EVENT_LIFE_DAYS": (
        "renamed to GLITCHTIP_RETENTION_DAYS. This value is ignored, so retention "
        "has fallen back to its default and the next maintenance run will drop "
        "partitions older than that. Set GLITCHTIP_RETENTION_DAYS to keep it"
    ),
    "GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS": (
        "renamed to GLITCHTIP_TRANSACTION_RETENTION_DAYS. This value is ignored, "
        "so transaction retention has fallen back to its default and the next "
        "maintenance run will drop older data"
    ),
    "GLITCHTIP_MAX_UPTIME_CHECK_LIFE_DAYS": (
        "renamed to GLITCHTIP_UPTIME_RETENTION_DAYS. This value is ignored, so "
        "uptime check retention has fallen back to its default and the next "
        "maintenance run will drop older data"
    ),
    "GLITCHTIP_MAX_FILE_LIFE_DAYS": (
        "renamed to GLITCHTIP_FILE_RETENTION_DAYS. This value is ignored, so file "
        "retention has fallen back to its default and the next maintenance run "
        "will delete older files"
    ),
    "GLITCHTIP_LOGS_COLD_DAYS": (
        "renamed to GLITCHTIP_LOG_RETENTION_DAYS. This value is ignored, so log "
        "retention has fallen back to its default and the next maintenance run "
        "will drop older data"
    ),
    "GLITCHTIP_EVENTS_HOT_DAYS": (
        "renamed to GLITCHTIP_EVENT_HOT_DAYS. This value is ignored, so events "
        "are archived to cold storage on the default schedule instead"
    ),
    "GLITCHTIP_LOGS_HOT_DAYS": (
        "renamed to GLITCHTIP_LOG_HOT_DAYS. This value is ignored, so logs are "
        "archived to cold storage on the default schedule instead"
    ),
    "ENABLE_OPEN_USER_REGISTRATION": (
        "renamed to ENABLE_ORGANIZATION_CREATION, which now governs on its own. "
        "It used to override that variable, so a false here no longer keeps "
        "organization creation closed"
    ),
}


# Values whose old meaning is what GlitchTip now does anyway. The published
# Helm chart sets GLITCHTIP_EMBED_WORKER=true and the sample compose file sets
# SERVER_ROLE=all_in_one, and both keep working untouched — warning about them
# would tell an operator their correct config is wrong, and they often cannot
# change it without waiting for a chart release.
RETIRED_ENV_VAR_NOOP_VALUES = {
    "GLITCHTIP_EMBED_WORKER": {"true", "1", "yes", "on"},
    "SERVER_ROLE": {"all_in_one"},
}


def warn_retired_env_vars():
    """Log a warning for each retired environment variable still present."""
    for name, explanation in RETIRED_ENV_VARS.items():
        value = os.environ.get(name)
        if value is None:
            continue
        if value.strip().lower() in RETIRED_ENV_VAR_NOOP_VALUES.get(name, ()):
            continue
        logger.warning("%s is set but no longer has any effect: %s", name, explanation)


def print_startup_banner():
    """Print the GlitchTip startup banner."""
    try:
        info = get_startup_info()
        banner = format_banner(info)
        # Print directly to ensure it appears in logs
        print(banner, flush=True)
        warn_retired_env_vars()
    except Exception as e:
        # Don't let banner errors prevent startup
        logger.debug(f"Could not print startup banner: {e}")
