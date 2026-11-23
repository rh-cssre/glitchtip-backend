import json
from timeit import default_timer as timer

from django.test import TestCase


class PerfTestCase(TestCase):
    def xtest_perf(self):
        data = json.dumps({"foo": "barrrrrr", "bar": 15544325, "lol": ["a", "c", "b"]})
        times = 10000

        url = "/api/echo/"
        self.client.post(url, data, content_type="application/json")
        print("ninja")
        start = timer()
        for _ in range(times):
            self.client.post(url, data, content_type="application/json")
        end = timer()
        print(end - start)

        print("view")
        url = "/api/echo_view/"
        self.client.post(url, data, content_type="application/json")
        start = timer()
        for _ in range(times):
            self.client.post(url, data, content_type="application/json")
        end = timer()
        print(end - start)

        print("DRF view")
        url = "/api/echo_class/"
        self.client.post(url, data, content_type="application/json")
        start = timer()
        for _ in range(times):
            self.client.post(url, data, content_type="application/json")
        end = timer()
        print(end - start)
