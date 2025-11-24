from django.core.cache import cache
from django.db.models import Count
from prometheus_client import Gauge

from apps.observability.constants import OBSERVABILITY_ORG_CACHE_KEY
from apps.organizations_ext.models import Organization

organizations_metric = Gauge("glitchtip_organizations", "Number of organizations")
projects_metric = Gauge(
    "glitchtip_projects", "Number of projects per organization", ["organization"]
)


async def compile_metrics():
    """Update and cache the organization and project metrics"""

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
