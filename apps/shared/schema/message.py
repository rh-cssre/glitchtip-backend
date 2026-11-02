from typing import Literal

from ninja import Schema


class MessageEntry(Schema):
    type: Literal["message"]
    data: dict
