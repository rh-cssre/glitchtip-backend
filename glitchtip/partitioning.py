from dateutil.relativedelta import relativedelta

from apps.issue_events.models import IssueEvent
from psqlextra.partitioning import (
    PostgresCurrentTimePartitioningStrategy,
    PostgresPartitioningManager,
    PostgresTimePartitionSize,
)
from psqlextra.partitioning.config import PostgresPartitioningConfig

manager = PostgresPartitioningManager(
    [
        PostgresPartitioningConfig(
            model=IssueEvent,
            strategy=PostgresCurrentTimePartitioningStrategy(
                size=PostgresTimePartitionSize(days=1),
                count=3,
                max_age=relativedelta(months=3),
            ),
        ),
    ]
)
