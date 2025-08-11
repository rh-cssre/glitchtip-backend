-- This function appends new text to an existing tsvector, strips all
-- positional data, and then truncates the vector to a maximum number
-- of unique lexemes.
CREATE OR REPLACE FUNCTION append_and_limit_tsvector(
    existing_vector tsvector,
    new_text TEXT,
    max_lexemes INTEGER,
    text_search_config REGCONFIG
)
RETURNS tsvector AS $$
DECLARE
    combined_vector tsvector;
    stripped_vector tsvector;
    lexemes_array TEXT[];
    lexemes_to_remove TEXT[];
    current_lexeme_count INTEGER;
BEGIN
    -- Combine the existing vector with the new text.
    combined_vector :=
        coalesce(existing_vector, ''::tsvector) ||
        to_tsvector(text_search_config, coalesce(new_text, ''));

    -- If the combined vector is effectively empty, there's nothing more to do.
    IF combined_vector = ''::tsvector THEN
        RETURN combined_vector;
    END IF;

    -- Use strip to remove all position and weight information.
    stripped_vector := strip(combined_vector);

    -- Get an array of all unique lexemes from the stripped vector.
    lexemes_array := tsvector_to_array(stripped_vector);
    current_lexeme_count := array_length(lexemes_array, 1);

    -- If the number of unique lexemes exceeds the limit, truncate.
    IF current_lexeme_count > max_lexemes THEN
        -- Determine which lexemes to remove (the ones at the end of the sorted array).
        lexemes_to_remove := lexemes_array[max_lexemes + 1 : current_lexeme_count];

        -- Use ts_delete to remove the excess lexemes from the stripped vector.
        RETURN ts_delete(stripped_vector, lexemes_to_remove);
    ELSE
        -- If the limit is not exceeded, return the stripped vector.
        RETURN stripped_vector;
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
