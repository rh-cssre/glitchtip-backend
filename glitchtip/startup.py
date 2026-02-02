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


def get_startup_info() -> dict:
    """Gather startup configuration info."""
    from django.conf import settings

    # Determine mode
    embed_worker = os.environ.get("GLITCHTIP_EMBED_WORKER") == "true"
    if embed_worker:
        mode = "All-in-one (embedded worker)"
    else:
        mode = "Web only"

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
        "mode": mode,
        "backend": backend,
        "retention_days": settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS,
    }


def format_banner(info: dict) -> str:
    """Format the startup banner with logo and info side by side."""
    logo_lines = LOGO.split("\n")

    # Info lines to display next to logo
    info_lines = [
        f"GlitchTip v{info['version']}",
        "───────────────────────────────",
        f"URL:              {info['url']}",
        f"Mode:             {info['mode']}",
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
