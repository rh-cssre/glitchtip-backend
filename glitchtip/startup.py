"""
GlitchTip startup banner display.
"""

import logging

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


def print_startup_banner():
    """Print the GlitchTip startup banner."""
    try:
        info = get_startup_info()
        banner = format_banner(info)
        # Print directly to ensure it appears in logs
        print(banner, flush=True)
    except Exception as e:
        # Don't let banner errors prevent startup
        logger.debug(f"Could not print startup banner: {e}")
