from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from model_bakery import baker

from apps.alerts.tasks import process_event_alerts
from glitchtip.test_utils.test_case import GlitchTipTestCase


class PartitionPruningTestCase(GlitchTipTestCase):
    def setUp(self):
        super().setUp()
        self.create_user_and_project()
        self.now = timezone.now()

    def test_alert_query_includes_id_filter(self):
        baker.make(
            "alerts.ProjectAlert",
            project=self.project,
            timespan_minutes=10,
            quantity=1,
        )
        issue = baker.make("issue_events.Issue", project=self.project)
        baker.make("issue_events.IssueEvent", issue=issue)

        # Capture queries to verify SQL generation
        with CaptureQueriesContext(connection) as queries:
            process_event_alerts.call()

        target_query = None
        for query in queries:
            sql = query["sql"]
            # Look for the aggregation query on issue_events_issueevent
            if (
                'received" >=' in sql
                and "issue_events_issueevent" in sql
                and "GROUP BY" in sql
            ):
                target_query = sql
                break

        self.assertIsNotNone(
            target_query, "Could not find the target aggregation query"
        )

        # Level 1 Pruning: ID Range
        self.assertIn(
            '"issue_events_issueevent"."id" >=',
            target_query,
            "Query does not contain ID filter for partition pruning",
        )

        # Level 2 Pruning: Organization Hash
        self.assertIn(
            '"issue_events_issueevent"."organization_id" =',
            target_query,
            "Query does not contain organization_id filter for hash partition pruning",
        )

        # Optimization verification: Ensure we are only selecting the ID, not the full object
        self.assertIn(
            'SELECT "issue_events_issue"."id" AS "id" FROM',
            target_query,
            "Query should select only the ID column (values_list optimization)",
        )
        self.assertNotIn(
            'SELECT "issue_events_issue"."title"',
            target_query,
            "Query is selecting unnecessary columns (title), implying full object fetch",
        )
