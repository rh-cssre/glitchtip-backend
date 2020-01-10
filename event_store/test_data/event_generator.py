import uuid
import random
from . import django_error_factory


def make_event_unique(event):
    """ Assign event a random new event_id """
    event["event_id"] = uuid.uuid4().hex
    return event


def generate_random_event():
    """ Return a random event from library of samples with unique event id """
    event = random.choice(django_error_factory.all_django_events)
    return make_event_unique(event)
