from model_bakery import baker

from glitchtip.test_utils.test_case import GlitchTipTestCase

from ..serializers import SpanSerializer, TransactionEventSerializer


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
            "start_timestamp": "2020-12-29T17:51:05.458023Z",
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
        self.assertEqual(transaction.duration, 3010.085)


class SpanSerializerTestCase(GlitchTipTestCase):
    def setUp(self):
        self.create_user_and_project()

    def test_description_is_optional(self):
        project = self.project
        project.release_id = None
        project.environment_id = None
        transaction = baker.make("performance.TransactionEvent", group__project=project)
        data = {
            "span_id": "d390335b84e74948",
            "trace_id": "581eb3bc1f4740eea53717cb7f7450f6",
            "start_timestamp": "2023-05-22T14:58:15.703399Z",
            "parent_span_id": "f9d24c19d5174f61",
            "timestamp": "2023-05-22T14:58:15.703515Z",
            "op": "sentry.sent",
        }
        serializer = SpanSerializer(
            data=data, context={"request": {}, "project": project}
        )

        self.assertTrue(serializer.is_valid())
        serializer.save(transaction=transaction)
