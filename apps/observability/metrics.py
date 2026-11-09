from django.core.cache import cache
from django.db.models import Count
from prometheus_client import Gauge

from apps.observability.constants import OBSERVABILITY_ORG_CACHE_KEY
from apps.organizations_ext.models import Organization

organizations_metric = Gauge("glitchtip_organizations", "Number of organizations")
projects_metric = Gauge("glitchtip_projects_total", "Total number of projects")


async def update_metrics():
    """Update and cache the organization and project metrics"""
    counts = await cache.aget(OBSERVABILITY_ORG_CACHE_KEY)
    if counts is None:
        result = await Organization.objects.aaggregate(
            org_count=Count("id"), project_count=Count("projects")
        )
        counts = {
            "org_count": result["org_count"] or 0,
            "project_count": result["project_count"] or 0,
        }
        await cache.aset(OBSERVABILITY_ORG_CACHE_KEY, counts, 60 * 60)

    organizations_metric.set(counts["org_count"])
    projects_metric.set(counts["project_count"])
