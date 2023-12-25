from datetime import datetime
from typing import Optional


class ThrottleException(Exception):
    """
    429 Too Many Requests Exception
    Supports optional retry_after kwarg seconds or Date

    https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/429
    """

    retry_after: Optional[int | datetime]

    def __init__(self, retry_after: Optional[int | datetime] = None) -> None:
        self.retry_after = retry_after
        super().__init__(retry_after)
