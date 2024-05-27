import random
from datetime import timedelta

from django.contrib.postgres.search import SearchVector
from django.db.models import Value
from django.utils import timezone

from apps.issue_events.models import (
    Issue,
    IssueEvent,
    IssueEventType,
    IssueTag,
    TagKey,
    TagValue,
)
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.utils import get_random_string

from .issue_generator import CULPRITS, EXCEPTIONS, SDKS, TITLE_CHOICES, generate_tags


class Command(MakeSampleCommand):
    help = "Create sample issues and events for dev and demonstration purposes"
    events_quantity_per: int

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
        parser.add_argument(
            "--over-days",
            type=int,
            default=1,
            help="Make events received datetime show up over x days",
        )

    def get_events_count(self) -> int:
        if count := self.events_quantity_per:
            return count
        return random.randint(1, 100)

    def create_events_and_issues(
        self, issues: list[Issue], issue_events: list[list[IssueEvent]]
    ):
        issues = Issue.objects.bulk_create(issues)
        # Assign issue to each event
        for i, issue in enumerate(issues):
            events = issue_events[i]
            for event in events:
                event.issue = issue

        flat_events = [x for xs in issue_events for x in xs]
        IssueEvent.objects.bulk_create(flat_events)

        keys = {
            key for issue_event in issue_events for key in issue_event[0].tags.keys()
        }
        values = {
            value
            for issue_event in issue_events
            for value in issue_event[0].tags.values()
        }
        TagKey.objects.bulk_create(
            [TagKey(key=key) for key in keys], ignore_conflicts=True
        )
        TagValue.objects.bulk_create(
            [TagValue(value=value) for value in values], ignore_conflicts=True
        )
        tag_keys = {
            tag["key"]: tag["id"]
            for tag in TagKey.objects.filter(key__in=keys).values()
        }
        tag_values = {
            tag["value"]: tag["id"]
            for tag in TagValue.objects.filter(value__in=values).values()
        }

        issue_tags = []
        for i, issue in enumerate(issues):
            events = issue_events[i]
            tags = events[0].tags
            for tag_key, tag_value in tags.items():
                tag_key_id = tag_keys[tag_key]
                tag_value_id = tag_values[tag_value]
                tag_count = max(int(issue.count / 10), 1)
                # Create a few groups of IssueTags over time
                for _ in range(tag_count):
                    # Rather than group to nearest minute, just make it random
                    # To avoid conflicts. Good enough for performance testing.
                    tag_date = issue.last_seen - timedelta(
                        minutes=random.randint(0, 60),
                        seconds=random.randint(0, 60),
                        milliseconds=random.randint(0, 1000),
                        microseconds=random.randint(0, 1000),
                    )
                    issue_tags.append(
                        IssueTag(
                            issue=issue,
                            date=tag_date,
                            tag_key_id=tag_key_id,
                            tag_value_id=tag_value_id,
                            count=tag_count,
                        )
                    )

        IssueTag.objects.bulk_create(issue_tags)
        self.progress_tick()

    def handle(self, *args, **options):
        super().handle(*args, **options)
        issue_quantity = options["issue_quantity"]
        over_days = options["over_days"]
        self.events_quantity_per = options["events_quantity_per"]

        now = timezone.now()
        start_time = now - timedelta(days=over_days)
        # timedelta between each new issue first_seen
        issue_delta = timedelta(seconds=over_days * 86400 / issue_quantity)
        # timedelta between each event for an issue
        if self.events_quantity_per:
            event_delta = issue_delta / self.events_quantity_per
        else:
            event_delta = issue_delta / 100

        # 10,000 per query is a good target
        average_events_per_issue = (
            self.events_quantity_per if self.events_quantity_per else 50
        )
        # Don't go lower than 1. >10,000 events per issue will perform worse
        issue_batch_size = max(10000 // average_events_per_issue, 1)

        random_tags = {
            get_random_string(): [
                get_random_string() for _ in range(options["tag_values_per_key"])
            ]
            for _ in range(options["tag_keys_per_event"])
        }

        issues: list[Issue] = []
        issue_events: list[list[IssueEvent]] = []
        for _ in range(issue_quantity):
            title = random.choice(TITLE_CHOICES) + " " + get_random_string()
            level = IssueEventType.ERROR
            culprit = random.choice(CULPRITS)
            event_count = self.get_events_count()

            # Include both realistic looking and random tags
            tags = generate_tags() | {
                tag: random.choice(value) for tag, value in random_tags.items()
            }

            first_seen = start_time
            last_seen = first_seen + event_delta * event_count
            start_time += issue_delta

            events: list[IssueEvent] = []
            timestamp = first_seen
            for _ in range(event_count):
                timestamp += event_delta
                received = timestamp + timezone.timedelta(milliseconds=1)
                events.append(
                    IssueEvent(
                        level=level,
                        data={
                            "title": title,
                            "sdk": random.choice(SDKS),
                            "culprit": culprit,
                            "exception": random.choice(EXCEPTIONS),
                        },
                        timestamp=timestamp,
                        received=received,
                        tags=tags,
                    )
                )

            issues.append(
                Issue(
                    title=title,
                    culprit=culprit,
                    level=level,
                    metadata={"title": title},
                    first_seen=first_seen,
                    last_seen=last_seen,
                    project=self.project,
                    search_vector=SearchVector(Value(title)),
                    count=event_count,
                ),
            )
            issue_events.append(events)
            if len(issues) > issue_batch_size:
                self.create_events_and_issues(issues, issue_events)
                issues = []
                issue_events = []
        if issues:
            self.create_events_and_issues(issues, issue_events)

        self.success_message('Successfully created "%s" issues' % issue_quantity)
