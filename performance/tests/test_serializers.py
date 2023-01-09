from glitchtip.test_utils.test_case import GlitchTipTestCase
from ..serializers import TransactionEventSerializer


class TransactionEventSerializerTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_serializer_tags(self):
        project = self.project
        project.release_id = None
        project.environment_id = None
        data = {
            "tags": {"http.status_code": "200"},
            "timestamp": "2020-12-29T17:51:08.468108Z",
            "start_timestamp": "2020-12-29T17:51:08.458023Z",
            "contexts": {
                "trace": {
                    "trace_id": "581eb3bc1f4740eea53717cb7f7450f6",
                    "op": "http.server",
                }
            },
            "transaction": "/",
        }
        serializer = TransactionEventSerializer(
            data=data, context={"request": {}, "project": project}
        )
        self.assertTrue(serializer.is_valid())
        transaction = serializer.save()

        data["tags"] = {"http.status_code": "400", "new": "foo"}
        serializer = TransactionEventSerializer(
            data=data, context={"request": {}, "project": project}
        )
        self.assertTrue(serializer.is_valid())
        transaction = serializer.save()
        self.assertEqual(len(transaction.group.tags["http.status_code"]), 2)
        self.assertEqual(len(transaction.group.tags["new"]), 1)
