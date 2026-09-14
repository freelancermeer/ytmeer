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

    def test_a_folder_outside_the_download_folder_is_refused(self):
        # A job writes into its folder and the file endpoints read from it, so a
        # caller-chosen /etc would hand out the machine's files.
        for bad in ("~/somewhere", "/etc", "../elsewhere", "a/../../b"):
            with self.assertRaises(ValueError, msg=bad):
                api.normalize_request({"links": "x", "outdir": bad})

    def test_numbers_are_bounded(self):
        # Gradio's JSON encoder cannot write integers past 64 bits: a limit of
        # 10**20 used to make GET /api/jobs a 500 for every caller.
        for field in ("views", "limit", "min_height", "max_height"):
            with self.assertRaises(ValueError, msg=field):
                api.normalize_request({"links": "x", field: 10**20})
        opts = api.normalize_request({"links": "x", "views": 10**12, "limit": 10**6})
        self.assertEqual((opts["views"], opts["limit"]), (10**12, 10**6))

    def test_comment_lines_are_dropped_and_all_comments_is_no_links(self):
        opts = api.normalize_request({"links": "# channels\nhttps://youtu.be/aaaaaaaaaaa\n  # later"})
        self.assertEqual(opts["links"], "https://youtu.be/aaaaaaaaaaa")
        with self.assertRaises(ValueError):
            api.normalize_request({"links": "# only a comment\n#another"})

    def test_sub_lang_must_look_like_a_language_code(self):
        for good in ("en", "pt-BR", "zh-Hans", "en_US"):
            self.assertEqual(api.normalize_request({"links": "x", "sub_lang": good})["sub_lang"], good)
        for bad in ("-x", "--no-subs", "en us", "en;rm", "a" * 40):
            with self.assertRaises(ValueError, msg=bad):
                api.normalize_request({"links": "x", "sub_lang": bad})

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
            saved, api.DEFAULT_OUT = api.DEFAULT_OUT, tmp
            try:
                one = api.normalize_request({"links": "x", "outdir": os.path.join(tmp, "a")})
                two = api.normalize_request({"links": "x", "outdir": "a/../a"})
            finally:
                api.DEFAULT_OUT = saved
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

    def test_a_skipped_video_gets_its_quality_from_videoinfo(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = self.make_video(tmp, "Ch/A", "A")
            with open(os.path.join(folder, "videoinfo.txt"), "a") as f:
                f.write("Quality: 1080p\n")
            self.write_stream(tmp, [{"url": "a", "status": "skipped", "folder": "Ch/A"}])
            self.assertEqual(api.read_videos(tmp)[0]["quality"], "1080p")

    def test_a_failed_video_does_not_borrow_unknown_title(self):
        # Its videoinfo.txt says "Unknown Title"; the job's failures say null. The
        # two must agree.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "video_x"))
            with open(os.path.join(tmp, "video_x", "videoinfo.txt"), "w") as f:
                f.write("Title:   Unknown Title\nStatus:  ERROR\n")
            self.write_stream(tmp, [{"url": "x", "status": "unavailable", "folder": "video_x"}])
            self.assertIsNone(api.read_videos(tmp)[0]["title"])

    def test_no_stream_yet_means_no_videos(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(api.read_videos(tmp), [])


class TestJobFile(unittest.TestCase):
    """What the download endpoint may hand out - and above all what it must not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, "out")
        self.put("Ch/Vid/videoinfo.txt", "Ch/Vid/Vid.mp4", "Ch/Vid/trans_Vid.txt",
                 "Ch/Vid/Vid.f137.mp4.part", "Ch/Vid/Vid.f137.mp4", "Ch/Vid/Vid.temp.mp4",
                 "Ch/Vid/INCOMPLETE_old.mp4", "Ch/Vid/incomplete_other.mp4",
                 "Ch/Vid/Vid.mp4.aria2", "Ch/Vid/cookies.txt",
                 "cookies.txt", "links.txt", "download_log.json", ".api_key")
        with open(os.path.join(self.tmp.name, "secret.txt"), "w") as f:
            f.write("outside")

    def put(self, *names):
        for name in names:
            path = os.path.join(self.root, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("x")

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_finished_file_is_served(self):
        self.assertTrue(api.job_file(self.root, "Ch/Vid/Vid.mp4").endswith("Vid.mp4"))

    def test_nothing_outside_the_folder(self):
        for bad in ("../secret.txt", "Ch/../../secret.txt", os.path.join(self.tmp.name, "secret.txt"),
                    "/etc/passwd", ".", "Ch/.."):
            with self.assertRaises(PermissionError, msg=bad):
                api.job_file(self.root, bad)

    def test_a_symlink_pointing_out_is_refused(self):
        os.symlink(os.path.join(self.tmp.name, "secret.txt"), os.path.join(self.root, "Ch", "Vid", "link.txt"))
        with self.assertRaises(PermissionError):
            api.job_file(self.root, "Ch/Vid/link.txt")

    def test_only_files_inside_a_video_folder(self):
        for bad in ("cookies.txt", "COOKIES.TXT", "links.txt", "download_log.json", ".api_key"):
            with self.assertRaises(PermissionError, msg=bad):
                api.job_file(self.root, bad)

    def test_private_and_unfinished_files_are_refused(self):
        for bad in ("Ch/Vid/cookies.txt", "Ch/Vid/Vid.f137.mp4.part", "Ch/Vid/Vid.f137.mp4",
                    "Ch/Vid/Vid.temp.mp4", "Ch/Vid/INCOMPLETE_old.mp4",
                    "Ch/Vid/incomplete_other.mp4", "Ch/Vid/Vid.mp4.aria2"):
            with self.assertRaises(PermissionError, msg=bad):
                api.job_file(self.root, bad)

    def test_awkward_titles_are_still_served(self):
        # Folder and file take the video's title, so a title may start with a dot
        # or end in something that looks like a temporary suffix.
        self.put(".NET tutorial/videoinfo.txt", ".NET tutorial/.NET tutorial.mp4",
                 "My.temp/videoinfo.txt", "My.temp/My.temp.mp4", "My.temp/My.temp.temp.mp4")
        self.assertTrue(api.job_file(self.root, ".NET tutorial/.NET tutorial.mp4"))
        self.assertTrue(api.job_file(self.root, "My.temp/My.temp.mp4"))
        with self.assertRaises(PermissionError):
            api.job_file(self.root, "My.temp/My.temp.temp.mp4")

    def test_a_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            api.job_file(self.root, "Ch/Vid/nope.mp4")

    def test_the_listing_offers_only_what_can_be_downloaded(self):
        paths = [f["path"] for f in api.list_files(self.root, "job_x")]
        self.assertEqual(paths, ["Ch/Vid/Vid.mp4", "Ch/Vid/trans_Vid.txt", "Ch/Vid/videoinfo.txt"])

    def test_download_urls_are_escaped(self):
        folder = os.path.join(self.root, "Ch", "A video #1? 100%")
        self.put("Ch/A video #1? 100%/videoinfo.txt")
        path = os.path.join(folder, "\u0627\u0631\u062f\u0648.mp4")
        open(path, "w").close()
        url = api.download_url("job_x", self.root, path)
        self.assertEqual(url, "/jobs/job_x/files/Ch/A%20video%20%231%3F%20100%25/"
                              "%D8%A7%D8%B1%D8%AF%D9%88.mp4")
        import urllib.parse
        rel = urllib.parse.unquote(url.split("/files/", 1)[1])
        self.assertEqual(api.job_file(self.root, rel), os.path.realpath(path))

    def test_downloads_are_offered_only_for_files_that_would_be_served(self):
        outside = os.path.join(self.tmp.name, "elsewhere", "Vid")
        os.makedirs(outside)
        for name in ("videoinfo.txt", "Vid.mp4"):
            open(os.path.join(outside, name), "w").close()
        os.symlink(os.path.dirname(outside), os.path.join(self.root, "Linked"))
        video = api.describe({"url": "u", "status": "ok", "folder": "Linked/Vid"}, self.root, "job_x")
        self.assertTrue(video["files"]["video"])           # the path exists...
        self.assertEqual(video["downloads"], {})           # ...but it is not served
        mine = api.describe({"url": "u", "status": "ok", "folder": "Ch/Vid"}, self.root, "job_x")
        self.assertEqual(sorted(mine["downloads"]), ["info", "transcript", "video"])


class TestPreview(unittest.TestCase):
    def test_a_second_preview_is_turned_away_rather_than_queued(self):
        # A request a proxy abandons keeps its listing running; waiting behind it
        # would make every later preview time out as well.
        d = api.downloader
        saved = (d.is_channel_url, api.PREVIEW_WAIT)
        d.is_channel_url, api.PREVIEW_WAIT = (lambda url: True), 0.2
        api.PREVIEW_LOCK.acquire()
        try:
            with self.assertRaises(api.PreviewBusy):
                api.preview_channel("https://www.youtube.com/@X", limit=1)
        finally:
            api.PREVIEW_LOCK.release()
            d.is_channel_url, api.PREVIEW_WAIT = saved

    def test_the_lock_is_released_even_when_the_listing_fails(self):
        d = api.downloader
        saved = (d.is_channel_url, d.list_channel_videos)
        d.is_channel_url = lambda url: True
        d.list_channel_videos = lambda url: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                api.preview_channel("https://www.youtube.com/@X", limit=1)
            self.assertTrue(api.PREVIEW_LOCK.acquire(timeout=0.1))
            api.PREVIEW_LOCK.release()
        finally:
            d.is_channel_url, d.list_channel_videos = saved


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
            saved_out, api.DEFAULT_OUT = api.DEFAULT_OUT, tmp
            try:
                with self.assertRaises(RuntimeError):
                    api.start_job(api.normalize_request({"links": "x"}))
            finally:
                api.threading.Thread = saved
                api.DEFAULT_OUT = saved_out
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
