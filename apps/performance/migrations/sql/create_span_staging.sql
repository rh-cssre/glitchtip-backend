-- SpanStaging: Write-heavy staging table for span data
-- Partitioned by RANGE on id (UUIDv7) with HASH sub-partitioning on organization_id
-- Matches the IssueEvent partitioning pattern for partition-aware queries
-- No additional indexes — optimized for bulk inserts, read only by promotion job
-- Rows are promoted to Parquet and deleted within hours

CREATE TABLE IF NOT EXISTS performance_spanstaging (
    -- 16-byte alignment: UUID
    id UUID NOT NULL,

    -- 8-byte alignment: FKs, timestamps, floats
    organization_id INTEGER NOT NULL,
    project_id INTEGER NOT NULL,
    duration DOUBLE PRECISION NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,

    -- Variable-width fields
    transaction_name VARCHAR(1024) NOT NULL,
    span_id VARCHAR(32) NOT NULL,
    transaction_id VARCHAR(32) NOT NULL,
    op VARCHAR(255) NOT NULL,
    description VARCHAR(500) NOT NULL DEFAULT '',

    PRIMARY KEY (id, organization_id)
) PARTITION BY RANGE (id);
