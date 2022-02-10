from django.core.management.base import BaseCommand
from model_bakery import baker
from model_bakery.random_gen import gen_json, gen_slug
from projects.models import Project
from organizations_ext.models import Organization
from events.models import Event
from issues.models import Issue
from events.test_data import event_generator
from . import bulk_event_data
from events.views import EventStoreAPIView

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
        project = Project.objects.first()
        if not project:
            project = baker.make("projects.Project", organization=organization)
        if options["quantity"] is None or options["quantity"] > 10000:
            options["quantity"] = 10000
        quantity = options["quantity"]
        batches = quantity // 10000

        first_event = event_generator.make_event_unique(bulk_event_data.large_event)
        serializer = EventStoreAPIView().get_event_serializer_class(first_event)(
                data=first_event, context={"project": project}
            )
        serializer.is_valid()
        # import ipdb; ipdb.set_trace()
        serializer.save()
        issue = Issue.objects.first()

        for _ in range(batches):
            event_list = []
            for _ in range(10000):
                event = Event(
                    data=event_generator.make_event_unique(bulk_event_data.large_event),
                    issue=issue
                )
                event_list.append(event)
            Event.objects.bulk_create(event_list)

        self.stdout.write(
            self.style.SUCCESS('Successfully created "%s" events' % quantity)
        )