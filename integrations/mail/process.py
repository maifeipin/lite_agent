"""Bounded subprocess transport for mail CLI stdout and stderr."""
import os
import queue
import signal
import subprocess
import threading
import time

MAX_OUTPUT_BYTES = 10 * 1024 * 1024


class OutputLimitExceeded(RuntimeError):
    pass


def run_process(args, *, cwd=None, env=None, timeout=60, capture_output=True,
                text=True, encoding='utf-8', max_output_bytes=MAX_OUTPUT_BYTES):
    """Drain both pipes concurrently; stop the process before output exceeds budget."""
    if not capture_output or not text:
        raise ValueError('Mail transport requires captured text output')
    if timeout <= 0 or max_output_bytes < 1:
        raise ValueError('Timeout and output budget must be positive')
    proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, start_new_session=(os.name == 'posix'))
    chunks = queue.Queue(maxsize=8)
    stopped = threading.Event()
    buffers = [bytearray(), bytearray()]

    def emit(item):
        while not stopped.is_set():
            try:
                chunks.put(item, timeout=.05)
                return
            except queue.Full:
                pass

    def drain(index, pipe):
        try:
            while not stopped.is_set():
                data = pipe.read1(65536)
                if not data:
                    break
                emit((index, data))
        finally:
            emit((index, None))

    readers = [threading.Thread(target=drain, args=(i, pipe), daemon=True)
               for i, pipe in enumerate((proc.stdout, proc.stderr))]
    for reader in readers:
        reader.start()
    deadline = time.monotonic() + timeout
    finished = set()
    size = 0
    try:
        while len(finished) < 2:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(args, timeout)
            try:
                index, data = chunks.get(timeout=min(.05, remaining))
            except queue.Empty:
                continue
            if data is None:
                finished.add(index)
                continue
            size += len(data)
            if size > max_output_bytes:
                raise OutputLimitExceeded(f'命令输出超出上限 ({max_output_bytes} 字节，stdout/stderr 合计)')
            buffers[index].extend(data)
        proc.wait(timeout=max(.001, deadline - time.monotonic()))
        return subprocess.CompletedProcess(args, proc.returncode,
            buffers[0].decode(encoding, errors='replace'),
            buffers[1].decode(encoding, errors='replace'))
    finally:
        stopped.set()
        if os.name == 'posix':
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        elif proc.poll() is None:
            proc.kill()
        proc.wait()
        for reader in readers:
            reader.join(timeout=1)
        for pipe in (proc.stdout, proc.stderr):
            pipe.close()
