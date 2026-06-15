import random
from collections import Counter
from datetime import timedelta

from django.contrib.postgres.search import SearchVector
from django.db import connection
from django.db.models import Value
from django.utils import timezone

from apps.issue_events.models import (
    Issue,
    IssueEvent,
    IssueEventType,
    IssueIndex,
    IssueTag,
    TagKey,
    TagValue,
)
from glitchtip.base_commands import MakeSampleCommand
from glitchtip.partition_manager import UUID7Helper
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
        # Populate the IssueIndex leaf (the hot/queryable projection +
        # full-text store): count/last_seen/level derived from the issue's events
        # (bulk_create skips the post_save signal, so create rows explicitly).
        IssueIndex.objects.bulk_create(
            [
                IssueIndex(
                    issue=issue,
                    organization_id=self.project.organization_id,
                    count=len(issue_events[i]),
                    last_seen=max(e.timestamp for e in issue_events[i]),
                    level=issue_events[i][0].level,
                    fts_document=SearchVector(Value(issue.title)),
                )
                for i, issue in enumerate(issues)
            ],
            ignore_conflicts=True,
        )
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
            # count/last_seen now live on the leaf; derive from the events here.
            event_count = len(events)
            issue_last_seen = max(e.timestamp for e in events)
            tags = events[0].tags
            for tag_key, tag_value in tags.items():
                tag_key_id = tag_keys[tag_key]
                tag_value_id = tag_values[tag_value]
                tag_count = max(int(event_count / 10), 1)
                # Create a few groups of IssueTags over time
                for _ in range(tag_count):
                    # Rather than group to nearest minute, just make it random
                    # To avoid conflicts. Good enough for performance testing.
                    tag_date = issue_last_seen - timedelta(
                        minutes=random.randint(0, 60),
                        seconds=random.randint(0, 60),
                        milliseconds=random.randint(0, 1000),
                        microseconds=random.randint(0, 1000),
                    )
                    issue_tags.append(
                        IssueTag(
                            issue=issue,
                            organization_id=self.project.organization_id,
                            date=tag_date,
                            tag_key_id=tag_key_id,
                            tag_value_id=tag_value_id,
                            count=tag_count,
                        )
                    )

        IssueTag.objects.bulk_create(issue_tags)

        # Populate IssueAggregate (per-issue hourly counts for issues-stats API)
        org_id = self.project.organization_id
        aggregate_data = []
        for i, issue in enumerate(issues):
            hourly_counts: Counter[timezone.datetime] = Counter()
            for event in issue_events[i]:
                hour = event.timestamp.replace(minute=0, second=0, microsecond=0)
                hourly_counts[hour] += 1
            for hour, count in hourly_counts.items():
                aggregate_data.append([hour, org_id, issue.id, count])

        if aggregate_data:
            aggregate_data.sort(key=lambda x: (x[0], x[1], x[2]))
            with connection.cursor() as cursor:
                args_str = ",".join(
                    cursor.mogrify("(%s,%s,%s,%s)", row) for row in aggregate_data
                )
                cursor.execute(
                    "INSERT INTO issue_events_issueaggregate"
                    " (date, organization_id, issue_id, count)"
                    f" VALUES {args_str}"
                    " ON CONFLICT (issue_id, organization_id, date)"
                    " DO UPDATE SET count = issue_events_issueaggregate.count + EXCLUDED.count;"
                )

        self.progress_tick()

    def handle(self, *args, **options):
        super().handle(*args, **options)
        issue_quantity = options["issue_quantity"]
        over_days = options["over_days"]
        self.events_quantity_per = options["events_quantity_per"]

        now = timezone.now()
        start_time = now - timedelta(days=over_days)

        self._ensure_partitions(
            start_time,
            now,
            daily_tables=["issue_events_issueevent"],
            weekly_tables=[
                "issue_events_issueaggregate",
                "issue_events_issuetag",
                "projects_issueeventprojecthourlystatistic",
            ],
        )

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

        all_event_timestamps: list = []
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
            start_time += issue_delta

            events: list[IssueEvent] = []
            timestamp = first_seen
            for _ in range(event_count):
                timestamp += event_delta
                received = timestamp + timezone.timedelta(milliseconds=1)
                events.append(
                    IssueEvent(
                        # UUIDv7 id encodes received time (millisecond precision)
                        id=UUID7Helper.from_datetime(received),
                        level=level,
                        data={
                            "title": title,
                            "sdk": random.choice(SDKS),
                            "culprit": culprit,
                            "exception": random.choice(EXCEPTIONS),
                        },
                        timestamp=timestamp,
                        tags=tags,
                        organization=self.project.organization,
                    )
                )

            issues.append(
                Issue(
                    title=title,
                    culprit=culprit,
                    metadata={"title": title},
                    first_seen=first_seen,
                    project=self.project,
                ),
            )
            issue_events.append(events)
            all_event_timestamps.extend(e.timestamp for e in events)
            if len(issues) > issue_batch_size:
                self.create_events_and_issues(issues, issue_events)
                issues = []
                issue_events = []
        if issues:
            self.create_events_and_issues(issues, issue_events)

        self.upsert_hourly_project_stats(
            "projects_issueeventprojecthourlystatistic", all_event_timestamps
        )

        self.success_message('Successfully created "%s" issues' % issue_quantity)
