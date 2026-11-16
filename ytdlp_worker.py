#!/usr/bin/env python3
"""A warm yt-dlp: one long-lived process per engine slot, not one per call.

Every `yt-dlp` run starts Python and imports yt-dlp - about 0.4s of a 7.7s
video lookup, measured on 16 Nov 2026 - and downloads YouTube's ~2 MB player
script again, another 1.7-2.2s. yt-dlp keeps that script in
YoutubeIE._code_cache, which lives as long as one process, and its
preprocessed-player disk cache is switched off
(_ENABLE_PREPROCESSED_PLAYER_CACHE in yt_dlp/extractor/youtube/jsc/_builtin/
ejs.py). A process that stays up, with one such cache shared by the YoutubeDL
each run makes, pays for both once.

Both halves live in this one file, so the protocol is written down once:

  WarmYtdlp   in the caller. downloader.run_logged() hands it the stages in
              downloader.WARM_STAGES, and only while an api.EnginePool slot has
              set downloader.WARM_YTDLP. The CLI, a download job and
              test_downloader.py never set it.
  serve()     in the child, which runs under the yt-dlp CLI's OWN interpreter -
              read from the shebang of `yt-dlp` on PATH - never the caller's.
              On this machine the caller is Python 3.15, whose bgutil plugin
              (2.0.2) does not match its 1.3.2 script: in-process extraction
              there crashed in provider_display_name, and its first call took
              46s. The CLI's 3.14 has 1.3.2 on both sides.

Per request the child runs yt_dlp.main() through sys.argv, exactly as the CLI
script does - so yt-dlp reads its config files as the CLI would, which
main(argv) would skip - and turns the SystemExit into the exit status the CLI
would have had. Its fd 1 and fd 2 ARE two capture files the caller made, so
whatever lands there - yt-dlp's output, a traceback, a deno it started - is
what the caller reads back as stdout and stderr. Requests and replies are
length-prefixed JSON on a pipe of their own, never stdout.

What the caller gets is what subprocess.run would have given it: a
CompletedProcess, or subprocess.TimeoutExpired once the child has been killed
together with its process group, so no deno or node outlives it. A child that
is busy, cannot start, or dies mid-run is not an error: run() returns None,
says why in .problem, and the caller runs the command as a process of its own.

Stdlib only. The child half runs under whatever Python the CLI uses, so
nothing here may need more than 3.9.
"""

import functools
import json
import locale
import os
import re
import selectors
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback

PROTOCOL = 1
WORKER_FILE = os.path.abspath(__file__)

RECYCLE_CALLS = 200      # a fresh process after this many runs: bounds slow leaks
IDLE_SECONDS = 600       # a process nobody used for this long exits by itself
PLAYERS_KEPT = 4         # player scripts the child keeps, ~2 MB each
HELLO_SECONDS = 60       # longest wait for a new child to import yt-dlp
BROKEN_SECONDS = 300     # after a child could not start, how long to stop trying
CRASH_LIMIT = 3          # crashes in a row that count as "cannot start"
STOP_SECONDS = 5         # wait for a killed child to be reaped

# The subprocess.run arguments a warm run honours. Anything else - input,
# cwd, check, stdin - is declined, and the caller runs the command itself.
KWARGS = frozenset({"capture_output", "text", "encoding", "errors", "timeout", "env"})

_HEADER = struct.Struct(">I")
_MAX_FRAME = 1 << 24
# What subprocess itself waits with: poll where there is one, else select.
_Selector = getattr(selectors, "PollSelector", selectors.SelectSelector)


def supported():
    """True where a warm process can work: POSIX, for pass_fds and killpg."""
    return os.name == "posix" and hasattr(os, "killpg") and hasattr(os, "pread")


# ------------------------------------------------------------------ framing
class _Late(Exception):
    """A deadline passed while waiting on the pipe. Not TimeoutError, which is
    an OSError, and OSError here means the other side is gone."""


def _readable(fd, timeout):
    with _Selector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        return bool(selector.select(timeout))


def _read_exact(fd, size, deadline):
    parts, got = [], 0
    while got < size:
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0 or not _readable(fd, left):
                raise _Late()
        chunk = os.read(fd, size - got)
        if not chunk:
            if got:
                raise EOFError("the pipe closed in the middle of a message")
            return None
        parts.append(chunk)
        got += len(chunk)
    return b"".join(parts)


def send_frame(fd, message):
    """Write one message: a 4-byte big-endian length, then that much JSON."""
    payload = json.dumps(message).encode("utf-8")
    data = memoryview(_HEADER.pack(len(payload)) + payload)
    while data:
        data = data[os.write(fd, data):]


def recv_frame(fd, deadline=None):
    """Read one message. None at a clean end of the pipe; _Late past deadline;
    EOFError or ValueError for anything that is not a whole message."""
    head = _read_exact(fd, _HEADER.size, deadline)
    if head is None:
        return None
    (size,) = _HEADER.unpack(head)
    if size > _MAX_FRAME:
        raise ValueError(f"a {size}-byte message is not one of ours")
    body = _read_exact(fd, size, deadline)
    if body is None:
        raise EOFError("the pipe closed in the middle of a message")
    return json.loads(body.decode("utf-8"))


# --------------------------------------------------------- which interpreter
_SH_EXEC = re.compile(r"""^'''exec' (?:"([^"]+)"|(\S+)) "\$0" "\$@"\s*$""")


def cli_command(path, search_path=None):
    """The interpreter command line the yt-dlp script at path runs under.

    ["/opt/homebrew/opt/python@3.14/bin/python3.14"] on this machine. Read from
    the shebang, because the point is to run exactly the Python the CLI runs:
    the plain "#!/path/python [one-arg]", "#!/usr/bin/env python3", and the
    "#!/bin/sh" + "'''exec' ..." pip writes when that path has a space in it.
    None for anything that is not a small Python console script - a zipapp, a
    PyInstaller build, a .exe, a shell wrapper - which then runs as before.
    """
    try:
        with open(path, "rb") as f:
            head = f.read(16384)
            more = f.read(1)
    except OSError:
        return None
    if more or not head.startswith(b"#!") or b"\0" in head:
        return None
    try:
        text = head.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if "yt_dlp" not in text:
        return None
    lines = text.splitlines()
    words = lines[0][2:].split()
    if not words:
        return None
    program = os.path.basename(words[0])
    if program == "env":
        rest = words[1:]
        while rest and (rest[0].startswith("-") or "=" in rest[0]):
            rest = rest[1:]
        found = shutil.which(rest[0], path=search_path) if rest else None
        if not found:
            return None
        words = [found] + rest[1:]
    elif program in ("sh", "bash", "dash", "zsh"):
        match = _SH_EXEC.match(lines[1]) if len(lines) > 1 else None
        if not match:
            return None
        words = [match.group(1) or match.group(2)]
    if not re.match(r"(python|pypy)", os.path.basename(words[0])):
        return None
    if not (os.path.isabs(words[0]) and os.access(words[0], os.X_OK)):
        return None
    return words[:2]


def _stat(path):
    try:
        st = os.stat(path)
    except (OSError, TypeError):
        return None
    return st.st_mtime_ns, st.st_size, st.st_ino


def _read_all(fd):
    size = os.fstat(fd).st_size
    parts, offset = [], 0
    while offset < size:
        chunk = os.pread(fd, size - offset, offset)
        if not chunk:
            break
        parts.append(chunk)
        offset += len(chunk)
    return b"".join(parts)


def completed(cmd, code, stdout, stderr, kwargs):
    """The CompletedProcess subprocess.run(cmd, **kwargs) would have returned:
    in text mode decoded with its encoding and errors, newlines translated."""
    if kwargs.get("text") or kwargs.get("encoding") or kwargs.get("errors"):
        encoding = kwargs.get("encoding") or locale.getpreferredencoding(False)
        errors = kwargs.get("errors") or "strict"
        stdout, stderr = (data.decode(encoding, errors).replace("\r\n", "\n").replace("\r", "\n")
                          for data in (stdout, stderr))
    return subprocess.CompletedProcess(cmd, code, stdout, stderr)


# ------------------------------------------------------------------- caller
class _Child:
    """One running worker process and the caller's ends of its pipes."""

    def __init__(self, proc, out, err, request_fd, reply_fd, lifeline_fd, env):
        self.proc, self.out, self.err = proc, out, err
        self.request_fd, self.reply_fd, self.lifeline_fd = request_fd, reply_fd, lifeline_fd
        self.env = env
        self.calls = 0
        self.last_used = time.monotonic()
        self.stamp = None
        self.hello = {}
        self.players = 0
        self._lock = threading.Lock()
        self._closed = False

    def output(self):
        return _read_all(self.out.fileno()), _read_all(self.err.fileno())

    def kill(self):
        """SIGKILL the whole process group: the worker, and any deno or node it
        started. Only while the worker is unreaped - returncode still None, and
        no poll() first, which would reap it - so its pid, which is the group's
        id, cannot have been handed to anything else."""
        if self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except OSError:
                pass

    def close(self):
        """Kill it and give back every descriptor, once, from whichever thread."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        for fd in (self.request_fd, self.reply_fd, self.lifeline_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        self.kill()
        try:
            self.proc.wait(STOP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        for f in (self.out, self.err):
            try:
                f.close()
            except OSError:
                pass


class WarmYtdlp:
    """One engine slot's warm yt-dlp. See the module docstring.

    run() is all a caller needs: subprocess.run's signature, for the arguments
    in KWARGS. One call at a time - a second, while one is running, is declined
    as busy and runs the usual way rather than waiting.
    """

    def __init__(self, recycle_after=RECYCLE_CALLS, idle_seconds=IDLE_SECONDS,
                 players=PLAYERS_KEPT):
        self.recycle_after = max(1, int(recycle_after))
        self.idle_seconds = max(1.0, float(idle_seconds))
        self.players = max(1, int(players))
        self.problem = None            # why the last call declined, if it did
        self.counts = dict.fromkeys(("starts", "runs", "declined", "timeouts",
                                     "crashes", "recycled"), 0)
        self._lock = threading.Lock()
        self._child = None
        self._broken_until = 0.0
        self._broken_reason = None
        self._crashes = 0
        self._sequence = 0

    # -- the one entry point
    def run(self, cmd, **kwargs):
        """subprocess.run(cmd, **kwargs), answered by the warm process.

        Returns a CompletedProcess, or None when the call was not run there -
        the reason is in .problem, and the caller runs cmd itself. Raises
        subprocess.TimeoutExpired past kwargs["timeout"], as subprocess.run
        would, the process killed with its group first.
        """
        if (not cmd or os.path.basename(str(cmd[0])) != "yt-dlp"
                or not kwargs.get("capture_output") or not set(kwargs) <= KWARGS):
            return self._decline("not a captured yt-dlp run this process can stand in for")
        if not self._lock.acquire(blocking=False):
            return self._decline("busy: this slot's warm yt-dlp is already running a call")
        env = kwargs.get("env")
        try:
            result = self._run(list(cmd), dict(os.environ if env is None else env),
                               kwargs.get("timeout"))
        finally:
            self._lock.release()
        if result is None:
            return None
        return completed(cmd, *result, kwargs)

    def stop(self):
        """Let the process go; the next call starts another."""
        got = self._lock.acquire(timeout=STOP_SECONDS)
        try:
            if self._child is not None:
                self._retire(self._child)
        finally:
            if got:
                self._lock.release()

    def status(self):
        child = self._child
        alive = child is not None and child.proc.poll() is None
        return {"alive": alive, "pid": child.proc.pid if alive else None,
                "runs_this_process": child.calls if alive else 0,
                "players_kept": child.players if alive else 0,
                "python": child.hello.get("python") if alive else None,
                "yt_dlp": child.hello.get("version") if alive else None,
                **self.counts, "problem": self.problem}

    # -- the work
    def _decline(self, why):
        self.problem = why
        self.counts["declined"] += 1
        return None

    def _broken(self, why):
        self._broken_until = time.monotonic() + BROKEN_SECONDS
        self._broken_reason = why
        return self._decline(why)

    def _run(self, cmd, env, timeout):
        started = time.monotonic()
        deadline = None if timeout is None else started + max(0.0, float(timeout))
        if started < self._broken_until:
            return self._decline(f"{self._broken_reason} (not retried for "
                                 f"{int(self._broken_until - started) + 1}s)")
        child = self._current(cmd[0], env)
        if child is None:
            child = self._start(cmd, env, deadline, timeout)
            if child is None:
                return None
        self._sequence += 1
        request = {"id": self._sequence, "argv": cmd[1:]}
        try:
            request["cwd"] = os.getcwd()
        except OSError:
            pass
        try:
            send_frame(child.request_fd, request)
            reply = recv_frame(child.reply_fd, deadline)
            if reply is None:
                raise EOFError("it exited")
            if reply.get("id") != request["id"] or not isinstance(reply.get("rc"), int):
                raise ValueError(f"an answer to another question: {reply!r:.200}")
        except _Late:
            child.kill()                       # first, so nothing writes any more
            out, err = child.output()
            self._retire(child)
            self.counts["timeouts"] += 1
            raise subprocess.TimeoutExpired(cmd, timeout, output=out or None,
                                            stderr=err or None) from None
        except (OSError, EOFError, ValueError) as e:
            self._retire(child)
            self.counts["crashes"] += 1
            self._crashes += 1
            why = f"the warm yt-dlp died in the middle of a call ({e})"
            if self._crashes >= CRASH_LIMIT:
                return self._broken(f"{why}, {self._crashes} times in a row")
            return self._decline(why)
        except BaseException:
            self._retire(child)            # an answer left unread would be the next call's
            raise
        out, err = child.output()
        child.calls += 1
        child.last_used = time.monotonic()
        child.players = reply.get("players") or 0
        self.counts["runs"] += 1
        self._crashes = 0
        self.problem = None
        if reply.get("retire"):
            self._retire(child)
        return reply["rc"], out, err

    def _stamp(self, name, env, child):
        """What must not change under a running child: which yt-dlp script PATH
        finds, that script, and the yt-dlp package it imported - an upgrade
        takes effect on the next call, as it would for the CLI."""
        path = shutil.which(name, path=env.get("PATH"))
        version_file = os.path.join(os.path.dirname(child.hello.get("yt_dlp") or ""),
                                    "version.py")
        return path, _stat(path), _stat(version_file)

    def _current(self, name, env):
        """The running child, if it may take this call; else retire it."""
        child = self._child
        if child is None:
            return None
        margin = min(5.0, self.idle_seconds / 4)
        if child.proc.poll() is not None:
            why = None                         # gone already (idle, or killed)
        elif child.calls >= self.recycle_after:
            why = "recycled"
        elif time.monotonic() - child.last_used > self.idle_seconds - margin:
            why = None                         # about to leave on its own
        elif child.env != env:
            why = "environment changed"
        elif self._stamp(name, env, child) != child.stamp:
            why = "yt-dlp changed on disk"
        else:
            return child
        if why == "recycled":
            self.counts["recycled"] += 1
        self._retire(child)
        return None

    def _retire(self, child):
        if self._child is child:
            self._child = None
        child.close()

    def _start(self, cmd, env, deadline, timeout):
        search_path = env.get("PATH")
        path = shutil.which(cmd[0], path=search_path)
        python = cli_command(path, search_path) if path else None
        if python is None:
            return self._broken(f"{path or cmd[0]} is not a Python script that "
                                f"can be kept warm")
        files, pipes = [], []
        try:
            # The child's stdout and stderr for its whole life: unlinked files
            # only this process and it can reach, read back after each call.
            files = [tempfile.TemporaryFile(prefix="ytmeer-warm-") for _ in range(2)]
            for _ in range(3):
                pipes.append(os.pipe())
            (request_r, request_w), (reply_r, reply_w), (lifeline_r, lifeline_w) = pipes
            # request: it reads, we write. reply: it writes, we read. lifeline:
            # it reads, we hold the write end and never write (see _orphaned).
            theirs = (request_r, reply_w, lifeline_r)
            argv = python + [WORKER_FILE, "--cli", path,
                             "--fds", ",".join(map(str, theirs)),
                             "--idle", repr(self.idle_seconds),
                             "--players", str(self.players)]
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=files[0],
                                    stderr=files[1], env=env, pass_fds=theirs,
                                    start_new_session=True)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            for fd in (fd for pair in pipes for fd in pair):
                os.close(fd)
            for f in files:
                f.close()
            return self._broken(f"could not start a warm yt-dlp: {e}")
        for fd in theirs:
            os.close(fd)
        child = _Child(proc, files[0], files[1], request_w, reply_r, lifeline_w, env)
        limit = time.monotonic() + HELLO_SECONDS
        if deadline is not None:
            limit = min(limit, deadline)
        try:
            hello = recv_frame(child.reply_fd, limit)
            if hello is None:
                raise EOFError("it exited")
        except _Late:
            child.close()
            if deadline is not None and time.monotonic() >= deadline:
                # The call's own time ran out while yt-dlp was starting, which
                # would have timed out the CLI just the same.
                self.counts["timeouts"] += 1
                raise subprocess.TimeoutExpired(cmd, timeout) from None
            return self._broken(f"a warm yt-dlp took over {HELLO_SECONDS}s to start")
        except (OSError, EOFError, ValueError) as e:
            said = _last_line(child.output()[1])
            child.close()
            return self._broken(f"a warm yt-dlp exited before it started ({said or e})")
        except BaseException:
            child.close()
            raise
        if not hello.get("ok") or hello.get("protocol") != PROTOCOL:
            child.close()
            return self._broken(f"a warm yt-dlp could not start: "
                                f"{hello.get('error') or hello!r:.300}")
        child.hello = hello
        child.stamp = self._stamp(cmd[0], env, child)
        self._child = child
        self.counts["starts"] += 1
        return child


def _last_line(data):
    lines = data.decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else ""


# -------------------------------------------------------------------- child
class _Players(dict):
    """YoutubeIE._code_cache, shared by every run in this process.

    Keyed by player id and variant. Holds the last `keep` scripts, ~2 MB each:
    YouTube rolls its player over now and then, and an old one is never asked
    for again.
    """

    def __init__(self, keep):
        super().__init__()
        self.keep = keep

    def __setitem__(self, key, value):
        self.pop(key, None)
        super().__setitem__(key, value)
        while len(self) > self.keep:
            super().__delitem__(next(iter(self)))


PLAYERS = _Players(PLAYERS_KEPT)


def share_player_code(players):
    """Point every YoutubeIE's _code_cache at players. True if it could.

    yt-dlp sets self._code_cache = {} in YoutubeIE.__init__, and a run makes a
    new YoutubeIE, so the player script it downloaded went with it. Patched on
    the real class, which lazy extractors and plugin subclasses both end in.
    """
    try:
        from yt_dlp.extractor.youtube._video import YoutubeIE
    except Exception:
        try:
            from yt_dlp.extractor.youtube import YoutubeIE
        except Exception:
            return False
    original = YoutubeIE.__init__
    if getattr(original, "_ytmeer_shared", None) is players:
        return True

    @functools.wraps(original)
    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self._code_cache = players

    __init__._ytmeer_shared = players
    YoutubeIE.__init__ = __init__
    return True


def exit_status(code):
    """The exit status CPython gives sys.exit(code): None is 0, an int is its
    low byte, anything else is printed to stderr and is 1."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code & 0xFF
    try:
        sys.stderr.write(f"{code}\n")
    except Exception:
        pass
    return 1


def _flush():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass


def _append_only(fd):
    """Every write to fd goes to the end, whoever makes it and however far a
    previous call's offset had got - so truncating to 0 starts afresh."""
    import fcntl
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_APPEND)
    except OSError:
        pass


def _orphaned(fd):
    """Wait for the caller to go, then take this process group down with it.

    The caller never writes to this pipe, so a read returns only when its end
    closes: the caller exited, was killed, or let this process go. A worker
    left running would finish its call - up to a channel listing's 300s - with
    nobody to answer to.
    """
    while True:
        try:
            if not os.read(fd, 64):
                break
        except InterruptedError:
            continue
        except OSError:
            break
    try:
        if os.getpgrp() == os.getpid():       # only a group of our own
            os.killpg(os.getpid(), signal.SIGKILL)
    finally:
        os._exit(0)


def _load(keep):
    """Import yt-dlp once and share its player cache: the hello message."""
    try:
        import yt_dlp
    except BaseException as e:
        return {"ok": False, "protocol": PROTOCOL, "error": f"{type(e).__name__}: {e}"}
    try:
        from yt_dlp.version import __version__ as version
    except Exception:
        version = None
    PLAYERS.keep = keep
    return {"ok": True, "protocol": PROTOCOL, "pid": os.getpid(),
            "python": sys.version.split()[0], "version": version,
            "yt_dlp": os.path.abspath(yt_dlp.__file__),
            "player_cache": share_player_code(PLAYERS)}


def run_one(argv, cwd, cli):
    """One CLI run in this process. Returns (exit status, retire).

    retire: something escaped yt-dlp that the CLI would have died of, so this
    process's state is no longer to be trusted, and it leaves after replying.
    """
    import yt_dlp
    _flush()
    for fd in (1, 2):
        try:
            os.ftruncate(fd, 0)
        except OSError:
            pass                       # not a file (run by hand): nothing to clear
    streams = sys.stdout, sys.stderr
    retire = False
    try:
        here = os.getcwd()
    except OSError:
        here = None
    try:
        if cwd and cwd != here:
            os.chdir(cwd)
        sys.argv = [cli] + [str(arg) for arg in argv]
        try:
            yt_dlp.main()
            code = 0
        except SystemExit as e:
            code = exit_status(e.code)
    except BaseException:
        traceback.print_exc()
        code, retire = 1, True
    finally:
        _flush()
        sys.stdout, sys.stderr = streams
        try:
            if here and os.getcwd() != here:
                os.chdir(here)
        except OSError:
            retire = True
    return code, retire


def serve(args):
    """The child's loop: answer requests until told to stop, idle or orphaned."""
    opts = dict(zip(args[::2], args[1::2]))
    cli = opts["--cli"]
    # The CLI's own sys.path, not this file's folder: a module beside this file
    # must never stand in for one yt-dlp imports.
    sys.path[0] = os.path.dirname(os.path.abspath(cli))
    request_fd, reply_fd, lifeline_fd = (int(fd) for fd in opts["--fds"].split(","))
    for fd in (request_fd, reply_fd, lifeline_fd):
        # pass_fds made them inheritable. A deno that kept the reply pipe open
        # would hide this process's death from the caller, which would then
        # wait out the whole timeout instead of falling back at once.
        os.set_inheritable(fd, False)
    idle = float(opts.get("--idle", IDLE_SECONDS))
    threading.Thread(target=_orphaned, args=(lifeline_fd,), daemon=True,
                     name="lifeline").start()
    for fd in (1, 2):
        _append_only(fd)
    hello = _load(int(opts.get("--players", PLAYERS_KEPT)))
    send_frame(reply_fd, hello)
    if not hello["ok"]:
        return 1
    while _readable(request_fd, idle):
        request = recv_frame(request_fd)
        if request is None:
            break
        code, retire = run_one(request.get("argv") or [], request.get("cwd"), cli)
        send_frame(reply_fd, {"id": request.get("id"), "rc": code, "retire": retire,
                              "players": len(PLAYERS)})
        if retire:
            break
    return 0


if __name__ == "__main__":
    sys.exit(serve(sys.argv[1:]))
