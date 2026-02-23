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

        def create_test_default_partitions(sender, **kwargs):
            from django.db import connections

            using = kwargs.get("using")
            if not using:
                return

            tables = [
                "issue_events_issueevent",
                "issue_events_issueaggregate",
                "issue_events_issuetag",
                "logs_logevent",
                "performance_transactionevent",
                "performance_transactiongroupaggregate",
                "uptime_monitorcheck",
                "projects_issueeventprojecthourlystatistic",
                "projects_transactioneventprojecthourlystatistic",
                "projects_logprojecthourlystatistic",
            ]

            with connections[using].cursor() as cursor:
                for table in tables:
                    cursor.execute(
                        f"CREATE TABLE IF NOT EXISTS {table}_default PARTITION OF {table} DEFAULT;"
                    )

        post_migrate.connect(create_test_default_partitions)
        try:
            result = super().setup_databases(**kwargs)
        finally:
            post_migrate.disconnect(create_test_default_partitions)
        return result
