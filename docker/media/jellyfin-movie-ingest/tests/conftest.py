import os
import sys
import tempfile

# ingest reads its settings and opens its database when imported; a throwaway folder stands in for /data
os.environ.setdefault("JELLYFIN_URL", "http://jellyfin.invalid")
os.environ.setdefault("JELLYFIN_API_KEY", "test")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="ingest-test-")
# The service's modules import each other by name, as from /app in its image
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
