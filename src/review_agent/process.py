import os
import selectors
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from review_agent.deadline import remaining_review_time


class _ProcessOutputLimitError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class ProcessOptions:
    output_max_bytes: int
    stage: str
    timeout_seconds: float | None = None
    use_review_deadline: bool = True
    env: Mapping[str, str] | None = None


class ProcessRunner(Protocol):
    def __call__(
        self,
        arguments: tuple[str, ...],
        options: ProcessOptions,
    ) -> subprocess.CompletedProcess[bytes]: ...


def _run_bounded_process(  # noqa: C901, PLR0912, PLR0915
    arguments: tuple[str, ...],
    options: ProcessOptions,
) -> subprocess.CompletedProcess[bytes]:
    timeout_at = (
        None if options.timeout_seconds is None else time.monotonic() + options.timeout_seconds
    )
    process = subprocess.Popen(  # noqa: S603 - arguments are structured and never use a shell.
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=options.env,
    )
    if process.stdout is None or process.stderr is None:
        message = "bounded process capture requires stdout and stderr pipes"
        raise RuntimeError(message)

    captured = {"stdout": bytearray(), "stderr": bytearray()}
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    total_bytes = 0
    exceeded = False
    try:
        while selector.get_map():
            timeouts: list[float] = []
            if options.use_review_deadline:
                review_timeout = remaining_review_time(stage=options.stage)
                if review_timeout is not None:
                    timeouts.append(review_timeout)
            if timeout_at is not None:
                fixed_timeout = timeout_at - time.monotonic()
                if fixed_timeout <= 0:
                    raise TimeoutError
                timeouts.append(fixed_timeout)
            select_timeout = min(timeouts) if timeouts else None
            for key, _events in selector.select(select_timeout):
                remaining = options.output_max_bytes - total_bytes
                chunk = os.read(key.fd, min(65_536, remaining + 1))
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                total_bytes += len(chunk)
                if total_bytes > options.output_max_bytes:
                    exceeded = True
                    process.kill()
                    break
                captured[str(key.data)].extend(chunk)
            if exceeded:
                break
        wait_timeout: float | None
        if timeout_at is not None:
            wait_timeout = timeout_at - time.monotonic()
            if wait_timeout <= 0:
                raise TimeoutError
        else:
            wait_timeout = (
                remaining_review_time(stage=options.stage) if options.use_review_deadline else None
            )
        try:
            return_code = process.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired as error:
            raise TimeoutError from error
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()

    if exceeded:
        raise _ProcessOutputLimitError
    completed = subprocess.CompletedProcess(
        arguments,
        return_code,
        stdout=bytes(captured["stdout"]),
        stderr=bytes(captured["stderr"]),
    )
    if return_code != 0:
        raise subprocess.CalledProcessError(
            return_code,
            arguments,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed
