from dateutil.relativedelta import relativedelta
from psqlextra.partitioning import (
    PostgresCurrentTimePartitioningStrategy,
    PostgresPartitioningManager,
    PostgresTimePartitionSize,
)
from psqlextra.partitioning.config import PostgresPartitioningConfig

from apps.issue_events.models import IssueEvent

manager = PostgresPartitioningManager(
    [
        PostgresPartitioningConfig(
            model=IssueEvent,
            strategy=PostgresCurrentTimePartitioningStrategy(
                size=PostgresTimePartitionSize(weeks=1),
                count=2,
                max_age=relativedelta(months=3),
            ),
        ),
    ]
)
