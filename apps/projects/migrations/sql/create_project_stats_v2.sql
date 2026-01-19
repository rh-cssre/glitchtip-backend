-- Storage Engine V2: Project Statistics Tables
-- Fresh Start: Drops old tables and data

-- IssueEventProjectHourlyStatistic
CREATE TABLE IF NOT EXISTS projects_issueeventprojecthourlystatistic (
    project_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    count INTEGER CHECK (count >= 0),
    PRIMARY KEY (project_id, organization_id, date)
) PARTITION BY RANGE (date);

ALTER TABLE projects_issueeventprojecthourlystatistic
    ADD CONSTRAINT projects_issueeventprojecthourlystatistic_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects_project(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE projects_issueeventprojecthourlystatistic
    ADD CONSTRAINT projects_issueeventprojecthourlystatistic_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS projects_issueeventprojecthourlystatistic_default
    PARTITION OF projects_issueeventprojecthourlystatistic DEFAULT;


-- TransactionEventProjectHourlyStatistic
CREATE TABLE IF NOT EXISTS projects_transactioneventprojecthourlystatistic (
    project_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    count INTEGER CHECK (count >= 0),
    PRIMARY KEY (project_id, organization_id, date)
) PARTITION BY RANGE (date);

ALTER TABLE projects_transactioneventprojecthourlystatistic
    ADD CONSTRAINT projects_transactioneventprojecthourlystatistic_project_id_fkey
    FOREIGN KEY (project_id) REFERENCES projects_project(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE projects_transactioneventprojecthourlystatistic
    ADD CONSTRAINT projects_transactioneventprojecthourlystatistic_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS projects_transactioneventprojecthourlystatistic_default
    PARTITION OF projects_transactioneventprojecthourlystatistic DEFAULT;
