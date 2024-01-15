from model_bakery import baker

from events.models import Event
from events.test_data import bulk_event_data, event_generator
from glitchtip.base_commands import MakeSampleCommand
from issues.models import Issue
from issues.tasks import update_search_index_issue
from organizations_ext.models import Organization
from projects.models import Project
from projects.tasks import update_event_project_hourly_statistic


class Command(MakeSampleCommand):
    help = "Create an issue with a large number of events for dev and demonstration purposes"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument("batch_size", nargs="?", type=int, default=10000)

    def handle(self, *args, **options):
        super().handle(*args, **options)
        issue_data = bulk_event_data.large_event
        title = issue_data.get("title")
        culprit = issue_data.get("culprit")
        metadata = issue_data.get("metadata")
        issue, _ = Issue.objects.get_or_create(
            title=title,
            culprit=culprit,
            metadata=metadata,
            project=self.project,
        )

        quantity = options["quantity"]
        batch_size = options["batch_size"]

        if quantity < batch_size:
            batches = 1
        else:
            batches = quantity // batch_size

        for _ in range(batches):
            if quantity < batch_size:
                batch_size = quantity
            event_list = []
            for _ in range(batch_size):
                event = Event(
                    data=event_generator.make_event_unique(bulk_event_data.large_event),
                    issue=issue,
                )
                event_list.append(event)
            Event.objects.bulk_create(event_list)
            quantity -= batch_size

        update_search_index_issue(args=[issue.pk])
        update_event_project_hourly_statistic(args=[self.project.pk, event.created])

        self.success_message('Successfully created "%s" events' % options["quantity"])
