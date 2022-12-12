from datetime import timedelta
from django.utils.timezone import now
from django.conf import settings
from celery import shared_task
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
    while True:
        try:
            empty_issue_delimiter = (
                Issue.objects.filter(event=None)
                .values_list("id", flat=True)[1000:1001]
                .get()
            )
            Issue.objects.filter(event=None, id__lte=empty_issue_delimiter).delete()
        except Issue.DoesNotExist:
            break

    Issue.objects.filter(event=None).delete()


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
