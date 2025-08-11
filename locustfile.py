import datetime
import time

from celery.result import AsyncResult
from locust import HttpUser, between, task

from events.test_data.event_generator import generate_random_event
from glitchtip.celery import app


class CeleryClient:
    """
    CeleryClient is a wrapper around the Celery client.
    It proxies any function calls and fires the *request* event when they finish,
    so that the calls get recorded in Locust.
    """

    def __init__(self, request_event):
        self.client = app
        self.task_timeout = 60
        self._request_event = request_event

    def send_task(self, name, args=None, kwargs=None, queue=None):
        options = {}
        if queue:
            options["queue"] = queue

        request_meta = {
            "request_type": "celery",
            "response_length": 0,
            "name": name,
            "start_time": time.time(),
            "response": None,
            "context": {},
            "exception": None,
        }
        t0 = datetime.datetime.now()
        try:
            async_result = self.client.send_task(
                name, args=args, kwargs=kwargs, **options
            )
            result = async_result.get(self.task_timeout)  # blocking
            request_meta["response"] = result
            t1 = async_result.date_done
        except Exception as e:
            t1 = None
            request_meta["exception"] = e

        request_meta["response_time"] = (
            None if not t1 else (t1 - t0).total_seconds() * 1000
        )
        self._request_event.fire(
            **request_meta
        )  # this is what makes the request actually get logged in Locust
        return request_meta["response"]

    def monitor_task(self, task_name, task_id):
        """Monitor and record a known task by id"""
        request_meta = {
            "request_type": "celery",
            "response_length": 0,
            "name": task_name,
            "start_time": time.time(),
            "response": None,
            "context": {},
            "exception": None,
        }
        t0 = datetime.datetime.now()
        try:
            async_result = AsyncResult(task_id)
            async_result.wait(self.task_timeout)
            t1 = async_result.date_done
        except Exception as e:
            t1 = None
            request_meta["exception"] = e
        request_meta["response_time"] = (
            None if not t1 else (t1 - t0).total_seconds() * 1000
        )
        self._request_event.fire(
            **request_meta
        )  # this is what makes the request actually get logged in Locust
        return request_meta["response"]


class WebsiteUser(HttpUser):
    wait_time = between(1.0, 2.0)

    def __init__(self, environment):
        super().__init__(environment)
        self.celery_client = CeleryClient(
            request_event=environment.events.request,
        )

    @task
    def send_event(self):
        project_id = "1"
        dsn = ""
        event_url = f"/api/{project_id}/store/?sentry_key={dsn}"
        event = generate_random_event(True)
        res = self.client.post(event_url, json=event)
        task_id = res.json().get("task_id")
        self.celery_client.monitor_task("ingest_event", task_id)

    # @task
    # def test_debug_task(self):
    #     self.celery_client.send_task("glitchtip.celery.debug_task")
