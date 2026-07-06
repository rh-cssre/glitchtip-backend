-- Storage Engine V2: Uptime Tables
-- Fresh Start: Drops old tables and data

-- MonitorCheck
-- Partitioning: Range by id (UUIDv7) -> Hash by organization_id (handled by partitions)
CREATE TABLE IF NOT EXISTS uptime_monitorcheck (
    id UUID DEFAULT uuid_generate_v7(),
    organization_id BIGINT NOT NULL,
    monitor_id BIGINT NOT NULL,
    start_check TIMESTAMPTZ NOT NULL,
    response_time INTEGER,
    reason SMALLINT,
    is_up BOOLEAN NOT NULL,
    is_change BOOLEAN NOT NULL,
    data JSONB,
    
    PRIMARY KEY (id, organization_id)
) PARTITION BY RANGE (id);

-- Foreign Keys
ALTER TABLE uptime_monitorcheck
    ADD CONSTRAINT uptime_monitorcheck_monitor_id_fkey
    FOREIGN KEY (monitor_id) REFERENCES uptime_monitor(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE uptime_monitorcheck
    ADD CONSTRAINT uptime_monitorcheck_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
