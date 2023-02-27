GENERATE_ISSUE_TSVECTOR = """
CREATE OR REPLACE FUNCTION generate_issue_tsvector(jsonb) RETURNS tsvector AS $$
BEGIN
    RETURN strip(jsonb_to_tsvector($1, '["string"]'));
    EXCEPTION WHEN program_limit_exceeded THEN
    RETURN null;
END;
$$ LANGUAGE plpgsql;;
"""

# https://stackoverflow.com/a/42998229/443457
JSONB_RECURSIVE_MERGE = """
create or replace function remove_dupes(p_array jsonb)
  returns jsonb
as
$$
select jsonb_agg(distinct e)
from jsonb_array_elements(p_array) as t(e);
$$
language sql;

create or replace function jsonb_merge_deep(jsonb, jsonb)
  returns jsonb
  language sql
  immutable
as $func$
  select case jsonb_typeof($1)
    when 'object' then case jsonb_typeof($2)
      when 'object' then (
        select    jsonb_object_agg(k, case
                    when e2.v is null then e1.v
                    when e1.v is null then e2.v
                    else jsonb_merge_deep(e1.v, e2.v)
                  end)
        from      jsonb_each($1) e1(k, v)
        full join jsonb_each($2) e2(k, v) using (k)
      )
      else $2
    end
    when 'array' then remove_dupes($1 || $2)
    else $2
  end
$func$;
"""

UPDATE_ISSUE_INDEX = """
CREATE OR REPLACE FUNCTION concat_tsvector(tsvector, tsvector) RETURNS tsvector AS $$
BEGIN
    RETURN $1 || $2;
    EXCEPTION WHEN program_limit_exceeded THEN
    RETURN $1;
END;
$$ LANGUAGE plpgsql;;

CREATE OR REPLACE FUNCTION collect_tag (jsonb, hstore)
returns jsonb language sql
as $$
    SELECT jsonb_merge_deep(jsonb_object_agg(y.key, y.values), $1) FROM (
        SELECT (a).key, array_agg(distinct(a).value) as values FROM (
            select each($2) as a
        ) t GROUP by key
    ) y
$$;

CREATE OR REPLACE AGGREGATE agg_collect_tags (hstore) (
    sfunc = collect_tag,
    stype = jsonb,
    initcond = '{}'
);

DROP PROCEDURE IF EXISTS update_issue_index;
CREATE OR REPLACE PROCEDURE update_issue_index(update_issue_id integer)
LANGUAGE SQL
AS $$
WITH event_agg as (
    SELECT COUNT(events_event.event_id) as new_count,
    MAX(events_event.created) as new_last_seen,
    MAX(events_event.level) as new_level,
    agg_collect_tags(events_event.tags) as new_tags
    FROM events_event
    LEFT JOIN issues_issue ON issues_issue.id = events_event.issue_id
    WHERE events_event.issue_id=update_issue_id
    AND events_event.created > issues_issue.last_seen
), event_vector as (
    SELECT strip(COALESCE(generate_issue_tsvector(data), ''::tsvector)) as vector
    FROM events_event
    LEFT JOIN issues_issue on issues_issue.id = events_event.issue_id
    WHERE events_event.issue_id=update_issue_id
    AND events_event.created > issues_issue.last_seen
    limit 1
)
UPDATE issues_issue
SET
  count = event_agg.new_count + issues_issue.count,
  last_seen = GREATEST(event_agg.new_last_seen, issues_issue.last_seen),
  level = GREATEST(event_agg.new_level, issues_issue.level),
  search_vector = concat_tsvector(COALESCE(search_vector, ''::tsvector), event_vector.vector),
  tags = CASE WHEN event_agg.new_tags is not null THEN jsonb_merge_deep(event_agg.new_tags, tags) ELSE tags END
FROM event_agg, event_vector
WHERE issues_issue.id = update_issue_id;
$$;
"""
