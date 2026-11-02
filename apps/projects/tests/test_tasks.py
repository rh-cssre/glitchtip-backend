from asgiref.sync import async_to_sync
from django.test import TestCase
from django.utils import timezone
from model_bakery import baker

from apps.issue_events.models import Issue, IssueEvent
from apps.logs.models import LogEvent
from apps.projects.models import Project
from glitchtip.partition_manager import UUID7Helper

from ..tasks import delete_project

_delete_project_sync = async_to_sync(delete_project.func)


class DeleteProjectTaskTestCase(TestCase):
    """Verify delete_project batch-deletes partitioned data before cascade."""

    def test_delete_project_with_issue_events(self):
        """IssueEvents in partitioned tables must be deleted."""
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

        _delete_project_sync(project.id)

        self.assertFalse(Project.objects.filter(id=project.id).exists())
        self.assertFalse(IssueEvent.objects.filter(id=event.id).exists())
        self.assertFalse(Issue.objects.filter(id=issue.id).exists())
        # Org should still exist
        self.assertTrue(org.__class__.objects.filter(id=org.id).exists())

    def test_delete_project_with_log_events(self):
        """LogEvents in partitioned tables must be deleted."""
        org = baker.make("organizations_ext.Organization")
        project = baker.make("projects.Project", organization=org)
        log = LogEvent.objects.create(
            id=UUID7Helper.from_datetime(timezone.now()),
            organization=org,
            project=project,
            level=4,
            body="test log",
            data={},
        )

        _delete_project_sync(project.id)

        self.assertFalse(Project.objects.filter(id=project.id).exists())
        self.assertFalse(LogEvent.objects.filter(id=log.id).exists())
