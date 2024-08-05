from django.core.cache import cache
from django.db.models import Count
from prometheus_client import Counter, Gauge

organizations_metric = Gauge("glitchtip_organizations", "Number of organizations")
projects_metric = Gauge(
    "glitchtip_projects", "Number of projects per organization", ["organization"]
)
issues_counter = Counter(
    "glitchtip_issues",
    "Issue creation counter per project",
    ["project", "organization"],
)

events_counter = Counter(
    "glitchtip_events",
    "Events creation counter per project",
    ["project", "organization", "issue"],
)

OBSERVABILITY_ORG_CACHE_KEY = "observability_org_metrics"


async def compile_metrics():
    """Update and cache the organization and project metrics"""
    from apps.organizations_ext.models import Organization  # avoid circular import

    orgs = cache.get(OBSERVABILITY_ORG_CACHE_KEY)
    if orgs is None:
        orgs = [
            org
            async for org in Organization.objects.annotate(Count("projects"))
            .values("slug", "projects__count")
            .all()
        ]
        cache.set(OBSERVABILITY_ORG_CACHE_KEY, orgs, 60 * 60)

    for org in orgs:
        projects_metric.labels(org["slug"]).set(org["projects__count"])

    organizations_metric.set(len(orgs))


def clear_metrics_cache():
    cache.delete(OBSERVABILITY_ORG_CACHE_KEY)
