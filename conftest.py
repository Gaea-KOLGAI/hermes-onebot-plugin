import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HERMES_SRC = Path.home() / ".hermes" / "hermes-agent"

for path in (ROOT, HERMES_SRC):
    if path.exists():
        sys.path.insert(0, str(path))

# Unit tests must not read the live Hermes config.yaml.  The live config can
# contain ONEBOT_HTTP_API_URL / gateway platform blocks, which changes adapter
# routing and makes WS-focused tests accidentally take the HTTP path.
_TEST_HOME = Path(tempfile.mkdtemp(prefix="onebot-platform-test-"))
os.environ["HERMES_HOME"] = str(_TEST_HOME)
for _key in (
    "ONEBOT_WS_URL",
    "ONEBOT_ACCESS_TOKEN",
    "ONEBOT_WS_MODE",
    "ONEBOT_HTTP_API_URL",
    "ONEBOT_ALLOWED_USERS",
    "ONEBOT_GROUP_IDS",
    "ONEBOT_HOME_CHANNEL",
    "ONEBOT_ALLOW_ALL_USERS",
    "ONEBOT_ADMIN_QQ",
):
    os.environ.pop(_key, None)

collect_ignore = ["test_full_chain.py"]
