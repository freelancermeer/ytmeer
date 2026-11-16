#!/usr/bin/env python3
"""Offline tests for the warm yt-dlp (ytdlp_worker.py).

Nothing here talks to YouTube. setUpModule writes a fake `yt-dlp` console
script - the same three lines pip writes, with this interpreter's shebang - and
a fake `yt_dlp` package on PYTHONPATH whose main() does whatever its argv says:
print, exit with a code, raise, sleep, start a grandchild, die. So every
behaviour can be compared with what the CLI itself does with the same argv.

    python3 -m pytest -q test_ytdlp_worker.py
    python3 test_ytdlp_worker.py
"""

import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import api               # noqa: E402
import downloader        # noqa: E402
import ytdlp_worker      # noqa: E402

FAKE_MAIN = r'''
import json, os, signal, subprocess, sys, time


def _runner():
    main = sys.modules.get("__main__")
    return os.path.basename(getattr(main, "__file__", "") or "?")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    url = None
    if "--" in args:
        url = args[args.index("--") + 1]
        args = args[:args.index("--")]
    i = 0
    while i < len(args):
        a, nxt = args[i], (args[i + 1] if i + 1 < len(args) else None)
        if a == "--say":
            print(nxt); i += 1
        elif a == "--warn":
            sys.stderr.write(nxt + "\n"); i += 1
        elif a == "--pid":
            print("pid=%d" % os.getpid())
        elif a == "--pid-to":
            with open(nxt, "w") as f:
                f.write(str(os.getpid()))
            i += 1
        elif a == "--runner":
            print("runner=" + _runner())
        elif a == "--cwd":
            print("cwd=" + os.getcwd())
        elif a == "--env":
            print("env=" + os.environ.get(nxt, "")); i += 1
        elif a == "--argv0":
            print("argv0=" + sys.argv[0])
        elif a == "--exit":
            raise SystemExit(int(nxt))
        elif a == "--exit-text":
            raise SystemExit(nxt)
        elif a == "--exit-none":
            raise SystemExit(None)
        elif a == "--raise":
            raise RuntimeError(nxt)
        elif a == "--sleep":
            sys.stdout.flush(); sys.stderr.flush()
            time.sleep(float(nxt)); i += 1
        elif a == "--grandchild":
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            with open(nxt, "w") as f:
                f.write(str(child.pid))
            i += 1
        elif a == "--grandchild-says":
            # Inherits fd 1, as a deno or node yt-dlp starts may.
            subprocess.run([sys.executable, "-c",
                            "import sys; sys.stdout.write(%r + '\\n')" % nxt])
            i += 1
        elif a == "--die-in-worker":
            if _runner() == "ytdlp_worker.py":
                os.kill(os.getpid(), signal.SIGKILL)
        elif a == "--big":
            sys.stdout.write("x" * int(nxt) + "\n"); i += 1
        elif a == "--crlf":
            sys.stdout.write("one\r\ntwo\rthree\n")
        elif a == "--bad-byte":
            sys.stdout.flush()
            sys.stdout.buffer.write(b"bad \xff byte\n")
            sys.stdout.buffer.flush()
        elif a == "--unicode":
            print("اردو \U0001f3ac café")
        elif a == "--player":
            from yt_dlp.extractor.youtube._video import YoutubeIE
            ie = YoutubeIE()
            hit = nxt in ie._code_cache
            if not hit:
                ie._code_cache[nxt] = "code of " + nxt
            print("player=%s hit=%s" % (nxt, hit))
            i += 1
        elif a == "--dump-single-json":
            print(json.dumps({"id": (url or "")[-11:], "title": "Fake title",
                              "runner": _runner(), "pid": os.getpid()}))
        i += 1
    raise SystemExit(0)
'''

FAKE_CLI = """#!{python}
import sys
from yt_dlp import main
if __name__ == '__main__':
    sys.argv[0] = sys.argv[0].removesuffix('.exe')
    sys.exit(main())
"""

FIXTURE = {}


def setUpModule():
    root = tempfile.mkdtemp(prefix="ytmeer-warm-test-")
    bin_dir, pkgs = os.path.join(root, "bin"), os.path.join(root, "pkgs")
    youtube = os.path.join(pkgs, "yt_dlp", "extractor", "youtube")
    os.makedirs(bin_dir)
    os.makedirs(youtube)
    files = {
        os.path.join(pkgs, "yt_dlp", "__init__.py"): FAKE_MAIN,
        os.path.join(pkgs, "yt_dlp", "version.py"): "__version__ = '2099.01.01'\n",
        os.path.join(pkgs, "yt_dlp", "extractor", "__init__.py"): "",
        os.path.join(youtube, "__init__.py"): "from ._video import YoutubeIE\n",
        os.path.join(youtube, "_video.py"): textwrap.dedent("""\
            class YoutubeIE:
                def __init__(self, *args, **kwargs):
                    self._code_cache = {}
            """),
        os.path.join(bin_dir, "yt-dlp"): FAKE_CLI.format(python=sys.executable),
    }
    for path, text in files.items():
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    os.chmod(os.path.join(bin_dir, "yt-dlp"), 0o755)
    env = dict(os.environ)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["PYTHONPATH"] = pkgs
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")
    FIXTURE.update(root=root, bin=bin_dir, pkgs=pkgs, env=env,
                   cli=os.path.join(bin_dir, "yt-dlp"),
                   version=os.path.join(pkgs, "yt_dlp", "version.py"))


def tearDownModule():
    shutil.rmtree(FIXTURE["root"], ignore_errors=True)


def gone(pid, seconds=6.0):
    """True once pid is no longer a process at all (not even a zombie)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


def wait_for_file(path, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            with open(path) as f:
                text = f.read().strip()
            if text:
                return int(text)
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


TEXT = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}


class WarmCase(unittest.TestCase):
    """A WarmYtdlp per test, pointed at the fake yt-dlp, always let go after."""

    def setUp(self):
        self.workers = []

    def tearDown(self):
        for worker in self.workers:
            worker.stop()

    def warm(self, **kwargs):
        worker = ytdlp_worker.WarmYtdlp(**kwargs)
        self.workers.append(worker)
        return worker

    def run_warm(self, worker, *args, env=None, timeout=30, **kwargs):
        options = dict(TEXT, env=env or FIXTURE["env"], timeout=timeout)
        options.update(kwargs)
        out = worker.run(["yt-dlp", *args], **options)
        self.assertIsNotNone(out, f"declined: {worker.problem}")
        return out

    def run_cli(self, *args, **kwargs):
        options = dict(TEXT, env=FIXTURE["env"], timeout=30)
        options.update(kwargs)
        return subprocess.run(["yt-dlp", *args], **options)

    def pid(self, worker, **kwargs):
        return int(self.run_warm(worker, "--pid", **kwargs).stdout.split("=")[1])


# --------------------------------------------------------------- the basics
class TestCapture(WarmCase):

    def test_stdout_and_stderr_come_back_apart_and_per_call(self):
        worker = self.warm()
        out = self.run_warm(worker, "--say", "hello", "--warn", "oops")
        self.assertEqual((out.returncode, out.stdout, out.stderr), (0, "hello\n", "oops\n"))
        out = self.run_warm(worker, "--say", "again")
        self.assertEqual((out.stdout, out.stderr), ("again\n", ""),
                         "nothing of the first call may leak into the second")

    def test_one_process_answers_every_call(self):
        worker = self.warm()
        first, second = self.pid(worker), self.pid(worker)
        self.assertEqual(first, second)
        self.assertNotEqual(first, os.getpid())
        self.assertEqual((worker.counts["starts"], worker.counts["runs"]), (1, 2))
        self.assertTrue(worker.status()["alive"])

    def test_it_runs_under_the_clis_own_interpreter_and_argv(self):
        worker = self.warm()
        self.assertEqual(self.run_warm(worker, "--argv0").stdout,
                         f"argv0={FIXTURE['cli']}\n")
        self.assertEqual(self.run_warm(worker, "--runner").stdout, "runner=ytdlp_worker.py\n")
        self.assertEqual(worker.status()["python"], sys.version.split()[0])
        self.assertEqual(worker.status()["yt_dlp"], "2099.01.01")

    def test_what_comes_back_is_what_the_cli_prints(self):
        """Run the same argv both ways: the answers must be identical."""
        worker = self.warm()
        for args in (["--say", "plain"], ["--crlf"], ["--bad-byte"], ["--unicode"],
                     ["--big", "3000000"], ["--grandchild-says", "from a child"],
                     ["--warn", "w", "--exit", "3"], ["--exit", "256"], ["--exit", "-1"],
                     ["--exit-text", "boom"], ["--exit-none"], ["--say", "x", "--exit", "2"]):
            with self.subTest(args=args):
                warm, cli = self.run_warm(worker, *args), self.run_cli(*args)
                self.assertEqual((warm.returncode, warm.stdout, warm.stderr),
                                 (cli.returncode, cli.stdout, cli.stderr))
        self.assertEqual(worker.counts["starts"], 1, "all of that in one process")

    def test_bytes_mode_is_bytes(self):
        worker = self.warm()
        out = worker.run(["yt-dlp", "--crlf"], capture_output=True, env=FIXTURE["env"],
                         timeout=30)
        self.assertEqual(out.stdout, b"one\r\ntwo\rthree\n")

    def test_the_callers_working_folder_is_used(self):
        worker = self.warm()
        self.pid(worker)                         # started in this folder
        before = os.getcwd()
        other = tempfile.mkdtemp(dir=FIXTURE["root"])
        os.chdir(other)
        try:
            out = self.run_warm(worker, "--cwd")
        finally:
            os.chdir(before)
        self.assertEqual(os.path.realpath(out.stdout.strip()[4:]), os.path.realpath(other))


class TestExitStatus(unittest.TestCase):

    def test_cpython_rules(self):
        cases = {None: 0, 0: 0, 3: 3, 256: 0, 257: 1, -1: 255, True: 1, False: 0}
        for code, expected in cases.items():
            self.assertEqual(ytdlp_worker.exit_status(code), expected, code)

    def test_text_is_printed_and_is_one(self):
        said = io.StringIO()
        with redirect_stderr(said):
            self.assertEqual(ytdlp_worker.exit_status("ERROR: gone"), 1)
        self.assertEqual(said.getvalue(), "ERROR: gone\n")


class TestException(WarmCase):

    def test_an_escaped_exception_is_a_traceback_and_rc_1_then_a_new_process(self):
        worker = self.warm()
        first = self.pid(worker)
        out = self.run_warm(worker, "--raise", "kaput")
        self.assertEqual(out.returncode, 1)
        self.assertIn("RuntimeError: kaput", out.stderr)
        self.assertIn("Traceback", out.stderr)
        self.assertEqual(out.returncode, self.run_cli("--raise", "kaput").returncode)
        # Something the CLI would have died of: that process is not reused.
        self.assertNotEqual(self.pid(worker), first)


# ---------------------------------------------------------- the player cache
class TestPlayerCache(WarmCase):

    def test_the_player_script_is_kept_between_runs(self):
        worker = self.warm()
        self.assertEqual(self.run_warm(worker, "--player", "p1").stdout, "player=p1 hit=False\n")
        self.assertEqual(self.run_warm(worker, "--player", "p1").stdout, "player=p1 hit=True\n")
        self.assertEqual(worker.status()["players_kept"], 1)
        # The CLI starts empty every time - which is the 1.7-2.2s this saves.
        self.assertEqual(self.run_cli("--player", "p1").stdout, "player=p1 hit=False\n")

    def test_only_the_last_few_players_are_kept(self):
        worker = self.warm(players=2)
        for name in ("p1", "p2", "p3"):
            self.run_warm(worker, "--player", name)
        self.assertEqual(worker.status()["players_kept"], 2)
        self.assertEqual(self.run_warm(worker, "--player", "p1").stdout,
                         "player=p1 hit=False\n", "the oldest went first")
        self.assertEqual(self.run_warm(worker, "--player", "p3").stdout, "player=p3 hit=True\n")

    def test_the_bounded_dict(self):
        players = ytdlp_worker._Players(2)
        players["a"], players["b"] = 1, 2
        players["a"] = 3                  # a rewrite counts as the newest
        players["c"] = 4
        self.assertEqual(list(players), ["a", "c"])


# ------------------------------------------------------ timeouts and crashes
class TestTimeout(WarmCase):

    def test_a_timeout_kills_the_process_group_and_raises_as_subprocess_does(self):
        worker = self.warm()
        first = self.pid(worker)
        grandchild_file = os.path.join(FIXTURE["root"], f"grandchild-{time.monotonic_ns()}")
        cmd = ["yt-dlp", "--say", "partial", "--warn", "started",
               "--grandchild", grandchild_file, "--sleep", "60"]
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            worker.run(cmd, env=FIXTURE["env"], timeout=2, **TEXT)
        self.assertLess(time.monotonic() - started, 10)
        e = caught.exception
        self.assertEqual((e.cmd, e.timeout), (cmd, 2))
        # Bytes, as subprocess.run hands them over, even in text mode.
        self.assertEqual((e.stdout, e.stderr), (b"partial\n", b"started\n"))
        self.assertTrue(gone(first), "the worker must be dead")
        self.assertTrue(gone(wait_for_file(grandchild_file)),
                        "and so must whatever it started")
        self.assertEqual(worker.counts["timeouts"], 1)
        self.assertNotEqual(self.pid(worker), first, "the next call gets a fresh one")

    def test_through_run_logged_the_caller_sees_timeout_expired(self):
        worker = self.warm()
        saved = downloader.WARM_YTDLP
        downloader.WARM_YTDLP = worker
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                downloader.run_logged("metadata", ["yt-dlp", "--sleep", "30"],
                                      capture_output=True, text=True, timeout=1,
                                      env=FIXTURE["env"])
        finally:
            downloader.WARM_YTDLP = saved
        self.assertEqual(worker.counts["timeouts"], 1)


class TestCrash(WarmCase):

    def test_a_crash_mid_call_is_declined_and_the_next_call_starts_afresh(self):
        worker = self.warm()
        first = self.pid(worker)
        out = worker.run(["yt-dlp", "--die-in-worker"], env=FIXTURE["env"], timeout=30, **TEXT)
        self.assertIsNone(out)
        self.assertIn("died", worker.problem)
        self.assertEqual(worker.counts["crashes"], 1)
        self.assertTrue(gone(first))
        self.assertNotEqual(self.pid(worker), first)

    def test_run_logged_falls_back_to_the_cli(self):
        worker = self.warm()
        saved = downloader.WARM_YTDLP
        downloader.WARM_YTDLP = worker
        try:
            out = downloader.run_logged("metadata", ["yt-dlp", "--die-in-worker", "--runner"],
                                        capture_output=True, text=True, timeout=30,
                                        env=FIXTURE["env"])
            self.assertEqual((out.returncode, out.stdout), (0, "runner=yt-dlp\n"))
            out = downloader.run_logged("metadata", ["yt-dlp", "--runner"],
                                        capture_output=True, text=True, timeout=30,
                                        env=FIXTURE["env"])
            self.assertEqual(out.stdout, "runner=ytdlp_worker.py\n")
        finally:
            downloader.WARM_YTDLP = saved

    def test_the_fallback_gets_only_the_time_that_is_left(self):
        worker = self.warm()
        pid = self.pid(worker)
        seen = {}

        def recorded(cmd, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(cmd, 0, "", "")

        def crash_later():
            time.sleep(0.5)                  # half a second into the call
            os.killpg(pid, signal.SIGKILL)

        saved = downloader.WARM_YTDLP, downloader.subprocess.run
        downloader.WARM_YTDLP, downloader.subprocess.run = worker, recorded
        try:
            threading.Thread(target=crash_later, daemon=True).start()
            downloader.run_logged("comments", ["yt-dlp", "--sleep", "20"],
                                  capture_output=True, text=True, timeout=30,
                                  env=FIXTURE["env"])
        finally:
            downloader.WARM_YTDLP, downloader.subprocess.run = saved
        self.assertEqual(worker.counts["crashes"], 1)
        self.assertLess(seen["timeout"], 29.6, "the half second already spent is gone")
        self.assertGreater(seen["timeout"], 20)

    def test_a_dead_parent_takes_the_worker_and_its_children_with_it(self):
        """No orphan finishing a 300s channel listing for nobody."""
        stamp = time.monotonic_ns()
        worker_file = os.path.join(FIXTURE["root"], f"worker-{stamp}")
        grandchild_file = os.path.join(FIXTURE["root"], f"grandchild-{stamp}")
        script = textwrap.dedent(f"""
            import os, sys
            sys.path.insert(0, {HERE!r})
            import ytdlp_worker
            ytdlp_worker.WarmYtdlp().run(
                ["yt-dlp", "--pid-to", {worker_file!r}, "--grandchild", {grandchild_file!r},
                 "--sleep", "120"], capture_output=True, env=dict(os.environ), timeout=300)
            """)
        parent = subprocess.Popen([sys.executable, "-c", script], env=FIXTURE["env"])
        try:
            worker_pid = wait_for_file(worker_file)
            grandchild_pid = wait_for_file(grandchild_file)
        finally:
            parent.kill()
            parent.wait()
        self.assertTrue(gone(worker_pid), "the worker outlived its caller")
        self.assertTrue(gone(grandchild_pid), "its child outlived it")


# --------------------------------------------------------- when to start again
class TestLifecycle(WarmCase):

    def test_recycled_after_so_many_runs(self):
        worker = self.warm(recycle_after=2)
        pids = [self.pid(worker) for _ in range(3)]
        self.assertEqual(pids[0], pids[1])
        self.assertNotEqual(pids[1], pids[2])
        self.assertEqual(worker.counts["recycled"], 1)
        self.assertTrue(gone(pids[0]))

    def test_an_idle_process_leaves_by_itself(self):
        worker = self.warm(idle_seconds=1.2)
        first = self.pid(worker)
        time.sleep(2.0)
        self.assertFalse(worker.status()["alive"], "it should have left after 1.2s")
        self.assertNotEqual(self.pid(worker), first)

    def test_a_changed_environment_starts_another(self):
        worker = self.warm()
        first = self.pid(worker)
        self.assertEqual(self.pid(worker), first)
        env = dict(FIXTURE["env"], YTMEER_TEST_EXTRA="1")
        self.assertNotEqual(self.pid(worker, env=env), first)
        self.assertEqual(self.run_warm(worker, "--env", "YTMEER_TEST_EXTRA", env=env).stdout,
                         "env=1\n")

    def test_an_upgraded_yt_dlp_starts_another(self):
        worker = self.warm()
        first = self.pid(worker)
        later = time.time() + 5
        os.utime(FIXTURE["version"], (later, later))
        self.assertNotEqual(self.pid(worker), first)

    def test_a_busy_worker_declines_rather_than_waits(self):
        worker = self.warm()
        with worker._lock:
            self.assertIsNone(worker.run(["yt-dlp", "--pid"], env=FIXTURE["env"], **TEXT))
        self.assertIn("busy", worker.problem)

    def test_stop_lets_it_go(self):
        worker = self.warm()
        first = self.pid(worker)
        worker.stop()
        self.assertTrue(gone(first))
        self.assertFalse(worker.status()["alive"])


class TestDeclines(WarmCase):

    def test_only_a_captured_yt_dlp_run_is_taken(self):
        worker = self.warm()
        env = FIXTURE["env"]
        for cmd, kwargs in ((["ffprobe", "x"], TEXT), (["yt-dlp", "--pid"], {"text": True}),
                            (["yt-dlp", "--pid"], dict(TEXT, input="x")),
                            (["yt-dlp", "--pid"], dict(TEXT, cwd="/"))):
            with self.subTest(cmd=cmd, kwargs=kwargs):
                self.assertIsNone(worker.run(cmd, env=env, **kwargs))
        self.assertEqual(worker.counts["starts"], 0)

    def test_a_cli_that_is_not_a_python_script_is_not_kept_warm(self):
        shell_dir = tempfile.mkdtemp(dir=FIXTURE["root"])
        with open(os.path.join(shell_dir, "yt-dlp"), "w") as f:
            f.write("#!/bin/sh\necho from-the-shell  # not yt_dlp's console script\n")
        os.chmod(os.path.join(shell_dir, "yt-dlp"), 0o755)
        env = dict(FIXTURE["env"], PATH=shell_dir + os.pathsep + FIXTURE["env"]["PATH"])
        worker = self.warm()
        self.assertIsNone(worker.run(["yt-dlp", "--pid"], env=env, **TEXT))
        self.assertIn("not a Python script", worker.problem)
        saved = downloader.WARM_YTDLP
        downloader.WARM_YTDLP = worker
        try:
            out = downloader.run_logged("metadata", ["yt-dlp"], capture_output=True,
                                        text=True, timeout=30, env=env)
        finally:
            downloader.WARM_YTDLP = saved
        self.assertEqual(out.stdout, "from-the-shell\n", "run the usual way instead")

    def test_a_yt_dlp_that_cannot_import_is_not_retried_at_once(self):
        broken = tempfile.mkdtemp(dir=FIXTURE["root"])
        os.makedirs(os.path.join(broken, "yt_dlp"))
        with open(os.path.join(broken, "yt_dlp", "__init__.py"), "w") as f:
            f.write("raise ImportError('this yt_dlp is broken')\n")
        env = dict(FIXTURE["env"], PYTHONPATH=broken)
        worker = self.warm()
        self.assertIsNone(worker.run(["yt-dlp", "--pid"], env=env, **TEXT))
        self.assertIn("this yt_dlp is broken", worker.problem)
        self.assertIsNone(worker.run(["yt-dlp", "--pid"], env=env, **TEXT))
        self.assertIn("not retried", worker.problem)
        self.assertEqual(worker.counts["starts"], 0)


# -------------------------------------------------------------- the shebang
class TestCliCommand(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(dir=FIXTURE["root"])

    def script(self, text, binary=False):
        path = os.path.join(self.dir, f"yt-dlp-{time.monotonic_ns()}")
        with open(path, "wb") as f:
            f.write(text if binary else text.encode())
        os.chmod(path, 0o755)
        return path

    def test_the_shapes_pip_writes(self):
        python = sys.executable
        body = "import sys\nfrom yt_dlp import main\n"
        self.assertEqual(ytdlp_worker.cli_command(self.script(f"#!{python}\n{body}")), [python])
        self.assertEqual(ytdlp_worker.cli_command(self.script(f"#!{python} -s\n{body}")),
                         [python, "-s"])
        sh = f"#!/bin/sh\n'''exec' \"{python}\" \"$0\" \"$@\"\n' '''\n{body}"
        self.assertEqual(ytdlp_worker.cli_command(self.script(sh)), [python])
        link_dir = tempfile.mkdtemp(dir=self.dir)
        os.symlink(python, os.path.join(link_dir, "python3"))
        self.assertEqual(ytdlp_worker.cli_command(self.script(f"#!/usr/bin/env python3\n{body}"),
                                                  link_dir),
                         [os.path.join(link_dir, "python3")])

    def test_what_is_not_a_python_console_script(self):
        python, body = sys.executable, "from yt_dlp import main\n"
        for text in (f"#!/bin/bash\nexec {python} -m yt_dlp \"$@\"\n",   # a shell wrapper
                     f"#!{python}\nprint('no yt-dlp here')\n",
                     f"{body}",                                      # no shebang
                     f"#!/no/such/python3\n{body}",
                     f"#!{python}\n{body}" + "#" * 20000):           # too big for a script
            with self.subTest(text=text[:40]):
                self.assertIsNone(ytdlp_worker.cli_command(self.script(text)))
        zipapp = f"#!/usr/bin/env python3\n".encode() + b"PK\x03\x04\x00\x00yt_dlp"
        self.assertIsNone(ytdlp_worker.cli_command(self.script(zipapp, binary=True)))
        self.assertIsNone(ytdlp_worker.cli_command(os.path.join(self.dir, "missing")))


class TestFraming(unittest.TestCase):

    def test_round_trip_end_and_deadline(self):
        r, w = os.pipe()
        try:
            # Bigger than a pipe holds, so it is written while being read.
            message = {"id": 1, "argv": ["ا", "x" * 200000]}
            writer = threading.Thread(target=ytdlp_worker.send_frame, args=(w, message))
            writer.start()
            self.assertEqual(ytdlp_worker.recv_frame(r), message)
            writer.join()
            with self.assertRaises(ytdlp_worker._Late):
                ytdlp_worker.recv_frame(r, time.monotonic() + 0.05)
            os.write(w, b"\x00\x00")                 # half a header, then the end
            os.close(w)
            w = None
            with self.assertRaises(EOFError):
                ytdlp_worker.recv_frame(r)
        finally:
            os.close(r)
            if w is not None:
                os.close(w)

    def test_a_clean_end_is_none(self):
        r, w = os.pipe()
        os.close(w)
        try:
            self.assertIsNone(ytdlp_worker.recv_frame(r))
        finally:
            os.close(r)


class TestCompleted(unittest.TestCase):

    def test_text_mode_decodes_and_translates_newlines_as_subprocess_does(self):
        out = ytdlp_worker.completed(["yt-dlp"], 0, b"a\r\nb\rc\xff\n", b"", TEXT)
        self.assertEqual(out.stdout, "a\nb\nc�\n")
        self.assertEqual(out.stderr, "")
        raw = ytdlp_worker.completed(["yt-dlp"], 2, b"a\r\n", b"e", {"capture_output": True})
        self.assertEqual((raw.returncode, raw.stdout, raw.stderr), (2, b"a\r\n", b"e"))


# ----------------------------------------------------- who gets the warm path
class TestOptIn(unittest.TestCase):
    """Only api's engine slots run warm; the CLI, jobs and tests never do."""

    def setUp(self):
        self.saved = (api.ENGINE, api.WARM_YTDLP, downloader.WARM_YTDLP,
                      downloader.subprocess.run)
        self.calls = []

        def recorded(cmd, **kwargs):
            self.calls.append(("subprocess", cmd[1] if len(cmd) > 1 else cmd[0]))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        downloader.subprocess.run = recorded

    def tearDown(self):
        api.ENGINE.stop_warm()
        (api.ENGINE, api.WARM_YTDLP, downloader.WARM_YTDLP,
         downloader.subprocess.run) = self.saved

    def recorder(self):
        calls = self.calls

        class Recorder:
            problem = None

            def run(self, cmd, **kwargs):
                calls.append(("warm", cmd[1]))
                return subprocess.CompletedProcess(cmd, 0, "", "")
        return Recorder()

    def test_off_by_default(self):
        self.assertIsNone(downloader.WARM_YTDLP)
        self.assertIsNone(api._engine_copy(99).WARM_YTDLP, "nor in a fresh slot module")
        del self.calls[:]
        downloader.run_logged("metadata", ["yt-dlp", "metadata"], capture_output=True,
                              text=True, timeout=5)
        self.assertEqual(self.calls, [("subprocess", "metadata")])

    def test_only_the_single_shot_stages_run_warm(self):
        downloader.WARM_YTDLP = self.recorder()
        for stage in ("metadata", "comments", "caption", "channel_page", "search_dated",
                      "search_dates", "thumbnail", "channel_recent", "ffprobe"):
            downloader.run_logged(stage, ["yt-dlp", stage], capture_output=True, text=True,
                                  timeout=5)
        warm = sorted(stage for kind, stage in self.calls if kind == "warm")
        self.assertEqual(warm, sorted(downloader.WARM_STAGES))
        self.assertEqual(sorted(downloader.WARM_STAGES),
                         ["caption", "channel_page", "comments", "metadata"])
        self.assertIn(("subprocess", "search_dated"), self.calls)
        self.assertIn(("subprocess", "search_dates"), self.calls)

    def test_a_slot_has_its_own_worker_for_the_request_only(self):
        api.WARM_YTDLP = True
        api.ENGINE = api.EnginePool(2)
        with api.downloader_globals() as first:
            self.assertIsInstance(first.WARM_YTDLP, ytdlp_worker.WarmYtdlp)
            with api.downloader_globals() as second:
                self.assertIsInstance(second.WARM_YTDLP, ytdlp_worker.WarmYtdlp)
                self.assertIsNot(first.WARM_YTDLP, second.WARM_YTDLP)
                self.assertIsNot(first, second)
            self.assertIsNone(second.WARM_YTDLP, "put back the moment the request ends")
        self.assertIsNone(first.WARM_YTDLP)
        self.assertIsNone(downloader.WARM_YTDLP, "slot 0 is the downloader module itself")
        with api.downloader_globals() as again:
            self.assertIs(again.WARM_YTDLP, api.ENGINE.warm(0), "the same worker next time")

    def test_switched_off(self):
        api.WARM_YTDLP = False
        api.ENGINE = api.EnginePool(1)
        with api.downloader_globals() as d:
            self.assertIsNone(d.WARM_YTDLP)
        self.assertFalse(api.engine_status()["warm_ytdlp"])

    def test_health_counts_the_warm_processes(self):
        api.WARM_YTDLP = True
        api.ENGINE = api.EnginePool(2)
        status = api.engine_status()
        self.assertEqual((status["warm_ytdlp"], status["warm_processes"], status["warm_runs"]),
                         (True, 0, 0))

    def test_jobs_run_the_downloader_as_a_process_of_its_own(self):
        """A job's downloader is a fresh interpreter: WARM_YTDLP is None there."""
        opts = api.normalize_request({"links": ["https://youtu.be/aaaaaaaaaaa"]})
        argv = api.build_argv(opts["outdir"], opts)
        self.assertEqual(argv[:3], [sys.executable, "-u", api.DOWNLOADER])


class TestEndToEnd(WarmCase):
    """A real lookup function, through a real engine slot, on the fake yt-dlp."""

    def setUp(self):
        super().setUp()
        self.saved_env = {k: os.environ.get(k) for k in ("PATH", "PYTHONPATH")}
        os.environ["PATH"] = FIXTURE["env"]["PATH"]
        os.environ["PYTHONPATH"] = FIXTURE["env"]["PYTHONPATH"]
        self.saved = api.ENGINE, api.WARM_YTDLP
        api.WARM_YTDLP = True
        api.ENGINE = api.EnginePool(2)

    def tearDown(self):
        api.ENGINE.stop_warm()
        api.ENGINE, api.WARM_YTDLP = self.saved
        for key, value in self.saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        super().tearDown()

    def test_a_slot_answers_warm_and_the_plain_module_does_not(self):
        url = "https://www.youtube.com/watch?v=aaaaaaaaaaa"
        with api.downloader_globals() as d:
            first, err = d.fetch_info_once(url)
            self.assertIsNone(err)
            second, _ = d.fetch_info_once(url)
        self.assertEqual(first["runner"], "ytdlp_worker.py")
        self.assertEqual(first["pid"], second["pid"], "one warm process per slot")
        self.assertEqual(first["id"], "aaaaaaaaaaa")
        plain, err = downloader.fetch_info_once(url)
        self.assertIsNone(err)
        self.assertEqual(plain["runner"], "yt-dlp", "outside a slot: the CLI, as before")
        self.assertEqual(api.engine_status()["warm_processes"], 1)
        self.assertEqual(api.engine_status()["warm_runs"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
