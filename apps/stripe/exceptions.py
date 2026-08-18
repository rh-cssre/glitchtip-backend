class StripeResourceNotFound(Exception):
    pass


class StripeError(Exception):
    """Non-retryable Stripe API error, carrying Stripe's structured fields so
    callers can branch on type (e.g. surface a card decline, hide a backend error)."""

    def __init__(self, message: str, *, status: int, type: str = "", code: str = ""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.type = type
        self.code = code
