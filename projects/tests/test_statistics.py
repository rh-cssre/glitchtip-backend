from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import EventProjectHourlyStatistic


class ProjectStatisticsTestCase(GlitchTipTestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")

    def test_update_count(self):
        baker.make("events.Event", issue__project=self.project)
        EventProjectHourlyStatistic.update(self.project.pk, timezone.now())
        self.assertTrue(
            EventProjectHourlyStatistic.objects.filter(
                project=self.project, count=1
            ).exists()
        )
