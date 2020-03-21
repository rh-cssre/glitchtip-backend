import copy
import uuid
import random
from django.utils import timezone
from . import django_error_factory
from .csp import mdn_sample_csp


def make_event_unique(event):
    """ Assign event a random new event_id and current timestamp """
    new_event = copy.deepcopy(event)
    new_event["event_id"] = uuid.uuid4().hex
    new_event["timestamp"] = timezone.now().isoformat()
    return new_event


def generate_random_event():
    """ Return a random event from library of samples with unique event id """

    events = django_error_factory.all_django_events
    events.append(mdn_sample_csp)
    event = random.choice(events)
    result = make_event_unique(event)

    return result
