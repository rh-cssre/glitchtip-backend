import json
from timeit import default_timer as timer

from django.conf import settings
from django.core.management.base import BaseCommand
from django.test import RequestFactory
from django.urls import reverse

from apps.organizations_ext.models import Organization
from apps.projects.models import Project
from events.test_data.event_generator import get_seeded_benchmark_events


class Command(BaseCommand):
    help = "Time (for performance) ingesting fake events, including celery processing."

    def handle(self, *args, **options):
        settings.CELERY_TASK_ALWAYS_EAGER = True
        slug = "benchark-test-jfhr3e3jlek8eewmksde"
        name = "Benchark Test Do Not Use"
        organization, _ = Organization.objects.get_or_create(
            slug=slug, defaults={"name": name}
        )
        project, _ = Project.objects.get_or_create(
            slug=slug, defaults={"name": name, "organization": organization}
        )
        project.issue_set.all().delete()
        key = project.projectkey_set.first()
        project_id = project.id
        url = (
            reverse("event_store", args=[project_id])
            + "?glitchtip_key="
            + key.public_key_hex
        )

        factory = RequestFactory()
        quantity = 300
        requests = [
            factory.post(url, data=json.dumps(event), content_type="application/json")
            for event in get_seeded_benchmark_events(quantity=quantity)
        ]
        view = EventStoreAPIView.as_view()  # noqa: F821

        start = timer()
        for request in requests:
            view(request, id=project_id)
        end = timer()
        print(end - start)
        assert Event.objects.filter(issue__project=project).count() == quantity  # noqa: F821
        project.issue_set.all().delete()
