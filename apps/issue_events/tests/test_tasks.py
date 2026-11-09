from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from ..models import Issue, IssueEvent
from ..tasks import delete_issue_task

_delete_issue_task_sync = async_to_sync(delete_issue_task.func)


class DeleteIssueTaskTestCase(TestCase):
    """Verify delete_issue_task batch-deletes partitioned data before cascade."""

    def test_delete_issue_with_events(self):
        """IssueEvents in partitioned tables must be deleted with the issue."""
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        issue = baker.make("issue_events.Issue", project=project)
        event = IssueEvent.objects.create(
            issue=issue,
            organization=org,
            timestamp=timezone.now(),
            type=0,
            level=4,
            title="Test",
            data={},
            tags={},
        )

        _delete_issue_task_sync([issue.id])

        self.assertFalse(Issue.objects.filter(id=issue.id).exists())
        self.assertFalse(IssueEvent.objects.filter(id=event.id).exists())

    def test_delete_multiple_issues(self):
        """Multiple issues should all be deleted with their events."""
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        issue1 = baker.make("issue_events.Issue", project=project)
        issue2 = baker.make("issue_events.Issue", project=project)
        for issue in [issue1, issue2]:
            IssueEvent.objects.create(
                issue=issue,
                organization=org,
                timestamp=timezone.now(),
                type=0,
                level=4,
                title="Test",
                data={},
                tags={},
            )

        _delete_issue_task_sync([issue1.id, issue2.id])

        self.assertEqual(Issue.objects.filter(project=project).count(), 0)
        self.assertEqual(IssueEvent.objects.filter(organization=org).count(), 0)
