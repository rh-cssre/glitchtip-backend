-- LogProjectHourlyStatistic - partitioned by date with HASH sub-partitions by org
-- Stores hourly log counts per project, broken down by level, service bucket, and environment bucket
-- Both buckets are hash(name) % 256 to bound cardinality
CREATE TABLE IF NOT EXISTS projects_logprojecthourlystatistic (
    project_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    level SMALLINT NOT NULL,
    service_bucket SMALLINT NOT NULL DEFAULT 0,
    environment_bucket SMALLINT NOT NULL DEFAULT 0,
    count INTEGER CHECK (count >= 0),
    PRIMARY KEY (project_id, organization_id, date, level, service_bucket, environment_bucket)
) PARTITION BY RANGE (date);

ALTER TABLE projects_logprojecthourlystatistic
    ADD CONSTRAINT projects_logprojecthourlystatistic_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects_project(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE projects_logprojecthourlystatistic
    ADD CONSTRAINT projects_logprojecthourlystatistic_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;
