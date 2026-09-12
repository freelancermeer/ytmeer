#!/usr/bin/env python3
"""Tests for api.py - the REST layer's logic.

    python3 test_api.py

No network, no downloads, and no fastapi needed: api.py keeps fastapi behind the
route factory, so everything tested here imports on a machine that only has the
standard library. Covers the parts where a bug would be silent - a request
normalised wrongly downloads the wrong thing, a failure dropped from the summary
is a failure nobody hears about.
"""

import json
import os
import tempfile
import unittest
import zipfile

import api


class TestNormalizeRequest(unittest.TestCase):
    """A request body has to be checked, not trusted."""

    def test_links_are_required(self):
        for bad in ({}, {"links": ""}, {"links": "   \n\n"}, {"links": []}):
            with self.assertRaises(ValueError):
                api.normalize_request(bad)

    def test_links_may_be_a_list(self):
        opts = api.normalize_request({"links": ["https://youtu.be/aaaaaaaaaaa",
                                               "https://youtu.be/bbbbbbbbbbb"]})
        self.assertEqual(opts["links"],
                         "https://youtu.be/aaaaaaaaaaa\nhttps://youtu.be/bbbbbbbbbbb")

    def test_defaults_are_filled_in(self):
        opts = api.normalize_request({"links": "x"})
        self.assertEqual(opts["views"], 0)
        self.assertEqual(opts["limit"], 0)
        self.assertEqual(opts["min_height"], 720)
        self.assertEqual(opts["max_height"], 1080)
        self.assertTrue(opts["subs"])
        self.assertEqual(opts["sub_lang"], "en")

    def test_a_misspelled_field_is_refused_not_ignored(self):
        # "view" for "views" would otherwise look like it worked, and quietly
        # download the entire channel.
        with self.assertRaises(ValueError) as caught:
            api.normalize_request({"links": "x", "view": 1000})
        self.assertIn("view", str(caught.exception))

    def test_numbers_arrive_as_numbers_even_as_strings(self):
        opts = api.normalize_request({"links": "x", "views": "1000", "limit": "3"})
        self.assertEqual((opts["views"], opts["limit"]), (1000, 3))

    def test_a_non_number_is_refused(self):
        with self.assertRaises(ValueError):
            api.normalize_request({"links": "x", "views": "lots"})

    def test_negatives_are_refused(self):
        with self.assertRaises(ValueError):
            api.normalize_request({"links": "x", "limit": -1})

    def test_an_inverted_height_range_is_refused(self):
        # yt-dlp would just find no format, which reads as "no formats available"
        # rather than as the mistake it is.
        with self.assertRaises(ValueError) as caught:
            api.normalize_request({"links": "x", "min_height": 1080,
                                   "max_height": 720})
        self.assertIn("max_height", str(caught.exception))

    def test_an_equal_height_range_is_fine(self):
        opts = api.normalize_request({"links": "x", "min_height": 720,
                                      "max_height": 720})
        self.assertEqual(opts["min_height"], 720)

    def test_flags_become_real_booleans(self):
        opts = api.normalize_request({"links": "x", "channel": 0, "subs": 1})
        self.assertIs(opts["channel"], False)
        self.assertIs(opts["subs"], True)

    def test_a_blank_language_falls_back_to_en(self):
        self.assertEqual(api.normalize_request({"links": "x", "sub_lang": ""})["sub_lang"],
                         "en")


class TestArgv(unittest.TestCase):
    """The request has to become the command line a person would have typed."""

    def argv(self, **over):
        payload = {"links": "x"}
        payload.update(over)
        opts = api.normalize_request(payload)
        return api.build_argv("/out", opts)

    def test_the_folder_is_the_positional_argument(self):
        self.assertEqual(self.argv()[3], "/out")

    def test_defaults_become_the_expected_flags(self):
        self.assertEqual(self.argv(views=1000, limit=3)[4:],
                         ["--channel", "--views", "1000", "--limit", "3",
                          "--min-height", "720", "--max-height", "1080",
                          "--sub-lang", "en"])

    def test_zero_views_and_limit_emit_no_flag_at_all(self):
        # --views 0 is a no-op, but it would still be reported in the banner and
        # in download_log.json as though a floor had been asked for.
        argv = self.argv(views=0, limit=0)
        self.assertNotIn("--views", argv)
        self.assertNotIn("--limit", argv)

    def test_turning_things_off_adds_the_negative_flags(self):
        argv = self.argv(channel=False, subs=False, thumbnail=False,
                         description=False, verbose=True)
        for flag in ("--no-subs", "--no-thumbnail", "--no-description", "--verbose"):
            self.assertIn(flag, argv)
        self.assertNotIn("--channel", argv)

    def test_no_subs_does_not_also_pass_a_language(self):
        self.assertNotIn("--sub-lang", self.argv(subs=False))

    def test_the_downloader_is_run_with_unbuffered_output(self):
        # Without -u the log would arrive in blocks, so a running job would look
        # stuck between videos.
        self.assertIn("-u", self.argv()[:3])


class TestApiKey(unittest.TestCase):
    def tearDown(self):
        api.API_KEY[0] = None

    def test_a_key_is_generated_when_none_is_given(self):
        key = api.configure_key()
        self.assertTrue(key and len(key) > 20)
        self.assertTrue(api.key_ok(key))

    def test_a_given_key_is_used_as_is(self):
        self.assertEqual(api.configure_key("hunter2"), "hunter2")
        self.assertTrue(api.key_ok("hunter2"))

    def test_the_wrong_key_and_no_key_are_both_refused(self):
        api.configure_key("hunter2")
        self.assertFalse(api.key_ok("wrong"))
        self.assertFalse(api.key_ok(None))
        self.assertFalse(api.key_ok(""))

    def test_authentication_can_be_turned_off(self):
        self.assertIsNone(api.configure_key(enabled=False))
        self.assertTrue(api.key_ok(None))     # nothing configured, nothing checked


class TestWriteInputs(unittest.TestCase):
    """downloader.py reads these files by name, so they have to be exact."""

    def test_links_and_skips_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            api.write_inputs(tmp, "https://youtu.be/aaaaaaaaaaa\n", "bbbbbbbbbbb")
            with open(os.path.join(tmp, "links.txt")) as f:
                self.assertEqual(f.read().strip(), "https://youtu.be/aaaaaaaaaaa")
            with open(os.path.join(tmp, "skip.txt")) as f:
                self.assertEqual(f.read().strip(), "bbbbbbbbbbb")

    def test_an_empty_skip_list_leaves_no_stale_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "skip.txt")
            with open(stale, "w") as f:
                f.write("ccccccccccc\n")
            api.write_inputs(tmp, "https://youtu.be/aaaaaaaaaaa", "")
            self.assertFalse(os.path.exists(stale))

    def test_a_cookie_file_lands_under_the_name_base_cmd_looks_for(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as up:
            src = os.path.join(up, "whatever_the_browser_called_it.txt")
            with open(src, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
            api.write_inputs(tmp, "https://youtu.be/aaaaaaaaaaa", "", src)
            with open(os.path.join(tmp, "cookies.txt")) as f:
                self.assertIn("Netscape", f.read())

    def test_no_cookie_clears_one_from_a_previous_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "cookies.txt")
            with open(stale, "w") as f:
                f.write("# old\n")
            api.write_inputs(tmp, "https://youtu.be/aaaaaaaaaaa", "", None)
            self.assertFalse(os.path.exists(stale))

    def test_the_folder_is_created_if_it_is_not_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            nested = os.path.join(tmp, "a", "b")
            api.write_inputs(nested, "https://youtu.be/aaaaaaaaaaa", "")
            self.assertTrue(os.path.exists(os.path.join(nested, "links.txt")))


class TestFailures(unittest.TestCase):
    """Whatever did not come down has to reach the caller, with its reason."""

    def test_every_kind_of_failure_is_reported(self):
        logged = {"videos": [
            {"url": "u1", "status": "failed", "error": "stream died", "title": "A"},
            {"url": "u2", "status": "unavailable", "error": "private video"},
            {"url": "u3", "status": "channel_failed", "error": "404"},
        ]}
        out = api.failures_in(logged)
        self.assertEqual([f["status"] for f in out],
                         ["failed", "unavailable", "channel_failed"])
        self.assertEqual(out[0]["error"], "stream died")

    def test_successes_and_skips_are_not_failures(self):
        logged = {"videos": [{"url": "u1", "status": "ok"},
                             {"url": "u2", "status": "skipped"},
                             {"url": "u3", "status": "channel", "videos": 4}]}
        self.assertEqual(api.failures_in(logged), [])

    def test_a_missing_or_empty_log_is_not_an_error(self):
        for logged in ({}, {"videos": []}, {"videos": None}, None):
            self.assertEqual(api.failures_in(logged), [])

    def test_a_failure_with_no_error_text_still_comes_through(self):
        out = api.failures_in({"videos": [{"url": "u1", "status": "failed"}]})
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["error"])


class TestZip(unittest.TestCase):
    def test_nothing_to_zip_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "links.txt"), "w").close()
            self.assertIsNone(api.zip_results(tmp))

    def test_a_missing_folder_is_not_an_error(self):
        self.assertIsNone(api.zip_results("/nope/not/here"))

    def test_video_folders_are_zipped_with_their_paths_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "Some Channel", "Some Video")
            os.makedirs(folder)
            with open(os.path.join(folder, "videoinfo.txt"), "w") as f:
                f.write("Title: x\n")
            path = api.zip_results(tmp)
            with zipfile.ZipFile(path) as z:
                self.assertEqual(z.namelist(),
                                 ["Some Channel/Some Video/videoinfo.txt"])

    def test_no_half_written_zip_is_left_at_the_real_name(self):
        # The zip is built beside the real name and moved into place, so a caller
        # never downloads a truncated archive.
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "v"))
            open(os.path.join(tmp, "v", "a.txt"), "w").close()
            api.zip_results(tmp)
            self.assertFalse(os.path.exists(os.path.join(tmp, "downloads.zip.part")))
            self.assertTrue(zipfile.is_zipfile(os.path.join(tmp, "downloads.zip")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
