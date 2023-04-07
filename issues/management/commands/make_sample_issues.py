import random

from model_bakery import baker

from events.models import Event
from events.test_data import bulk_event_data, event_generator
from events.views import EventStoreAPIView
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.utils import get_random_string
from projects.models import Project


class Command(MakeSampleCommand):
    help = "Create sample issues and events for dev and demonstration purposes"

    def add_arguments(self, parser):
        self.add_org_project_arguments(parser)
        parser.add_argument("--issue-quantity", type=int, default=100)
        parser.add_argument(
            "--events-quantity-per",
            type=int,
            help="Defaults to a random amount from 1-100",
        )
        parser.add_argument("--tag-keys-per-event", type=int, default=1)
        parser.add_argument("--tag-values-per-key", type=int, default=1)
        parser.add_argument(
            "--only-real",
            action="store_true",
            help="Only include real sample events. Issue quanity will be the number of generated, real-looking events.",
        )

    def generate_real_event(self, project, unique_issue=False):
        """Generate an event based on real sample data"""
        data = event_generator.generate_random_event(unique_issue)
        project.release_id = None
        project.environment_id = None
        serializer = EventStoreAPIView().get_event_serializer_class(data)(
            data=data, context={"project": project}
        )
        serializer.is_valid()
        serializer.save()

    def generate_issue(self, get_events_count, quantity, tags):
        issues = baker.make_recipe(
            "issues.issue_recipe",
            count=get_events_count,
            project=self.project,
            tags=tags,
            _quantity=quantity,
            _bulk_create=True,
        )
        for i, issue in enumerate(issues):
            if i % 100 == 0:
                self.progress_tick()
            event_list = []
            for _ in range(issue.count):
                event_tags = {
                    name: random.choice(values) for name, values in tags.items()
                }
                event = Event(
                    data=event_generator.make_event_unique(bulk_event_data.large_event),
                    issue=issue,
                    tags=event_tags,
                )
                event_list.append(event)
            Event.objects.bulk_create(event_list)

    def handle(self, *args, **options):
        super().handle(*args, **options)
        issue_quantity = options["issue_quantity"]
        events_quantity_per = options["events_quantity_per"]
        only_real = options["only_real"]

        tags = {
            get_random_string(): [
                get_random_string() for _ in range(options["tag_values_per_key"])
            ]
            for _ in range(options["tag_keys_per_event"])
        }

        def get_events_count():
            if events_quantity_per:
                return events_quantity_per
            return random.randint(1, 100)

        if only_real:
            for i in range(issue_quantity):
                if i % 100 == 0:
                    self.progress_tick()
                self.generate_real_event(self.project)
        else:
            # Chunk issue generation to lessen ram requirements
            for _ in range(int(issue_quantity / self.batch_size)):
                self.generate_issue(get_events_count, self.batch_size, tags)
                self.progress_tick()
            if remainder := issue_quantity % self.batch_size:
                self.generate_issue(get_events_count, remainder, tags)

        self.success_message('Successfully created "%s" issues' % issue_quantity)
