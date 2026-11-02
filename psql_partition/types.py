from enum import Enum


class PostgresPartitioningMethod(Enum):
    RANGE = "RANGE"
    LIST = "LIST"
    HASH = "HASH"

    def __getitem__(self, key):
        return self
