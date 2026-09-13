#!/usr/bin/env python3
"""Tests for api.py's logic - no network, no downloads, no fastapi needed.

    python3 test_api.py
"""

import json
import os
import tempfile
import threading
import unittest

import api


class TestNormalizeRequest(unittest.TestCase):
    def test_links_are_required(self):
        for bad in ({}, {"links": ""}, {"links": "  \n"}, {"links": []}):
            with self.assertRaises(ValueError):
                api.normalize_request(bad)

    def test_links_may_be_a_list(self):
        opts = api.normalize_request({"links": ["https://youtu.be/aaaaaaaaaaa",
                                               "https://youtu.be/bbbbbbbbbbb"]})
        self.assertEqual(opts["links"],
                         "https://youtu.be/aaaaaaaaaaa\nhttps://youtu.be/bbbbbbbbbbb")

    def test_defaults(self):
        opts = api.normalize_request({"links": "x"})
        self.assertEqual((opts["views"], opts["limit"], opts["min_height"],
                          opts["max_height"], opts["sub_lang"]), (0, 0, 720, 1080, "en"))
        self.assertIsNone(opts["skip"])            # left out = keep skip.txt

    def test_a_misspelled_field_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            api.normalize_request({"links": "x", "view": 1000})
        self.assertIn("view", str(caught.exception))

    def test_numbers_may_arrive_as_strings(self):
        opts = api.normalize_request({"links": "x", "views": "1000", "limit": "3"})
        self.assertEqual((opts["views"], opts["limit"]), (1000, 3))

    def test_bad_numbers_are_refused(self):
        for bad in ({"views": "lots"}, {"limit": -1}):
            with self.assertRaises(ValueError):
                api.normalize_request(dict(links="x", **bad))

    def test_an_inverted_height_range_is_refused(self):
        with self.assertRaises(ValueError):
            api.normalize_request({"links": "x", "min_height": 1080, "max_height": 720})

    def test_a_height_of_zero_is_refused(self):
        # 0 means "no limit" for views and limit, but no format has height 0.
        for field in ("min_height", "max_height"):
            with self.assertRaises(ValueError):
                api.normalize_request({"links": "x", field: 0})

    def test_skip_given_as_a_list_or_empty(self):
        self.assertEqual(api.normalize_request({"links": "x", "skip": ["a", "b"]})["skip"], "a\nb")
        self.assertEqual(api.normalize_request({"links": "x", "skip": ""})["skip"], "")

    def test_the_folder_is_made_absolute(self):
        opts = api.normalize_request({"links": "x", "outdir": "~/somewhere"})
        self.assertTrue(os.path.isabs(opts["outdir"]))
        self.assertNotIn("~", opts["outdir"])

    def test_a_relative_folder_goes_inside_the_default_one(self):
        # Not wherever the server was started: on Colab that is not the Drive
        # folder cell 1 chose.
        with tempfile.TemporaryDirectory() as tmp:
            saved, api.DEFAULT_OUT = api.DEFAULT_OUT, tmp
            try:
                opts = api.normalize_request({"links": "x", "outdir": "batch1"})
            finally:
                api.DEFAULT_OUT = saved
            self.assertEqual(opts["outdir"], os.path.join(os.path.realpath(tmp), "batch1"))

    def test_one_folder_spelled_two_ways_is_one_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "a"))
            one = api.normalize_request({"links": "x", "outdir": os.path.join(tmp, "a")})
            two = api.normalize_request({"links": "x", "outdir": os.path.join(tmp, "a", "..", "a")})
            self.assertEqual(one["outdir"], two["outdir"])


class TestArgv(unittest.TestCase):
    def argv(self, **over):
        return api.build_argv("/out", api.normalize_request(dict(links="x", **over)))

    def test_the_command_line(self):
        self.assertEqual(self.argv(views=1000, limit=3)[3:],
                         ["/out", "--channel", "--views", "1000", "--limit", "3",
                          "--min-height", "720", "--max-height", "1080", "--sub-lang", "en"])

    def test_zeros_emit_no_flag(self):
        argv = self.argv()
        self.assertNotIn("--views", argv)
        self.assertNotIn("--limit", argv)

    def test_switching_things_off(self):
        argv = self.argv(channel=False, subs=False, thumbnail=False,
                         description=False, verbose=True)
        for flag in ("--no-subs", "--no-thumbnail", "--no-description", "--verbose"):
            self.assertIn(flag, argv)
        self.assertNotIn("--channel", argv)
        self.assertNotIn("--sub-lang", argv)

    def test_a_links_file_is_passed_on(self):
        opts = api.normalize_request({"links": "x"})
        argv = api.build_argv("/out", opts, "/tmp/job.txt")
        self.assertEqual(argv[3:6], ["/out", "--links", "/tmp/job.txt"])

    def test_output_is_unbuffered(self):
        # Buffered, the log would arrive in blocks and a job would look stuck.
        self.assertEqual(self.argv()[1], "-u")


class TestKey(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (api.KEY_FILE, os.environ.pop("YTDL_API_KEY", None))
        api.KEY_FILE = os.path.join(self.tmp.name, ".api_key")

    def tearDown(self):
        api.KEY_FILE = self.saved[0]
        if self.saved[1] is not None:
            os.environ["YTDL_API_KEY"] = self.saved[1]
        api.API_KEY[0] = None
        self.tmp.cleanup()

    def test_a_generated_key_is_saved_owner_only_and_reused(self):
        first = api.configure_key()
        self.assertEqual(os.stat(api.KEY_FILE).st_mode & 0o777, 0o600)
        self.assertEqual(api.configure_key(), first)       # a restart keeps it

    def test_new_key_replaces_the_saved_one(self):
        first = api.configure_key()
        second = api.configure_key(rotate=True)
        self.assertNotEqual(first, second)
        self.assertEqual(api.configure_key(), second)

    def test_explicit_and_env_keys_win_and_are_not_saved(self):
        self.assertEqual(api.configure_key("given"), "given")
        os.environ["YTDL_API_KEY"] = "from-env"
        self.assertEqual(api.configure_key(), "from-env")
        self.assertFalse(os.path.exists(api.KEY_FILE))

    def test_checking(self):
        api.configure_key("hunter2", persist=False)
        self.assertTrue(api.key_ok("hunter2"))
        for bad in ("wrong", "", None):
            self.assertFalse(api.key_ok(bad))

    def test_authentication_off(self):
        self.assertIsNone(api.configure_key(enabled=False))
        self.assertTrue(api.key_ok(None))


class TestWriteInputs(unittest.TestCase):
    def test_the_links_go_to_a_file_of_their_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = api.write_inputs(tmp, "https://youtu.be/aaaaaaaaaaa")
            try:
                with open(path) as f:
                    self.assertEqual(f.read(), "https://youtu.be/aaaaaaaaaaa\n")
                self.assertFalse(os.path.exists(os.path.join(tmp, "links.txt")))
            finally:
                os.remove(path)

    def test_a_hand_kept_links_txt_in_the_folder_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            kept = os.path.join(tmp, "links.txt")
            with open(kept, "w") as f:
                f.write("https://youtu.be/handkept123\n")
            os.remove(api.write_inputs(tmp, "https://youtu.be/aaaaaaaaaaa"))
            with open(kept) as f:
                self.assertEqual(f.read(), "https://youtu.be/handkept123\n")

    def test_a_hand_kept_skip_list_survives_a_request_without_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "skip.txt")
            with open(path, "w") as f:
                f.write("bbbbbbbbbbb\n")
            os.remove(api.write_inputs(tmp, "x", skip=None))
            with open(path) as f:
                self.assertEqual(f.read(), "bbbbbbbbbbb\n")

    def test_a_given_skip_list_replaces_it_and_an_empty_one_clears_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "skip.txt")
            os.remove(api.write_inputs(tmp, "x", skip="ccccccccccc"))
            with open(path) as f:
                self.assertEqual(f.read(), "ccccccccccc\n")
            os.remove(api.write_inputs(tmp, "x", skip=""))
            self.assertFalse(os.path.exists(path))

    def test_cookies_are_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cookies.txt")
            with open(path, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
            os.remove(api.write_inputs(tmp, "x", skip=""))
            self.assertTrue(os.path.exists(path))


class TestReadVideos(unittest.TestCase):
    """The stream is read while the downloader is still writing it."""

    def make_video(self, root, rel, name):
        folder = os.path.join(root, rel)
        os.makedirs(folder)
        for fname in (f"{name}.mp4", f"trans_{name}.txt", f"words_{name}.txt",
                      f"{name}.jpg", f"description_{name}.txt"):
            open(os.path.join(folder, fname), "w").close()
        with open(os.path.join(folder, "videoinfo.txt"), "w") as f:
            f.write(f"Title:   {name} title\nStatus:  OK\n")
        return folder

    def write_stream(self, root, records, tail=""):
        with open(os.path.join(root, api.STREAM), "w", encoding="utf-8") as f:
            f.write("".join(json.dumps(r) + "\n" for r in records) + tail)

    def test_a_half_written_last_line_is_not_handed_over(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_video(tmp, "Ch/A", "A")
            self.write_stream(tmp, [{"url": "a", "status": "ok", "folder": "Ch/A"}],
                              tail='{"url": "b", "stat')
            self.assertEqual([v["url"] for v in api.read_videos(tmp)], ["a"])

    def test_paths_are_absolute_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make_video(tmp, "Ch/A", "A")
            self.write_stream(tmp, [{"url": "a", "status": "ok", "title": "A",
                                     "folder": "Ch/A"}])
            files = api.read_videos(tmp)[0]["files"]
            self.assertEqual(files["video"], os.path.join(folder, "A.mp4"))
            self.assertEqual(files["transcript"], os.path.join(folder, "trans_A.txt"))
            self.assertEqual(files["words"], os.path.join(folder, "words_A.txt"))
            self.assertEqual(files["info"], os.path.join(folder, "videoinfo.txt"))

    def test_a_skipped_video_gets_its_title_from_videoinfo(self):
        # Already on disk from an earlier run: still a video to analyse.
        with tempfile.TemporaryDirectory() as tmp:
            self.make_video(tmp, "Ch/A", "A")
            self.write_stream(tmp, [{"url": "a", "status": "skipped", "folder": "Ch/A"}])
            video = api.read_videos(tmp)[0]
            self.assertEqual(video["title"], "A title")
            self.assertTrue(video["files"]["video"])

    def test_channel_records_are_not_videos_and_failures_have_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_stream(tmp, [
                {"url": "c", "status": "channel", "videos": 2},
                {"url": "d", "status": "channel_failed", "error": "404"},
                {"url": "e", "status": "failed", "error": "boom", "folder": "Ch/E"}])
            videos = api.read_videos(tmp)
            self.assertEqual([v["url"] for v in videos], ["e"])
            self.assertEqual(videos[0]["files"], {})
            self.assertEqual(videos[0]["error"], "boom")

    def test_a_words_not_found_marker_is_not_a_words_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "V")
            os.makedirs(folder)
            open(os.path.join(folder, "words_not_found_V.txt"), "w").close()
            self.assertIsNone(api.folder_files(folder)["words"])

    def test_a_line_torn_inside_a_non_ascii_character_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.make_video(tmp, "Ch/A", "A")
            title = "\u0627\u0631\u062f\u0648"                 # Urdu: 2 bytes a character
            whole = json.dumps({"url": "a", "status": "ok", "title": title,
                                "folder": "Ch/A"}, ensure_ascii=False) + "\n"
            torn = json.dumps({"url": "b", "status": "ok", "title": title},
                              ensure_ascii=False).encode()
            cut = torn[:torn.index(title[0].encode()) + 1]        # half a character
            with open(os.path.join(tmp, api.STREAM), "wb") as f:
                f.write(whole.encode() + cut)
            self.assertEqual([v["url"] for v in api.read_videos(tmp)], ["a"])

    def test_no_stream_yet_means_no_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(api.read_videos(tmp), [])


class TestDefaultFolder(unittest.TestCase):
    def test_a_tilde_in_the_environment_is_expanded(self):
        saved = os.environ.get("YTDL_OUTDIR")
        os.environ["YTDL_OUTDIR"] = "~/yt-test-folder"
        try:
            self.assertEqual(api._default_out(),
                             os.path.join(os.path.expanduser("~"), "yt-test-folder"))
        finally:
            if saved is None:
                os.environ.pop("YTDL_OUTDIR", None)
            else:
                os.environ["YTDL_OUTDIR"] = saved


class TestStartAndStop(unittest.TestCase):
    def setUp(self):
        self.saved_jobs = dict(api.JOBS)
        api.JOBS.clear()

    def tearDown(self):
        api.JOBS.clear()
        api.JOBS.update(self.saved_jobs)

    def leftover_links_files(self, before):
        return [n for n in set(os.listdir(tempfile.gettempdir())) - before
                if n.startswith("ytdl_links_")]

    def test_a_failed_write_leaves_no_links_file_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "skip.txt"))      # a folder where the file goes
            before = set(os.listdir(tempfile.gettempdir()))
            with self.assertRaises(OSError):
                api.write_inputs(tmp, "x", skip="bbbbbbbbbbb")
            self.assertEqual(self.leftover_links_files(before), [])

    def test_a_job_that_cannot_start_leaves_nothing_behind(self):
        class Broken:
            def __init__(self, *a, **kw):
                pass
            def start(self):
                raise RuntimeError("can't start new thread")

        with tempfile.TemporaryDirectory() as tmp:
            before = set(os.listdir(tempfile.gettempdir()))
            saved, api.threading.Thread = api.threading.Thread, Broken
            try:
                with self.assertRaises(RuntimeError):
                    api.start_job(api.normalize_request({"links": "x", "outdir": tmp}))
            finally:
                api.threading.Thread = saved
            self.assertEqual(api.JOBS, {})                 # nothing blocks the folder
            self.assertEqual(self.leftover_links_files(before), [])

    def test_a_stop_after_the_run_ended_does_not_relabel_it(self):
        class Exited:
            def poll(self):
                return 0
        job = {"state": "running", "proc": Exited(), "stop_requested": False}
        self.assertFalse(api.stop_job(job))
        self.assertFalse(job["stop_requested"])

    def test_a_second_stop_does_not_signal_again(self):
        class Live:
            def poll(self):
                return None
        job = {"state": "running", "proc": Live(), "stop_requested": False}
        signalled = []
        saved, api._interrupt = api._interrupt, signalled.append
        try:
            self.assertTrue(api.stop_job(job))
            self.assertTrue(api.stop_job(job))
        finally:
            api._interrupt = saved
        self.assertEqual(len(signalled), 1)

    def test_a_links_file_whose_write_fails_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            before = set(os.listdir(tempfile.gettempdir()))
            real = api.os.fdopen

            def full_disk(*args, **kwargs):
                os.close(args[0])
                raise OSError(28, "No space left on device")

            api.os.fdopen = full_disk
            try:
                with self.assertRaises(OSError):
                    api.write_inputs(tmp, "x")
            finally:
                api.os.fdopen = real
            self.assertEqual(self.leftover_links_files(before), [])

    def test_the_server_bounds_how_long_shutdown_waits_for_requests(self):
        # Otherwise a slow request delays stopping the jobs indefinitely.
        import signal as _signal, sys as _sys, types
        seen = {}
        fake = types.ModuleType("uvicorn")
        fake.run = lambda app, **kw: seen.update(kw)
        saved = (_sys.modules.get("uvicorn"), _signal.getsignal(_signal.SIGTERM))
        _sys.modules["uvicorn"] = fake
        try:
            api.main(["--no-key", "--port", "1"])
        finally:
            if saved[0] is not None:
                _sys.modules["uvicorn"] = saved[0]
            else:
                _sys.modules.pop("uvicorn", None)
            _signal.signal(_signal.SIGTERM, saved[1])
        self.assertEqual(seen.get("timeout_graceful_shutdown"), 5)

    def test_a_finished_job_cannot_be_stopped(self):
        job = {"state": "finished", "proc": None, "stop_requested": False}
        self.assertFalse(api.stop_job(job))
        self.assertFalse(job["stop_requested"])

    def test_a_queued_job_is_marked_to_stop_as_it_starts(self):
        job = {"state": "queued", "proc": None, "stop_requested": False}
        self.assertTrue(api.stop_job(job))
        self.assertTrue(job["stop_requested"])


class TestFailures(unittest.TestCase):
    def test_only_failures_are_reported(self):
        records = [{"url": "1", "status": "ok"}, {"url": "2", "status": "skipped"},
                   {"url": "3", "status": "failed", "error": "x"},
                   {"url": "4", "status": "unavailable"},
                   {"url": "5", "status": "channel_failed"},
                   {"url": "6", "status": "channel", "videos": 3}]
        self.assertEqual([f["url"] for f in api.failures_from(records)], ["3", "4", "5"])

    def test_a_failure_the_final_retry_fixed_is_not_a_failure(self):
        records = [{"url": "a", "status": "failed", "error": "stream died"},
                   {"url": "b", "status": "ok"},
                   {"url": "a", "status": "ok"}]
        self.assertEqual(api.failures_from(records), [])

    def test_a_link_that_failed_twice_is_reported_once_with_its_last_error(self):
        records = [{"url": "a", "status": "failed", "error": "first"},
                   {"url": "a", "status": "failed", "error": "second"}]
        self.assertEqual([(f["url"], f["error"]) for f in api.failures_from(records)],
                         [("a", "second")])

    def test_nothing_recorded(self):
        self.assertEqual(api.failures_from([]), [])


class TestJobRecords(unittest.TestCase):
    def test_a_poll_that_straddles_the_end_of_a_job_gets_that_jobs_list(self):
        # The job ends while its stream is being read, and a new job in the same
        # folder has already replaced the stream: the snapshot is the right answer.
        job = {"state": "running", "records": None, "outdir": "/nowhere"}
        mine = [{"url": "mine", "status": "ok"}]

        def read_while_it_finishes(outdir):
            job["state"], job["records"] = "finished", mine
            return [{"url": "the next job's", "status": "ok"}]

        saved, api.read_stream = api.read_stream, read_while_it_finishes
        try:
            self.assertEqual(api.job_records(job), ("finished", mine))
        finally:
            api.read_stream = saved

    def test_a_finished_job_never_rereads_the_folder(self):
        job = {"state": "finished", "records": [{"url": "x", "status": "ok"}],
               "outdir": "/nowhere"}
        saved, api.read_stream = api.read_stream, lambda o: self.fail("reread")
        try:
            self.assertEqual(api.job_records(job)[1], [{"url": "x", "status": "ok"}])
        finally:
            api.read_stream = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)
