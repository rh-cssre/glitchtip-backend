from locust import HttpUser, between, task
from event_store.test_data.event_generator import generate_random_event


class WebsiteUser(HttpUser):
    wait_time = between(1.0, 2.0)

    @task
    def send_event(self):
        project_id = "248"
        dsn = "5174afbcc4ad47f287bc28696bfb170f"
        event_url = f"/api/{project_id}/store/?sentry_key={dsn}"
        event = generate_random_event(True)
        self.client.post(event_url, json=event)
