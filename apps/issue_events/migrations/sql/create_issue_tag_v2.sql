-- Storage Engine V2: IssueTag Table
-- Partitioning: Range by date -> Hash by organization_id (handled by partitions)

CREATE TABLE IF NOT EXISTS issue_events_issuetag (
    issue_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    tag_key_id INTEGER NOT NULL,
    tag_value_id INTEGER NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    count INTEGER CHECK (count >= 0),
    PRIMARY KEY (issue_id, organization_id, tag_key_id, tag_value_id, date)
) PARTITION BY RANGE (date);

-- Foreign Keys
-- issue_id FK intentionally omitted (db_constraint=False) — see create_events_v2.sql.

ALTER TABLE issue_events_issuetag
    ADD CONSTRAINT issue_events_issuetag_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

