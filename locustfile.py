from locust import HttpLocust, TaskSet, between
from event_store.test_data.event_generator import generate_random_event


def send_event(l):
    project_id = "1"
    # dsn = "d74b8844b5664a7da945b74c98b8301b"
    dsn = "28b72f66826d4c818a40455085d23382"
    event_url = f"/api/{project_id}/store/?sentry_key={dsn}"
    event = generate_random_event()
    l.client.post(event_url, json=event)


class UserBehavior(TaskSet):
    tasks = {send_event: 1}


class WebsiteUser(HttpLocust):
    task_set = UserBehavior
    wait_time = between(1.0, 2.0)
