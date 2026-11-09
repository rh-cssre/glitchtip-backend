"""
Custom managers for issue_events models.

Provides smart event lookup that can target specific partitions
based on UUID version detection.
"""

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from django.db import models

from glitchtip.partition_manager import UUID7Helper

if TYPE_CHECKING:
    from .models import IssueEvent

logger = logging.getLogger(__name__)


class EventManager(models.Manager):
    """
    Manager for IssueEvent with smart UUID-based partition targeting.

    This manager provides optimized lookups that leverage UUIDv7's
    timestamp encoding to target specific partitions, reducing query time.
    """

    def get_event(self, id_or_event_id: str | UUID) -> "IssueEvent":
        """
        Intelligent event lookup with automatic partition targeting.

        Strategy:
        - UUIDv7 (id): Extract timestamp, add time filter to enable partition pruning
        - UUIDv4 (event_id): Use event_id column with partial index
        - Unknown version: Try both lookups

        Args:
            id_or_event_id: UUID string or UUID object

        Returns:
            IssueEvent instance

        Raises:
            ValueError: If UUID is invalid
            IssueEvent.DoesNotExist: If event not found

        Example:
            # Server-generated ID (UUIDv7) - uses partition pruning
            event = IssueEvent.objects.get_event("019d1234-5678-7abc-def0-123456789abc")

            # Client-provided ID (UUIDv4) - uses event_id index
            event = IssueEvent.objects.get_event("a1b2c3d4-e5f6-4789-abcd-ef0123456789")
        """
        # Convert string to UUID if needed
        if isinstance(id_or_event_id, str):
            try:
                uuid_val = UUID(id_or_event_id)
            except ValueError:
                raise ValueError(f"Invalid UUID format: {id_or_event_id}")
        else:
            uuid_val = id_or_event_id

        # Detect UUID version and optimize query accordingly
        if uuid_val.version == 7:
            # UUIDv7: Direct lookup - the id itself is the partition key
            # PostgreSQL will prune partitions automatically based on the UUID range
            return self.get(id=uuid_val)

        elif uuid_val.version == 4:
            # UUIDv4: This is a client-provided event_id
            # Use the partial index on event_id column
            return self.filter(event_id=uuid_val).get()

        else:
            # Unknown UUID version - try both approaches
            logger.debug(
                f"Unknown UUID version {uuid_val.version}, trying both lookups"
            )

            # Try as server ID first (most common case going forward)
            try:
                return self.get(id=uuid_val)
            except self.model.DoesNotExist:
                pass

            # Try as client event_id
            try:
                return self.filter(event_id=uuid_val).get()
            except self.model.DoesNotExist:
                pass

            # If neither worked, raise the standard DoesNotExist
            raise self.model.DoesNotExist(
                f"{self.model._meta.object_name} matching query does not exist: {uuid_val}"
            )

    def filter_by_time_range(self, start, end):
        """
        Optimize time-range queries with UUID boundaries for partition pruning.

        When querying by time range, this method adds UUID range filters
        that allow PostgreSQL to eliminate irrelevant partitions.

        Args:
            start: Start datetime (inclusive)
            end: End datetime (exclusive)

        Returns:
            Filtered queryset

        Example:
            # Query last 7 days with partition pruning
            events = IssueEvent.objects.filter_by_time_range(
                start=now() - timedelta(days=7),
                end=now()
            )
        """
        qs = self.all()
        # Filter by UUID range only - UUIDv7 encodes timestamp so this is equivalent
        # to time-based filtering and enables PostgreSQL partition pruning
        if start:
            start_uuid = UUID7Helper._uuid7_for_timestamp(start, min_random=True)
            qs = qs.filter(id__gte=start_uuid)
        if end:
            end_uuid = UUID7Helper._uuid7_for_timestamp(end, min_random=True)
            qs = qs.filter(id__lt=end_uuid)

        return qs
