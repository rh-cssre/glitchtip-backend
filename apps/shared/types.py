from typing import Union

TypeJson = Union[
    dict[str, Union[str, int, bool, "TypeJson"]],
    list[Union[str, int, bool, "TypeJson"]],
]
"""Python object that is valid JSON (can be serialized to and from)"""
