import os
import signal
import sys
import time
from pathlib import Path

from review_agent.attempt import (
    AttemptCommand,
    AttemptOutcome,
    AttemptPublication,
    AttemptStatus,
)
from review_agent.resources import WORKSPACE_PREFIX

document = sys.stdin.buffer.read()
command = AttemptCommand.from_json_bytes(document)
Path(sys.argv[1]).write_bytes(document)
workspace = Path(os.environ["WORKSPACE_ROOT"]) / f"{WORKSPACE_PREFIX}{command.attempt_id}"
workspace.mkdir(parents=True)
mode = sys.argv[2] if len(sys.argv) > 2 else "success"
if mode == "crash":
    raise SystemExit(17)
if len(sys.argv) == 3 and sys.argv[2] == "emit-output":
    sys.stdout.write("child stdout is inherited\n")
    sys.stdout.flush()
    sys.stderr.write("child stderr is inherited\n")
    sys.stderr.flush()
if len(sys.argv) == 5 and sys.argv[2] == "record-start":
    started = Path(sys.argv[3])
    release = Path(sys.argv[4])
    started.mkdir(parents=True, exist_ok=True)
    (started / command.attempt_id).touch()
    while not release.exists():
        time.sleep(0.01)
if len(sys.argv) == 4 and sys.argv[2] != "ignore-term-group":
    started = Path(sys.argv[2])
    release = Path(sys.argv[3])
    started.touch()
    while not release.exists():
        time.sleep(0.01)
if len(sys.argv) == 5 and sys.argv[2] == "exit-on-term":
    started = Path(sys.argv[3])
    terminated = Path(sys.argv[4])

    def exit_on_term(_signum: int, _frame: object) -> None:
        terminated.write_text(str(time.monotonic()), encoding="utf-8")
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, exit_on_term)
    started.write_text(str(time.monotonic()), encoding="utf-8")
    fallback_exit = time.monotonic() + 1.0
    while time.monotonic() < fallback_exit:
        time.sleep(0.01)
if len(sys.argv) == 4 and sys.argv[2] == "ignore-term-group":
    started = Path(sys.argv[3])
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    descendant_pid = os.fork()
    if descendant_pid == 0:
        fallback_exit = time.monotonic() + 1.0
        while time.monotonic() < fallback_exit:
            time.sleep(0.01)
        os._exit(0)
    started.write_text(f"{os.getpid()} {descendant_pid}", encoding="utf-8")
    fallback_exit = time.monotonic() + 1.0
    while time.monotonic() < fallback_exit:
        time.sleep(0.01)
    os.waitpid(descendant_pid, 0)
if command.outcome_fd is not None:
    with os.fdopen(command.outcome_fd, "wb", closefd=True) as outcome:
        valid_outcome = AttemptOutcome(
            attempt_id=command.attempt_id,
            status=AttemptStatus.REVIEWED,
            review_status="no_important_issues",
            publication=AttemptPublication.PUBLISHED,
            failure_stage=None,
            failure_category=None,
        ).to_json_bytes()
        if mode == "missing-outcome":
            pass
        elif mode == "invalid-outcome":
            outcome.write(b"secret model text and subprocess output")
        elif mode == "oversized-outcome":
            outcome.write(b"x" * 4_097)
        elif mode == "duplicated-outcome":
            outcome.write(valid_outcome + valid_outcome)
        elif mode == "mismatched-outcome":
            outcome.write(
                AttemptOutcome(
                    attempt_id="f" * 32,
                    status=AttemptStatus.REVIEWED,
                    review_status="no_important_issues",
                    publication=AttemptPublication.PUBLISHED,
                    failure_stage=None,
                    failure_category=None,
                ).to_json_bytes()
            )
        else:
            outcome.write(valid_outcome)
