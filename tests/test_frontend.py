"""Exercise frontend event routing through Node's built-in test runner."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_clarification_form_routes_to_the_review_decision_after_editing():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for frontend listener tests")
    result = subprocess.run(
        [node, "--test", str(Path(__file__).parent / "frontend" / "clarification.test.cjs")],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
