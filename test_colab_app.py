#!/usr/bin/env python3
"""Tests for the Colab/Gradio front-end (colab_app.py).

Kept out of test_downloader.py because this needs gradio installed, which the
local downloader does not. Without gradio the whole file skips, so
`python3 test_colab_app.py` stays green on a machine that only runs the CLI.

    python3 test_colab_app.py

No network and no downloads: this covers the translation layer — UI values to a
command line, and which paths Gradio is allowed to hand back.
"""

import os
import tempfile
import unittest
import zipfile

try:
    import colab_app as c
    import gradio as gr
    HAVE_GRADIO = True
except ImportError:                      # gradio is not installed here
    HAVE_GRADIO = False


@unittest.skipUnless(HAVE_GRADIO, "gradio is not installed")
class TestArgv(unittest.TestCase):
    """The UI's values have to become the command line a person would type."""

    def argv(self, **over):
        kw = dict(outdir="/out", channel=True, views=1000, limit=3, min_h=720,
                  max_h=1080, subs=True, sub_lang="en", thumbnail=True,
                  description=True, verbose=False)
        kw.update(over)
        return c.build_argv(kw["outdir"], kw["channel"], kw["views"], kw["limit"],
                            kw["min_h"], kw["max_h"], kw["subs"], kw["sub_lang"],
                            kw["thumbnail"], kw["description"], kw["verbose"])

    def test_the_folder_is_the_positional_argument(self):
        self.assertEqual(self.argv()[3], "/out")

    def test_defaults_become_the_expected_flags(self):
        self.assertEqual(self.argv()[4:],
                         ["--channel", "--views", "1000", "--limit", "3",
                          "--min-height", "720", "--max-height", "1080",
                          "--sub-lang", "en"])

    def test_zero_views_and_limit_emit_no_flag_at_all(self):
        # --views 0 would be a no-op, but it would also misreport the run in the
        # banner and in download_log.json, so it must not be passed.
        argv = self.argv(views=0, limit=0)
        self.assertNotIn("--views", argv)
        self.assertNotIn("--limit", argv)

    def test_blank_views_and_limit_are_tolerated(self):
        argv = self.argv(views=None, limit="")
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

    def test_a_blank_language_falls_back_to_en(self):
        argv = self.argv(sub_lang="")
        self.assertEqual(argv[argv.index("--sub-lang") + 1], "en")


@unittest.skipUnless(HAVE_GRADIO, "gradio is not installed")
class TestServablePaths(unittest.TestCase):
    """Gradio refuses to hand over a file outside the roots given to launch().

    Returning one raises inside Gradio and takes the whole response with it, so a
    finished download would report nothing at all. servable() has to catch that.
    """

    def test_a_file_inside_an_allowed_root_comes_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "downloads.zip")
            open(path, "w").close()
            original = c.ALLOWED_ROOTS
            c.ALLOWED_ROOTS = [tmp]
            self.addCleanup(setattr, c, "ALLOWED_ROOTS", original)
            self.assertEqual(c.servable(path), path)

    def test_a_file_outside_every_root_is_refused_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            path = os.path.join(other, "downloads.zip")
            open(path, "w").close()
            original = c.ALLOWED_ROOTS
            c.ALLOWED_ROOTS = [tmp]
            self.addCleanup(setattr, c, "ALLOWED_ROOTS", original)
            self.assertIsNone(c.servable(path))

    def test_a_missing_file_is_refused(self):
        self.assertIsNone(c.servable("/nope/nothing.zip"))
        self.assertIsNone(c.servable(None))

    def test_a_sibling_root_is_not_mistaken_for_a_prefix(self):
        # "downloads-old" must not pass because "downloads" is allowed; the
        # separator has to be part of the comparison. The files are real, or the
        # existence check would refuse them first and prove nothing.
        with tempfile.TemporaryDirectory() as tmp:
            allowed = os.path.join(tmp, "downloads")
            sibling = os.path.join(tmp, "downloads-old")
            os.makedirs(allowed)
            os.makedirs(sibling)
            inside = os.path.join(allowed, "x.zip")
            outside = os.path.join(sibling, "x.zip")
            open(inside, "w").close()
            open(outside, "w").close()
            original = c.ALLOWED_ROOTS
            c.ALLOWED_ROOTS = [allowed]
            self.addCleanup(setattr, c, "ALLOWED_ROOTS", original)
            self.assertEqual(c.servable(inside), inside)   # control
            self.assertIsNone(c.servable(outside))


@unittest.skipUnless(HAVE_GRADIO, "gradio is not installed")
class TestPrepare(unittest.TestCase):
    """The folder handed to downloader.py has to hold exactly the right files."""

    def test_links_and_skips_are_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            c.prepare(tmp, "https://youtu.be/aaaaaaaaaaa\n", "bbbbbbbbbbb", None)
            with open(os.path.join(tmp, "links.txt")) as f:
                self.assertEqual(f.read().strip(), "https://youtu.be/aaaaaaaaaaa")
            with open(os.path.join(tmp, "skip.txt")) as f:
                self.assertEqual(f.read().strip(), "bbbbbbbbbbb")

    def test_no_links_is_an_error_rather_than_an_empty_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(gr.Error):
                c.prepare(tmp, "   \n\n", "", None)

    def test_an_empty_skip_box_leaves_no_stale_skip_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "skip.txt")
            with open(stale, "w") as f:
                f.write("ccccccccccc\n")
            c.prepare(tmp, "https://youtu.be/aaaaaaaaaaa", "", None)
            self.assertFalse(os.path.exists(stale))

    def test_an_uploaded_cookie_file_lands_under_the_expected_name(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as up:
            src = os.path.join(up, "whatever_the_browser_called_it.txt")
            with open(src, "w") as f:
                f.write("# Netscape HTTP Cookie File\n")
            c.prepare(tmp, "https://youtu.be/aaaaaaaaaaa", "", src)
            # base_cmd() looks for exactly this name, nothing else.
            with open(os.path.join(tmp, "cookies.txt")) as f:
                self.assertIn("Netscape", f.read())

    def test_no_upload_clears_a_cookie_file_from_a_previous_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = os.path.join(tmp, "cookies.txt")
            with open(stale, "w") as f:
                f.write("# old\n")
            c.prepare(tmp, "https://youtu.be/aaaaaaaaaaa", "", None)
            self.assertFalse(os.path.exists(stale))


@unittest.skipUnless(HAVE_GRADIO, "gradio is not installed")
class TestFailures(unittest.TestCase):
    """Whatever did not come down has to reach the caller, with its reason.

    An API caller sees only what the summary carries, so a failure missing from
    here is a failure nobody finds out about.
    """

    def test_every_kind_of_failure_is_reported(self):
        logged = {"videos": [
            {"url": "u1", "status": "failed", "error": "stream died", "title": "A"},
            {"url": "u2", "status": "unavailable", "error": "private video"},
            {"url": "u3", "status": "channel_failed", "error": "404"},
        ]}
        out = c.failures_in(logged)
        self.assertEqual([f["status"] for f in out],
                         ["failed", "unavailable", "channel_failed"])
        self.assertEqual(out[0]["error"], "stream died")
        self.assertEqual(out[0]["title"], "A")

    def test_successes_and_skips_are_not_failures(self):
        logged = {"videos": [
            {"url": "u1", "status": "ok"},
            {"url": "u2", "status": "skipped"},
            {"url": "u3", "status": "channel", "channel": "X", "videos": 4},
        ]}
        self.assertEqual(c.failures_in(logged), [])

    def test_a_missing_or_empty_log_is_not_an_error(self):
        for logged in ({}, {"videos": []}, {"videos": None}, None):
            self.assertEqual(c.failures_in(logged), [])

    def test_a_failure_with_no_error_text_still_comes_through(self):
        # Better a failure with no reason than a failure nobody hears about.
        out = c.failures_in({"videos": [{"url": "u1", "status": "failed"}]})
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0]["error"])


@unittest.skipUnless(HAVE_GRADIO, "gradio is not installed")
class TestZip(unittest.TestCase):
    def test_nothing_to_zip_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "links.txt"), "w").close()   # a file, no folders
            self.assertIsNone(c.zip_results(tmp))

    def test_video_folders_are_zipped_with_their_paths_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "Some Channel", "Some Video")
            os.makedirs(folder)
            with open(os.path.join(folder, "videoinfo.txt"), "w") as f:
                f.write("Title: x\n")
            path = c.zip_results(tmp)
            self.assertTrue(path.endswith("downloads.zip"))
            with zipfile.ZipFile(path) as z:
                self.assertEqual(z.namelist(),
                                 ["Some Channel/Some Video/videoinfo.txt"])


if __name__ == "__main__":
    if not HAVE_GRADIO:
        print("gradio is not installed here; these tests only matter on Colab.")
    unittest.main(verbosity=2)
