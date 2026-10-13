import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()  # before the app import: it opens the DB on import
os.environ["SEMANTIC_MODEL"] = ""  # no 240 MB download in tests; test_meaning stubs the model


import pytest  # noqa: E402


@pytest.fixture(scope="session")
def live():
    """A client with the lifespan running (the MCP session manager starts once per process)."""
    from fastapi.testclient import TestClient
    from inventory.app import app
    with TestClient(app) as c:
        yield c
