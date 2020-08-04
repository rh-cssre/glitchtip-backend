ADD_EVENT_TAGS = """
DROP TYPE IF EXISTS tag_key_value CASCADE;
CREATE TYPE tag_key_value AS (key_id integer, value text);

CREATE OR REPLACE FUNCTION add_event_tags(IN event_id uuid, IN tags tag_key_value[]) RETURNS void as $$
DECLARE
  event_tag tag_key_value;
BEGIN
  FOREACH event_tag IN ARRAY tags
  LOOP
    WITH e AS (
        INSERT INTO issues_eventtag (key_id, "value") VALUES
        (event_tag.key_id, event_tag.value)
        ON CONFLICT (key_id, value) DO NOTHING
        RETURNING id
    ), y as (
    SELECT id FROM e
        UNION
            SELECT id
            FROM issues_eventtag
            WHERE key_id=event_tag.key_id AND value=event_tag.value
    )
    INSERT INTO issues_event_tags (event_id, eventtag_id)
    select event_id, id
    FROM y;
  END LOOP;
END
$$ LANGUAGE plpgsql;
"""
