-- UptimeCheckHourlyStatistic - weekly range partitioned by date
-- Stores hourly uptime check counts per organization
CREATE TABLE IF NOT EXISTS uptime_uptimecheckhourlystatistic (
    organization_id INTEGER NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    count INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (organization_id, date)
) PARTITION BY RANGE (date);

ALTER TABLE uptime_uptimecheckhourlystatistic
    ADD CONSTRAINT uptime_uptimecheckhourlystatistic_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
