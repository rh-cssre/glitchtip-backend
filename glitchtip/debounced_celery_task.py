"""
Based on https://gist.github.com/pardo/19d672235bbef6fa793a

Debounced tasks should

- Execute on first call
- Execute on last call (last being the last call within a countdown time)
- Execute periodically in between (every 10, 100, 1000th)

Examples:

- 1 call happens, execute immediately
- 99 calls happen in 2 seconds - execute 1st, 10th 99th)
- 1 call happens every second forever - execute 1st, 10th, 100th, 1000th, 2000th, etc
"""
import functools

from django.conf import settings
from django.core.cache import cache
from django_redis import get_redis_connection

CACHE_PREFIX = ":1:"  # Django cache version
# Run task on each mark, last mark will repeat
# 30th, 250th, 1000th, 2000th, etc
RUN_ON = [30, 250, 1000]


def debounced_wrap(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        key = kwargs.pop("key")
        call_count = kwargs.pop("call_count", 1)
        count = cache.get(key, 1)

        # If last task, or on every RUN_ON
        if count <= call_count or call_count in RUN_ON or call_count % RUN_ON[-1] == 0:
            return func(*args, **kwargs)

    return wrapper


def debounced_task(key_generator):
    """
    :param func: must be the @debounced_wrap decorated with @task / @shared_task from celery
    :param key_generator: function that knows how to generate a key from
    args and kwargs passed to func or a constant str used in the cache
    key will be prepended with function module and name

    Run on first task, last task, and every few tasks in between

    :return: function that calls apply_async on the task keep that in mind when send the arguments
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(**kwargs):
            func_args = kwargs.get("args", [])
            func_kwargs = kwargs.get("kwargs", {})
            key = f"{func.__module__}.{func.__name__}.{key_generator(*func_args, **func_kwargs)}"
            # Use countdown for expiration times on counter
            kwargs["countdown"] = kwargs.get(
                "countdown", settings.TASK_DEBOUNCE_DELAY
            )  # Defaults to 30
            countdown = kwargs["countdown"]
            # redis-cache incr treats None as 0
            try:
                with get_redis_connection() as con:
                    call_count = con.incr(CACHE_PREFIX + key)
                # Reset expiration on each call
                # Only redis-cache supports expire
                cache.expire(key, timeout=countdown + 1)
            # django cache requires two non-atomic calls
            except NotImplementedError:
                # Fallback method is limited and may execute more than desired
                cache.add(key, 0, countdown)
                call_count = cache.incr(key)
            if call_count == 1:
                kwargs["countdown"] = 0  # Execute first task immediately
            # Task should never expire, but better to expire if workers are overloaded
            # than to queue up and break further
            kwargs["expire"] = countdown * 100
            func_kwargs.update({"key": key, "call_count": call_count})
            kwargs["args"] = func_args
            kwargs["kwargs"] = func_kwargs
            return func.apply_async(**kwargs)

        return wrapper

    return decorator
