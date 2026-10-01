"""Environment every test module expects, set before any of them imports.

config.py and database.py read the environment once, at import. Each test
file used to set what it needed at its own top -- which only worked for
whichever file pytest happened to import first: put any file that imports
config ahead of test_app.py (`pytest tests/test_discord_roster.py
tests/test_app.py`) and DEV_LOGIN was already read as off, failing over a
hundred tests for a reason unrelated to any of them. pytest loads this
file before collecting the others, so the order can't matter.

setdefault throughout, so a value set on purpose outside pytest wins.
"""
import os
import tempfile

os.environ.setdefault("SITE_DB_PATH",
                      os.path.join(tempfile.mkdtemp(prefix="proclubs-tests-"), "site.db"))
os.environ.setdefault("DEV_LOGIN", "1")
os.environ.setdefault("SESSION_SECRET", "test-secret")
os.environ.setdefault("HTTPS_ONLY", "")
