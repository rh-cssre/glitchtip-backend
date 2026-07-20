-- One database per benchmark arm so the two web services never share
-- tables, partitions, or vtasks state. Runs once on a fresh postgres volume
-- (docker-entrypoint-initdb.d).
CREATE DATABASE glitchtip_py;
CREATE DATABASE glitchtip_rust;
