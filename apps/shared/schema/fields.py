from typing import Annotated

from pydantic import Field

SlugStr = Annotated[str, Field(pattern=r"^[-a-zA-Z0-9_]+$", max_length=50)]
