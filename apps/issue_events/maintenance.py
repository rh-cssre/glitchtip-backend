from datetime import timedelta

from django.conf import settings
from django.utils.timezone import now

from .models import Issue


def cleanup_old_issues():
    days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS

    # Delete ~1k empty issues at a time until less than 1k remain then delete the rest. Avoids memory overload.
    queryset = Issue.objects.filter(
        issueevent=None, last_seen__lt=now() - timedelta(days=days)
    ).order_by("id")

    while True:
        try:
            empty_issue_delimiter = queryset.values_list("id", flat=True)[
                1000:1001
            ].get()
            queryset.filter(id__lte=empty_issue_delimiter).delete()
        except Issue.DoesNotExist:
            break

    queryset.delete()
