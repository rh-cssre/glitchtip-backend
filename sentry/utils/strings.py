def truncatechars(value: str, chars=100):
    """ Truncate string and append … """
    return (value[:chars] + "…") if len(value) > chars else value

