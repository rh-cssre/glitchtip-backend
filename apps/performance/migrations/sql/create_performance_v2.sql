-- Storage Engine V2: Performance Tables
-- Fresh Start: Drops old tables and data

-- TransactionEvent
-- Partitioning: Range by id (UUIDv7) -> Hash by organization_id (handled by partitions)
CREATE TABLE IF NOT EXISTS performance_transactionevent (
    id UUID DEFAULT uuid_generate_v7(),
    event_id UUID,
    trace_id UUID NOT NULL,
    start_timestamp TIMESTAMPTZ NOT NULL,
    timestamp TIMESTAMPTZ,
    group_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    duration INTEGER CHECK (duration >= 0),
    data JSONB NOT NULL,
    tags JSONB NOT NULL,
    
    PRIMARY KEY (id, organization_id)
) PARTITION BY RANGE (id);

-- Foreign Keys
ALTER TABLE performance_transactionevent
    ADD CONSTRAINT performance_transactionevent_group_id_fkey
    FOREIGN KEY (group_id) REFERENCES performance_transactiongroup(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE performance_transactionevent
    ADD CONSTRAINT performance_transactionevent_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

-- Default partition
CREATE TABLE IF NOT EXISTS performance_transactionevent_default
    PARTITION OF performance_transactionevent DEFAULT;


-- TransactionGroupAggregate
-- Partitioning: Range by date -> Hash by organization_id (handled by partitions)
CREATE TABLE IF NOT EXISTS performance_transactiongroupaggregate (
    group_id BIGINT NOT NULL,
    organization_id BIGINT NOT NULL,
    date TIMESTAMPTZ NOT NULL,
    total_duration BIGINT CHECK (total_duration >= 0),
    sum_of_squares_duration BIGINT CHECK (sum_of_squares_duration >= 0),
    count INTEGER CHECK (count >= 0),
    histogram JSONB NOT NULL,
    
    PRIMARY KEY (group_id, organization_id, date)
) PARTITION BY RANGE (date);

-- Foreign Keys
ALTER TABLE performance_transactiongroupaggregate
    ADD CONSTRAINT performance_transactiongroupaggregate_group_id_fkey
    FOREIGN KEY (group_id) REFERENCES performance_transactiongroup(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE performance_transactiongroupaggregate
    ADD CONSTRAINT performance_transactiongroupaggregate_organization_id_fkey
    FOREIGN KEY (organization_id) REFERENCES organizations_ext_organization(id)
    ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED;

-- Default partition
CREATE TABLE IF NOT EXISTS performance_transactiongroupaggregate_default
    PARTITION OF performance_transactiongroupaggregate DEFAULT;