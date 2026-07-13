import datetime
import random
import string

from django.utils import timezone

from .histogram import merge_durations, new_histogram, percentile_from_histogram
from .models import TransactionGroup

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


def generate_random_duration_ms():
    """
    Generate a realistic looking random duration in milliseconds.
    small chance between 0 and 30 seconds
    most will be between 0 and 2 seconds
    """
    if random.getrandbits(3) == 0:
        return random.randint(0, 30000)
    return random.randint(0, 2000)


def generate_fake_transaction_group(project):
    """
    Generate a random TransactionGroup with realistic stats.
    Will get_or_create the group.
    """
    op = generate_random_op()
    method = ""
    if op == "http.server":
        method = generate_random_method()

    now = timezone.now()

    group, created = TransactionGroup.objects.get_or_create(
        transaction=generate_random_transaction(),
        project=project,
        op=op,
        method=method,
        defaults={
            "organization": project.organization,
            "first_seen": now - datetime.timedelta(days=random.randint(1, 30)),
            "last_seen": now,
        },
    )

    if created:
        # Simulate some historical data
        count = random.randint(10, 1000)
        sample_size = min(count, 100)
        durations = [generate_random_duration_ms() for _ in range(sample_size)]
        histogram = new_histogram()
        merge_durations(histogram, durations)
        avg = sum(durations) / len(durations)
        # Use sample_size (histogram total) for percentile, not count
        p50 = percentile_from_histogram(histogram, sample_size, 50)
        p95 = percentile_from_histogram(histogram, sample_size, 95)

        group.count = count
        group.avg_duration = avg
        group.p50 = p50
        group.p95 = p95
        group.error_count = random.randint(0, count // 10)
        group.duration_histogram = histogram
        group.save()

    return group
