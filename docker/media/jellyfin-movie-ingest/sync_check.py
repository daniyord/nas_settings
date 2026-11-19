# Run by ingest.py in its own low-priority process: aligns a subtitle to the speech in the video, writes the
# aligned copy to the output path and prints the result as the last line, after ffsubsync's own log
import json
import sys

from ffsubsync.ffsubsync import make_parser, run

video, subtitle, output = sys.argv[1:4]
result = run(make_parser().parse_args([video, "-i", subtitle, "-o", output]))
print("RESULT " + json.dumps({k: result[k] for k in ("offset_seconds", "framerate_scale_factor", "sync_was_successful")}), flush=True)
