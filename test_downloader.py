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
import datetime
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
                       d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION,
                       d.MIN_VIEWS, d.LIMIT, d.SKIP_IDS, d.CHANNEL_PAGE,
                       d.TRANSCRIPT_ONLY, d.EXTRACT_COMMENTS, d.COMMENT_LIMIT,
                       d.COMMENT_REPLIES, d.COMMENT_SORT, d.SEARCH_QUERY,
                       d.SEARCH_COUNT, d.SORT_BY, d.THIS_MONTH, d.IN_CHANNEL,
                       d.DAYS, d.SEARCH_MATCH, d.INCLUDE_SHORTS, d.MIN_DURATION,
                       d.RESOLVE_DATES)
        d.IS_WINDOWS = self.WINDOWS
        d.NAME_LIMIT = 60 if self.WINDOWS else 150
        d.TRANSCRIPT_WRAP = 40
        d.SUB_LANG = "en"
        d.DOWNLOAD_SUBS = True
        d.SAVE_THUMBNAIL = True
        d.SAVE_DESCRIPTION = True
        d.MIN_VIEWS = 0
        d.LIMIT = 0
        d.SKIP_IDS = set()
        d.TRANSCRIPT_ONLY = False
        d.EXTRACT_COMMENTS = False
        d.COMMENT_LIMIT = 100
        d.COMMENT_REPLIES = False
        d.COMMENT_SORT = "top"
        d.SEARCH_QUERY = None
        d.SEARCH_COUNT = None          # as the module ships: pick by route
        d.SORT_BY = "relevance"
        d.THIS_MONTH = False
        d.IN_CHANNEL = None
        d.DAYS = 0
        d.SEARCH_MATCH = "related"
        # Fetching publish dates is a network call per video. The tests that
        # care about it stub the subprocess and switch it on themselves.
        d.RESOLVE_DATES = False
        d.INCLUDE_SHORTS = False
        # The length floor is a concern of its own, with its own tests below.
        # Left off here so a fixture written to test a view floor or the
        # live/upcoming rule does not also have to carry a duration.
        d.MIN_DURATION = 0
        # Module state like any other: without this a summary written by one
        # test counts the channel failures logged by another.
        self._records = list(d.LOG_RECORDS)
        d.LOG_RECORDS.clear()
        d.skipped_by_list[0] = 0
        d.TRANSCRIPT_COUNT[0] = 0
        d.COMMENT_COUNT[0] = 0
        d.SEARCH_FOUND[0] = 0

    def tearDown(self):
        (d.IS_WINDOWS, d.NAME_LIMIT, d.TRANSCRIPT_WRAP,
         d.SUB_LANG, d.DOWNLOAD_SUBS, d.DOWNLOAD_DIR,
         d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION,
         d.MIN_VIEWS, d.LIMIT, d.SKIP_IDS, d.CHANNEL_PAGE,
         d.TRANSCRIPT_ONLY, d.EXTRACT_COMMENTS, d.COMMENT_LIMIT,
         d.COMMENT_REPLIES, d.COMMENT_SORT, d.SEARCH_QUERY,
         d.SEARCH_COUNT, d.SORT_BY, d.THIS_MONTH, d.IN_CHANNEL,
         d.DAYS, d.SEARCH_MATCH, d.INCLUDE_SHORTS, d.MIN_DURATION,
         d.RESOLVE_DATES) = self._saved
        d.LOG_RECORDS[:] = self._records

    # ------------------------------------------------------------------ search
    def test_the_default_net_is_youtubes_own_results_page(self):
        # ytsearch: answers a far narrower question: measured on one long title
        # it returned 2 videos where the results page returned 150.
        self.assertEqual(d.search_target("cats", 25),
                         "https://www.youtube.com/results?search_query=cats")

    def test_match_exact_uses_the_narrow_ytsearch_route(self):
        d.SEARCH_MATCH = "exact"
        self.assertEqual(d.search_target("cats", 25), "ytsearch25:cats")

    def test_a_query_is_url_encoded_for_the_results_page(self):
        self.assertEqual(d.search_target("two words & more", 5),
                         "https://www.youtube.com/results?"
                         "search_query=two%20words%20%26%20more")

    def test_only_the_dated_route_asks_for_videos_only(self):
        # The filter does not merely hide the non-video cards, it changes which
        # search YouTube runs - measured 150 -> 3 on one long title and 23 ->
        # 150 on a broad one. So it goes only where it is load-bearing: the
        # dated route, which resolves cards into their channels' uploads.
        self.assertNotIn("sp=", d.search_target("cats", 25))
        self.assertIn(f"&sp={d.VIDEOS_ONLY}",
                      d.search_target("cats", 25, videos_only=True))
        d.SEARCH_MATCH = "exact"
        self.assertNotIn("sp=", d.search_target("cats", 25, videos_only=True))

    def test_the_dated_route_really_passes_the_filter(self):
        _items, _err, cmd = self._dated("")
        target = cmd[-1]
        self.assertIn("/results?search_query=", target)
        self.assertIn(f"&sp={d.VIDEOS_ONLY}", target)

    def test_a_pipe_in_a_channel_name_cannot_shift_the_duration(self):
        # "NOOR | Holy Quran" adds a field to a "|"-joined record. With the name
        # in the middle that shifted duration off the end, and the length floor
        # then dropped all 32 of that channel's hour-long videos.
        d.MIN_DURATION = 180
        d.DAYS = 21
        lines = [
            "b-2e7duVMbM|418|not_live|3451|NOOR | Holy Quran - Quran",
            "c-3e7duVMbN|500|not_live|2800|Plain Name",
            "d-4e7duVMbO|600|not_live|30|Short | Channel",      # a real Short
            "e-5e7duVMbP|700|not_live|NA|No Length | Channel",
        ]
        saved = d.subprocess.run
        d.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, "\n".join(lines) + "\n", "")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                links, name, err = d.list_channel_recent(
                    "https://www.youtube.com/@X")
        finally:
            d.subprocess.run = saved
        self.assertIsNone(err)
        self.assertEqual(links, [d.watch_url("b-2e7duVMbM"), d.watch_url("c-3e7duVMbN")],
                         "the piped name must not cost that channel its videos")
        self.assertEqual(name, "NOOR | Holy Quran - Quran",
                         "and the name must come back whole")

    def test_a_short_record_is_skipped_not_read_as_blanks(self):
        d.MIN_DURATION = 0
        d.DAYS = 7
        saved = d.subprocess.run
        d.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 0, "aaaaaaaaaaa|500|not_live\n"
                    "bbbbbbbbbbb|500|not_live|900|Ch\n", "")
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                links, _name, _err = d.list_channel_recent("https://www.youtube.com/@X")
        finally:
            d.subprocess.run = saved
        self.assertEqual(links, [d.watch_url("bbbbbbbbbbb")])

    def test_a_query_may_contain_a_colon(self):
        # Only the first colon separates the prefix from the query.
        d.SEARCH_MATCH = "exact"
        self.assertEqual(d.search_target("kash patel: the hearing", 5),
                         "ytsearch5:kash patel: the hearing")

    def test_search_target_follows_in_channel(self):
        d.IN_CHANNEL = "https://www.youtube.com/@Handle"
        self.assertEqual(d.search_target("cats", 25),
                         "https://www.youtube.com/@Handle/search?query=cats")

    def test_channel_search_url_encodes_and_drops_a_tab(self):
        base = "https://www.youtube.com/@Handle"
        self.assertEqual(d.channel_search_url(base, "two words"),
                         base + "/search?query=two%20words")
        for tail in ("/videos", "/shorts", "/streams", "/search", "/about", "/"):
            self.assertEqual(d.channel_search_url(base + tail, "x"),
                             base + "/search?query=x",
                             f"a trailing {tail} should not be kept")

    def test_search_window_from_days(self):
        d.DAYS = 30
        since, label = d.search_window()
        self.assertEqual(label, "last 30 days")
        self.assertAlmostEqual(since, time.time() - 30 * 86400, delta=5)

    def test_search_window_this_month_starts_at_the_first(self):
        d.THIS_MONTH = True
        since, label = d.search_window()
        start = datetime.datetime.fromtimestamp(since)
        self.assertEqual((start.day, start.hour, start.minute), (1, 0, 0))
        self.assertIn(start.strftime("%b %Y"), label)

    def test_no_window_by_default(self):
        self.assertEqual(d.search_window(), (None, ""))

    def test_sort_by_views_puts_the_most_watched_first(self):
        items = [{"views": 10}, {"views": 900}, {"views": 50}]
        d.SORT_BY = "views"
        self.assertEqual([i["views"] for i in d.sort_items(items)], [900, 50, 10])

    def test_reverse_popularity_puts_the_least_watched_first(self):
        items = [{"views": 900}, {"views": 10}, {"views": 50}]
        d.SORT_BY = "least-viewed"
        self.assertEqual([i["views"] for i in d.sort_items(items)], [10, 50, 900])

    def test_an_unmeasured_count_sorts_last_either_direction(self):
        # Also for least-viewed: an unknown count is not "zero views".
        d.SORT_BY = "least-viewed"
        items = [{"views": None}, {"views": 5}]
        self.assertEqual([i["views"] for i in d.sort_items(items)], [5, None])

    def test_an_unmeasured_count_sorts_last(self):
        # A video YouTube declined to count must not outrank one it did.
        d.SORT_BY = "views"
        items = [{"views": None}, {"views": 5}]
        self.assertEqual([i["views"] for i in d.sort_items(items)], [5, None])

    def test_sort_by_date_and_oldest(self):
        items = [{"epoch": 100}, {"epoch": 300}, {"epoch": 200}]
        d.SORT_BY = "date"
        self.assertEqual([i["epoch"] for i in d.sort_items(items)], [300, 200, 100])
        d.SORT_BY = "oldest"
        self.assertEqual([i["epoch"] for i in d.sort_items(items)], [100, 200, 300])

    def test_relevance_keeps_youtubes_own_order(self):
        items = [{"views": 1}, {"views": 999}, {"views": 5}]
        d.SORT_BY = "relevance"
        self.assertEqual([i["views"] for i in d.sort_items(items)], [1, 999, 5])

    def test_links_file_carries_the_titles_as_comments(self):
        items = [{"id": "aaaaaaaaaaa", "url": "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                  "title": "One"},
                 {"id": "bbbbbbbbbbb", "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
                  "title": "Two"}]
        with tempfile.TemporaryDirectory() as base:
            path = d.write_search_links(os.path.join(base, "search_links.txt"),
                                        "cats", items, "last 7 days")
            body = open(path, encoding="utf-8").read()
            self.assertIn("# search: cats", body)
            self.assertIn("window: last 7 days", body)
            self.assertIn("# One\nhttps://www.youtube.com/watch?v=aaaaaaaaaaa", body)
            # the file the run then reads must yield exactly the two links
            self.assertEqual(d.read_links(path), [i["url"] for i in items])

    def test_a_search_never_overwrites_a_curated_links_file(self):
        # The search result is its own artifact. Storing it over the file the
        # user chose as input would destroy that input.
        import argparse as _ap
        with tempfile.TemporaryDirectory() as base:
            curated = os.path.join(base, "my_links.txt")
            with open(curated, "w", encoding="utf-8") as f:
                f.write("https://www.youtube.com/watch?v=aaaaaaaaaaa\n")
            d.DOWNLOAD_DIR = base
            d.SEARCH_QUERY = "cats"
            items = [{"id": "b" * 11, "title": "Two",
                      "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
                      "views": 1, "epoch": None, "channel": ""}]
            real_items, real_rec = d.search_items, d.log_record
            d.search_items = lambda q: (items, None)
            d.log_record = lambda **kw: None
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    written = d.run_search(_ap.Namespace(search="cats", links=curated))
            finally:
                d.search_items, d.log_record = real_items, real_rec
            self.assertEqual(os.path.basename(written), "search_links.txt")
            self.assertEqual(open(curated, encoding="utf-8").read(),
                             "https://www.youtube.com/watch?v=aaaaaaaaaaa\n",
                             "the curated file must be untouched")

    def test_search_flag_defaults(self):
        args = d.parse_args(["/tmp"])
        self.assertIsNone(args.search)
        self.assertIsNone(args.in_channel)
        self.assertIsNone(args.search_count)       # None = pick by route
        self.assertEqual(args.sort, "relevance")
        self.assertFalse(args.this_month)
        self.assertEqual(args.match, "related")    # near-matches come too
        self.assertFalse(args.shorts)              # long videos only
        self.assertEqual(args.min_duration, d.SHORTS_SECONDS)

    def test_the_search_depth_follows_the_route(self):
        # Scanning costs about 0.07s per result flat and about 2s dated, so one
        # default cannot serve both: 150 dated would take five minutes.
        d.SEARCH_COUNT = None
        self.assertEqual(d.search_depth(False), d.SEARCH_DEPTH_FLAT)
        self.assertEqual(d.search_depth(True), d.SEARCH_DEPTH_DATED)
        self.assertGreater(d.SEARCH_DEPTH_FLAT, d.SEARCH_DEPTH_DATED)

    def test_an_explicit_search_count_wins_on_either_route(self):
        d.SEARCH_COUNT = 7
        self.assertEqual(d.search_depth(False), 7)
        self.assertEqual(d.search_depth(True), 7)

    def test_a_date_window_shortens_the_default_scan(self):
        d.SEARCH_COUNT = None
        d.DAYS = 30
        since, _label = d.search_window()
        self.assertEqual(d.search_depth(since is not None), d.SEARCH_DEPTH_DATED)
        d.DAYS = 0
        d.SORT_BY = "date"            # a date sort needs the dated route too
        since, _label = d.search_window()
        self.assertEqual(d.search_depth(since is not None or d.SORT_BY in ("date", "oldest")),
                         d.SEARCH_DEPTH_DATED)

    def test_a_duration_is_read_not_merely_type_checked(self):
        # A route handing over "900" describes a 15-minute video; treating that
        # as "length unknown" would drop it, which is the one thing the floor
        # must never do by accident.
        for value, want in ((900, 900.0), ("900", 900.0), (" 900 ", 900.0),
                            ("900.0", 900.0), (900.5, 900.5)):
            self.assertEqual(d.as_seconds(value), want, repr(value))
        for value in ("NA", None, "", "abc", True, False, [], {}):
            self.assertIsNone(d.as_seconds(value), repr(value))

    def test_both_routes_read_a_duration_the_same_way(self):
        d.MIN_DURATION = 180
        line = "aaaaaaaaaaa\t1700000000\t500\tnot_live\t900.0\tChan\tA Title"
        items, _err, _cmd = self._dated(line + "\n")
        self.assertEqual(items[0]["duration"], 900.0,
                         "a fractional duration must not read as unknown")

    def test_a_channel_filtered_to_nothing_says_why(self):
        # The tally goes to the log through note(), which is --verbose only, so
        # without this a channel of Shorts looks like a channel with no videos.
        d.MIN_DURATION = 180
        self.listing([
            {"id": "a" * 11, "title": "S1", "view_count": 100, "duration": 30},
            {"id": "b" * 11, "title": "S2", "view_count": 200, "duration": None},
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            links, _name, err = d.list_channel_videos("https://www.youtube.com/@X")
        shown = buf.getvalue()
        self.assertEqual(links, [])
        self.assertIsNone(err)
        self.assertIn("none of 2 video(s) matched", shown)
        self.assertIn("1 shorter than", shown)
        self.assertIn("1 of unknown length", shown)
        self.assertIn("--shorts", shown, "it must name the way to undo it")

    def test_a_channel_that_kept_something_explains_nothing(self):
        d.MIN_DURATION = 180
        self.listing([
            {"id": "a" * 11, "title": "Long", "view_count": 100, "duration": 900},
            {"id": "b" * 11, "title": "S", "view_count": 200, "duration": 30},
        ])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual(len(links), 1)
        self.assertNotIn("none of", buf.getvalue())

    def test_an_empty_channel_is_not_explained_as_filtered(self):
        self.listing([])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual(links, [])
        self.assertNotIn("none of", buf.getvalue(),
                         "nothing was scanned, so there is no reason to give")

    def test_match_and_shorts_flags_parse(self):
        self.assertEqual(d.parse_args(["/tmp", "--match", "exact"]).match, "exact")
        self.assertTrue(d.parse_args(["/tmp", "--shorts"]).shorts)
        self.assertEqual(d.parse_args(["/tmp", "--min-duration", "600"]).min_duration, 600)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                d.parse_args(["/tmp", "--match", "sideways"])
            with self.assertRaises(SystemExit):
                d.parse_args(["/tmp", "--min-duration", "-1"])

    def test_least_viewed_is_a_sort_choice(self):
        self.assertEqual(d.parse_args(["/tmp", "--sort", "least-viewed"]).sort,
                         "least-viewed")

    def test_popularity_is_accepted_as_a_spelling_of_views(self):
        self.assertEqual(d.parse_args(["/tmp", "--sort", "popularity"]).sort,
                         "popularity")

    def test_in_channel_needs_a_query(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                d.parse_args(["/tmp", "--in-channel",
                              "https://www.youtube.com/@Handle"])

    def test_in_channel_rejects_a_video_link(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                d.parse_args(["/tmp", "--search", "x", "--in-channel",
                              "https://www.youtube.com/watch?v=aaaaaaaaaaa"])

    def test_two_windows_are_rejected(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                d.parse_args(["/tmp", "--search", "x", "--days", "7",
                              "--this-month"])

    # ---------------------------------------------------------------- comments
    @staticmethod
    def _comment(cid, parent="root", text="hi", likes=0, stamp=None, **extra):
        c = {"id": cid, "parent": parent, "text": text, "like_count": likes,
             "author": f"@{cid}"}
        if stamp is not None:
            c["timestamp"] = stamp
        c.update(extra)
        return c

    def test_comment_args_travel_in_one_extractor_pair(self):
        # yt-dlp keys extractor args by extractor, so a second "youtube:" pair
        # replaces the first. Client and comment settings must share one pair or
        # whichever came first is silently dropped.
        flags = d.comment_extractor_args(d.PO_TOKEN_FLAGS)
        self.assertEqual(flags.count("--extractor-args"), 1)
        pair = flags[flags.index("--extractor-args") + 1]
        self.assertTrue(pair.startswith("youtube:"))
        self.assertIn("player_client=mweb,default", pair)
        self.assertIn("comment_sort=top", pair)
        self.assertIn("max_comments=", pair)

    def test_comment_args_without_a_client(self):
        pair = d.comment_extractor_args([])[1]
        self.assertNotIn("player_client", pair)
        self.assertIn("comment_sort=top", pair)

    def test_replies_off_by_default_and_on_with_the_flag(self):
        # Measured: 100 top-level with no replies took 7.4s, while a plain
        # "100 comments" took 10.8s and came back 28 real ones and 72 replies.
        d.COMMENT_REPLIES = False
        self.assertIn("max_comments=all,100,0,0", d.comment_extractor_args([])[1])
        d.COMMENT_REPLIES = True
        self.assertIn("max_comments=all,100,all,all", d.comment_extractor_args([])[1])

    def test_comment_limit_zero_means_every_comment(self):
        d.COMMENT_LIMIT = 0
        self.assertIn("max_comments=all,all,0,0", d.comment_extractor_args([])[1])

    def test_threading_groups_replies_under_their_comment(self):
        flat = [self._comment("a"), self._comment("r1", parent="a"),
                self._comment("b"), self._comment("r2", parent="a")]
        threads = d.thread_comments(flat)
        self.assertEqual([t[0]["id"] for t in threads], ["a", "b"])
        self.assertEqual([r["id"] for r in threads[0][1]], ["r1", "r2"])
        self.assertEqual(threads[1][1], [])

    def test_a_reply_whose_parent_was_capped_is_not_lost(self):
        # Capping the parents can leave a reply whose comment was never fetched.
        threads = d.thread_comments([self._comment("a"),
                                     self._comment("orphan", parent="gone")])
        self.assertEqual(len(threads), 2, "the orphan is promoted, not dropped")
        self.assertIn("orphan", [t[0]["id"] for t in threads])

    def test_comment_time_prefers_the_timestamp(self):
        self.assertEqual(d.comment_time({"timestamp": 1778622302}),
                         "13 May 2026, 02:45 AM PKT")

    def test_comment_time_falls_back_then_gives_up(self):
        self.assertEqual(d.comment_time({"_time_text": "2 weeks ago"}), "2 weeks ago")
        self.assertEqual(d.comment_time({"timestamp": "nope"}), "NA")
        self.assertEqual(d.comment_time({}), "NA")

    def test_comments_file_is_threaded_and_labelled(self):
        d.EXTRACT_COMMENTS = True
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "My Clip")
            os.makedirs(folder)
            flat = [self._comment("a", likes=1, stamp=1778622302),
                    self._comment("r", parent="a", likes=5),
                    self._comment("b", likes=12, author_is_uploader=True)]
            path = d.write_comments_txt(folder, {"title": "T", "webpage_url": "u"}, flat)
            self.assertEqual(os.path.basename(path), "comments_My Clip.txt")
            body = open(path, encoding="utf-8").read()
            self.assertIn("Sort:         top", body)
            self.assertIn("2 comment(s), 1 reply(ies)", body)
            self.assertIn("(replies off)", body)
            self.assertIn("1 like |", body, "one like is not 1 likes")
            self.assertIn("12 likes |", body)
            self.assertIn("(uploader)", body)
            self.assertIn("\n      @r", body, "a reply is indented under its comment")

    def test_no_comments_file_when_there_are_no_comments(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(d.write_comments_txt(folder, {}, []))
            self.assertIsNone(d.find_comments_file(folder))

    def test_taking_comments_removes_the_info_json(self):
        # It is ~923 KB even for ten comments, because the bulk is format
        # metadata - about a megabyte of noise per folder if it is left behind.
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "clip")
            os.makedirs(folder)
            with open(os.path.join(folder, "clip.info.json"), "w") as f:
                json.dump({"comments": [self._comment("a")], "formats": [1, 2]}, f)
            got = d.take_comments(folder)
            self.assertEqual([c["id"] for c in got], ["a"])
            self.assertEqual([n for n in os.listdir(folder) if n.endswith(".info.json")], [])

    def test_taking_comments_survives_a_broken_info_json(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "clip")
            os.makedirs(folder)
            with open(os.path.join(folder, "clip.info.json"), "w") as f:
                f.write("{not json")
            self.assertEqual(d.take_comments(folder), [])
            self.assertEqual(os.listdir(folder), [], "the bad file is still removed")

    def test_taking_comments_leaves_an_info_json_it_did_not_write(self):
        # The folder may hold metadata some other tool put there. Deleting it
        # was data loss for a file this run never asked yt-dlp to produce.
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "clip")
            os.makedirs(folder)
            with open(os.path.join(folder, "clip.info.json"), "w") as f:
                json.dump({"comments": [self._comment("a")]}, f)
            with open(os.path.join(folder, "notes.info.json"), "w") as f:
                json.dump({"mine": True}, f)
            self.assertEqual([c["id"] for c in d.take_comments(folder)], ["a"])
            self.assertEqual(sorted(os.listdir(folder)), ["notes.info.json"])

    def test_taking_comments_handles_a_folder_name_with_dots(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "S01.E02.Finale")
            os.makedirs(folder)
            with open(os.path.join(folder, "S01.E02.Finale.info.json"), "w") as f:
                json.dump({"comments": [self._comment("a")]}, f)
            self.assertEqual([c["id"] for c in d.take_comments(folder)], ["a"])
            self.assertEqual(os.listdir(folder), [])

    def test_comments_line_states(self):
        self.assertEqual(d.comments_line("/x/comments_A.txt", 37, "saved"),
                         "comments_A.txt (37 fetched)")
        self.assertEqual(d.comments_line(None, 0, "none"), "none")
        self.assertEqual(d.comments_line(None, 0, "failed"), "failed")

    def test_comments_are_skipped_entirely_when_off(self):
        d.EXTRACT_COMMENTS = False
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(d.build_comments(folder, "u", {}), (None, 0, "off"))
            self.assertEqual(os.listdir(folder), [])

    def test_a_comment_failure_never_fails_the_video(self):
        d.EXTRACT_COMMENTS = True
        real = d.fetch_comments
        d.fetch_comments = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            with tempfile.TemporaryDirectory() as folder:
                with contextlib.redirect_stdout(io.StringIO()):
                    path, count, state = d.build_comments(folder, "u", {})
        finally:
            d.fetch_comments = real
        self.assertEqual((path, count, state), (None, 0, "failed"))

    def test_comment_flag_defaults(self):
        args = d.parse_args(["/tmp"])
        self.assertFalse(args.extract_comments)
        self.assertEqual(args.comment_limit, 100, "top 100 is the default")
        self.assertFalse(args.comment_replies, "replies are off by default")
        self.assertEqual(args.comment_sort, "top")

    # ----------------------------------------------------- transcript-only
    def _folder(self, base, name, url, status="OK", video=True, transcript=True):
        """A video folder on disk, as one of the resume indexes would find it."""
        folder = os.path.join(base, name)
        os.makedirs(folder, exist_ok=True)
        if video:
            with open(os.path.join(folder, f"{name}.mp4"), "w") as f:
                f.write("x")
        if transcript:
            with open(os.path.join(folder, f"trans_{name}.txt"), "w") as f:
                f.write("[00:00:00] hello\n")
        d.write_info(folder, name, url, "1080p", status)
        return folder

    def test_walk_video_folders_covers_both_layouts(self):
        with tempfile.TemporaryDirectory() as base:
            flat = os.path.join(base, "Flat Video")
            nested = os.path.join(base, "A Channel", "Nested Video")
            os.makedirs(flat); os.makedirs(nested)
            found = list(d.walk_video_folders(base))
            self.assertIn(flat, found)
            self.assertIn(nested, found)
            self.assertIn(os.path.join(base, "A Channel"), found,
                          "the channel folder itself is walked too")
            self.assertLess(found.index(os.path.join(base, "A Channel")),
                            found.index(nested),
                            "a parent must be yielded before its children")

    def test_walk_video_folders_survives_an_unreadable_base(self):
        self.assertEqual(list(d.walk_video_folders("/nope/not/here")), [])

    def test_transcript_index_takes_both_statuses(self):
        with tempfile.TemporaryDirectory() as base:
            # left by an earlier --transcript run: no video file at all
            self._folder(base, "From Transcript Run",
                         "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                         status=d.TRANSCRIPT_STATUS, video=False)
            # left by a full download: its trans_ file is already written
            self._folder(base, "From Full Download",
                         "https://www.youtube.com/watch?v=bbbbbbbbbbb")
            index = d.index_transcribed(base)
            self.assertIn("aaaaaaaaaaa", index)
            self.assertIn("bbbbbbbbbbb", index,
                          "a full download already has the transcript")

    def test_transcript_index_wants_the_transcript_not_the_video(self):
        with tempfile.TemporaryDirectory() as base:
            self._folder(base, "Video No Transcript",
                         "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                         transcript=False)
            self.assertEqual(d.index_transcribed(base), {},
                             "a video without a transcript is not transcribed")

    def test_transcript_folder_never_counts_as_downloaded(self):
        # The load-bearing property: asking for the video later must still get
        # it, so a transcript-only folder must stay out of index_downloaded.
        with tempfile.TemporaryDirectory() as base:
            self._folder(base, "Transcript Only",
                         "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                         status=d.TRANSCRIPT_STATUS, video=False)
            self.assertEqual(d.index_downloaded(base), {})
            self.assertIn("aaaaaaaaaaa", d.index_transcribed(base))

    def test_transcript_contradicts_no_subs(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                d.parse_args(["/tmp", "--transcript", "--no-subs"])
        self.assertEqual(caught.exception.code, 2,
                         "argparse must reject the pair, not silently do nothing")

    def test_transcript_flag_defaults_off(self):
        self.assertFalse(d.parse_args(["/tmp"]).transcript)
        self.assertTrue(d.parse_args(["/tmp", "--transcript"]).transcript)

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

    # Writes a .txt with an explicit utf-8 encoding, so non-ASCII is fine there.
    FILE_WRITERS = {"write_words_not_found"}

    def test_terminal_output_stays_ascii(self):
        """No non-ASCII text can reach the console.

        A Windows console that is not in UTF-8 mode turns anything else into
        mojibake, and none of it earns its place on screen. This walks the
        whole module rather than matching print( line by line, because a
        message is regularly built on one line and written on the next -
        scan_progress does exactly that, and a line-based check cannot see it.
        """
        import ast as _ast
        with open("downloader.py", encoding="utf-8") as f:
            tree = _ast.parse(f.read())

        skip = set()
        for node in _ast.walk(tree):
            # Docstrings are never printed.
            if isinstance(node, (_ast.Module, _ast.FunctionDef, _ast.ClassDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, _ast.Expr) and isinstance(first.value, _ast.Constant) \
                        and isinstance(first.value.value, str):
                    skip.add(id(first.value))
            if isinstance(node, _ast.FunctionDef) and node.name in self.FILE_WRITERS:
                skip.update(id(n) for n in _ast.walk(node))

        offenders = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in skip:
                continue
            if any(ord(c) > 127 for c in node.value):
                offenders.append(node.lineno)
        self.assertEqual(sorted(offenders), [],
                         f"non-ASCII text reaching the console at lines {sorted(offenders)}")

    def test_diagnostic_trace_is_structured_and_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "download_debug.jsonl")
            saved = (d.LOG_DEBUG[0], d.RUN_ID[0], d.subprocess.run)
            try:
                d.LOG_DEBUG[0] = path
                d.RUN_ID[0] = "test-run"
                d.subprocess.run = lambda cmd, **kw: subprocess.CompletedProcess(
                    cmd, 0, "ok https://www.youtube.com/watch?v=abcdefghijk&pot=SECRET", "")
                d.run_logged(
                    "unit", ["yt-dlp", "--cookies", "/private/cookies.txt",
                             "--extractor-args", "youtube:po_token=SECRET",
                             "https://www.youtube.com/watch?v=abcdefghijk&pot=SECRET"],
                    capture_output=True, text=True)
            finally:
                d.LOG_DEBUG[0], d.RUN_ID[0], d.subprocess.run = saved

            with open(path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f]
            self.assertEqual([e["event"] for e in events],
                             ["command_start", "command_finish"])
            self.assertEqual(events[1]["returncode"], 0)
            body = json.dumps(events)
            self.assertNotIn("SECRET", body)
            self.assertIn("<cookies-file>", json.dumps(events[0]["command"]))
            self.assertIn("v=abcdefghijk", body)

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
        if status == "OK" and d.DOWNLOAD_SUBS:
            with open(os.path.join(folder, "trans_T.txt"), "w") as f:
                f.write("[00:00:00] caption\n")

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

    def test_a_vtt_only_auto_track_gives_way_to_the_manual_track(self):
        # "Me at the zoo": auto "en" is offered as vtt only, manual "en" as json3.
        info = {"automatic_captions": {"en": [{"ext": "vtt", "name": "English"}]},
                "subtitles": {"en": [{"ext": "json3", "name": "English"}, {"ext": "vtt", "name": "English"}]}}
        self.assertEqual(d.resolve_sub_lang(info), ("en", "manual"))
        got = [(code, source) for code, source, _c in d.caption_candidates("u", {**info, "automatic_captions": {
            "en": [{"ext": "vtt"}], "en-orig": [{"ext": "json3"}]}})]
        self.assertEqual(got, [("en-orig", "auto"), ("en", "manual")])

    def test_no_caption_file_outlives_the_transcript_search(self):
        # A fetch that yt-dlp answered with a vtt, however the search ends.
        saved = (d.caption_candidates, d.fetch_caption, d.DOWNLOAD_SUBS)
        with tempfile.TemporaryDirectory() as folder:
            def fetch(folder_, url, lang, source, client=()):
                for name in ("Clip.en.vtt", "Clip.en.ttml", "Clip.en.json3"):
                    open(os.path.join(folder_, name), "w").close()
                return False
            d.caption_candidates = lambda url, info: iter([("en", "auto", []), ("en", "manual", [])])
            d.fetch_caption, d.DOWNLOAD_SUBS = fetch, True
            try:
                for name in ("Clip.en.vtt", "Clip.mp4", "videoinfo.txt", "Old.en.srt"):
                    open(os.path.join(folder, name), "w").close()
                d.build_transcripts(folder, "u", {})
            finally:
                d.caption_candidates, d.fetch_caption, d.DOWNLOAD_SUBS = saved
            # .srt stays: the first version kept its transcripts that way.
            self.assertEqual(sorted(n for n in os.listdir(folder) if not n.startswith(("trans_", "words_"))),
                             ["Clip.mp4", "Old.en.srt", "videoinfo.txt"])

    def test_a_non_english_json3_auto_track_still_leaves_room_for_the_mweb_opinion(self):
        saved = d.fetch_info
        asked = []
        d.fetch_info = lambda url, flags=(), attempts=3: (asked.append(1), (None, "x"))[1]
        try:
            list(d.caption_candidates("u", {"automatic_captions": {"fr": [{"ext": "json3"}],
                                                                   "en": [{"ext": "vtt"}]}}))
        finally:
            d.fetch_info = saved
        self.assertEqual(asked, [1])

    def test_a_stopped_download_waits_for_yt_dlp_before_going(self):
        # Left running, yt-dlp would write its cookie jar into the removed private copy.
        class Proc:
            def __init__(self, timeout_first=False):
                self.calls, self.timeout_first = [], timeout_first
                self.stdout = self
            def read1(self, n):
                raise self.error
            def terminate(self): self.calls.append("terminate")
            def send_signal(self, sig): self.calls.append("sigint" if sig == signal.SIGINT else sig)
            def kill(self): self.calls.append("kill")
            def wait(self, timeout=None): self.calls.append("wait")
            def communicate(self, timeout=None):
                self.calls.append("communicate")
                if self.timeout_first == "again" and self.calls.count("communicate") == 1:
                    raise KeyboardInterrupt           # a second signal lands in the wait
                if self.timeout_first is True and "kill" not in self.calls:
                    raise subprocess.TimeoutExpired("yt-dlp", timeout)
                return b"", None
        saved, handler = d.subprocess.Popen, signal.getsignal(signal.SIGINT)
        try:
            for error, timeout_first, expected in (
                    (KeyboardInterrupt(), False, ["communicate"]),          # the group got the Ctrl+C
                    (SystemExit(143), False, ["sigint" if os.name == "posix" else "terminate",
                                              "communicate"]),                     # a kill reached us alone
                    (KeyboardInterrupt(), True, ["communicate", "kill", "wait"]),    # never unbounded
                    (SystemExit(143), "again", ["sigint" if os.name == "posix" else "terminate",
                                                "communicate", "communicate"])):     # waits on, exits 143
                proc = Proc(timeout_first)
                proc.error = error
                d.subprocess.Popen = lambda *a, **k: proc
                with self.assertRaises(type(error)):
                    d.run_streaming(["yt-dlp"])
                self.assertEqual(proc.calls, expected)
                # Ctrl+C handling is left alone: after an error the batch carries on.
                self.assertEqual(signal.getsignal(signal.SIGINT), handler)
        finally:
            d.subprocess.Popen = saved
            signal.signal(signal.SIGINT, handler)

    def test_ctrl_c_stops_the_run_once_and_later_ones_are_ignored(self):
        saved = d.STOPPING[0]
        d.STOPPING[0] = False
        try:
            with self.assertRaises(KeyboardInterrupt):
                d.on_sigint(signal.SIGINT, None)
            self.assertIsNone(d.on_sigint(signal.SIGINT, None))   # the second press
        finally:
            d.STOPPING[0] = saved

    def test_a_kill_while_writing_the_summary_keeps_the_last_one_whole(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "download_log.json")
            with open(path, "w") as f:
                f.write('{"run": "before"}')
            saved = (d.LOG_JSON[0], d.json.dump)
            def dump(obj, f, **kw):
                f.write('{"run": ')
                raise SystemExit(143)
            d.LOG_JSON[0], d.json.dump = path, dump
            try:
                with self.assertRaises(SystemExit):
                    d.write_json_log({"x": 1})
            finally:
                d.LOG_JSON[0], d.json.dump = saved
            with open(path) as f:
                self.assertEqual(json.load(f), {"run": "before"})
            self.assertEqual(os.listdir(folder), ["download_log.json"])

    def test_a_kill_to_the_downloader_alone_reaches_its_group(self):
        # Otherwise aria2c keeps downloading and holds the pipe the wait is on.
        sent = []
        saved = (d.os.getpgrp, d.os.getpid, d.os.killpg, d.signal.signal)
        self.addCleanup(d.STOPPING.__setitem__, 0, d.STOPPING[0])
        d.os.getpgrp, d.os.getpid = (lambda: 4242), (lambda: 4242)
        d.os.killpg = lambda pgid, sig: sent.append((pgid, sig))
        d.signal.signal = lambda sig, handler: sent.append((sig, handler))
        try:
            with self.assertRaises(SystemExit) as stop:
                d.stop_on_sigterm(signal.SIGTERM, None)
            d.os.getpid = lambda: 99                    # inside someone else's group: only exit
            with self.assertRaises(SystemExit):
                d.stop_on_sigterm(signal.SIGTERM, None)
        finally:
            d.os.getpgrp, d.os.getpid, d.os.killpg, d.signal.signal = saved
        self.assertEqual(stop.exception.code, 128 + signal.SIGTERM)
        if os.name == "posix":
            self.assertEqual([s for s in sent if s[0] == 4242], [(4242, signal.SIGTERM)])

    def test_stale_cookie_copies_are_swept_but_live_ones_kept(self):
        with tempfile.TemporaryDirectory() as folder:
            old, live = (os.path.join(folder, n) for n in (".ytdl_cookies_old.txt", ".ytdl_cookies_live.txt"))
            for p in (old, live):
                open(p, "w").close()
            os.utime(old, (time.time() - 2 * 86400,) * 2)
            d.remove_stale_copies(folder)
            self.assertEqual(os.listdir(folder), [".ytdl_cookies_live.txt"])

    def test_a_vtt_only_auto_track_still_asks_the_mweb_client(self):
        saved = d.fetch_info
        asked = []
        d.fetch_info = lambda url, flags=(), attempts=3: (asked.append(list(flags)), ({"automatic_captions": {
            "en": [{"ext": "json3"}]}}, None))[1]
        try:
            got = list(d.caption_candidates("u", {"automatic_captions": {"en": [{"ext": "vtt"}]},
                                                  "subtitles": {"en": [{"ext": "json3"}]}}))
        finally:
            d.fetch_info = saved
        self.assertEqual(asked, [d.PO_TOKEN_FLAGS])
        self.assertEqual([(c, s) for c, s, _ in got], [("en", "auto"), ("en", "manual")])

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

    def test_counts_fall_back_to_na(self):
        self.assertEqual(d.count_or_na(8920), "8,920")
        self.assertEqual(d.count_or_na(7550000), "7,550,000")
        self.assertEqual(d.count_or_na(0), "0")
        for missing in (None, "", "abc"):
            self.assertEqual(d.count_or_na(missing), "NA")

    def test_upload_time_is_pakistan_time_on_a_12_hour_clock(self):
        # 1778622302 is 2026-05-12 21:45:02 UTC, which is the 13th in PKT.
        self.assertEqual(d.upload_time_pkt({"timestamp": 1778622302}),
                         "13 May 2026, 02:45:02 AM PKT")

    def test_upload_time_says_so_when_only_the_day_is_known(self):
        stamped = d.upload_time_pkt({"upload_date": "20260512"})
        self.assertEqual(stamped, "12 May 2026 (time NA)",
                         "a date-only field must not imply a time")

    def test_upload_time_falls_back_to_na(self):
        for info in ({}, {"timestamp": "abc"}, {"upload_date": "nope"}, None):
            self.assertEqual(d.upload_time_pkt(info), "NA")

    def test_category_falls_back_to_na(self):
        self.assertEqual(d.category_or_na({"categories": ["News & Politics"]}),
                         "News & Politics")
        self.assertEqual(d.category_or_na({"categories": ["Music", "Comedy"]}),
                         "Music, Comedy")
        for info in ({"categories": []}, {}, None):
            self.assertEqual(d.category_or_na(info), "NA")

    def test_videoinfo_always_has_every_field(self):
        # Written even for a video whose metadata came back empty, so anything
        # reading the file finds the same shape every time.
        with tempfile.TemporaryDirectory() as folder:
            d.write_info(folder, "T", "u", "FAILED", "ERROR", "boom", info=None)
            fields = d.read_info(folder)
            for key in ("Channel", "Views", "Subscribers", "Uploaded", "Category"):
                self.assertEqual(fields[key], "NA", f"{key} should read NA")

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
        self.assertGreaterEqual(len(runs), 2)
        self.assertIn("youtube:player_client=web", runs[0])
        self.assertTrue(any("youtube:player_client=mweb,default" in run for run in runs[1:]))
        self.assertIn("--ignore-no-formats-error", runs[0])

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
            with open(path, encoding="utf-8") as f:
                body = f.read()
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

    # ------------------------------------------------ how a failure is recorded
    def _download_one_failing(self, error, info_error=None, base=None):
        """Run download_one with the network stubbed out; return (result, record)."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        saved = (d.fetch_info, d.choose_caption, d.run_with_retries, d.DONE_INDEX,
                 d.CHANNEL_MODE, list(d.LOG_RECORDS))
        info = None if info_error else {"id": "abcdefghijk", "title": "T"}
        d.fetch_info = lambda url, *a, **k: (info, info_error)
        d.choose_caption = lambda url, i: (None, None, [])
        d.run_with_retries = lambda attempt: error
        d.DONE_INDEX, d.CHANNEL_MODE, d.DOWNLOAD_DIR = {}, False, base or tmp.name
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                result = d.download_one("https://youtu.be/abcdefghijk", 1, 1)
            record = d.LOG_RECORDS[-1]
        finally:
            (d.fetch_info, d.choose_caption, d.run_with_retries, d.DONE_INDEX,
             d.CHANNEL_MODE, records) = saved
            d.LOG_RECORDS[:] = records
        return result, record

    def test_failure_statuses(self):
        self.assertEqual(d.failure_status("Sign in to confirm you\u2019re not a bot"), "blocked")
        self.assertEqual(d.failure_status("ERROR: Private video"), "unavailable")
        self.assertEqual(d.failure_status("HTTP Error 403: Forbidden"), "failed")

    def test_a_bot_gated_download_is_recorded_as_blocked(self):
        result, record = self._download_one_failing("ERROR: [youtube] x: Sign in to confirm you're not a bot.")
        self.assertEqual((result, record["status"]), ("gone", "blocked"))

    def test_a_bot_gated_lookup_is_recorded_as_blocked(self):
        result, record = self._download_one_failing(None, info_error="Sign in to confirm you're not a bot")
        self.assertEqual((result, record["status"]), ("gone", "blocked"))

    def test_no_format_in_range_is_final_so_the_sweep_leaves_it(self):
        result, record = self._download_one_failing(
            "[youtube] abcdefghijk: Requested format is not available. Use --list-formats")
        self.assertEqual((result, record["status"]), ("gone", "failed"))

    def test_a_passing_hiccup_is_still_worth_the_sweep(self):
        result, record = self._download_one_failing("HTTP Error 503: Service Unavailable")
        self.assertEqual((result, record["status"]), ("fail", "failed"))

    def test_media_download_does_not_finish_without_required_captions(self):
        """A caption 429 cannot be mistaken for a complete download."""
        with tempfile.TemporaryDirectory() as base:
            commands = []
            saved = (d.fetch_info, d.run_with_retries, d.run_streaming,
                     d.build_transcripts, d.DOWNLOAD_DIR, d.DONE_INDEX,
                     d.CHANNEL_MODE, d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION,
                     d.LOG_JSONL[0], list(d.LOG_RECORDS), dict(d.STATS))

            def run_streaming(cmd, progress=None):
                commands.append(cmd)
                d.last_returncode[0] = 0
                return ""

            try:
                d.fetch_info = lambda url: ({"id": "abcdefghijk", "title": "T"}, None)
                d.run_with_retries = lambda attempt: attempt([], True)
                d.run_streaming = run_streaming
                d.build_transcripts = lambda folder, url, info: (None, None)
                d.DOWNLOAD_DIR = base
                d.DONE_INDEX = {}
                d.CHANNEL_MODE = False
                d.SAVE_THUMBNAIL = d.SAVE_DESCRIPTION = False
                d.LOG_JSONL[0] = None
                d.LOG_RECORDS[:] = []
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(d.download_one("https://youtu.be/abcdefghijk", 1, 1), "fail")
            finally:
                (d.fetch_info, d.run_with_retries, d.run_streaming,
                 d.build_transcripts, d.DOWNLOAD_DIR, d.DONE_INDEX,
                 d.CHANNEL_MODE, d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION,
                 d.LOG_JSONL[0], records, stats) = saved
                d.LOG_RECORDS[:] = records
                d.STATS.clear()
                d.STATS.update(stats)

            self.assertEqual(len(commands), 1)
            self.assertNotIn("--write-auto-subs", commands[0])
            self.assertNotIn("--write-subs", commands[0])
            self.assertEqual(d.read_info(os.path.join(base, "T"))["Status"], "ERROR")
            self.assertIn("captions", d.read_info(os.path.join(base, "T"))["Error"])

    def test_files_take_the_name_of_the_folder_they_land_in(self):
        with tempfile.TemporaryDirectory() as base:
            os.makedirs(os.path.join(base, "T"))
            with open(os.path.join(base, "T", "videoinfo.txt"), "w") as f:
                f.write("Link:    https://youtu.be/zzzzzzzzzzz\n")      # another video's "T"
            seen, real = [], d.output_template
            d.output_template = lambda folder, name: seen.append(name) or real(folder, name)
            try:
                self._download_one_failing("HTTP Error 503", base=base)
            finally:
                d.output_template = real
        self.assertEqual(seen, ["T [abcdefghijk]"])

    def test_an_unmerged_stream_is_never_taken_for_the_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "My Video [abcdefghijk]")
            os.makedirs(folder)
            for name in ("My Video.f137.mp4", "My Video.temp.mp4"):
                open(os.path.join(folder, name), "w").close()
            self.assertIsNone(d.find_video_file(folder))
            open(os.path.join(folder, "My Video [abcdefghijk].mp4"), "w").close()
            self.assertTrue(d.find_video_file(folder).endswith("[abcdefghijk].mp4"))

    def _with_cookie_file(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "cookies.txt")
        with open(path, "w") as f:
            f.write("# Netscape HTTP Cookie File\n")
        saved = (d.COOKIES_FILE, d.use_cookies[0])
        d.COOKIES_FILE, d.use_cookies[0] = path, True
        self.addCleanup(lambda: (setattr(d, "COOKIES_FILE", saved[0]),
                                 d.use_cookies.__setitem__(0, saved[1])))
        return path

    def test_yt_dlp_gets_a_private_copy_of_the_cookies_until_youtube_rejects_them(self):
        path = self._with_cookie_file()
        with open(path, "a") as f:
            f.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n")
        def read(p):
            with open(p) as f:
                return f.read()
        before = read(path)
        cmd = d.base_cmd()
        copy = cmd[cmd.index("--cookies") + 1]
        self.assertNotEqual(copy, path)                   # yt-dlp saves its jar into this file
        self.assertEqual(read(copy), before)
        with open(copy, "w") as f:
            f.write("rewritten by yt-dlp")
        self.assertEqual(read(path), before)              # the user's own export is untouched
        self.assertEqual(d.base_cmd()[-1], d.base_cmd()[-1])
        d.use_cookies[0] = False
        self.assertNotIn("--cookies", d.base_cmd())

    def test_the_cookie_copy_sits_beside_the_original_and_follows_its_changes(self):
        path = self._with_cookie_file()
        with open(path, "a") as f:
            f.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\told\n")
        stat = os.stat(path)
        first = d.base_cmd()[-1]
        self.assertEqual(os.path.dirname(first), os.path.dirname(path))
        with open(path, "w") as f:                       # replaced, same mtime
            f.write("# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\tSID\tnew\n")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = d.base_cmd()[-1]
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(first))           # no pile of stale copies
        with open(second) as f:
            self.assertIn("new", f.read())

    def test_a_rejected_cookie_file_is_dropped_for_a_thumbnail_too(self):
        path = self._with_cookie_file()
        with open(path, "a") as f:
            f.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n")
        saved = (d.subprocess.run, d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION, d.po_token_first[0])
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            def run(cmd, **kw):
                calls.append("--cookies" in cmd)
                if "--cookies" in cmd:
                    return subprocess.CompletedProcess(cmd, 1, "", "ERROR: The page needs to be reloaded.\n")
                open(os.path.join(folder, os.path.basename(folder) + ".jpg"), "w").close()
                return subprocess.CompletedProcess(cmd, 0, "", "")
            d.subprocess.run, d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION, d.po_token_first[0] = run, True, False, False
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    thumb, _ = d.build_extras(folder, "https://youtu.be/x", {})
            finally:
                d.subprocess.run, d.SAVE_THUMBNAIL, d.SAVE_DESCRIPTION, d.po_token_first[0] = saved
            self.assertTrue(thumb)
        self.assertEqual(calls, [True, False])

    def test_an_unreadable_cookie_file_is_dropped_up_front(self):
        for content, why in ((b'[{"domain": ".youtube.com"}]', "JSON"), (b"\xff\xfe junk", "not a text"),
                             (b"# Netscape HTTP Cookie File\n", "no cookies")):
            path = self._with_cookie_file()
            with open(path, "wb") as f:
                f.write(content)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertNotIn("--cookies", d.base_cmd())
            self.assertIn(why, out.getvalue())
            self.assertFalse(d.use_cookies[0])

    def test_a_rejected_cookie_file_is_dropped_for_channel_listings_and_downloads_too(self):
        self._no_sleep()
        path = self._with_cookie_file()
        with open(path, "a") as f:
            f.write(".youtube.com\tTRUE\t/\tTRUE\t0\tSID\tvalue\n")
        saved = d.subprocess.run
        calls = []

        def run(cmd, **kw):
            calls.append("--cookies" in cmd)
            if "--cookies" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: [youtube:tab] x: The page needs to be reloaded.\n")
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"channel": "C", "entries": []}), "")

        d.subprocess.run = run
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(d.channel_page("https://www.youtube.com/@x/videos", 1, 50),
                                 ({"channel": "C", "entries": []}, None))
        finally:
            d.subprocess.run = saved
        self.assertEqual(calls, [True, False])

        d.use_cookies[0] = True
        tries = []

        def attempt(flags, fast=True):
            tries.append(d.use_cookies[0])
            return "[youtube] x: The page needs to be reloaded." if d.use_cookies[0] else None

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(d.run_with_retries(attempt))
        self.assertEqual(tries, [True, False])

    def test_every_url_goes_after_a_double_dash(self):
        # A "link" such as --exec=... must reach yt-dlp as a URL, never as an option.
        saved = d.subprocess.run
        seen = []
        d.subprocess.run = lambda cmd, **kw: (seen.append(cmd), subprocess.CompletedProcess(cmd, 1, "", "ERROR: x"))[1]
        try:
            d.fetch_info_once("--exec=touch x")
            d.channel_page("--exec=touch x", 1, 2)
        finally:
            d.subprocess.run = saved
        for cmd in seen:
            self.assertEqual(cmd[-2:], ["--", "--exec=touch x"])

    def test_rejected_cookies_are_dropped_and_the_lookup_retried_without_them(self):
        self._no_sleep()
        self._with_cookie_file()
        calls, saved = [], d.fetch_info_once

        def once(url, flags=()):
            calls.append(d.use_cookies[0])
            return (None, "[youtube] x: The page needs to be reloaded.") if d.use_cookies[0] else ({"id": "x"}, None)

        d.fetch_info_once = once
        try:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                info, err = d.fetch_info("https://youtu.be/x")
        finally:
            d.fetch_info_once = saved
        self.assertEqual((info, err), ({"id": "x"}, None))
        self.assertEqual(calls, [True, False])            # once with, once without
        self.assertFalse(d.use_cookies[0])                # the rest of the batch goes without
        self.assertIn("rejected cookies.txt", out.getvalue())

    def test_a_truncated_video_id_is_not_retried(self):
        err = "ERROR: [youtube:truncated_id] abc: Incomplete YouTube ID abc. URL https://www.youtube.com/watch?v=abc looks truncated."
        self.assertEqual(d.failure_status(err), "unavailable")

    def test_long_non_latin_titles_fit_a_linux_file_name(self):
        for title in ("\u0939\u093f" * 200, "\U0001F600" * 100, "a" * 400):
            name = d.sanitize(title)
            self.assertLessEqual(len(name.encode("utf-8")), d.NAME_BYTES)
            self.assertLessEqual(len(("words_not_found_" + name + " [abcdefghijk].f137.mp4.part").encode()), 255)
            name.encode("utf-8").decode("utf-8")          # never cut inside a character

    def test_a_reload_page_without_cookies_is_blocked_and_fails_fast(self):
        self._no_sleep()
        saved = (d.fetch_info_once, d.COOKIES_FILE, d.use_cookies[0])
        calls = []
        d.COOKIES_FILE = "/nonexistent/cookies.txt"
        d.fetch_info_once = lambda url, flags=(): (calls.append(1), (None, "The page needs to be reloaded."))[1]
        try:
            info, err = d.fetch_info("https://youtu.be/x")
        finally:
            d.fetch_info_once, d.COOKIES_FILE, d.use_cookies[0] = saved
        self.assertIsNone(info)
        self.assertEqual(d.failure_status(err), "blocked")
        self.assertEqual(len(calls), 2)                   # default, then mweb - no 3-round ladder

    def test_a_bot_gated_lookup_tries_the_mweb_client_once(self):
        self._no_sleep()
        calls, saved = [], (d.fetch_info_once, d.po_token_first[0])

        def once(url, flags=()):
            calls.append(list(flags))
            return ({"id": "x"}, None) if flags else (None, "Sign in to confirm you're not a bot")

        d.fetch_info_once, d.po_token_first[0] = once, False
        try:
            info, err = d.fetch_info("https://youtu.be/x")
            latched = d.po_token_first[0]
        finally:
            d.fetch_info_once, d.po_token_first[0] = saved
        self.assertEqual((info, err), ({"id": "x"}, None))
        self.assertEqual(calls, [[], d.PO_TOKEN_FLAGS])
        self.assertTrue(latched, "the rest of the batch should start with that client")

    def test_a_lookup_gated_on_every_client_gives_up_at_once(self):
        self._no_sleep()
        calls, saved = [], d.fetch_info_once
        d.fetch_info_once = lambda url, flags=(): (calls.append(1), (None, "Sign in to confirm you're not a bot"))[1]
        try:
            info, err = d.fetch_info("https://youtu.be/x")
        finally:
            d.fetch_info_once = saved
        self.assertIsNone(info)
        self.assertIn("not a bot", err)
        self.assertEqual(len(calls), 2)

    def test_a_listing_with_a_deadline_answers_with_what_it_has(self):
        d.CHANNEL_PAGE = 2
        asked = self.listing([self.entry("aaaaaaaaaaa"), self.entry("bbbbbbbbbbb")],
                             [self.entry("ccccccccccc")])
        links, _name, err = d.list_channel_videos("https://www.youtube.com/@X",
                                                  deadline=time.monotonic() - 1)
        self.assertEqual(asked, [(1, 2)])                   # never the second page
        self.assertEqual(len(links), 2)
        self.assertIn("stopped after scanning 2 videos", err)

    @unittest.skipUnless(shutil.which("yt-dlp") and shutil.which("ffmpeg"),
                         "main() refuses to start without yt-dlp and ffmpeg")
    def test_runs_that_end_early_still_write_a_summary(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for links, skip, excluded in (("https://youtu.be/abcdefghijk\n", "abcdefghijk\n", 1),
                                      ("# only a comment\n", None, 0)):
            with tempfile.TemporaryDirectory() as folder:
                with open(os.path.join(folder, "links.txt"), "w") as f:
                    f.write(links)
                if skip:
                    with open(os.path.join(folder, "skip.txt"), "w") as f:
                        f.write(skip)
                cookies = os.path.join(folder, "elsewhere-cookies.txt")
                script = ("import sys, downloader as d\n"
                          f"sys.argv = ['downloader.py', {folder!r}, '--cookies', {cookies!r}]\n"
                          "d.main()\nprint('COOKIES', d.COOKIES_FILE)\n")
                out = subprocess.run([sys.executable, "-c", script], cwd=here,
                                     capture_output=True, text=True, timeout=60)
                self.assertIn(f"COOKIES {cookies}", out.stdout, out.stderr)
                with open(os.path.join(folder, "download_log.json")) as f:
                    run = json.load(f)["run"]
                self.assertEqual((run["downloaded"], run["excluded_by_skip_list"]), (0, excluded))

    def test_no_format_in_the_height_range_is_not_retried_for_minutes(self):
        self._no_sleep()
        saved = d.po_token_first[0]
        d.po_token_first[0] = False
        calls = []
        try:
            def no_format(flags, fast=True):
                calls.append(tuple(flags))
                return "[youtube] abc: Requested format is not available. Use --list-formats"
            routes = len(d.download_routes())
            err = d.run_with_retries(no_format)
        finally:
            d.po_token_first[0] = saved
        self.assertIn("Requested format is not available", err)
        self.assertEqual(len(calls), routes, "one round over every route, not three")

    def test_formats_missing_on_one_route_only_still_get_every_attempt(self):
        # The default client can hide formats the PO-token client sees, so this is
        # not "the video has none" - it keeps the full retry schedule.
        self._no_sleep()
        saved = d.po_token_first[0]
        d.po_token_first[0] = False
        calls = []
        try:
            def mixed(flags, fast=True):
                calls.append(tuple(flags))
                return ("Requested format is not available" if not flags
                        else "HTTP Error 403: Forbidden")
            routes = len(d.download_routes())
            d.run_with_retries(mixed)
        finally:
            d.po_token_first[0] = saved
        self.assertEqual(len(calls), d.MAX_ATTEMPTS * routes)

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


    # ------------------------------------------------- channel links & skips
    def listing(self, *pages):
        """Install a channel_page() serving prepared pages; return the ranges asked."""
        asked = []

        def page(url, start, end):
            asked.append((start, end))
            i = (start - 1) // d.CHANNEL_PAGE
            return ({"channel": "Test Channel",
                     "entries": list(pages[i]) if i < len(pages) else []}, None)

        original = d.channel_page
        d.channel_page = page
        self.addCleanup(setattr, d, "channel_page", original)
        return asked

    @staticmethod
    def entry(vid, views=5000, **extra):
        e = {"id": vid, "title": f"video {vid}", "view_count": views}
        e.update(extra)
        return e

    def test_channel_links_are_recognised(self):
        for url in ("https://www.youtube.com/@SomeChannel",
                    "https://www.youtube.com/@SomeChannel/videos",
                    "https://youtube.com/c/SomeChannel",
                    "https://www.youtube.com/channel/UCBJycsmduvYEL83R_U4JriQ",
                    "https://www.youtube.com/user/SomeChannel"):
            self.assertTrue(d.is_channel_url(url), url)

    def test_video_links_are_never_treated_as_channels(self):
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "https://youtu.be/dQw4w9WgXcQ",
                    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
                    "https://www.youtube.com/@SomeChannel/live/dQw4w9WgXcQ"):
            self.assertFalse(d.is_channel_url(url), url)

    def test_channel_link_is_read_from_its_videos_tab(self):
        # A bare channel link resolves to the list of TABS, not to any videos.
        self.assertEqual(d.channel_videos_url("https://www.youtube.com/@X"),
                         "https://www.youtube.com/@X/videos")
        self.assertEqual(d.channel_videos_url("https://www.youtube.com/@X/"),
                         "https://www.youtube.com/@X/videos")
        self.assertEqual(d.channel_videos_url("https://www.youtube.com/@X/featured"),
                         "https://www.youtube.com/@X/videos")
        self.assertEqual(d.channel_videos_url("https://www.youtube.com/@X/videos?x=1"),
                         "https://www.youtube.com/@X/videos")

    def test_an_explicitly_asked_for_tab_is_kept(self):
        self.assertEqual(d.channel_videos_url("https://www.youtube.com/@X/shorts"),
                         "https://www.youtube.com/@X/shorts")

    def test_view_floor_drops_the_quiet_videos(self):
        d.MIN_VIEWS = 1000
        self.listing([self.entry("aaaaaaaaaaa", 5000),
                      self.entry("bbbbbbbbbbb", 999),
                      self.entry("ccccccccccc", 1000)])
        links, name, err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertIsNone(err)
        self.assertEqual(name, "Test Channel")
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                 "https://www.youtube.com/watch?v=ccccccccccc"])

    def test_no_view_floor_takes_every_video(self):
        self.listing([self.entry("aaaaaaaaaaa", 1), self.entry("bbbbbbbbbbb", 0)])
        links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual(len(links), 2)

    def test_limit_keeps_only_the_newest_matches(self):
        d.MIN_VIEWS, d.LIMIT = 1000, 2
        self.listing([self.entry("aaaaaaaaaaa", 5000),
                      self.entry("bbbbbbbbbbb", 10),      # under the floor
                      self.entry("ccccccccccc", 2000),
                      self.entry("ddddddddddd", 9000)])   # never reached
        links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                 "https://www.youtube.com/watch?v=ccccccccccc"])

    def test_a_skipped_video_does_not_use_up_a_limit_slot(self):
        d.LIMIT = 2
        d.SKIP_IDS = {"aaaaaaaaaaa"}
        self.listing([self.entry("aaaaaaaaaaa"), self.entry("bbbbbbbbbbb"),
                      self.entry("ccccccccccc")])
        links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=bbbbbbbbbbb",
                                 "https://www.youtube.com/watch?v=ccccccccccc"])

    def test_the_summary_counts_videos_skip_txt_kept_out_of_a_listing(self):
        # skip.txt is applied inside the listing (before --limit), so those
        # videos never reach drop_skipped and must be counted where they happen.
        d.LIMIT = 1
        d.SKIP_IDS = {"aaaaaaaaaaa", "bbbbbbbbbbb"}
        self.listing([self.entry("aaaaaaaaaaa"), self.entry("bbbbbbbbbbb"),
                      self.entry("ccccccccccc")])
        links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=ccccccccccc"])
        self.assertEqual(d.skipped_by_list[0], 2)

    def test_live_and_upcoming_videos_are_left_out(self):
        self.listing([self.entry("aaaaaaaaaaa", live_status="is_upcoming"),
                      self.entry("bbbbbbbbbbb", live_status="is_live"),
                      self.entry("ccccccccccc")])
        links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=ccccccccccc"])

    def test_a_video_with_no_view_count_cannot_clear_the_floor(self):
        d.MIN_VIEWS = 1000
        self.listing([self.entry("aaaaaaaaaaa", None), self.entry("bbbbbbbbbbb", 2000)])
        with contextlib.redirect_stdout(io.StringIO()) as out:
            links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=bbbbbbbbbbb"])
        self.assertIn("no view count", out.getvalue())   # never dropped silently

    def test_listing_pages_until_the_channel_runs_out(self):
        d.CHANNEL_PAGE = 2
        asked = self.listing([self.entry("aaaaaaaaaaa"), self.entry("bbbbbbbbbbb")],
                             [self.entry("ccccccccccc")])
        links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual(len(links), 3)
        self.assertEqual(asked, [(1, 2), (3, 4)])        # stopped on a short page

    def test_a_small_limit_costs_one_listing_request(self):
        d.CHANNEL_PAGE, d.LIMIT = 2, 1
        asked = self.listing([self.entry("aaaaaaaaaaa"), self.entry("bbbbbbbbbbb")],
                             [self.entry("ccccccccccc")])
        links, _name, _err = d.list_channel_videos("https://www.youtube.com/@X")
        self.assertEqual(len(links), 1)
        self.assertEqual(asked, [(1, 2)])                # the second page is never read

    def test_a_channel_that_cannot_be_listed_is_reported(self):
        original = d.channel_page
        d.channel_page = lambda url, start, end: ({}, "Channel not found")
        self.addCleanup(setattr, d, "channel_page", original)
        links, _name, err = d.list_channel_videos("https://www.youtube.com/@Nope")
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], [])
        self.assertEqual(err, "Channel not found")

    def test_video_links_pass_through_expansion_untouched(self):
        links = d.expand_sources(["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                  "https://youtu.be/aaaaaaaaaaa"])
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                 "https://youtu.be/aaaaaaaaaaa"])

    def test_channels_and_videos_mix_in_one_links_file(self):
        self.listing([self.entry("aaaaaaaaaaa"), self.entry("bbbbbbbbbbb")])
        with contextlib.redirect_stdout(io.StringIO()):
            links = d.expand_sources(["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                      "https://www.youtube.com/@X"])
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                                 "https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                 "https://www.youtube.com/watch?v=bbbbbbbbbbb"])

    def test_a_video_reached_twice_is_downloaded_once(self):
        self.listing([self.entry("dQw4w9WgXcQ"), self.entry("bbbbbbbbbbb")])
        with contextlib.redirect_stdout(io.StringIO()):
            links = d.expand_sources(["https://youtu.be/dQw4w9WgXcQ",
                                      "https://www.youtube.com/@X"])
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://youtu.be/dQw4w9WgXcQ",
                                 "https://www.youtube.com/watch?v=bbbbbbbbbbb"])

    def test_a_listing_that_breaks_partway_keeps_what_it_found(self):
        # Part of a channel is still real; it just must not look like all of it.
        d.CHANNEL_PAGE = 2
        pages = [({"channel": "Test Channel",
                   "entries": [self.entry("aaaaaaaaaaa"), self.entry("bbbbbbbbbbb")]}, None),
                 ({}, "Unable to download API page: HTTP Error 500")]
        calls = [0]

        def page(url, start, end):
            calls[0] += 1
            return pages[min(calls[0] - 1, 1)]

        original = d.channel_page
        d.channel_page = page
        self.addCleanup(setattr, d, "channel_page", original)

        with contextlib.redirect_stdout(io.StringIO()) as out:
            links = d.expand_sources(["https://www.youtube.com/@X"])
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                 "https://www.youtube.com/watch?v=bbbbbbbbbbb"])
        self.assertIn("stopped early", out.getvalue())

    def test_the_same_video_listed_twice_is_downloaded_once(self):
        # links.txt routinely repeats a video with a different tracking suffix.
        links = d.expand_sources([
            "https://www.youtube.com/watch?v=8e6xIpf7qpk&pp=ygULa2FzaA%3D%3D",
            "https://www.youtube.com/watch?v=8e6xIpf7qpk",
            "https://youtu.be/8e6xIpf7qpk?t=12",
        ])
        self.assertEqual(
            [u[0] if isinstance(u, tuple) else u for u in links], ["https://www.youtube.com/watch?v=8e6xIpf7qpk&pp=ygULa2FzaA%3D%3D"])

    def test_a_link_that_is_not_a_youtube_video_is_left_alone(self):
        links = d.expand_sources(["https://example.com/whatever"])
        self.assertEqual([u[0] if isinstance(u, tuple) else u for u in links], ["https://example.com/whatever"])

    def test_skip_list_reads_links_comments_and_bare_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "skip.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("# already made these\n"
                        "\n"
                        "https://www.youtube.com/watch?v=aaaaaaaaaaa\n"
                        "https://youtu.be/bbbbbbbbbbb?t=30\n"
                        "ccccccccccc\n"
                        "not a link at all\n")
            self.assertEqual(d.read_skips(path),
                             {"aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"})

    def test_a_missing_skip_file_excludes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(d.read_skips(os.path.join(tmp, "skip.txt")), set())

    def test_skipped_video_links_are_dropped(self):
        d.SKIP_IDS = {"aaaaaaaaaaa"}
        kept, gone = d.drop_skipped(["https://www.youtube.com/watch?v=aaaaaaaaaaa",
                                     "https://youtu.be/bbbbbbbbbbb"])
        self.assertEqual(kept, ["https://youtu.be/bbbbbbbbbbb"])
        self.assertEqual(gone, 1)

    def test_the_view_and_limit_flags_are_spelled_either_way(self):
        for flag in ("--views", "--view", "--min-views"):
            args = d.parse_args(["/tmp/x", flag, "1000", "--limit", "3"])
            self.assertEqual((args.views, args.limit), (1000, 3))

    def test_dry_run_and_yes_flags_are_available(self):
        dry = d.parse_args(["/tmp/x", "--dry-run"])
        yes = d.parse_args(["/tmp/x", "--yes"])
        self.assertTrue(dry.dry_run)
        self.assertFalse(dry.yes)
        self.assertTrue(yes.yes)
        self.assertFalse(yes.dry_run)

    def test_plan_counts_finished_videos_without_removing_them(self):
        saved = d.DONE_INDEX
        d.DONE_INDEX = {"aaaaaaaaaaa": "/tmp/already"}
        try:
            links = [("https://www.youtube.com/watch?v=aaaaaaaaaaa", None),
                     ("https://www.youtube.com/watch?v=bbbbbbbbbbb", None)]
            pending, already = d.plan_links(links)
        finally:
            d.DONE_INDEX = saved
        self.assertEqual(already, 1)
        self.assertEqual(pending, [links[1]])

    def test_plan_prints_every_eligible_link(self):
        links = [(f"https://www.youtube.com/watch?v={chr(97 + i) * 11}", None)
                 for i in range(12)]
        with contextlib.redirect_stdout(io.StringIO()) as out:
            d.print_download_plan(1, links, links, 0, 0)
        text = out.getvalue()
        self.assertNotIn("and 2 more", text)
        for i in range(12):
            self.assertIn(chr(97 + i) * 11, text)

    def test_dry_run_never_confirms_and_yes_confirms(self):
        self.assertIsNone(d.choose_download_links(
            d.parse_args(["/tmp/x", "--dry-run"]), [], []))
        self.assertEqual(d.choose_download_links(
            d.parse_args(["/tmp/x", "--yes"]), ["one"], []), ["one"])



    # ------------------------------------------------------------ --links
    def test_links_default_to_the_folder_and_can_come_from_a_file(self):
        self.assertIsNone(d.parse_args(["/tmp/x"]).links)
        self.assertEqual(d.parse_args(["/tmp/x", "--links", "/tmp/l.txt"]).links, "/tmp/l.txt")

    @unittest.skipUnless(shutil.which("yt-dlp") and shutil.which("ffmpeg"),
                         "main() refuses to start without yt-dlp and ffmpeg")
    def test_a_links_file_is_used_and_the_folders_links_txt_left_alone(self):
        # The API passes each job's links this way, so a hand-kept links.txt in
        # the download folder is never overwritten.
        here = os.path.dirname(os.path.abspath(__file__))
        with tempfile.TemporaryDirectory() as folder, tempfile.TemporaryDirectory() as elsewhere:
            with open(os.path.join(folder, "links.txt"), "w") as f:
                f.write("https://www.youtube.com/watch?v=handkept123\n")
            job_links = os.path.join(elsewhere, "job.txt")
            with open(job_links, "w") as f:
                f.write("https://www.youtube.com/watch?v=fromfile123\n")
            script = ("import sys, downloader as d\n"
                      "seen = []\n"
                      "d.download_one = lambda url, i, n: (seen.append(url), 'ok')[1]\n"
                      f"sys.argv = ['downloader.py', {folder!r}, '--links', {job_links!r}]\n"
                      "d.main()\n"
                      "print('SEEN', seen)\n")
            out = subprocess.run([sys.executable, "-c", script], cwd=here,
                                 capture_output=True, text=True, timeout=60)
            self.assertIn("SEEN ['https://www.youtube.com/watch?v=fromfile123']",
                          out.stdout, out.stderr)
            with open(os.path.join(folder, "links.txt")) as f:
                self.assertEqual(f.read(), "https://www.youtube.com/watch?v=handkept123\n")

    # --------------------------------------------------------- record stream
    def test_each_record_is_streamed_the_moment_it_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "download_log.jsonl")
            saved = (d.LOG_JSONL[0], list(d.LOG_RECORDS))
            d.LOG_JSONL[0] = path
            try:
                d.log_record(url="u1", status="ok", title="\u00dcn\u00efcode \u2713")
                with open(path, encoding="utf-8") as f:
                    first = f.read()          # before the second record exists
                d.log_record(url="u2", status="skipped")
                with open(path, encoding="utf-8") as f:
                    lines = f.read().splitlines()
            finally:
                d.LOG_JSONL[0] = saved[0]
                d.LOG_RECORDS[:] = saved[1]
        self.assertEqual(json.loads(first)["title"], "\u00dcn\u00efcode \u2713")
        self.assertEqual([json.loads(l)["url"] for l in lines], ["u1", "u2"])

    def test_no_stream_is_written_when_logging_is_not_set_up(self):
        saved = (d.LOG_JSONL[0], list(d.LOG_RECORDS))
        d.LOG_JSONL[0] = None
        try:
            d.log_record(url="u", status="ok")           # must not raise
        finally:
            d.LOG_JSONL[0] = saved[0]
            d.LOG_RECORDS[:] = saved[1]

    def test_a_skipped_video_is_recorded_only_after_its_backfill(self):
        # A reader acts on the streamed line at once, so the folder has to be
        # complete by then - the transcript backfill must come first.
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "Vid")
            os.makedirs(folder)
            with open(os.path.join(folder, "videoinfo.txt"), "w", encoding="utf-8") as f:
                f.write("Title: V\nLink: https://youtu.be/aaaaaaaaaaa\nViews: 1\n"
                        "Subscribers: 1\nUploaded: NA\nCategory: NA\nStatus:  OK\n")
            d.DOWNLOAD_DIR = tmp
            d.SAVE_THUMBNAIL = d.SAVE_DESCRIPTION = False
            before = len(d.LOG_RECORDS)
            records_during_backfill = []
            original = d.download_subs

            def backfill(folder_, url_):
                records_during_backfill.append(len(d.LOG_RECORDS))
                return None, None

            d.download_subs = backfill
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    d.skip_finished(folder, "https://youtu.be/aaaaaaaaaaa")
                after = len(d.LOG_RECORDS)
            finally:
                d.download_subs = original
                del d.LOG_RECORDS[before:]
        self.assertEqual(records_during_backfill, [before])   # nothing yet
        self.assertEqual(after, before + 1)                    # then exactly one

    # ------------------------------------------------- answers, not files
    def test_scratch_dir_is_always_removed(self):
        with d.scratch_dir() as folder:
            kept = folder
            open(os.path.join(folder, "junk.json"), "w").close()
            self.assertTrue(os.path.isdir(folder))
        self.assertFalse(os.path.exists(kept))

    def test_scratch_dir_is_removed_after_a_failure(self):
        kept = None
        with self.assertRaises(RuntimeError):
            with d.scratch_dir() as folder:
                kept = folder
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(kept))

    def test_details_of_keeps_an_absence_as_none(self):
        # This is data, not a text file: "NA" would be indistinguishable from a
        # video whose likes really are zero.
        got = d.details_of({"id": "aaaaaaaaaaa", "title": "T"})
        self.assertEqual(got["id"], "aaaaaaaaaaa")
        self.assertEqual(got["link"], "https://www.youtube.com/watch?v=aaaaaaaaaaa")
        for key in ("views", "likes", "subscribers", "duration", "upload_epoch",
                    "thumbnail", "description"):
            self.assertIsNone(got[key], key)

    def test_details_of_maps_the_fields_a_lookup_is_for(self):
        got = d.details_of({
            "id": "aaaaaaaaaaa", "webpage_url": "https://youtu.be/aaaaaaaaaaa",
            "title": "Clip", "channel": "Chan", "channel_follower_count": 1234,
            "view_count": 5678, "like_count": 90, "comment_count": 12,
            "duration": 125, "timestamp": 1700000000, "categories": ["News"],
            "tags": ["a", "b"], "description": "line one\nline two",
        })
        self.assertEqual(got["link"], "https://youtu.be/aaaaaaaaaaa")
        self.assertEqual((got["channel"], got["subscribers"]), ("Chan", 1234))
        self.assertEqual((got["views"], got["likes"], got["comment_count"]),
                         (5678, 90, 12))
        self.assertEqual(got["duration_text"], d.human_time(125))
        self.assertEqual(got["category"], "News")
        self.assertEqual(got["tags"], ["a", "b"])
        self.assertIn("line two", got["description"])     # line breaks survive
        self.assertTrue(got["uploaded"])                   # converted to PKT

    def test_caption_tracks_lists_only_the_wanted_language(self):
        info = {"automatic_captions": {"en": [{"ext": "json3"}], "fr": [{}]},
                "subtitles": {"en-GB": [{}], "de": [{}]}}
        self.assertEqual(d.caption_tracks(info), {"auto": ["en"], "manual": ["en-GB"]})

    def test_the_transcript_text_is_the_same_text_the_file_gets(self):
        # One source of truth: the API's "text" and trans_*.txt cannot drift.
        entries = [(0.0, 0.4, "hello"), (0.5, 0.9, "there")]
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "t.txt")
            d.write_trans_txt(path, entries)
            self.assertEqual(open(path, encoding="utf-8").read(),
                             "\n".join(d.trans_lines(entries)) + "\n")
            path = os.path.join(folder, "w.txt")
            d.write_words_txt(path, entries)
            self.assertEqual(open(path, encoding="utf-8").read(),
                             "\n".join(d.words_lines(entries)) + "\n")

    def test_video_transcript_returns_text_and_leaves_no_files(self):
        entries = [(0.0, 0.4, "hello"), (0.5, 0.9, "there")]
        made = []
        saved = (d.fetch_info, d.best_caption_entries)
        d.fetch_info = lambda url, *a, **k: ({"id": "aaaaaaaaaaa", "title": "Clip",
                                              "webpage_url": url}, None)

        def entries_for(folder, url, info):
            made.append(folder)
            return entries

        d.best_caption_entries = entries_for
        try:
            got, err = d.video_transcript("https://youtu.be/aaaaaaaaaaa")
        finally:
            d.fetch_info, d.best_caption_entries = saved
        self.assertIsNone(err)
        self.assertTrue(got["word_level"])
        self.assertEqual(got["segments"], 2)
        self.assertIn("hello", got["text"])
        self.assertIn("hello", got["words"])
        self.assertIsNone(got["note"])
        self.assertEqual([f for f in made if os.path.exists(f)], [],
                         "the scratch folder must not survive the call")

    def test_video_transcript_says_why_there_are_no_word_timings(self):
        phrases = [(0.0, 2.0, "a whole phrase at once"), (2.0, 4.0, "and another one")]
        saved = (d.fetch_info, d.best_caption_entries)
        d.fetch_info = lambda url, *a, **k: ({"id": "a" * 11}, None)
        d.best_caption_entries = lambda folder, url, info: phrases
        try:
            got, err = d.video_transcript("https://youtu.be/aaaaaaaaaaa")
        finally:
            d.fetch_info, d.best_caption_entries = saved
        self.assertIsNone(err)
        self.assertFalse(got["word_level"])
        self.assertIsNone(got["words"])
        self.assertIn("auto-generated", got["note"])
        self.assertIn("whole phrase", got["text"])

    def test_video_transcript_reports_a_video_it_cannot_read(self):
        saved = d.fetch_info
        d.fetch_info = lambda url, *a, **k: (None, "Video unavailable")
        try:
            got, err = d.video_transcript("https://youtu.be/aaaaaaaaaaa")
        finally:
            d.fetch_info = saved
        self.assertIsNone(got)
        self.assertEqual(err, "Video unavailable")

    def test_video_comments_threads_the_answer_and_leaves_no_files(self):
        flat = [self._comment("1", parent="root", text="top"),
                self._comment("1.1", parent="1", text="reply"),
                self._comment("2", parent="root", text="other")]
        made = []
        saved = (d.fetch_info, d.fetch_comments)
        d.fetch_info = lambda url, *a, **k: ({"id": "a" * 11, "title": "Clip",
                                              "comment_count": 4321}, None)

        def comments_for(folder, url):
            made.append(folder)
            return flat

        d.fetch_comments = comments_for
        d.COMMENT_SORT, d.COMMENT_REPLIES = "new", True
        try:
            got, err = d.video_comments("https://youtu.be/aaaaaaaaaaa")
        finally:
            d.fetch_info, d.fetch_comments = saved
        self.assertIsNone(err)
        self.assertEqual((got["fetched"], got["top_level"], got["replies"]), (3, 2, 1))
        self.assertEqual(got["total_on_video"], 4321)
        self.assertEqual((got["sort"], got["replies_included"]), ("new", True))
        self.assertEqual([t["text"] for t in got["threads"]], ["top", "other"])
        self.assertEqual([r["text"] for r in got["threads"][0]["replies"]], ["reply"])
        self.assertEqual([f for f in made if os.path.exists(f)], [])

    def test_comment_data_keeps_a_comments_own_paragraphs(self):
        got = d.comment_data(self._comment("1", text="one\n\ntwo\x07three"))
        self.assertIn("one\n\ntwo", got["text"])     # line breaks are the comment's
        self.assertNotIn("\x07", got["text"])         # control characters are not

    def test_safe_text_and_clean_text_differ_only_in_line_breaks(self):
        self.assertEqual(d.safe_text("a\x00b\nc"), "ab\nc")
        self.assertEqual(d.clean_text("a\x00b\nc"), "a b c")
        self.assertEqual(d.clean_text(None), "")

    # ------------------------------------------- regressions: videoinfo.txt
    def _info_file(self, folder, *lines):
        path = os.path.join(folder, "videoinfo.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return path

    def test_a_transcript_run_keeps_a_finished_downloads_thumbnail_record(self):
        # --transcript forces SAVE_THUMBNAIL off. Stripping a line it then
        # declines to rewrite deleted the record of a thumbnail really on disk.
        with tempfile.TemporaryDirectory() as folder:
            path = self._info_file(folder, "Title:      Clip",
                                   "Thumbnail:  Clip.jpg",
                                   "Description: Clip.txt")
            d.SAVE_THUMBNAIL = False
            d.SAVE_DESCRIPTION = True
            d.update_info_extras(folder, None, os.path.join(folder, "Clip.txt"))
            body = open(path, encoding="utf-8").read()
        self.assertIn("Thumbnail:  Clip.jpg", body)
        self.assertIn("Description: Clip.txt", body)

    def test_update_info_extras_writes_nothing_when_both_are_off(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._info_file(folder, "Thumbnail:  Clip.jpg")
            d.SAVE_THUMBNAIL = d.SAVE_DESCRIPTION = False
            d.update_info_extras(folder, None, None)
            self.assertEqual(open(path, encoding="utf-8").read(),
                             "Thumbnail:  Clip.jpg\n")

    def test_comments_off_leaves_a_real_comments_record_alone(self):
        # A --transcript run over a folder downloaded with --extract-comments
        # replaced its true record with "Comments: none".
        with tempfile.TemporaryDirectory() as folder:
            path = self._info_file(folder, "Title:    Clip",
                                   "Comments: comments_Clip.txt (100 fetched)")
            d.update_info_comments(folder, None, 0, "off")
            self.assertIn("comments_Clip.txt (100 fetched)",
                          open(path, encoding="utf-8").read())

    def test_a_rerun_keeps_a_comment_count_it_cannot_recount(self):
        # Finding the file already there fetches nothing, so the count is 0; the
        # count the original fetch recorded is the true one.
        with tempfile.TemporaryDirectory() as folder:
            path = self._info_file(folder, "Comments: comments_Clip.txt (100 fetched)")
            d.update_info_comments(folder, os.path.join(folder, "comments_Clip.txt"),
                                   0, "saved")
            self.assertIn("comments_Clip.txt (100 fetched)",
                          open(path, encoding="utf-8").read())

    def test_a_fresh_fetch_replaces_the_comments_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._info_file(folder, "Comments: comments_Clip.txt (100 fetched)")
            d.update_info_comments(folder, os.path.join(folder, "comments_Clip.txt"),
                                   250, "saved")
            body = open(path, encoding="utf-8").read()
        self.assertIn("(250 fetched)", body)
        self.assertNotIn("(100 fetched)", body)

    def test_a_failed_fetch_is_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self._info_file(folder, "Comments: none")
            d.update_info_comments(folder, None, 0, "failed")
            self.assertIn("Comments: failed", open(path, encoding="utf-8").read())

    # ------------------------------------------- regressions: comment fetching
    def _fetch_comments(self, outcomes, rejected_on=()):
        """Drive fetch_comments with a scripted outcome per attempt.

        Each outcome is a list of comments to "find", or the string "timeout".
        rejected_on names the attempt numbers whose stderr looks like YouTube
        rejecting cookies.txt.
        """
        calls = []
        saved = (d.subprocess.run, d.take_comments, d.drop_rejected_cookies,
                 d.po_token_first[0])
        d.po_token_first[0] = False          # two routes, so escalation is visible
        state = {"n": 0}

        def run(cmd, **kw):
            state["n"] += 1
            calls.append(state["n"])
            if outcomes[state["n"] - 1] == "timeout":
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        d.subprocess.run = run
        d.take_comments = lambda folder: (
            [] if state["n"] == 0 or outcomes[state["n"] - 1] == "timeout"
            else list(outcomes[state["n"] - 1]))
        d.drop_rejected_cookies = lambda text: state["n"] in rejected_on
        try:
            with tempfile.TemporaryDirectory() as folder:
                with contextlib.redirect_stdout(io.StringIO()):
                    got = d.fetch_comments(folder, "https://youtu.be/aaaaaaaaaaa")
        finally:
            (d.subprocess.run, d.take_comments, d.drop_rejected_cookies,
             d.po_token_first[0]) = saved
        return got, calls

    def test_a_rejected_cookie_file_really_is_retried(self):
        # The retry used to be a no-op: both branches of the test continued to
        # the next route, so the request was never made again without cookies.
        got, calls = self._fetch_comments([[], [self._comment("1")]], rejected_on=(1,))
        self.assertEqual([c["id"] for c in got], ["1"])
        self.assertEqual(calls, [1, 2], "the same route must be tried again")

    def test_a_route_is_not_retried_when_cookies_were_not_the_problem(self):
        got, calls = self._fetch_comments([[], [], [], []])
        self.assertEqual(got, [])
        self.assertEqual(calls, [1, 2], "one attempt per route, two routes")

    def test_a_comment_timeout_moves_on_to_the_next_route(self):
        # An uncaught TimeoutExpired skipped the PO-token route altogether and
        # put the whole command line in the log.
        got, calls = self._fetch_comments(["timeout", [self._comment("1")]])
        self.assertEqual([c["id"] for c in got], ["1"])
        self.assertEqual(calls, [1, 2])

    def test_every_route_timing_out_returns_no_comments(self):
        got, calls = self._fetch_comments(["timeout", "timeout"])
        self.assertEqual(got, [])
        self.assertEqual(calls, [1, 2])

    def test_a_control_character_cannot_rearrange_the_comments_file(self):
        with tempfile.TemporaryDirectory() as base:
            folder = os.path.join(base, "Clip")
            os.makedirs(folder)
            path = d.write_comments_txt(
                folder, {"title": "T"},
                [self._comment("1", author="ev\nil", text="a\nb\x1b[31m")])
            lines = open(path, encoding="utf-8").read().splitlines()
        self.assertIn("ev il", "\n".join(lines))
        self.assertNotIn("\x1b", "\n".join(lines))
        # The author's newline must not have produced an extra record line.
        self.assertEqual(sum(1 for ln in lines if ln.startswith("[1] ")), 1)

    # ------------------------------------------------ regressions: search
    def _dated(self, stdout, returncode=0):
        """Run search_dated against a scripted yt-dlp, returning (items, cmd)."""
        seen = {}
        saved = d.subprocess.run

        def run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")

        d.subprocess.run = run
        try:
            items, err = d.search_dated("cats", 10, None if "no_window" in stdout
                                        else time.time() - 7 * 86400)
        finally:
            d.subprocess.run = saved
        return items, err, seen["cmd"]

    def test_the_dated_route_reads_live_status(self):
        # The live/upcoming rule read a key this route never produced, so every
        # premiere and in-progress stream sailed through it.
        line = "aaaaaaaaaaa\t1700000000\t500\tis_live\t900\tChan\tA Title"
        items, err, _cmd = self._dated(line + "\n")
        self.assertIsNone(err)
        self.assertEqual(items[0]["live_status"], "is_live")

    def test_the_dated_route_reads_na_live_status_as_unknown(self):
        line = "aaaaaaaaaaa\t1700000000\t500\tNA\t900\tChan\tA Title"
        items, _err, _cmd = self._dated(line + "\n")
        self.assertIsNone(items[0]["live_status"])

    def test_a_tab_inside_a_title_does_not_truncate_it(self):
        line = "aaaaaaaaaaa\t1700000000\t500\tnot_live\t900\tChan\tPart A\tPart B"
        items, _err, _cmd = self._dated(line + "\n")
        self.assertEqual(items[0]["title"], "Part A\tPart B")

    def test_the_dated_route_asks_for_a_window_only_when_there_is_one(self):
        _items, _err, cmd = self._dated("")
        self.assertIn("--dateafter", cmd)
        # "no_window" makes the helper pass since=None: a date sort with no
        # window needs this route for its timestamps, not for a filter.
        _items, _err, cmd = self._dated("no_window")
        self.assertNotIn("--dateafter", cmd)

    def test_sorting_by_date_no_longer_invents_a_window(self):
        # It used to apply a silent 365-day floor, which put the oldest videos
        # on a title out of reach of the sort meant to find them.
        for sort in ("date", "oldest"):
            d.SORT_BY = sort
            self.assertEqual(d.search_window(), (None, ""), sort)

    def _search_items(self, items, dated=True):
        """search_items over a scripted route, returning (kept, found, excluded)."""
        saved = (d.search_dated, d.search_flat)
        d.search_dated = lambda q, c, since=None: (list(items), None)
        d.search_flat = lambda q, c: ([dict(i, view_count=i.get("views")) for i in items], None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                kept, err = d.search_items("cats")
        finally:
            d.search_dated, d.search_flat = saved
        return kept, err, d.SEARCH_FOUND[0], d.skipped_by_list[0]

    def test_a_dated_search_keeps_a_result_with_no_timestamp(self):
        # --dateafter already accepted it; dropping it over a field YouTube
        # declined to print silently shrank every dated search.
        d.DAYS = 7
        now = time.time()
        kept, err, _found, _skipped = self._search_items([
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "A",
             "views": 5, "epoch": int(now - 3600)},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "B",
             "views": 5, "epoch": None},
        ])
        self.assertIsNone(err)
        self.assertEqual([i["id"] for i in kept], ["a" * 11, "b" * 11])

    def test_a_dated_search_still_drops_what_is_outside_the_window(self):
        d.DAYS = 7
        kept, _err, _found, _skipped = self._search_items([
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "A",
             "views": 5, "epoch": int(time.time() - 3600)},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "B",
             "views": 5, "epoch": int(time.time() - 90 * 86400)},
        ])
        self.assertEqual([i["id"] for i in kept], ["a" * 11])

    def test_a_search_counts_what_the_skip_list_kept_out(self):
        d.SKIP_IDS = {"b" * 11}
        kept, _err, found, skipped = self._search_items([
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "A", "views": 5},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "B", "views": 5},
        ], dated=False)
        self.assertEqual([i["id"] for i in kept], ["a" * 11])
        self.assertEqual((found, skipped), (2, 1))

    def test_a_search_records_what_it_scanned_not_only_what_it_kept(self):
        d.MIN_VIEWS = 100
        kept, _err, found, _skipped = self._search_items([
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "A", "views": 500},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "B", "views": 5},
        ], dated=False)
        self.assertEqual(len(kept), 1)
        self.assertEqual(found, 2, "found must be what the search returned")

    def test_a_live_stream_never_reaches_a_dated_searchs_results(self):
        d.DAYS = 7
        now = int(time.time() - 3600)
        kept, _err, _found, _skipped = self._search_items([
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "A",
             "views": 5, "epoch": now, "live_status": "not_live"},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "B",
             "views": 5, "epoch": now, "live_status": "is_live"},
            {"id": "c" * 11, "url": d.watch_url("c" * 11), "title": "C",
             "views": 5, "epoch": now, "live_status": "is_upcoming"},
        ])
        self.assertEqual([i["id"] for i in kept], ["a" * 11])

    def test_a_newline_cannot_inject_a_link_into_the_links_file(self):
        # "# <title>" ended early and what followed stood on a line of its own,
        # which the links reader treats as a video to download.
        items = [{"id": "a" * 11, "url": d.watch_url("a" * 11),
                  "title": "Real\nhttps://evil.example/pwned"}]
        with tempfile.TemporaryDirectory() as base:
            path = d.write_search_links(os.path.join(base, "search_links.txt"),
                                        "cats\nhttps://evil.example/query",
                                        items, "last 7 days\nhttps://evil.example/w")
            self.assertEqual(d.read_links(path), [d.watch_url("a" * 11)])
            self.assertNotIn("evil.example", "\n".join(d.read_links(path)))

    def test_only_a_real_video_id_becomes_a_link(self):
        items = [{"id": "a" * 11, "url": "x", "title": "Good"},
                 {"id": "../../etc", "url": "x", "title": "Bad"},
                 {"id": "", "url": "x", "title": "Empty"}]
        with tempfile.TemporaryDirectory() as base:
            path = d.write_search_links(os.path.join(base, "search_links.txt"),
                                        "cats", items, "")
            self.assertEqual(d.read_links(path), [d.watch_url("a" * 11)])

    def test_a_search_that_matches_nothing_clears_the_last_ones_answers(self):
        with tempfile.TemporaryDirectory() as base:
            saved = d.DOWNLOAD_DIR
            d.DOWNLOAD_DIR = base
            try:
                for name in ("search_links.txt", "search_results.json"):
                    with open(os.path.join(base, name), "w") as f:
                        f.write("stale")
                d.clear_search_answers()
                self.assertEqual(os.listdir(base), [])
                d.clear_search_answers()          # absent files are not an error
            finally:
                d.DOWNLOAD_DIR = saved

    # --------------------------------------- regressions: the flags themselves
    def test_a_negative_count_is_refused(self):
        for flag in ("--comment-limit", "--search-count", "--views", "--limit",
                     "--days"):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit, msg=f"{flag} -1 should be refused"):
                    d.parse_args(["/tmp", flag, "-1"])

    def test_a_zero_count_is_still_accepted(self):
        self.assertEqual(d.parse_args(["/tmp", "--comment-limit", "0"]).comment_limit, 0)

    def test_an_empty_query_is_refused(self):
        for value in ("", "   "):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    d.parse_args(["/tmp", f"--search={value}"])

    def test_this_month_without_a_search_is_refused(self):
        # It is a search window and nothing else reads it, so the run used to
        # accept it and then download every video in links.txt regardless.
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                d.parse_args(["/tmp", "--this-month"])
        self.assertTrue(d.parse_args(["/tmp", "--search", "x", "--this-month"]).this_month)

    # ------------------------------------------- regressions: the summaries
    def _summary(self, writer):
        with tempfile.TemporaryDirectory() as base:
            saved = (d.LOG_JSON[0], d.DOWNLOAD_DIR)
            d.LOG_JSON[0] = os.path.join(base, "download_log.json")
            d.DOWNLOAD_DIR = base
            try:
                writer()
                with open(d.LOG_JSON[0], encoding="utf-8") as f:
                    return json.load(f)["run"]
            finally:
                d.LOG_JSON[0], d.DOWNLOAD_DIR = saved

    def test_a_failed_search_is_not_reported_as_a_clean_empty_run(self):
        d.SEARCH_QUERY = "cats"
        d.log_record(url="ytsearch:cats", status="failed", error="Search failed")
        summary = self._summary(d.write_empty_summary)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["search_query"], "cats")

    def test_every_summary_writer_says_what_kind_of_run_it_was(self):
        d.TRANSCRIPT_ONLY = True
        d.SEARCH_QUERY = "cats"
        d.SORT_BY = "views"
        keys = ("mode", "transcripts", "comments", "search_query", "search_sort",
                "search_in_channel")
        for writer in (d.write_empty_summary,
                       lambda: d.write_plan_summary(1, 1, 1, 0)):
            summary = self._summary(writer)
            for key in keys:
                self.assertIn(key, summary, f"{writer} should write {key}")
            self.assertEqual(summary["mode"], "transcript")
            self.assertEqual(summary["search_sort"], "views")

    # ----------------------------------------- long videos only, no Shorts
    def test_the_length_floor_is_on_by_default(self):
        # Asserted on what a real run gets, not on the module global: setUp
        # turns the floor off so the other tests need no durations.
        self.assertEqual(d.SHORTS_SECONDS, 180, "a Short is at most 3 minutes")
        args = d.parse_args(["/tmp"])
        self.assertEqual(args.min_duration, 180)
        self.assertFalse(args.shorts)

    def test_the_floor_drops_a_short_and_keeps_a_long_video(self):
        d.MIN_DURATION = 180
        self.assertEqual(d.wanted_entry({"duration": 45})[1], "short")
        self.assertEqual(d.wanted_entry({"duration": 179})[1], "short")
        self.assertTrue(d.wanted_entry({"duration": 180})[0])
        self.assertTrue(d.wanted_entry({"duration": 1277})[0])

    def test_an_unknown_length_cannot_be_shown_to_be_a_long_video(self):
        # A Shorts listing reports no duration at all, so "no length" is the
        # one signal that a Shorts tab gives - and it is kept apart from
        # "short", which was actually measured.
        d.MIN_DURATION = 180
        self.assertEqual(d.wanted_entry({"duration": None})[1], "nolength")
        self.assertEqual(d.wanted_entry({})[1], "nolength")
        self.assertEqual(d.wanted_entry({"duration": "NA"})[1], "nolength")

    def test_asking_for_shorts_turns_the_floor_off(self):
        d.MIN_DURATION = 180
        d.INCLUDE_SHORTS = True
        self.assertEqual(d.duration_floor(), 0)
        for entry in ({"duration": 5}, {"duration": None}, {}):
            self.assertTrue(d.wanted_entry(entry)[0], entry)

    def test_a_zero_min_duration_also_turns_it_off(self):
        d.MIN_DURATION = 0
        self.assertEqual(d.duration_floor(), 0)
        self.assertTrue(d.wanted_entry({"duration": 5})[0])

    def test_a_shorts_tab_link_is_a_request_for_shorts(self):
        # Applying the floor there would drop every video on the tab, since a
        # Shorts listing reports no duration.
        for url in ("https://www.youtube.com/@H/shorts",
                    "https://www.youtube.com/@H/shorts/",
                    "https://www.youtube.com/@H/shorts?x=1"):
            self.assertTrue(d.is_shorts_tab(url), url)
        for url in ("https://www.youtube.com/@H/videos",
                    "https://www.youtube.com/@H",
                    "https://www.youtube.com/watch?v=aaaaaaaaaaa"):
            self.assertFalse(d.is_shorts_tab(url), url)
        d.MIN_DURATION = 180
        self.assertTrue(d.wanted_entry({"duration": None}, allow_short=True)[0])
        self.assertTrue(d.wanted_entry({"duration": 20}, allow_short=True)[0])

    def test_the_floor_does_not_override_the_live_rule(self):
        d.MIN_DURATION = 180
        self.assertEqual(d.wanted_entry({"duration": 9999,
                                         "live_status": "is_live"})[1], "live")

    def test_the_view_floor_still_applies_above_the_length_floor(self):
        d.MIN_DURATION = 180
        d.MIN_VIEWS = 1000
        self.assertEqual(d.wanted_entry({"duration": 900, "view_count": 10})[1], "views")
        self.assertTrue(d.wanted_entry({"duration": 900, "view_count": 5000})[0])

    def test_a_search_drops_shorts_on_the_flat_route(self):
        d.MIN_DURATION = 180
        kept, _err, found, _skipped = self._search_items([
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "Long",
             "views": 500, "duration": 1200},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "Short",
             "views": 900000, "duration": 30},
            {"id": "c" * 11, "url": d.watch_url("c" * 11), "title": "No length",
             "views": 700, "duration": None},
        ], dated=False)
        # The Short had by far the most views and still must not win.
        self.assertEqual([i["id"] for i in kept], ["a" * 11])
        self.assertEqual(found, 3)

    def test_a_search_drops_shorts_on_the_dated_route_too(self):
        d.MIN_DURATION = 180
        d.DAYS = 7
        now = int(time.time() - 3600)
        kept, _err, _found, _skipped = self._search_items([
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "Long",
             "views": 5, "epoch": now, "duration": 1200},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "Short",
             "views": 5, "epoch": now, "duration": 30},
        ])
        self.assertEqual([i["id"] for i in kept], ["a" * 11])

    def test_the_dated_route_reads_duration(self):
        line = "aaaaaaaaaaa\t1700000000\t500\tnot_live\t1277\tChan\tA Title"
        items, err, _cmd = self._dated(line + "\n")
        self.assertIsNone(err)
        self.assertEqual(items[0]["duration"], 1277)
        line = "aaaaaaaaaaa\t1700000000\t500\tnot_live\tNA\tChan\tA Title"
        items, _err, _cmd = self._dated(line + "\n")
        self.assertIsNone(items[0]["duration"])

    def test_a_search_row_reports_the_length(self):
        row = d.search_row({"id": "a" * 11, "url": "u", "title": "T",
                            "views": 5, "duration": 1277}, 1)
        self.assertEqual(row["duration"], 1277)
        self.assertEqual(row["duration_text"], d.human_time(1277))
        row = d.search_row({"id": "a" * 11, "url": "u", "title": "T"}, 1)
        self.assertIsNone(row["duration"])
        self.assertIsNone(row["duration_text"])

    # -------------------------------- saying what the filters actually did
    def test_a_search_emptied_by_the_floor_names_the_floor(self):
        # "Try a wider window" is useless advice when there is no window and the
        # length floor is what emptied the answer - and the floor is on by default.
        d.MIN_DURATION = 180
        saved = (d.search_flat, d.LOG_JSON[0])
        d.search_flat = lambda q, c: ([
            {"id": "a" * 11, "title": "S1", "view_count": 500, "duration": 30},
            {"id": "b" * 11, "title": "S2", "view_count": 600, "duration": 45},
        ], None)
        buf = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as base:
                d.DOWNLOAD_DIR = base
                args = d.parse_args(["/tmp", "--search", "cats"])
                with contextlib.redirect_stdout(buf):
                    path = d.run_search(args)
        finally:
            d.search_flat, d.LOG_JSON[0] = saved
        shown = buf.getvalue()
        self.assertIsNone(path)
        self.assertIn("none of 2 video(s) matched", shown)
        self.assertIn("2 shorter than", shown)
        self.assertIn("--shorts", shown)
        self.assertNotIn("wider window", shown)

    def test_a_search_that_truly_matched_nothing_still_says_so(self):
        saved = d.search_flat
        d.search_flat = lambda q, c: ([], None)
        buf = io.StringIO()
        try:
            with tempfile.TemporaryDirectory() as base:
                d.DOWNLOAD_DIR = base
                args = d.parse_args(["/tmp", "--search", "cats"])
                with contextlib.redirect_stdout(buf):
                    d.run_search(args)
        finally:
            d.search_flat = saved
        self.assertIn("Nothing matched", buf.getvalue())

    def test_the_search_table_says_how_many_were_scanned(self):
        d.MIN_DURATION = 180
        d.SEARCH_FOUND[0] = 59
        d.SEARCH_COUNTS[0] = {"scanned": 59, "short": 28, "nolength": 1,
                              "views": 0, "live": 0, "skipped": 0, "unknown": 0}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            d.print_search_results([{"id": "a" * 11, "url": "u", "title": "T",
                                     "views": 5, "duration": 900}], "")
        shown = buf.getvalue()
        self.assertIn("1 video(s) of 59 scanned", shown)
        self.assertIn("28 under", shown)
        self.assertIn("1 of unknown length", shown)

    def test_the_search_table_is_quiet_when_nothing_was_filtered(self):
        d.SEARCH_FOUND[0] = 1
        d.SEARCH_COUNTS[0] = {"scanned": 1, "short": 0, "nolength": 0, "views": 0,
                              "live": 0, "skipped": 0, "unknown": 0}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            d.print_search_results([{"id": "a" * 11, "url": "u", "title": "T",
                                     "views": 5, "duration": 900}], "")
        self.assertNotIn("Filtered out", buf.getvalue())
        self.assertNotIn("scanned", buf.getvalue())

    def test_search_items_keeps_the_reason_for_every_drop(self):
        d.MIN_DURATION = 180
        d.MIN_VIEWS = 100
        d.SKIP_IDS = {"e" * 11}
        kept, _err, found, _skipped = self._search_items([
            {"id": "a" * 11, "url": "u", "title": "keep", "views": 500, "duration": 900},
            {"id": "b" * 11, "url": "u", "title": "short", "views": 500, "duration": 30},
            {"id": "c" * 11, "url": "u", "title": "nolen", "views": 500, "duration": None},
            {"id": "d" * 11, "url": "u", "title": "quiet", "views": 5, "duration": 900},
            {"id": "e" * 11, "url": "u", "title": "skip", "views": 500, "duration": 900},
        ], dated=False)
        self.assertEqual([i["id"] for i in kept], ["a" * 11])
        self.assertEqual(found, 5)
        counts = d.SEARCH_COUNTS[0]
        self.assertEqual((counts["short"], counts["nolength"], counts["views"],
                          counts["skipped"]), (1, 1, 1, 1))

    def test_a_shorts_tab_says_the_floor_does_not_apply(self):
        d.MIN_DURATION = 180
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(d.shorts_tab_note("https://www.youtube.com/@H/shorts"))
        self.assertIn("Shorts tab", buf.getvalue())
        self.assertIn("@H/shorts", buf.getvalue())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertFalse(d.shorts_tab_note("https://www.youtube.com/@H/videos"))
        self.assertEqual(buf.getvalue(), "")
        d.INCLUDE_SHORTS = True            # no floor, so nothing to explain
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertTrue(d.shorts_tab_note("https://www.youtube.com/@H/shorts"))
        self.assertEqual(buf.getvalue(), "")

    def test_the_summary_records_the_net_and_the_floor(self):
        d.SEARCH_QUERY = "cats"
        d.SEARCH_MATCH = "exact"
        d.MIN_DURATION = 600
        fields = d.mode_fields()
        self.assertEqual(fields["search_match"], "exact")
        self.assertEqual(fields["min_duration"], 600)
        self.assertFalse(fields["shorts"])
        d.IN_CHANNEL = "https://www.youtube.com/@H"
        self.assertIsNone(d.mode_fields()["search_match"],
                          "an in-channel search uses neither net")
        d.INCLUDE_SHORTS = True
        self.assertIsNone(d.mode_fields()["min_duration"])

    def test_the_results_json_records_the_net_and_the_floor(self):
        d.SEARCH_MATCH = "related"
        d.MIN_DURATION = 180
        d.SEARCH_FOUND[0] = 42
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "search_results.json")
            d.write_search_results(path, "cats", [
                {"id": "a" * 11, "url": "u", "title": "T", "views": 5, "duration": 900}], "")
            payload = json.load(open(path, encoding="utf-8"))
        self.assertEqual(payload["match"], "related")
        self.assertEqual(payload["min_duration"], 180)
        self.assertEqual(payload["scanned"], 42)
        self.assertEqual(payload["found"], 1)

    def test_the_results_json_reports_no_net_for_an_in_channel_search(self):
        d.IN_CHANNEL = "https://www.youtube.com/@H"
        with tempfile.TemporaryDirectory() as base:
            path = os.path.join(base, "search_results.json")
            d.write_search_results(path, "cats", [], "")
            self.assertIsNone(json.load(open(path, encoding="utf-8"))["match"])

    def test_a_whole_duration_stays_a_whole_number(self):
        # Otherwise the same video's duration changes JSON type depending on
        # which route --sort happened to pick.
        self.assertIsInstance(d.as_seconds("900"), int)
        self.assertIsInstance(d.as_seconds(900.0), int)
        self.assertIsInstance(d.as_seconds("900.5"), float)
        self.assertEqual(d.as_seconds("900.0"), 900)

    # ------------------------------------------- publish dates for a search
    def _resolve(self, stdout, returncode=0, raises=None, stderr=""):
        """Drive resolve_dates against a scripted yt-dlp.

        Returns (items, dated, error, commands). One route only, so a single
        scripted answer is the whole story unless the test says otherwise.
        """
        cmds = []

        def run(cmd, **kw):
            cmds.append(cmd)
            if raises:
                raise raises
            return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)

        saved = (d.subprocess.run, d.po_token_first[0])
        d.subprocess.run = run
        d.po_token_first[0] = True            # one route, for a clear test
        items = [{"id": "a" * 11, "url": d.watch_url("a" * 11), "epoch": None},
                 {"id": "b" * 11, "url": d.watch_url("b" * 11), "epoch": None}]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                dated, error = d.resolve_dates(items)
        finally:
            d.subprocess.run, d.po_token_first[0] = saved
        return items, dated, error, cmds

    def test_dates_are_fetched_for_the_results_that_survived(self):
        items, dated, error, cmds = self._resolve(
            f"{'a' * 11}\t1700000000\n{'b' * 11}\t1700000001\n")
        self.assertEqual((dated, error), (2, None))
        self.assertEqual([i["epoch"] for i in items], [1700000000, 1700000001])
        self.assertIn(d.watch_url("a" * 11), cmds[0])
        self.assertIn(d.watch_url("b" * 11), cmds[0])

    def test_a_video_that_cannot_be_dated_keeps_no_date(self):
        # A removed or private video simply has none; inventing one would be
        # worse than reporting the gap.
        items, dated, error, _cmds = self._resolve(f"{'a' * 11}\t1700000000\n")
        self.assertEqual((dated, error), (1, None))
        self.assertEqual(items[0]["epoch"], 1700000000)
        self.assertIsNone(items[1]["epoch"])

    def test_an_na_timestamp_is_not_read_as_a_date(self):
        items, dated, error, _cmds = self._resolve(
            f"{'a' * 11}\tNA\n{'b' * 11}\t\n")
        self.assertEqual(dated, 0)
        self.assertIsNone(error, "yt-dlp exited cleanly: these videos have no date")
        self.assertEqual([i["epoch"] for i in items], [None, None])

    def test_a_failed_date_fetch_is_not_an_empty_one(self):
        # The difference is the whole point of the error channel: a window
        # applied over dates that never arrived would call every video
        # out-of-window.
        _items, dated, error, _cmds = self._resolve(
            "", returncode=1,
            stderr="ERROR: [youtube] Sign in to confirm you're not a bot")
        self.assertEqual(dated, 0)
        self.assertIsNotNone(error)
        self.assertIn("bot", error)

    def test_a_timeout_keeps_the_dates_that_already_arrived(self):
        # yt-dlp flushes a line per video, so what reached us is paid for.
        timeout = subprocess.TimeoutExpired("yt-dlp", 1)
        timeout.stdout = f"{'a' * 11}\t1700000000\n".encode()
        items, dated, error, _cmds = self._resolve("", raises=timeout)
        self.assertEqual((dated, error), (1, None))
        self.assertEqual(items[0]["epoch"], 1700000000)
        self.assertIsNone(items[1]["epoch"])

    def test_a_timeout_with_nothing_printed_is_an_error(self):
        items, dated, error, _cmds = self._resolve(
            "", raises=subprocess.TimeoutExpired("yt-dlp", 1))
        self.assertEqual(dated, 0)
        self.assertIn("Timed out", error)
        self.assertEqual([i["epoch"] for i in items], [None, None])

    def test_a_half_written_line_is_not_read_as_a_date(self):
        # The last line can be cut mid-number, and "17000" would read as 1970.
        timeout = subprocess.TimeoutExpired("yt-dlp", 1)
        timeout.stdout = f"{'a' * 11}\t1700000000\n{'b' * 11}\t17000".encode()
        items, dated, _error, _cmds = self._resolve("", raises=timeout)
        self.assertEqual(dated, 1)
        self.assertEqual(items[0]["epoch"], 1700000000)
        self.assertIsNone(items[1]["epoch"], "a cut line must not become a date")

    def test_a_rejected_cookie_file_is_retried_for_dates_too(self):
        # Every other lone fetch here drops the jar and asks again; this one
        # did not, so a stale cookies.txt cost the whole run its dates.
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                return subprocess.CompletedProcess(
                    cmd, 1, "", "ERROR: the page needs to be reloaded")
            return subprocess.CompletedProcess(
                cmd, 0, f"{'a' * 11}\t1700000000\n", "")

        saved = (d.subprocess.run, d.drop_rejected_cookies, d.po_token_first[0])
        d.subprocess.run = run
        d.drop_rejected_cookies = lambda text: "reloaded" in (text or "")
        d.po_token_first[0] = True
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                dated, error = d.resolve_dates(
                    [{"id": "a" * 11, "url": d.watch_url("a" * 11), "epoch": None}])
        finally:
            d.subprocess.run, d.drop_rejected_cookies, d.po_token_first[0] = saved
        self.assertEqual((dated, error), (1, None))
        self.assertEqual(len(calls), 2, "the same route must be tried again")

    def test_dates_escalate_to_the_po_token_client(self):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if len(calls) == 1:
                return subprocess.CompletedProcess(cmd, 1, "", "ERROR: blocked")
            return subprocess.CompletedProcess(
                cmd, 0, f"{'a' * 11}\t1700000000\n", "")

        saved = (d.subprocess.run, d.po_token_first[0], d.drop_rejected_cookies)
        d.subprocess.run = run
        d.po_token_first[0] = False           # plain first, then PO token
        d.drop_rejected_cookies = lambda text: False
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                dated, error = d.resolve_dates(
                    [{"id": "a" * 11, "url": d.watch_url("a" * 11), "epoch": None}])
        finally:
            d.subprocess.run, d.po_token_first[0], d.drop_rejected_cookies = saved
        self.assertEqual((dated, error), (1, None))
        self.assertEqual(len(calls), 2, "it must try the PO-token route")

    def test_nothing_is_asked_for_when_every_date_is_known(self):
        saved = d.subprocess.run
        calls = []
        d.subprocess.run = lambda cmd, **kw: calls.append(cmd)
        try:
            self.assertEqual(d.resolve_dates(
                [{"id": "a" * 11, "url": "u", "epoch": 1700000000}]), (0, None))
        finally:
            d.subprocess.run = saved
        self.assertEqual(calls, [], "the dated route already has them")

    def test_a_search_dates_what_it_kept(self):
        d.RESOLVE_DATES = True
        saved = d.resolve_dates
        seen = {}

        def fake(items, deadline=None):
            seen["n"] = len(items)
            for number, item in enumerate(items):
                item["epoch"] = 1700000000 + number
            return len(items), None

        d.resolve_dates = fake
        try:
            kept, _err, _found, _skipped = self._search_items([
                {"id": c * 11, "url": d.watch_url(c * 11), "title": c,
                 "views": 100, "duration": 900} for c in "abc"
            ], dated=False)
        finally:
            d.resolve_dates = saved
        self.assertEqual(seen["n"], 3, "only the survivors are dated")
        self.assertEqual([i["epoch"] for i in kept],
                         [1700000000, 1700000001, 1700000002])

    def test_a_search_too_wide_to_date_says_so_instead(self):
        d.RESOLVE_DATES = True
        d.DATE_RESOLVE_MAX = 2
        saved = d.resolve_dates
        d.resolve_dates = lambda items: self.fail("must not date past the cap")
        buf = io.StringIO()
        try:
            saved_flat = d.search_flat
            d.search_flat = lambda q, c: ([
                {"id": c * 11, "url": d.watch_url(c * 11), "title": c,
                 "view_count": 100, "duration": 900} for c in "abc"], None)
            with contextlib.redirect_stdout(buf):
                kept, _err = d.search_items("cats")
        finally:
            d.resolve_dates = saved
            d.search_flat = saved_flat
            d.DATE_RESOLVE_MAX = 40
        self.assertEqual(len(kept), 3)
        self.assertIn("publish dates left out", buf.getvalue())
        self.assertIn("--limit", buf.getvalue())

    def test_a_date_sort_still_takes_the_dated_route(self):
        # Ordering BY date needs every date before anything can be ordered, so
        # that one route still asks YouTube for them.
        d.RESOLVE_DATES = True
        d.SORT_BY = "date"
        saved = d.resolve_dates
        d.resolve_dates = lambda items: self.fail("the dated route has dates already")
        try:
            kept, _err, _found, _skipped = self._search_items([
                {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "A",
                 "views": 100, "duration": 900, "epoch": int(time.time() - 3600)},
            ])
        finally:
            d.resolve_dates = saved
        self.assertEqual(len(kept), 1)

    def test_a_window_is_applied_locally_not_by_youtube(self):
        # YouTube's own date search throws away the relevance breadth: on one
        # long title it returned 3 videos where the unfiltered list returned 148.
        d.RESOLVE_DATES = True
        d.DAYS = 30
        d.SORT_BY = "views"
        now = int(time.time())
        pool = [
            {"id": "a" * 11, "url": d.watch_url("a" * 11), "title": "fresh big",
             "view_count": 9000, "duration": 900},
            {"id": "b" * 11, "url": d.watch_url("b" * 11), "title": "old big",
             "view_count": 8000, "duration": 900},
            {"id": "c" * 11, "url": d.watch_url("c" * 11), "title": "fresh small",
             "view_count": 7000, "duration": 900},
        ]
        ages = {"a" * 11: now - 3600, "b" * 11: now - 90 * 86400,
                "c" * 11: now - 7200}
        asked, saved = [], (d.search_flat, d.search_dated, d.resolve_dates)
        d.search_flat = lambda q, c: (list(pool), None)
        d.search_dated = lambda *a, **k: self.fail("a window must not take the dated route")

        def fake_resolve(items, deadline=None):
            asked.extend(i["id"] for i in items)
            for item in items:
                item["epoch"] = ages[item["id"]]
            return len(items), None

        d.resolve_dates = fake_resolve
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                kept, err = d.search_items("cats")
        finally:
            d.search_flat, d.search_dated, d.resolve_dates = saved
        self.assertIsNone(err)
        # Sorted by views, then the window keeps only the two fresh ones.
        self.assertEqual([i["id"] for i in kept], ["a" * 11, "c" * 11])
        self.assertEqual(len(asked), 3, "every candidate had to be dated")

    def test_the_window_walk_stops_as_soon_as_the_limit_is_filled(self):
        # The point of walking a list that is already in the caller's order:
        # "top 1 by views this month" dates one video, not all of them.
        d.RESOLVE_DATES = True
        d.DAYS = 30
        d.SORT_BY = "views"
        d.LIMIT = 1
        now = int(time.time())
        pool = [{"id": c * 11, "url": d.watch_url(c * 11), "title": c,
                 "view_count": 9000 - n, "duration": 900}
                for n, c in enumerate("abcdefghij")]
        asked, saved = [], (d.search_flat, d.resolve_dates)
        d.search_flat = lambda q, cnt: (list(pool), None)

        def fake_resolve(items, deadline=None):
            asked.extend(i["id"] for i in items)
            for item in items:
                item["epoch"] = now - 3600
            return len(items), None

        d.resolve_dates = fake_resolve
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                kept, _err = d.search_items("cats")
        finally:
            d.search_flat, d.resolve_dates = saved
        self.assertEqual([i["id"] for i in kept], ["a" * 11])
        self.assertLessEqual(len(asked), 10)
        self.assertEqual(asked[0], "a" * 11, "it must start at the top of the order")

    def test_the_window_walk_respects_the_date_cap(self):
        d.RESOLVE_DATES = True
        d.DATE_RESOLVE_MAX = 4
        now = int(time.time())
        items = [{"id": c * 11, "url": d.watch_url(c * 11), "epoch": None}
                 for c in "abcdefghij"]
        asked, saved = [], d.resolve_dates

        def fake_resolve(chunk, deadline=None):
            asked.extend(i["id"] for i in chunk)
            for item in chunk:
                item["epoch"] = now - 90 * 86400      # all outside the window
            return len(chunk), None

        d.resolve_dates = fake_resolve
        try:
            walk = d.take_in_window(items, now - 30 * 86400, limit=5)
        finally:
            d.resolve_dates = saved
            d.DATE_RESOLVE_MAX = 40
        self.assertEqual(walk["kept"], [])
        self.assertEqual(walk["asked"], 4, "it must stop at the cap")
        self.assertEqual(walk["dated"], 4)
        self.assertEqual(len(asked), 4)
        self.assertFalse(walk["exhausted"], "the cap was hit, not the end")
        self.assertTrue(walk["capped"])

    def test_the_window_walk_reports_an_exhausted_list(self):
        now = int(time.time())
        items = [{"id": c * 11, "url": d.watch_url(c * 11), "epoch": None}
                 for c in "ab"]
        saved = d.resolve_dates

        def fake_resolve(chunk, deadline=None):
            for item in chunk:
                item["epoch"] = now - 3600
            return len(chunk), None

        d.resolve_dates = fake_resolve
        try:
            walk = d.take_in_window(items, now - 30 * 86400, limit=5)
        finally:
            d.resolve_dates = saved
        self.assertEqual(len(walk["kept"]), 2)
        self.assertEqual(walk["dated"], 2)
        self.assertTrue(walk["exhausted"])
        self.assertFalse(walk["capped"])

    def test_the_walk_counts_dates_that_came_back_not_videos_walked(self):
        # It printed "dated 30, kept 0" when zero had been dated.
        now = int(time.time())
        items = [{"id": c * 11, "url": d.watch_url(c * 11), "epoch": None}
                 for c in "abcd"]
        saved = d.resolve_dates
        d.resolve_dates = lambda chunk, deadline=None: (0, None)
        try:
            walk = d.take_in_window(items, now - 30 * 86400, limit=0)
        finally:
            d.resolve_dates = saved
        self.assertEqual(walk["dated"], 0)
        self.assertEqual(walk["asked"], 4)
        self.assertEqual(walk["undated"], 4)
        self.assertEqual(walk["kept"], [])

    def test_the_walk_stops_when_the_fetch_itself_fails(self):
        # Walking on repeats the same failure for every block - minutes of it.
        now = int(time.time())
        items = [{"id": c * 11, "url": d.watch_url(c * 11), "epoch": None}
                 for c in "abcdefghij"]
        calls, saved = [], d.resolve_dates

        def failing(chunk, deadline=None):
            calls.append(len(chunk))
            return 0, "Sign in to confirm you are not a bot"

        d.resolve_dates = failing
        try:
            walk = d.take_in_window(items, now - 30 * 86400, limit=2)
        finally:
            d.resolve_dates = saved
        self.assertEqual(len(calls), 1, "one failure is enough")
        self.assertIn("bot", walk["error"])
        self.assertFalse(walk["exhausted"], "it did not reach the end")
        self.assertFalse(walk["capped"], "the cap is not why it stopped")

    def test_an_epoch_of_zero_is_not_treated_as_missing(self):
        # Truthiness would drop 1 Jan 1970; parse_dates already refuses 0 as a
        # date, so this guards the walk's own test rather than the parser's.
        items = [{"id": "a" * 11, "url": "u", "epoch": None}]
        saved = d.resolve_dates

        def fake(chunk, deadline=None):
            chunk[0]["epoch"] = 0
            return 1, None

        d.resolve_dates = fake
        try:
            walk = d.take_in_window(items, -1, limit=0)
        finally:
            d.resolve_dates = saved
        self.assertEqual(len(walk["kept"]), 1)

    def test_the_block_is_no_bigger_than_the_limit_needs(self):
        # A --limit 2 paid for ten dates when the block had a floor of ten.
        items = [{"id": c * 11, "url": "u", "epoch": None} for c in "abcdefghij"]
        sizes, saved = [], d.resolve_dates

        def fake(chunk, deadline=None):
            sizes.append(len(chunk))
            for item in chunk:
                item["epoch"] = 1
            return len(chunk), None

        d.resolve_dates = fake
        try:
            d.take_in_window(items, 2, limit=2)      # nothing is in window
        finally:
            d.resolve_dates = saved
        self.assertTrue(all(n <= 2 for n in sizes), f"blocks were {sizes}")

    def test_no_dates_sends_a_window_back_to_youtube(self):
        # Without local dates there is nothing to filter on here, so the window
        # has to be asked of YouTube after all.
        d.RESOLVE_DATES = False
        d.DAYS = 30
        d.SORT_BY = "views"
        took, saved = [], (d.search_dated, d.search_flat)
        d.search_dated = lambda q, c, since=None: (took.append("dated") or ([], None))
        d.search_flat = lambda q, c: (took.append("flat") or ([], None))
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                d.search_items("cats")
        finally:
            d.search_dated, d.search_flat = saved
        self.assertEqual(took, ["dated"])

    def test_the_route_decision_has_one_answer(self):
        # The banner printed the dated route's shallow depth while the run took
        # the cheap route and scanned 150, so the two disagreed on screen.
        d.RESOLVE_DATES = True
        d.SORT_BY = "views"
        d.THIS_MONTH = True
        self.assertFalse(d.needs_dated_route(), "a window is applied locally")
        self.assertEqual(d.search_depth(d.needs_dated_route()), d.SEARCH_DEPTH_FLAT)
        d.SORT_BY = "date"
        self.assertTrue(d.needs_dated_route(), "a date sort needs every date")
        self.assertEqual(d.search_depth(d.needs_dated_route()), d.SEARCH_DEPTH_DATED)
        d.SORT_BY = "views"
        d.RESOLVE_DATES = False
        self.assertTrue(d.needs_dated_route(), "without local dates, ask YouTube")

    def test_a_search_with_no_window_needs_no_dated_route(self):
        d.RESOLVE_DATES = True
        d.SORT_BY = "views"
        self.assertFalse(d.needs_dated_route())
        d.RESOLVE_DATES = False
        self.assertFalse(d.needs_dated_route(), "no window, so nothing to filter")

    def test_the_filtered_out_tally_adds_up(self):
        # "3 of 146 scanned" with only two reasons listed left a silent
        # remainder of 47 videos the walk never looked at.
        d.MIN_DURATION = 180
        d.SEARCH_FOUND[0] = 10
        d.SEARCH_COUNTS[0] = {"scanned": 10, "short": 2, "nolength": 0,
                              "views": 0, "live": 0, "skipped": 0,
                              "outside": 3, "undated": 1, "unreached": 3}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            d.print_search_results([{"id": "a" * 11, "url": "u", "title": "T",
                                     "views": 5, "duration": 900}], "")
        shown = buf.getvalue()
        for text in ("2 under", "3 outside the window", "1 with no publish date",
                     "3 not reached"):
            self.assertIn(text, shown)
        # 1 kept + 2 + 3 + 1 + 3 == 10 scanned
        self.assertIn("1 video(s) of 10 scanned", shown)

    def test_the_walk_counts_what_it_never_reached(self):
        d.RESOLVE_DATES = True
        d.DAYS = 30
        d.SORT_BY = "views"
        d.LIMIT = 1
        now = int(time.time())
        pool = [{"id": c * 11, "url": d.watch_url(c * 11), "title": c,
                 "view_count": 9000 - n, "duration": 900}
                for n, c in enumerate("abcdefghij")]
        saved = (d.search_flat, d.resolve_dates)
        d.search_flat = lambda q, cnt: (list(pool), None)

        def fake(items, deadline=None):
            for item in items:
                item["epoch"] = now - 3600
            return len(items), None

        d.resolve_dates = fake
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                kept, _err = d.search_items("cats")
        finally:
            d.search_flat, d.resolve_dates = saved
        counts = d.SEARCH_COUNTS[0]
        self.assertEqual(len(kept), 1)
        self.assertEqual(counts["unreached"], 9, "one was dated, nine untouched")

    def test_no_dates_flag_parses(self):
        self.assertFalse(d.parse_args(["/tmp"]).no_dates)
        self.assertTrue(d.parse_args(["/tmp", "--no-dates"]).no_dates)


class TestMacOS(PlatformCase, unittest.TestCase):
    WINDOWS = False


class TestWindows(PlatformCase, unittest.TestCase):
    WINDOWS = True


if __name__ == "__main__":
    unittest.main(verbosity=2)
