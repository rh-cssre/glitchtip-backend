import os
import platform
import shutil
import subprocess
import sys
import time
from urllib.parse import urlparse

import requests

GLITCHTIP_CLI_VERSION = "v0.1.0"
GLITCHTIP_CLI_RELEASE_URL = (
    "https://gitlab.com/glitchtip/glitchtip-cli/-/jobs/artifacts/"
    "{version}/raw/artifacts/glitchtip-cli-linux-{arch}"
    "?job=build-linux-{arch}"
)

BASE_URL = os.getenv("GLITCHTIP_URL", "http://localhost:8000")
EMAIL = f"e2e_{int(time.time())}@example.com"
PASSWORD = "password123!"
ORG_NAME = "e2e-org"
TEAM_NAME = "e2e-team"
PROJECT_NAME = "e2e-project"

session = requests.Session()


def install_glitchtip_cli():
    if shutil.which("glitchtip-cli"):
        print("glitchtip-cli already installed.")
        return

    bin_dir = os.path.abspath("bin")
    os.makedirs(bin_dir, exist_ok=True)
    binary_path = os.path.join(bin_dir, "glitchtip-cli")

    if not os.path.exists(binary_path):
        arch = "arm64" if platform.machine() in ("arm64", "aarch64") else "x86_64"
        url = GLITCHTIP_CLI_RELEASE_URL.format(version=GLITCHTIP_CLI_VERSION, arch=arch)
        print(f"Downloading glitchtip-cli {GLITCHTIP_CLI_VERSION} from {url}")
        subprocess.check_call(["curl", "-sSfL", "-o", binary_path, url])
        os.chmod(binary_path, 0o755)

    os.environ["PATH"] += os.pathsep + bin_dir


def wait_for_api():
    print(f"Waiting for API at {BASE_URL}...")
    start_time = time.time()
    while time.time() - start_time < 60:
        try:
            resp = requests.get(f"{BASE_URL}/_health/")
            if resp.status_code == 200:
                print("API is ready.")
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(1)
    raise Exception("API failed to come online.")


def update_csrf():
    if "csrftoken" in session.cookies:
        session.headers.update({"X-CSRFToken": session.cookies["csrftoken"]})


def register_and_login():
    print(f"Registering user {EMAIL}...")
    # 1. Get CSRF token
    resp = session.get(f"{BASE_URL}/_allauth/browser/v1/config")

    # 2. Signup
    signup_data = {
        "email": EMAIL,
        "password": PASSWORD,
    }
    update_csrf()

    resp = session.post(f"{BASE_URL}/_allauth/browser/v1/auth/signup", json=signup_data)
    if resp.status_code == 409:  # Conflict, already exists?
        print("User already exists, trying login...")
        login_data = {"email": EMAIL, "password": PASSWORD}
        update_csrf()
        resp = session.post(
            f"{BASE_URL}/_allauth/browser/v1/auth/login", json=login_data
        )

    if resp.status_code not in [200, 201]:
        print(f"Registration/Login failed: {resp.text}")
        sys.exit(1)

    print("Logged in.")
    # Verify we are logged in by hitting /api/0/users/me/
    resp = session.get(f"{BASE_URL}/api/0/users/me/")
    if resp.status_code != 200:
        print(f"Failed to get user info: {resp.text}")
        sys.exit(1)
    print(f"User ID: {resp.json()['id']}")


def create_org():
    print("Creating organization...")
    update_csrf()
    resp = session.post(f"{BASE_URL}/api/0/organizations/", json={"name": ORG_NAME})
    if resp.status_code == 201:
        return resp.json()
    elif resp.status_code == 400 and "already exists" in resp.text:
        # Fetch existing
        resp = session.get(f"{BASE_URL}/api/0/organizations/")
        for org in resp.json():
            if org["name"] == ORG_NAME:
                return org

    print(f"Failed to create org: {resp.text}")
    sys.exit(1)


def create_team(org_slug):
    print("Creating team...")
    update_csrf()
    resp = session.post(
        f"{BASE_URL}/api/0/organizations/{org_slug}/teams/", json={"slug": TEAM_NAME}
    )
    if resp.status_code == 201:
        return resp.json()
    elif resp.status_code == 400 and "already exists" in resp.text:
        return {"slug": TEAM_NAME}  # Assume it exists

    print(f"Failed to create team: {resp.text}")
    sys.exit(1)


def create_project(org_slug, team_slug):
    print("Creating project...")
    update_csrf()
    resp = session.post(
        f"{BASE_URL}/api/0/teams/{org_slug}/{team_slug}/projects/",
        json={"name": PROJECT_NAME},
    )
    if resp.status_code == 201:
        return resp.json()
    elif resp.status_code == 400 and "already exists" in resp.text:
        # Get existing
        resp = session.get(f"{BASE_URL}/api/0/projects/{org_slug}/{PROJECT_NAME}/")
        if resp.status_code == 200:
            return resp.json()

    print(f"Failed to create project: {resp.text}")
    sys.exit(1)


def get_dsn(org_slug, project_slug):
    print("Getting DSN...")
    resp = session.get(f"{BASE_URL}/api/0/projects/{org_slug}/{project_slug}/keys/")
    if resp.status_code == 200:
        keys = resp.json()
        if keys:
            dsn = keys[0]["dsn"]["public"]
            # In CI, the DSN might have localhost but we need to use the actual host
            # Replace localhost with the host from BASE_URL
            base_parsed = urlparse(BASE_URL)
            dsn_parsed = urlparse(dsn)
            if (
                dsn_parsed.hostname == "localhost"
                and base_parsed.hostname != "localhost"
            ):
                # Replace localhost with the actual host
                dsn = dsn.replace(
                    f"localhost:{dsn_parsed.port or 80}",
                    f"{base_parsed.hostname}:{base_parsed.port or dsn_parsed.port or 80}",
                )
            print(f"DSN: {dsn}")
            return dsn
    print(f"Failed to get DSN: {resp.text}")
    sys.exit(1)


def send_event(dsn):
    print("Sending test event via glitchtip-cli...")
    env = os.environ.copy()
    env["SENTRY_DSN"] = dsn
    try:
        output = subprocess.check_output(
            ["glitchtip-cli", "send-event", "-m", "Hello GlitchTip E2E"], env=env
        )
        print(f"Event sent: {output.decode()}")
        # glitchtip-cli prints the event id as the last whitespace-delimited
        # token on stdout.
        event_id = output.decode().strip().split()[-1]
        return event_id
    except subprocess.CalledProcessError as e:
        print(f"Failed to send event: {e.output.decode()}")
        sys.exit(1)


def verify_event(org_slug, project_slug, event_id):
    print(f"Verifying event {event_id}...")
    # Poll for the event
    start_time = time.time()
    while time.time() - start_time < 120:
        # Issues endpoint: /api/0/projects/{org_slug}/{project_slug}/issues/
        # Or search?
        # Let's list issues and check latest.
        resp = session.get(
            f"{BASE_URL}/api/0/projects/{org_slug}/{project_slug}/issues/"
        )
        if resp.status_code == 200:
            issues = resp.json()
            if issues:
                print(f"Found {len(issues)} issues")
                # Check if our message is there?
                # "Hello GlitchTip E2E"
                for issue in issues:
                    if "Hello GlitchTip E2E" in issue["title"]:
                        print("Event found!")
                        return
            else:
                print("No issues found yet.")
        else:
            print(f"Failed to fetch issues: {resp.status_code} {resp.text}")

        time.sleep(2)
        print("Waiting for worker...")

    print("Event verification failed.")
    sys.exit(1)


def run():
    install_glitchtip_cli()
    wait_for_api()
    register_and_login()
    org = create_org()
    org_slug = org["slug"]
    team = create_team(org_slug)
    team_slug = team["slug"]
    project = create_project(org_slug, team_slug)
    project_slug = project["slug"]
    dsn = get_dsn(org_slug, project_slug)
    event_id = send_event(dsn)
    verify_event(org_slug, project_slug, event_id)


if __name__ == "__main__":
    run()
