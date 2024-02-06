import random

from django.contrib.postgres.search import SearchVector
from model_bakery import baker

from events.models import Event
from events.test_data import bulk_event_data, event_generator
from events.views import EventStoreAPIView
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.utils import get_random_string
from issues.models import EventType, Issue
from projects.models import Project

from .issue_generator import CULPRITS, EXCEPTIONS, SDKS, TITLE_CHOICES, generate_tag


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
        parser.add_argument(
            "--tag-keys-per-event", type=int, default=0, help="Extra random tag keys"
        )
        parser.add_argument(
            "--tag-values-per-key", type=int, default=1, help="Extra random tag values"
        )

    def handle(self, *args, **options):
        super().handle(*args, **options)
        issue_quantity = options["issue_quantity"]
        events_quantity_per = options["events_quantity_per"]

        random_tags = {
            get_random_string(): [
                get_random_string() for _ in range(options["tag_values_per_key"])
            ]
            for _ in range(options["tag_keys_per_event"])
        }

        def get_events_count():
            if events_quantity_per:
                return events_quantity_per
            return random.randint(1, 100)

        issues = []
        issue_ids = []
        for _ in range(issue_quantity):
            title = random.choice(TITLE_CHOICES) + " " + get_random_string()
            tags = generate_tag()
            if tags:
                tags = {tag[0]: [tag[1]] for tag in tags}
            else:
                tags = {}
            event_count = get_events_count()
            issues.append(
                Issue(
                    title=title,
                    culprit=random.choice(CULPRITS),
                    level=EventType.ERROR,
                    metadata={"title": title},
                    tags=tags,
                    project=self.project,
                    count=event_count,
                )
            )
            if len(issues) > 10000:
                issues = Issue.objects.bulk_create(issues)
                issue_ids += [issue.pk for issue in issues]
                issues = []
                self.progress_tick()
        if issues:
            issues = Issue.objects.bulk_create(issues)
            issue_ids += [issue.pk for issue in issues]
        issues = Issue.objects.filter(pk__in=issue_ids)
        self.progress_tick()

        events = []
        for issue in issues:
            for _ in range(issue.count):
                data = issue.metadata.copy()
                data["sdk"] = random.choice(SDKS)
                data["culprit"] = issue.culprit
                data["exception"] = random.choice(EXCEPTIONS)
                tags = generate_tag() or {}
                events.append(
                    Event(issue=issue, level=issue.level, data=data, tags=tags)
                )
            if len(events) > 10000:
                Event.objects.bulk_create(events)
                events = []
                self.progress_tick()
        if events:
            Event.objects.bulk_create(events)

        issues.update(search_vector=SearchVector("title"))

        self.success_message('Successfully created "%s" issues' % issue_quantity)
