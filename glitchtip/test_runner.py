from time import time
from unittest.runner import TextTestResult, TextTestRunner

from django.test.runner import DiscoverRunner


class TimedTextTestResult(TextTestResult):
    def __init__(self, *args, **kwargs):
        super(TimedTextTestResult, self).__init__(*args, **kwargs)
        self.clocks = dict()

    def startTest(self, test):
        self.clocks[test] = time()
        super(TextTestResult, self).startTest(test)
        if self.showAll:
            self.stream.write(self.getDescription(test))
            self.stream.write(" ... ")
            self.stream.flush()

    def addSuccess(self, test):
        super(TextTestResult, self).addSuccess(test)
        if self.showAll:
            self.stream.writeln("runtime (%.6fs)" % (time() - self.clocks[test]))
        elif self.dots:
            self.stream.write(".")
            self.stream.flush()


class TimedTextTestRunner(TextTestRunner):
    resultclass = TimedTextTestResult


class TimedTestRunner(DiscoverRunner):
    """To view timings, use ./manage.py test -v 2"""

    test_runner = TimedTextTestRunner

    def setup_databases(self, **kwargs):
        from django.db.models.signals import post_migrate

        def create_test_partitions(sender, **kwargs):
            from django.db import connections

            using = kwargs.get("using")
            if not using:
                return

            # DEFAULT partitions are used here intentionally for tests.
            # In production, DEFAULT partitions must never be used (see AGENTS.md)
            # because they silently absorb rows and block nested RANGE->HASH
            # partition creation. In tests, they're safe: they catch all data
            # regardless of date, and PostgreSQL correctly routes rows to more
            # specific partitions when tests create their own.
            tables = [
                "issue_events_issueevent",
                "issue_events_issueaggregate",
                "issue_events_issuetag",
                "logs_logevent",
                "performance_spanstaging",
                "uptime_monitorcheck",
                "projects_issueeventprojecthourlystatistic",
                "projects_transactioneventprojecthourlystatistic",
                "projects_logprojecthourlystatistic",
                "uptime_uptimecheckhourlystatistic",
            ]

            with connections[using].cursor() as cursor:
                for table in tables:
                    cursor.execute(
                        f"CREATE TABLE IF NOT EXISTS {table}_default "
                        f"PARTITION OF {table} DEFAULT"
                    )

        post_migrate.connect(create_test_partitions)
        try:
            result = super().setup_databases(**kwargs)
        finally:
            post_migrate.disconnect(create_test_partitions)
        return result

    def teardown_databases(self, old_config, **kwargs):
        # ``psycopg.AsyncConnection`` can't close synchronously from
        # ``__del__``, so async-backend connections opened in tasks that
        # have since ended still hold the socket open. Forcibly terminate
        # them so ``DROP DATABASE`` doesn't fail with "being accessed by
        # other users".
        from django.db import connections as sync_connections

        for entry in old_config or []:
            try:
                wrapper = entry[0]
                test_db = wrapper.settings_dict["NAME"]
                with sync_connections[wrapper.alias].cursor() as cursor:
                    cursor.execute(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = %s AND pid <> pg_backend_pid()",
                        [test_db],
                    )
            except Exception:
                # Don't let teardown failures mask the real test result.
                pass
        return super().teardown_databases(old_config, **kwargs)

