import random


from glitchtip.test_utils.test_data import ENVIRONMENTS, RELEASES
from issues.models import EventType, Issue


def brower_tag_generator():
    return random.choice(BROWSER_TAGS)


SDKS = [
    {
        "version": "1.5.1",
        "name": "sentry.python",
        "packages": [{"version": "1.5.1", "name": "pypi:sentry-sdk"}],
        "integrations": [],
    },
    {
        "name": "sentry.javascript.react",
        "version": "7.36.0",
        "packages": [{"name": "npm:@sentry/react", "version": "7.36.0"}],
        "integrations": [
            "InboundFilters",
        ],
    },
    {"name": "sentry.php.laravel", "version": "2.9.0"},
]
BROWSER_TAGS = [
    {
        "device": "Desktop",
        "browser": "GlitchBot 1.2",
        "os.name": "Linux",
        "server_name": "glitch-server",
        "browser.name": "GlitchBot",
    },
    {
        "device": "Mobile",
        "browser": "Firefox 120",
        "os.name": "Android",
        "server_name": "glitch-server",
        "browser.name": "Firefox",
    },
    {
        "device": "Desktop",
        "browser": "bingbot 2.0",
        "os.name": "Other",
        "server_name": "cool-server",
        "browser.name": "bingbot",
    },
    {
        "browser": "Chrome 106.0.0",
        "os.name": "Windows",
        "release": "v1.0.0",
        "browser.name": "Chrome",
    },
]
CULPRITS = [
    "/src/Controller/FunController.php in App\Controller\FunController::showFun",
    "blarg.php in ?",
    "subscriber.contract.view.modal.block.unblock",
]
SERVER_TAGS = [
    {
        "os.name": ["Linux"],
        "server_name": ["ip-111-11-11-111.ec3.internal"],
    },
    {
        "server_name": ["11335c17-b136-1e4a-1241-e243221c1221"],
    },
]
TAG_CHOICES = BROWSER_TAGS + SERVER_TAGS
TITLE_CHOICES = [
    "The error has happened here",
    "ErrorException: Notice: Trying to access array offset on value of type null",
    "WorkerLostError: Worker exited prematurely: signal 15 (SIGTERM) Job: 1.",
    "ErrorException: Warning: filesize(): stat failed for /var/www/vhosts/example.com/private/me/data/1",
    "Project\Database\QueryException: SQLSTATE[22P02]: Invalid text representation: 7 ERROR:  invalid input syntax for type uuid: 12345",
    "OperationalError: ERROR:  no more connections allowed (max_client_conn)",
]


def generate_tag():
    tag = random.choice(TAG_CHOICES)
    if environment := random.choice(ENVIRONMENTS):
        tag["environment"] = environment
    release = random.choice(RELEASES)
