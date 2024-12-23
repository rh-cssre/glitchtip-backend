from datetime import timedelta
from uuid import uuid4

from django.conf import settings
from django.utils import timezone
from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTestCase

from ..maintenance import cleanup_old_debug_symbol_bundles
from ..models import DebugSymbolBundle


class SourceCodeMaintenanceTestCase(GlitchTestCase):
    def test_cleanup(self):
        now = timezone.now()
        baker.make("sourcecode.DebugSymbolBundle", last_used=now, debug_id=uuid4())
        baker.make(
            "sourcecode.DebugSymbolBundle",
            last_used=now - timedelta(days=settings.GLITCHTIP_MAX_FILE_LIFE_DAYS),
            debug_id=uuid4()
        )
        cleanup_old_debug_symbol_bundles()
        self.assertEqual(DebugSymbolBundle.objects.count(), 1)
