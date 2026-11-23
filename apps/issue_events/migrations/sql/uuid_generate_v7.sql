-- UUIDv7 Generation Function for PostgreSQL
-- Implements RFC 9562 UUIDv7 with millisecond timestamp precision
-- https://datatracker.ietf.org/doc/html/rfc9562

CREATE OR REPLACE FUNCTION uuid_generate_v7()
RETURNS UUID
AS $$
DECLARE
    unix_ts_ms BIGINT;
    uuid_bytes BYTEA;
    rand_a BYTEA;
    rand_b BYTEA;
BEGIN
    -- Get current Unix timestamp in milliseconds (48 bits)
    unix_ts_ms := FLOOR(EXTRACT(EPOCH FROM CLOCK_TIMESTAMP()) * 1000);

    -- Generate random bytes for the random portions
    rand_a := GEN_RANDOM_BYTES(2);  -- 16 bits, we'll use 12
    rand_b := GEN_RANDOM_BYTES(8);  -- 64 bits, we'll use 62

    -- Construct UUIDv7 bytes:
    -- [0-5]   48-bit timestamp (6 bytes)
    -- [6-7]   version (4 bits = 0x7) + 12-bit random (2 bytes total)
    -- [8]     variant (2 bits = 0b10) + 6-bit random (1 byte)
    -- [9-15]  56-bit random (7 bytes)

    uuid_bytes :=
        -- Bytes 0-5: 48-bit timestamp in big-endian
        SUBSTRING(INT8SEND(unix_ts_ms), 3, 6) ||
        -- Bytes 6-7: Version 7 (0x7) in upper 4 bits of byte 6, plus 12 random bits
        SET_BYTE('\x00'::BYTEA, 0, (GET_BYTE(rand_a, 0) & 15) | 112) ||  -- 112 = 0x70 = version 7
        SET_BYTE('\x00'::BYTEA, 0, GET_BYTE(rand_a, 1)) ||
        -- Byte 8: Variant (0b10) in upper 2 bits, plus 6 random bits
        SET_BYTE('\x00'::BYTEA, 0, (GET_BYTE(rand_b, 0) & 63) | 128) ||  -- 128 = 0x80 = variant 0b10
        -- Bytes 9-15: 56 random bits (7 bytes)
        SUBSTRING(rand_b, 2, 7);

    RETURN ENCODE(uuid_bytes, 'hex')::UUID;
END;
$$ LANGUAGE plpgsql VOLATILE;
