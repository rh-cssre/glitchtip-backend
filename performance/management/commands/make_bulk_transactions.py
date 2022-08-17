from django.core.management.base import BaseCommand
from django.utils import timezone
from model_bakery import baker
from model_bakery.random_gen import gen_json, gen_slug

from organizations_ext.models import Organization
from performance.models import TransactionEvent
from performance.test_data import generate_fake_transaction_event
from projects.models import Project

baker.generators.add("organizations.fields.SlugField", gen_slug)
baker.generators.add("django.db.models.JSONField", gen_json)


class Command(BaseCommand):
    help = (
        "Create a large number of transaction events for dev and demonstration purposes"
    )

    def add_arguments(self, parser):
        parser.add_argument("quantity", nargs="?", type=int, default=10000)

    def handle(self, *args, **options):
        organization = Organization.objects.first()
        if not organization:
            organization = baker.make("organizations_ext.Organization")
        project = Project.objects.filter(organization=organization).first()
        if not project:
            project = baker.make("projects.Project", organization=organization)

        quantity = options["quantity"]
        batch_size = 10000

        if quantity < batch_size:
            batches = 1
        else:
            batches = quantity // batch_size

        for _ in range(batches):
            if quantity < batch_size:
                batch_size = quantity
            event_list = []
            for _ in range(batch_size):
                event = generate_fake_transaction_event(project, timezone.now())
                event_list.append(event)
            TransactionEvent.objects.bulk_create(event_list)
            quantity -= batch_size
            self.stdout.write(self.style.NOTICE("."), ending="")

        self.stdout.write(
            self.style.SUCCESS(
                'Successfully created "%s" transaction events' % options["quantity"]
            )
        )
