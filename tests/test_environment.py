import sys
from pathlib import Path


def test_python_version():
    assert sys.version_info[:2] == (3, 12)


def test_project_structure():
    root = Path(__file__).resolve().parents[1]

    for folder in ("app", "data", "docs", "tests"):
        assert (root / folder).is_dir()


def test_virtual_environment():
    assert sys.prefix != sys.base_prefix
