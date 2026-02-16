-- Storage Engine V2: IssueAggregate Table
-- Partitioning: Range by date -> Sub-partition by HASH (organization_id)
-- Strategy: Fresh Start (Old data discarded)

CREATE TABLE IF NOT EXISTS issue_events_issueaggregate (
    issue_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    count INTEGER CHECK (count >= 0),
    PRIMARY KEY (issue_id, organization_id, date)
) PARTITION BY RANGE (date);

-- Foreign keys
ALTER TABLE issue_events_issueaggregate
    ADD CONSTRAINT issue_events_issueaggregate_issue_id_fkey
    FOREIGN KEY (issue_id) REFERENCES issue_events_issue(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE issue_events_issueaggregate
    ADD CONSTRAINT issue_events_issueaggregate_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;


