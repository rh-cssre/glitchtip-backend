from celery import shared_task
from django.utils.dateparse import parse_datetime

from glitchtip.debounced_celery_task import debounced_task, debounced_wrap

from .models import EventProjectHourlyStatistic, TransactionEventProjectHourlyStatistic


@debounced_task(lambda x, *a, **k: x)
@shared_task
@debounced_wrap
def update_event_project_hourly_statistic(project_id: int, start_time: str):
    EventProjectHourlyStatistic.update(project_id, parse_datetime(start_time))


@debounced_task(lambda x, *a, **k: x)
@shared_task
@debounced_wrap
def update_transaction_event_project_hourly_statistic(project_id: int, start_time: str):
    TransactionEventProjectHourlyStatistic.update(
        project_id, parse_datetime(start_time)
    )
