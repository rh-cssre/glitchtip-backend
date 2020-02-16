from locust import HttpLocust, TaskSet, between

def send_event(l):
    l.client.get("/")

class UserBehavior(TaskSet):
    tasks = {send_event: 1}

class WebsiteUser(HttpLocust):
    task_set = UserBehavior
    wait_time = between(5.0, 9.0)
