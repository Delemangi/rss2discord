"""Guard the container's builder and runtime Python versions."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_builder_and_runtime_python_versions_match() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    builder = re.search(
        r"astral-sh/uv:python(?P<version>\d+\.\d+)",
        dockerfile,
    )
    runtime = re.search(
        r"^FROM python:(?P<version>\d+\.\d+)",
        dockerfile,
        flags=re.MULTILINE,
    )

    assert builder is not None
    assert runtime is not None
    assert builder.group("version") == runtime.group("version")


def test_local_python_version_matches_the_container() -> None:
    version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime = re.search(
        r"^FROM python:(?P<version>\d+\.\d+)",
        dockerfile,
        flags=re.MULTILINE,
    )

    assert runtime is not None
    assert version == runtime.group("version")
