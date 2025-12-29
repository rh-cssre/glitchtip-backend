"""
All-in-one GlitchTip process
Fine to scale beyond 1 instance
Larger instances should consider dedicated resources, using bin/run-* scripts
"""

import contextlib
import logging
import os
import signal
import threading
import time

import django
import uvicorn
from django.conf import settings
from django.core.management import call_command
from django.db import connections

from glitchtip.celery import app

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "glitchtip.settings")
django.setup()
log = logging.getLogger(__name__)


CELERY_BEAT_LOCK_ID = 4724754730  # A random unique number for GlitchTip


class Server(uvicorn.Server):
    def install_signal_handlers(self):
        pass

    @contextlib.contextmanager
    def run_in_thread(self):
        thread = threading.Thread(target=self.run)
        thread.start()
        try:
            while not self.started:
                time.sleep(1e-3)
            yield
        finally:
            self.should_exit = True
            thread.join()


def run_celery_worker(stop_event: threading.Event):
    # We restart the worker periodically to mitigate celery memory leaks
    while not stop_event.is_set():
        worker = app.Worker(pool="threads", loglevel="info")
        worker_thread = threading.Thread(target=worker.start)
        worker_thread.start()

        # Run for 6 hours
        for _ in range(60 * 60 * 6):
            if stop_event.is_set():
                break
            time.sleep(1)

        # Stop the worker
        if worker_thread.is_alive():
            app.control.broadcast("shutdown", reply=True, destination=[worker.hostname])
            worker_thread.join()

        if stop_event.is_set():
            break


def run_celery_beat():
    # Get a lock to ensure only one instance of Celery Beat runs at a time
    connection = connections.create_connection("default")
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT pg_advisory_lock({CELERY_BEAT_LOCK_ID})")
        log.info("Lock acquired. Starting Celery Beat...")
    app.Beat().run(schedule="/tmp/celerybeat-schedule")


def run_django_server(stop_event: threading.Event):
    config = uvicorn.Config(
        "glitchtip.asgi:application",
        workers=int(os.environ.get("WEB_CONCURRENCY", 1)),
        host="0.0.0.0",
        port=8000,
        log_level="info",
        lifespan="off",
    )
    server = Server(config=config)
    with server.run_in_thread():
        while not stop_event.is_set():
            time.sleep(1)


def run_init():
    call_command("migrate", no_input=True, skip_checks=True)
    if "django.contrib.sessions" in settings.INSTALLED_APPS:
        call_command("createcachetable")


def run_pgpartition(stop_event: threading.Event):
    """Run every 12 hours. Handle sigterms cleanly"""
    while not stop_event.is_set():
        call_command("pgpartition", yes=True)
        for _ in range(12 * 60 * 60):
            if stop_event.is_set():
                break
            time.sleep(1)


def handle_signal(stop_event: threading.Event):
    def _handler(sig, frame):
        stop_event.set()
        exit(0)

    return _handler


def main():
    run_init()

    stop_event = threading.Event()
    signal.signal(signal.SIGTERM, handle_signal(stop_event))
    signal.signal(signal.SIGINT, handle_signal(stop_event))

    threads = [
        threading.Thread(target=run_celery_worker, args=(stop_event,)),
        # Force beat thread to halt
        threading.Thread(target=run_celery_beat, daemon=True),
        threading.Thread(target=run_pgpartition, args=(stop_event,)),
        threading.Thread(target=run_django_server, args=(stop_event,)),
    ]

    for thread in threads:
        thread.start()

    # celery worker gets to be the single process handles a simplistic sigterm
    threads[0].join()


if __name__ == "__main__":
    main()
