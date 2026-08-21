"""Bring the database up to date, safely, from any number of processes at once.

Every GlitchTip process runs this on start unless ``SKIP_INIT`` is set, so it
has to tolerate several of them starting together. Django takes no lock around
``migrate``: two processes racing on a fresh database both try to create
``django_migrations`` and one dies on a duplicate-key error, and two racing
inside a non-atomic migration each execute its DDL against the same schema —
which is only survivable when that migration happens to have been written
idempotently. Either way the loser exits non-zero, which on a rolling deploy
reads as a failed rollout.

So the whole sequence is taken under one Postgres advisory lock. The second
process waits, then finds nothing left to do. The lock is advisory and
session-scoped: a process that dies mid-migration releases it on disconnect,
so a crash can't wedge the next start.

Running migrations as a pre-deploy step and setting ``SKIP_INIT`` is still the
better shape for a large install — it keeps schema changes out of the serving
path entirely — but it is now an optimization rather than a requirement.
"""

import time
from contextlib import contextmanager

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

# Arbitrary, and must stay stable: advisory locks share one 64-bit key space
# per cluster, so this both excludes other GlitchTip processes and stays out of
# another application's way. Changing it stops it excluding anything.
LOCK_ID = 6553634917364001

# Long enough for a real migration on a large install, short enough that a
# process which will never get the lock gives up and lets the container
# restart. Exiting beats serving against a half-migrated schema.
LOCK_WAIT_SECONDS = 900
POLL_SECONDS = 2


class Command(BaseCommand):
    help = "Migrate, maintain partitions and create the cache table, under a lock"

    # The database is the thing being initialised, so system checks that talk
    # to it can't run first — this is what --skip-checks did at the call site.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--lock-wait",
            type=int,
            default=LOCK_WAIT_SECONDS,
            metavar="SECONDS",
            help=(
                "How long to wait for another process's initialization to "
                f"finish before giving up (default {LOCK_WAIT_SECONDS})."
            ),
        )

    def handle(self, *args, **options):
        # Every step here is DDL, so all three go to the maintenance
        # connection, which is the same database as DATABASE_URL when
        # MAINTENANCE_DATABASE_URL isn't set.
        alias = settings.MAINTENANCE_DATABASE_ALIAS
        with self._advisory_lock(connections[alias], options["lock_wait"]):
            call_command("migrate", interactive=False, database=alias, skip_checks=True)
            call_command("maintain_partitions")
            # No-op unless one of CACHES uses the database backend.
            call_command("createcachetable", database=alias)
        self.stdout.write(self.style.SUCCESS("Database initialization complete."))

    @contextmanager
    def _advisory_lock(self, connection, wait_seconds):
        deadline = time.monotonic() + wait_seconds
        announced = False
        while not self._try_lock(connection):
            if time.monotonic() >= deadline:
                raise CommandError(
                    f"Gave up after {wait_seconds}s waiting for another process "
                    "to finish initializing the database. If migrations here "
                    "legitimately take longer, run them as a pre-deploy step "
                    "and start with SKIP_INIT=true."
                )
            if not announced:
                self.stdout.write(
                    "Another process is initializing the database, waiting..."
                )
                announced = True
            time.sleep(POLL_SECONDS)
        try:
            yield
        finally:
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
                if not cursor.fetchone()[0]:
                    # Only reachable if the session was replaced underneath us,
                    # which means the lock was already gone and another process
                    # may have been initializing alongside this one.
                    self.stderr.write(
                        "Initialization lock was already released — the "
                        "database connection was reset during initialization."
                    )

    def _try_lock(self, connection) -> bool:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            return cursor.fetchone()[0]
