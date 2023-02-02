import random
from model_bakery.recipe import Recipe
from model_bakery.random_gen import gen_string

from events.models import Event
from .models import Issue


title_choices = [
    "OperationalError: string is too long for tsvector",
    "HttpErrorResponse: Http failure response for https://app.glitchtip.com/api/0/organizations/: 403 OK",
    'Error: Uncaught (in promise): at: {"headers":{"normalizedNames":{},"lazyUpdate":null},"status":403,"statusText":"OK","url":"(/api/0/api-tokens/","ok":false,"name":"HttpErrorResponse"',
]

culprit_chopices = [
    "style-src cdn.example.com",
    "/message/",
    "http://localhost",
    "?(src/index)",
]


metadata_choices = [
    {
        "type": "InvalidCursorName",
        "value": 'cursor "_django_curs_140385757349504_sync_1" does not exist\n',
        "filename": "django/db/models/sql/compiler.py",
        "function": "cursor_iter",
    },
    {
        "type": "SyntaxError",
        "value": "invalid syntax (views.py, line 165)",
        "filename": "organizations_ext/urls.py",
        "function": "<module>",
    },
    {
        "type": "TransactionGroup.MultipleObjectsReturned",
        "value": "get() returned more than one TransactionGroup -- it returned 2!",
        "filename": "django/db/models/query.py",
        "function": "get",
    },
]

tag_choices = [
    {
        "browser": ["Chrome 109.0.0"],
        "os.name": ["Windows"],
        "release": ["glitchtip@3.0.4"],
        "environment": ["prod"],
        "browser.name": ["Chrome"],
    },
    {"release": ["glitchtip@test1"], "environment": ["staging"]},
    {
        "release": ["glitchtip@3.0.3", "glitchtip@3.0.4"],
        "environment": ["prod"],
    },
]

browser_choices = ["Chrome", "Firefox", "Edge", "Opera"]
os_choices = ["Linux", "Windows", "FreeBSD", "Android"]
environment_choices = ["prod", "staging", "testing", "local"]


def gen_string_50():
    return gen_string(50)


def choice_or_random(choices, generator=gen_string_50):
    if random.getrandbits(1):
        return random.choice(choices)
    return generator()


def gen_title(*args, **kwargs):
    # Add 8 random chars to end, to make unique
    return choice_or_random(title_choices) + gen_string(8)


def gen_culprit(*args, **kwargs):
    return choice_or_random(culprit_chopices)


def gen_random_metadata():
    if random.getrandbits(1):
        return {}
    return {
        "type": gen_string(20),
        "value": gen_string(30),
        "filename": gen_string(10),
        "function": gen_string(6),
    }


def gen_metadata():
    return choice_or_random(metadata_choices, gen_random_metadata)


def gen_version():
    return f"{random.randint(1, 300)}.{random.randint(0, 9)}"


def gen_tags():
    tags = {}
    if random.getrandbits(1):
        browser = random.choice(browser_choices)
        tags["browser.name"] = browser
        if random.getrandbits(1):
            tags["browser"] = f"browser {gen_version()}"
    if random.getrandbits(1):
        tags["release"] = gen_version()
    if random.getrandbits(1):
        tags["environment"] = random.choices(environment_choices)
    if random.getrandbits(1):
        tags["os.name"] = random.choice(os_choices)

    return tags


issue_recipe = Recipe(
    Issue, title=gen_title, culprit=gen_culprit, metadata=gen_metadata, tags=gen_tags
)

event_recipe = Recipe(Event)
