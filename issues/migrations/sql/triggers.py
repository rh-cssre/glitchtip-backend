# No longer used
UPDATE_ISSUE_TRIGGER = """
DROP TRIGGER IF EXISTS event_issue_update on events_event;
DROP FUNCTION IF EXISTS update_issue;
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
