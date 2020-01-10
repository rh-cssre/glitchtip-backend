import random
from django.core.management.base import BaseCommand
from model_bakery import baker
from projects.models import Project
from glitchtip.test_utils import generators
from event_store.test_data import event_generator
from event_store.views import EventStoreAPIView


class Command(BaseCommand):
    help = "Create sample issues and events for dev and demonstration purposes"

    def add_arguments(self, parser):
        parser.add_argument("quantity", nargs="+", type=int)
        parser.add_argument(
            "--include_real", action="store_true", help="Include real sample events",
        )

    def handle(self, *args, **options):
        project = Project.objects.first()
        if not project:
            project = baker.make("projects.Project")
        quantity = options["quantity"][0]

        if options["include_real"]:
            for _ in range(quantity):
                if random.choice([0, 1]):
                    baker.make("issues.Event", issue__project=project)
                else:
                    data = event_generator.generate_random_event()
                    serializer = EventStoreAPIView().get_serializer(data)
                    serializer.is_valid()
                    serializer.create(project, serializer.data)
        else:
            baker.make("issues.Event", issue__project=project, _quantity=quantity)

        self.stdout.write(
            self.style.SUCCESS('Successfully created "%s" events' % quantity)
        )

