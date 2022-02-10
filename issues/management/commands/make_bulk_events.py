from django.core.management.base import BaseCommand
from model_bakery import baker
from model_bakery.random_gen import gen_json, gen_slug

from events.models import Event
from events.test_data import event_generator
from issues.models import Issue
from issues.tasks import update_search_index_issue
from organizations_ext.models import Organization
from projects.models import Project

from events.test_data import bulk_event_data

baker.generators.add("organizations.fields.SlugField", gen_slug)
baker.generators.add("django.db.models.JSONField", gen_json)


class Command(BaseCommand):
    help = "Create an issue with 10,000 or more large events for dev and demonstration purposes"

    def add_arguments(self, parser):
        parser.add_argument("quantity", nargs="?", type=int)

    def handle(self, *args, **options):
        organization = Organization.objects.first()
        if not organization:
            organization = baker.make("organizations_ext.Organization")
        project = Project.objects.filter(organization=organization).first()
        if not project:
            project = baker.make("projects.Project", organization=organization)
        issue_data = bulk_event_data.large_event
        title = issue_data.get("title")
        culprit = issue_data.get("culprit")
        metadata = issue_data.get("metadata")
        issue, issue_created = Issue.objects.get_or_create(
            title=title, culprit=culprit, metadata=metadata, project=project,
        )

        if options["quantity"] is None or options["quantity"] < 10000:
            options["quantity"] = 10000
        quantity = options["quantity"]
        batches = quantity // 10000
        for _ in range(batches):
            event_list = []
            for _ in range(10000):
                event = Event(
                    data=event_generator.make_event_unique(bulk_event_data.large_event),
                    issue=issue,
                )
                event_list.append(event)
            Event.objects.bulk_create(event_list)

        update_search_index_issue(args=[issue.pk, issue_created])
        self.stdout.write(
            self.style.SUCCESS('Successfully created "%s" events' % quantity)
        )
