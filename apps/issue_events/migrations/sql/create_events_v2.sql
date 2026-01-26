-- Storage Engine V2: IssueEvent Table
-- Dual ID Schema: id (server UUIDv7) + event_id (client UUIDv4, nullable)
-- Column alignment optimized: 16-byte (UUID), 8-byte (timestamp/FK), 2-byte (smallint), variable-width

-- Create parent table with RANGE partitioning on id (UUIDv7)
CREATE TABLE IF NOT EXISTS issue_events_issueevent (
    -- 16-byte alignment (UUIDs)
    id UUID NOT NULL DEFAULT uuid_generate_v7(),
    event_id UUID,

    -- 8-byte alignment (timestamps)
    timestamp TIMESTAMPTZ NOT NULL,
    received TIMESTAMPTZ NOT NULL,
    created timestamp with time zone NOT NULL DEFAULT timezone('utc'::text, now()),

    -- 8-byte alignment (foreign keys - bigint)
    issue_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    release_id BIGINT,

    -- 2-byte alignment (small integers)
    type SMALLINT NOT NULL DEFAULT 0,
    level SMALLINT NOT NULL DEFAULT 4,

    -- Variable-width fields (varchar, jsonb, array)
    title VARCHAR(255) NOT NULL,
    transaction VARCHAR(1024) NOT NULL,
    data JSONB NOT NULL,
    tags JSONB NOT NULL,
    hashes text[] NOT NULL DEFAULT ARRAY[]::text[],

    -- Primary Key: Composite (id, organization_id)
    -- Required for HASH sub-partitioning by organization_id
    CONSTRAINT issue_events_issueevent_pkey PRIMARY KEY (id, organization_id)

) PARTITION BY RANGE (id);

-- Foreign key constraints
ALTER TABLE issue_events_issueevent
    ADD CONSTRAINT issue_events_issueevent_issue_id_fkey
    FOREIGN KEY (issue_id) REFERENCES issue_events_issue(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE issue_events_issueevent
    ADD CONSTRAINT issue_events_issueevent_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE issue_events_issueevent
    ADD CONSTRAINT issue_events_issueevent_release_id_fkey
    FOREIGN KEY (release_id) REFERENCES releases_release(id)
    ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;

-- Indexes on parent table (will be inherited by partitions)
CREATE INDEX IF NOT EXISTS issue_events_issueevent_issue_received_idx
    ON issue_events_issueevent (issue_id, received DESC);

CREATE INDEX IF NOT EXISTS issue_events_issueevent_hashes_idx
    ON issue_events_issueevent USING GIN (hashes);

CREATE INDEX IF NOT EXISTS issue_events_issueevent_release_id_idx
    ON issue_events_issueevent (release_id);

-- Partial index for client-provided event_ids (sparse - only when NOT NULL)
-- This enables fast lookups for SDK-provided event IDs without indexing all NULLs
CREATE INDEX IF NOT EXISTS issue_events_issueevent_event_id_idx
    ON issue_events_issueevent (event_id)
    WHERE event_id IS NOT NULL;

-- Add check constraints for data integrity
ALTER TABLE issue_events_issueevent
    ADD CONSTRAINT issue_events_issueevent_type_check
    CHECK (type >= 0);

ALTER TABLE issue_events_issueevent
    ADD CONSTRAINT issue_events_issueevent_level_check
    CHECK (level >= 0 AND level <= 5);

