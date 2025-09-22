from django.db.models import Func


class PipeConcat(Func):
    """
    Double pipe based concat works with more types than the Concat function
    """

    template = "(%(expressions)s)"
    arg_joiner = " || "


class PGAppendAndLimitTsVector(Func):
    """
    Custom Django Func expression to call the append_and_limit_tsvector PostgreSQL function.
    """

    function = "append_and_limit_tsvector"
    template = "%(function)s(%(expressions)s)"  # Django will comma-separate expressions

    def __init__(
        self,
        existing_vector_expr,
        new_text_expr,
        max_lexemes_expr,
        config_expr,
        **extra,
    ):
        # Arguments must be in the same order as the SQL function's parameters
        super().__init__(
            existing_vector_expr, new_text_expr, max_lexemes_expr, config_expr, **extra
        )
