#!/usr/bin/env python3
"""Tests for api.py's logic - no network, no downloads, no fastapi needed.

    python3 test_api.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

import api


class TestNormalizeRequest(unittest.TestCase):
    def test_links_are_required(self):
        for bad in ({}, {"links": ""}, {"links": "  \n"}, {"links": []}):
            with self.assertRaises(ValueError):
                api.normalize_request(bad)

    def test_a_link_that_yt_dlp_would_read_as_an_option_is_refused(self):
        for bad in ("--exec=touch /tmp/x", "-f best", "https://youtu.be/aaaaaaaaaaa\n-v"):
            with self.assertRaises(ValueError, msg=bad):
                api.normalize_request({"links": bad})
        # A bare video id may start with "-"; the downloader puts every URL after "--".
        self.assertEqual(api.normalize_request({"links": "-wNyEUrxzFU"})["links"], "-wNyEUrxzFU")

    def test_a_run_that_leaves_no_summary_is_an_error_unless_it_was_stopped(self):
        import collections
        with tempfile.TemporaryDirectory() as out:
            for stopped, state in ((False, "error"), (True, "stopped")):
                job = {"outdir": out, "links_file": None, "stop_requested": stopped, "error": None,
                       "log": collections.deque(["(could not write download_log.json: denied)"])}
                api._finish(job)
                self.assertEqual(job["state"], state)
                if stopped:
                    self.assertIsNone(job["error"])
                else:
                    self.assertIn("could not write download_log.json", job["error"])

    def test_a_stop_python_swallowed_while_starting_leaves_the_run_finished(self):
        import collections
        with tempfile.TemporaryDirectory() as out:
            for stopped_early, state in ((False, "finished"), (True, "stopped")):
                with open(os.path.join(out, "download_log.json"), "w") as f:
                    json.dump({"run": {"stopped_early": stopped_early}, "videos": []}, f)
                job = {"outdir": out, "links_file": None, "stop_requested": True, "error": None,
                       "log": collections.deque()}
                api._finish(job)
                self.assertEqual(job["state"], state)

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

    def search_argv(self, **over):
        """A search job's command line. It carries no links: the search is the
        worklist, and sending both is refused."""
        return api.build_argv("/out", api.normalize_request(dict(**over)))

    def test_the_command_line(self):
        self.assertEqual(self.argv(views=1000, limit=3)[3:],
                         ["/out", "--channel", "--views", "1000", "--limit", "3",
                          "--min-height", "720", "--max-height", "1080", "--sub-lang", "en",
                          "--min-duration", "180", "--yes"])

    def test_asking_for_shorts_replaces_the_floor(self):
        argv = self.argv(shorts=True)
        self.assertIn("--shorts", argv)
        self.assertNotIn("--min-duration", argv)

    def test_a_custom_floor_reaches_the_command_line(self):
        argv = self.argv(min_duration=600)
        self.assertEqual(argv[argv.index("--min-duration") + 1], "600")

    def test_match_reaches_the_command_line(self):
        argv = self.search_argv(search="cats", match="exact")
        self.assertEqual(argv[argv.index("--match") + 1], "exact")
        self.assertEqual(self.search_argv(search="cats")[
            self.search_argv(search="cats").index("--match") + 1], "related")

    def test_a_bad_match_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            api.normalize_request({"search": "cats", "match": "sideways"})
        self.assertIn("match must be one of", str(caught.exception))

    def test_no_match_flag_without_a_query(self):
        self.assertNotIn("--match", self.argv())

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

    def test_transcript_only_reaches_the_command_line(self):
        argv = self.argv(transcript=True)
        self.assertIn("--transcript", argv)
        self.assertEqual(argv[-1], "--yes", "--yes must stay last")

    def test_comment_flags_travel_together(self):
        argv = self.argv(extract_comments=True, comment_limit=30,
                         comment_sort="new", comment_replies=True)
        self.assertIn("--extract-comments", argv)
        self.assertEqual(argv[argv.index("--comment-limit") + 1], "30")
        self.assertEqual(argv[argv.index("--comment-sort") + 1], "new")
        self.assertIn("--comment-replies", argv)

    def test_replies_stay_off_unless_asked(self):
        argv = self.argv(extract_comments=True)
        self.assertIn("--extract-comments", argv)
        self.assertNotIn("--comment-replies", argv)
        self.assertEqual(argv[argv.index("--comment-limit") + 1], "100",
                         "top 100 is the default")

    def test_no_comment_flags_when_comments_are_off(self):
        argv = self.argv()
        for flag in ("--extract-comments", "--comment-limit", "--comment-sort",
                     "--comment-replies"):
            self.assertNotIn(flag, argv)

    def test_search_reaches_the_command_line(self):
        argv = self.search_argv(search="cats", search_count=10, sort="popularity",
                                this_month=True)
        self.assertIn("--search=cats", argv)
        self.assertEqual(argv[argv.index("--search-count") + 1], "10")
        self.assertEqual(argv[argv.index("--sort") + 1], "popularity")
        self.assertIn("--this-month", argv)
        self.assertEqual(argv[-1], "--yes")

    def test_in_channel_reaches_the_command_line(self):
        argv = self.search_argv(search="cats",
                                in_channel="https://www.youtube.com/@Handle")
        self.assertEqual(argv[argv.index("--in-channel") + 1],
                         "https://www.youtube.com/@Handle")

    def test_no_search_flags_without_a_query(self):
        argv = self.argv()
        for flag in ("--search", "--search-count", "--sort", "--this-month",
                     "--in-channel"):
            self.assertNotIn(flag, argv)

    def test_search_count_zero_leaves_the_depth_to_the_route(self):
        # Scanning 150 results costs about 10s flat but 3m37s once a date window
        # forces a full extraction per video, so one number cannot serve both.
        opts = api.normalize_request({"search": "cats"})
        self.assertEqual(opts["search_count"], 0)
        self.assertNotIn("--search-count", api.build_argv("/out", opts))
        opts = api.normalize_request({"search": "cats", "search_count": 60})
        argv = api.build_argv("/out", opts)
        self.assertEqual(argv[argv.index("--search-count") + 1], "60")

    def test_a_search_job_needs_no_links(self):
        # The search produces the worklist, so there is nothing to supply.
        opts = api.normalize_request({"search": "cats"})
        self.assertEqual(opts["links"], "")
        self.assertIn("--search=cats", api.build_argv("/out", opts))

    def test_a_query_that_starts_with_a_dash_is_still_a_query(self):
        # --search -ad reads would reach argparse as an option and kill the run.
        argv = self.search_argv(search="-ad reads")
        self.assertIn("--search=-ad reads", argv)
        self.assertNotIn("-ad reads", argv)

    def test_links_and_search_together_are_refused(self):
        # A search replaces the worklist; accepting both would download
        # something other than what was asked for.
        with self.assertRaises(ValueError) as caught:
            api.normalize_request({"links": "x", "search": "cats"})
        self.assertIn("two different worklists", str(caught.exception))

    def test_a_job_with_neither_links_nor_search_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            api.normalize_request({})
        self.assertIn("links is required", str(caught.exception))

    def test_contradictory_options_are_refused(self):
        for payload, expected in (
                ({"links": "x", "in_channel": "https://www.youtube.com/@H"}, "needs search"),
                ({"search": "c", "in_channel": "nonsense"}, "channel link"),
                ({"search": "c", "days": 7, "this_month": True}, "pick one"),
                ({"links": "x", "this_month": True}, "needs search"),
                ({"search": "c", "match": "sideways"}, "match must be one of"),
                ({"links": "x", "transcript": True, "subs": False}, "cannot be combined"),
                ({"links": "x", "comment_sort": "best"}, "comment_sort must be"),
                ({"links": "x", "sort": "sideways"}, "sort must be one of"),
        ):
            with self.assertRaises(ValueError, msg=f"{payload} should be refused") as caught:
                api.normalize_request(payload)
            self.assertIn(expected, str(caught.exception))

    def test_reverse_popularity_is_an_accepted_sort(self):
        argv = self.search_argv(search="cats", sort="least-viewed")
        self.assertEqual(argv[argv.index("--sort") + 1], "least-viewed")

    def test_a_links_file_is_passed_on(self):
        opts = api.normalize_request({"links": "x"})
        argv = api.build_argv("/out", opts, "/tmp/job.txt")
        self.assertEqual(argv[3:6], ["/out", "--links", "/tmp/job.txt"])

    def test_a_shared_cookie_file_reaches_jobs_in_subfolders(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved, api.DEFAULT_OUT = api.DEFAULT_OUT, tmp
            try:
                opts = api.normalize_request({"links": "x", "outdir": "batch1"})
                os.makedirs(opts["outdir"])
                self.assertNotIn("--cookies", api.build_argv(opts["outdir"], opts))
                open(os.path.join(tmp, "cookies.txt"), "w").close()
                argv = api.build_argv(opts["outdir"], opts)
                self.assertEqual(argv[argv.index("--cookies") + 1], os.path.join(tmp, "cookies.txt"))
                open(os.path.join(opts["outdir"], "cookies.txt"), "w").close()   # its own wins
                self.assertNotIn("--cookies", api.build_argv(opts["outdir"], opts))
            finally:
                api.DEFAULT_OUT = saved

    def test_output_is_unbuffered(self):
        # Buffered, the log would arrive in blocks and a job would look stuck.
        self.assertEqual(self.argv()[1], "-u")

    def test_api_jobs_skip_the_interactive_selector(self):
        self.assertEqual(self.argv()[-1], "--yes")


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
                    "Ch/Vid/Vid.temp.mp4", "Ch/Vid/INCOMPLETE_old.mp4", "Ch/Vid/Vid.mp4.aria2"):
            with self.assertRaises(PermissionError, msg=bad):
                api.job_file(self.root, bad)

    def test_only_the_downloaders_own_marker_means_incomplete(self):
        # sanitize("Incomplete: ideas") is "Incomplete_ ideas" - a title, not a leftover.
        self.put("Ch/Incomplete_ ideas/videoinfo.txt", "Ch/Incomplete_ ideas/Incomplete_ ideas.mp4")
        self.assertTrue(api.job_file(self.root, "Ch/Incomplete_ ideas/Incomplete_ ideas.mp4"))
        self.assertTrue(api.job_file(self.root, "Ch/Vid/incomplete_other.mp4"))

    def test_a_folder_spelled_another_way_is_not_served_either(self):
        # Stream parts are recognised by the folder's name; another spelling of the
        # folder must not turn them into ordinary files.
        self.put("Ch/Vid/Vid.f137.mp4", "Ch/Vid/Vid.temp.mp4")
        for other in ("Ch/VID/Vid.f137.mp4", "ch/vid/Vid.temp.mp4"):
            # PermissionError on a case-insensitive disk, FileNotFoundError on another.
            with self.assertRaises((PermissionError, FileNotFoundError), msg=other):
                api.job_file(self.root, other)
        self.assertTrue(api._unfinished("Vid.f137.mp4", "VID"))
        self.assertTrue(api._unfinished("Cafe\u0301.temp.mp4", "Caf\u00e9"))

    def test_only_the_name_as_it_is_on_disk_is_served(self):
        # A case-insensitive disk (macOS, Windows) would open these as the real files.
        self.put("Ch/Vid/download_log.txt")
        for bad in ("Ch/Vid/DOWNLOAD_LOG.TXT", "Ch/Vid/Skip.TXT", "Ch/Vid/Links.txt"):
            with self.assertRaises(PermissionError, msg=bad):
                api.job_file(self.root, bad)
        for other in ("Ch/Vid/VID.MP4", "Ch/Vid/incomplete_OLD.mp4"):
            with self.assertRaises(FileNotFoundError, msg=other):
                api.job_file(self.root, other)

    def test_awkward_titles_are_still_served(self):
        # Folder and file take the video's title, so a title may start with a dot
        # or end in something that looks like a temporary suffix.
        self.put(".NET tutorial/videoinfo.txt", ".NET tutorial/.NET tutorial.mp4",
                 "My.temp/videoinfo.txt", "My.temp/My.temp.mp4", "My.temp/My.temp.temp.mp4")
        self.assertTrue(api.job_file(self.root, ".NET tutorial/.NET tutorial.mp4"))
        self.assertTrue(api.job_file(self.root, "My.temp/My.temp.mp4"))
        with self.assertRaises(PermissionError):
            api.job_file(self.root, "My.temp/My.temp.temp.mp4")

    def test_a_jobs_own_run_files_are_never_served(self):
        # Even when a job's folder is itself a video folder (holds videoinfo.txt).
        self.put("Ch/Vid/download_log.json", "Ch/Vid/skip.txt", "Ch/Vid/links.txt", "videoinfo.txt")
        for bad in ("Ch/Vid/download_log.json", "Ch/Vid/skip.txt", "Ch/Vid/links.txt"):
            with self.assertRaises(PermissionError, msg=bad):
                api.job_file(self.root, bad)
        with self.assertRaises(PermissionError):
            api.job_file(self.root, "download_log.json")       # top level, videoinfo.txt or not
        video_root = os.path.join(self.root, "Ch", "Vid")      # a job pointed at a video folder
        with self.assertRaises(PermissionError):
            api.job_file(video_root, "Vid.mp4")

    def test_hidden_files_are_refused_but_a_dotted_title_is_not(self):
        self.put("Ch/Vid/.DS_Store", ".NET/videoinfo.txt", ".NET/.NET.mp4")
        with self.assertRaises(PermissionError):
            api.job_file(self.root, "Ch/Vid/.DS_Store")
        self.assertTrue(api.job_file(self.root, ".NET/.NET.mp4"))

    def test_an_older_clash_folder_still_recognises_its_streams(self):
        # "<title> [<id>]" folders made before files took the folder's full name.
        self.put("Ch/My Video [abcdefghijk]/videoinfo.txt", "Ch/My Video [abcdefghijk]/My Video.mp4",
                 "Ch/My Video [abcdefghijk]/My Video.f137.mp4", "Ch/My Video [abcdefghijk]/My Video.temp.mp4")
        self.assertTrue(api.job_file(self.root, "Ch/My Video [abcdefghijk]/My Video.mp4"))
        for bad in ("My Video.f137.mp4", "My Video.temp.mp4"):
            with self.assertRaises(PermissionError, msg=bad):
                api.job_file(self.root, "Ch/My Video [abcdefghijk]/" + bad)

    def test_a_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            api.job_file(self.root, "Ch/Vid/nope.mp4")

    def test_the_listing_offers_only_what_can_be_downloaded(self):
        paths = [f["path"] for f in api.list_files(self.root, "job_x")]
        self.assertEqual(paths, ["Ch/Vid/Vid.mp4", "Ch/Vid/incomplete_other.mp4",
                                 "Ch/Vid/trans_Vid.txt", "Ch/Vid/videoinfo.txt"])

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
    def setUp(self):
        api.reset_engine_state()          # a kept preview would answer the next test

    def test_a_preview_past_every_slot_is_turned_away_rather_than_queued(self):
        # A request a proxy abandons keeps its listing running; waiting behind it
        # would make every later preview time out as well. So once every slot
        # is taken, the next caller is told after PREVIEW_WAIT, not parked.
        d = api.downloader
        saved = (d.is_channel_url, api.PREVIEW_WAIT)
        d.is_channel_url, api.PREVIEW_WAIT = (lambda url: True), 0.2
        held = [api.ENGINE.acquire(0) for _ in range(api.ENGINE.size)]
        try:
            self.assertNotIn(None, held)
            began = time.monotonic()
            with self.assertRaises(api.PreviewBusy) as caught:
                api.preview_channel("https://www.youtube.com/@X", limit=1)
            self.assertLess(time.monotonic() - began, 2)
            self.assertIn("still running", str(caught.exception))
        finally:
            for index in held:
                if index is not None:
                    api.ENGINE.release(index)
            d.is_channel_url, api.PREVIEW_WAIT = saved

    def test_a_preview_scans_with_a_deadline(self):
        d = api.downloader
        seen = {}
        saved = (d.is_channel_url, d.list_channel_videos, d.channel_videos_url)
        d.is_channel_url, d.channel_videos_url = (lambda url: True), (lambda url: url)
        d.list_channel_videos = lambda url, deadline=None: (seen.update(deadline=deadline), ([], "Ch", None))[1]
        try:
            began = __import__("time").monotonic()
            api.preview_channel("https://www.youtube.com/@X", limit=1)
        finally:
            d.is_channel_url, d.list_channel_videos, d.channel_videos_url = saved
        self.assertAlmostEqual(seen["deadline"] - began, api.PREVIEW_SECONDS, delta=2)

    def test_the_lock_is_released_even_when_the_listing_fails(self):
        d = api.downloader
        saved = (d.is_channel_url, d.list_channel_videos)
        d.is_channel_url = lambda url: True
        d.list_channel_videos = lambda url, deadline=None: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with self.assertRaises(RuntimeError):
                api.preview_channel("https://www.youtube.com/@X", limit=1)
            self.assertEqual(api.ENGINE.in_use(), 0, "the slot must be given back")
        finally:
            d.is_channel_url, d.list_channel_videos = saved


BOT_CHECK = ("ERROR: [youtube] aaaaaaaaaaa: Sign in to confirm you’re not a bot. "
             "This helps protect our community.")


class EngineCase(unittest.TestCase):
    """A private engine pool per test, and the stubs to drive it offline."""

    def setUp(self):
        self.saved = (api.ENGINE, api.CACHE, api.PREVIEW_WAIT, api.BOT_PAUSE_FIRST)
        self.stubbed = []
        api.CACHE = api.AnswerCache(900, 64)
        api.reset_bot_pause()

    def tearDown(self):
        for module, name, value in reversed(self.stubbed):
            setattr(module, name, value)
        api.ENGINE, api.CACHE, api.PREVIEW_WAIT, api.BOT_PAUSE_FIRST = self.saved
        api.reset_bot_pause()

    def pool(self, size):
        api.ENGINE = api.EnginePool(size)
        for index in range(size):
            api.ENGINE.module(index)          # every copy made up front
        return api.ENGINE

    def stub(self, module, **functions):
        for name, fn in functions.items():
            self.stubbed.append((module, name, getattr(module, name)))
            setattr(module, name, fn)

    def stub_all(self, **functions):
        for module in api.ENGINE.modules():
            self.stub(module, **functions)

    @staticmethod
    def until(test, seconds=3.0):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if test():
                return True
            time.sleep(0.01)
        return False


class TestEngineSlots(EngineCase):
    """Lookups overlap now, each on a private copy of downloader's globals."""

    def test_no_more_than_the_slots_run_at_once(self):
        self.pool(3)
        api.PREVIEW_WAIT = 10
        gate, lock, live, peak = threading.Event(), threading.Lock(), [0], [0]

        def details(url):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            gate.wait(5)
            with lock:
                live[0] -= 1
            return {"id": url[-11:]}, None

        self.stub_all(video_details=details)
        threads = [threading.Thread(target=api.video_info, args=(c * 11,))
                   for c in "abcdefg"]
        for t in threads:
            t.start()
        try:
            self.assertTrue(self.until(lambda: live[0] == 3), "three should start")
            time.sleep(0.3)                   # time for a fourth to sneak in
            self.assertEqual(live[0], 3, "a fourth lookup ran past three slots")
            self.assertEqual(api.ENGINE.in_use(), 3)
        finally:
            gate.set()
            for t in threads:
                t.join(10)
        self.assertEqual(peak[0], 3)
        self.assertEqual(api.ENGINE.in_use(), 0)

    def test_overlapping_lookups_finish_together_not_in_turn(self):
        self.pool(4)
        self.stub_all(video_details=lambda url: (time.sleep(0.4), ({"id": url[-11:]}, None))[1])
        began = time.monotonic()
        threads = [threading.Thread(target=api.video_info, args=(c * 11,))
                   for c in "abcd"]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        # One at a time this was 1.6s; four slots make it one lookup's time.
        self.assertLess(time.monotonic() - began, 1.2)

    def test_a_caller_past_every_slot_is_told_after_the_wait(self):
        pool = self.pool(2)
        api.PREVIEW_WAIT = 0.2
        held = [pool.acquire(0), pool.acquire(0)]
        try:
            with self.assertRaises(api.PreviewBusy) as caught:
                api.video_info("a" * 11)
            self.assertIn("still running", str(caught.exception))
        finally:
            for index in held:
                pool.release(index)

    def test_a_waiting_caller_gets_the_first_slot_to_come_free(self):
        pool = self.pool(1)
        api.PREVIEW_WAIT = 5
        self.stub_all(video_details=lambda url: ({"id": url[-11:]}, None))
        index = pool.acquire(0)
        got = {}
        waiter = threading.Thread(
            target=lambda: got.update(answer=api.video_info("a" * 11)))
        waiter.start()
        time.sleep(0.3)
        self.assertNotIn("answer", got, "it ran without a slot")
        pool.release(index)
        waiter.join(5)
        self.assertEqual(got["answer"]["id"], "a" * 11)

    def test_overlapping_requests_never_see_each_others_settings(self):
        self.pool(2)
        meet = threading.Barrier(2, timeout=5)
        seen = {}

        def items(query, module):
            meet.wait()                       # both requests are in flight here
            seen[query] = (module.__name__, module.MIN_DURATION, module.SEARCH_QUERY,
                           module.LIMIT)
            module.SEARCH_FOUND[0] = len(query)
            return [], None

        for module in api.ENGINE.modules():
            self.stub(module, search_items=lambda q, m=module: items(q, m))
        answers = {}
        threads = [threading.Thread(target=lambda q=q, floor=floor, limit=limit:
                                    answers.update({q: api.search_youtube(
                                        q, min_duration=floor, limit=limit,
                                        dates=False)}))
                   for q, floor, limit in (("one", 0, 5), ("second", 600, 9))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertNotEqual(seen["one"][0], seen["second"][0], "one module for both")
        self.assertEqual(seen["one"][1:], (0, "one", 5))
        self.assertEqual(seen["second"][1:], (600, "second", 9))
        # Each answer reports its own counter, not the other request's.
        self.assertEqual(answers["one"]["scanned"], 3)
        self.assertEqual(answers["second"]["scanned"], 6)

    def test_a_lone_caller_gets_the_imported_module(self):
        # Slot 0 is api.downloader itself, handed out first, so a stub on it -
        # which every older test relies on - is what a lone lookup runs.
        pool = self.pool(3)
        self.assertIs(pool.module(0), api.downloader)
        self.assertEqual([pool.acquire(0), pool.acquire(0)], [0, 1])
        pool.release(0)
        self.assertEqual(pool.acquire(0), 0)

    def test_facts_about_the_process_are_shared_by_every_slot(self):
        pool = self.pool(3)
        for module in pool.modules()[1:]:
            self.assertIsNot(module, api.downloader)
            for name in api.EnginePool.SHARED:
                self.assertIs(getattr(module, name), getattr(api.downloader, name), name)
        # ...while a setting stays its own module's.
        first, second = pool.modules()[1:]
        first.MIN_DURATION = 7
        self.assertNotEqual(second.MIN_DURATION, 7)
        self.assertNotEqual(api.downloader.MIN_DURATION, 7)

    def _dating_engine(self, slots):
        """slots engine copies, all dating through a scripted yt-dlp that
        waits on a gate - so a test can count the runs in flight at once."""
        d = api.downloader
        # Swapped in BEFORE the copies are made: they take the shared pool from
        # the imported module at birth, and that hand-over is what is tested.
        self.stub(d, DATE_LANES=threading.BoundedSemaphore(2))
        pool = self.pool(slots)
        saved = d.po_token_first[0]
        d.po_token_first[0] = True            # one route, shared by every slot
        self.addCleanup(d.po_token_first.__setitem__, 0, saved)
        nowhere = os.path.join(tempfile.gettempdir(), "ytmeer-none", "cookies.txt")
        for module in pool.modules():
            self.stub(module, DATE_WORKERS=3, DATE_CHUNK_MIN=3, COOKIES_FILE=nowhere)
        gate, lock, live, peak, runs = (threading.Event(), threading.Lock(), [0], [0], [])

        def run(cmd, **kw):
            with lock:
                live[0] += 1
                peak[0] = max(peak[0], live[0])
                runs.append(cmd)
            gate.wait(5)
            with lock:
                live[0] -= 1
            ids = [url[-11:] for url in cmd[cmd.index("--") + 1:]]
            return subprocess.CompletedProcess(
                cmd, 0, "".join(f"{v}\t1700000000\n" for v in ids), "")

        self.stub(subprocess, run=run)
        return pool, gate, live, peak, runs

    def test_every_slot_borrows_its_extra_date_runs_from_one_pool(self):
        # A pool per copy would let three slots dating at once split three
        # ways each: nine extractions from one IP. With one pool it is each
        # slot's own run plus the pool's two.
        pool, gate, live, peak, _runs = self._dating_engine(3)
        for module in pool.modules():
            self.assertIs(module.DATE_LANES, api.downloader.DATE_LANES)
        answers = []

        def date(module, letter):
            items = [{"id": f"{letter}{n:010d}",
                      "url": module.watch_url(f"{letter}{n:010d}"), "epoch": None}
                     for n in range(9)]
            answers.append(module.resolve_dates(items))

        threads = [threading.Thread(target=date, args=(module, letter))
                   for module, letter in zip(pool.modules(), "abc")]
        for t in threads:
            t.start()
        try:
            self.assertTrue(self.until(lambda: live[0] == 5), "five runs should start")
            time.sleep(0.3)                   # time for a sixth to sneak in
            self.assertEqual(live[0], 5, "a slot split past the shared pool")
        finally:
            gate.set()
            for t in threads:
                t.join(10)
        self.assertEqual(peak[0], 5)
        self.assertEqual(answers, [(9, None)] * 3)

    def test_a_search_alone_dates_its_ten_in_three_runs(self):
        # The route itself, end to end: nothing in the per-request reset may
        # turn the split off.
        _pool, gate, _live, _peak, runs = self._dating_engine(1)
        gate.set()                            # overlap is the test above's business
        entries = [{"id": f"v{n:010d}", "title": str(n), "view_count": 10 + n,
                    "duration": 900} for n in range(30)]
        self.stub_all(search_flat=lambda query, count: (list(entries), None))
        answer = api.search_youtube("cats", limit=10)
        self.assertEqual((answer["found"], answer["dates_fetched"]), (10, 10))
        self.assertIsNone(answer["date_error"])
        self.assertTrue(all(row["published"] for row in answer["results"]))
        self.assertEqual(sorted(len(cmd[cmd.index("--") + 1:]) for cmd in runs), [3, 3, 4])

    def test_a_bad_setting_in_the_environment_is_the_default(self):
        os.environ["YTMEER_TEST_INT"] = "four"
        try:
            self.assertEqual(api._env_int("YTMEER_TEST_INT", 4, 1, 16), 4)
            os.environ["YTMEER_TEST_INT"] = "99"
            self.assertEqual(api._env_int("YTMEER_TEST_INT", 4, 1, 16), 16)
            os.environ["YTMEER_TEST_INT"] = "0"
            self.assertEqual(api._env_int("YTMEER_TEST_INT", 4, 1, 16), 1)
        finally:
            del os.environ["YTMEER_TEST_INT"]


class TestAnswerCache(EngineCase):
    def setUp(self):
        super().setUp()
        self.pool(2)
        self.calls = []

    def details(self, answer=None, error=None, gate=None):
        def fn(url):
            self.calls.append(url)
            if gate is not None:
                gate.wait(5)
            return (dict(answer or {"id": url[-11:], "views": 42}), None) if error is None \
                else (None, error)
        self.stub_all(video_details=fn)

    def test_a_repeat_is_answered_from_memory(self):
        self.details()
        first = api.video_info("a" * 11)
        # The same video spelled another way is the same question.
        again = api.video_info("https://youtu.be/aaaaaaaaaaa?t=42")
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("cached", first)
        self.assertTrue(again["cached"])
        self.assertGreaterEqual(again["cache_age_seconds"], 0)
        self.assertEqual(again["views"], 42)

    def test_a_cached_answer_is_a_copy(self):
        self.details()
        api.video_info("a" * 11)["views"] = -1
        hit = api.video_info("a" * 11)
        hit["views"] = -2
        self.assertEqual(api.video_info("a" * 11)["views"], 42)

    def test_an_old_answer_is_fetched_again(self):
        self.details()
        api.video_info("a" * 11)
        with api.CACHE._lock:                 # age every entry past the TTL
            for key, (stored, answer) in list(api.CACHE._store.items()):
                api.CACHE._store[key] = (stored - 10_000, answer)
        api.video_info("a" * 11)
        self.assertEqual(len(self.calls), 2)

    def test_a_failure_is_never_kept(self):
        self.details(error="ERROR: Video unavailable")
        for _ in range(2):
            with self.assertRaises(api.LookupFailed):
                api.video_info("a" * 11)
        self.assertEqual(len(self.calls), 2)

    def test_a_partial_answer_is_never_kept(self):
        calls = []
        self.stub_all(channel_videos=lambda url, limit=0: (
            calls.append(url), ({"videos": [], "listing_error": "cut short"}, None))[1])
        for _ in range(2):
            self.assertEqual(api.channel_video_rows("https://www.youtube.com/@X")
                             ["listing_error"], "cut short")
        self.assertEqual(len(calls), 2, "a partial answer must be asked again")

    def test_different_questions_are_different_answers(self):
        self.details()
        api.video_info("a" * 11)
        api.video_info("b" * 11)
        self.assertEqual(len(self.calls), 2)
        # The route is part of the question, and so is every argument.
        self.assertNotEqual(api.video_info.cache_key("a" * 11),
                            api.video_transcript.cache_key("a" * 11))
        self.assertNotEqual(api.video_comments.cache_key("a" * 11, 100),
                            api.video_comments.cache_key("a" * 11, 20))
        self.assertEqual(api.search_youtube.cache_key("q", skip="aaaaaaaaaaa bbbbbbbbbbb"),
                         api.search_youtube.cache_key("q", skip="https://youtu.be/bbbbbbbbbbb aaaaaaaaaaa"))

    def test_overlapping_identical_asks_share_one_fetch(self):
        gate = threading.Event()
        self.details(gate=gate)
        got = []
        threads = [threading.Thread(target=lambda: got.append(api.video_info("a" * 11)))
                   for _ in range(3)]
        for t in threads:
            t.start()
        self.assertTrue(self.until(lambda: len(self.calls) == 1))
        self.assertTrue(self.until(lambda: api.CACHE.shared == 2))
        # A fetch in flight is not an answer yet; a peek never waits for it.
        self.assertIsNone(api.video_info.peek("a" * 11))
        gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(len(self.calls), 1, "three asks, one fetch")
        self.assertEqual([a["views"] for a in got], [42, 42, 42])
        self.assertEqual(api.ENGINE.in_use(), 0)

    def test_a_shared_failure_reaches_every_caller(self):
        gate = threading.Event()
        self.details(error="ERROR: Video unavailable", gate=gate)
        errors = []

        def ask():
            try:
                api.video_info("a" * 11)
            except api.LookupFailed as e:
                errors.append(e)

        threads = [threading.Thread(target=ask) for _ in range(3)]
        for t in threads:
            t.start()
        self.assertTrue(self.until(lambda: api.CACHE.shared == 2))
        gate.set()
        for t in threads:
            t.join(10)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(errors), 3)
        self.assertEqual(len({id(e) for e in errors}), 3, "one exception per caller")

    def test_a_bad_field_is_shared_with_its_field(self):
        self.assertEqual(api._clone_error(api.BadField("sort", "no")).field, "sort")

    def test_the_cache_can_be_turned_off(self):
        api.CACHE = api.AnswerCache(0, 64)
        self.details()
        api.video_info("a" * 11)
        self.assertNotIn("cached", api.video_info("a" * 11))
        self.assertEqual(len(self.calls), 2)

    def test_the_oldest_answer_goes_first_when_full(self):
        api.CACHE = api.AnswerCache(900, 2)
        self.details()
        for c in "abc":
            api.video_info(c * 11)
        self.assertEqual(len(api.CACHE), 2)
        api.video_info("a" * 11)              # pushed out, so fetched again
        self.assertEqual(len(self.calls), 4)


class TestBotPause(EngineCase):
    def setUp(self):
        super().setUp()
        self.pool(2)
        api.BOT_PAUSE_FIRST = 60
        self.calls = []

    def answers(self, outcome):
        """video_details answering ok, or with the error text outcome[url]."""
        def fn(url):
            self.calls.append(url)
            error = outcome.get(url[-11:])
            return (None, error) if error else ({"id": url[-11:]}, None)
        self.stub_all(video_details=fn)

    def test_a_bot_check_pauses_every_lookup(self):
        self.answers({"a" * 11: BOT_CHECK})
        with self.assertRaises(api.LookupFailed) as caught:
            api.video_info("a" * 11)
        self.assertNotIsInstance(caught.exception, api.BackingOff, "that one was asked")
        with self.assertRaises(api.BackingOff) as caught:
            api.video_info("b" * 11)      # another video: still this IP
        self.assertEqual(len(self.calls), 1, "a paused lookup must not ask YouTube")
        self.assertIn("paused", str(caught.exception))
        left, reason = api.bot_pause_left()
        self.assertTrue(50 < left <= 61)
        self.assertIn("not a bot", reason)

    def test_answers_already_kept_are_served_during_a_pause(self):
        self.answers({"b" * 11: BOT_CHECK})
        api.video_info("a" * 11)
        with self.assertRaises(api.LookupFailed):
            api.video_info("b" * 11)
        self.assertTrue(api.video_info("a" * 11)["cached"])

    def test_a_pause_doubles_while_the_check_keeps_coming_back(self):
        api._note_outcome(BOT_CHECK)
        self.assertEqual(api.BOT_PAUSE["seconds"], 60)
        api._note_outcome(BOT_CHECK)          # one already in flight: no change
        self.assertEqual(api.BOT_PAUSE["seconds"], 60)
        for expected in (120, 240, 480, 900, 900):
            api.BOT_PAUSE["until"] = 0.0      # the pause ran out...
            api._note_outcome(BOT_CHECK)      # ...and the first ask met it again
            self.assertEqual(api.BOT_PAUSE["seconds"], expected)
        api.BOT_PAUSE["until"] = 0.0
        api._note_outcome(None)               # an answer got through
        self.assertEqual(api.BOT_PAUSE["streak"], 0)
        api._note_outcome(BOT_CHECK)
        self.assertEqual(api.BOT_PAUSE["seconds"], 60)

    def test_other_failures_do_not_pause(self):
        for said in ("ERROR: Video unavailable", "ERROR: Private video",
                     "ERROR: the page needs to be reloaded",   # cookies.txt, not the IP
                     "YouTube refused the 'en' captions (HTTP Error 429)"):
            api._note_outcome(said)
            self.assertEqual(api.bot_pause_left(), (0, None), said)

    def test_a_search_that_met_the_check_pauses_too(self):
        self.stub_all(search_items=lambda query: ([], BOT_CHECK))
        answer = api.search_youtube("anything", dates=False)
        self.assertIn("not a bot", answer["error"])
        self.assertTrue(api.bot_pause_left()[0])
        with self.assertRaises(api.BackingOff):
            api.search_youtube("something else", dates=False)

    def test_the_pause_can_be_turned_off(self):
        api.BOT_PAUSE_FIRST = 0
        self.answers({"a" * 11: BOT_CHECK})
        for _ in range(2):
            with self.assertRaises(api.LookupFailed) as caught:
                api.video_info("a" * 11)
            self.assertNotIsInstance(caught.exception, api.BackingOff)
        self.assertEqual(len(self.calls), 2)

    def test_the_engine_reports_itself(self):
        status = api.engine_status()
        self.assertEqual(status["slots"], 2)
        self.assertEqual(status["bot_pause_seconds_left"], 0)
        api._note_outcome(BOT_CHECK)
        self.assertTrue(api.engine_status()["bot_pause_seconds_left"])


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
                   {"url": "6", "status": "channel", "videos": 3},
                   {"url": "7", "status": "blocked", "error": "not a bot"}]
        self.assertEqual([f["url"] for f in api.failures_from(records)], ["3", "4", "5", "7"])

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


class TestCrossProcessFolderLock(unittest.TestCase):
    """start_job's own clash check is blind to other processes.

    JOBS is per process, so a second server - or a plain CLI run in the same
    folder - used to start beside a live job and truncate the same three logs.
    The downloader takes the real lock as it starts; asking here turns that
    into a refusal now (a 409) rather than a job that starts and ends in error.
    """

    HOLDER = """
import os, sys, time
sys.path.insert(0, %r)
import downloader
print("HELD" if not downloader.hold_folder(sys.argv[1], "download") else "BUSY", flush=True)
time.sleep(90)
"""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.saved_out, api.DEFAULT_OUT = api.DEFAULT_OUT, self.dir
        self.addCleanup(setattr, api, "DEFAULT_OUT", self.saved_out)

    def hold(self):
        import downloader
        code = self.HOLDER % os.path.dirname(os.path.abspath(downloader.__file__))
        proc = subprocess.Popen([sys.executable, "-c", code, self.dir],
                                stdout=subprocess.PIPE, text=True)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        self.assertEqual(proc.stdout.readline().strip(), "HELD")

    def test_a_folder_another_process_holds_is_refused(self):
        self.hold()
        opts = api.normalize_request({"links": "dQw4w9WgXcQ"})
        with self.assertRaises(RuntimeError) as caught:
            api.start_job(opts)
        self.assertIn("not one of this server's", str(caught.exception))

    def test_it_refuses_before_writing_anything(self):
        """The refusal must leave no links file and no job behind."""
        self.hold()
        before = set(os.listdir(self.dir))
        jobs_before = set(api.JOBS)
        with self.assertRaises(RuntimeError):
            api.start_job(api.normalize_request({"links": "dQw4w9WgXcQ"}))
        self.assertEqual(set(os.listdir(self.dir)), before)
        self.assertEqual(set(api.JOBS), jobs_before)

    def test_a_free_folder_still_passes_the_check(self):
        # The guard must not refuse the ordinary case.
        self.assertFalse(api.downloader.folder_busy(self.dir))


if __name__ == "__main__":
    unittest.main(verbosity=2)
