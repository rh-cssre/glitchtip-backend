"""Async-DB benchmark endpoints.

Two endpoints, both gated on ``ASYNC_PROBE_ENABLED`` and routed through
the :class:`glitchtip.ingest_asgi.IngestDispatcher` minimal-middleware
path so request handling consists almost entirely of the await chain:

``/api/_probe/async/`` — three trivial raw-SQL SELECTs in a row. Pure
synthetic; the cleanest signal for "is the async cursor wired to real
asyncio?", but not representative of real workloads because there is
no Python CPU work between the awaits.

``/api/_probe/realistic/`` — a more representative mix: one fast
SELECT, a small Python CPU burst, an ORM ``aget`` (model construction
through Django's async ORM path), more CPU, then a 10 ms ``pg_sleep``.
Approximates the shape of an authenticated read endpoint where the
handler does a few DB hops with real validation/formatting work
between them. Better target for "does this beat / match psycopg3 +
async-backend in a realistic deployment?".

Both are gated independently of ``DEBUG``/``ENABLE_TEST_API`` because
realistic concurrency benches run with ``DEBUG=False``.
"""

from __future__ import annotations

import hashlib
import json
import os

from django.conf import settings
from django.http import HttpResponse
from django_async_backend.db.models.query import QuerySet as AsyncQuerySet

from apps.projects.models import Project
from apps.shared.async_db import fetchall


def _python_cpu_burn(target_us: int) -> None:
    """Spend roughly ``target_us`` microseconds in pure Python.

    Used in the realistic probe to simulate the validation / response
    formatting / log-line construction that real handlers do between
    awaits. We don't need an exact duration — we just need *something*
    to keep the GIL busy between DB calls so the bench captures the
    cost of GIL-release vs no-release.
    """
    # ~1.5 µs per iteration on a modern x86 box. The actual cost varies
    # with CPU frequency / sibling load, which is fine for a
    # qualitative bench; we don't need wallclock fidelity.
    iters = max(1, int(target_us / 1.5))
    h = hashlib.sha256(os.urandom(16))
    for i in range(iters):
        h.update(i.to_bytes(8, "little"))
    h.hexdigest()


async def async_probe(request):
    """Three trivial raw-SQL SELECTs. Pure async-cursor synthetic."""
    if not getattr(settings, "ASYNC_PROBE_ENABLED", False):
        return HttpResponse(status=404)

    _, now_row = await fetchall("SELECT NOW(), pg_backend_pid()")
    now, pid = now_row[0]

    _, org_row = await fetchall("SELECT count(*) FROM organizations_ext_organization")
    org_count = org_row[0][0]

    _, proj_row = await fetchall("SELECT count(*) FROM projects_project")
    proj_count = proj_row[0][0]

    body = json.dumps(
        {
            "queries": 3,
            "now": now.isoformat(),
            "pid": pid,
            "rows": {"organizations": org_count, "projects": proj_count},
        },
        separators=(",", ":"),
    )
    return HttpResponse(body, content_type="application/json")


async def realistic_probe(request):
    """Realistic mix: raw SQL + Python CPU + ORM + Python CPU + slow query.

    Designed to look like a typical authenticated read endpoint —
    enough Python work between awaits that the GIL-release benefit of
    a Rust driver has somewhere to land, while still bottlenecking on
    the DB awaits when the load is high enough.
    """
    if not getattr(settings, "ASYNC_PROBE_ENABLED", False):
        return HttpResponse(status=404)

    # Step 1: cheap async SQL (auth-style lookup).
    _, now_row = await fetchall("SELECT NOW(), pg_backend_pid()")
    now, pid = now_row[0]

    # Step 2: ~0.5 ms of Python CPU — request validation / hashing.
    _python_cpu_burn(500)

    # Step 3: non-trivial ORM (model construction + field decoding).
    # Use async-backend's QuerySet whose ``aget`` runs through the async
    # cursor end to end. Django's built-in ``Project.objects.aget(...)``
    # always threadpools the sync get(), which would defeat the bench.
    # Project pk=1 is created by ``manage.py bootstrap_dev``; the bench
    # compose entrypoint runs that automatically.
    project = await AsyncQuerySet(model=Project).aget(pk=1)
    project_slug = project.slug

    # Step 4: ~0.5 ms more Python CPU — response shaping / formatting.
    _python_cpu_burn(500)

    # Step 5: server-side 10 ms sleep — simulates a moderate analytical
    # query. This is where asyncio gets the most leverage; long awaits
    # let other tasks run.
    await fetchall("SELECT pg_sleep(0.01)")

    body = json.dumps(
        {
            "kind": "realistic",
            "now": now.isoformat(),
            "pid": pid,
            "project_slug": project_slug,
        },
        separators=(",", ":"),
    )
    return HttpResponse(body, content_type="application/json")
