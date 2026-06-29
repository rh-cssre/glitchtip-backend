-- IssueIndex: hash-partitioned leaf for issues, decoupled from Issue.
-- Hash partitions are created by RunPython (uses settings.PARTITION_HASH_BUCKETS).
--
-- This is the hot/queryable projection of Issue: it holds the per-event-updated
-- columns (count/last_seen/last_release) plus status/level and the full-text
-- vector, so the Issue table stays small and near-static and these columns get
-- org-partition pruning on the issue-list and search paths.
--
-- Column order is alignment-optimal (8 / 8 / 8 / 4 / 4 / 2 / 2 then the varlena
-- tsvector) so the 36-byte fixed head packs with zero padding; fts_document MUST
-- stay last. The fixed-width columns carry DB-level defaults so that a leaf row
-- inserted by the *previous* release during a rolling deploy (which only writes
-- issue_id/organization_id/fts_document) still satisfies NOT NULL; the new
-- write path sets them explicitly.

CREATE TABLE IF NOT EXISTS issue_events_issueindex (
    -- 8-byte
    issue_id BIGINT NOT NULL,
    last_release_id BIGINT,                          -- nullable FK (db_constraint omitted)
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 4-byte
    organization_id INTEGER NOT NULL,                -- partition key
    count INTEGER NOT NULL DEFAULT 1,
    -- 2-byte
    status SMALLINT NOT NULL DEFAULT 0,              -- EventStatus.UNRESOLVED
    level SMALLINT NOT NULL DEFAULT 4,               -- LogLevel.ERROR
    -- variable-width: MUST stay last so the fixed head packs without padding
    fts_document TSVECTOR NOT NULL DEFAULT '',

    PRIMARY KEY (issue_id, organization_id)
) PARTITION BY HASH (organization_id);

-- GIN index for full-text search (created on parent, inherited by partitions)
CREATE INDEX issueindex_fts_gin
    ON issue_events_issueindex USING GIN (fts_document);

-- List-serving btree indexes (org-pruned). (org, status, last_seen DESC) serves
-- the default "is:unresolved ORDER BY last_seen" list; (org, count DESC) serves
-- count-sorted lists. Both keep the common issue-list reads single-table on the
-- leaf with a small PK hydrate back to Issue.
CREATE INDEX issueindex_org_status_lastseen
    ON issue_events_issueindex (organization_id, status, last_seen DESC);
CREATE INDEX issueindex_org_count
    ON issue_events_issueindex (organization_id, count DESC);

-- No DB-level FK constraints: partitioned tables cannot be the target of a
-- foreign key, and DB-level FKs on the partitioned side cause "cannot truncate
-- a table referenced in a foreign key constraint" during Django's test flush.
-- ORM FK fields (DO_NOTHING) still provide joins and validation.
