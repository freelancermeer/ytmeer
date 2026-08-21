#!/usr/bin/env python3
"""Regression tests for downloader.py — run them on macOS and on Windows.

Every test runs twice: once with the macOS code path and once with the Windows
one, so platform-specific behaviour (name limits, reserved names, path
separators) is covered from either machine.

    python3 test_downloader.py

No network access and no downloads: this exercises the pure logic — naming,
video-id matching, resume indexing, caption selection, transcript formatting.
"""

import json
import os
import sys
import tempfile
import unittest

import downloader as d


def glob_words(folder):
    """Real words_ files in a folder (excluding the not-found marker)."""
    return [n for n in os.listdir(folder)
            if n.startswith("words_") and not n.startswith("words_not_found_")]


class PlatformCase:
    """Mixin holding the tests; the two subclasses below run them per platform.

    It deliberately does not inherit from TestCase, so the shared body is not
    collected and run a third time on its own.
    """

    WINDOWS = False

    def setUp(self):
        self._saved = (d.IS_WINDOWS, d.NAME_LIMIT, d.TRANSCRIPT_WRAP,
                       d.SUB_LANG, d.DOWNLOAD_SUBS, d.DOWNLOAD_DIR)
        d.IS_WINDOWS = self.WINDOWS
        d.NAME_LIMIT = 60 if self.WINDOWS else 150
        d.TRANSCRIPT_WRAP = 40
        d.SUB_LANG = "en"
        d.DOWNLOAD_SUBS = True

    def tearDown(self):
        (d.IS_WINDOWS, d.NAME_LIMIT, d.TRANSCRIPT_WRAP,
         d.SUB_LANG, d.DOWNLOAD_SUBS, d.DOWNLOAD_DIR) = self._saved

    # ---------------------------------------------------------------- naming
    def test_forbidden_characters_replaced(self):
        self.assertEqual(d.sanitize(r'a/b\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_windows_reserved_names_are_escaped(self):
        for name in ("CON", "nul", "COM1", "LPT9", "aux.txt"):
            self.assertNotIn(d.sanitize(name).split(".")[0].upper(),
                             d.WINDOWS_RESERVED, f"{name} still reserved")

    def test_ordinary_names_are_not_mangled(self):
        for name in ("Console", "NULL", "COMEDY"):
            self.assertEqual(d.sanitize(name), name)

    def test_trailing_dots_and_spaces_removed(self):
        self.assertEqual(d.sanitize("Ends with a dot. "), "Ends with a dot")
        self.assertFalse(d.sanitize("x" * (d.NAME_LIMIT - 1) + ".. ").endswith("."))

    def test_control_characters_stripped(self):
        self.assertEqual(d.sanitize("bad\x07name\x00"), "badname")

    def test_length_is_capped(self):
        self.assertEqual(len(d.sanitize("Z" * 300)), d.NAME_LIMIT)

    def test_channel_path_fits_windows_limit(self):
        base = r"C:\Users\someone\Desktop\A Project"
        name = d.sanitize("Z" * 300)
        path = os.path.join(base, name, name, f"words_{name}.txt")
        if self.WINDOWS:
            self.assertLessEqual(len(path), 260, "path would exceed Windows MAX_PATH")

    def test_output_template_escapes_percent(self):
        tpl = d.output_template(os.path.join("base", "vid"), "100% Real")
        self.assertEqual(tpl, os.path.join("base", "vid", "100%% Real.%(ext)s"))

    # ------------------------------------------------------------- video ids
    def test_video_id_from_every_url_shape(self):
        for url in ("https://www.youtube.com/watch?v=6iyI6PLfVnI",
                    "https://www.youtube.com/watch?v=6iyI6PLfVnI&pp=ygULa2FzaA%3D%3D",
                    "https://youtu.be/6iyI6PLfVnI?si=xyz",
                    "https://www.youtube.com/shorts/6iyI6PLfVnI",
                    "https://www.youtube.com/live/6iyI6PLfVnI",
                    "https://www.youtube.com/embed/6iyI6PLfVnI"):
            self.assertEqual(d.video_id_from_url(url), "6iyI6PLfVnI", url)

    def test_non_youtube_url_has_no_id(self):
        self.assertIsNone(d.video_id_from_url("https://example.com/watch"))

    # ---------------------------------------------------------------- resume
    def _finished_video(self, folder, url, status="OK"):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "clip.mp4"), "w") as f:
            f.write("x")
        d.write_info(folder, "T", url, "1080p", status)

    def test_index_finds_both_layouts_and_ignores_failures(self):
        with tempfile.TemporaryDirectory() as base:
            flat = os.path.join(base, "Flat Video")
            nested = os.path.join(base, "A Channel", "Nested Video")
            broken = os.path.join(base, "Broken Video")
            self._finished_video(flat, "https://www.youtube.com/watch?v=aaaaaaaaaaa")
            self._finished_video(nested, "https://www.youtube.com/watch?v=bbbbbbbbbbb")
            self._finished_video(broken, "https://www.youtube.com/watch?v=ccccccccccc",
                                 status="ERROR")

            index = d.index_downloaded(base)
            self.assertEqual(index.get("aaaaaaaaaaa"), flat)
            self.assertEqual(index.get("bbbbbbbbbbb"), nested)
            self.assertNotIn("ccccccccccc", index, "failed download must be retried")

    def test_index_matches_url_with_tracking_suffix(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "Video")
            self._finished_video(folder, "https://www.youtube.com/watch?v=aaaaaaaaaaa&pp=ygUL")
            index = d.index_downloaded(base)
            plain = d.video_id_from_url("https://www.youtube.com/watch?v=aaaaaaaaaaa")
            self.assertIn(plain, index, "same video listed twice must resolve to one entry")

    def test_index_skips_folder_whose_video_file_is_gone(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "Video")
            os.makedirs(folder)
            d.write_info(folder, "T", "https://www.youtube.com/watch?v=aaaaaaaaaaa", "1080p", "OK")
            self.assertEqual(d.index_downloaded(base), {})

    def test_same_title_different_video_gets_its_own_folder(self):
        with tempfile.TemporaryDirectory() as base:
            taken = os.path.join(base, "Same Title")
            self._finished_video(taken, "https://www.youtube.com/watch?v=aaaaaaaaaaa")
            chosen = d.choose_folder(base, "Same Title", "bbbbbbbbbbb")
            self.assertEqual(chosen, os.path.join(base, "Same Title [bbbbbbbbbbb]"))

    def test_same_video_reuses_its_folder(self):
        with tempfile.TemporaryDirectory() as base:
            taken = os.path.join(base, "Same Title")
            self._finished_video(taken, "https://www.youtube.com/watch?v=aaaaaaaaaaa")
            self.assertEqual(d.choose_folder(base, "Same Title", "aaaaaaaaaaa"), taken)

    def test_channel_folder_only_used_in_channel_mode(self):
        d.DOWNLOAD_DIR = os.path.join("base")
        info = {"channel": "Some / Channel"}
        d.CHANNEL_MODE = False
        self.assertEqual(d.channel_dir(info), "base")
        d.CHANNEL_MODE = True
        self.assertEqual(d.channel_dir(info), os.path.join("base", "Some _ Channel"))
        self.assertEqual(d.channel_dir({}), os.path.join("base", "Unknown Channel"))
        d.CHANNEL_MODE = False

    # -------------------------------------------------------------- captions
    @staticmethod
    def _track(name):
        return [{"ext": "json3", "name": name}]

    def test_exact_language_preferred(self):
        info = {"automatic_captions": {"en": self._track("English"),
                                       "en-orig": self._track("English (Original)")}}
        self.assertEqual(d.resolve_sub_lang(info), ("en", "auto"))

    def test_variant_used_when_plain_code_absent(self):
        info = {"automatic_captions": {"en-orig": self._track("English (Original)")}}
        self.assertEqual(d.resolve_sub_lang(info), ("en-orig", "auto"))

    def test_machine_translated_track_is_last_resort(self):
        info = {"automatic_captions": {"en-fr-X": self._track("English from French - CC1"),
                                       "en-Jk": self._track("English - CC1")}}
        self.assertEqual(d.resolve_sub_lang(info), ("en-Jk", "auto"))

    def test_auto_captions_beat_manual_ones(self):
        # Auto-captions carry word timings; a broadcast CC track is per phrase.
        # Picking the manual one here is what words_<name>.txt cannot survive.
        info = {"automatic_captions": {"en": self._track("English")},
                "subtitles": {"en": self._track("English - CC1"),
                              "en-uYU": self._track("English - CC1")}}
        self.assertEqual(d.resolve_sub_lang(info), ("en", "auto"))

    def test_manual_captions_used_only_when_nothing_else(self):
        self.assertEqual(d.resolve_sub_lang({"subtitles": {"en": self._track("English")}}),
                         ("en", "manual"))

    def test_no_english_track_returns_none(self):
        self.assertEqual(d.resolve_sub_lang({"automatic_captions": {"de": self._track("German")}}),
                         (None, None))
        self.assertEqual(d.sub_flags(None, None), [], "no track means no subtitle flags")

    def test_only_the_chosen_kind_of_track_is_requested(self):
        auto = d.sub_flags("en", "auto")
        self.assertIn("--write-auto-subs", auto)
        self.assertNotIn("--write-subs", auto,
                         "enabling both lets yt-dlp swap in the phrase-timed manual track")
        manual = d.sub_flags("en", "manual")
        self.assertIn("--write-subs", manual)
        self.assertNotIn("--write-auto-subs", manual)

    def test_only_one_track_is_requested(self):
        flags = d.sub_flags("en", "auto")
        self.assertEqual(flags[flags.index("--sub-langs") + 1], "en",
                         "a wildcard here would pull hundreds of tracks and hit HTTP 429")

    # ------------------------------------------------------------ transcript
    def test_word_timed_segments_join_on_their_own_spaces(self):
        words = [(0.0, None, "of"), (0.1, None, " the"), (0.3, None, " FBI,")]
        self.assertEqual(d.group_words_into_lines(words)[0][1], "of the FBI,")

    def test_cc_segments_do_not_glue_together(self):
        words = [(0.0, None, "WAS"), (1.0, None, "UNABLE")]
        self.assertEqual(d.group_words_into_lines(words)[0][1], "WAS UNABLE")

    def test_lines_wrap_at_the_limit(self):
        words = [(float(i), None, f" word{i}") for i in range(20)]
        for _start, text in d.group_words_into_lines(words):
            self.assertLessEqual(len(text), d.TRANSCRIPT_WRAP + len(" word19"))

    def test_newlines_inside_a_segment_are_flattened(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "c.json3")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"events": [{"tStartMs": 0, "segs": [
                    {"utf8": "LINE ONE\nLINE TWO"}, {"utf8": "\n"}]}]}, f)
            words = d.parse_json3_words(path)
            self.assertEqual(len(words), 1, "the newline-only segment is not a word")
            self.assertNotIn("\n", words[0][2])

    def test_word_level_detection(self):
        word_timed = [(0.0, 0.4, "Senator"), (0.4, 0.6, " Van"), (0.6, 1.2, " Hollen")]
        phrase_timed = [(0.0, 2.0, "SENATOR VAN HOLLEN IS ASKING"),
                        (2.0, 4.0, "ABOUT THE EXCESSIVE DRINKING.")]
        self.assertTrue(d.is_word_level(word_timed))
        self.assertFalse(d.is_word_level(phrase_timed))
        self.assertFalse(d.is_word_level([]), "no caption is not word-level")

    def test_word_level_survives_a_few_multiword_entries(self):
        entries = [(float(i), None, "word") for i in range(20)]
        entries += [(20.0, None, "New York"), (21.0, None, "San Francisco")]
        self.assertTrue(d.is_word_level(entries), "a couple of place names are fine")

    def test_phrase_caption_yields_marker_not_fake_timings(self):
        import downloader
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "clip.en.json3")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"events": [
                    {"tStartMs": 0, "dDurationMs": 2000,
                     "segs": [{"utf8": "SENATOR VAN HOLLEN IS ASKING"}]},
                    {"tStartMs": 2000, "dDurationMs": 2000,
                     "segs": [{"utf8": "ABOUT THE EXCESSIVE DRINKING."}]}]}, f)
            trans, words = d.make_transcripts(folder)
            self.assertTrue(os.path.basename(trans).startswith("trans_"))
            self.assertTrue(os.path.basename(words).startswith("words_not_found_"),
                            "phrase-timed captions must not masquerade as word-level")
            self.assertFalse(glob_words(folder), "no words_ file should be written")

    def test_word_caption_yields_a_real_words_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "clip.en.json3")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"events": [{"tStartMs": 0, "dDurationMs": 1200, "segs": [
                    {"utf8": "Senator"}, {"tOffsetMs": 400, "utf8": " Van"},
                    {"tOffsetMs": 600, "utf8": " Hollen"}]}]}, f)
            trans, words = d.make_transcripts(folder)
            self.assertTrue(os.path.basename(words).startswith("words_"))
            self.assertNotIn("not_found", os.path.basename(words))
            with open(words, encoding="utf-8") as f:
                lines = [l for l in f if l.strip()]
            self.assertEqual(len(lines), 3, "one line per word")
            self.assertTrue(lines[1].startswith("[00:00:00.400]"),
                            "the caption's own timing must be used verbatim")

    def test_timestamp_formats(self):
        self.assertEqual(d.format_timestamp(3661.9), "[01:01:01]")
        self.assertEqual(d.format_timestamp_ms(1.2534), "[00:00:01.253]")

    def test_every_written_line_starts_with_a_timestamp(self):
        words = [(0.0, None, "ONE\nTWO"), (1.0, None, "THREE")]
        with tempfile.TemporaryDirectory() as tmp:
            for writer in (d.write_trans_txt, d.write_words_txt):
                out = os.path.join(tmp, "t.txt")
                writer(out, words)
                with open(out, encoding="utf-8") as f:
                    for line in f:
                        self.assertRegex(line, r"^\[\d{2}:\d{2}:\d{2}")

    # ------------------------------------------------------- command building
    def test_po_token_errors_trigger_a_retry(self):
        for text in ("ERROR: unable to download video data: HTTP Error 403: Forbidden",
                     "aria2c exited with code 22",
                     "Requested format is not available"):
            self.assertTrue(d.needs_po_token(text), text)

    def test_real_errors_do_not_trigger_a_retry(self):
        for text in ("Video unavailable", "This video is private", ""):
            self.assertFalse(d.needs_po_token(text), text)
            self.assertFalse(d.is_transient(text), text)

    def test_stream_hiccups_are_retried(self):
        for text in ("ERROR: Did not get any data blocks",
                     "HTTP Error 503: Service Unavailable",
                     "The read operation timed out",
                     "Remote end closed connection without response"):
            self.assertTrue(d.is_transient(text), text)

    def test_failed_download_leftovers_are_marked(self):
        with tempfile.TemporaryDirectory() as folder:
            partial = os.path.join(folder, "Clip.mp4")
            with open(partial, "w") as f:
                f.write("video with no audio")

            marked = d.mark_incomplete(folder)

            self.assertEqual(marked, ["INCOMPLETE_Clip.mp4"])
            self.assertFalse(os.path.exists(partial))
            self.assertIsNone(d.find_video_file(folder),
                              "a marked partial must never count as the video")

    def test_folder_named_video_wins_over_an_older_one(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "My Clip")
            os.makedirs(folder)
            for name in ("An Older Download.mp4", "My Clip.mp4", "Another.mkv"):
                with open(os.path.join(folder, name), "w") as f:
                    f.write("x")
            self.assertEqual(d.find_video_file(folder),
                             os.path.join(folder, "My Clip.mp4"))

    def test_marked_partial_does_not_satisfy_resume(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "Video")
            os.makedirs(folder)
            with open(os.path.join(folder, "INCOMPLETE_Clip.mp4"), "w") as f:
                f.write("x")
            d.write_info(folder, "T", "https://www.youtube.com/watch?v=aaaaaaaaaaa", "1080p", "OK")
            self.assertEqual(d.index_downloaded(base), {},
                             "a video with only a partial file must be downloaded again")

    def test_windows_only_flags(self):
        flags = d.windows_flags()
        if self.WINDOWS:
            self.assertIn("--windows-filenames", flags)
            self.assertNotIn("--trim-filenames", flags,
                             "it trims the whole template and flattens the video folder")
        else:
            self.assertEqual(flags, [], "macOS must stay on the plain path")

    def test_po_token_client_is_not_passed_twice(self):
        # On Windows the download already starts with PO_TOKEN_FLAGS, so
        # windows_flags() must not repeat the same --extractor-args.
        self.assertNotIn("youtube:player_client=mweb,default", d.windows_flags())

    def test_speed_flags_use_parallel_connections(self):
        flags = d.speed_flags()
        self.assertIn("--concurrent-fragments", flags)
        if d.ARIA2C:
            self.assertIn("aria2c", flags)
            self.assertTrue(any("-x16" in f for f in flags), "16 connections expected")


class TestMacOS(PlatformCase, unittest.TestCase):
    WINDOWS = False


class TestWindows(PlatformCase, unittest.TestCase):
    WINDOWS = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
