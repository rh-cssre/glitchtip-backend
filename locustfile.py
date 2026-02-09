from events.test_data.event_generator import generate_random_event
from locust import HttpUser, between, task


class WebsiteUser(HttpUser):
    wait_time = between(1.0, 2.0)

    @task
    def send_event(self):
        project_id = "1"
        dsn = ""
        event_url = f"/api/{project_id}/store/?sentry_key={dsn}"
        event = generate_random_event(True)
        self.client.post(event_url, json=event)
