import os
import sys

os.environ.setdefault("JELLYFIN_URL", "http://jellyfin.invalid")
os.environ.setdefault("JELLYFIN_API_KEY", "test")
here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [here, os.path.dirname(here)]
