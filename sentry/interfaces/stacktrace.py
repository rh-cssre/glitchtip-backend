def get_context(lineno, context_line, pre_context=None, post_context=None):
    if lineno is None:
        return []

    if context_line is None and not (pre_context or post_context):
        return []

    lineno = int(lineno)
    context = []
    start_lineno = lineno - len(pre_context or [])
    if pre_context:
        start_lineno = lineno - len(pre_context)
        at_lineno = start_lineno
        for line in pre_context:
            context.append([at_lineno, line])
            at_lineno += 1
    else:
        start_lineno = lineno
        at_lineno = lineno

    if start_lineno < 0:
        start_lineno = 0

    context.append([at_lineno, context_line])
    at_lineno += 1

    if post_context:
        for line in post_context:
            context.append([at_lineno, line])
            at_lineno += 1

    return context
