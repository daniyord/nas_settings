import os
import sys
import tempfile

# The services read their settings and open their database when imported; a throwaway folder stands in for /data
os.environ.setdefault("JELLYFIN_URL", "http://jellyfin.invalid")
os.environ.setdefault("JELLYFIN_API_KEY", "test")
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="media-test-")
# As in the images: the service's own modules by name from /app, the shared ones as common.*
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [here, os.path.dirname(here)]
