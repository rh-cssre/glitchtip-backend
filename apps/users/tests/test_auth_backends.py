from django.contrib.auth import aauthenticate
from django.test import TestCase

from ..models import User


class ModelBackendTestCase(TestCase):
    """The subclassed ModelBackend must match Django's authentication
    behavior; only the unknown-user dummy-hash offload differs."""

    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            email="backend@example.com", password="hunter2-hunter2"
        )
        User.objects.create_user(
            email="inactive@example.com", password="hunter2-hunter2", is_active=False
        )

    async def test_valid_credentials(self):
        user = await aauthenticate(
            email="backend@example.com", password="hunter2-hunter2"
        )
        self.assertEqual(user, self.user)

    async def test_wrong_password(self):
        user = await aauthenticate(
            email="backend@example.com", password="wrong-password"
        )
        self.assertIsNone(user)

    async def test_unknown_email_burns_dummy_hash(self):
        user = await aauthenticate(
            email="nonexistent@example.com", password="hunter2-hunter2"
        )
        self.assertIsNone(user)

    async def test_inactive_user(self):
        user = await aauthenticate(
            email="inactive@example.com", password="hunter2-hunter2"
        )
        self.assertIsNone(user)

    async def test_missing_credentials(self):
        self.assertIsNone(await aauthenticate(email="backend@example.com"))
        self.assertIsNone(await aauthenticate(password="hunter2-hunter2"))
