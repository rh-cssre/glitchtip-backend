GENERATE_ISSUE_TSVECTOR = """
CREATE OR REPLACE FUNCTION generate_issue_tsvector(jsonb) RETURNS tsvector AS $$
BEGIN
    RETURN strip(jsonb_to_tsvector($1, '["string"]'));
    EXCEPTION WHEN program_limit_exceeded THEN
    RETURN null;
END;
$$ LANGUAGE plpgsql;;
"""
