from locust import HttpUser, between, task
from event_store.test_data.event_generator import generate_random_event


class WebsiteUser(HttpUser):
    wait_time = between(1.0, 2.0)

    @task
    def send_event(self):
        project_id = "6"
        dsn = "244703e8083f4b16988c376ea46e9a08"
        event_url = f"/api/{project_id}/store/?sentry_key={dsn}"
        event = generate_random_event()
        self.client.post(event_url, json=event)
