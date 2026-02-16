from ninja import Schema
from pydantic import ConfigDict


def to_camel(string: str) -> str:
    """If a word is exactly id, make it ID"""
    return "".join(
        word if i == 0 else "ID" if word == "id" else word.capitalize()
        for i, word in enumerate(string.split("_"))
    )


class CamelSchema(Schema):
    """
    Use json camel case convention by default

    - event_id > eventID
    - event_number > eventNumber
    - foobar_100 > foobar100
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
