from pathlib import Path

import tomllib

import openclips


def test_version_matches_pyproject():
    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    assert openclips.__version__ == data["project"]["version"]
