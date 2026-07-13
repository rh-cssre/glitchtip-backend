-- Logs Storage: LogEvent Table
-- ID Schema: id (UUIDv7 from client timestamp)
-- Column alignment optimized: 16-byte (UUID), 8-byte (FK/bigint), 2-byte (smallint), variable-width
-- Partitioning: RANGE on id (UUIDv7) -> HASH on organization_id
--
-- Timestamp is embedded in UUIDv7 id - no separate timestamp column needed.
-- The id encodes millisecond-precise client timestamp, enabling both
-- time-based partitioning and time queries from a single column.

-- Create parent table with RANGE partitioning on id (UUIDv7)
CREATE TABLE IF NOT EXISTS logs_logevent (
    -- 16-byte alignment (UUIDs)
    id UUID NOT NULL,  -- UUIDv7 generated from client timestamp (no default - app generates)
    trace_id UUID,

    -- 8-byte alignment (foreign keys + span_id)
    organization_id BIGINT NOT NULL,
    project_id BIGINT NOT NULL,
    span_id BIGINT,  -- OpenTelemetry span_id is 8 bytes

    -- 2-byte alignment (small integers)
    level SMALLINT NOT NULL DEFAULT 2,
    severity_number SMALLINT,  -- OpenTelemetry severity (1-24)

    -- Variable-width fields (varchar, text, jsonb)
    body TEXT NOT NULL,
    service VARCHAR(255) NOT NULL DEFAULT '',
    environment VARCHAR(255) NOT NULL DEFAULT '',
    host VARCHAR(255) NOT NULL DEFAULT '',
    data JSONB NOT NULL DEFAULT '{}',

    -- Primary Key: Composite (id, organization_id)
    -- Required for HASH sub-partitioning by organization_id
    CONSTRAINT logs_logevent_pkey PRIMARY KEY (id, organization_id)

) PARTITION BY RANGE (id);

-- Foreign key constraints
ALTER TABLE logs_logevent
    ADD CONSTRAINT logs_logevent_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE logs_logevent
    ADD CONSTRAINT logs_logevent_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects_project(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

-- Indexes on parent table (will be inherited by partitions)
-- Primary query pattern: list logs for org, ordered by time (using UUIDv7 id)
CREATE INDEX IF NOT EXISTS logevent_org_id_idx
    ON logs_logevent (organization_id, id DESC);

-- Filter by project within org
CREATE INDEX IF NOT EXISTS logevent_proj_id_idx
    ON logs_logevent (project_id, id DESC);

-- Filter by level
CREATE INDEX IF NOT EXISTS logevent_org_level_idx
    ON logs_logevent (organization_id, level, id DESC);

-- Trace correlation (partial index for non-null trace_ids)
CREATE INDEX IF NOT EXISTS logevent_trace_id_idx
    ON logs_logevent (trace_id)
    WHERE trace_id IS NOT NULL;

-- Service filtering (common filter in log queries)
CREATE INDEX IF NOT EXISTS logevent_org_svc_idx
    ON logs_logevent (organization_id, service, id DESC);

-- Environment filtering
CREATE INDEX IF NOT EXISTS logevent_org_env_idx
    ON logs_logevent (organization_id, environment, id DESC);

-- Host filtering
CREATE INDEX IF NOT EXISTS logevent_org_host_idx
    ON logs_logevent (organization_id, host, id DESC);

-- Full-text search on body using trigrams (for ILIKE queries)
-- Requires pg_trgm extension (CREATE EXTENSION IF NOT EXISTS pg_trgm)
CREATE INDEX IF NOT EXISTS logevent_body_trgm_idx
    ON logs_logevent USING GIN (body gin_trgm_ops);

-- Add check constraints for data integrity
ALTER TABLE logs_logevent
    ADD CONSTRAINT logs_logevent_level_check
    CHECK (level >= 0 AND level <= 5);

ALTER TABLE logs_logevent
    ADD CONSTRAINT logs_logevent_severity_check
    CHECK (severity_number IS NULL OR (severity_number >= 1 AND severity_number <= 24));
