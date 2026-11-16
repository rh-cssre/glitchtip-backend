"""Guard: a read must execute on the SAME connection as the active transaction.

This catches a subtle, high-severity class of bug that bites any DB layer which
manages connections outside Django's view:

* a Rust (or other) "fused" read path that checks out a *fresh pool connection*
  to materialize rows — it runs outside the current transaction, so it can't see
  uncommitted writes (and under the test runner's outer transaction it would see
  an effectively empty database, or deadlock on a size-1 pool);
* an async read that lands on a *different* async connection than the one that
  did the write — the same divergence django-async-backend can exhibit.

The invariant is simple and engine-agnostic: if you write a row inside a
transaction and then read it back, every read shape must see it. If it doesn't,
the read is on the wrong connection. These tests pass on stock psycopg and are
meant to FAIL loudly for any engine (e.g. a fused gt_rust path) that breaks the
connection/transaction contract.
"""

from django.apps import apps
from django.db import transaction
from django.test import TestCase, TransactionTestCase

MODEL = "organizations_ext.Organization"


class SyncConnectionIdentityTest(TestCase):
    def test_all_read_shapes_see_uncommitted_write(self):
        # TestCase already runs inside a transaction; the nested atomic() makes
        # the intent explicit. The row is uncommitted — only visible on this
        # transaction's own connection.
        model = apps.get_model(MODEL)
        with transaction.atomic():
            org = model.objects.create(name="conn-identity-sync")

            # exists()/filter — the cheapest read
            self.assertTrue(
                model.objects.filter(pk=org.pk).exists(),
                "filter().exists() ran on a different connection",
            )
            # values_list (FlatValuesListIterable)
            self.assertIn(
                org.pk,
                list(model.objects.values_list("pk", flat=True)),
                "values_list() ran on a different connection",
            )
            # values (ValuesIterable) — the dict materialization path
            self.assertIn(
                org.pk,
                [d["id"] for d in model.objects.values("id")],
                "values() ran on a different connection",
            )
            # full model instances (ModelIterable) — the instance path
            self.assertIn(
                org.pk,
                [o.pk for o in model.objects.all()],
                "list(qs) ran on a different connection",
            )


class AsyncConnectionIdentityTest(TransactionTestCase):
    """Async twin: a write and the reads that follow it inside one async
    transaction must share a connection. TransactionTestCase (no outer wrapping
    transaction) so async_atomic owns the transaction; we roll back explicitly.
    """

    def test_async_reads_see_uncommitted_write(self):
        from asgiref.sync import async_to_sync
        from django.apps import apps

        model = apps.get_model(MODEL)

        async def body():
            from django_async_backend.db.transaction import async_atomic

            sentinel = {}
            async with async_atomic():
                org = await model.objects.acreate(name="conn-identity-async")
                sentinel["exists"] = await model.objects.filter(pk=org.pk).aexists()
                sentinel["values_list"] = org.pk in [
                    pk async for pk in model.objects.values_list("pk", flat=True)
                ]
                sentinel["values"] = org.pk in [
                    d["id"] async for d in model.objects.values("id")
                ]
                sentinel["instances"] = org.pk in [
                    o.pk async for o in model.objects.all()
                ]
                await model.objects.filter(pk=org.pk).adelete()
            return sentinel

        result = async_to_sync(body)()
        for shape, ok in result.items():
            self.assertTrue(ok, f"async {shape} ran on a different connection")
