from django.core.management.base import BaseCommand, CommandError
from model_bakery import baker
from projects.models import Project
from glitchtip.test_utils import generators


class Command(BaseCommand):
    help = 'Create sample issues and events for dev and demonstration purposes'

    def add_arguments(self, parser):
        parser.add_argument('quantity', nargs='+', type=int)

    def handle(self, *args, **options):
        project = Project.objects.first()
        if not project:
            project = baker.make("projects.Project")
        quantity = options["quantity"][0]
        event = baker.make("issues.Event", issue__project=project, _quantity=quantity)
        self.stdout.write(self.style.SUCCESS('Successfully created "%s" events' % quantity))