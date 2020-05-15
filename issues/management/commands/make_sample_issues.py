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
        parser.add_argument("quantity", nargs="?", type=int)
        parser.add_argument(
            "--only-real", action="store_true", help="Only include real sample events",
        )
        parser.add_argument(
            "--only-fake",
            action="store_true",
            help="Only include faked generated events",
        )

    def generate_real_event(self, project):
        """ Generate an event based on real sample data """
        data = event_generator.generate_random_event()
        serializer = EventStoreAPIView().get_serializer_class(data)(data=data)
        serializer.is_valid()
        serializer.create(project.id, serializer.data)

    def handle(self, *args, **options):
        project = Project.objects.first()
        if not project:
            project = baker.make("projects.Project")
        if options["quantity"] is None:
            options["quantity"] = 1
        quantity = options["quantity"]

        only_real = options["only_real"]
        only_fake = options["only_fake"]

        if only_real:
            for _ in range(quantity):
                self.generate_real_event(project)
        elif only_fake:
            baker.make("issues.Event", issue__project=project, _quantity=quantity)
        else:
            for _ in range(quantity):
                if random.choice([0, 1]):
                    baker.make("issues.Event", issue__project=project)
                else:
                    self.generate_real_event(project)

        self.stdout.write(
            self.style.SUCCESS('Successfully created "%s" events' % quantity)
        )
