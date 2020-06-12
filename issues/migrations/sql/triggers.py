update_issue_trigger = """
DROP TRIGGER IF EXISTS event_search_vector_update on issues_event;
DROP FUNCTION IF EXISTS data_to_search_vector;

CREATE FUNCTION update_issue() RETURNS trigger AS $$
DECLARE event_count INT;
DECLARE events_search_vector tsvector;
BEGIN

event_count := (SELECT count(*) from issues_event where issues_event.issue_id = new.issue_id);

UPDATE issues_issue
SET last_seen = new.created, count = event_count
WHERE issues_issue.id = new.issue_id;

IF event_count <= 100 THEN
    events_search_vector := (
        SELECT strip(jsonb_to_tsvector('english', jsonb_agg(issues_event.data), '["string"]'))
        FROM issues_event
        WHERE issues_event.issue_id = new.issue_id
    );
    UPDATE issues_issue
    SET search_vector = events_search_vector 
    where issues_issue.id = new.issue_id;
END IF;

RETURN new;
END
$$ LANGUAGE plpgsql;;


CREATE TRIGGER event_issue_update AFTER INSERT OR UPDATE
ON issues_event FOR EACH ROW EXECUTE PROCEDURE
update_issue();
"""
