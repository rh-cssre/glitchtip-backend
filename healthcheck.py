#!/usr/bin/env python3
"""Simple healthcheck script for Docker healthcheck."""

import sys
import urllib.request

try:
    with urllib.request.urlopen(
        "http://localhost:8000/_health/", timeout=5
    ) as response:
        if response.status == 200:
            sys.exit(0)
except Exception:
    pass

sys.exit(1)
