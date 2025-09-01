from dateutil.relativedelta import relativedelta
from django.conf import settings
from psql_partition.partitioning import (
    PostgresCurrentTimePartitioningStrategy,
    PostgresPartitioningManager,
    PostgresTimePartitionSize,
)
from psql_partition.partitioning.config import PostgresPartitioningConfig

from apps.issue_events.models import IssueAggregate, IssueEvent, IssueTag
from apps.performance.models import TransactionEvent, TransactionGroupAggregate
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    TransactionEventProjectHourlyStatistic,
)
from apps.uptime.models import MonitorCheck

issue_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=7,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS),
)
transaction_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=7,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS),
)
project_stat_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(weeks=1),
    count=4,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS * 4),
)
uptime_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=4,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_UPTIME_CHECK_LIFE_DAYS),
)
issue_stat_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=4,
    max_age=relativedelta(days=14),  # stats support up to 14 days
)

manager_configs = [
    PostgresPartitioningConfig(model=IssueEvent, strategy=issue_strategy),
    PostgresPartitioningConfig(model=IssueTag, strategy=issue_strategy),
    PostgresPartitioningConfig(
        model=IssueEventProjectHourlyStatistic, strategy=project_stat_strategy
    ),
    PostgresPartitioningConfig(
        model=TransactionEventProjectHourlyStatistic, strategy=project_stat_strategy
    ),
    PostgresPartitioningConfig(model=MonitorCheck, strategy=uptime_strategy),
]
if not settings.GLITCHTIP_ADVANCED_PARTITIONING:
    manager_configs += [
        PostgresPartitioningConfig(model=IssueAggregate, strategy=issue_stat_strategy),
        PostgresPartitioningConfig(
            model=TransactionEvent, strategy=transaction_strategy
        ),
        PostgresPartitioningConfig(
            model=TransactionGroupAggregate, strategy=transaction_strategy
        ),
    ]

manager = PostgresPartitioningManager(manager_configs)
