import hashlib
from typing import TYPE_CHECKING

from .schema import EventMessage

if TYPE_CHECKING:
    from apps.issue_events.models import IssueEventType


def default_hash_input(title: str, culprit: str, type: "IssueEventType") -> str:
    return title + culprit + str(type)


def generate_hash(
    title: str, culprit: str, type: "IssueEventType", extra: list[str] | None = None
) -> str:
    """Generate insecure hash used for grouping issues"""
    if extra:
        hash_input = "".join(
            [
                default_hash_input(title, culprit, type)
                if part == "{{ default }}"
                else (part or "")
                for part in extra
            ]
        )
    else:
        hash_input = default_hash_input(title, culprit, type)
    return hashlib.md5(hash_input.encode()).hexdigest()


def transform_parameterized_message(message: str | EventMessage) -> str:
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
        if isinstance(params, list) and params:
            return message.message % tuple(params)
        elif isinstance(params, dict):
            return message.message.format(**params)
        else:
            # Params not provided, return message as is
            return message.message
    return message.formatted


Replacable = str | dict | list
KNOWN_BADS = ["\u0000"]


def _clean_string(s: str) -> str:
    for char in KNOWN_BADS:
        s = s.replace(char, "")
    return s


def remove_bad_chars(obj: Replacable) -> Replacable:
    """Remove charachers which postgresql cannot store"""

    if isinstance(obj, dict):
        return {
            _clean_string(key): remove_bad_chars(value) for key, value in obj.items()
        }
    elif isinstance(obj, (list, tuple)):
        return [remove_bad_chars(item) for item in obj]
    elif isinstance(obj, str):
        return _clean_string(obj)
    else:
        return obj
