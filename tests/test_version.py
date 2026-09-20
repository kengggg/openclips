import re
from pathlib import Path

import openclips


def test_version_matches_pyproject():
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    m = re.search(r'^version = "([^"]+)"$', text, re.M)
    assert m, "no version in pyproject.toml"
    assert openclips.__version__ == m.group(1)
