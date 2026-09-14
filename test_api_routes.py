#!/usr/bin/env python3
"""The API over HTTP, in-process, against a stand-in downloader.

    python3 test_api_routes.py

Needs fastapi and httpx; skips without them. No network: the stand-in writes
video folders and stream lines on a timer, the way downloader.py does, so the
tests can watch videos arrive while a job is still running.
"""

import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.error
import urllib.request

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

import api

FAKE = textwrap.dedent('''
    import json, os, subprocess, sys, time
    out = sys.argv[1]
    links = sys.argv[sys.argv.index("--links") + 1] if "--links" in sys.argv else os.path.join(out, "links.txt")
    n, delay = int(os.environ.get("FAKE_N", "3")), float(os.environ.get("FAKE_DELAY", "0.3"))
    mode = os.environ.get("FAKE_MODE", "")
    stream = os.path.join(out, "download_log.jsonl")
    open(stream, "w").close()
    records = []

    def log(rec):
        records.append(rec)
        with open(stream, "a") as f:
            f.write(json.dumps(rec) + "\\n")

    try:
        log({"url": "https://www.youtube.com/@Ch", "status": "channel", "videos": n})
        with open(links) as f:
            print("links:", f.read().strip(), flush=True)
        if mode == "child":        # a busy, silent yt-dlp
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            with open(os.path.join(out, "child.pid"), "w") as f:
                f.write(str(child.pid))
        if mode == "sweep":
            log({"url": "https://youtu.be/flakyxxxxxx", "status": "failed", "error": "stream died"})
        for i in range(n):
            time.sleep(delay)
            name = f"Vid {i}"
            folder = os.path.join(out, "Ch", name)
            os.makedirs(folder, exist_ok=True)
            for fname in (f"{name}.mp4", f"trans_{name}.txt"):
                with open(os.path.join(folder, fname), "w") as f:
                    f.write(f"contents of {fname}")
            with open(os.path.join(folder, "videoinfo.txt"), "w") as f:    # as the real one does
                f.write(f"Title:   {name}\\nQuality: 720p\\nStatus:  OK\\n")
            log({"url": f"https://youtu.be/vid{i}xxxxxx", "status": "ok",
                 "title": name, "quality": "720p", "folder": f"Ch/{name}"})
            print(f"[{i+1}/{n}] OK {name}", flush=True)
        if mode == "crash":
            raise RuntimeError("disk full")
        if mode == "sweep":        # the final retry fixes it
            log({"url": "https://youtu.be/flakyxxxxxx", "status": "ok", "title": "Flaky",
                 "folder": "Ch/Vid 0"})
        log({"url": "https://youtu.be/deadxxxxxxx", "status": "unavailable",
             "error": "This video is unavailable"})
        stopped = False
    except KeyboardInterrupt:
        print("Stopped.", flush=True)
        stopped = True
    with open(os.path.join(out, "download_log.json"), "w") as f:
        json.dump({"run": {"downloaded": sum(r["status"] == "ok" for r in records),
                           "stopped_early": stopped}, "videos": records}, f)
''')


@unittest.skipUnless(HAVE_FASTAPI, "fastapi/httpx not installed")
class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake = os.path.join(self.tmp.name, "fake_downloader.py")
        with open(fake, "w") as f:
            f.write(FAKE)
        self.saved = (api.DOWNLOADER, api.DEFAULT_OUT, dict(api.JOBS))
        api.DOWNLOADER = fake
        api.DEFAULT_OUT = os.path.join(self.tmp.name, "out")
        api.JOBS.clear()
        api.configure_key("k", persist=False)
        os.environ["FAKE_N"], os.environ["FAKE_DELAY"] = "3", "0.3"
        os.environ.pop("FAKE_MODE", None)
        self.app = api.build_app()
        self.c = TestClient(self.app)
        self.h = {"X-API-Key": "k"}

    def tearDown(self):
        for job in list(api.JOBS.values()):
            if job["proc"] is not None or job["state"] in api.ACTIVE:
                api.stop_job(job)
        self.wait_all()
        api.DOWNLOADER, api.DEFAULT_OUT = self.saved[0], self.saved[1]
        api.JOBS.clear()
        api.JOBS.update(self.saved[2])
        api.API_KEY[0] = None
        self.tmp.cleanup()

    def wait_all(self, timeout=15):
        end = time.time() + timeout
        while time.time() < end and any(j["state"] in api.ACTIVE for j in api.JOBS.values()):
            time.sleep(0.05)

    def start(self, **body):
        body.setdefault("links", ["https://www.youtube.com/@Ch"])
        r = self.c.post("/api/jobs", json=body, headers=self.h)
        self.assertEqual(r.status_code, 202, r.text)
        return r.json()["job_id"]

    def wait(self, job_id, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            job = self.c.get(f"/api/jobs/{job_id}", headers=self.h).json()
            if job["state"] not in api.ACTIVE:
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    # ------------------------------------------------------------------ auth
    def test_ping_needs_no_key_and_everything_else_does(self):
        self.assertEqual(self.c.get("/api/ping").json()["auth_required"], True)
        for method, path in (("get", "/api/health"), ("get", "/api/jobs"),
                             ("get", "/api/preview?channel=x"), ("post", "/api/jobs"),
                             ("get", "/api/jobs/x"), ("get", "/api/jobs/x/videos"),
                             ("post", "/api/jobs/x/stop"), ("delete", "/api/jobs/x"),
                             ("get", "/api/jobs/x/files"), ("get", "/api/jobs/x/files/a.mp4")):
            self.assertEqual(getattr(self.c, method)(path).status_code, 401, path)
            bad = getattr(self.c, method)(path, headers={"X-API-Key": "nope"})
            self.assertEqual(bad.status_code, 401, path)

    def test_docs_list_every_endpoint(self):
        paths = self.c.get("/api/openapi.json").json()["paths"]
        self.assertEqual(sorted(paths), sorted([
            "/api/ping", "/api/health", "/api/preview", "/api/jobs",
            "/api/jobs/{job_id}", "/api/jobs/{job_id}/videos",
            "/api/jobs/{job_id}/stop", "/api/jobs/{job_id}/files",
            "/api/jobs/{job_id}/files/{path}"]))
        self.assertEqual(self.c.get("/api/docs").status_code, 200)

    # ------------------------------------------------------------ validation
    def test_bad_requests_are_refused_with_the_reason(self):
        for body in ({}, {"links": "x", "view": 5}, {"links": "x", "limit": -1}):
            self.assertEqual(self.c.post("/api/jobs", json=body, headers=self.h).status_code,
                             422, body)
        for body in ({"links": "x", "min_height": 1080, "max_height": 720},
                     {"links": "x", "max_height": 0}):
            r = self.c.post("/api/jobs", json=body, headers=self.h)
            self.assertEqual(r.status_code, 422, body)
            detail = r.json()["detail"]         # one shape for every 422: a list
            self.assertIsInstance(detail, list)
            # the field is named in "loc" (pydantic) or in "msg" (our own checks)
            self.assertTrue(any("height" in e["msg"] + " ".join(map(str, e["loc"]))
                                for e in detail), detail)

    def test_unknown_job_is_404(self):
        self.assertEqual(self.c.get("/api/jobs/job_nope", headers=self.h).status_code, 404)

    # ------------------------------------------------------------- the point
    def test_videos_arrive_while_the_job_is_still_running(self):
        os.environ["FAKE_DELAY"] = "0.6"
        job_id = self.start()
        got, since, saw_partial = [], 0, False
        end = time.time() + 20
        while time.time() < end:
            page = self.c.get(f"/api/jobs/{job_id}/videos?since={since}",
                              headers=self.h).json()
            got += page["videos"]
            if page["videos"] and not page["done"] and len(got) < 3:
                saw_partial = True
            since = page["next"]
            if page["done"]:
                break
            time.sleep(0.05)
        self.assertTrue(saw_partial, "every video only showed up after the job ended")
        self.assertEqual([v["title"] for v in got if v["status"] == "ok"],
                         ["Vid 0", "Vid 1", "Vid 2"])           # in order, once each
        self.assertEqual(len(got), 4)                          # + the failure
        for v in got[:3]:
            self.assertTrue(os.path.isfile(v["files"]["video"]))
        self.assertEqual(got[3]["error"], "This video is unavailable")
        again = self.c.get(f"/api/jobs/{job_id}/videos?since={since}", headers=self.h).json()
        self.assertEqual((again["videos"], again["done"]), ([], True))

    def test_a_finished_job_reports_summary_and_failures(self):
        job = self.wait(self.start())
        self.assertEqual(job["state"], "finished")
        self.assertEqual(job["summary"]["downloaded"], 3)
        self.assertEqual(job["videos_done"], 3)                # videos, not records
        self.assertEqual([f["status"] for f in job["failures"]], ["unavailable"])
        self.assertIn("[3/3] OK Vid 2", job["log"])

    def test_the_links_reach_the_downloader(self):
        folder = os.path.join(api.DEFAULT_OUT, "kept")
        os.makedirs(folder)
        with open(os.path.join(folder, "links.txt"), "w") as f:
            f.write("https://youtu.be/handkept123\n")
        job = self.wait(self.start(outdir="kept", links=["https://youtu.be/aaaaaaaaaaa",
                                                         "https://www.youtube.com/@Ch"]))
        self.assertIn("links: https://youtu.be/aaaaaaaaaaa\nhttps://www.youtube.com/@Ch",
                      "\n".join(job["log"]))
        with open(os.path.join(folder, "links.txt")) as f:          # never overwritten
            self.assertEqual(f.read(), "https://youtu.be/handkept123\n")

    def test_a_second_job_in_the_same_folder_is_refused(self):
        os.environ["FAKE_DELAY"] = "1"
        self.start()
        r = self.c.post("/api/jobs", json={"links": "x"}, headers=self.h)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.c.post("/api/jobs", json={"links": "x", "outdir": "other"},
                                     headers=self.h).status_code, 202)

    def test_stop_keeps_what_finished_and_still_summarises(self):
        os.environ["FAKE_N"], os.environ["FAKE_DELAY"] = "50", "0.4"
        job_id = self.start()
        end = time.time() + 10
        while time.time() < end and not self.c.get(
                f"/api/jobs/{job_id}/videos", headers=self.h).json()["videos"]:
            time.sleep(0.05)
        self.assertTrue(self.c.post(f"/api/jobs/{job_id}/stop", headers=self.h).json()["stopping"])
        job = self.wait(job_id)
        self.assertEqual(job["state"], "stopped")
        self.assertTrue(job["summary"]["stopped_early"])
        self.assertGreaterEqual(job["videos_done"], 1)
        self.assertLess(job["videos_done"], 50)

    def test_a_running_job_cannot_be_deleted_a_finished_one_can(self):
        os.environ["FAKE_DELAY"] = "1"
        job_id = self.start()
        self.assertEqual(self.c.delete(f"/api/jobs/{job_id}", headers=self.h).status_code, 409)
        self.c.post(f"/api/jobs/{job_id}/stop", headers=self.h)
        self.wait(job_id)
        self.assertEqual(self.c.delete(f"/api/jobs/{job_id}", headers=self.h).status_code, 200)
        self.assertEqual(self.c.get(f"/api/jobs/{job_id}", headers=self.h).status_code, 404)

    def test_a_new_run_does_not_inherit_the_last_ones_videos(self):
        first = self.wait(self.start())
        os.environ["FAKE_N"] = "1"
        second_id = self.start()
        self.wait(second_id)
        second = self.c.get(f"/api/jobs/{second_id}/videos", headers=self.h).json()
        self.assertEqual(len(second["videos"]), 2)           # its 1 + its failure
        old = self.c.get(f"/api/jobs/{first['job_id']}/videos", headers=self.h).json()
        self.assertEqual(len(old["videos"]), 4)               # snapshot, not overwritten

    def test_a_crash_is_an_error_with_the_reason_not_finished(self):
        os.environ["FAKE_MODE"] = "crash"
        job = self.wait(self.start())
        self.assertEqual(job["state"], "error")
        self.assertIn("exited with code 1", job["error"])
        self.assertIn("disk full", job["error"])
        self.assertEqual(job["videos_done"], 3)         # what finished before still counts

    def test_a_failure_the_final_retry_fixed_is_not_reported(self):
        os.environ["FAKE_MODE"] = "sweep"
        job = self.wait(self.start())
        self.assertEqual([f["url"] for f in job["failures"]], ["https://youtu.be/deadxxxxxxx"])

    def test_a_stop_while_queued_stops_it_as_soon_as_it_starts(self):
        os.environ["FAKE_N"], os.environ["FAKE_DELAY"] = "50", "0.2"
        held = []
        real_thread = api.threading.Thread

        class Held:                                # a thread that does not start yet
            def __init__(self, target, args, **kw):
                held.append((target, args))
            def start(self):
                pass

        api.threading.Thread = Held
        try:
            job_id = self.start()
        finally:
            api.threading.Thread = real_thread
        job = api.JOBS[job_id]
        self.assertTrue(api.stop_job(job))         # queued: accepted
        began = time.time()
        held[0][0](*held[0][1])                    # now let it run
        self.assertLess(time.time() - began, 5, "the whole batch ran despite the stop")
        self.assertEqual(job["state"], "stopped")

    @unittest.skipUnless(os.name == "posix", "process groups")
    def test_stop_takes_the_running_yt_dlp_with_it(self):
        os.environ["FAKE_MODE"], os.environ["FAKE_N"], os.environ["FAKE_DELAY"] = "child", "50", "0.2"
        job_id = self.start()
        pid_file = os.path.join(api.DEFAULT_OUT, "child.pid")
        end = time.time() + 10
        while not os.path.exists(pid_file) and time.time() < end:
            time.sleep(0.05)
        with open(pid_file) as f:
            child = int(f.read())
        self.c.post(f"/api/jobs/{job_id}/stop", headers=self.h)
        self.wait(job_id)
        end = time.time() + 5
        while time.time() < end:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        os.kill(child, 9)
        self.fail("yt-dlp kept running after the job stopped")

    def test_a_channel_preview_does_not_hold_up_the_jobs(self):
        d = api.downloader
        saved = (d.is_channel_url, d.list_channel_videos, d.channel_videos_url)
        d.is_channel_url = lambda url: True
        d.channel_videos_url = lambda url: url
        d.list_channel_videos = lambda url: (time.sleep(3), ([], "Ch", None))[1]
        try:
            slow = TestClient(self.app)
            import threading as _t
            t = _t.Thread(target=lambda: slow.get("/api/preview?channel=x", headers=self.h))
            t.start()
            time.sleep(0.4)
            began = time.time()
            self.assertEqual(self.c.get("/api/jobs", headers=self.h).status_code, 200)
            self.assertLess(time.time() - began, 1.0, "jobs waited for the preview")
            t.join()
        finally:
            d.is_channel_url, d.list_channel_videos, d.channel_videos_url = saved

    def test_ping_says_whether_anything_is_still_downloading(self):
        self.assertFalse(self.c.get("/api/ping").json()["busy"])
        os.environ["FAKE_DELAY"] = "0.5"
        job_id = self.start()
        self.assertTrue(self.c.get("/api/ping").json()["busy"])
        self.wait(job_id)
        self.assertFalse(self.c.get("/api/ping").json()["busy"])

    @unittest.skipUnless(os.name == "posix", "process groups")
    def test_shutting_the_server_down_stops_its_downloads(self):
        # Downloaders run in their own process groups, so without this they would
        # outlive the server and keep writing into a folder nobody is watching.
        here = os.path.dirname(os.path.abspath(__file__))
        for sig in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=sig.name):
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                out = os.path.join(self.tmp.name, f"shutdown_{sig.name}")
                runner = os.path.join(self.tmp.name, "runner.py")
                with open(runner, "w") as f:
                    f.write(f"import sys\nsys.path.insert(0, {here!r})\nimport api\n"
                            f"api.DOWNLOADER = {api.DOWNLOADER!r}\n"
                            f"api.main(['--port', '{port}', '--no-key', '--outdir', {out!r}])\n")
                env = dict(os.environ, FAKE_MODE="child", FAKE_N="100", FAKE_DELAY="0.2")
                server = subprocess.Popen([sys.executable, runner], env=env,
                                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    base = f"http://127.0.0.1:{port}/api"
                    for _ in range(100):
                        try:
                            urllib.request.urlopen(base + "/ping", timeout=1)
                            break
                        except OSError:
                            time.sleep(0.1)
                    urllib.request.urlopen(urllib.request.Request(
                        base + "/jobs", method="POST", data=b'{"links": ["x"]}',
                        headers={"Content-Type": "application/json"}), timeout=5)
                    pid_file = os.path.join(out, "child.pid")
                    for _ in range(100):
                        if os.path.exists(pid_file):
                            break
                        time.sleep(0.1)
                    with open(pid_file) as f:
                        child = int(f.read())
                    server.send_signal(sig)
                    server.wait(timeout=30)
                    for _ in range(50):
                        try:
                            os.kill(child, 0)
                        except ProcessLookupError:
                            break
                        time.sleep(0.1)
                    else:
                        os.kill(child, 9)
                        self.fail(f"a download kept running after {sig.name}")
                    self.assertTrue(os.path.exists(os.path.join(out, "download_log.json")),
                                    "the downloader was killed rather than stopped")
                finally:
                    if server.poll() is None:
                        server.kill()

    def test_each_finished_video_can_be_downloaded_through_the_api(self):
        job_id = self.start()
        self.wait(job_id)
        videos = [v for v in self.c.get(f"/api/jobs/{job_id}/videos", headers=self.h).json()["videos"]
                  if v["status"] == "ok"]
        self.assertEqual(len(videos), 3)
        for v in videos:
            r = self.c.get("/api" + v["downloads"]["video"], headers=self.h)
            self.assertEqual(r.status_code, 200)
            with open(v["files"]["video"], "rb") as f:
                self.assertEqual(r.content, f.read())
            self.assertIn("attachment", r.headers["content-disposition"])
            self.assertEqual(self.c.get("/api" + v["downloads"]["transcript"], headers=self.h).text,
                             f"contents of trans_{v['title']}.txt")
        self.assertEqual(self.c.get("/api" + videos[0]["downloads"]["video"]).status_code, 401)

    def test_a_download_can_be_fetched_in_parts(self):
        job_id = self.start()
        self.wait(job_id)
        url = "/api" + self.c.get(f"/api/jobs/{job_id}/videos", headers=self.h).json()["videos"][0]["downloads"]["video"]
        r = self.c.get(url, headers={**self.h, "Range": "bytes=0-7"})
        self.assertEqual((r.status_code, r.content), (206, b"contents"))

    def test_the_file_listing_and_what_it_refuses(self):
        job_id = self.start()
        job = self.wait(job_id)
        folder = job["folder"]
        for name in ("cookies.txt", "Ch/Vid 0/Vid 0.f137.mp4.part", ".api_key"):
            with open(os.path.join(folder, name), "w") as f:
                f.write("secret")
        listed = self.c.get(f"/api/jobs/{job_id}/files", headers=self.h).json()["files"]
        paths = {f["path"] for f in listed}
        self.assertIn("Ch/Vid 0/Vid 0.mp4", paths)
        self.assertFalse(paths & {"cookies.txt", ".api_key", "Ch/Vid 0/Vid 0.f137.mp4.part"})
        for f in listed:
            self.assertEqual(self.c.get("/api" + f["url"], headers=self.h).status_code, 200, f)
        base = f"/api/jobs/{job_id}/files/"
        for bad, code in (("cookies.txt", 403), ("COOKIES.TXT", 403), (".api_key", 403),
                          ("Ch/Vid%200/Vid%200.f137.mp4.part", 403), ("%2e%2e/%2e%2e/etc/passwd", 403),
                          ("..%2F..%2Fsecret", 403),
                          ("nope.mp4", 403),                  # outside a video folder: refused unseen
                          ("Ch/Vid%200/nope.mp4", 404)):     # inside one: simply not there
            self.assertEqual(self.c.get(base + bad, headers=self.h).status_code, code, bad)
        self.assertEqual(self.c.get("/api/jobs/job_nope/files/x.mp4", headers=self.h).status_code, 404)

    def test_requests_the_live_test_broke_things_with_are_refused(self):
        for body in ({"links": "x", "limit": 10**20}, {"links": "x", "views": 10**13},
                     {"links": "x", "outdir": "/etc"}, {"links": "x", "outdir": "../../x"},
                     {"links": "# only a comment"}, {"links": "x", "sub_lang": "-x"}):
            r = self.c.post("/api/jobs", json=body, headers=self.h)
            self.assertEqual(r.status_code, 422, body)
            self.assertIsInstance(r.json()["detail"], list)
        self.assertEqual(self.c.get("/api/jobs", headers=self.h).status_code, 200)

    def test_preview_limit_is_bounded(self):
        for q in ("limit=0", "limit=501", "views=10000000000000"):
            r = self.c.get(f"/api/preview?channel=https://www.youtube.com/@X&{q}", headers=self.h)
            self.assertEqual(r.status_code, 422, q)

    def test_a_second_preview_meanwhile_gets_503(self):
        d = api.downloader
        saved = (d.is_channel_url, d.list_channel_videos, d.channel_videos_url, api.PREVIEW_WAIT)
        d.is_channel_url, d.channel_videos_url = (lambda url: True), (lambda url: url)
        d.list_channel_videos = lambda url: (time.sleep(2), ([], "Ch", None))[1]
        api.PREVIEW_WAIT = 0.3
        try:
            import threading as _t
            first = _t.Thread(target=lambda: TestClient(self.app).get("/api/preview?channel=x", headers=self.h))
            first.start()
            time.sleep(0.3)
            r = self.c.get("/api/preview?channel=y", headers=self.h)
            self.assertEqual(r.status_code, 503)
            self.assertIn("another channel preview", r.json()["detail"])
            first.join()
            self.assertEqual(self.c.get("/api/preview?channel=z", headers=self.h).status_code, 200)
        finally:
            d.is_channel_url, d.list_channel_videos, d.channel_videos_url, api.PREVIEW_WAIT = saved

    def test_health_reports_the_code_version(self):
        body = self.c.get("/api/health", headers=self.h).json()
        self.assertEqual(body["code"], api.code_version())
        self.assertIsNone(body["public_url"])                  # not served with --share



    def test_preview_rejects_a_non_channel_link(self):
        r = self.c.get("/api/preview?channel=https://youtu.be/aaaaaaaaaaa", headers=self.h)
        self.assertEqual(r.status_code, 422)

    def test_the_output_pipe_is_closed_when_a_job_ends(self):
        made, real = [], api.subprocess.Popen

        def spy(*args, **kwargs):
            made.append(real(*args, **kwargs))
            return made[-1]

        api.subprocess.Popen = spy
        try:
            os.environ["FAKE_N"], os.environ["FAKE_DELAY"] = "1", "0"
            self.wait(self.start())
        finally:
            api.subprocess.Popen = real
        self.assertTrue(made and made[0].stdout.closed)

    def test_health(self):
        body = self.c.get("/api/health", headers=self.h).json()
        self.assertEqual(body["default_folder"], api.DEFAULT_OUT)
        self.assertIn("yt-dlp", body["tools"])


@unittest.skipUnless(importlib.util.find_spec("gradio"), "gradio is not installed")
class TestPublicServer(unittest.TestCase):
    """--share: the same API and docs, on Gradio's server (here without the tunnel)."""

    def test_the_api_and_its_docs_ride_on_gradio(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        api.configure_key("k", persist=False)
        demo = api.serve_public("127.0.0.1", port, share=False)
        base = f"http://127.0.0.1:{port}"

        def get(path, key=None):
            req = urllib.request.Request(base + path, headers={"X-API-Key": key} if key else {})
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                e.close()
                return e.code, ""

        try:
            self.assertEqual(get("/api/ping")[0], 200)
            self.assertEqual(get("/api/health")[0], 401)
            status, body = get("/api/health", key="k")
            self.assertEqual(status, 200)
            self.assertIn("public_url", json.loads(body))
            status, body = get("/api/openapi.json")
            paths = json.loads(body)["paths"]
            self.assertIn("/api/jobs/{job_id}/videos", paths)    # this API's schema,
            self.assertFalse(any(p.startswith("/gradio_api") for p in paths))  # not Gradio's
            status, body = get("/api/docs")
            self.assertEqual(status, 200)
            self.assertIn("/api/openapi.json", body)
            self.assertEqual(get("/")[0], 200)                   # Gradio's page is still there
        finally:
            demo.close()
            api.API_KEY[0] = None

    @unittest.skipUnless(os.name == "posix", "process groups")
    def test_share_mode_serves_then_stops_its_downloads_on_sigterm(self):
        # The process cell 2 starts: python3 api.py --share. The tunnel is left out
        # here (nothing public is opened); everything else is the real path.
        here = os.path.dirname(os.path.abspath(__file__))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with tempfile.TemporaryDirectory() as tmp:
            fake = os.path.join(tmp, "fake_downloader.py")
            with open(fake, "w") as f:
                f.write(FAKE)
            out = os.path.join(tmp, "out")
            runner = os.path.join(tmp, "runner.py")
            with open(runner, "w") as f:
                f.write(f"import functools, sys\nsys.path.insert(0, {here!r})\nimport api\n"
                        f"api.DOWNLOADER = {fake!r}\n"
                        "api.serve_public = functools.partial(api.serve_public, share=False)\n"
                        f"api.main(['--share', '--port', '{port}', '--no-key', '--outdir', {out!r}])\n")
            env = dict(os.environ, FAKE_MODE="child", FAKE_N="100", FAKE_DELAY="0.2")
            server = subprocess.Popen([sys.executable, runner], env=env,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"http://127.0.0.1:{port}/api"
                for _ in range(300):
                    try:
                        urllib.request.urlopen(base + "/health", timeout=1).close()
                        break
                    except urllib.error.HTTPError as e:   # 404 until the routes are added
                        e.close()
                        time.sleep(0.1)
                    except OSError:
                        time.sleep(0.1)
                with urllib.request.urlopen(base + "/health", timeout=5) as r:
                    self.assertIsNone(json.load(r)["public_url"])
                urllib.request.urlopen(urllib.request.Request(
                    base + "/jobs", method="POST", data=b'{"links": ["x"]}',
                    headers={"Content-Type": "application/json"}), timeout=5).close()
                pid_file = os.path.join(out, "child.pid")
                for _ in range(100):
                    if os.path.exists(pid_file):
                        break
                    time.sleep(0.1)
                with open(pid_file) as f:
                    child = int(f.read())
                server.send_signal(signal.SIGTERM)
                server.wait(timeout=40)
                for _ in range(50):
                    try:
                        os.kill(child, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.1)
                else:
                    os.kill(child, 9)
                    self.fail("a download kept running after the share server got SIGTERM")
                self.assertTrue(os.path.exists(os.path.join(out, "download_log.json")))
            finally:
                if server.poll() is None:
                    server.kill()


if __name__ == "__main__":
    unittest.main(verbosity=2)
