DROP FUNCTION IF EXISTS get_project_auth_info(bigint,uuid);
CREATE OR REPLACE FUNCTION get_project_auth_info(p_project_id BIGINT, p_sentry_key UUID)
RETURNS TABLE (
    project_id BIGINT,
    project_scrub_ip_addresses BOOLEAN,
    project_event_throttle_rate SMALLINT,
    organization_id INT,
    organization_is_accepting_events BOOLEAN,
    organization_event_throttle_rate SMALLINT,
    organization_scrub_ip_addresses BOOLEAN,
    project_first_event TIMESTAMP WITH TIME ZONE
)
AS $$
BEGIN
    RETURN QUERY
    SELECT
        "projects_project"."id",
        "projects_project"."scrub_ip_addresses",
        "projects_project"."event_throttle_rate",
        "projects_project"."organization_id",
        "organizations_ext_organization"."is_accepting_events",
        "organizations_ext_organization"."event_throttle_rate",
        "organizations_ext_organization"."scrub_ip_addresses",
        "projects_project"."first_event"
    FROM
        "projects_project"
    INNER JOIN
        "projects_projectkey" ON ("projects_project"."id" = "projects_projectkey"."project_id")
    INNER JOIN
        "organizations_ext_organization" ON ("projects_project"."organization_id" = "organizations_ext_organization"."id")
    WHERE
        "projects_project"."id" = p_project_id
        AND "projects_projectkey"."public_key" = p_sentry_key;
END;
$$ LANGUAGE plpgsql;