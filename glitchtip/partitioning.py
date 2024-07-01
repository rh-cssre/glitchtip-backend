from dateutil.relativedelta import relativedelta
from django.conf import settings

from apps.issue_events.models import IssueEvent, IssueTag
from apps.projects.models import (
    IssueEventProjectHourlyStatistic,
    TransactionEventProjectHourlyStatistic,
)

# from apps.performance_events import TransactionGroup
from psqlextra.partitioning import (
    PostgresCurrentTimePartitioningStrategy,
    PostgresPartitioningManager,
    PostgresTimePartitionSize,
)
from psqlextra.partitioning.config import PostgresPartitioningConfig

issue_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=7,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS),
)
# transaction_strategy = PostgresCurrentTimePartitioningStrategy(
#     size=PostgresTimePartitionSize(days=1),
#     count=7,
#     max_age=relativedelta(days=settings.GLITCHTIP_MAX_TRANSACTION_EVENT_LIFE_DAYS),
# )
project_stat_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(weeks=1),
    count=4,
    max_age=relativedelta(days=settings.GLITCHTIP_MAX_EVENT_LIFE_DAYS * 4),
)

manager = PostgresPartitioningManager(
    [
        PostgresPartitioningConfig(model=IssueEvent, strategy=issue_strategy),
        PostgresPartitioningConfig(model=IssueTag, strategy=issue_strategy),
        # PostgresPartitioningConfig(
        #     model=TransactionGroup, strategy=transaction_strategy
        # ),
        PostgresPartitioningConfig(
            model=IssueEventProjectHourlyStatistic, strategy=project_stat_strategy
        ),
        PostgresPartitioningConfig(
            model=TransactionEventProjectHourlyStatistic, strategy=project_stat_strategy
        ),
    ]
)
