from django.db.models import Func


class PipeConcat(Func):
    """
    Double pipe based concat works with more types than the Concat function
    """

    template = "(%(expressions)s)"
    arg_joiner = " || "
