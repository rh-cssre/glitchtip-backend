-- Appends new text to a tsvector, then truncates the result to a max number of lexemes.
-- Includes safeguards to prevent oversized vectors and excessive CPU usage.
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
    existing_size INTEGER;
BEGIN
    -- 1. Truncate incoming text to 250KB to prevent to_tsvector from
    -- using too much CPU or creating an oversized new vector.
    new_text := left(new_text, 250000);

    -- 2. Check the size of the existing vector. If it's already approaching
    -- the ~1MB limit, return it as-is to prevent a crash on concatenation.
    existing_size := pg_column_size(existing_vector);
    IF existing_size > 1040000 THEN
        RETURN existing_vector;
    END IF;

    -- 3. Combine the existing vector with the vector from the new text.
    combined_vector :=
        coalesce(existing_vector, ''::tsvector) ||
        to_tsvector(text_search_config, coalesce(new_text, ''));

    IF combined_vector = ''::tsvector THEN
        RETURN combined_vector;
    END IF;

    -- 4. Strip positional data and truncate the vector to the desired number of lexemes.
    stripped_vector := strip(combined_vector);
    lexemes_array := tsvector_to_array(stripped_vector);
    current_lexeme_count := array_length(lexemes_array, 1);

    IF current_lexeme_count > max_lexemes THEN
        lexemes_to_remove := lexemes_array[max_lexemes + 1 : current_lexeme_count];
        RETURN ts_delete(stripped_vector, lexemes_to_remove);
    ELSE
        RETURN stripped_vector;
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
