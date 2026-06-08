from django.test import TestCase

from apps.performance.parameterize import (
    parameterize_description,
    parameterize_sql,
)


class ParameterizeSQLTestCase(TestCase):
    def test_string_literals(self):
        sql = "SELECT * FROM users WHERE name = 'alice'"
        self.assertEqual(parameterize_sql(sql), "SELECT * FROM users WHERE name = %s")

    def test_numeric_literals(self):
        sql = "SELECT * FROM users WHERE id = 42 AND age > 18"
        self.assertEqual(
            parameterize_sql(sql),
            "SELECT * FROM users WHERE id = %s AND age > %s",
        )

    def test_float_literals(self):
        sql = "SELECT * FROM t WHERE price = 19.99"
        self.assertEqual(parameterize_sql(sql), "SELECT * FROM t WHERE price = %s")

    def test_in_list_collapse(self):
        sql = "SELECT * FROM t WHERE id IN (1, 2, 3)"
        result = parameterize_sql(sql)
        self.assertEqual(result, "SELECT * FROM t WHERE id IN (%s)")

    def test_mixed(self):
        sql = (
            "UPDATE t SET name = 'bob' WHERE id = 5 AND status IN ('active', 'pending')"
        )
        result = parameterize_sql(sql)
        self.assertEqual(
            result, "UPDATE t SET name = %s WHERE id = %s AND status IN (%s)"
        )

    def test_escaped_quotes(self):
        sql = r"SELECT * FROM t WHERE name = 'it\'s'"
        result = parameterize_sql(sql)
        self.assertEqual(result, "SELECT * FROM t WHERE name = %s")

    def test_empty_string(self):
        self.assertEqual(parameterize_sql(""), "")


class ParameterizeDescriptionTestCase(TestCase):
    def test_db_op(self):
        result = parameterize_description(
            "db.sql.query", "SELECT * FROM users WHERE id = 42"
        )
        self.assertEqual(result, "SELECT * FROM users WHERE id = %s")

    def test_http_op_numeric_path(self):
        result = parameterize_description("http.client", "/api/users/123/posts")
        self.assertEqual(result, "/api/users/%s/posts")

    def test_http_op_uuid_path(self):
        result = parameterize_description(
            "http.client",
            "/api/users/550e8400-e29b-41d4-a716-446655440000/profile",
        )
        self.assertEqual(result, "/api/users/%s/profile")

    def test_http_op_strips_query_string(self):
        result = parameterize_description("http.client", "/api/users?page=1&limit=10")
        self.assertEqual(result, "/api/users")

    def test_http_op_hex_hash_path(self):
        result = parameterize_description(
            "http.client",
            "/api/commits/abcdef1234567890abcdef1234567890abcdef12",
        )
        self.assertEqual(result, "/api/commits/%s")

    def test_other_op_numeric_path(self):
        result = parameterize_description("resource.img", "/images/12345/thumb")
        self.assertEqual(result, "/images/%s/thumb")

    def test_none_description(self):
        self.assertEqual(parameterize_description("db", None), "")

    def test_empty_description(self):
        self.assertEqual(parameterize_description("db", ""), "")

    def test_truncation(self):
        long_desc = "SELECT " + "x" * 600
        result = parameterize_description("db.sql.query", long_desc)
        self.assertEqual(len(result), 500)
