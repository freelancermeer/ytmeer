#!/usr/bin/env python3
"""Regression tests for downloader.py — run them on macOS and on Windows.

Every test runs twice: once with the macOS code path and once with the Windows
one, so platform-specific behaviour (name limits, reserved names, path
separators) is covered from either machine.

    python3 test_downloader.py

No network access and no downloads: this exercises the pure logic — naming,
video-id matching, resume indexing, caption selection, transcript formatting.
"""

import contextlib
import io
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
                       d.SUB_LANG, d.DOWNLOAD_SUBS, d.DOWNLOAD_DIR,
                       d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION)
        d.IS_WINDOWS = self.WINDOWS
        d.NAME_LIMIT = 60 if self.WINDOWS else 150
        d.TRANSCRIPT_WRAP = 40
        d.SUB_LANG = "en"
        d.DOWNLOAD_SUBS = True
        d.SAVE_THUMBNAIL = True
        d.SAVE_DESCRIPTION = True

    def tearDown(self):
        (d.IS_WINDOWS, d.NAME_LIMIT, d.TRANSCRIPT_WRAP,
         d.SUB_LANG, d.DOWNLOAD_SUBS, d.DOWNLOAD_DIR,
         d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION) = self._saved

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
        # Every file the tool writes, longest prefix first — the budget has to
        # hold for all of them, not just the video.
        for prefix, suffix in (("words_not_found_", ".txt"), ("description_", ".txt"),
                               ("words_", ".txt"), ("trans_", ".txt"),
                               ("", ".mp4"), ("", ".jpg")):
            path = os.path.join(base, name, name, f"{prefix}{name}{suffix}")
            if self.WINDOWS:
                self.assertLessEqual(len(path), 260,
                                     f"{prefix}<name>{suffix} exceeds Windows MAX_PATH")

    def test_terminal_output_stays_ascii(self):
        # A Windows console that is not in UTF-8 mode turns anything else into
        # mojibake, and none of it earns its place on screen.
        import re as _re
        source = open("downloader.py", encoding="utf-8").read()
        offenders = []
        for number, line in enumerate(source.splitlines(), 1):
            if not _re.search(r"\b(print|bar\.done|bar\.update|note)\s*\(", line):
                continue
            if [c for c in line if ord(c) > 127] or _re.findall(r"\\u[0-9a-fA-F]{4}", line):
                offenders.append(number)
        self.assertEqual(offenders, [], f"non-ASCII printed at lines {offenders}")

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

    def test_candidates_try_auto_tracks_before_broadcast_ones(self):
        info = {"automatic_captions": {"en-orig": self._track("English (Original)"),
                                       "en": self._track("English")},
                "subtitles": {"en-uYU": self._track("English - CC1")},
                "_dummy": None}
        got = [(code, source) for code, source, _c in d.caption_candidates("u", info)]
        self.assertEqual(got, [("en", "auto"), ("en-orig", "auto"), ("en-uYU", "manual")])

    def test_candidates_ignore_other_languages(self):
        info = {"automatic_captions": {"de": self._track("German"),
                                       "fr": self._track("French")}}
        self.assertEqual(list(d.caption_candidates("u", info)), [])

    # A fake track store: each code maps to the caption entries it serves.
    def _stub_fetch(self, store, log):
        def fetch(folder, url, lang, source, client=()):
            log.append(lang)
            entries = store.get(lang)
            if entries is None:
                return False
            with open(os.path.join(folder, f"c.{lang}.json3"), "w", encoding="utf-8") as f:
                json.dump({"events": [{"tStartMs": int(s * 1000), "dDurationMs": 500,
                                       "segs": [{"utf8": t}]} for s, _e, t in entries]}, f)
            return True
        return fetch

    def _with_stub(self, store, info):
        log = []
        real_fetch, real_info = d.fetch_caption, d.fetch_info
        d.fetch_caption = self._stub_fetch(store, log)
        d.fetch_info = lambda url, extra=(), **kw: (info, None)
        try:
            with tempfile.TemporaryDirectory() as folder:
                trans, words = d.build_transcripts(folder, "u", info)
                return log, (os.path.basename(words) if words else None)
        finally:
            d.fetch_caption, d.fetch_info = real_fetch, real_info

    def test_keeps_looking_past_a_phrase_timed_track(self):
        info = {"automatic_captions": {"en": self._track("English"),
                                       "en-orig": self._track("English (Original)")}}
        store = {"en":      [(0.0, None, "A WHOLE PHRASE HERE NOW")],
                 "en-orig": [(0.0, None, "one"), (0.4, None, "word"), (0.8, None, "each")]}
        log, words = self._with_stub(store, info)
        self.assertEqual(log, ["en", "en-orig"], "it must try the next track")
        self.assertTrue(words.startswith("words_") and "not_found" not in words,
                        "the word-timed track was available and should have won")

    def test_marker_only_after_every_track_was_tried(self):
        info = {"automatic_captions": {"en": self._track("English")},
                "subtitles": {"en-uYU": self._track("English - CC1"),
                              "en-JkeT": self._track("English - DTVCC1")}}
        store = {"en": None,                                     # listed but serves nothing
                 "en-uYU":  [(0.0, None, "A WHOLE PHRASE HERE")],
                 "en-JkeT": [(0.0, None, "ANOTHER WHOLE PHRASE")]}
        log, words = self._with_stub(store, info)
        # Exact code, then the rest in a stable alphabetical order.
        self.assertEqual(log, ["en", "en-JkeT", "en-uYU"], "every English track must be tried")
        self.assertTrue(words.startswith("words_not_found_"))

    def test_phrase_track_still_produces_a_transcript(self):
        info = {"subtitles": {"en-uYU": self._track("English - CC1")}}
        store = {"en-uYU": [(0.0, None, "A WHOLE PHRASE HERE NOW")]}
        with tempfile.TemporaryDirectory() as folder:
            real_fetch, real_info = d.fetch_caption, d.fetch_info
            d.fetch_caption = self._stub_fetch(store, [])
            d.fetch_info = lambda url, extra=(), **kw: (info, None)
            try:
                trans, words = d.build_transcripts(folder, "u", info)
            finally:
                d.fetch_caption, d.fetch_info = real_fetch, real_info
            self.assertTrue(os.path.exists(trans), "trans_ must exist even without word timings")
            self.assertIn("not_found", os.path.basename(words))

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

    # ---------------------------------------------------------------- stats
    def test_sizes_are_readable(self):
        self.assertEqual(d.human_size(0), "0 B")
        self.assertEqual(d.human_size(900), "900 B")
        self.assertEqual(d.human_size(45_000_000), "42.9 MB")
        self.assertEqual(d.human_size(1_400_000_000), "1.3 GB")

    def test_durations_are_readable(self):
        self.assertEqual(d.human_time(0), "0s")
        self.assertEqual(d.human_time(7), "7s")
        self.assertEqual(d.human_time(83), "1m 23s")
        self.assertEqual(d.human_time(3725), "1h 02m 05s",
                         "past an hour it should not read as 62 minutes")

    def test_progress_follows_the_download_in_hand(self):
        # yt-dlp prints a "100% of ... at ..." line as each file finishes, and
        # the subtitle and thumbnail finish first. Following those pinned the
        # bar to 100% for the whole video.
        started = "[download]  45.2% of   38.76MiB at    1.50MiB/s ETA 00:25"
        finished = "[download] 100% of   85.89KiB in 00:00:00 at 175.56KiB/s"
        aria = "[#613b59 2.5MiB/7.6MiB(33%) CN:16 DL:3.8MiB ETA:1s]"
        self.assertEqual(d.parse_progress(started), (45.2, "1.50MiB/s"))
        self.assertIsNone(d.parse_progress(finished),
                          "a finished side file must not drive the bar")
        self.assertEqual(d.parse_progress(aria), (33.0, "3.8MiB/s"))
        self.assertIsNone(d.parse_progress("[youtube] Extracting URL: ..."))

    def test_a_new_file_resets_the_bar(self):
        self.assertTrue(d.NEW_FILE_RE.search("[download] Destination: video.f137.mp4"))
        self.assertTrue(d.NEW_FILE_RE.search('[Merger] Merging formats into "out.mp4"'))
        self.assertIsNone(d.NEW_FILE_RE.search("[download]  45.2% of 38.76MiB"))

    def test_aria2c_is_asked_to_report_progress(self):
        # With --summary-interval=0 aria2c says nothing while it works, and the
        # bar has nothing to follow.
        self.assertNotIn("--summary-interval=0", d.ARIA2C_ARGS)
        self.assertIn("--summary-interval=1", d.ARIA2C_ARGS)

    def test_folder_size_counts_every_file(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(d.folder_size(folder), 0)
            for name, size in (("a.mp4", 1000), ("b.txt", 24)):
                with open(os.path.join(folder, name), "wb") as f:
                    f.write(b"x" * size)
            os.makedirs(os.path.join(folder, "sub"))
            self.assertEqual(d.folder_size(folder), 1024)

    def test_folder_size_survives_a_missing_folder(self):
        self.assertEqual(d.folder_size("/nope/not/here"), 0)

    # ------------------------------------------------------------- backfill
    def _record_runs(self):
        """Capture the yt-dlp command lines a backfill would run."""
        runs = []
        real = d.subprocess.run

        class Done:
            returncode = 0
            stdout = stderr = ""

        d.subprocess.run = lambda cmd, *a, **k: runs.append(cmd) or Done()
        self.addCleanup(lambda: setattr(d.subprocess, "run", real))
        return runs

    def test_lone_caption_fetch_escalates_to_the_po_token_client(self):
        # A caption fetched on its own hits the same bot check the video does.
        # Without escalation the backfill silently produces nothing.
        saved = d.po_token_first[0]
        d.po_token_first[0] = False
        runs = self._record_runs()
        try:
            with tempfile.TemporaryDirectory() as folder:
                got = d.fetch_caption(folder, "u", "en", "auto")
        finally:
            d.po_token_first[0] = saved
        self.assertFalse(got, "no caption file appeared")
        self.assertEqual(len(runs), 2, "it should try again through the token client")
        self.assertIn("youtube:player_client=mweb,default", runs[1])

    def test_lone_thumbnail_fetch_escalates_too(self):
        saved = d.po_token_first[0]
        d.po_token_first[0] = False
        runs = self._record_runs()
        try:
            with tempfile.TemporaryDirectory() as folder:
                d.build_extras(folder, "u", {})
        finally:
            d.po_token_first[0] = saved
        self.assertEqual(len(runs), 2)
        self.assertIn("youtube:player_client=mweb,default", runs[1])

    def test_a_caller_that_knows_the_client_is_trusted(self):
        runs = self._record_runs()
        with tempfile.TemporaryDirectory() as folder:
            d.fetch_caption(folder, "u", "en", "auto", client=d.PO_TOKEN_FLAGS)
        self.assertEqual(len(runs), 1, "no need to guess when the client is known")

    # ------------------------------------------------ thumbnail & description
    def test_thumbnail_is_converted_to_jpg(self):
        flags = d.thumb_flags()
        self.assertIn("--write-thumbnail", flags)
        self.assertEqual(flags[flags.index("--convert-thumbnails") + 1], "jpg",
                         "YouTube serves .webp, which many editors will not open")
        d.SAVE_THUMBNAIL = False
        self.assertEqual(d.thumb_flags(), [])

    def test_description_file_keeps_the_details_and_the_text(self):
        info = {"description": "Line one.\nLine two.", "title": "My Video",
                "channel": "Some Channel", "upload_date": "20260512",
                "duration": 337, "view_count": 8904,
                "webpage_url": "https://www.youtube.com/watch?v=x"}
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "My Video")
            os.makedirs(folder)
            path = d.write_description(folder, info)
            self.assertEqual(os.path.basename(path), "description_My Video.txt")
            body = open(path, encoding="utf-8").read()
            self.assertIn("Channel:  Some Channel", body)
            self.assertIn("Uploaded: 2026-05-12", body, "the date should be readable")
            self.assertIn("Line one.\nLine two.", body, "the description itself is kept")

    def test_no_description_file_when_the_video_has_none(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(d.write_description(folder, {"description": "   "}))
            self.assertIsNone(d.find_description(folder))

    def test_existing_extras_are_not_fetched_again(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "My Video")
            os.makedirs(folder)
            open(os.path.join(folder, "My Video.jpg"), "w").close()
            d.write_description(folder, {"description": "text"})

            called = []
            real = d.subprocess.run
            d.subprocess.run = lambda *a, **k: called.append(a) or real(["true"], **k)
            try:
                thumb, desc = d.build_extras(folder, "u", {"description": "text"})
            finally:
                d.subprocess.run = real
            self.assertEqual(called, [], "nothing to fetch, so no yt-dlp call")
            self.assertTrue(thumb.endswith("My Video.jpg"))
            self.assertTrue(os.path.basename(desc).startswith("description_"))

    def test_videoinfo_records_the_extras(self):
        with tempfile.TemporaryDirectory() as folder:
            d.write_info(folder, "T", "u", "1080p", "OK",
                         thumbnail=os.path.join(folder, "T.jpg"),
                         description=os.path.join(folder, "description_T.txt"))
            fields = d.read_info(folder)
            self.assertEqual(fields["Thumbnail"], "T.jpg")
            self.assertEqual(fields["Description"], "description_T.txt")

    def test_extras_can_be_turned_off(self):
        d.SAVE_THUMBNAIL = False
        d.SAVE_DESCRIPTION = False
        with tempfile.TemporaryDirectory() as folder:
            thumb, desc = d.build_extras(folder, "u", {"description": "text"})
            self.assertIsNone(thumb)
            self.assertIsNone(desc)
            self.assertEqual(os.listdir(folder), [])

    # ------------------------------------------------------------- retrying
    def _no_sleep(self):
        """Skip the real backoff waits, and keep the retry chatter out of the
        test output."""
        real = d.time.sleep
        d.time.sleep = lambda _s: None
        self.addCleanup(lambda: setattr(d.time, "sleep", real))
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(lambda: quiet.__exit__(None, None, None))

    def test_permanent_failures_are_not_retried(self):
        for text in ("ERROR: Private video. Sign in if you've been granted access",
                     "ERROR: [youtube] xxx: This video is unavailable",
                     "Video has been removed by the uploader"):
            self.assertTrue(d.is_permanent(text), text)

    def test_recoverable_failures_are_not_treated_as_permanent(self):
        for text in ("HTTP Error 403: Forbidden",
                     "aria2c exited with code 22",
                     "Did not get any data blocks",
                     "This video is not available"):
            self.assertFalse(d.is_permanent(text), text)

    def _stub_info(self, results):
        """Make fetch_info_once return each result in turn; records the flags used."""
        seen = []
        results = list(results)
        real = d.fetch_info_once

        def once(url, extra_flags=()):
            seen.append(tuple(extra_flags))
            return results.pop(0) if results else (None, "exhausted")

        d.fetch_info_once = once
        self.addCleanup(lambda: setattr(d, "fetch_info_once", real))
        return seen

    def test_metadata_lookup_is_retried(self):
        self._no_sleep()
        seen = self._stub_info([(None, "HTTP Error 429: Too Many Requests"),
                                ({"title": "T"}, None)])
        info, err = d.fetch_info("u")
        self.assertEqual(info, {"title": "T"}, "the second lookup succeeded")
        self.assertIsNone(err)
        self.assertEqual(len(seen), 2)

    def test_metadata_lookup_falls_back_to_the_po_token_client(self):
        self._no_sleep()
        seen = self._stub_info([(None, "timed out"), (None, "timed out"),
                                ({"title": "T"}, None)])
        info, _err = d.fetch_info("u")
        self.assertEqual(info, {"title": "T"})
        self.assertEqual(len(seen), d.INFO_ATTEMPTS)
        self.assertEqual(seen[-1], tuple(d.PO_TOKEN_FLAGS),
                         "the last try should go through the PO-token client")

    def test_metadata_lookup_gives_up_on_a_dead_video(self):
        self._no_sleep()
        seen = self._stub_info([(None, "ERROR: [youtube] x: Video unavailable")])
        info, err = d.fetch_info("u")
        self.assertIsNone(info)
        self.assertIn("unavailable", err.lower())
        self.assertEqual(len(seen), 1, "a dead video must not be looked up three times")

    def test_caption_second_opinion_stays_cheap(self):
        self._no_sleep()
        seen = self._stub_info([(None, "timed out")])
        d.fetch_info("u", d.PO_TOKEN_FLAGS, attempts=1)
        self.assertEqual(len(seen), 1, "an optional caption lookup should not retry")

    def test_routes_skip_the_default_client_once_a_token_is_known_to_be_needed(self):
        saved = d.po_token_first[0]
        try:
            d.po_token_first[0] = False
            self.assertEqual(d.download_routes()[0][0], "default client")
            d.po_token_first[0] = True
            labels = [label for label, _f, _fast in d.download_routes()]
            self.assertNotIn("default client", labels,
                             "no point paying for an attempt already known to 403")
        finally:
            d.po_token_first[0] = saved

    def test_a_failing_video_gets_several_attempts(self):
        self._no_sleep()
        saved = d.po_token_first[0]
        d.po_token_first[0] = False
        calls = []
        try:
            def always_fails(flags, fast=True):
                calls.append((tuple(flags), fast))
                return "HTTP Error 403: Forbidden"
            err = d.run_with_retries(always_fails)
        finally:
            d.po_token_first[0] = saved
        self.assertTrue(err)
        self.assertEqual(len(calls), d.MAX_ATTEMPTS * len(d.download_routes()),
                         "every route should be tried on every attempt")
        self.assertGreaterEqual(len(calls), 3, "a failing video must get retried")

    def test_retrying_stops_as_soon_as_one_route_works(self):
        self._no_sleep()
        saved = d.po_token_first[0]
        d.po_token_first[0] = False
        calls = []
        try:
            def second_one_works(flags, fast=True):
                calls.append(tuple(flags))
                return None if flags else "HTTP Error 403: Forbidden"
            err = d.run_with_retries(second_one_works)
        finally:
            d.po_token_first[0] = saved
        self.assertIsNone(err)
        self.assertEqual(len(calls), 2)

    def test_a_permanent_failure_gives_up_immediately(self):
        self._no_sleep()
        calls = []
        def gone(flags, fast=True):
            calls.append(tuple(flags))
            return "ERROR: Private video. Sign in if you've been granted access"
        err = d.run_with_retries(gone)
        self.assertTrue(err)
        self.assertEqual(len(calls), 1, "a private video must not be retried nine times")

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
