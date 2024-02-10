from dateutil.relativedelta import relativedelta

from apps.issue_events.models import IssueEvent, IssueTag
from psqlextra.partitioning import (
    PostgresCurrentTimePartitioningStrategy,
    PostgresPartitioningManager,
    PostgresTimePartitionSize,
)
from psqlextra.partitioning.config import PostgresPartitioningConfig

issue_strategy = PostgresCurrentTimePartitioningStrategy(
    size=PostgresTimePartitionSize(days=1),
    count=7,
    max_age=relativedelta(months=3),
)

manager = PostgresPartitioningManager(
    [
        PostgresPartitioningConfig(model=IssueEvent, strategy=issue_strategy),
        PostgresPartitioningConfig(model=IssueTag, strategy=issue_strategy),
    ]
)
