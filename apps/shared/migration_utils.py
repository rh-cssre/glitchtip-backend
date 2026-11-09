import os


def get_sql_content(dir_file: str, filename: str):
    """Helper to read SQL from a file."""
    sql_dir = os.path.join(os.path.dirname(dir_file), "./sql")
    file_path = os.path.join(sql_dir, filename)
    with open(file_path, "r") as f:
        return f.read()
