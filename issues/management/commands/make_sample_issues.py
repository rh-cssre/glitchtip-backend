import random

from model_bakery import baker

from events.test_data import event_generator
from events.views import EventStoreAPIView
from glitchtip.base_commands import MakeSampleCommand
from projects.models import Project


class Command(MakeSampleCommand):
    help = "Create sample issues and events for dev and demonstration purposes"

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--only-real",
            action="store_true",
            help="Only include real sample events",
        )
        parser.add_argument(
            "--only-fake",
            action="store_true",
            help="Only include faked generated events",
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

    def handle(self, *args, **options):
        super().handle(*args, **options)
        quantity = options["quantity"]

        only_real = options["only_real"]
        only_fake = options["only_fake"]

        if only_real:
            for _ in range(quantity):
                self.generate_real_event(self.project)
        elif only_fake:
            baker.make("events.Event", issue__project=self.project, _quantity=quantity)
        else:
            for _ in range(quantity):
                if random.choice([0, 1]):  # nosec
                    baker.make("events.Event", issue__project=self.project)
                else:
                    self.generate_real_event(self.project)

        self.success_message('Successfully created "%s" events' % quantity)
