"""Skip a test when an external binary is missing -- locally only.

In CI (``CI=true``, set by GitHub Actions) the marker never skips: the workflow installs these
binaries, so a missing one is a broken runner, and a silent skip would hide an untested OCR or
queue path. The test then runs and fails on the missing tool.
"""

import os
import shutil

import pytest


def needs(*tools: str) -> pytest.MarkDecorator:
    missing = [t for t in tools if shutil.which(t) is None]
    in_ci = os.environ.get("CI", "").lower() == "true"
    return pytest.mark.skipif(
        bool(missing) and not in_ci, reason=f"needs {', '.join(missing)} (never skipped in CI)",
    )
