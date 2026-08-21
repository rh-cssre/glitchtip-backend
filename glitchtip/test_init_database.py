from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections
from django.test import TestCase

from glitchtip.management.commands.init_database import LOCK_ID


class InitDatabaseTestCase(TestCase):
    """``init_database`` serialises deploy-time DDL on an advisory lock.

    Every process runs it on start unless SKIP_INIT is set, so what matters is
    that the steps only ever run while the lock is held and that the lock is
    given back afterwards.
    """

    def setUp(self):
        # A second session: advisory locks are re-entrant within one, so asking
        # the command's own connection whether the lock is held would always
        # say yes.
        self.other = connections.create_connection("default")
        self.addCleanup(self.other.close)

    def _lock_is_held(self) -> bool:
        with self.other.cursor() as cursor:
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_ID])
            acquired = cursor.fetchone()[0]
            if acquired:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_ID])
        return not acquired

    def test_steps_run_under_the_lock_and_release_it(self):
        steps = []

        def record(name, *args, **kwargs):
            steps.append(name)
            self.assertTrue(self._lock_is_held(), f"{name} ran without the lock")

        with patch("glitchtip.management.commands.init_database.call_command", record):
            call_command("init_database")

        self.assertEqual(steps, ["migrate", "maintain_partitions", "createcachetable"])
        self.assertFalse(self._lock_is_held())

    def test_refuses_to_run_alongside_another_process(self):
        with self.other.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(%s)", [LOCK_ID])

        steps = []
        with (
            patch(
                "glitchtip.management.commands.init_database.call_command",
                lambda name, *args, **kwargs: steps.append(name),
            ),
            self.assertRaises(CommandError),
        ):
            call_command("init_database", lock_wait=0)

        # Waiting out the other process is the point; running anyway is not.
        self.assertEqual(steps, [])
