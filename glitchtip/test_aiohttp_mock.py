"""Guards on the aiohttp test double's own contract.

Tests elsewhere lean on three properties of ``mock_aiohttp`` that are easy to
weaken without noticing, because weakening them makes tests pass rather than
fail. A request nobody registered has to raise: that is what turns "only the
rebased URL is registered" into a real assertion that the importer never
contacts the foreign host, and a double that answered everything with a 200
would retire that test silently. Ordering and ``repeat`` are what let a test
script a retry sequence.
"""

import aiohttp
from django.test import SimpleTestCase

from glitchtip.test_utils.aiohttp_mock import mock_aiohttp

URL = "https://example.com/resource"


class MockAiohttpTests(SimpleTestCase):
    async def test_unregistered_request_raises(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL)
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.get("https://elsewhere.example/")

    async def test_method_and_query_are_part_of_the_match(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL)
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.post(URL)
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.get(URL, params={"cursor": "abc"})

    async def test_query_reaches_the_same_registration_either_spelling(self):
        with mock_aiohttp() as mocked:
            mocked.get(f"{URL}?b=2&a=1", status=204)
            async with aiohttp.ClientSession() as session:
                response = await session.get(URL, params={"a": "1", "b": "2"})
        self.assertEqual(response.status, 204)

    async def test_registrations_are_consumed_in_order(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL, status=429)
            mocked.get(URL, status=200)
            async with aiohttp.ClientSession() as session:
                first = await session.get(URL)
                second = await session.get(URL)
                with self.assertRaises(aiohttp.ClientConnectionError):
                    await session.get(URL)
        self.assertEqual([first.status, second.status], [429, 200])

    async def test_repeat_answers_every_further_request(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL, status=500, repeat=True)
            async with aiohttp.ClientSession() as session:
                statuses = [(await session.get(URL)).status for _ in range(3)]
        self.assertEqual(statuses, [500, 500, 500])

    async def test_body_is_readable_as_text_json_and_stream(self):
        with mock_aiohttp() as mocked:
            mocked.get(URL, payload={"id": 1}, repeat=True)
            async with aiohttp.ClientSession() as session:
                self.assertEqual(await (await session.get(URL)).json(), {"id": 1})
                self.assertEqual(await (await session.get(URL)).text(), '{"id": 1}')
                response = await session.get(URL)
                self.assertEqual(await response.content.read(4), b'{"id')
                self.assertEqual(await response.content.read(100), b'": 1}')

    async def test_requests_are_recorded_with_their_payload(self):
        with mock_aiohttp() as mocked:
            mocked.post(URL)
            async with aiohttp.ClientSession() as session:
                await session.post(URL, data={"mode": "flexible"})
        self.assertEqual(mocked.requests[0].method, "POST")
        self.assertEqual(mocked.requests[0].kwargs["data"], {"mode": "flexible"})

    def test_patch_is_removed_on_exit(self):
        original = aiohttp.ClientSession.__dict__["_request"]
        with mock_aiohttp():
            self.assertIsNot(aiohttp.ClientSession.__dict__["_request"], original)
        self.assertIs(aiohttp.ClientSession.__dict__["_request"], original)
