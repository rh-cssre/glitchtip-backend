import hashlib
from typing import List, Optional

from issues.models import EventType


def default_hash_input(title: str, culprit: str, type: EventType) -> str:
    return title + culprit + str(type)


def generate_hash(
    title: str, culprit: str, type: EventType, extra: Optional[List[str]] = None
) -> str:
    """Generate insecure hash used for grouping issues"""
    if extra:
        hash_input = "".join(
            [
                default_hash_input(title, culprit, type)
                if part == "{{ default }}"
                else part
                for part in extra
            ]
        )
    else:
        hash_input = default_hash_input(title, culprit, type)
    return hashlib.md5(hash_input.encode()).hexdigest()
