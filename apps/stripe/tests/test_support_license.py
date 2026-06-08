from django.test import TestCase, override_settings

from apps.stripe.models import SupportLicense


class SupportLicenseSingletonTestCase(TestCase):
    def test_save_forces_pk_one(self):
        sl = SupportLicense(license_key="sub_first")
        sl.save()
        self.assertEqual(sl.pk, 1)

        sl2 = SupportLicense(license_key="sub_second")
        sl2.save()
        self.assertEqual(sl2.pk, 1)
        self.assertEqual(SupportLicense.objects.count(), 1)
        self.assertEqual(SupportLicense.objects.get().license_key, "sub_second")

    async def test_load_creates_when_missing(self):
        self.assertEqual(await SupportLicense.objects.acount(), 0)
        sl = await SupportLicense.load()
        self.assertEqual(sl.pk, 1)
        self.assertEqual(sl.license_key, "")
        self.assertEqual(await SupportLicense.objects.acount(), 1)

    async def test_load_returns_existing(self):
        await SupportLicense(
            license_key="sub_existing", billing_email="a@b.co"
        ).asave()
        sl = await SupportLicense.load()
        self.assertEqual(sl.license_key, "sub_existing")
        self.assertEqual(sl.billing_email, "a@b.co")


class SupportLicenseResolverTestCase(TestCase):
    @override_settings(BILLING_ENABLED=True)
    async def test_billing_enabled_short_circuits_to_empty(self):
        await SupportLicense(
            license_key="sub_dbKey", billing_email="db@example.com"
        ).asave()
        self.assertEqual(await SupportLicense.resolved(), ("", ""))

    @override_settings(BILLING_ENABLED=False)
    async def test_returns_db_values(self):
        await SupportLicense(
            license_key="sub_dbKey", billing_email="db@example.com"
        ).asave()
        self.assertEqual(
            await SupportLicense.resolved(), ("sub_dbKey", "db@example.com")
        )

    @override_settings(BILLING_ENABLED=False)
    async def test_empty_db_returns_empty(self):
        self.assertEqual(await SupportLicense.resolved(), ("", ""))
