import os
import sys

os.environ.setdefault("JELLYFIN_URL", "http://jellyfin.invalid")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
