from datetime import timedelta

from celery import shared_task
from django.db import connection
from django.conf import settings
from django.utils.timezone import now

from events.models import Event
from glitchtip.debounced_celery_task import debounced_task, debounced_wrap

from .models import Issue


@shared_task
def cleanup_old_events():
    """Delete older events and associated data"""
    days = settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS
    qs = Event.objects.filter(created__lt=now() - timedelta(days=days))
    # Fast bulk delete - see https://code.djangoproject.com/ticket/9519
    qs._raw_delete(qs.db)

    # Delete ~1k empty issues at a time until less than 1k remain then delete the rest. Avoids memory overload.
    queryset = Issue.objects.filter(event=None).order_by("id")

    while True:
        try:
            empty_issue_delimiter = queryset.values_list("id", flat=True)[
                1000:1001
            ].get()
            queryset.filter(id__lte=empty_issue_delimiter).delete()
        except Issue.DoesNotExist:
            break

    queryset.delete()


@shared_task
def update_search_index_all_issues():
    """Very slow, force reindex of all issues"""
    for issue_pk in Issue.objects.all().values_list("pk", flat=True):
        Issue.update_index(issue_pk)


@debounced_task(lambda x, *a, **k: x)
@shared_task
@debounced_wrap
def update_search_index_issue(issue_id: int):
    """
    Debounced task to update one issue's search index/tags.
    Useful for mitigating excessive DB updates on rapidly recurring issues.
    Usage: update_search_index_issue(args=[issue_id], countdown=10)
    """
    Issue.update_index(issue_id)


@shared_task
def reindex_issues_model():
    """
    The GIN index on the issues table grows indefinitely, it needs reindexed regularly
    """
    with connection.cursor() as cursor:
        cursor.execute("REINDEX TABLE CONCURRENTLY issues_issue")
