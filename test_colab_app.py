#!/usr/bin/env python3
"""Tests for the Gradio layer (colab_app.py).

Everything that does not need gradio lives in test_api.py, which runs anywhere.
What is left here is the one thing that is genuinely Gradio's: which paths it is
allowed to hand back to a browser. Without gradio installed the file skips, so
`python3 test_colab_app.py` stays green on a machine that only runs the CLI.

    python3 test_colab_app.py
"""

import os
import tempfile
import unittest

try:
    import colab_app as c
    HAVE_GRADIO = True
except ImportError:                      # gradio is not installed here
    HAVE_GRADIO = False


@unittest.skipUnless(HAVE_GRADIO, "gradio is not installed")
class TestServablePaths(unittest.TestCase):
    """Gradio refuses to hand over a file outside the roots given to launch().

    Returning one raises inside Gradio and takes the whole response with it, so a
    finished download would report nothing at all. servable() has to catch that
    and say where the file is instead.
    """

    def roots(self, *paths):
        original = c.ALLOWED_ROOTS
        c.ALLOWED_ROOTS = list(paths)
        self.addCleanup(setattr, c, "ALLOWED_ROOTS", original)

    def test_a_file_inside_an_allowed_root_comes_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "downloads.zip")
            open(path, "w").close()
            self.roots(tmp)
            self.assertEqual(c.servable(path), path)

    def test_a_file_outside_every_root_is_refused_not_raised(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            path = os.path.join(other, "downloads.zip")
            open(path, "w").close()
            self.roots(tmp)
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
            inside, outside = (os.path.join(allowed, "x.zip"),
                               os.path.join(sibling, "x.zip"))
            open(inside, "w").close()
            open(outside, "w").close()
            self.roots(allowed)
            self.assertEqual(c.servable(inside), inside)     # control
            self.assertIsNone(c.servable(outside))


@unittest.skipUnless(HAVE_GRADIO, "gradio is not installed")
class TestSharedCore(unittest.TestCase):
    def test_the_ui_and_the_rest_api_share_one_implementation(self):
        # If these ever diverge, the UI and /api would download different things
        # from the same inputs.
        import api
        self.assertIs(c.api, api)
        self.assertEqual(c.DEFAULT_OUT, api.DEFAULT_OUT)


if __name__ == "__main__":
    if not HAVE_GRADIO:
        print("gradio is not installed here; these tests only matter on Colab.")
    unittest.main(verbosity=2)
