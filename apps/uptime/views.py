from django.core.cache import cache
from django.db.models import Q
from django.http import Http404
from django.shortcuts import render
from django.views.decorators.cache import cache_control

from .models import Monitor, StatusPage


@cache_control(public=True, max_age=60)
async def status_page_detail(request, organization, slug):
    qs = StatusPage.objects.filter(
        organization__slug=organization,
        slug=slug,
    )
    user = await request.auser()
    if user.is_authenticated:
        qs = qs.filter(Q(is_public=True) | Q(organization__users=user))
    else:
        qs = qs.filter(is_public=True)

    status_page = await qs.distinct().afirst()
    if status_page is None:
        raise Http404

    cache_key = f"status_page_monitors:{status_page.pk}"
    monitors = await cache.aget(cache_key)
    if monitors is None:
        monitors = [
            m
            async for m in Monitor.objects.with_check_annotations()
            .filter(statuspage=status_page)
            .values("name", "latest_is_up", "last_change")
        ]
        await cache.aset(cache_key, monitors, 60)

    return render(
        request,
        "uptime/statuspage_detail.html",
        {"object": status_page, "statuspage": status_page, "monitors": monitors},
    )
