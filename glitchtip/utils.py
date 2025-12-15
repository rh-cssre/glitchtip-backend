import random
import string

from asgiref.sync import sync_to_async
from django.conf import settings


def get_random_string(length=16):
    letters = string.ascii_lowercase
    result_str = "".join(random.choice(letters) for i in range(length))
    return result_str


async def async_call_celery_task(task, *args):
    """
    Either dispatch the real celery task or run it with sync_to_async
    This can be used for testing or a celery-less operation.
    """
    if settings.CELERY_TASK_ALWAYS_EAGER:
        return await sync_to_async(task.delay)(*args)
    else:
        return task.delay(*args)
