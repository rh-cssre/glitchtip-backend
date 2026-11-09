import random
import string

from django.db import connection
from django.test import TestCase


class TsVectorFunctionsTest(TestCase):
    """Test custom PostgreSQL functions related to tsvector."""

    def _generate_random_words(self, count):
        """
        Generates a list of unique random "words" to avoid tsvector
        deduplication from affecting the size of the resulting vector.
        """
        words = set()
        while len(words) < count:
            word = "".join(random.choices(string.ascii_lowercase, k=10))
            words.add(word)
        return list(words)

    def test_append_and_limit_tsvector_exceeds_limit(self):
        """
        Tests that append_and_limit_tsvector fails when the intermediate
        concatenated tsvector exceeds PostgreSQL's 1MB limit.

        This test is expected to FAIL with a django.db.utils.ProgrammingError
        (wrapping psycopg2.errors.ProgramLimitExceeded), which confirms the bug.
        When the bug is fixed, this test should pass.
        """
        # A tsvector is limited to ~1MB. A lexeme and its position info are
        # roughly 20 bytes on average. To create a vector > 1MB, we need
        # more than 1,048,576 / 20 = ~52,428 lexemes in total.
        # We'll generate two strings that each produce a tsvector of ~600KB.
        # 600 * 1024 / 20 = ~30,720 lexemes per string. We'll use 64,000
        # to be safe, aiming for roughly 1.1MB per vector.
        num_words = 64000

        long_string1 = " ".join(self._generate_random_words(num_words))
        long_string2 = " ".join(self._generate_random_words(num_words))

        with connection.cursor() as cursor:
            # The following call is expected to raise a ProgrammingError because
            # the intermediate tsvector created by the `||` operator inside the
            # function exceeds the 1MB size limit before truncation.
            cursor.execute(
                """
                SELECT append_and_limit_tsvector(
                    to_tsvector('english', %s),
                    %s,
                    %s,
                    'english'::regconfig
                )
                """,
                [long_string1, long_string2, 16384],  # max_lexemes
            )

        # If the query succeeds without error, it means the bug might be fixed.
        # This assertion will pass, confirming the successful execution.
        self.assertTrue(
            True, "The function call succeeded, indicating the bug might be fixed."
        )
