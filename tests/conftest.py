"""Import helpers: the modules under test are scripts at the repo root, and one
of them (`studio/validate-reels.py`) has a filename that is not importable with
a plain `import`."""

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(name, relpath):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def watch():
    return _load("watch", "watch.py")


@pytest.fixture(scope="session")
def approve():
    return _load("approve", "approve.py")


@pytest.fixture(scope="session")
def publish():
    return _load("publish", "publish.py")


@pytest.fixture(scope="session")
def validate_reels():
    return _load("validate_reels", "studio/validate-reels.py")


class FakeResponse:
    """Minimal stand-in for the object `urllib.request.urlopen` returns."""

    def __init__(self, payload):
        self._body = payload if isinstance(payload, bytes) else payload.encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def fake_response():
    return FakeResponse
