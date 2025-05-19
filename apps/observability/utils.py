from django.core.cache import cache

from apps.observability.constants import OBSERVABILITY_ORG_CACHE_KEY


def clear_metrics_cache():
    cache.delete(OBSERVABILITY_ORG_CACHE_KEY)
