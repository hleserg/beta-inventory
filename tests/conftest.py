import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()  # before the app import: it opens the DB on import
os.environ["SEMANTIC_MODEL"] = ""  # no 240 MB download in tests; test_meaning stubs the model
