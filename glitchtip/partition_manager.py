"""
Native Python partition management for PostgreSQL nested partitioning.
Replaces pg_partman with idempotent SQL generation.

Supports two partition key types:
- 'uuid7': Range partition by UUIDv7 (for Event tables)
- 'datetime': Range partition by timestamp column (for Aggregate tables)

All tables use nested partitioning: TIME → HASH for optimal cold storage
and multi-tenant query performance.
"""

import logging
import re
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID

from django.conf import settings
from django.db import connection, connections, transaction

logger = logging.getLogger(__name__)


class UUID7Helper:
    """
    Helper for working with UUIDv7 timestamps.

    UUIDv7 format (RFC 9562):
    - 48 bits: Unix timestamp in milliseconds
    - 4 bits: version (0111 = 7)
    - 12 bits: random data
    - 2 bits: variant (10)
    - 62 bits: random data
    """

    @staticmethod
    def from_datetime(dt: datetime | None = None) -> UUID:
        """
        Generate UUIDv7 from datetime.

        Args:
            dt: Datetime to encode (must be timezone-aware). Defaults to now.

        Returns:
            UUIDv7 with encoded timestamp
        """
        import os

        if dt is None:
            dt = datetime.now(timezone.utc)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # Convert datetime to milliseconds since Unix epoch
        timestamp_ms = int(dt.timestamp() * 1000)

        # UUIDv7 format (RFC 9562):
        # - 48 bits: Unix timestamp in milliseconds
        # - 4 bits: version (0111 = 7)
        # - 12 bits: random data
        # - 2 bits: variant (10)
        # - 62 bits: random data

        # Generate random bytes for the random portions
        rand_a = int.from_bytes(os.urandom(2), byteorder="big") & 0x0FFF  # 12 bits
        rand_b = (
            int.from_bytes(os.urandom(8), byteorder="big") & 0x3FFFFFFFFFFFFFFF
        )  # 62 bits

        # Construct the 128-bit UUID integer
        uuid_int = (
            (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
        )

        return UUID(int=uuid_int)

    @staticmethod
    def extract_datetime(uuid_val: UUID) -> datetime:
        """
        Extract datetime from UUIDv7.

        Args:
            uuid_val: UUID to extract timestamp from

        Returns:
            Datetime extracted from UUID (timezone-aware UTC)
        """
        if uuid_val.version != 7:
            raise ValueError(f"UUID must be version 7, got version {uuid_val.version}")

        # Extract first 48 bits (6 bytes) as milliseconds since epoch
        uuid_bytes = uuid_val.bytes
        timestamp_ms = int.from_bytes(uuid_bytes[:6], byteorder="big")

        # Convert to datetime
        timestamp_sec = timestamp_ms / 1000.0
        return datetime.fromtimestamp(timestamp_sec, tz=timezone.utc)

    @staticmethod
    def get_range_for_date(
        start_date: datetime, end_date: datetime
    ) -> tuple[UUID, UUID]:
        """
        Get UUID range that covers a date range.

        This is critical for partition pruning: by filtering on UUID ranges,
        PostgreSQL can eliminate irrelevant partitions from the query plan.

        To ensure deterministic partition bounds, we use min/max UUIDs:
        - start_uuid: timestamp with all random bits set to 0
        - end_uuid: timestamp with all random bits set to 1

        Args:
            start_date: Start of date range
            end_date: End of date range (exclusive)

        Returns:
            Tuple of (start_uuid, end_uuid)
        """
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        # Generate deterministic UUID bounds for partition ranges
        # Use minimum possible UUID (all random bits = 0) for start
        # Use minimum possible UUID (all random bits = 0) for end (the start of the NEXT range)
        # This ensures [start_uuid, end_uuid) ranges are perfectly contiguous with no gaps or overlaps.
        start_uuid = UUID7Helper._uuid7_for_timestamp(start_date, min_random=True)
        end_uuid = UUID7Helper._uuid7_for_timestamp(end_date, min_random=True)

        return start_uuid, end_uuid

    @staticmethod
    def _uuid7_for_timestamp(dt: datetime, min_random: bool = False) -> UUID:
        """
        Generate a deterministic UUIDv7 for a given timestamp.

        Args:
            dt: Datetime to encode
            min_random: If True, use all zeros for random bits (minimum UUID)
                       If False, use all ones for random bits (maximum UUID)

        Returns:
            UUIDv7 with deterministic random bits
        """
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        # Convert datetime to milliseconds since Unix epoch
        # UUIDv7 uses a 48-bit unsigned timestamp, so clamp to [0, 2^48 - 1]
        timestamp_ms = max(0, min(int(dt.timestamp() * 1000), (1 << 48) - 1))

        # Set random bits to either all 0s or all 1s for deterministic bounds
        if min_random:
            rand_a = 0x0000  # 12 bits of zeros
            rand_b = 0x0000000000000000  # 62 bits of zeros
        else:
            rand_a = 0x0FFF  # 12 bits of ones
            rand_b = 0x3FFFFFFFFFFFFFFF  # 62 bits of ones

        # Construct the 128-bit UUID integer
        # [48-bit timestamp][ver=7][12-bit rand][variant=10][62-bit rand]
        uuid_int = (
            (timestamp_ms << 80) | (0x7 << 76) | (rand_a << 64) | (0x2 << 62) | rand_b
        )

        return UUID(int=uuid_int)


class PartitionManager:
    """
    Manages PostgreSQL nested partitions (TIME → HASH).

    This replaces pg_partman with pure Python logic, providing:
    - Idempotent SQL generation (IF NOT EXISTS)
    - Support for both UUIDv7 and DateTime partition keys
    - Configurable hash buckets per time partition
    - Transparent SQL for logging and inspection

    Example usage:
        manager = PartitionManager()

        # Create datetime-based partition for aggregates
        sqls = manager.create_time_partition(
            parent_table="issue_events_issueaggregate",
            partition_name="issue_events_issueaggregate_20250115",
            start_date=datetime(2025, 1, 15, tzinfo=timezone.utc),
            end_date=datetime(2025, 1, 16, tzinfo=timezone.utc),
            hash_buckets=16,
            key_type="datetime",
        )

        # Execute SQL
        with connection.cursor() as cursor:
            for sql in sqls:
                cursor.execute(sql)
    """

    def __init__(self, db_connection=None):
        """
        Initialize partition manager.

        Args:
            db_connection: Optional Django database connection alias (str) or connection object
        """
        if isinstance(db_connection, str):
            self.db_connection = connections[db_connection]
        else:
            self.db_connection = db_connection or connection

    def create_time_partition(
        self,
        parent_table: str,
        partition_name: str,
        start_date: datetime,
        end_date: datetime,
        hash_buckets: int | None = None,
        hash_column: str = "organization_id",
        key_type: Literal["uuid7", "datetime"] = "datetime",
        partition_column: str = "date",
    ) -> list[str]:
        """
        Create a time-range parent partition with HASH sub-partitions.

        This generates SQL for nested partitioning:
        1. Parent time partition (RANGE on date/uuid)
        2. Child hash partitions (HASH on organization_id)

        Hash Buckets:
        - If `hash_buckets` is None, it reads from settings.PARTITION_HASH_BUCKETS (default: 16).
        - If `hash_buckets` is 0, it creates a simple leaf partition (no hash sub-partitioning).
        - If `hash_buckets` > 0, it creates that many sub-partitions.

        Example for datetime mode:
          CREATE TABLE events_2025_01_15 PARTITION OF events
            FOR VALUES FROM ('2025-01-15') TO ('2025-01-16')
            PARTITION BY HASH (organization_id);

          CREATE TABLE events_2025_01_15_h0 PARTITION OF events_2025_01_15
            FOR VALUES WITH (MODULUS 16, REMAINDER 0);
          ... (h1 through h15)

        Example for uuid7 mode:
          CREATE TABLE events_2025_01_15 PARTITION OF events
            FOR VALUES FROM ('018d1234-5678-7000-0000-000000000000')
                        TO ('018d1234-abcd-7fff-ffff-ffffffffffff')
            PARTITION BY HASH (organization_id);

        Args:
            parent_table: Fully qualified parent table name
            partition_name: Name for the time-range partition
            start_date: Start of date range (inclusive)
            end_date: End of date range (exclusive)
            hash_buckets: Number of hash sub-partitions. None=settings default, 0=no hash.
            hash_column: Column to hash on (default: organization_id)
            key_type: 'uuid7' or 'datetime'
            partition_column: Column name for partitioning (e.g., 'id', 'date')

        Returns:
            List of SQL statements to execute
        """
        sqls = []

        if hash_buckets is None:
            # Hash buckets are used to distribute data across multiple tables
            # to avoid hot spots and improve performance.
            hash_buckets = settings.PARTITION_HASH_BUCKETS

        # Ensure timezone-aware datetimes
        if start_date.tzinfo is None:
            start_date = start_date.replace(tzinfo=timezone.utc)
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)

        # Calculate range bounds based on key type
        if key_type == "uuid7":
            start_val, end_val = UUID7Helper.get_range_for_date(start_date, end_date)
            range_from = f"'{start_val}'"
            range_to = f"'{end_val}'"
        else:  # datetime
            # Use ISO format with timezone
            range_from = f"'{start_date.isoformat()}'"
            range_to = f"'{end_date.isoformat()}'"

        # Create parent time partition
        if hash_buckets > 0:
            # Nested Partitioning: TIME -> HASH
            parent_sql = f"""CREATE TABLE IF NOT EXISTS {partition_name} PARTITION OF {parent_table}
FOR VALUES FROM ({range_from}) TO ({range_to})
PARTITION BY HASH ({hash_column});"""
            sqls.append(parent_sql)

            # Create HASH child partitions
            for i in range(hash_buckets):
                child_name = f"{partition_name}_h{i}"
                child_sql = f"""CREATE TABLE IF NOT EXISTS {child_name} PARTITION OF {partition_name}
FOR VALUES WITH (MODULUS {hash_buckets}, REMAINDER {i});"""
                sqls.append(child_sql)
        else:
            # Simple Range Partitioning (Leaf Node)
            parent_sql = f"""CREATE TABLE IF NOT EXISTS {partition_name} PARTITION OF {parent_table}
FOR VALUES FROM ({range_from}) TO ({range_to});"""
            sqls.append(parent_sql)

        return sqls

    def drop_partition(self, partition_name: str) -> str:
        """
        Generate SQL to drop a partition (and all sub-partitions).

        Args:
            partition_name: Name of partition to drop

        Returns:
            SQL statement
        """
        return f"DROP TABLE IF EXISTS {partition_name} CASCADE;"

    def list_partitions(self, parent_table: str) -> list[dict]:
        """
        Query PostgreSQL catalog to list existing partitions.

        Args:
            parent_table: Parent table name

        Returns:
            List of partition metadata dicts
        """
        sql = """
        SELECT
            c.relname AS partition_name,
            pg_get_expr(c.relpartbound, c.oid) AS partition_bounds
        FROM pg_class c
        JOIN pg_inherits i ON c.oid = i.inhrelid
        JOIN pg_class p ON i.inhparent = p.oid
        WHERE p.relname = %s
        ORDER BY c.relname;
        """

        with self.db_connection.cursor() as cursor:
            cursor.execute(
                sql, [parent_table.split(".")[-1]]
            )  # Handle schema-qualified names
            columns = [col[0] for col in cursor.description]
            return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def drop_old_partitions(
        self, parent_table: str, max_days: int, partition_interval_days: int = 1
    ) -> int:
        """
        Identify and drop partitions older than max_days.
        Assumes partition naming convention: parent_table_YYYYMMDD

        A partition is only dropped when its entire range is older than
        max_days — i.e. when (partition_start + interval) < threshold.
        This prevents weekly partitions from being dropped mid-week when
        retention is shorter than 7 days.

        Args:
            parent_table: Parent table name
            max_days: Maximum age of partitions in days
            partition_interval_days: Size of each partition in days (7 for weekly)

        Returns:
            Number of partitions dropped
        """
        from datetime import timedelta

        partitions = self.list_partitions(parent_table)
        threshold_date = datetime.now(timezone.utc).date() - timedelta(days=max_days)
        dropped_count = 0
        interval = timedelta(days=partition_interval_days)

        # Pattern for YYYYMMDD suffix
        pattern = re.compile(r".*_(\d{8})$")

        for p in partitions:
            name = p["partition_name"]
            match = pattern.match(name)
            if match:
                try:
                    date_str = match.group(1)
                    partition_start = datetime.strptime(date_str, "%Y%m%d").date()
                    partition_end = partition_start + interval
                    if partition_end < threshold_date:
                        logger.info(f"Dropping old partition {name}...")
                        sql = self.drop_partition(name)
                        with self.db_connection.cursor() as cursor:
                            cursor.execute(sql)
                        dropped_count += 1
                except ValueError:
                    continue

        return dropped_count

    def get_partition_info(self, partition_name: str) -> dict | None:
        """
        Get metadata about a specific partition.

        Args:
            partition_name: Partition table name

        Returns:
            Dict with partition metadata, or None if not found
        """
        sql = """
        SELECT
            c.relname AS partition_name,
            p.relname AS parent_name,
            pg_get_expr(c.relpartbound, c.oid) AS partition_bounds,
            pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
        FROM pg_class c
        LEFT JOIN pg_inherits i ON c.oid = i.inhrelid
        LEFT JOIN pg_class p ON i.inhparent = p.oid
        WHERE c.relname = %s;
        """

        with self.db_connection.cursor() as cursor:
            cursor.execute(sql, [partition_name])
            row = cursor.fetchone()
            if row:
                columns = [col[0] for col in cursor.description]
                return dict(zip(columns, row))
            return None

    def is_table_partitioned(self, table_name: str) -> bool:
        """
        Check if a table exists and is a partitioned table.

        Args:
            table_name: Table name

        Returns:
            True if table is partitioned, False otherwise
        """
        sql = """
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s
            AND c.relkind = 'p'
        );
        """
        with self.db_connection.cursor() as cursor:
            cursor.execute(sql, [table_name.split(".")[-1]])
            return cursor.fetchone()[0]

    def table_exists(self, table_name: str) -> bool:
        """
        Check if a table exists in the database.

        Args:
            table_name: Table name

        Returns:
            True if table exists
        """
        sql = """
        SELECT EXISTS (
            SELECT 1 FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE c.relname = %s
        );
        """
        with self.db_connection.cursor() as cursor:
            cursor.execute(sql, [table_name.split(".")[-1]])
            return cursor.fetchone()[0]

    def execute_partition_creation(
        self,
        parent_table: str,
        partition_name: str,
        start_date: datetime,
        end_date: datetime,
        hash_buckets: int | None = None,
        hash_column: str = "organization_id",
        key_type: Literal["uuid7", "datetime"] = "datetime",
        partition_column: str = "date",
    ) -> int:
        """
        Generate and execute SQL to create only missing partitions.

        Checks existence before executing DDL to avoid unnecessary
        AccessExclusiveLock on the root table and all siblings — even
        ``CREATE TABLE IF NOT EXISTS … PARTITION OF`` acquires those locks
        before checking existence, which deadlocks with concurrent INSERTs.

        Args:
            Same as create_time_partition()

        Returns:
            Number of SQL statements executed
        """
        if hash_buckets is None:
            hash_buckets = settings.PARTITION_HASH_BUCKETS

        # Check what already exists to avoid unnecessary heavy locks.
        range_exists = self.table_exists(partition_name)
        if range_exists and hash_buckets > 0:
            existing_children = self.list_partitions(partition_name)
        else:
            existing_children = []

        # A hash partition set always tiles the entire keyspace at a single
        # modulus, and that modulus cannot be changed online (no incremental
        # bucket add without detach + rewrite). If a range partition already
        # has hash children at a *different* modulus than the configured
        # bucket count, it is already 100% complete — every row maps somewhere
        # — so attempting to create buckets at the new modulus would overlap
        # the existing ones ("partition ... would overlap partition ..."). In
        # that case the only correct action is to leave the existing set alone.
        existing_modulus = None
        if existing_children:
            moduli = set()
            for child in existing_children:
                bounds = child.get("partition_bounds") or ""
                match = re.search(r"modulus\s+(\d+)", bounds, re.IGNORECASE)
                if match:
                    moduli.add(int(match.group(1)))
            if len(moduli) == 1:
                existing_modulus = moduli.pop()
            elif len(moduli) > 1:
                # Hash siblings disagree on modulus — an inconsistent set we
                # must not touch. Leave it for manual inspection.
                logger.warning(
                    f"Partition {partition_name} has hash children with "
                    f"inconsistent moduli {sorted(moduli)}; leaving as-is"
                )
                return 0
            else:
                # Children exist but none expose a hash modulus — e.g. the
                # range partition was sub-partitioned by RANGE/LIST instead of
                # HASH, or has some other unexpected structure. Creating HASH
                # children here would clash with the existing strategy, so do
                # not touch it. Generating no DDL also avoids a heavy lock.
                logger.warning(
                    f"Partition {partition_name} has children with no hash "
                    f"modulus; leaving as-is (unexpected partition structure)"
                )
                return 0

        if existing_modulus is not None and existing_modulus != hash_buckets:
            # The range partition is already fully tiled at a modulus that
            # differs from the configured bucket count. A hash set always
            # covers the whole keyspace at its modulus, and that modulus
            # cannot be changed online (detach + rewrite required), so do
            # nothing here regardless of how many children are present.
            logger.info(
                f"Partition {partition_name} has a {existing_modulus}-bucket "
                f"hash set; configured count ({hash_buckets}) differs, leaving "
                f"as-is (changing hash modulus requires a rewrite)"
            )
            return 0

        existing_child_names = {p["partition_name"] for p in existing_children}

        sqls = self.create_time_partition(
            parent_table=parent_table,
            partition_name=partition_name,
            start_date=start_date,
            end_date=end_date,
            hash_buckets=hash_buckets,
            hash_column=hash_column,
            key_type=key_type,
            partition_column=partition_column,
        )

        # Filter out DDL for tables that already exist.
        if not range_exists:
            sqls_to_execute = sqls
        elif hash_buckets > 0:
            # Range partition exists — only create missing hash children.
            # Reaching here means existing children (if any) share the
            # configured modulus, so name-based gap filling is correct.
            # sqls[0] is the range partition, sqls[1:] are h0..hN in order.
            sqls_to_execute = [
                sql
                for i, sql in enumerate(sqls[1:])
                if f"{partition_name}_h{i}" not in existing_child_names
            ]
        else:
            sqls_to_execute = []

        if not sqls_to_execute:
            logger.debug(
                f"Partition {partition_name} with {hash_buckets} hash buckets "
                f"already up to date"
            )
            return 0

        with transaction.atomic(using=self.db_connection.alias):
            with self.db_connection.cursor() as cursor:
                for sql in sqls_to_execute:
                    logger.debug(f"Executing partition SQL: {sql[:100]}...")
                    cursor.execute(sql)

        logger.debug(
            f"Verified partition {partition_name} with {hash_buckets} hash buckets "
            f"for date range {start_date.date()} to {end_date.date()}"
        )

        return len(sqls_to_execute)

    def create_partitions_for_date_range(
        self,
        parent_table: str,
        start_date: datetime,
        end_date: datetime,
        partition_interval: str | int = "WEEK",
        hash_buckets: int | None = None,
        hash_column: str = "organization_id",
        key_type: Literal["uuid7", "datetime"] = "datetime",
        partition_column: str = "date",
    ) -> int:
        """
        Create multiple partitions covering a date range.

        Args:
            parent_table: Parent table name
            start_date: Start of range
            end_date: End of range (exclusive)
            partition_interval: 'WEEK' (default), 'DAY', or int (days)
            hash_buckets: Hash sub-partitions per time partition. None=settings default.
            hash_column: Column to hash on
            key_type: 'uuid7' or 'datetime'
            partition_column: Partition key column name

        Returns:
            Total number of NEW partitions created (parent tables).
        """
        from datetime import timedelta

        new_partitions_count = 0
        current_date = start_date

        # Determine interval timedelta
        if isinstance(partition_interval, int):
            interval = timedelta(days=partition_interval)
        elif partition_interval.upper() == "WEEK":
            interval = timedelta(weeks=1)
        elif partition_interval.upper() == "DAY":
            interval = timedelta(days=1)
        else:
            raise ValueError(f"Invalid partition interval: {partition_interval}")

        while current_date < end_date:
            next_date = current_date + interval
            if next_date > end_date:
                next_date = end_date

            # Generate partition name: parent_table_YYYYMMDD
            date_suffix = current_date.strftime("%Y%m%d")
            partition_name = f"{parent_table}_{date_suffix}"

            executed = self.execute_partition_creation(
                parent_table=parent_table,
                partition_name=partition_name,
                start_date=current_date,
                end_date=next_date,
                hash_buckets=hash_buckets,
                hash_column=hash_column,
                key_type=key_type,
                partition_column=partition_column,
            )

            if executed:
                new_partitions_count += 1

            current_date = next_date

        if new_partitions_count > 0:
            logger.info(
                f"Created {new_partitions_count} new partitions for {parent_table} "
                f"(Covering {start_date.date()} to {end_date.date()})"
            )
        else:
            logger.debug(
                f"Partitions up to date for {parent_table} "
                f"(Covering {start_date.date()} to {end_date.date()})"
            )

        return new_partitions_count
