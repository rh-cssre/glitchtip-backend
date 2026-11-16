from apps.shared.schema.exception import MAX_VAR_SIZE, StackTraceFrame


class TestStackTraceFrameVarsTruncation:
    def test_small_vars_unchanged(self):
        """Vars within size limit should not be modified."""
        frame = StackTraceFrame(
            filename="test.py",
            vars={"small": "ok", "number": "123", "normal": "x" * 1000},
        )
        assert frame.vars["small"] == "ok"
        assert frame.vars["number"] == "123"
        assert frame.vars["normal"] == "x" * 1000

    def test_large_string_var_truncated(self):
        """String vars exceeding MAX_VAR_SIZE should be truncated."""
        large_value = "x" * 100000
        frame = StackTraceFrame(
            filename="test.py",
            vars={"small": "ok", "huge": large_value},
        )
        assert frame.vars["small"] == "ok"
        assert frame.vars["huge"] == f"[Truncated: {len(large_value)} bytes]"

    def test_large_dict_var_truncated(self):
        """Dict vars exceeding MAX_VAR_SIZE when serialized should be truncated."""
        large_dict = {"data": "x" * 100000}
        frame = StackTraceFrame(
            filename="test.py",
            vars={"payload": large_dict},
        )
        assert frame.vars["payload"].startswith("[Truncated:")
        assert "bytes]" in frame.vars["payload"]

    def test_large_list_var_truncated(self):
        """List vars exceeding MAX_VAR_SIZE when serialized should be truncated."""
        large_list = ["x" * 10000 for _ in range(20)]
        frame = StackTraceFrame(
            filename="test.py",
            vars={"items": large_list},
        )
        assert frame.vars["items"].startswith("[Truncated:")
        assert "bytes]" in frame.vars["items"]

    def test_none_vars_unchanged(self):
        """None vars should remain None."""
        frame = StackTraceFrame(filename="test.py", vars=None)
        assert frame.vars is None

    def test_empty_vars_unchanged(self):
        """Empty vars dict should remain empty."""
        frame = StackTraceFrame(filename="test.py", vars={})
        assert frame.vars == {}

    def test_var_at_boundary(self):
        """Var exactly at MAX_VAR_SIZE should not be truncated."""
        boundary_value = "x" * MAX_VAR_SIZE
        frame = StackTraceFrame(
            filename="test.py",
            vars={"boundary": boundary_value},
        )
        assert frame.vars["boundary"] == boundary_value

    def test_var_just_over_boundary(self):
        """Var just over MAX_VAR_SIZE should be truncated."""
        over_value = "x" * (MAX_VAR_SIZE + 1)
        frame = StackTraceFrame(
            filename="test.py",
            vars={"over": over_value},
        )
        assert frame.vars["over"] == f"[Truncated: {len(over_value)} bytes]"
