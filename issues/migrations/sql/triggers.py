UPDATE_ISSUE_TRIGGER = """
DROP TRIGGER IF EXISTS event_issue_update on issues_event;
DROP FUNCTION IF EXISTS update_issue;

CREATE FUNCTION update_issue() RETURNS trigger AS $$
DECLARE event_count INT;
DECLARE events_search_vector tsvector;
BEGIN

event_count := (SELECT count(*) from issues_event where issues_event.issue_id = new.issue_id);

UPDATE issues_issue
SET last_seen = new.created, count = event_count
WHERE issues_issue.id = new.issue_id;

IF event_count <= 100 THEN
    BEGIN
        events_search_vector := (
            SELECT strip(jsonb_to_tsvector('english', jsonb_agg(issues_event.data), '["string"]'))
            FROM issues_event
            WHERE issues_event.issue_id = new.issue_id
        );

        UPDATE issues_issue
        SET search_vector = events_search_vector 
        where issues_issue.id = new.issue_id;

        EXCEPTION WHEN program_limit_exceeded THEN
    END;
END IF;

RETURN new;
END
$$ LANGUAGE plpgsql;;


CREATE TRIGGER event_issue_update AFTER INSERT OR UPDATE
ON issues_event FOR EACH ROW EXECUTE PROCEDURE
update_issue();
"""

INCREMENT_PROJECT_COUNTER_TRIGGER = """
DROP TRIGGER IF EXISTS increment_project_counter on issues_issue;

CREATE OR REPLACE FUNCTION increment_project_counter() RETURNS trigger AS $$
DECLARE
    counter_value int;
BEGIN
    INSERT INTO projects_projectcounter (value, project_id)
    VALUES (0, NEW.project_id)
    ON CONFLICT (project_id) DO UPDATE SET value = projects_projectcounter.value + 1
    RETURNING value into counter_value;
    NEW.short_id=counter_value;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;;

CREATE TRIGGER increment_project_counter BEFORE INSERT
ON issues_issue FOR EACH ROW EXECUTE PROCEDURE
increment_project_counter();
"""
