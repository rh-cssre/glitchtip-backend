import datetime
import random
import string
import uuid

from .models import TransactionEvent, TransactionGroup

TRANSACTIONS = [
    "generic WSGI request",
    "/admin",
    "/admin/login/",
    "/",
    "/favicon.ico",
    "/foo",
    "/bar",
]

OPS = [
    "http.server",
    "pageload",
    "http",
    "browser",
    "db",
    "django.middleware",
    "django.view",
    "django.foo",
    "django.bar",
]

METHODS = [
    "GET",
    "POST",
    "PATCH",
    "PUT",
    "DELETE",
]

RELEASES = [
    None,
    "1.0",
    "1.1",
    "1.2",
    "1.3",
    "2.0",
    "2.1",
]

ENVIRONMENTS = [None, "local", "dev", "staging", "production"]


def maybe_random_string():
    if random.getrandbits(6) == 0:  # small chance
        return "".join(random.choices(string.ascii_uppercase + string.digits, k=20))


def generate_random_transaction():
    return maybe_random_string() or random.choice(TRANSACTIONS)


def generate_random_op():
    randbits = random.getrandbits(3)
    if randbits == 0:  # Favor http.server
        return "http.server"
    return maybe_random_string() or random.choice(OPS)


def generate_random_method():
    return random.choice(METHODS)


def generate_random_timestamp(start_timestamp):
    """
    Generate a realistic looking random time interval
    small chance between 0 and 30 seconds
    most will be between 0 and 2 seconds
    """
    if random.getrandbits(3) == 0:
        interval = random.randint(0, 30000)
    else:
        interval = random.randint(0, 2000)
    return start_timestamp + datetime.timedelta(milliseconds=interval)


def generate_fake_transaction_event(project, start_timestamp):
    """
    Generate random transaction and return result (unsaved)
    Will get_or_create the transaction group, function will result in queries
    """
    op = generate_random_op()
    method = None
    if op == "http.server":
        method = generate_random_method()
    group, _ = TransactionGroup.objects.get_or_create(
        transaction=generate_random_transaction(),
        project=project,
        op=op,
        method=method,
    )
    timestamp = generate_random_timestamp(start_timestamp)
    tags = {}
    if release := random.choice(RELEASES):
        tags["release"] = release
    if environment := random.choice(ENVIRONMENTS):
        tags["environment"] = environment

    return TransactionEvent(
        group=group,
        trace_id=uuid.uuid4(),
        start_timestamp=start_timestamp,
        data={},
        timestamp=timestamp,
        duration=timestamp - start_timestamp,
        tags=tags,
    )
