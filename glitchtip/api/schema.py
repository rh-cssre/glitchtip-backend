from ninja import Schema


def to_camel(string: str) -> str:
    return "".join(
        word if i == 0 else word.capitalize()
        for i, word in enumerate(string.split("_"))
    )


class CamelSchema(Schema):
    class Config(Schema.Config):
        alias_generator = to_camel
        populate_by_name = True
