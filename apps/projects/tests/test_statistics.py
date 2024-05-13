from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..models import TransactionEventProjectHourlyStatistic


class ProjectStatisticsTestCase(GlitchTipTestCase):
    def setUp(self):
        self.project = baker.make("projects.Project")

    def test_transaction_event_update_count(self):
        baker.make("performance.TransactionEvent", group__project=self.project)
        TransactionEventProjectHourlyStatistic.update(self.project.pk, timezone.now())
        self.assertTrue(
            TransactionEventProjectHourlyStatistic.objects.filter(
                project=self.project, count=1
            ).exists()
        )
