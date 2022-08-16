GENERATE_ISSUE_TSVECTOR = """
CREATE OR REPLACE FUNCTION generate_issue_tsvector(jsonb) RETURNS tsvector AS $$
BEGIN
    RETURN strip(jsonb_to_tsvector($1, '["string"]'));
    EXCEPTION WHEN program_limit_exceeded THEN
    RETURN null;
END;
$$ LANGUAGE plpgsql;;
"""

UPDATE_ISSUE_INDEX = """
DROP PROCEDURE IF EXISTS update_issue_index;
CREATE OR REPLACE PROCEDURE update_issue_index(update_issue_id integer)
LANGUAGE SQL
AS $$
WITH event_agg as (
    SELECT COUNT(events_event.event_id) as new_count,
    MAX(events_event.created) as new_last_seen,
    MAX(events_event.level) as new_level
    FROM events_event
    WHERE events_event.issue_id=update_issue_id
), event_vector as (
    SELECT strip(COALESCE(generate_issue_tsvector(data), '') || COALESCE(issues_issue.search_vector, '')) as vector
    FROM events_event
    LEFT JOIN issues_issue on issues_issue.id = events_event.issue_id
    WHERE events_event.issue_id=update_issue_id
    limit 1
), event_tags as (
  SELECT jsonb_object_agg(y.key, y.values) as new_tags FROM (
    SELECT (a).key, array_agg(distinct(a).value) as values
    FROM (
      SELECT each(tags) as a
      FROM events_event
      WHERE events_event.issue_id=update_issue_id
    ) t GROUP by key
  ) y
)
UPDATE issues_issue
SET
  count = event_agg.new_count,
  last_seen = event_agg.new_last_seen,
  level = event_agg.new_level,
  search_vector = CASE WHEN search_vector is null or length(search_vector) < 100000 THEN event_vector.vector ELSE search_vector END,
  tags = CASE WHEN event_Tags.new_tags is not null THEN event_tags.new_tags ELSE tags END
FROM event_agg, event_vector, event_tags
WHERE issues_issue.id = update_issue_id;
$$;
"""
