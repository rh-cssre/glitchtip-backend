# Vulture whitelist — suppress false positives from Django/framework patterns.
# Run: uv run vulture apps/ glitchtip/ vulture_whitelist.py --min-confidence 90

# API query parameter accepted for sentry-cli compatibility (read by Django Ninja
# from the query string, not used in view logic).
sortBy = None  # noqa: F841
