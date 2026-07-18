import os
import sys
import time
from pathlib import Path

from review_agent.attempt import AttemptCommand
from review_agent.resources import WORKSPACE_PREFIX

document = sys.stdin.buffer.read()
command = AttemptCommand.from_json_bytes(document)
Path(sys.argv[1]).write_bytes(document)
workspace = Path(os.environ["WORKSPACE_ROOT"]) / f"{WORKSPACE_PREFIX}{command.attempt_id}"
workspace.mkdir(parents=True)
if len(sys.argv) == 3 and sys.argv[2] == "emit-output":
    sys.stdout.write("child stdout is inherited\n")
    sys.stdout.flush()
    sys.stderr.write("child stderr is inherited\n")
    sys.stderr.flush()
if len(sys.argv) == 4:
    started = Path(sys.argv[2])
    release = Path(sys.argv[3])
    started.touch()
    while not release.exists():
        time.sleep(0.01)
