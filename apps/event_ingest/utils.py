import hashlib
from typing import List, Optional, Union

from apps.issue_events.models import IssueEventType

from .schema import EventMessage


def default_hash_input(title: str, culprit: str, type: IssueEventType) -> str:
    return title + culprit + str(type)


def generate_hash(
    title: str, culprit: str, type: IssueEventType, extra: Optional[List[str]] = None
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


def transform_parameterized_message(message: Union[str, EventMessage]) -> str:
    """
    Accept str or Event Message interface
    Returns formatted string with interpolation

    Both examples would return "Hello there":
    {
        "message": "Hello %s",
        "params": ["there"]
    }
    {
        "message": "Hello {foo}",
        "params": {"foo": "there"}
    }
    """
    if isinstance(message, str):
        return message
    if not message.formatted and message.message:
        params = message.params
        if isinstance(params, list):
            return message.message % tuple(params)
        elif isinstance(params, dict):
            return message.message.format(**params)
        else:
            # Params not provided, return message as is
            return message.message
    return message.formatted
