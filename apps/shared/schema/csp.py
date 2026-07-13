from typing import Literal

from ninja import Schema


class CSPEntry(Schema):
    type: Literal["csp"]
    data: dict
