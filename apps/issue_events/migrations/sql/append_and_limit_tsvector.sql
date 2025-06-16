CREATE OR REPLACE FUNCTION append_and_limit_tsvector(
    existing_vector tsvector,
    new_text TEXT,
    max_lexemes INTEGER,
    text_search_config REGCONFIG
)
RETURNS tsvector AS $$
DECLARE
    combined_vector tsvector;
    new_vector_segment tsvector;
    lexemes_array TEXT[]; -- Renamed for clarity
BEGIN
    -- Convert new_text to a tsvector segment
    IF new_text IS NULL OR new_text = '' THEN
        new_vector_segment := ''::tsvector; -- Empty tsvector
    ELSE
        new_vector_segment := to_tsvector(text_search_config, new_text);
    END IF;

    -- Combine with the existing vector
    IF existing_vector IS NULL OR existing_vector = ''::tsvector THEN
        combined_vector := new_vector_segment;
    ELSE
        combined_vector := existing_vector || new_vector_segment;
    END IF;

    -- If the combined vector is effectively empty, return it
    IF combined_vector IS NULL OR combined_vector = ''::tsvector THEN
        RETURN combined_vector;
    END IF;

    lexemes_array := tsvector_to_array(combined_vector);
    IF array_length(lexemes_array, 1) > max_lexemes THEN
        -- Take the first 'max_lexemes' based on tsvector's internal sorting
        RETURN to_tsvector(text_search_config, array_to_string(lexemes_array[1:max_lexemes], ' '));
    ELSE
        RETURN combined_vector; -- No truncation needed
    END IF;
END;
$$ LANGUAGE plpgsql;
