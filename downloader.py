#!/usr/bin/env python3
"""
YouTube Video Downloader (yt-dlp based)

Usage:
    python3 downloader.py <directory>

    <directory>   A folder that contains BOTH:
                    - cookies.txt   (your exported cookies, Netscape format)
                    - links.txt     (one YouTube link per line)
                  Videos are also downloaded INTO this same folder.

Optional flags:
    --channel            Group videos by channel (see LAYOUT below)
    --views N            Channel links: only videos with at least N views
    --limit N            Channel links: stop after the newest N matches
    --links FILE         Read the links from FILE instead of <directory>/links.txt
    --cookies FILE       Use FILE instead of <directory>/cookies.txt
    --min-height N       Quality floor   (default: 720)
    --max-height N       Quality ceiling (default: 1080)
    --no-subs            Do NOT build transcripts (built by default)
    --sub-lang CODE      Transcript language code (default: en)
    --transcript         Transcript only - no video is downloaded
    --extract-comments   Also save comments_<name>.txt (top 100, replies off)
    --comment-limit N    Top-level comments per video (default: 100; 0 = all)
    --comment-replies    Also fetch the replies under those comments
    --comment-sort S     top (default) or new
    --search QUERY       Search YouTube and use the results as the links
    --in-channel URL     With --search: search inside that channel only
    --match MODE         related (default: YouTube's own results page, so
                         near-matches come too) | exact (narrower ytsearch)
    --search-count N     Results to scan before filtering (default: 150,
                         or 40 when a date window forces the dated route)
    --sort ORDER         relevance (default) | views/popularity |
                         least-viewed (reverse) | date | oldest
    --this-month         Search window: since the 1st of this month
    --shorts             Include short-form videos (left out by default)
    --min-duration SECS  Shortest video to keep (default: 180; 0 = any)
    --no-thumbnail       Do NOT save the thumbnail (saved by default)
    --no-description     Do NOT save the description (saved by default)
    --dry-run            Show the download plan and exit without downloading
    --yes                Skip the interactive confirmation prompt

Examples:
    python3 downloader.py "/Users/me/Videos/YT"
    python3 downloader.py "/Users/me/Videos/YT" --channel
    python3 downloader.py ~/Downloads/batch --max-height 1080 --min-height 720
    python3 downloader.py ~/yt --channel --views 1000 --limit 3   (channel links)

LAYOUT
    default     <dir>/<Video Title>/
    --channel   <dir>/<Channel Name>/<Video Title>/

Inside every video folder:
    <Video Title>.mp4    1080p (priority) down to 720p (floor) — never lower
    <Video Title>.jpg    the video's thumbnail
    videoinfo.txt        title, link, quality, status/error, what else was saved
    trans_<name>.txt     English transcript, grouped lines  -> [hh:mm:ss] text
    words_<name>.txt     word-level transcript              -> [hh:mm:ss.mmm] word
    description_<name>.txt  the video's description, with channel/date/views
  (transcript formatting adapted from ../Transcript with timestamps/transcribe.py)

CHANNEL LINKS
    links.txt may hold channel links as well as video links, mixed freely. A
    channel link is expanded into its videos, newest first:
        --views N   keep only videos with at least N views
        --limit N   stop after the newest N that match (default: the whole channel)
    Only long-form videos are listed: YouTube keeps Shorts and streams in
    separate tabs, and the channel's /videos tab is the one read.

SKIP LIST
    skip.txt next to links.txt lists videos to leave alone - one link (or bare
    11-character id) per line, # for comments. They are never downloaded, and a
    skipped video does not use up a --limit slot. A run says how many it left out.

SEARCH
    YouTube's own search filters are unreachable: yt-dlp drops the sp= parameter
    that carries them, so every filter and sort here is applied locally - which
    also makes "last 30 days" exactly 30 days rather than a coarse month bucket.
    No date asked for means one cheap flat listing; a date window or a date sort
    uses one non-flat listing with --dateafter, which filters and dates in the
    same pass.

RESUME
    Finished videos are indexed by video id at startup — in BOTH layouts — so a
    re-run skips them without any network call, even if you switch modes or the
    same video is listed twice with different tracking suffixes (?v=X&pp=...).
    Interrupted downloads are picked up again (yt-dlp continues its .part file).

WINDOWS
    Windows needs extra yt-dlp flags (Node path + mweb client); they are applied
    automatically there and never on macOS. See windows_flags() below, the notes
    in README.md, and install requirements.txt.
"""

import argparse
import contextlib
import datetime
import json
import os
import atexit
import hashlib
import tempfile
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote, urlsplit, urlunsplit

# These are filled in from command-line arguments in main().
LINKS_FILE     = "links.txt"
COOKIES_FILE   = "cookies.txt"
DOWNLOAD_DIR   = "."
MAX_HEIGHT     = 1080
MIN_HEIGHT     = 720
FORMAT         = ""
DOWNLOAD_SUBS  = True      # also write an English auto-transcript .txt
SUB_LANG       = "en"      # transcript language code
TRANSCRIPT_WRAP = 40       # max chars per line before wrapping (transcribe.py default)
CHANNEL_MODE   = False     # True = group videos into <Channel Name>/ folders
SAVE_THUMBNAIL = True      # also save the video thumbnail as .jpg
SAVE_DESCRIPTION = True    # also save the video description as .txt
VERBOSE        = False     # True = show yt-dlp's full output instead of a bar
MIN_VIEWS      = 0         # channel links: view floor (0 = take every video)
LIMIT          = 0         # channel links: newest N matches each (0 = no cap)
DAYS           = 0         # filter by last N days (0 = any time)
SKIP_FILE      = "skip.txt"
SKIP_IDS       = set()     # video ids listed in skip.txt - never downloaded
TRANSCRIPT_ONLY = False    # True = write the transcript and never fetch the video

# What a transcript-only folder records. The status is deliberately not "OK":
# index_downloaded() wants "OK" plus a video file, so such a folder is never
# mistaken for a finished download and asking for the video later still gets it.
TRANSCRIPT_STATUS = "TRANSCRIPT"
TRANSCRIPT_QUALITY = "NA (transcript only)"
TRANSCRIPT_COUNT = [0]     # transcripts written this run, for the summary

EXTRACT_COMMENTS = False   # True = also save each video's comments
COMMENT_LIMIT   = 100      # top-level comments per video (0 = every one)
COMMENT_REPLIES = False    # True = fetch the replies under those comments too
COMMENT_SORT    = "top"    # "top" (YouTube's ranking) or "new"
COMMENT_COUNT   = [0]      # comments written this run, for the summary
# Comment extraction is paged and can be slow; measured 7.4s for 100 top-level
# comments and 18.6s with their replies. The cap is what stops a video with tens
# of thousands of them from holding up the batch.
COMMENT_TIMEOUT = 600

SEARCH_QUERY   = None      # --search QUERY: run a YouTube search first

# How many results to scan, when --search-count was not given. One number
# cannot serve both routes: scanning costs about 0.07s per result on the cheap
# flat listing and about 2s on the dated one, which needs a full extraction per
# video to learn its upload date. 150 is the depth the research wants and takes
# about 10s flat; the same 150 dated would take five minutes, longer than the
# REST API will hold its lock for. So the depth follows the route, and the run
# prints which one it used.
SEARCH_DEPTH_FLAT  = 150
SEARCH_DEPTH_DATED = 40

# The flat route returns no upload dates - YouTube's flat listing simply has
# none - so they are fetched afterwards, for the videos that SURVIVED the
# filters and the limit rather than for everything scanned. Measured at about
# 2.1s per video (20 in 42.6s, one yt-dlp call), so dating the 20 results of a
# search costs 42s where dating the 150 it looked at would cost five minutes.
#
# The cap is what stops an unlimited search from quietly spending those five
# minutes: past it the dates are left out and the run says so, naming --limit.
RESOLVE_DATES    = True    # --no-dates turns it off
# At about 2.1s each this is the longest a run will spend on dates: a shade
# over three minutes. The REST API lowers it for itself, because a request
# cannot hold the downloader's globals for that long (see ROUTE_GLOBALS).
DATE_RESOLVE_MAX = 100
SEARCH_COUNT   = None      # --search-count N; None = pick by route
SORT_BY        = "relevance"   # relevance | views | date | oldest
THIS_MONTH     = False     # --this-month: since the 1st of the current month
IN_CHANNEL     = None      # --in-channel URL: search inside that channel only
SEARCH_FOUND   = [0]       # results the last search returned, before filtering
# Why the last search dropped what it dropped, in the shape the channel
# listings use. The length floor is on by default, so a thin answer needs to
# say which filter thinned it.
SEARCH_COUNTS  = [{}]

# How wide a net the search casts. These are two different questions asked of
# YouTube, not two orderings of one answer - see web_search_url().
#   related - YouTube's own results page: the near-matches come too
#   exact   - yt-dlp's ytsearch: pseudo-URL, which answers far more narrowly
SEARCH_MATCH   = "related"
MATCH_MODES    = ("related", "exact")

# A Short is at most 3 minutes, so this floor excludes them without needing a
# field YouTube does not publish. It is a floor on length, not a Shorts flag:
# that is the only signal a search listing actually carries.
SHORTS_SECONDS = 180
MIN_DURATION   = SHORTS_SECONDS   # --min-duration N: shortest video to keep
INCLUDE_SHORTS = False            # --shorts: let short-form videos through
SEARCH_TIMEOUT = 900       # a dated search extracts every result it scans

IS_WINDOWS = platform.system() == "Windows"

# aria2c downloads one file over many parallel connections — the same trick IDM
# uses. YouTube throttles each single connection well below the line rate, so
# this is the difference between a few hundred KB/s and saturating the link.
ARIA2C = shutil.which("aria2c")
ARIA2C_ARGS = ("-x16 -s16 -k1M -c --file-allocation=none "
               "--console-log-level=warn --summary-interval=1"
               + (" " + os.environ["ARIA2C_EXTRA"] if os.environ.get("ARIA2C_EXTRA") else ""))


def build_format():
    """yt-dlp format string: best video+audio within [MIN_HEIGHT, MAX_HEIGHT].

    Preference order (all capped to the 720-1080 range):
      1. H.264 video + AAC audio      -> most compatible .mp4 (plays in QuickTime, editors)
      2. H.264 video + any audio
      3. any codec video + any audio  (e.g. AV1/VP9 fallback)
      4. best combined stream
    If nothing exists in the range, yt-dlp errors -> we record it (no lower-quality download).
    """
    lo, hi = MIN_HEIGHT, MAX_HEIGHT
    return (
        f"bv*[height<={hi}][height>={lo}][vcodec^=avc1]+ba[acodec^=mp4a]/"
        f"bv*[height<={hi}][height>={lo}][vcodec^=avc1]+ba/"
        f"bv*[height<={hi}][height>={lo}]+ba/"
        f"b[height<={hi}][height>={lo}]"
    )


# Names Windows refuses to use for a file or folder, with or without an
# extension. Creating one raises OSError there, while macOS accepts it happily.
WINDOWS_RESERVED = ({"CON", "PRN", "AUX", "NUL"}
                    | {f"COM{i}" for i in range(1, 10)}
                    | {f"LPT{i}" for i in range(1, 10)})

# Windows caps a whole path at 260 characters by default, and --channel nests a
# channel folder above the video folder, so components are kept much shorter
# there. Budget: base + channel + folder + "words_<folder>.txt" stays inside 260
# even from a fairly deep base directory.
NAME_LIMIT = 60 if IS_WINDOWS else 150
# Linux limits a name to 255 bytes, not characters: 150 characters of Hindi or
# Chinese are 450 bytes. The cap leaves room for what goes around a title -
# "words_not_found_", " [<id>]", yt-dlp's ".f137.mp4.part".
NAME_BYTES = 180


def sanitize(name: str) -> str:
    """Make a string safe to use as a folder/file name on macOS *and* Windows."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)   # forbidden chars
    name = re.sub(r"[\x00-\x1f]", "", name)       # control chars
    name = re.sub(r"\s+", " ", name).strip()      # collapse whitespace
    name = name.rstrip(". ")                       # no trailing dots/spaces
    name = name[:NAME_LIMIT].rstrip(". ")          # keep the path within limits
    name = name.encode("utf-8")[:NAME_BYTES].decode("utf-8", "ignore").rstrip(". ")
    # Windows matches a reserved device by the part before the first dot, so the
    # underscore has to go on the stem: "aux.txt_" is still AUX, "aux_.txt" is not.
    stem, dot, rest = name.partition(".")
    if stem.upper() in WINDOWS_RESERVED:
        name = f"{stem}_{dot}{rest}"
    return name


def human_size(num_bytes):
    """Bytes as a readable size: 900 KB, 42.9 MB, 1.4 GB."""
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_time(seconds):
    """Seconds with their units: 45s, 1m 22s, 2h 05m 09s."""
    total = int(round(seconds or 0))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def count_or_na(value):
    """A count with thousands separators, or NA when YouTube did not report it.

    Views and subscriber counts are usually present but not guaranteed — some
    channels hide their subscriber count, and a video can come back without a
    view count — so the field is always written, with NA standing in.
    """
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "NA"


# Pakistan Standard Time is a flat UTC+5 with no daylight saving, so a fixed
# offset is exactly right and needs no timezone database.
PKT = datetime.timezone(datetime.timedelta(hours=5), "PKT")


def upload_time_pkt(info):
    """When the video went up, in Pakistan time on a 12-hour clock.

    YouTube's `timestamp` carries the exact moment; `upload_date` only has the
    day, so it is the fallback and says so rather than implying a time.
    """
    stamp = (info or {}).get("timestamp")
    if stamp:
        try:
            moment = datetime.datetime.fromtimestamp(int(stamp), PKT)
            return moment.strftime("%d %b %Y, %I:%M:%S %p PKT")
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    date = str((info or {}).get("upload_date") or "")
    if len(date) == 8 and date.isdigit():
        try:
            day = datetime.datetime.strptime(date, "%Y%m%d")
            return day.strftime("%d %b %Y") + " (time NA)"
        except ValueError:
            pass
    return "NA"


def category_or_na(info):
    """The video's YouTube category, or NA."""
    names = [str(c).strip() for c in ((info or {}).get("categories") or []) if c]
    return ", ".join(names) or "NA"


def folder_size(folder):
    """Total bytes of the files in a folder."""
    total = 0
    try:
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                total += os.path.getsize(path)
    except OSError:
        pass
    return total


# Totals for the run: bytes that landed on disk, and the seconds actually spent
# downloading (waiting between retries is deliberately not counted, so the rate
# reported is the rate you got, not an average dragged down by backoff).
STATS = {"bytes": 0, "seconds": 0.0}


def read_links(path: str):
    """Return a list of non-empty, non-comment links from the links file."""
    if not os.path.exists(path):
        print(f"ERROR: links file not found: {path}")
        sys.exit(1)
    links = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                links.append(line)
    return links


# ============================== output =====================================
# yt-dlp's own output is a wall of text — several lines per video, plus a
# progress bar that repaints constantly. By default it all goes to a log file
# and the terminal shows one rewriting line per video instead. --verbose puts
# the raw output back on screen.

LOG_FILE = [None]          # path to the run's text log, once main() sets it
LOG_JSON = [None]          # path to the run's JSON log
LOG_JSONL = [None]         # one line per record, written the moment it exists
LOG_DEBUG = [None]         # redacted, one-event-per-line diagnostic trace
RUN_ID = [None]            # identifies one invocation across all trace events
SUMMARY_WRITTEN = [False]  # the run's download_log.json is on disk
RUN_LOOP_STARTED = [False] # past this point only main() itself writes the summary
STOPPING = [False]         # a Ctrl+C was taken, or the run is writing its totals


def on_sigint(signum, frame):
    """Ctrl+C stops the run - once. Later ones (a second press, a stop sent twice) are
    ignored wherever they land, so a stop can never cut its own clean-up short."""
    if STOPPING[0]:
        return
    STOPPING[0] = True
    raise KeyboardInterrupt
LOG_RECORDS = []           # one structured record per video

# Both downloaders report progress, in their own shapes:
#   yt-dlp:  [download]  45.2% of  38.76MiB at  1.50MiB/s ETA 00:25
#   aria2c:  [#06bc5a 3.1MiB/4.0MiB(79%) CN:16 DL:1.7MiB ETA:12s]
PROGRESS_PATTERNS = (
    re.compile(r"\[download\]\s+(?P<pct>[\d.]+)% of.*?at\s+(?P<speed>[\d.]+\s*\S+/s)"
               r"\s+ETA"),
    re.compile(r"\((?P<pct>\d+)%\).*?DL:\s*(?P<speed>[\d.]+\s*\S+?)\s"),
)

# yt-dlp announces each new file it starts. The bar goes back to zero there, so
# a video that follows a finished subtitle does not appear to start at 100%.
NEW_FILE_RE = re.compile(r"\[download\] Destination:|\[Merger\]")


def parse_progress(line):
    """(percent, speed) from a downloader's progress line, or None."""
    for pattern in PROGRESS_PATTERNS:
        m = pattern.search(line)
        if m:
            speed = m.group("speed").strip()
            return float(m.group("pct")), (speed if speed.endswith("/s") else speed + "/s")
    return None


def log_line(text):
    """Append a line to the run's text log; silent if logging is not set up."""
    if not LOG_FILE[0] or not text:
        return
    try:
        with open(LOG_FILE[0], "a", encoding="utf-8") as f:
            f.write(text.rstrip() + "\n")
    except OSError:
        pass


def _text(value):
    """Turn subprocess output or an exception value into safe text."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def redact_url(value):
    """Keep a URL useful for correlation without recording signed query values."""
    text = _text(value)
    try:
        parsed = urlsplit(text)
        if parsed.scheme and parsed.netloc:
            vid = video_id_from_url(text)
            query = f"v={vid}" if vid else ""
            return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))
    except (TypeError, ValueError):
        pass
    return re.sub(r"(?i)(token|signature|sig|cookie|authorization)=([^&\s]+)",
                  r"\1=<redacted>", text)


def redact_text(value):
    """Remove signed URLs and common credential/token values from diagnostics."""
    text = _text(value)
    text = re.sub(r"https?://[^\s'\"]+", lambda m: redact_url(m.group(0)), text)
    return re.sub(
        r"(?i)((?:po[_-]?token|token|signature|sig|visitor_data|cookie|authorization|"
        r"sapisid|sid)=)([^&\s]+)", r"\1<redacted>", text)


def redact_command(cmd):
    """Return a command suitable for a local diagnostic log."""
    safe, hide_next = [], False
    for arg in cmd:
        arg = _text(arg)
        if hide_next:
            safe.append("<cookies-file>")
            hide_next = False
        elif arg in ("--cookies", "--password"):
            safe.append(arg)
            hide_next = True
        else:
            safe.append(redact_text(arg))
    return safe


def log_debug(event, **fields):
    """Append one structured diagnostic event, without exposing credentials."""
    if not LOG_DEBUG[0]:
        return
    record = {
        "time": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds"),
        "run_id": RUN_ID[0],
        "event": event,
    }
    for key, value in fields.items():
        if key in ("command",):
            record[key] = redact_command(value)
        elif key in ("url",):
            record[key] = redact_url(value)
        elif key in ("stdout_tail", "stderr_tail", "error", "message"):
            record[key] = redact_text(value)[-2000:]
        else:
            record[key] = value
    try:
        with open(LOG_DEBUG[0], "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        return
def run_logged(stage, cmd, **kwargs):
    """Run a captured subprocess and record its full lifecycle in the trace."""
    started = time.monotonic()
    log_debug("command_start", stage=stage, command=cmd, timeout=kwargs.get("timeout"))
    try:
        out = subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired as e:
        log_debug("command_timeout", stage=stage, command=cmd,
                  duration=round(time.monotonic() - started, 3),
                  timeout=kwargs.get("timeout"), stdout_tail=e.stdout, stderr_tail=e.stderr)
        raise
    except OSError as e:
        log_debug("command_error", stage=stage, command=cmd,
                  duration=round(time.monotonic() - started, 3), error=str(e))
        raise
    log_debug("command_finish", stage=stage, command=cmd,
              duration=round(time.monotonic() - started, 3),
              returncode=getattr(out, "returncode", None),
              stdout_tail=_text(getattr(out, "stdout", "")),
              stderr_tail=_text(getattr(out, "stderr", "")))
    return out


def note(text):
    """Detail that belongs in the log; on screen only with --verbose."""
    log_line(text)
    if VERBOSE:
        print(text)


def log_record(**fields):
    """Keep one structured record for the JSON log, and stream it as a line.

    The JSON log is written once, when the run is over. The .jsonl line goes out
    the moment the record exists, so whatever is watching - the API - can hand
    over each video as it lands instead of after the whole batch.
    """
    LOG_RECORDS.append(fields)
    if LOG_JSONL[0]:
        try:
            with open(LOG_JSONL[0], "a", encoding="utf-8") as f:
                f.write(json.dumps(fields, ensure_ascii=False) + "\n")
        except OSError:
            pass


def mode_fields():
    """The keys that say what kind of run this was and what it produced.

    Every summary writer includes these, so a caller reading download_log.json
    never has to know which of them wrote it - a search that failed before the
    first download still says it was a search, and for what.
    """
    return {
        "mode": "transcript" if TRANSCRIPT_ONLY else "download",
        "transcripts": TRANSCRIPT_COUNT[0],
        "comments": COMMENT_COUNT[0],
        "search_query": SEARCH_QUERY,
        "search_sort": SORT_BY if SEARCH_QUERY else None,
        "search_in_channel": IN_CHANNEL,
        # Which net was cast. None for an in-channel search, which uses the
        # channel's own tab, where neither value of --match applies.
        "search_match": SEARCH_MATCH if (SEARCH_QUERY and not IN_CHANNEL) else None,
        "min_duration": duration_floor() or None,
        "shorts": INCLUDE_SHORTS,
    }


def failed_before_download():
    """Links that never reached the download loop: a channel that could not be
    listed, or a search that failed outright. Both are usually the whole reason
    there was nothing to do, so neither may be reported as a clean empty run."""
    return sum(1 for r in LOG_RECORDS
               if r.get("status") in ("channel_failed", "failed"))


def write_empty_summary(stopped=False, excluded=0):
    """The JSON log for a run that ended before downloading anything, so a caller
    always has a summary. Unreadable channels are counted as failed, since they are
    usually why there was nothing to do."""
    write_json_log({
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layout": "channel" if CHANNEL_MODE else "flat",
        "min_views": MIN_VIEWS or None, "limit": LIMIT or None,
        "days": DAYS or None,
        "excluded_by_skip_list": excluded, "links": 0, "downloaded": 0, "skipped": 0,
        "failed": failed_before_download(),
        **mode_fields(),
        "stopped_early": stopped, "bytes": 0, "size": human_size(0),
        "download_seconds": 0.0, "run_seconds": 0.0, "average_speed": None,
        "debug_log": os.path.basename(LOG_DEBUG[0]) if LOG_DEBUG[0] else None,
    })


def write_plan_summary(source_links, eligible_links, planned_downloads,
                       already_downloaded, excluded=0, dry_run=False,
                       confirmed=False, selected_downloads=None):
    """Persist the preflight plan when no download loop was started."""
    write_json_log({
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layout": "channel" if CHANNEL_MODE else "flat",
        "min_views": MIN_VIEWS or None, "limit": LIMIT or None,
        "days": DAYS or None,
        "excluded_by_skip_list": excluded,
        "source_links": source_links,
        "links": eligible_links,
        "planned_downloads": planned_downloads,
        "selected_downloads": (planned_downloads if selected_downloads is None
                                else selected_downloads),
        "already_downloaded": already_downloaded,
        "downloaded": 0, "skipped": already_downloaded,
        "failed": failed_before_download(),
        **mode_fields(),
        "dry_run": dry_run, "confirmed": confirmed,
        "stopped_early": False, "bytes": 0, "size": human_size(0),
        "download_seconds": 0.0, "run_seconds": 0.0, "average_speed": None,
        "debug_log": os.path.basename(LOG_DEBUG[0]) if LOG_DEBUG[0] else None,
    })


def write_json_log(summary):
    """Write the JSON log: the run's totals, then a record per video."""
    if not LOG_JSON[0]:
        return
    tmp = LOG_JSON[0] + ".tmp"            # replaced whole: a kill mid-write keeps the old one
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"run": summary, "videos": LOG_RECORDS}, f,
                      indent=2, ensure_ascii=False)
        os.replace(tmp, LOG_JSON[0])
        SUMMARY_WRITTEN[0] = True
    except OSError as e:
        print(f"  (could not write {os.path.basename(LOG_JSON[0])}: {e})")
    finally:
        _remove_quietly(tmp)


BAR_WIDTH = 20


# Repaint at most this often. Faster adds nothing a person can read, and on a
# slow terminal the redraws start costing more than the download.
REPAINT_SECONDS = 0.12


class ProgressLine:
    """One rewriting terminal line for the video being downloaded.

    The bar is drawn only when stdout is a terminal. Redirected to a file or a
    pipe there is nothing to rewrite, and every repaint would land as another
    line, so the video's final line is all that gets written.
    """

    def __init__(self, index, total, title):
        self.head = f"[{index}/{total}]"
        self.title = title
        self.width = 0
        self.painted = 0.0

    def _write(self, text):
        columns = shutil.get_terminal_size((80, 20)).columns
        text = text[:columns - 1]
        # Pad over whatever the previous, possibly longer, line left behind.
        sys.stdout.write("\r" + text.ljust(self.width))
        sys.stdout.flush()
        self.width = len(text)

    def restart(self):
        """A new file is starting: show it from zero rather than where the last
        one finished."""
        self.painted = 0.0
        self.update(0.0, "")

    def update(self, pct, speed):
        now = time.monotonic()
        if not sys.stdout.isatty() or now - self.painted < REPAINT_SECONDS:
            return
        self.painted = now
        filled = int(BAR_WIDTH * max(0.0, min(100.0, pct)) / 100)
        bar = "#" * filled + "." * (BAR_WIDTH - filled)
        left = f"{self.head} [{bar}] {pct:5.1f}% {speed:>11}  "
        room = max(0, shutil.get_terminal_size((80, 20)).columns - len(left) - 1)
        title = self.title if len(self.title) <= room else self.title[:max(0, room - 3)] + "..."
        self._write(left + title)

    def done(self, text):
        """Replace the bar with the video's final line, and keep it."""
        line = f"{self.head} {text}"
        if sys.stdout.isatty():
            self._write(line)
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            print(line)
        log_line(line)
        self.width = 0


# Holds the exit code of the most recent run_streaming() call.
last_returncode = [0]


def run_streaming(cmd, progress=None, stage="media"):
    """Run a command and capture its output. Returns the captured text.

    With --verbose the raw output goes straight to the terminal, so yt-dlp's own
    progress bar behaves as usual. Otherwise every line is written to the log
    file and only the percentage and speed are lifted out, to drive the compact
    one-line display.
    """
    started = time.monotonic()
    log_debug("command_start", stage=stage, command=cmd)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        last_returncode[0] = 1
        log_debug("command_error", stage=stage, command=cmd,
                  duration=round(time.monotonic() - started, 3), error=str(e))
        return f"ERROR: could not start yt-dlp: {e}"

    # read1() returns as soon as any bytes are available (so progress stays
    # live) and is safe on Windows too — a low-level os.read() on the raw file
    # descriptor crashes there with "Bad file descriptor" or hangs.
    chunks, pending = [], ""
    try:
        while True:
            data = proc.stdout.read1(4096)
            if not data:
                break
            chunks.append(data)
            if VERBOSE:
                sys.stdout.buffer.write(data)   # raw bytes -> keeps the \r bar intact
                sys.stdout.buffer.flush()
                continue
            # Progress repaints with \r rather than \n, so split on both and hold
            # back the unfinished tail until more arrives.
            pending += data.decode("utf-8", "replace")
            parts = re.split(r"[\r\n]", pending)
            pending = parts.pop()
            for line in parts:
                if not line.strip():
                    continue
                log_line(line)
                if progress and NEW_FILE_RE.search(line):
                    progress.restart()
                found = parse_progress(line)
                if found and progress:
                    progress.update(*found)
    except BaseException as e:
        log_debug("command_interrupted", stage=stage, command=cmd,
                  duration=round(time.monotonic() - started, 3), error=str(e))
        # Stopped (Ctrl+C, a kill): yt-dlp has to be gone before this process is. Left
        # running, it saves its cookie jar into the private copy after that has been
        # removed, and keeps aria2c writing into the folder.
        if not isinstance(e, KeyboardInterrupt):
            # A kill reached this process alone. yt-dlp gets a Ctrl+C rather than a
            # SIGTERM: it stops its aria2c on one, and would leave it running on the other.
            if os.name == "posix":
                proc.send_signal(signal.SIGINT)
            else:
                proc.terminate()
        deadline = time.monotonic() + 15
        while True:
            try:
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, 15)
                proc.communicate(timeout=deadline - time.monotonic())
                break
            except subprocess.TimeoutExpired:
                proc.kill()
                try:                      # aria2c may hold the pipe open: wait, don't read
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                break
            except BaseException:         # a second kill or Ctrl+C: keep waiting
                continue
        raise e
    if pending.strip():
        log_line(pending)

    proc.wait()
    last_returncode[0] = proc.returncode
    result = b"".join(chunks).decode("utf-8", "replace")
    log_debug("command_finish", stage=stage, command=cmd,
              duration=round(time.monotonic() - started, 3),
              returncode=proc.returncode, stdout_tail=result)
    return result


# The `mweb` client makes yt-dlp mint the "Proof of Origin" token that YouTube
# now demands for some videos; without one those downloads die with HTTP Error
# 403 (fix_yt_dlp_windows.md #2).
PO_TOKEN_FLAGS = ["--extractor-args", "youtube:player_client=mweb,default"]
# Subtitle PO tokens are generated for the web client and are bound to the
# individual video. This is separate from the mweb GVS token used for media.
SUBTITLE_PO_TOKEN_FLAGS = ["--extractor-args", "youtube:player_client=web"]

# The token itself is generated by the bgutil provider, which runs a small JS
# script — no browser window, unlike the browser-driving providers.
BGUTIL_PATHS = [
    os.environ.get("BGUTIL_SCRIPT"),
    "~/.local/share/bgutil-pot/server/build/generate_once.js",
    "~/bgutil-ytdlp-pot-provider/server/build/generate_once.js",
]


def find_bgutil_script():
    """Path to the bgutil PO-token script, if it is installed."""
    for path in BGUTIL_PATHS:
        if path:
            full = os.path.expanduser(path)
            if os.path.exists(full):
                return full
    return None


BGUTIL_SCRIPT = find_bgutil_script()

# Once one video turns out to need the PO-token client, the rest of the batch
# almost always does too (same source, same gating). Remembering that skips a
# doomed first attempt on every later video.
po_token_first = [IS_WINDOWS]

# Error text that means "this video needs a PO token" -> worth one retry.
# aria2c reports the same 403 opaquely: exit code 22 is "bad HTTP response
# header", which is what a rejected stream URL looks like from its side.
PO_TOKEN_ERRORS = ("403", "forbidden", "po token", "requested format is not available",
                   "aria2c exited with code 22")


def windows_flags():
    """Extra yt-dlp flags needed only on Windows (fix_yt_dlp_windows.md).

    macOS/Linux need neither — yt-dlp finds its JS runtime by itself there, and
    the default clients work for most videos — so this stays empty off Windows,
    leaving the working Mac path untouched.
      1. Windows (especially inside a venv, under a Python subprocess) fails to
         detect Node, so the absolute node path is passed explicitly.
      2. Windows goes straight to the PO-token client instead of paying for a
         failed first attempt; elsewhere that is used as an automatic retry.
    """
    if not IS_WINDOWS:
        return []
    # Let yt-dlp apply its own Windows-safe name rules as a backstop; the actual
    # length budget is handled by naming the output file ourselves (see
    # output_template), because --trim-filenames would trim the whole path
    # template and collapse the per-video folder into the filename.
    flags = ["--windows-filenames"]
    node = shutil.which("node")
    if node:
        flags += ["--js-runtimes", f"node:{node}"]
    # The PO-token client itself is not added here: po_token_first starts True
    # on Windows, so the download already begins with PO_TOKEN_FLAGS. Adding it
    # in both places would pass the same --extractor-args twice.
    return flags


def speed_flags():
    """Flags that pull the download up to the line's real speed.

    - aria2c (when installed) fetches each file over 16 parallel connections,
      which sidesteps YouTube's per-connection throttling. Measured here: a
      video that crawled at ~470 KB/s finished at ~1.7 MB/s, the link's ceiling.
    - concurrent-fragments does the same for fragmented DASH/HLS formats, which
      the native downloader handles itself.
    """
    flags = ["--concurrent-fragments", "8"]
    if ARIA2C:
        flags += ["--downloader", "aria2c", "--downloader-args", f"aria2c:{ARIA2C_ARGS}"]
    return flags


def needs_po_token(error_text):
    """True if a failure looks like YouTube's PO-token gate rather than a real error."""
    low = (error_text or "").lower()
    return any(hint in low for hint in PO_TOKEN_ERRORS)


# A stream can simply stall or drop mid-batch — nothing is wrong with the video,
# and the same command usually works on the next try. Worth one retry before
# giving up, rather than losing a video out of a long run.
TRANSIENT_ERRORS = ("did not get any data blocks", "content too short",
                    "connection", "timed out", "timeout", "temporary failure",
                    "unable to connect", "read error", "http error 5",
                    "incomplete", "remote end closed")


def is_transient(error_text):
    """True if a failure looks like a hiccup worth retrying once."""
    low = (error_text or "").lower()
    return any(hint in low for hint in TRANSIENT_ERRORS)


# Nothing about these changes on a retry, so a video that reports one is failed
# immediately instead of spending three rounds proving it.
# YouTube gates requests it thinks are automated, which is what a datacenter IP
# looks like from its side. The apostrophe in "you're not a bot" comes back as a
# curly U+2019 as often as a plain one, so the marker avoids it entirely.
BOT_GATE_ERRORS = ("not a bot", "this helps protect our community",
                   "the page needs to be reloaded")

# What YouTube answers a signed-in request it no longer accepts - cookies that were
# rotated after export, or used from elsewhere. The same request without them may
# well work, so a batch drops the cookies once this happens rather than failing on
# every video.
COOKIES_REJECTED = "the page needs to be reloaded"
use_cookies = [True]
COOKIE_COPIES = {}          # cookies.txt -> (sha1 of its bytes, the private copy yt-dlp gets)


def is_bot_gated(error_text):
    """True when the failure is YouTube refusing the IP, not refusing the video."""
    low = (error_text or "").lower()
    return any(hint in low for hint in BOT_GATE_ERRORS)


PERMANENT_ERRORS = ("private video", "incomplete youtube id", "video is unavailable", "video unavailable",
                    "removed by the uploader", "has been terminated",
                    "sign in to confirm your age", "members-only",
                    "is not a valid url", "unsupported url",
                    "video has been removed", "copyright claim",
                    "upload date is not in range") + BOT_GATE_ERRORS


def failure_status(error_text):
    """How a failure is recorded: "blocked" when YouTube refused this machine's IP,
    "unavailable" when the video itself is gone, "failed" when another go may work.
    A caller has to tell the first two apart - one needs cookies or another IP, the
    other needs nothing."""
    if is_bot_gated(error_text):
        return "blocked"
    return "unavailable" if is_permanent(error_text) else "failed"


def is_permanent(error_text):
    """True when retrying cannot possibly help."""
    low = (error_text or "").lower()
    return any(hint in low for hint in PERMANENT_ERRORS)


# How many times to work through every download route before giving up, and how
# long to wait between those rounds. The waits matter: when YouTube throttles or
# a token goes stale, the same request often succeeds a moment later.
MAX_ATTEMPTS = 3
RETRY_DELAYS = (10, 30)

# What yt-dlp says when nothing lies between --min-height and --max-height.
FORMAT_GONE = "requested format is not available"


def mark_incomplete(folder):
    """Rename media left by a failed download so it can never be mistaken for a
    finished one. A half-finished video (say, the picture with no sound) is the
    right size and plays, which makes it the most misleading thing in the folder.
    """
    marked = []
    for path in [find_video_file(folder)]:
        if path and not os.path.basename(path).startswith("INCOMPLETE_"):
            target = os.path.join(os.path.dirname(path),
                                  "INCOMPLETE_" + os.path.basename(path))
            try:
                os.replace(path, target)
                marked.append(os.path.basename(target))
            except OSError:
                pass
    return marked


def cookies_problem(data):
    """Why yt-dlp could not load a cookies.txt holding these bytes, or None. It gives up
    on a file it cannot parse before making any request, so every video would fail."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "it is not a text file"
    if text.lstrip().startswith(("[", "{")):
        return "it is a JSON export; it has to be in the Netscape cookies.txt format"
    if not any(len(line.split("\t")) >= 7 for line in text.splitlines()):
        return "there are no cookies in it"
    return None


def _remove_quietly(path):
    try:
        os.remove(path)
    except OSError:
        pass


def _remove_cookie_copies():
    for _digest, copy in COOKIE_COPIES.values():
        _remove_quietly(copy)


atexit.register(_remove_cookie_copies)


def remove_stale_copies(folder, age=86400):
    """Remove cookie copies a day old: left by a run killed outright (SIGKILL, or a
    stop on Windows), since a live one is rewritten by every yt-dlp call."""
    try:
        names = [n for n in os.listdir(folder) if n.startswith(".ytdl_cookies_")]
    except OSError:
        return
    for name in names:
        path = os.path.join(folder, name)
        try:
            if time.time() - os.path.getmtime(path) > age:
                os.remove(path)
        except OSError:
            pass


def cookie_flags():
    """--cookies pointing at a private copy of cookies.txt, or nothing.

    yt-dlp writes its cookie jar back into the file it is given, so the user's own
    export would be rewritten by every call - with whatever YouTube rotated or
    cleared in it. The copy sits beside the original, so a run killed outright
    leaves it there rather than in a temp folder; it is replaced when the original
    changes, and removed when the process ends.
    """
    if not use_cookies[0] or not os.path.exists(COOKIES_FILE):
        return []
    path = os.path.abspath(COOKIES_FILE)
    try:
        with open(path, "rb") as f:
            data = f.read()
        digest = hashlib.sha1(data).hexdigest()
        known = COOKIE_COPIES.get(path)
        if known and known[0] == digest and os.path.exists(known[1]):
            return ["--cookies", known[1]]
        problem = cookies_problem(data)
        if problem:
            use_cookies[0] = False
            print(f"  NOTE: cookies.txt cannot be used ({problem}); carrying on without it.")
            return []
        if known:
            _remove_quietly(known[1])
        remove_stale_copies(os.path.dirname(path))
        try:
            fd, copy = tempfile.mkstemp(prefix=".ytdl_cookies_", suffix=".txt",
                                        dir=os.path.dirname(path))
        except OSError:                   # a folder it cannot write to
            fd, copy = tempfile.mkstemp(prefix="ytdl_cookies_", suffix=".txt")
        COOKIE_COPIES[path] = (digest, copy)
        with os.fdopen(fd, "wb") as dst:
            dst.write(data)
    except OSError as e:
        use_cookies[0] = False
        print(f"  NOTE: cookies.txt cannot be read ({e}); carrying on without it.")
        return []
    return ["--cookies", copy]


def drop_rejected_cookies(error_text):
    """True when YouTube just refused cookies.txt: it is dropped for the rest of the
    run, and the caller makes the same request again without it."""
    if (COOKIES_REJECTED not in (error_text or "").lower() or not use_cookies[0]
            or not os.path.exists(COOKIES_FILE)):
        return False
    use_cookies[0] = False
    print("  NOTE: YouTube rejected cookies.txt (\"the page needs to be reloaded\": rotated"
          " after export, or used from another IP); carrying on without it.")
    return True


def base_cmd():
    """Common yt-dlp arguments (cookies added only if the file exists)."""
    cmd = ["yt-dlp"]
    # Let yt-dlp fetch its EJS challenge-solver script so it can solve YouTube's
    # JS "n" challenge (needs a JS runtime like deno/node installed). Without this
    # some videos fail with "This video is not available" / missing formats.
    cmd += ["--remote-components", "ejs:github"]
    if BGUTIL_SCRIPT:
        cmd += ["--extractor-args",
                f"youtubepot-bgutilscript:script_path={BGUTIL_SCRIPT}"]
    cmd += windows_flags()
    return cmd + cookie_flags()


# =================== transcript formatting component =======================
# Adapted from ../Transcript with timestamps/transcribe.py (its "default" and
# "oneword" modes). Kept as a self-contained copy so this repo has no external
# path dependency (and doesn't pull in that script's hard-coded HF token).
#   trans_<name>.txt : grouped, wrapped lines   -> [hh:mm:ss] text
#   words_<name>.txt : one word per line        -> [hh:mm:ss.mmm] word

def format_timestamp(seconds: float) -> str:
    """0.0 -> [00:00:00] (floored to whole seconds)."""
    total = int(seconds or 0)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def format_timestamp_ms(seconds: float) -> str:
    """1.2534 -> [00:00:01.253] (with millisecond decimals)."""
    ms_total = int(round(float(seconds or 0) * 1000))
    h, rem = divmod(ms_total, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"[{h:02d}:{m:02d}:{s:02d}.{ms:03d}]"


def join_segments(parts):
    """Join caption segments into one line.

    Word-timed tracks carry their own leading spaces ('of', ' the', ' FBI,') and
    are simply concatenated. CC-style tracks do not ('WAS', 'UNABLE'), so a space
    is inserted where neither side has one — otherwise they glue into 'WASUNABLE'.
    """
    out = ""
    for part in parts:
        if out and not out.endswith(" ") and not part.startswith(" "):
            out += " "
        out += part
    return out.strip()


def group_words_into_lines(words):
    """words: list of (start, end, word_text) -> list of (start, line_text)."""
    lines, current, start = [], [], None
    for w_start, _w_end, text in words:
        if start is None:
            start = w_start
        if current and len(join_segments(current + [text])) > TRANSCRIPT_WRAP:
            lines.append((start, join_segments(current)))
            current, start = [text], w_start
        else:
            current.append(text)
    if current:
        lines.append((start, join_segments(current)))
    return lines


def parse_json3_words(path):
    """Parse a YouTube json3 caption file into [(start_sec, end_sec, text), ...].

    The end time is kept because closed-caption tracks time a whole phrase at
    once; word_spans() needs the phrase's span to place the words inside it.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    words = []
    for ev in data.get("events", []):
        segs = ev.get("segs")
        if not segs:
            continue
        base = ev.get("tStartMs", 0) or 0
        span_end = base + (ev.get("dDurationMs") or 0)
        offsets = [s.get("tOffsetMs") or 0 for s in segs]
        for i, s in enumerate(segs):
            # Flatten any newline/tab inside a segment to a single space: CC-style
            # tracks pack whole lines (with line breaks) into one segment, which
            # would otherwise spill across output lines with no timestamp. A
            # single leading space is preserved — word segments rely on it.
            text = re.sub(r"\s+", " ", s.get("utf8", "") or "")
            if not text.strip():               # blanks / newline-only separators
                continue
            start = base + offsets[i]
            # This segment runs until the next one in the event, or to the end
            # of the event when it is the last.
            nxt = next((o for o in offsets[i + 1:] if o > offsets[i]), None)
            end = base + nxt if nxt is not None else span_end
            words.append((start / 1000.0,
                          end / 1000.0 if end > start else None,
                          text))
    return words


def one_line(text):
    """Collapse a caption's whitespace so it cannot break the one-line-per-
    timestamp format. Both writers apply it, so that guarantee holds whatever
    the caption track contained."""
    return re.sub(r"\s+", " ", text or "").strip()


def trans_lines(words):
    """Default style: grouped, wrapped lines with [hh:mm:ss] timestamps."""
    for start, text in group_words_into_lines(words):
        text = one_line(text)
        if text:
            yield f"{format_timestamp(start)} {text}"


def write_trans_txt(out_txt, words):
    """Write the default-style transcript file."""
    with open(out_txt, "w", encoding="utf-8") as f:
        for line in trans_lines(words):
            f.write(line + "\n")


# A caption is word-level when almost every entry holds a single word. Auto-
# captions look like that; broadcast closed-caption tracks time a whole phrase
# at once. A small tolerance allows the occasional "New York" style entry.
WORD_LEVEL_TOLERANCE = 0.2


def is_word_level(entries):
    """True when the caption carries a timestamp per word rather than per phrase."""
    texts = [one_line(t) for _s, _e, t in entries]
    texts = [t for t in texts if t]
    if not texts:
        return False
    multi = sum(1 for t in texts if len(t.split()) > 1)
    return multi <= len(texts) * WORD_LEVEL_TOLERANCE


def words_lines(words):
    """Oneword style: one word per line with [hh:mm:ss.mmm] timestamps."""
    for start, _end, text in words:
        word = one_line(text)
        if word:
            yield f"{format_timestamp_ms(start)} {word}"


def write_words_txt(out_txt, words):
    """Write the one-word-per-line transcript file."""
    with open(out_txt, "w", encoding="utf-8") as f:
        for line in words_lines(words):
            f.write(line + "\n")


def write_words_not_found(out_txt, entries):
    """Explain, in the video's own folder, why there is no word-level file.

    Timings are never invented here: spreading a phrase's words across its span
    would put most of them near the right place and some of them seconds away,
    and a file that looks precise while being approximate is worse than an
    honest gap.
    """
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(
            "No word-level transcript is available for this video.\n"
            "\n"
            "YouTube only times captions per word in its auto-generated track.\n"
            "This video does not have one: it carries the broadcast closed-caption\n"
            "tracks (CC1 / DTVCC1) instead, which are timed one phrase at a time.\n"
            "YouTube does not auto-caption a video that already ships captions, so\n"
            "per-word timings do not exist anywhere in the source.\n"
            "\n"
            f"The phrase-level text is in the trans_ file next to this one "
            f"({len(entries)} caption lines).\n"
            "\n"
            "To get real word-level timings, transcribe the .mp4 locally with\n"
            "Whisper — for example:\n"
            "    python3 transcribe.py oneword \"<folder containing this video>\"\n"
        )


# ======================== thumbnail & description ==========================
def thumb_flags():
    """yt-dlp flags to save the thumbnail as a .jpg next to the video."""
    if not SAVE_THUMBNAIL:
        return []
    # YouTube serves .webp; converting keeps it openable everywhere.
    return ["--write-thumbnail", "--convert-thumbnails", "jpg"]


def find_thumbnail(folder):
    """Path to the saved thumbnail, if there is one."""
    try:
        for name in sorted(os.listdir(folder)):
            if name.lower().endswith((".jpg", ".jpeg", ".png", ".webp")):
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def find_description(folder):
    """Path to the saved description file, if there is one."""
    try:
        for name in sorted(os.listdir(folder)):
            if name.startswith("description_") and name.endswith(".txt"):
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def write_description(folder, info):
    """Save the video's own description, with the details worth keeping on top.

    The description comes from the metadata already fetched, so this costs no
    extra request. A video with an empty description gets no file.
    """
    if not SAVE_DESCRIPTION:
        return None
    text = ((info or {}).get("description") or "").strip()
    if not text:
        return None
    base = os.path.basename(folder.rstrip("/\\"))
    path = os.path.join(folder, f"description_{base}.txt")

    date = (info.get("upload_date") or "")
    if len(date) == 8:
        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    header = [
        f"Title:    {info.get('title') or ''}",
        f"Channel:  {info.get('channel') or info.get('uploader') or ''}",
        f"Uploaded: {date}",
        f"Duration: {info.get('duration') or ''}s",
        f"Views:    {info.get('view_count') or ''}",
        f"Link:     {info.get('webpage_url') or ''}",
        "",
        "-" * 60,
        "",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(header) + text + "\n")
    return path


def build_extras(folder, url, info):
    """Make sure the thumbnail and description are present. Returns their paths.

    The thumbnail normally arrives with the video; it is only fetched here when
    it is missing, which is what happens for videos downloaded before these were
    switched on.
    """
    thumb = find_thumbnail(folder) if SAVE_THUMBNAIL else None
    log_debug("extras_start", stage="extras", url=url, folder=rel(folder),
              thumbnail=SAVE_THUMBNAIL, description=SAVE_DESCRIPTION)
    if SAVE_THUMBNAIL and not thumb:
        name = os.path.basename(folder.rstrip("/\\"))
        # Fetching a thumbnail on its own runs into the same bot check the video
        # does, so it gets the same escalation to the PO-token client.
        routes = [PO_TOKEN_FLAGS] if po_token_first[0] else [[], PO_TOKEN_FLAGS]
        for route_no, extra in enumerate(routes, 1):
            for attempt_no in range(1, 3):
                cmd = base_cmd() + ["--skip-download", "--no-warnings"] + thumb_flags() \
                    + list(extra) + ["-o", output_template(folder, name), "--", url]
                out = run_logged("thumbnail", cmd, capture_output=True, text=True)
                thumb = find_thumbnail(folder)
                log_debug("thumbnail_attempt", stage="thumbnail", url=url, folder=rel(folder),
                          route=route_no, attempt=attempt_no, found=bool(thumb),
                          returncode=out.returncode)
                if thumb or not drop_rejected_cookies(out.stderr):
                    break
            if thumb:
                break

    desc = find_description(folder) if SAVE_DESCRIPTION else None
    if SAVE_DESCRIPTION and not desc:
        desc = write_description(folder, info)
    log_debug("extras_finish", stage="extras", url=url, folder=rel(folder),
              thumbnail=bool(thumb), description=bool(desc))
    return thumb, desc


# ============================== comments ===================================
def comment_extractor_args(client=()):
    """The one --extractor-args pair carrying the route's client AND the comment
    settings.

    They have to travel together. yt-dlp keys extractor arguments by extractor,
    so a second "youtube:" pair replaces the first rather than adding to it -
    passing the player_client in one pair and max_comments in another silently
    drops whichever came first.

    max_comments is total,parents,replies,replies-per-thread. Capping the
    parents and taking no replies is both what is wanted by default and the
    fastest route: measured 7.4s for 100 top-level comments, against 10.8s for
    a plain "100 comments" that came back as 28 real ones and 72 replies.
    """
    parents = COMMENT_LIMIT if COMMENT_LIMIT else "all"
    replies = "all" if COMMENT_REPLIES else "0"
    parts = []
    for item in client:
        if isinstance(item, str) and item.startswith("youtube:"):
            parts.append(item.split(":", 1)[1])      # keep the route's client
    parts.append(f"comment_sort={COMMENT_SORT}")
    parts.append(f"max_comments=all,{parents},{replies},{replies}")
    return ["--extractor-args", "youtube:" + ";".join(parts)]


def owns_info_json(name, folder_name):
    """Whether this .info.json is the one yt-dlp wrote for this folder.

    output_template() names it after the folder, so "My Clip.info.json" belongs
    here and so would "My Clip.en.info.json". A folder whose own name contains
    dots still matches, which splitting on "." would not manage.
    """
    if not name.endswith(".info.json"):
        return False
    return name == f"{folder_name}.info.json" or name.startswith(f"{folder_name}.")


def find_comments_file(folder):
    """Path to the saved comments_*.txt in the folder, or None."""
    try:
        for name in sorted(os.listdir(folder)):
            if name.startswith("comments_") and name.endswith(".txt"):
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def take_comments(folder):
    """Lift the comments out of the .info.json this run wrote, then delete it.

    --write-comments has nowhere else to put them, and the file is large whether
    or not the comments are: 923 KB for ten of them, because the bulk is format
    metadata. Leaving it behind would put about a megabyte of noise in every
    video folder, so it is removed the way take_caption removes the json3.

    Only files named after the folder are touched. yt-dlp writes exactly that
    one (see output_template), and anything else ending in .info.json was put
    there by something other than this run - it is not ours to delete.
    """
    name = os.path.basename(folder.rstrip("/\\"))
    found = []
    try:
        found = [os.path.join(folder, n) for n in sorted(os.listdir(folder))
                 if owns_info_json(n, name)]
    except OSError:
        return []
    comments = []
    for path in found:
        try:
            with open(path, "r", encoding="utf-8") as f:
                comments = (json.load(f).get("comments") or []) or comments
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    for path in found:
        try:
            os.remove(path)
        except OSError:
            pass
    return comments


def thread_comments(comments):
    """Group a flat comments array into [(top_level, [replies...]), ...].

    The parent field is the only thing tying a reply to its comment ("root" for
    a top-level one, else the parent's id), and yt-dlp returns them flat. A
    reply whose parent is not in the batch - which happens when the parents are
    capped - is promoted to top level rather than dropped, so no fetched comment
    is silently lost.
    """
    tops = [c for c in comments if (c.get("parent") or "root") == "root"]
    by_id = {c.get("id"): c for c in tops}
    replies = {}
    orphans = []
    for c in comments:
        parent = c.get("parent") or "root"
        if parent == "root":
            continue
        if parent in by_id:
            replies.setdefault(parent, []).append(c)
        else:
            orphans.append(c)
    return [(c, replies.get(c.get("id"), [])) for c in tops + orphans]


def comment_time(comment):
    """A readable posting time for one comment, in Pakistan time.

    Falls back to yt-dlp's own relative wording ("2 weeks ago") when there is no
    integer timestamp, and to NA when there is neither.
    """
    stamp = (comment or {}).get("timestamp")
    if stamp:
        try:
            return (datetime.datetime.fromtimestamp(int(stamp), PKT)
                    .strftime("%d %b %Y, %I:%M %p PKT"))
        except (TypeError, ValueError, OSError, OverflowError):
            pass
    return str((comment or {}).get("_time_text") or "NA")


# Everything except tab/newline/carriage-return, which one_line() already folds
# into spaces. An escape sequence or a stray control character in a comment
# would otherwise rewrite the file's shape around it - enough to make a reply
# read as a top-level comment, or to repaint the terminal of whoever cats it.
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def safe_text(value):
    """Text with control characters removed and its line breaks kept.

    For data handed to a caller, where a comment's own paragraphs are worth
    keeping - unlike a laid-out text file, where they are not (see clean_text).
    """
    return CONTROL_RE.sub("", str(value or ""))


def clean_text(value):
    """One line of text, with control characters removed."""
    return one_line(CONTROL_RE.sub(" ", str(value or "")))


def write_comments_txt(folder, info, comments):
    """Write comments_<folder name>.txt and return its path, or None.

    Threaded rather than flat, because a reply read without its comment is
    usually meaningless. Plain ASCII in the furniture; the comment text itself
    is whatever the commenter wrote.
    """
    if not comments:
        return None
    name = os.path.basename(folder.rstrip("/\\"))
    path = os.path.join(folder, f"comments_{name}.txt")
    threads = thread_comments(comments)
    tops = len(threads)
    replies = len(comments) - tops

    def block(comment, indent):
        pad = " " * indent
        who = clean_text(comment.get("author")) or "unknown"
        marks = ""
        if comment.get("author_is_uploader"):
            marks += "  (uploader)"
        if comment.get("author_is_verified"):
            marks += "  (verified)"
        if comment.get("is_pinned"):
            marks += "  (pinned)"
        likes = comment.get("like_count")
        if isinstance(likes, int):
            likes = f"{likes:,} like" + ("" if likes == 1 else "s")
        else:
            likes = "likes NA"
        lines = [f"{pad}{who}{marks}", f"{pad}  {likes} | {comment_time(comment)}"]
        text = clean_text(comment.get("text"))
        lines += [f"{pad}  {text}" if text else f"{pad}  (no text)"]
        return "\n".join(lines)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"Comments for: {clean_text(info.get('title'))}\n")
        f.write(f"Link:         {clean_text(info.get('webpage_url'))}\n")
        f.write(f"Sort:         {COMMENT_SORT}\n")
        f.write(f"Fetched:      {tops} comment(s), {replies} reply(ies)"
                f"{'' if COMMENT_REPLIES else '  (replies off)'}\n")
        f.write("\n" + "-" * 60 + "\n")
        for number, (top, kids) in enumerate(threads, 1):
            f.write(f"\n[{number}] {block(top, 0)}\n")
            for kid in kids:
                f.write(f"\n{block(kid, 6)}\n")
    return path


def fetch_comments(folder, url):
    """Run the one yt-dlp command that leaves the comments in an .info.json.

    Gets the same route escalation every lone fetch here does, because asking
    for comments on their own meets the same bot check the video download does.
    """
    name = os.path.basename(folder.rstrip("/\\"))
    routes = [PO_TOKEN_FLAGS] if po_token_first[0] else [[], PO_TOKEN_FLAGS]
    log_debug("comments_start", stage="comments", url=url, folder=rel(folder),
              sort=COMMENT_SORT, limit=COMMENT_LIMIT, replies=COMMENT_REPLIES,
              routes=len(routes))
    for route_no, extra in enumerate(routes, 1):
        # Two attempts per route, because the second is the point of the first:
        # when YouTube rejects cookies.txt, drop_rejected_cookies() drops it and
        # the identical request is worth making again without it.
        for attempt in (1, 2):
            cmd = base_cmd() + [
                "--skip-download", "--no-playlist", "--ignore-no-formats-error",
                "--write-comments", "--write-info-json", "--no-warnings",
                "-o", output_template(folder, name),
            ] + comment_extractor_args(extra) + ["--", url]
            try:
                out = run_logged("comments", cmd, capture_output=True, text=True,
                                 timeout=COMMENT_TIMEOUT)
            except subprocess.TimeoutExpired:
                # A video with tens of thousands of comments hits the cap rather
                # than the end. The next route is still worth trying, and letting
                # this escape would put the whole command line in the job log.
                log_debug("comments_attempt", stage="comments", url=url,
                          folder=rel(folder), route=route_no, attempt=attempt,
                          timed_out=True, found=0)
                take_comments(folder)          # leave no half-written .info.json
                break
            comments = take_comments(folder)
            log_debug("comments_attempt", stage="comments", url=url, folder=rel(folder),
                      route=route_no, attempt=attempt,
                      returncode=getattr(out, "returncode", None),
                      found=len(comments), stderr_tail=getattr(out, "stderr", ""))
            if comments:
                return comments
            if attempt == 1 and drop_rejected_cookies(getattr(out, "stderr", "") or ""):
                continue
            break
    return []


def build_comments(folder, url, info):
    """Save a video's comments. Returns (path, count, state) and never raises.

    Comments are an extra, so nothing here may fail a video: a run that lost its
    comments to a rate limit still has the video, the transcript and the rest.
    state is "off", "none" (the video has none, or has them disabled - a normal
    state, not an error), "saved", or "failed".
    """
    if not EXTRACT_COMMENTS:
        return None, 0, "off"
    existing = find_comments_file(folder)
    if existing:
        return existing, 0, "saved"
    try:
        comments = fetch_comments(folder, url)
        if not comments:
            log_debug("comments_finish", stage="comments", url=url,
                      folder=rel(folder), saved=False, count=0)
            return None, 0, "none"
        path = write_comments_txt(folder, info or {}, comments)
        COMMENT_COUNT[0] += len(comments)
        log_debug("comments_finish", stage="comments", url=url, folder=rel(folder),
                  saved=bool(path), count=len(comments))
        return path, len(comments), "saved" if path else "none"
    except Exception as e:                      # never fail the video over this
        note(f"      (comments unavailable: {e})")
        log_debug("comments_finish", stage="comments", url=url, folder=rel(folder),
                  saved=False, count=0, error=str(e))
        return None, 0, "failed"


# ============================ transcript helpers ===========================
def offers_json3(entries):
    """True unless a caption track's formats are listed and json3 is not one of them.
    Some auto tracks come as vtt only; asking for json3 then gets a vtt, which gives
    no transcript - while the manual track of the same code would."""
    exts = {e.get("ext") for e in entries or [] if e.get("ext")}
    return not exts or "json3" in exts


def best_track_in(tracks):
    """The closest match to SUB_LANG among one set of caption tracks.

    Videos do not all publish English as plain "en" — it can be "en-orig", a
    regional "en-US", or a closed-caption code like "en-uYU-mmqFLq8". Matching
    with a wildcard would drag in every machine-translated "English from French"
    track too: dozens of downloads, and YouTube answers with HTTP 429. So one
    explicit code is chosen instead.
    """
    tracks = {code: entries for code, entries in tracks.items() if offers_json3(entries)}
    if SUB_LANG in tracks:
        return SUB_LANG
    if f"{SUB_LANG}-orig" in tracks:
        return f"{SUB_LANG}-orig"

    def track_name(code):
        entries = tracks.get(code) or []
        return (entries[0].get("name", "") if entries else "").lower()

    variants = sorted(c for c in tracks if c.startswith(f"{SUB_LANG}-"))
    # "X from Y" entries are machine translations of another language; a plain
    # name means this is the video's own English track.
    native = [c for c in variants if " from " not in track_name(c)]
    return (native or variants or [None])[0]


def resolve_sub_lang(info):
    """Choose the caption track to fetch: (code, "auto"|"manual"|None).

    Auto-captions are timed per WORD, which is what words_<name>.txt is for.
    Broadcast closed-caption tracks are timed per phrase, so they can only ever
    produce phrase-level output — they are the fallback, never the default.
    The caller keeps the two apart, because asking yt-dlp for both kinds at once
    lets it hand back the manual track and silently lose the word timings.
    """
    auto = (info or {}).get("automatic_captions") or {}
    manual = (info or {}).get("subtitles") or {}
    pick = best_track_in(auto)
    if pick:
        return pick, "auto"
    pick = best_track_in(manual)
    return (pick, "manual") if pick else (None, None)


def sub_flags(lang, source):
    """yt-dlp flags to fetch exactly one caption track as json3.

    Only the kind of track we actually chose is enabled: passing both
    --write-auto-subs and --write-subs makes yt-dlp prefer the manual track for
    a language, which would quietly downgrade word timings to phrase timings.
    """
    if not DOWNLOAD_SUBS or not lang:
        return []
    kind = "--write-auto-subs" if source == "auto" else "--write-subs"
    return [kind, "--sub-langs", lang, "--sub-format", "json3"]


# Not .srt: the first version of this script kept its transcripts as <name>.en.srt.
CAPTION_EXTS = (".json3", ".vtt", ".ttml", ".srv1", ".srv2", ".srv3")


def clear_captions(folder):
    """Remove every caption file yt-dlp left in the folder - including a vtt it fell
    back to when a track had no json3, which gives no transcript at all."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.lower().endswith(CAPTION_EXTS):
            _remove_quietly(os.path.join(folder, name))


def find_json3(folder):
    """Every downloaded .json3 caption in the folder."""
    try:
        return sorted(os.path.join(folder, n) for n in os.listdir(folder)
                      if n.lower().endswith(".json3"))
    except OSError:
        return []


def pick_best_json3(paths):
    """Choose the caption closest to the requested language: the exact code
    first, then the original-audio track, then any other variant."""
    def rank(path):
        name = os.path.basename(path)
        if name.endswith(f".{SUB_LANG}.json3"):
            return (0, len(name))
        if name.endswith(f".{SUB_LANG}-orig.json3"):
            return (1, len(name))
        return (2, len(name))
    return min(paths, key=rank) if paths else None


def find_transcript_file(folder):
    """Return the trans_*.txt transcript in the folder, if one exists."""
    try:
        for name in os.listdir(folder):
            if name.startswith("trans_") and name.endswith(".txt"):
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


def take_caption(folder):
    """Read whatever json3 caption is sitting in the folder, and remove it.
    The caption file is only ever a means to the transcript files."""
    found = find_json3(folder)
    best = pick_best_json3(found)
    entries = parse_json3_words(best) if best else []
    for j in found:
        try:
            os.remove(j)
        except OSError:
            pass
    return entries


def write_transcripts(folder, entries):
    """Write the transcript files for one caption. Returns (trans, words).

    trans_<name>.txt is always written. Its companion depends on what the
    caption actually contains: words_<name>.txt when the track is timed per
    word, and words_not_found_<name>.txt when it is only timed per phrase, so a
    folder never leaves you guessing which kind you got.
    """
    if not entries:
        return None, None
    base = os.path.basename(folder.rstrip("/\\"))   # strip both separators (Mac + Windows)
    trans_path = os.path.join(folder, f"trans_{base}.txt")
    write_trans_txt(trans_path, entries)

    if is_word_level(entries):
        words_path = os.path.join(folder, f"words_{base}.txt")
        write_words_txt(words_path, entries)
    else:
        words_path = os.path.join(folder, f"words_not_found_{base}.txt")
        write_words_not_found(words_path, entries)
    return trans_path, words_path


def make_transcripts(folder):
    """Turn the caption already in the folder into the transcript files."""
    return write_transcripts(folder, take_caption(folder))


def update_info_transcripts(folder, trans, words):
    """Add/refresh the Transcript: and Words: lines in an existing videoinfo.txt."""
    info = os.path.join(folder, "videoinfo.txt")
    if not os.path.exists(info):
        return
    try:
        with open(info, "r", encoding="utf-8") as f:
            kept = [ln for ln in f.read().splitlines()
                    if not ln.startswith(("Transcript:", "Words:"))]
    except OSError:
        return
    kept.append(f"Transcript: {os.path.basename(trans) if trans else 'none'}")
    kept.append(f"Words:      {os.path.basename(words) if words else 'none'}")
    with open(info, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")


def choose_caption(url, info):
    """Decide which caption track to download: (lang, source, client_flags).

    YouTube's metadata is not consistent between player clients — the default
    one sometimes reports no auto-captions for a video that plainly has them.
    Settling for a phrase-timed broadcast track when word timings exist would
    quietly cost words_<name>.txt its whole point, so before falling back to a
    manual track we ask the PO-token client for a second opinion, and download
    through that same client if it is the one that can see the track.
    """
    lang, source = resolve_sub_lang(info)
    if source == "auto" or not DOWNLOAD_SUBS:
        return lang, source, []
    richer, _ = fetch_info(url, PO_TOKEN_FLAGS, attempts=1)
    if richer:
        better, better_source = resolve_sub_lang(richer)
        if better_source == "auto":
            return better, better_source, PO_TOKEN_FLAGS
    return lang, source, []


def fetch_caption(folder, url, lang, source, client=()):
    """Download one caption track into the folder. True if a json3 arrived.

    Fetching a caption on its own — which is what a backfill does — runs into
    the same bot check the video download does, so it gets the same escalation
    to the PO-token client. A caller that already knows which client can see the
    track passes it, and that one is used as given.
    """
    flags = sub_flags(lang, source)
    if not flags:
        return False
    name = os.path.basename(folder.rstrip("/\\"))
    if client:
        routes = [list(client)]
    else:
        # Ask the web client first so bgutil can mint the video-bound subtitle
        # PO token. Keep the existing mweb/default routes as fallbacks: older
        # installations may not have the provider plugin, and some videos are
        # still exposed only through those clients.
        routes = ([list(SUBTITLE_PO_TOKEN_FLAGS)] if BGUTIL_SCRIPT else [])
        routes += [PO_TOKEN_FLAGS] if po_token_first[0] else [[], PO_TOKEN_FLAGS]
    log_debug("caption_start", stage="caption", url=url, folder=rel(folder),
              language=lang, source=source, routes=len(routes), known_client=bool(client))
    for route_no, extra in enumerate(routes, 1):
        for attempt_no in range(1, 3):
            cmd = base_cmd() + ["--skip-download", "--ignore-no-formats-error"] + flags + list(extra) + [
                "--no-warnings", "-o", output_template(folder, name), "--", url,
            ]
            out = run_logged("caption", cmd, capture_output=True, text=True)
            found = find_json3(folder)
            log_debug("caption_attempt", stage="caption", url=url, folder=rel(folder),
                      language=lang, source=source, route=route_no, attempt=attempt_no,
                      client=extra, returncode=out.returncode, found=bool(found),
                      stderr_tail=out.stderr)
            if found:
                log_debug("caption_finish", stage="caption", url=url, folder=rel(folder),
                          success=True, language=lang, source=source)
                return True
            if not drop_rejected_cookies(out.stderr):
                break
    log_debug("caption_finish", stage="caption", url=url, folder=rel(folder),
              success=False, language=lang, source=source)
    return False


# How many caption tracks to try before settling. Enough to exhaust a video's
# English tracks, few enough that a long batch never looks like hammering.
MAX_CAPTION_TRIES = 4


def caption_candidates(url, info):
    """Every English caption track worth trying, best first.

    Auto-captions come first because they are the ones timed per word. If the
    metadata shows none, the PO-token client is asked for a second opinion — it
    sometimes sees tracks the default client does not — and its tracks are tried
    through that same client. Broadcast tracks come last: they still make a
    perfectly good trans_ file when nothing better exists.
    """
    seen = set()
    sources = [(info, ())]
    if not best_track_in((info or {}).get("automatic_captions") or {}):
        richer, _ = fetch_info(url, PO_TOKEN_FLAGS, attempts=1)
        if richer:
            sources.append((richer, tuple(PO_TOKEN_FLAGS)))

    for kind, source in (("automatic_captions", "auto"), ("subtitles", "manual")):
        for meta, client in sources:
            tracks = (meta or {}).get(kind) or {}
            ordered = sorted(tracks, key=lambda c: (c != SUB_LANG, c))
            for code in ordered:
                if not (code == SUB_LANG or code.startswith(f"{SUB_LANG}-")):
                    continue
                if (code, source) in seen or not offers_json3(tracks[code]):
                    continue
                seen.add((code, source))
                yield code, source, list(client)


def build_transcripts(folder, url, info):
    """trans_/words_ for a video (see find_transcripts); no caption file stays behind."""
    if not DOWNLOAD_SUBS:
        return None, None
    started = time.monotonic()
    log_debug("transcript_start", stage="transcript", url=url, folder=rel(folder),
              language=SUB_LANG)
    try:
        result = find_transcripts(folder, url, info)
        log_debug("transcript_finish", stage="transcript", url=url, folder=rel(folder),
                  success=bool(result[0]), words=bool(result[1]),
                  duration=round(time.monotonic() - started, 3))
        return result
    finally:
        clear_captions(folder)


def required_caption_error():
    """Error used when a required transcript could not be produced."""
    return f"Could not download required '{SUB_LANG}' captions"


def find_transcripts(folder, url, info):
    """Produce trans_/words_ for a video, fetching the caption if needed.

    A caption normally arrives with the video, so usually there is nothing to
    fetch. If what we have is not timed per word, the other English tracks are
    tried before giving up — words_not_found is meant to be the last word on a
    video, not the first track's verdict. The best phrase-timed track found
    along the way is kept, so a video never loses its trans_ file either.
    """
    if not DOWNLOAD_SUBS:
        return None, None

    return write_transcripts(folder, best_caption_entries(folder, url, info))


def best_caption_entries(folder, url, info):
    """The best caption a video has, as parsed entries. Writes no transcript.

    This is the choosing half of find_transcripts: it fetches into `folder` and
    leaves no caption file behind, but produces no trans_/words_ files either -
    so a caller that wants the text itself rather than the files can use it.
    """
    entries = take_caption(folder)          # whatever came down with the video
    if is_word_level(entries):
        return entries

    fallback = entries
    tried = 0
    for lang, source, client in caption_candidates(url, info):
        if tried >= MAX_CAPTION_TRIES:
            break
        tried += 1
        if not fetch_caption(folder, url, lang, source, client):
            continue
        got = take_caption(folder)
        if is_word_level(got):
            return got
        fallback = fallback or got
    return fallback


def update_info_counts(folder, info):
    """Add/refresh Channel, Views and Subscribers in an existing videoinfo.txt."""
    path = os.path.join(folder, "videoinfo.txt")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    fresh = {
        "Channel:": f"Channel: {(info or {}).get('channel') or (info or {}).get('uploader') or 'NA'}",
        "Views:": f"Views:   {count_or_na((info or {}).get('view_count'))}",
        "Subscribers:": f"Subscribers: {count_or_na((info or {}).get('channel_follower_count'))}",
        "Uploaded:": f"Uploaded: {upload_time_pkt(info)}",
        "Category:": f"Category: {category_or_na(info)}",
    }
    kept, seen = [], set()
    for line in lines:
        key = next((k for k in fresh if line.startswith(k)), None)
        if key:
            if key not in seen:      # replace in place, keeping the file's order
                kept.append(fresh[key])
                seen.add(key)
        else:
            kept.append(line)
    # Anything the file did not have yet goes after the Link line.
    missing = [fresh[k] for k in fresh if k not in seen]
    if missing:
        at = next((i for i, l in enumerate(kept) if l.startswith("Link:")), 0) + 1
        kept[at:at] = missing
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")


def update_info_extras(folder, thumbnail, description):
    """Add/refresh the Thumbnail: and Description: lines in an existing videoinfo.txt.

    Only the lines this run is going to rewrite are dropped. --transcript forces
    SAVE_THUMBNAIL off, and removing a line it then declines to replace would
    erase a finished download's record of a thumbnail it really does have.
    """
    info = os.path.join(folder, "videoinfo.txt")
    if not os.path.exists(info):
        return
    replacing = tuple(label for label, on in (("Thumbnail:", SAVE_THUMBNAIL),
                                              ("Description:", SAVE_DESCRIPTION)) if on)
    if not replacing:
        return
    try:
        with open(info, "r", encoding="utf-8") as f:
            kept = [ln for ln in f.read().splitlines()
                    if not ln.startswith(replacing)]
    except OSError:
        return
    if SAVE_THUMBNAIL:
        kept.append(f"Thumbnail:  {os.path.basename(thumbnail) if thumbnail else 'none'}")
    if SAVE_DESCRIPTION:
        kept.append(f"Description: {os.path.basename(description) if description else 'none'}")
    with open(info, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")


def update_info_comments(folder, path, count, state):
    """Add or refresh the single Comments: line in an existing videoinfo.txt.

    "off" means this run was never asked for comments, so whatever an earlier
    run recorded stays: replacing it with "none" would claim the video has no
    comments when the file sitting beside it says otherwise.
    """
    if state == "off":
        return
    info_path = os.path.join(folder, "videoinfo.txt")
    if not os.path.exists(info_path):
        return
    try:
        with open(info_path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return
    line = f"Comments: {comments_line(path, count, state)}"
    old = next((ln for ln in lines if ln.startswith("Comments:")), "")
    # A re-run that merely found the file already there reports a count of 0,
    # because it fetched nothing. The count the original fetch recorded is the
    # true one, so for the same file the old line wins.
    if path and not count and old.startswith(f"Comments: {os.path.basename(path)}"):
        line = old
    kept = [ln for ln in lines if not ln.startswith("Comments:")]
    kept.append(line)
    try:
        with open(info_path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + "\n")
    except OSError:
        pass


def existing_transcripts(folder):
    """The transcript files already in the folder, as (trans, words).

    (None, None) when there is no trans_ file. Used to recognise work that is
    already done, so a run asked for something else about the same video - its
    comments, say - does not fetch the caption all over again.
    """
    existing = find_transcript_file(folder)
    if not existing:
        return None, None
    name = os.path.basename(existing)
    for prefix in ("words_", "words_not_found_"):
        companion = os.path.join(folder, name.replace("trans_", prefix, 1))
        if os.path.exists(companion):
            return existing, companion
    return existing, None


def files_size(*paths):
    """Total bytes of the named files, skipping any that are absent."""
    total = 0
    for path in paths:
        try:
            total += os.path.getsize(path) if path else 0
        except OSError:
            pass
    return total


def download_subs(folder, url):
    """Backfill: build the transcripts for an already-downloaded video."""
    if not DOWNLOAD_SUBS:
        return None, None
    trans, words = existing_transcripts(folder)
    if trans:
        return trans, words
    info, _ = fetch_info(url)          # tells us which caption track to ask for
    return build_transcripts(folder, url, info)


# The metadata lookup gets its own attempts. A hiccup here ends a video before
# the download retries can do anything about it, so it is worth a couple of goes.
INFO_ATTEMPTS = 3
INFO_RETRY_DELAY = 5


def fetch_info_once(url: str, extra_flags=()):
    """One metadata lookup. Returns (info, error)."""
    cmd = base_cmd() + ["--skip-download", "--dump-single-json",
                        "--no-warnings"] + list(extra_flags) + ["--", url]
    try:
        out = run_logged("metadata", cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "Timed out while fetching video info"
    except OSError as e:
        return None, f"Could not run yt-dlp: {e}"
    if out.returncode != 0:
        msg = out.stderr.strip().splitlines()
        return None, (msg[-1] if msg else "Failed to fetch video info")
    
    if "not in range" in out.stderr.lower() or "dateafter" in out.stderr.lower():
        return None, "upload date is not in range"

    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError:
        return None, "Could not parse video info (invalid JSON from yt-dlp)"


def fetch_info(url: str, extra_flags=(), attempts=INFO_ATTEMPTS):
    """Fetch video metadata (title, id, captions), retrying a couple of times.

    Everything about a video hangs off this call, so one rate-limited or dropped
    request should not be the end of it. Permanently gone videos still fail on
    the first try. The last attempt switches to the PO-token client, which
    sometimes resolves metadata the default client cannot.
    """
    err = None
    flags = list(extra_flags)
    for attempt in range(1, attempts + 1):
        info, err = fetch_info_once(url, flags)
        if info:
            return info, None
        if drop_rejected_cookies(err):
            info, err = fetch_info_once(url, flags)
            if info:
                return info, None
        if is_bot_gated(err) and not extra_flags:
            # The gate is on this IP, but the mweb client sometimes still gets
            # through. One immediate try; if it works, the rest of the batch starts
            # with that client too.
            info, _ = fetch_info_once(url, PO_TOKEN_FLAGS)
            if info:
                po_token_first[0] = True
                return info, None
            return None, err
        if is_permanent(err):
            return None, err
        if attempt < attempts:
            if attempt == attempts - 1 and not extra_flags:
                flags = list(PO_TOKEN_FLAGS)
            note(f"      info lookup failed ({(err or '').strip()[:70]}); "
                 f"retry {attempt + 1}/{attempts}...")
            time.sleep(INFO_RETRY_DELAY)
    return None, err


def probe_height(filepath: str):
    """Use ffprobe to get the real height (e.g. 1080) of the downloaded file."""
    try:
        out = run_logged("ffprobe",
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", filepath],
            capture_output=True, text=True, timeout=30,
        )
        h = out.stdout.strip().splitlines()
        return int(h[0]) if h and h[0].isdigit() else None
    except Exception:
        return None


def find_video_file(folder: str):
    """Return the path to the finished video file in a folder, if any.

    Media left over from a failed attempt is prefixed INCOMPLETE_ and ignored,
    so it is never mistaken for a usable download. When a folder holds more than
    one video — an older download made under different naming, say — the one
    named after the folder wins, so the answer never depends on listing order.
    """
    exts = (".mp4", ".mkv", ".webm", ".mov")
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    wanted = os.path.basename(folder.rstrip("/\\"))
    candidates = [n for n in names
                  if n.lower().endswith(exts) and not n.startswith("INCOMPLETE_")]
    for name in candidates:
        if os.path.splitext(name)[0] == wanted:
            return os.path.join(folder, name)
    # Otherwise the first finished one: "<title>.f137.mp4" and "<title>.temp.mp4" are
    # streams yt-dlp has not merged yet, and would sort ahead of "<title>.mp4".
    finished = [n for n in candidates if not re.search(r"\.(f\d+|temp)\.[A-Za-z0-9]+$", n)]
    return os.path.join(folder, finished[0]) if finished else None


# ======================= identity, layout & resume =========================
# A YouTube video is identified by its 11-char id, never by the raw link: the
# same video is often listed twice with different tracking suffixes
# (…?v=ABC and …?v=ABC&pp=XYZ). Keying on the id makes those one video.
VIDEO_ID_RE = re.compile(r"(?:v=|/shorts/|/embed/|/live/|youtu\.be/)([A-Za-z0-9_-]{11})")


def watch_url(vid):
    """The canonical watch link for a video id - the one form the whole tool uses."""
    return f"https://www.youtube.com/watch?v={vid}"


def video_id_from_url(url):
    """Extract the 11-char video id from any YouTube URL form, else None."""
    m = VIDEO_ID_RE.search(url or "")
    return m.group(1) if m else None


def read_info(folder):
    """Parse a folder's videoinfo.txt into {field: value}. Empty dict if none."""
    path = os.path.join(folder, "videoinfo.txt")
    fields = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()
    except OSError:
        pass
    return fields


def channel_dir(info):
    """Parent folder a video belongs in: the base dir, or <base>/<Channel Name>
    when --channel is on."""
    if not CHANNEL_MODE:
        return DOWNLOAD_DIR
    name = (info or {}).get("channel") or (info or {}).get("uploader") or ""
    return os.path.join(DOWNLOAD_DIR, sanitize(name) or "Unknown Channel")


def choose_folder(parent, folder_name, vid):
    """Pick the output folder for a video: <parent>/<title>.

    If that folder already exists but holds a DIFFERENT video, fall back to
    "<title> [<id>]" so two same-titled videos never clobber each other.
    The same video re-run reuses its folder (the download overwrites).
    """
    folder = os.path.join(parent, folder_name)
    if os.path.isdir(folder) and vid:
        existing = video_id_from_url(read_info(folder).get("Link", ""))
        if existing and existing != vid:
            return os.path.join(parent, f"{folder_name} [{vid}]")
    return folder


# video id -> folder of a finished download. Built once at startup, then kept
# up to date as videos complete, so resuming costs no rescans and no network.
DONE_INDEX = {}


def walk_video_folders(base):
    """Yield every folder under base that could hold one video.

    Both layouts at once: <base>/<video>/ and, beneath each of those,
    <base>/<channel>/<video>/ - so a run resumes whichever layout produced the
    files, including after switching --channel on or off. The order matters: a
    flat folder is yielded before its children, and both indexes keep the FIRST
    folder found for a video id. An unreadable base ends the walk; an unreadable
    child level only skips that one branch.
    """
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return
    for name in names:
        level1 = os.path.join(base, name)
        if not os.path.isdir(level1):
            continue
        yield level1                               # flat layout
        try:
            subs = sorted(os.listdir(level1))
        except OSError:
            continue
        for sub in subs:
            level2 = os.path.join(level1, sub)
            if os.path.isdir(level2):
                yield level2                       # channel layout


def index_downloaded(base):
    """Find every finished download under base and index it by video id.

    Scans <base>/<video>/ (flat layout) AND <base>/<channel>/<video>/ (--channel
    layout), so a run resumes correctly whichever mode produced the files —
    including switching between the two. A download counts as finished only if
    its videoinfo.txt says Status: OK and the video file is still there, so
    interrupted or failed videos are picked up again.
    """
    index = {}

    def consider(folder):
        fields = read_info(folder)
        if fields.get("Status") != "OK":
            return
        if DOWNLOAD_SUBS and not find_transcript_file(folder):
            return
        vid = video_id_from_url(fields.get("Link", ""))
        if vid and vid not in index and find_video_file(folder):
            index[vid] = folder

    for folder in walk_video_folders(base):
        consider(folder)
    return index


def index_transcribed(base):
    """Index every folder that already holds a transcript, by video id.

    The resume index for a --transcript run. It accepts both statuses that can
    leave a transcript behind: "TRANSCRIPT" from an earlier transcript-only run,
    and "OK" from a full download, whose trans_ file is already written. Unlike
    index_downloaded it does NOT want a video file - that absence is the whole
    point of the mode, and a transcript run is finished with a video the moment
    its trans_ file exists.
    """
    index = {}
    for folder in walk_video_folders(base):
        fields = read_info(folder)
        if fields.get("Status") not in (TRANSCRIPT_STATUS, "OK"):
            continue
        vid = video_id_from_url(fields.get("Link", ""))
        if not vid or vid in index or not find_transcript_file(folder):
            continue
        # The transcript is there, but this run also wants comments and this
        # folder has none. Counting it as done would skip the video and write no
        # comments at all, with nothing in the output to say why.
        if EXTRACT_COMMENTS and not find_comments_file(folder):
            continue
        index[vid] = folder
    return index


def comments_line(path, count, state):
    """The Comments: value for videoinfo.txt.

    "none" when the video has no comments or has them switched off - a normal
    state, not an error - and "failed" only when the fetch itself went wrong.
    """
    if path:
        return f"{os.path.basename(path)} ({count} fetched)" if count else os.path.basename(path)
    return "failed" if state == "failed" else "none"


def write_info(folder, title, url, quality, status, error=None,
               transcript=None, words=None, thumbnail=None, description=None,
               info=None, comments=None, comment_count=0, comment_state="off"):
    """Write videoinfo.txt inside the video's folder."""
    lines = [
        f"Title:   {title}",
        f"Link:    {url}",
        f"Channel: {(info or {}).get('channel') or (info or {}).get('uploader') or 'NA'}",
        f"Views:   {count_or_na((info or {}).get('view_count'))}",
        f"Subscribers: {count_or_na((info or {}).get('channel_follower_count'))}",
        f"Uploaded: {upload_time_pkt(info)}",
        f"Category: {category_or_na(info)}",
        f"Quality: {quality}",
        f"Status:  {status}",
    ]
    if DOWNLOAD_SUBS:
        lines.append(f"Transcript: {os.path.basename(transcript) if transcript else 'none'}")
        lines.append(f"Words:      {os.path.basename(words) if words else 'none'}")
    if SAVE_THUMBNAIL:
        lines.append(f"Thumbnail:  {os.path.basename(thumbnail) if thumbnail else 'none'}")
    if SAVE_DESCRIPTION:
        lines.append(f"Description: {os.path.basename(description) if description else 'none'}")
    if EXTRACT_COMMENTS:
        lines.append(f"Comments: {comments_line(comments, comment_count, comment_state)}")
    if error:
        lines.append(f"Error:   {error}")
    with open(os.path.join(folder, "videoinfo.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def output_template(folder, folder_name):
    """yt-dlp -o template that names the file after its folder.

    The name is the one sanitize() already vetted, rather than yt-dlp's raw
    %(title)s: that keeps the file within the path-length budget on Windows,
    keeps the .mp4 and the trans_/words_ files consistently named, and avoids
    --trim-filenames, which trims the whole template and would flatten the
    per-video folder away. A literal % must be doubled to survive the template.
    """
    return os.path.join(folder, folder_name.replace("%", "%%") + ".%(ext)s")


def skip_finished(folder, url, bar=None):
    """Report an already-finished video, and fill in anything it is missing.

    A video downloaded before transcripts, thumbnails or descriptions were
    switched on gets them here — without fetching the video again.
    """
    name = os.path.basename(folder.rstrip("/\\"))
    if bar:
        bar.done(f"SKIP  already downloaded   {name}")
    else:
        print(f"    Already downloaded -> {rel(folder)}/  (skipping)")
    if DOWNLOAD_SUBS and not find_transcript_file(folder):
        trans, words = download_subs(folder, url)
        update_info_transcripts(folder, trans, words)
        if trans:
            note(f"      + {os.path.basename(trans)} + "
                 f"{os.path.basename(words) if words else 'words_*.txt'}")
        else:
            note("      (no transcript available)")

    fields = read_info(folder)
    wants_thumb = SAVE_THUMBNAIL and not find_thumbnail(folder)
    wants_desc = SAVE_DESCRIPTION and not find_description(folder)
    wants_counts = any(k not in fields for k in ("Views", "Subscribers",
                                                 "Uploaded", "Category"))
    # Comments asked for after the video was already downloaded are filled in
    # here too, so an existing library gains them without downloading again.
    wants_comments = EXTRACT_COMMENTS and not find_comments_file(folder)

    # One metadata lookup for everything that needs it. It is by far the
    # slowest step in a skip, and backfilling a description and the comments in
    # the same pass used to make the identical request twice.
    info = None
    if wants_desc or wants_counts or wants_comments:
        info, _ = fetch_info(url)

    if wants_thumb or wants_desc or wants_counts:
        thumb, desc = build_extras(folder, url, info)
        for path in (thumb, desc):
            if path:
                note(f"      + {os.path.basename(path)}")
        update_info_extras(folder, thumb, desc)
        if wants_counts and info:
            update_info_counts(folder, info)
            note("      + views and subscriber count")

    if wants_comments:
        cpath, ccount, cstate = build_comments(folder, url, info)
        update_info_comments(folder, cpath, ccount, cstate)
        if cpath:
            note(f"      + {os.path.basename(cpath)}")
        else:
            note("      (no comments)")
    # Recorded last: the moment this record exists it is streamed, and whoever
    # reads it may open the folder straight away - so the backfill must be done.
    log_record(url=url, status="skipped", folder=rel(folder))


def skip_transcribed(folder, url, bar=None):
    """Report a video whose transcript is already on disk, and do nothing else.

    Unlike skip_finished() this backfills nothing. A --transcript run was asked
    for the transcript and the transcript is there; a thumbnail, a description
    or fresh view counts would be network calls for files nobody asked for, and
    on a folder left by a full download it would redo that download's work.
    """
    name = os.path.basename(folder.rstrip("/\\"))
    if bar:
        bar.done(f"SKIP  already transcribed  {name}")
    else:
        print(f"    Already transcribed -> {rel(folder)}/  (skipping)")
    trans = find_transcript_file(folder)
    log_record(url=url, status="skipped", mode="transcript", folder=rel(folder),
               files={"transcript": os.path.basename(trans) if trans else None})


def skip_existing(folder, url, bar=None):
    """Report an already-done video, the way the current mode defines "done"."""
    if TRANSCRIPT_ONLY:
        skip_transcribed(folder, url, bar)
    else:
        skip_finished(folder, url, bar)


def rel(path):
    """Path shown relative to the download dir (so channel folders are visible)."""
    try:
        return os.path.relpath(path, DOWNLOAD_DIR)
    except ValueError:
        return path


def download_routes():
    """The ways a video can be fetched, in the order worth trying.

    Each is (label, extra flags, use the fast multi-connection downloader). The
    default client is skipped once something in this batch has proved the
    PO-token client is needed, and the single-connection route exists because a
    token-bound URL sometimes rejects aria2c's parallel range requests.
    """
    routes = []
    if not po_token_first[0]:
        routes.append(("default client", [], True))
    routes.append(("PO-token client (mweb)", list(PO_TOKEN_FLAGS), True))
    if ARIA2C:
        routes.append(("PO-token client, single connection", list(PO_TOKEN_FLAGS), False))
    return routes


def run_with_retries(attempt):
    """Try every download route, and repeat the whole set a few times.

    Most YouTube failures pass on their own: a throttled stream, a token that
    went stale, a fragment that 403s once. Switching route fixes some of them
    and waiting fixes others, so both are tried before a video is given up on.
    Returns None on success, else the last error.
    """
    err = None
    for round_no in range(1, MAX_ATTEMPTS + 1):
        round_errors = []
        for label, flags, fast in download_routes():
            if err:
                note(f"    ! {err}")
                note(f"    Attempt {round_no}/{MAX_ATTEMPTS} via {label}...")
            route_started = time.monotonic()
            log_debug("media_attempt_start", stage="media", route=label,
                      round=round_no, fast=fast, flags=flags)
            err = attempt(flags, fast)
            if err and drop_rejected_cookies(err):
                log_debug("media_cookie_retry", stage="media", route=label,
                          round=round_no, error=err)
                err = attempt(flags, fast)
            log_debug("media_attempt_finish", stage="media", route=label,
                      round=round_no, fast=fast, success=not err,
                      duration=round(time.monotonic() - route_started, 3), error=err)
            if not err:
                if flags:
                    po_token_first[0] = True   # the rest of the batch will need it
                return None
            if is_permanent(err):
                return err                     # retrying cannot help
            round_errors.append(err)
        # Every route - the PO-token client included, which can see formats the
        # default one cannot - says nothing exists in the height range. That is the
        # video, not a hiccup: waiting 10 s and 30 s and asking twice more cannot add
        # a format, and on a bulk run it cost three minutes a video.
        if round_errors and all(FORMAT_GONE in e.lower() for e in round_errors):
            return err
        if round_no < MAX_ATTEMPTS:
            delay = RETRY_DELAYS[min(round_no - 1, len(RETRY_DELAYS) - 1)]
            note(f"    ! {err}")
            note(f"    Every route failed; waiting {delay}s before "
                 f"attempt {round_no + 1} of {MAX_ATTEMPTS}...")
            time.sleep(delay)
    return err


def download_one(url, index, total):
    """Download a single video into its own folder.

    Returns "ok", "skip", "fail" (worth another go later), or "gone" (private,
    deleted, or otherwise never coming back — retrying it would be pointless).
    """
    log_debug("video_start", stage="video", url=url, index=index, total=total)
    note(f"\n[{index}/{total}] {url}")
    bar = ProgressLine(index, total, url)

    # 0) Skip if this video is already finished — matched by video id, so a link
    #    repeated with a different tracking suffix is recognised. No network call.
    vid = video_id_from_url(url)
    if vid and vid in DONE_INDEX:
        skip_existing(DONE_INDEX[vid], url, bar)
        log_debug("video_finish", stage="video", url=url, status="skipped",
                  reason="already_downloaded", folder=rel(DONE_INDEX[vid]))
        return "skip"

    # 1) Get the metadata first: it names the folder (title) and, in --channel
    #    mode, the channel folder it goes under.
    info, err = fetch_info(url)
    vid = (info or {}).get("id") or vid or ""
    title = (info or {}).get("title") or "Unknown Title"
    # With no metadata there is no title to name the folder after, so the video
    # id is used: two dead links would otherwise share one folder and overwrite
    # each other's error record.
    safe_title = sanitize(title) if info else sanitize(f"video_{vid}" if vid else "")

    # The id was unknown until now (unusual URL form) — re-check the index.
    if vid and vid in DONE_INDEX:
        skip_existing(DONE_INDEX[vid], url, bar)
        log_debug("video_finish", stage="video", url=url, status="skipped",
                  reason="already_downloaded", folder=rel(DONE_INDEX[vid]))
        return "skip"

    # Folder = video title, under the channel folder when --channel is on.
    bar.title = title
    parent = channel_dir(info)
    folder_name = safe_title or (f"video_{vid}" if vid else
                                 "link_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:8])
    folder = choose_folder(parent, folder_name, vid)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as e:
        bar.done(f"FAIL  cannot create folder: {e}")
        log_record(url=url, status="failed", error=str(e))
        log_debug("video_finish", stage="video", url=url, status="failed",
                  error=str(e), folder=rel(folder))
        return "fail"

    # If we couldn't even fetch info, record the error and stop here.
    if info is None:
        err = (err or "Failed to fetch video info").replace("ERROR:", "").strip()
        
        if "not in range" in err.lower() or "dateafter" in err.lower():
            bar.done("SKIP  (older than requested days)")
            log_record(url=url, status="skipped", error="older than requested days")
            return "skip"

        note(f"    ! Failed to fetch info: {err}")
        bar.done(f"FAIL  {err}")
        write_info(folder, title, url, "FAILED", "ERROR", err)
        log_record(url=url, status=failure_status(err), error=err, folder=rel(folder))
        log_debug("video_finish", stage="video", url=url, status=failure_status(err),
                  error=err, folder=rel(folder))
        return "gone" if is_permanent(err) else "fail"

    # 2a) Transcript only: no media command runs at all, so everything below
    #     this point - the format string, aria2c, ffprobe - is skipped.
    if TRANSCRIPT_ONLY:
        return transcript_only(folder, url, vid, title, info, bar)

    # 2) Download best 720p-1080p video+audio (merged mp4) and the thumbnail.
    #
    # Captions are deliberately not part of this command. YouTube can rate-limit
    # the caption endpoint independently (HTTP 429); if captions are requested
    # alongside the media, yt-dlp returns a failure for the entire job even when
    # the video itself downloaded successfully. build_transcripts() fetches the
    # selected caption after the video is complete, so a caption problem cannot
    # turn a usable video into a failed download.
    # Named after the folder actually used - "<title> [<id>]" when another video
    # already has "<title>" - so every name in it (video, trans_, words_, and the
    # .f137/.temp streams yt-dlp writes on the way) shares one stem.
    out_template = output_template(folder, os.path.basename(folder))
    def attempt(extra_flags, fast=True):
        """Run yt-dlp, teeing its output so the live progress bar (%, size,
        speed, ETA) shows while the text is kept for error reporting.
        Returns None on success, else the error line."""
        cmd = base_cmd() + [
            "-f", FORMAT,
            "--merge-output-format", "mp4",
            "--no-playlist",
            "--progress",          # show the live download progress bar
            "-o", out_template,
        ] + (speed_flags() if fast else ["--concurrent-fragments", "8"]) \
          + thumb_flags() + extra_flags + ["--", url]
        text = run_streaming(cmd, bar)
        if last_returncode[0] == 0:
            if "not in range" in (text or "").lower() or "dateafter" in (text or "").lower():
                return "upload date is not in range"
            return None
        lines = re.split(r"[\r\n]+", text or "")
        err = next((l for l in reversed(lines) if "ERROR" in l),
                   next((l for l in reversed(lines) if l.strip()), "Download failed"))
        return err.replace("ERROR:", "").strip()

    note(f"    Downloading (>= {MIN_HEIGHT}p, <= {MAX_HEIGHT}p) -> {rel(folder)}/")
    bar.update(0.0, "")
    started = time.monotonic()
    err = run_with_retries(attempt)
    elapsed = time.monotonic() - started

    if err == "upload date is not in range":
        bar.done("SKIP  (older than requested days)")
        log_record(url=url, status="skipped", error="older than requested days")
        shutil.rmtree(folder, ignore_errors=True)
        return "skip_date"

    if err:
        note(f"    ! Download failed: {err}")
        bar.done(f"FAIL  {err}")
        clear_captions(folder)
        leftovers = mark_incomplete(folder)
        for name in leftovers:
            note(f"      partial file kept as {name}")
        log_record(url=url, title=title, status=failure_status(err),
                   error=err, folder=rel(folder), partial=leftovers or None)
        write_info(folder, title, url, "FAILED", "ERROR", err)
        # No format in the height range is as final as a deleted video: the final
        # sweep would only run the same rounds again and record it twice.
        result = "gone" if is_permanent(err) or FORMAT_GONE in err.lower() else "fail"
        log_debug("video_finish", stage="video", url=url, status="failed",
                  result=result, error=err, folder=rel(folder), partial=leftovers or None)
        return result

    # 3) Verify the actual downloaded quality with ffprobe.
    vfile = find_video_file(folder)
    height = probe_height(vfile) if vfile else None
    quality = f"{height}p" if height else "Unknown"

    # Transcripts: fetch the json3 caption only after the video succeeds, then
    # convert it into trans_<name>.txt (grouped) + words_<name>.txt (word-level).
    trans = words = None
    sub_note = ""
    if DOWNLOAD_SUBS:
        trans, words = build_transcripts(folder, url, info)
        if not trans:
            err = required_caption_error()
            note(f"    ! {err}")
            bar.done(f"FAIL  {err}")
            write_info(folder, title, url, quality, "ERROR", error=err, info=info)
            log_record(url=url, title=title, status="failed", quality=quality,
                       error=err, folder=rel(folder))
            log_debug("video_finish", stage="video", url=url, status="failed",
                      result="fail", error=err, quality=quality, folder=rel(folder),
                      captions=False)
            return "fail"
        sub_note = (f"  Transcript: {os.path.basename(trans)} + "
                    f"{os.path.basename(words)}")
    thumb, desc = build_extras(folder, url, info)
    cpath, ccount, cstate = build_comments(folder, url, info)
    extras_note = "".join(f"  {label}: {os.path.basename(path)}"
                          for label, path in (("Thumbnail", thumb), ("Description", desc),
                                              ("Comments", cpath))
                          if path)
    size = folder_size(folder)
    STATS["bytes"] += size
    STATS["seconds"] += elapsed
    rate = f", {human_size(size / elapsed)}/s" if elapsed > 0 else ""
    note(f"    Done. Quality: {quality}  "
         f"[{human_size(size)} in {human_time(elapsed)}{rate}]{sub_note}{extras_note}")
    got = "".join(c for c, path in (("T", trans), ("W", words and "not_found" not in words),
                                    ("j", thumb), ("d", desc), ("c", cpath)) if path)
    speed = f"{human_size(size / elapsed)}/s" if elapsed > 0 else "-"
    bar.done(f"OK  {quality:>6} {human_size(size):>9} {human_time(elapsed):>9} "
             f"{speed:>10}  [{got}]  {title}")
    write_info(folder, title, url, quality, "OK", transcript=trans, words=words,
               thumbnail=thumb, description=desc, info=info,
               comments=cpath, comment_count=ccount, comment_state=cstate)
    # Recorded after videoinfo.txt, so the folder is complete when it streams.
    log_record(url=url, title=title, status="ok", quality=quality, folder=rel(folder),
               channel=info.get("channel") or info.get("uploader"),
               views=info.get("view_count"),
               subscribers=info.get("channel_follower_count"),
               uploaded=upload_time_pkt(info), category=category_or_na(info),
               bytes=size, seconds=round(elapsed, 1),
               files={"video": os.path.basename(vfile) if vfile else None,
                      "transcript": os.path.basename(trans) if trans else None,
                      "words": os.path.basename(words) if words else None,
                      "thumbnail": os.path.basename(thumb) if thumb else None,
                      "description": os.path.basename(desc) if desc else None,
                      "comments": os.path.basename(cpath) if cpath else None})
    log_debug("video_finish", stage="video", url=url, status="ok", quality=quality,
              folder=rel(folder), bytes=size, seconds=round(elapsed, 1),
              captions=True, thumbnail=bool(thumb), description=bool(desc))
    # Register it so a link repeated later in this same run is skipped too.
    if vid:
        DONE_INDEX[vid] = folder
    return "ok"


# ========================= transcript-only mode ===========================
def transcript_only(folder, url, vid, title, info, bar):
    """Write just the transcript for one video. Returns download_one's verdict.

    This replaces the media download and the ffprobe quality check, so the run
    loop, the final sweep and the counts need to know nothing about the mode.
    Never returns "gone": a permanently dead video is already settled by the
    shared metadata step, and a caption that fails is usually the caption
    endpoint's own rate limit - exactly what the end-of-batch sweep is for.
    """
    started = time.monotonic()
    # Normally there is nothing here yet. There is when the video was already
    # transcribed and this run wants its comments as well, and re-fetching the
    # caption for a file already on disk would be a network call for nothing.
    trans, words = existing_transcripts(folder)
    if not trans:
        trans, words = build_transcripts(folder, url, info)
    elapsed = time.monotonic() - started

    # A folder that already holds a video was made by a full download (one run
    # with --no-subs, say). Its videoinfo.txt is edited line by line rather than
    # replaced: a blanket Status: TRANSCRIPT would take a finished download out
    # of the resume index, and Quality: NA would deny the quality it really has.
    vfile = find_video_file(folder)

    if not trans:
        err = required_caption_error()
        note(f"    ! {err}")
        bar.done(f"FAIL  {err}")
        if vfile:
            update_info_transcripts(folder, None, None)
        else:
            write_info(folder, title, url, TRANSCRIPT_QUALITY, "ERROR",
                       error=err, info=info)
        log_record(url=url, title=title, status="failed", mode="transcript",
                   error=err, folder=rel(folder))
        log_debug("video_finish", stage="video", url=url, status="failed",
                  result="fail", mode="transcript", error=err, folder=rel(folder),
                  captions=False)
        return "fail"

    # SAVE_THUMBNAIL is forced off in this mode, so this writes only the
    # description, out of metadata already fetched, and starts no subprocess.
    thumb, desc = build_extras(folder, url, info)
    cpath, ccount, cstate = build_comments(folder, url, info)
    # Only what this mode wrote. folder_size() would include a video file left
    # by an earlier full download and report it as this run's output.
    size = files_size(trans, words, desc, cpath)
    got = "".join(c for c, path in (("T", trans),
                                    ("W", words and "not_found" not in words),
                                    ("d", desc), ("c", cpath)) if path)
    note(f"    Transcript done. {os.path.basename(trans)} "
         f"[{human_size(size)} in {human_time(elapsed)}]")
    bar.done(f"TEXT {human_size(size):>9} {human_time(elapsed):>9} "
             f"            [{got}]  {title}")

    if vfile:
        update_info_transcripts(folder, trans, words)
        update_info_extras(folder, thumb, desc)
        update_info_comments(folder, cpath, ccount, cstate)
    else:
        write_info(folder, title, url, TRANSCRIPT_QUALITY, TRANSCRIPT_STATUS,
                   transcript=trans, words=words, thumbnail=thumb,
                   description=desc, info=info, comments=cpath,
                   comment_count=ccount, comment_state=cstate)
    # "ok" so the API counts it as done; the mode and the absent video are
    # visible in the record itself rather than hidden behind a new status word.
    log_record(url=url, title=title, status="ok", mode="transcript",
               quality=TRANSCRIPT_QUALITY, folder=rel(folder),
               channel=info.get("channel") or info.get("uploader"),
               views=info.get("view_count"),
               subscribers=info.get("channel_follower_count"),
               uploaded=upload_time_pkt(info), category=category_or_na(info),
               bytes=size, seconds=round(elapsed, 1),
               files={"video": None,
                      "transcript": os.path.basename(trans),
                      "words": os.path.basename(words) if words else None,
                      "thumbnail": None,
                      "description": os.path.basename(desc) if desc else None,
                      "comments": os.path.basename(cpath) if cpath else None})
    log_debug("video_finish", stage="video", url=url, status="ok",
              mode="transcript", folder=rel(folder), bytes=size,
              seconds=round(elapsed, 1), captions=True)
    TRANSCRIPT_COUNT[0] += 1
    if vid:
        DONE_INDEX[vid] = folder          # a repeat of this link now skips
    return "ok"


# ============================== search =====================================
# YouTube's own search filters are NOT reachable from here. yt-dlp's
# youtube:search_url extractor parses out the search_query and drops the sp=
# parameter that carries them: a "last hour only" sp returned the identical
# video ids as no filter at all, and a "sort by views" sp returned them
# unsorted. "ytsearchdate:" is not supported by this yt-dlp either. So every
# filter and every sort below is applied here, which also means "last 30 days"
# is exactly 30 days rather than YouTube's coarse "this month" bucket.
#
# Two routes, because the cost difference is large and measured:
#   - no date needed: one --flat-playlist listing. 1.5s for 10 results, and it
#     already carries view_count, duration, channel and live_status.
#   - date needed: one non-flat listing with --dateafter, which filters AND
#     returns timestamps in the same pass. 22.7s for 12 results scanned.
# Resolving dates one watch URL at a time was measured at 10.8s per video -
# five times worse than letting the search extraction do it - so it is not used.

def search_window():
    """The date floor for a search: (since_epoch, label). (None, "") if none."""
    if DAYS:
        return time.time() - DAYS * 86400, f"last {DAYS} days"
    if THIS_MONTH:
        now = datetime.datetime.now()
        first = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return first.timestamp(), f"since {first.strftime('%d %b %Y')}"
    return None, ""


def channel_search_url(channel, query):
    """A channel's own search tab: <channel>/search?query=<query>.

    YouTube searches only inside that channel there, which is a different answer
    from a site-wide search filtered by uploader - it is the channel's own index,
    so it finds older videos a site-wide search would never surface.
    """
    base = (channel or "").split("?")[0].split("#")[0].rstrip("/")
    tail = base.rsplit("/", 1)[-1].lower()
    if tail in VIDEO_TABS or tail in OTHER_TABS or tail == "search":
        base = base.rsplit("/", 1)[0]        # drop a tab that is already there
    return f"{base}/search?query={quote(query)}"


# YouTube's "Videos" search filter - the one a person clicks under Filters.
#
# It is NOT applied to every search, because it does not merely hide the
# non-video cards: it changes which search YouTube runs, and by a lot in both
# directions. Measured at --playlist-end 150:
#
#   query                         without it   with it
#   one long video title             150          3
#   "how to learn guitar"             23        150
#
# So it is applied where it is load-bearing and nowhere else: the dated route.
# A channel or playlist card is harmless to the flat route, which leaves it as
# one entry with a non-video id that search_items drops. The dated route cannot
# use --flat-playlist - it needs timestamps - so yt-dlp RESOLVES each card, and
# a channel card resolves into that channel's own uploads, which then arrive as
# if they were search results. Measured on "how to learn guitar --sort date"
# without the filter: all ten results were one channel's uploads, not one of
# them a hit for the query, and it took 101s instead of 18s.
VIDEOS_ONLY = "EgIQAQ%3D%3D"


def web_search_url(query, videos_only=False):
    """YouTube's own results page for a query - what a person sees in a browser.

    This is the relevance-ordered list, and relevance is the point: it also
    carries the near-matches, which for research are the useful part - the same
    story told by another channel, the opposite angle on it, the same formula in
    another language. Asking YouTube to sort by views instead collapses onto the
    exact phrase and loses all of that.

    yt-dlp's ytsearch: pseudo-URL answers a much narrower question. Measured on
    one long title: ytsearch100: returned 2 videos where this returned 150.

    videos_only adds the Videos filter; see VIDEOS_ONLY for when that is right.
    """
    url = f"https://www.youtube.com/results?search_query={quote(query)}"
    return f"{url}&sp={VIDEOS_ONLY}" if videos_only else url


def search_target(query, count, videos_only=False):
    """What to hand yt-dlp for this search.

    With --in-channel that is the channel's own search tab. Otherwise it is
    whichever net SEARCH_MATCH asks for: the results page, or ytsearchN: - where
    only the first colon separates the prefix from the query, so the query may
    contain colons of its own.

    videos_only is for the dated route alone; see VIDEOS_ONLY.
    """
    if IN_CHANNEL:
        return channel_search_url(IN_CHANNEL, query)
    if SEARCH_MATCH == "exact":
        return f"ytsearch{max(1, count)}:{query}"
    return web_search_url(query, videos_only)


def search_flat(query, count):
    """The cheap route: one flat listing. Returns (entries, error).

    No upload dates - a flat search entry simply has none - but view_count,
    duration, channel and live_status are all there, which is everything the
    non-date filters need.
    """
    data, err = channel_page(search_target(query, count), 1, max(1, count))
    if err:
        return [], err
    return [e for e in (data.get("entries") or []) if e], None


def search_dated(query, count, since=None):
    """The dated route: one non-flat listing that dates - and filters - in a pass.

    With a window, --dateafter makes yt-dlp skip anything older while it
    extracts, so the output is already inside the window and carries
    timestamps. Without one - a date sort and no window - the flag is simply
    left off: this route is needed for the timestamps it returns, not for a
    filter, and inventing a floor would put the oldest videos on a title out of
    reach of the sort that exists to find them.

    Returns (items, error); each item has id/title/views/channel/epoch.
    """
    cmd = base_cmd() + [
        "--no-warnings", "--ignore-errors", "--skip-download",
        "--playlist-end", str(max(1, count)),
        # live_status comes before the title so the title stays last: it is the
        # one field that can itself contain a tab, and the parse below gives the
        # last field whatever remains of the line.
        "--print", "%(id)s\t%(timestamp)s\t%(view_count)s\t%(live_status)s"
                   "\t%(duration)s\t%(channel)s\t%(title)s",
    ]
    if since is not None:
        days = max(1, int((time.time() - since) / 86400) + 1)
        cmd += ["--dateafter", f"today-{days}days"]
    # videos_only: this route resolves whatever it is given, and a channel card
    # resolves into that channel's uploads.
    cmd += ["--", search_target(query, count, videos_only=True)]
    try:
        out = run_logged("search_dated", cmd, capture_output=True, text=True,
                         timeout=SEARCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [], "Timed out while searching"
    except OSError as e:
        return [], f"Could not run yt-dlp: {e}"
    items = []
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t", 6)
        if len(parts) < 7 or len(parts[0]) != 11:
            continue
        vid, stamp, views, live, length, channel, title = parts
        items.append({
            "id": vid,
            "url": watch_url(vid),
            "title": title or "Unknown Title",
            "channel": channel if channel != "NA" else "",
            "views": int(views) if views.isdigit() else None,
            "epoch": int(stamp) if stamp.isdigit() else None,
            # Carried so the live/upcoming rule can be applied to this route
            # too; without it a premiere or an in-progress stream would pass
            # the filter that exists to keep them out.
            "live_status": None if live == "NA" else live,
            # Same reason, for the Shorts floor. as_seconds, so there is one
            # rule for reading a duration wherever it comes from.
            "duration": as_seconds(length),
        })
    if not items and out.returncode != 0:
        msg = (out.stderr or "").strip().splitlines()
        return [], (msg[-1] if msg else "Search failed")
    return items, None


def resolve_dates(items):
    """Fill in the upload date of each item, in one yt-dlp pass.

    Returns how many were dated. Items left undated keep epoch None, which the
    row then reports as a missing date rather than a wrong one.
    """
    urls = [i["url"] for i in items if i.get("epoch") is None and i.get("id")]
    if not urls:
        return 0
    cmd = base_cmd() + [
        "--no-warnings", "--ignore-errors", "--skip-download",
        "--print", "%(id)s\t%(timestamp)s", "--",
    ] + urls
    try:
        out = run_logged("search_dates", cmd, capture_output=True, text=True,
                         timeout=SEARCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        note("      (ran out of time fetching publish dates)")
        return 0
    except OSError as e:
        note(f"      (could not fetch publish dates: {e})")
        return 0
    dates = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) == 2 and parts[1].isdigit():
            dates[parts[0]] = int(parts[1])
    for item in items:
        if item.get("epoch") is None:
            item["epoch"] = dates.get(item.get("id"))
    return len(dates)


def take_in_window(items, since, limit, cap=None):
    """Walk an already-sorted list, dating as it goes, keeping what is in the
    window. Returns (kept, how many were dated, whether the list ran out).

    The window cannot be applied before the dates are known, and the dates are
    the expensive part, so they are fetched a block at a time DOWN A LIST THAT
    IS ALREADY IN THE ORDER THE CALLER ASKED FOR. A "top 20 by views, this
    month" therefore stops as soon as it has twenty, instead of dating all 150
    - and it keeps the full relevance breadth, which is what asking YouTube to
    filter by date costs: on one long title YouTube's own Videos-plus-date
    search returned 3 videos where the unfiltered relevance list returned 148.
    """
    cap = DATE_RESOLVE_MAX if cap is None else cap
    block = max(10, limit or 20)
    kept, dated, index = [], 0, 0
    while index < len(items) and dated < cap:
        chunk = items[index:index + min(block, cap - dated)]
        if not chunk:
            break
        index += len(chunk)
        dated += len(chunk)
        resolve_dates(chunk)
        for item in chunk:
            epoch = item.get("epoch")
            if epoch and epoch >= since:
                kept.append(item)
                if limit and len(kept) >= limit:
                    return kept, dated, index >= len(items)
    return kept, dated, index >= len(items)


def sort_items(items):
    """Order the results. "relevance" keeps the order YouTube returned them in.

    A missing count or date sorts last rather than first, so an entry YouTube
    declined to measure never outranks one it did.
    """
    if SORT_BY == "views":
        return sorted(items, key=lambda i: (i.get("views") is None, -(i.get("views") or 0)))
    if SORT_BY == "least-viewed":
        # Reverse popularity: the weakest performers on a title first, which is
        # what shows an angle nobody has managed to make work yet.
        return sorted(items, key=lambda i: (i.get("views") is None, i.get("views") or 0))
    if SORT_BY == "date":
        return sorted(items, key=lambda i: (i.get("epoch") is None, -(i.get("epoch") or 0)))
    if SORT_BY == "oldest":
        return sorted(items, key=lambda i: (i.get("epoch") is None, i.get("epoch") or 0))
    return items


def needs_dated_route():
    """Whether this search has to ask YouTube for the dates.

    Sorting BY date needs every date before anything can be ordered, so that
    takes the dated route. A date WINDOW does not: the cheap route brings the
    whole relevance list and the window is applied locally over dates fetched
    afterwards - unless --no-dates means they cannot be fetched at all.

    One function, because the banner and the run must not disagree about which
    route (and therefore which scan depth) this search is going to use.
    """
    if SORT_BY in ("date", "oldest"):
        return True
    since, _label = search_window()
    return since is not None and not RESOLVE_DATES


def search_depth(needs_dates):
    """How many results this search will scan.

    An explicit --search-count is used as given, however slow that makes it.
    Without one the depth follows the route (see SEARCH_DEPTH_FLAT).
    """
    if SEARCH_COUNT:
        return max(1, SEARCH_COUNT)
    return SEARCH_DEPTH_DATED if needs_dates else SEARCH_DEPTH_FLAT


def search_items(query):
    """Run the search and return (items, error), filtered, sorted and capped."""
    since, label = search_window()
    # A date SORT needs every date before it can order anything, so it has to
    # take the dated route. A date WINDOW does not: the cheap route brings the
    # whole relevance list, and the window is applied here over dates fetched
    # for the results that survive. That keeps the breadth YouTube's own date
    # search throws away - on one long title it returned 3 videos where the
    # unfiltered relevance list returned 148.
    needs_dates = needs_dated_route()
    window_here = since is not None and not needs_dates
    depth = search_depth(needs_dates)
    log_debug("search_start", stage="search", query=redact_text(query),
              count=depth, asked=SEARCH_COUNT, sort=SORT_BY, days=DAYS,
              this_month=THIS_MONTH,
              in_channel=redact_url(IN_CHANNEL) if IN_CHANNEL else None,
              views=MIN_VIEWS, limit=LIMIT, dated_route=needs_dates)

    if needs_dates:
        items, err = search_dated(query, depth, since)
        if since is not None:
            # --dateafter is accurate to the day, so the window is tightened
            # here to the exact second. An item whose timestamp YouTube declined
            # to print is kept: --dateafter already accepted it, and dropping a
            # real result over a missing field would shrink every dated search.
            items = [i for i in items if i.get("epoch") is None or i["epoch"] >= since]
    else:
        entries, err = search_flat(query, depth)
        items = []
        for entry in entries:
            vid = entry.get("id") or ""
            if len(vid) != 11:
                continue
            items.append({
                "id": vid,
                "url": watch_url(vid),
                "title": entry.get("title") or "Unknown Title",
                "channel": entry.get("channel") or entry.get("uploader") or "",
                "views": entry.get("view_count"),
                "epoch": None,
                "live_status": entry.get("live_status"),
                "duration": entry.get("duration"),
            })
    if err:
        return [], err

    found = len(items)
    # wanted_entry() already decides the view floor, the live/upcoming rule and
    # the length floor for channel listings; a search result is judged by the
    # same rules - and its reasons are kept, not discarded, because the length
    # floor is on by default and a thin answer has to be explainable.
    counts = {"scanned": found, "views": 0, "unknown": 0, "live": 0,
              "skipped": 0, "short": 0, "nolength": 0}
    wanted = []
    for item in items:
        keep, reason = wanted_entry({"view_count": item.get("views"),
                                     "live_status": item.get("live_status"),
                                     "duration": item.get("duration")})
        if keep:
            wanted.append(item)
        else:
            counts[reason] = counts.get(reason, 0) + 1
    seen, unique = set(), []
    for item in wanted:
        if item["id"] in seen:
            continue
        if item["id"] in SKIP_IDS:
            counts["skipped"] += 1        # reported like a channel listing's
            continue
        seen.add(item["id"])
        unique.append(item)
    items = sort_items(unique)
    skipped_by_list[0] += counts["skipped"]

    if window_here:
        # Dates fetched down the sorted list, keeping what is in the window and
        # stopping as soon as --limit is satisfied.
        print(f"  Dates   : fetching publish dates down the list to find"
              f"{f' {LIMIT}' if LIMIT else ''} video(s) {label}"
              f" (about 2.1s each)...")
        items, dated, exhausted = take_in_window(items, since, LIMIT)
        counts["dated"] = dated
        print(f"            dated {dated}, kept {len(items)}")
        if not exhausted and (not LIMIT or len(items) < LIMIT):
            print(f"            stopped at the {DATE_RESOLVE_MAX}-video date cap;"
                  f" there may be more further down the list")
    else:
        if LIMIT:
            items = items[:LIMIT]
        # The flat route brings no dates. Fetch them for what is left rather
        # than for everything scanned - see DATE_RESOLVE_MAX.
        if RESOLVE_DATES and items and all(i.get("epoch") is None for i in items):
            if len(items) <= DATE_RESOLVE_MAX:
                print(f"  Dates   : fetching {len(items)} publish date(s)"
                      f" (about {round(len(items) * 2.1)}s)...")
                dated = resolve_dates(items)
                counts["dated"] = dated
                if dated < len(items):
                    print(f"            {len(items) - dated} could not be dated")
            else:
                print(f"  Dates   : {len(items)} results is past the"
                      f" {DATE_RESOLVE_MAX} this will date in one run; publish"
                      f" dates left out.  (--limit {DATE_RESOLVE_MAX} to get them)")
    # What the search returned, kept apart from what survived the filters: the
    # log record needs both, and only this function ever sees the first.
    SEARCH_FOUND[0] = found
    SEARCH_COUNTS[0] = counts
    log_debug("search_finish", stage="search", query=redact_text(query), found=found,
              kept=len(items), window=label, **{k: v for k, v in counts.items()
                                                if k != "scanned" and v})
    return items, None


def search_row(item, rank):
    """One search result as the four things a search is actually for:
    where it is, how it did, when it went up, and whose it is."""
    epoch = item.get("epoch")
    return {
        "rank": rank,
        "id": item.get("id"),
        "link": item.get("url"),
        "title": item.get("title"),
        "channel": item.get("channel") or None,
        "views": item.get("views"),
        "duration": item.get("duration"),
        "duration_text": (human_time(item["duration"])
                          if isinstance(item.get("duration"), (int, float)) else None),
        "published": upload_time_pkt({"timestamp": epoch}) if epoch else None,
        "published_epoch": epoch,
    }


def write_search_results(path, query, items, label):
    """The machine-readable form of the result table.

    The printed table is for reading and the links file is for the downloader;
    neither is parseable, and the four fields per video are the point of a
    search, so they also go out as JSON.
    """
    rows = [search_row(item, number) for number, item in enumerate(items, 1)]
    payload = {
        "query": query,
        "in_channel": IN_CHANNEL,
        # As in the REST answer: None for an in-channel search, which neither
        # value of --match applies to.
        "match": None if IN_CHANNEL else SEARCH_MATCH,
        "sort": SORT_BY,
        "window": label or None,
        "min_duration": duration_floor() or None,
        "scanned": SEARCH_FOUND[0],
        "found": len(rows),
        "results": rows,
    }
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    except OSError as e:
        print(f"  (could not write {os.path.basename(path)}: {e})")
    return path


def write_search_links(path, query, items, label):
    """Write the links file the rest of the run then reads.

    Every line that is meant to be a comment is flattened to one line first. A
    title - or a query - carrying a newline would otherwise end its "# " comment
    early and leave whatever followed standing on a line of its own, which this
    file's reader treats as a link to download. The links themselves are rebuilt
    from the video id rather than copied, so only a real watch URL can appear.
    """
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# search: {one_line(query)}\n")
        f.write(f"# sort: {SORT_BY}{'  window: ' + one_line(label) if label else ''}\n")
        f.write(f"# found: {len(items)} video(s)\n")
        for item in items:
            vid = item.get("id") or ""
            if not BARE_ID_RE.match(vid):
                continue
            f.write(f"# {one_line(item.get('title'))}\n{watch_url(vid)}\n")
    return path


def clear_search_answers():
    """Delete the previous search's answers.

    A search that fails or matches nothing must not leave the last one's
    search_links.txt and search_results.json sitting in the folder: they read
    exactly like this search's answer, and the links file is what a later run
    would download.
    """
    for name in ("search_links.txt", "search_results.json"):
        try:
            os.remove(os.path.join(DOWNLOAD_DIR, name))
        except OSError:
            pass


def print_search_results(items, label):
    """The table: views, publish date, channel and link for every result.

    Those four are what a search is for, so each is printed for every video and
    a missing one reads NA rather than being left out - a blank column would be
    read as zero views or an unknown channel.
    """
    scanned = SEARCH_FOUND[0]
    print(f"\n  Search results (sort: {SORT_BY}"
          f"{', ' + label if label else ''}): {len(items)} video(s)"
          f"{f' of {scanned} scanned' if scanned > len(items) else ''}")
    counts = SEARCH_COUNTS[0] or {}
    dropped = ", ".join(p for p in (
        f"{counts.get('short')} under {human_time(duration_floor())}"
        if counts.get("short") else "",
        f"{counts.get('nolength')} of unknown length" if counts.get("nolength") else "",
        f"{counts.get('views')} under {MIN_VIEWS:,} views" if counts.get("views") else "",
        f"{counts.get('live')} live or upcoming" if counts.get("live") else "",
        f"{counts.get('skipped')} in skip.txt" if counts.get("skipped") else "",
    ) if p)
    if dropped:
        print(f"  Filtered out: {dropped}")
    for number, item in enumerate(items, 1):
        row = search_row(item, number)
        when = row["published"].split(",")[0] if row["published"] else "date NA"
        print(f"  {number:>3}. {count_or_na(row['views']):>12} views   "
              f"{when:>12}   {row['channel'] or 'channel NA'}")
        print(f"       {row['title']}")
        print(f"       {row['link']}")


def run_search(args):
    """Do the search and write its links file. Returns the path, or None to stop."""
    query = (args.search or "").strip()
    if not query:
        print("\nERROR: --search needs something to search for.")
        return None
    since, label = search_window()
    print(f"\n  Searching {'in ' + IN_CHANNEL if IN_CHANNEL else 'YouTube'} "
          f"for: {query}")
    if label:
        print(f"  Window  : {label}")
    items, err = search_items(query)
    if err:
        print(f"\n  Search failed: {err}")
        clear_search_answers()
        log_record(url=f"ytsearch:{query}", status="failed", error=err)
        return None
    if not items:
        counts = SEARCH_COUNTS[0] or {}
        if counts.get("scanned"):
            # Something came back and the filters took all of it, so say which
            # one - "widen the window" is useless advice when the length floor
            # is what emptied the answer, and the floor is on by default.
            print()
            explain_empty_listing(counts)
        else:
            print("\n  Nothing matched. Try a wider window, fewer filters, or "
                  "a bigger --search-count.")
        clear_search_answers()
        log_record(url=f"ytsearch:{query}", status="search",
                   found=SEARCH_FOUND[0], kept=0)
        return None
    print_search_results(items, label)
    # Always its own file, never the --links path. A search result is a
    # different thing from a curated list, and writing over someone's
    # links.txt to store it would destroy the input they chose.
    path = os.path.join(DOWNLOAD_DIR, "search_links.txt")
    write_search_links(path, query, items, label)
    results = write_search_results(os.path.join(DOWNLOAD_DIR, "search_results.json"),
                                   query, items, label)
    print(f"\n  Wrote {len(items)} link(s) to {os.path.basename(path)}"
          f" and {os.path.basename(results)}")
    # "search" is not in the API's DONE or FAILED sets on purpose: the expansion
    # is visible to a caller without being counted as a video either way.
    log_record(url=f"ytsearch:{query}", status="search", found=SEARCH_FOUND[0],
               kept=len(items), links_file=os.path.basename(path))
    return path


# ======================== answers, not files ===============================
# Downloading is only one of the things this tool is for. Asking what a video
# is, what it says, or what people said about it produces an answer, not a
# folder - so these functions fetch it and hand the data straight back, leaving
# nothing on disk. They are what the REST API serves: a caller that wants a
# video's comments wants the comments, not a directory it must then read and
# tidy up. Everything here writes nothing permanent and starts no job.


@contextlib.contextmanager
def scratch_dir():
    """A throwaway folder, removed whatever happens inside it.

    The fetch steps are all built around a video's own folder, so the functions
    below lend them one and then take it away again.
    """
    folder = tempfile.mkdtemp(prefix="ytmeer_")
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def caption_tracks(info):
    """Which English caption tracks a video has: {"auto": [...], "manual": [...]}.

    Worth knowing before asking for the transcript: "auto" is the track timed
    per word, and a video with only "manual" tracks cannot produce a word-level
    transcript at all (see write_words_not_found).
    """
    out = {}
    for key, name in (("automatic_captions", "auto"), ("subtitles", "manual")):
        tracks = (info or {}).get(key) or {}
        out[name] = sorted(code for code in tracks
                           if code == SUB_LANG or code.startswith(f"{SUB_LANG}-"))
    return out


def details_of(info):
    """One video's metadata, as the fields this tool is actually asked for.

    A missing value stays None rather than becoming "NA": this is data, and a
    caller can tell the difference between "no likes" and "likes not published"
    only if the absence survives. The printed forms (uploaded, duration_text)
    come along because they are the awkward ones to derive - the upload time is
    converted to PKT here exactly as videoinfo.txt records it.
    """
    info = info or {}
    vid = info.get("id")
    seconds = info.get("duration")
    return {
        "id": vid,
        "link": info.get("webpage_url") or (watch_url(vid) if vid else None),
        "title": info.get("title"),
        "description": safe_text(info.get("description")) or None,
        "channel": info.get("channel") or info.get("uploader"),
        "channel_id": info.get("channel_id"),
        "channel_url": info.get("channel_url") or info.get("uploader_url"),
        "subscribers": info.get("channel_follower_count"),
        "views": info.get("view_count"),
        "likes": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "duration": seconds,
        "duration_text": human_time(seconds) if isinstance(seconds, (int, float)) else None,
        "uploaded": upload_time_pkt(info),
        "upload_epoch": info.get("timestamp"),
        "category": category_or_na(info),
        "categories": list(info.get("categories") or []),
        "tags": list(info.get("tags") or []),
        "thumbnail": info.get("thumbnail"),
        "live_status": info.get("live_status"),
        "availability": info.get("availability"),
        "age_limit": info.get("age_limit"),
        "captions": caption_tracks(info),
    }


def video_details(url):
    """(details, error) for one video. Reads metadata only; writes nothing."""
    info, err = fetch_info(url)
    if not info:
        return None, err or "Could not read this video"
    return details_of(info), None


# Said in the answer rather than in a file, for the same reason
# write_words_not_found says it in the folder: a caller asking for word timings
# deserves to be told they do not exist, not handed a silent null.
NO_WORD_TIMINGS = (
    "No word-level transcript exists for this video. YouTube times captions per "
    "word only in its auto-generated track, and this video does not have one - "
    "it carries broadcast closed captions, which are timed one phrase at a time. "
    "The phrase-level text is in \"text\"."
)


def video_transcript(url):
    """(transcript, error) for one video: the text itself, in both forms.

    No files are left anywhere. "text" is the grouped, wrapped transcript and
    "words" is one word per line - the same two shapes trans_/words_ have on
    disk, because they are produced by the same code.
    """
    info, err = fetch_info(url)
    if not info:
        return None, err or "Could not read this video"
    with scratch_dir() as folder:
        entries = best_caption_entries(folder, url, info)
    if not entries:
        return None, required_caption_error()
    word_level = is_word_level(entries)
    return {
        "id": info.get("id"),
        "link": info.get("webpage_url"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "language": SUB_LANG,
        "word_level": word_level,
        "segments": len(entries),
        "duration": info.get("duration"),
        "text": "\n".join(trans_lines(entries)),
        "words": "\n".join(words_lines(entries)) if word_level else None,
        "note": None if word_level else NO_WORD_TIMINGS,
    }, None


def comment_data(comment):
    """One comment as data. Its own text keeps its line breaks."""
    return {
        "id": comment.get("id"),
        "author": safe_text(comment.get("author")) or None,
        "author_is_uploader": bool(comment.get("author_is_uploader")),
        "author_is_verified": bool(comment.get("author_is_verified")),
        "pinned": bool(comment.get("is_pinned")),
        "likes": comment.get("like_count"),
        "published": comment_time(comment),
        "published_epoch": comment.get("timestamp"),
        "text": safe_text(comment.get("text")),
    }


def video_comments(url):
    """(comments, error) for one video: threaded, as data, leaving no files.

    Threaded rather than flat for the same reason the text file is: a reply read
    without the comment it answers is usually meaningless. COMMENT_LIMIT,
    COMMENT_SORT and COMMENT_REPLIES decide what is fetched, exactly as they do
    for a download.
    """
    info, err = fetch_info(url)
    if not info:
        return None, err or "Could not read this video"
    with scratch_dir() as folder:
        comments = fetch_comments(folder, url)
    threads = thread_comments(comments)
    return {
        "id": info.get("id"),
        "link": info.get("webpage_url"),
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "sort": COMMENT_SORT,
        "replies_included": COMMENT_REPLIES,
        # What the video claims to have, against what this fetch asked for: a
        # caller comparing 100 to 54,000 can see the limit did its job.
        "total_on_video": info.get("comment_count"),
        "fetched": len(comments),
        "top_level": len(threads),
        "replies": len(comments) - len(threads),
        "threads": [dict(comment_data(top), replies=[comment_data(k) for k in kids])
                    for top, kids in threads],
    }, None


# ====================== channel links & the skip list ======================
# links.txt may name a whole channel instead of a single video. Those links are
# expanded here - into the video links that clear --views, newest first, cut off
# by --limit - so everything downstream still deals with one video at a time and
# none of the download logic has to know the difference.

CHANNEL_URL_RE = re.compile(
    r"youtube\.com/(?:@[^/?#]+|c/[^/?#]+|channel/[^/?#]+|user/[^/?#]+)", re.I)

# Tabs that are themselves a list of videos: a link pointing at one is honoured
# as given. Anything else is redirected to /videos, because a bare channel link
# resolves to the channel's list of TABS rather than to any videos, and because
# YouTube files long-form videos, Shorts and streams under separate tabs - so
# /videos is what "the channel's long videos" means.
VIDEO_TABS = ("videos", "shorts", "streams", "live")
OTHER_TABS = ("featured", "playlists", "community", "posts", "about", "shop",
              "podcasts", "releases", "channels", "store")

# Videos per listing request. --flat-playlist keeps a page to the listing alone
# (no per-video extraction), so this is one cheap request whatever the videos are.
CHANNEL_PAGE = 100

# How many videos skip.txt kept out of the channel listings this run. Counted
# here because a listing applies skip.txt itself, before --limit, so those
# videos never reach drop_skipped() and the run summary would report none.
skipped_by_list = [0]

# Width of the scan line currently on screen, so the next one can paint over it.
scan_width = [0]


def scan_progress(label, scanned, kept):
    """Rewriting progress while a channel is paged through.

    A --views floor that little clears means every page of a big channel gets
    read before the answer is known, and silence through that is impossible to
    tell from a hang. Only drawn on a terminal: redirected, each repaint would
    land as another line.
    """
    if not sys.stdout.isatty():
        return
    text = f"    {label}: scanned {scanned}, kept {kept}..."
    text = text[:shutil.get_terminal_size((80, 20)).columns - 1]
    sys.stdout.write("\r" + text.ljust(scan_width[0]))
    sys.stdout.flush()
    scan_width[0] = len(text)


def scan_done():
    """Wipe the scan line so the channel's result prints on a clean one."""
    if sys.stdout.isatty() and scan_width[0]:
        sys.stdout.write("\r" + " " * scan_width[0] + "\r")
        sys.stdout.flush()
    scan_width[0] = 0


def is_channel_url(url):
    """True for a link that names a channel rather than one video.

    The video check comes second and wins: a channel link can carry a video in
    its path (/@handle/live/<id>), and that is a video, not a channel.
    """
    return bool(CHANNEL_URL_RE.search(url or "")) and not video_id_from_url(url)


def channel_videos_url(url):
    """The channel tab to list: the one asked for, else long-form /videos."""
    base = (url or "").split("?")[0].split("#")[0].rstrip("/")
    tail = base.rsplit("/", 1)[-1].lower()
    if tail in VIDEO_TABS:
        return base                      # an explicit tab is taken as given
    if tail in OTHER_TABS:
        base = base.rsplit("/", 1)[0]    # a non-video tab is swapped for /videos
    return base + "/videos"


def channel_page(url, start, end):
    """One page of a channel's video list, newest first. Returns (data, error)."""
    for _ in range(2):
        cmd = base_cmd() + ["--flat-playlist", "--dump-single-json", "--no-warnings",
                            "--playlist-start", str(start),
                            "--playlist-end", str(end), "--", url]
        try:
            out = run_logged("channel_page", cmd, capture_output=True, text=True, timeout=300)
        except subprocess.TimeoutExpired:
            return {}, "Timed out while listing the channel"
        except OSError as e:
            return {}, f"Could not run yt-dlp: {e}"
        if out.returncode == 0:
            break
        msg = out.stderr.strip().splitlines()
        err = msg[-1] if msg else "Failed to list the channel"
        if not drop_rejected_cookies(err):
            return {}, err
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        return {}, "Could not read the channel listing (invalid JSON from yt-dlp)"
    # A channel that does not exist answers with a bare `null`.
    return (data, None) if isinstance(data, dict) else ({}, "Channel not found")


def as_seconds(value):
    """A duration as a number, or None when there is not one.

    Read rather than type-checked: a route that hands over "900" instead of 900
    describes a 15-minute video, and treating that as "length unknown" would
    drop it - which is the one outcome the length floor must never cause by
    accident. "NA" is yt-dlp's way of saying it does not know, and stays None.
    """
    if isinstance(value, bool):          # True is not a duration
        return None
    if not isinstance(value, (int, float)):
        try:
            value = float(str(value).strip())
        except (TypeError, ValueError):
            return None
    # A whole number stays an int, so a video's duration does not change JSON
    # type just because --sort sent the search down the other route.
    return int(value) if float(value).is_integer() else value


def explain_empty_listing(counts):
    """Say on screen why a channel listing kept nothing.

    The per-reason tally goes to the log through note(), which only reaches the
    screen with --verbose - so without this a channel whose every video was
    filtered out looks exactly like a channel with no videos at all. The length
    floor is on by default, which makes that the likely case rather than a rare
    one, so the reason is printed and the way to undo it is named.
    """
    if not counts.get("scanned"):
        return
    floor = duration_floor()
    reasons = [
        (counts.get("short"), f"{counts.get('short')} shorter than {human_time(floor)}"),
        (counts.get("nolength"), f"{counts.get('nolength')} of unknown length"),
        (counts.get("views"), f"{counts.get('views')} under {MIN_VIEWS:,} views"),
        (counts.get("unknown"), f"{counts.get('unknown')} with no view count"),
        (counts.get("live"), f"{counts.get('live')} live or upcoming"),
        (counts.get("skipped"), f"{counts.get('skipped')} in skip.txt"),
    ]
    said = ", ".join(text for number, text in reasons if number)
    print(f"      none of {counts['scanned']} video(s) matched"
          f"{': ' + said if said else ''}")
    if counts.get("short") or counts.get("nolength"):
        print("      (--shorts, or --min-duration 0, includes short-form videos)")


def duration_floor():
    """The shortest video to keep, in seconds. 0 means keep any length.

    --shorts is the switch and --min-duration is the dial; asking for shorts
    turns the floor off rather than arguing with a number that was set to keep
    them out.
    """
    return 0 if INCLUDE_SHORTS else max(0, MIN_DURATION)


def shorts_tab_note(tab):
    """Whether this listing is a Shorts tab, saying so when a floor is set.

    Asking for the Shorts tab is asking for Shorts, so the length floor is
    waived there - said out loud, because a --min-duration that quietly does
    nothing is worse than one that is refused.
    """
    if not is_shorts_tab(tab):
        return False
    if duration_floor():
        shown = (tab or "").replace("https://www.youtube.com/", "")
        print(f"      NOTE: {shown} is a Shorts tab, so the length floor "
              f"does not apply to it.")
    return True


def is_shorts_tab(url):
    """Whether this link points at a channel's Shorts tab.

    A link there is a request FOR short-form videos, so the length floor is not
    applied to it - and it would drop every one of them, since a Shorts listing
    reports no duration at all.
    """
    return (url or "").split("?")[0].rstrip("/").lower().endswith("/shorts")


def wanted_entry(entry, allow_short=False):
    """Whether a listed video should be downloaded: (yes, reason-if-not).

    "unknown" is kept apart from "too few views": a video YouTube listed without
    a view count cannot be shown to clear the floor, so it is left out - but
    quietly dropping it would look the same as a video that genuinely fell short.
    The same distinction is made for length: "short" is measured, "nolength" is
    a listing that did not say - which a Shorts tab never does.
    """
    entry = entry or {}
    if entry.get("live_status") in ("is_upcoming", "is_live", "post_live"):
        return False, "live"          # nothing finished to download yet
    floor = 0 if allow_short else duration_floor()
    if floor:
        seconds = as_seconds(entry.get("duration"))
        if seconds is None:
            # Length unknown, so it cannot be shown to be a long video. Kept
            # apart from "short" in the count, because the two are not the same
            # fact and a run that drops many of these should say so.
            return False, "nolength"
        if seconds < floor:
            return False, "short"
    if not MIN_VIEWS:
        return True, ""
    views = entry.get("view_count")
    if not isinstance(views, (int, float)):
        return False, "unknown"
    return (True, "") if views >= MIN_VIEWS else (False, "views")


def list_channel_recent(url, deadline=None):
    """Scan a channel using --dateafter to only extract recent videos.

    Uses yt-dlp's --dateafter + --break-on-reject so it stops as soon as it
    hits a video older than DAYS.  Slightly slower than --flat-playlist (which
    cannot filter by date at all) but only touches the recent videos instead of
    the whole channel.
    """
    tab = channel_videos_url(url)
    shorts_tab = shorts_tab_note(tab)
    links, seen, name = [], set(), ""
    counts = {"scanned": 0, "views": 0, "unknown": 0, "live": 0, "skipped": 0,
              "short": 0, "nolength": 0}

    cmd = base_cmd() + [
        # The channel name goes LAST, and the split below is bounded. A channel
        # named "NOOR | Holy Quran" adds a field to a "|"-joined record, and with
        # the name in the middle that shifted every field after it - which sent
        # duration off the end and made the length floor drop all 32 of that
        # channel's hour-long videos as "length unknown".
        "--print", "%(id)s|%(view_count)s|%(live_status)s|%(duration)s|%(channel)s",
        "--dateafter", f"today-{DAYS}days",
        "--break-on-reject",
        "--no-warnings",
        "--", tab
    ]

    try:
        out = run_logged("channel_recent", cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        scan_done()
        return links, name, "Timed out while scanning recent videos"
    except OSError as e:
        scan_done()
        return links, name, f"Could not run yt-dlp: {e}"

    # --break-on-reject exits with 101 after the first out-of-range video,
    # but the stdout lines for the in-range videos are still valid.
    for line in out.stdout.splitlines():
        if deadline is not None and time.monotonic() >= deadline:
            scan_done()
            return links, name, (f"stopped after scanning {counts['scanned']} videos to "
                                 f"answer in time; lower views to find matches sooner")
        line = line.strip()
        if not line:
            continue

        # maxsplit, so the name keeps any "|" of its own; and exactly five
        # fields, so a short record is skipped rather than read as blanks.
        parts = line.split("|", 4)
        if len(parts) != 5:
            continue
        vid, views, live_status, length, ch_name = parts

        counts["scanned"] += 1
        name = name or ch_name

        if vid in seen:
            continue

        if live_status in ("is_upcoming", "is_live", "post_live"):
            counts["live"] += 1
            continue

        floor = 0 if shorts_tab else duration_floor()
        if floor:
            seconds = as_seconds(length)
            if seconds is None:
                counts["nolength"] += 1
                continue
            if seconds < floor:
                counts["short"] += 1
                continue

        if views in ("NA", "None"):
            if MIN_VIEWS:
                counts["unknown"] += 1
                continue
        else:
            try:
                v_count = int(views)
                if MIN_VIEWS and v_count < MIN_VIEWS:
                    counts["views"] += 1
                    continue
            except ValueError:
                if MIN_VIEWS:
                    counts["unknown"] += 1
                    continue

        if vid in SKIP_IDS:
            counts["skipped"] += 1
            continue

        seen.add(vid)
        links.append(watch_url(vid))

        if LIMIT and len(links) >= LIMIT:
            break
        
    scan_done()
    note(f"      listed {counts['scanned']} video(s) from last {DAYS} days: kept {len(links)}"
         + (f", {counts['views']} under {MIN_VIEWS} views" if counts["views"] else "")
         + (f", {counts['skipped']} in skip.txt" if counts["skipped"] else "")
         + (f", {counts['live']} live/upcoming" if counts["live"] else "")
         + (f", {counts['short']} under {human_time(duration_floor())}"
            if counts["short"] else "")
         + (f", {counts['nolength']} of unknown length" if counts["nolength"] else ""))
    if not links:
        explain_empty_listing(counts)
    skipped_by_list[0] += counts["skipped"]
    if counts["unknown"]:
        print(f"      NOTE: {counts['unknown']} video(s) had no view count and "
              f"were left out (could not be shown to clear --views).")
    return links, name, None


def list_channel_videos(url, deadline=None):
    """A channel's videos worth downloading. Returns (links, channel name, error).

    With a deadline (a time.monotonic() value) it stops before the next page once
    that has passed, and answers with what it found and an error saying so - a
    preview has to answer in time even when few videos clear the views floor.

    Pages through the channel's video tab newest-first and stops as soon as
    --limit is satisfied, so a small --limit costs one listing request even on a
    channel with thousands of videos. With no --limit the whole channel is read.
    """
    if DAYS > 0:
        return list_channel_recent(url, deadline)

    tab = channel_videos_url(url)
    shorts_tab = shorts_tab_note(tab)
    links, seen, name = [], set(), ""
    counts = {"scanned": 0, "views": 0, "unknown": 0, "live": 0, "skipped": 0,
              "short": 0, "nolength": 0}
    start = 1
    while True:
        if deadline is not None and start > 1 and time.monotonic() >= deadline:
            scan_done()
            return links, name, (f"stopped after scanning {counts['scanned']} videos to "
                                 f"answer in time; lower views to find matches sooner")
        data, err = channel_page(tab, start, start + CHANNEL_PAGE - 1)
        if err:
            scan_done()
            return links, name, err
        name = name or data.get("channel") or data.get("uploader") or ""
        entries = data.get("entries") or []
        for entry in entries:
            counts["scanned"] += 1
            vid = (entry or {}).get("id")
            if not vid or vid in seen:
                continue
            keep, reason = wanted_entry(entry, allow_short=shorts_tab)
            if not keep:
                counts[reason] += 1
                continue
            # skip.txt is applied here, before --limit, so a video you asked to
            # leave alone does not use up one of the N slots.
            if vid in SKIP_IDS:
                counts["skipped"] += 1
                continue
            seen.add(vid)
            links.append(watch_url(vid))
            if LIMIT and len(links) >= LIMIT:
                break
        if LIMIT and len(links) >= LIMIT:
            break
        scan_progress(name or url, counts["scanned"], len(links))
        if len(entries) < CHANNEL_PAGE:
            break                        # the channel has no more videos
        start += CHANNEL_PAGE
    scan_done()
    note(f"      listed {counts['scanned']} video(s): kept {len(links)}"
         + (f", {counts['views']} under {MIN_VIEWS} views" if counts["views"] else "")
         + (f", {counts['skipped']} in skip.txt" if counts["skipped"] else "")
         + (f", {counts['live']} live/upcoming" if counts["live"] else "")
         + (f", {counts['short']} under {human_time(duration_floor())}"
            if counts["short"] else "")
         + (f", {counts['nolength']} of unknown length" if counts["nolength"] else ""))
    if not links:
        explain_empty_listing(counts)
    skipped_by_list[0] += counts["skipped"]
    if counts["unknown"]:
        print(f"      NOTE: {counts['unknown']} video(s) had no view count and "
              f"were left out (could not be shown to clear --views).")
    return links, name, None


def expand_sources(links):
    """Replace every channel link with the video links it stands for.

    A video link passes straight through, so links.txt can mix the two. Repeats
    are dropped by video id - so a video reached through both a channel and its
    own link is downloaded once, and --limit keeps meaning N videos.

    When there are multiple channel links, they are scanned concurrently so the
    total wall time is roughly that of the slowest single channel, not the sum.
    """
    skipped_by_list[0] = 0            # one expansion, one count
    channels = [u for u in links if is_channel_url(u)]
    if channels:
        floor = duration_floor()
        # A /shorts tab is exempt from the floor, so naming it for a links.txt
        # of nothing but /shorts links would describe a filter that cannot bite.
        if floor and all(is_shorts_tab(channel_videos_url(c)) for c in channels):
            floor = 0
        criteria = ", ".join(p for p in (
            f">= {MIN_VIEWS:,} views" if MIN_VIEWS else "",
            f"at least {human_time(floor)} long" if floor else "",
            f"newest {LIMIT} each" if LIMIT else "",
            f"last {DAYS} days" if DAYS else "",
        ) if p) or "every video"
        print(f"\n  Reading {len(channels)} channel link(s) ({criteria})...")
    elif (MIN_VIEWS or LIMIT) and not SEARCH_QUERY:
        # Not during a search: search_items() applies both to the results, so
        # saying they were ignored would be the opposite of what happened.
        print("\n  NOTE: --views/--limit only apply to channel links, and "
              "links.txt has none.")

    out, seen = [], set()

    def add(url, group=None):
        """Keep a link, unless that video is in the list already."""
        vid = video_id_from_url(url)
        if vid:
            if vid in seen:
                return
            seen.add(vid)
        out.append((url, group))          # not a video link: left alone, as before

    # Scan all channels concurrently, then merge results in link order.
    channel_results = {}  # url -> (found, name, err)
    if channels:
        workers = min(len(channels), 6)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(list_channel_videos, u): u for u in channels}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    channel_results[url] = future.result()
                except Exception as e:
                    channel_results[url] = ([], url, str(e))

    for url in links:
        if not is_channel_url(url):
            add(url, None)
            continue
        found, name, err = channel_results[url]
        label = name or url
        before = len(out)
        for u in found:
            add(u, url)
        kept = len(out) - before
        if err:
            # Whatever the listing reached before it broke is still real, so it
            # is used rather than thrown away - and said out loud, because a
            # part of a channel must never look like the whole of one.
            print(f"    ! {label}: {err}")
            if kept:
                print(f"      (listing stopped early; using the {kept} "
                      f"video(s) found before it failed)")
            log_record(url=url, status="channel_failed", channel=name,
                       error=err, videos=kept)
        else:
            print(f"    {label}: {kept} video(s)")
            log_record(url=url, status="channel", channel=name, videos=kept)
    return out


# A line in skip.txt may be a link or just the id on its own.
BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def read_skips(path):
    """Video ids from skip.txt: videos to leave alone whatever else asks for them.

    One link - or bare 11-character id - per line; blanks and # comments are
    ignored, exactly as in links.txt. A missing file simply means nothing.
    """
    ids = set()
    if not os.path.exists(path):
        return ids
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError as e:
        print(f"  NOTE: could not read {os.path.basename(path)} ({e}); "
              f"nothing will be excluded.")
        return ids
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        vid = video_id_from_url(line) or (line if BARE_ID_RE.match(line) else None)
        if vid:
            ids.add(vid)
    return ids


def drop_skipped(links):
    """Drop the links named in skip.txt. Returns (kept, how many went)."""
    if not SKIP_IDS:
        return links, 0
    
    kept = []
    for item in links:
        u = item[0] if isinstance(item, tuple) else item
        if video_id_from_url(u) not in SKIP_IDS:
            kept.append(item)
    return kept, len(links) - len(kept)


def plan_links(links):
    """Split eligible links into new media and already-finished videos."""
    pending, already = [], 0
    for item in links:
        url = item[0] if isinstance(item, tuple) else item
        vid = video_id_from_url(url)
        if vid and vid in DONE_INDEX:
            already += 1
        else:
            pending.append(item)
    return pending, already


def print_download_plan(source_links, links, pending, already_downloaded, excluded):
    """Show the exact filtered batch before any media download starts."""
    print("\n" + "=" * 60)
    print("  Download plan (no media downloaded yet)")
    print("=" * 60)
    print(f"  Source link(s)       : {source_links}")
    print(f"  Videos after filters : {len(links)}")
    print(f"  Already complete     : {already_downloaded}")
    print(f"  New videos to fetch  : {len(pending)}")
    print(f"  Skip-list exclusions : {excluded}")
    print(f"  Mode                 : {'channel folders' if CHANNEL_MODE else 'flat folders'}")
    print(f"  Quality              : {MAX_HEIGHT}p priority / {MIN_HEIGHT}p floor")
    print(f"  Captions             : {'required (' + SUB_LANG + ')' if DOWNLOAD_SUBS else 'off'}")
    if MIN_VIEWS:
        print(f"  View filter          : >= {MIN_VIEWS:,}")
    if DAYS:
        print(f"  Date filter          : last {DAYS} day(s)")
    if LIMIT:
        print(f"  Channel limit        : newest {LIMIT} match(es) per channel")
    if links:
        pending_ids = {id(item) for item in pending}
        print("  All eligible videos (new downloads start selected):")
        for number, item in enumerate(links, 1):
            url = item[0] if isinstance(item, tuple) else item
            marker = "x" if id(item) in pending_ids else "="
            state = "download" if marker == "x" else "already complete"
            print(f"    [{marker}] {number:>3}. {redact_url(url)}  ({state})")
    print("=" * 60)


def select_download_links(pending):
    """Interactively toggle every pending link with arrows and the spacebar.

    Returns the selected items, or None when the user cancels. The curses UI is
    used only for a real terminal; callers keep the non-interactive path for the
    API and redirected jobs.
    """
    try:
        import curses
    except ImportError:
        answer = input("  Terminal selector unavailable. Download all selected links? [y/N]: ")
        return list(pending) if answer.strip().lower() in ("y", "yes") else None

    selected = [True] * len(pending)

    def draw(stdscr, current):
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        visible = max(1, height - 5)
        top = max(0, min(current - visible + 1, len(pending) - visible))
        stdscr.addnstr(0, 0, "Select videos to download", max(0, width - 1), curses.A_BOLD)
        instructions = "Arrows move | Space toggle | a all | n none | Enter continue | q cancel"
        stdscr.addnstr(1, 0, instructions, max(0, width - 1))
        for row, index in enumerate(range(top, min(top + visible, len(pending))), 3):
            item = pending[index]
            url = item[0] if isinstance(item, tuple) else item
            marker = "x" if selected[index] else " "
            text = f"[{marker}] {index + 1:>3}. {redact_url(url)}"
            attr = curses.A_REVERSE if index == current else curses.A_NORMAL
            stdscr.addnstr(row, 0, text, max(0, width - 1), attr)
        footer = f"Selected {sum(selected)}/{len(selected)}"
        stdscr.addnstr(height - 1, 0, footer, max(0, width - 1), curses.A_BOLD)
        stdscr.refresh()

    def run(stdscr):
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        current = 0
        draw(stdscr, current)
        while True:
            key = stdscr.getch()
            if key in (curses.KEY_UP, ord("k")):
                current = max(0, current - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                current = min(len(pending) - 1, current + 1)
            elif key == curses.KEY_PPAGE:
                current = max(0, current - max(1, stdscr.getmaxyx()[0] - 5))
            elif key == curses.KEY_NPAGE:
                current = min(len(pending) - 1, current + max(1, stdscr.getmaxyx()[0] - 5))
            elif key == ord(" "):
                selected[current] = not selected[current]
            elif key in (ord("a"), ord("A")):
                selected[:] = [True] * len(selected)
            elif key in (ord("n"), ord("N")):
                selected[:] = [False] * len(selected)
            elif key in (ord("q"), ord("Q"), 27):
                return None
            elif key in (10, 13, curses.KEY_ENTER):
                return [item for item, keep in zip(pending, selected) if keep]
            draw(stdscr, current)

    try:
        return curses.wrapper(run)
    except curses.error:
        answer = input("  Terminal selector failed. Download all selected links? [y/N]: ")
        return list(pending) if answer.strip().lower() in ("y", "yes") else None


def choose_download_links(args, links, pending):
    """Return the post-selection worklist, or None when the user cancels."""
    if args.dry_run:
        print("  Dry run complete. Nothing was downloaded.")
        log_debug("plan_confirmation", method="dry-run", proceeded=False)
        return None
    if args.yes:
        log_debug("plan_confirmation", method="--yes", proceeded=True)
        return links
    if not pending:
        log_debug("plan_confirmation", method="nothing-new", proceeded=True,
                  selected_downloads=0)
        return links
    if not sys.stdin.isatty():
        print("  Non-interactive input detected; proceeding without a prompt.")
        log_debug("plan_confirmation", method="non-interactive", proceeded=True)
        return links
    chosen = select_download_links(pending)
    if chosen is None:
        print("  Cancelled. Nothing was downloaded.")
        log_debug("plan_confirmation", method="selector", proceeded=False)
        return None
    pending_ids = {id(item) for item in pending}
    chosen_ids = {id(item) for item in chosen}
    selected_links = [item for item in links
                      if id(item) not in pending_ids or id(item) in chosen_ids]
    print(f"  Selected {len(chosen)} of {len(pending)} new video(s).")
    log_debug("plan_confirmation", method="selector", proceeded=bool(chosen),
              selected_downloads=len(chosen), planned_downloads=len(pending))
    return selected_links if chosen else None


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="YouTube batch downloader (yt-dlp) - 1080p priority, 720p floor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    def count(value):
        """A count flag's value: a whole number, never negative.

        Several of these read 0 as "no limit", so a negative one slipping
        through would quietly mean the opposite of what was asked - a
        --comment-limit of -1 would fetch every comment there is.
        """
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise argparse.ArgumentTypeError(f"{value!r} is not a whole number")
        if number < 0:
            raise argparse.ArgumentTypeError(
                f"cannot be negative (got {number}); 0 means no limit")
        return number

    p.add_argument("directory",
                   help="Folder containing cookies.txt and links.txt (videos download here too).")
    p.add_argument("--channel", action="store_true",
                   help="Group videos by channel: <dir>/<Channel Name>/<Video Title>/")
    p.add_argument("--min-height", type=int, default=720, help="Quality floor (default 720).")
    p.add_argument("--max-height", type=int, default=1080, help="Quality ceiling (default 1080).")
    p.add_argument("--views", "--view", "--min-views", type=count, default=0,
                   dest="views", metavar="N",
                   help="Channel links: only videos with at least N views.")
    p.add_argument("--limit", type=count, default=0, metavar="N",
                   help="Channel links: stop after the newest N matching "
                        "videos (default: the whole channel).")
    p.add_argument("--days", type=count, default=0, metavar="N",
                   help="Only download videos uploaded in the last N days (0 = any time).")
    p.add_argument("--search", metavar="QUERY", default=None,
                   help="Search YouTube for QUERY and use the results as the "
                        "links (filters: --days/--this-month, --views, --limit).")
    p.add_argument("--in-channel", metavar="URL", default=None, dest="in_channel",
                   help="With --search: search inside this channel only, using "
                        "the channel's own search tab.")
    p.add_argument("--search-count", type=count, default=None, metavar="N",
                   dest="search_count",
                   help=f"Results to scan before filtering. Default follows the "
                        f"route, because their costs differ about thirtyfold: "
                        f"{SEARCH_DEPTH_FLAT} normally (about 0.07s each), "
                        f"{SEARCH_DEPTH_DATED} with a date window or a date sort "
                        f"(about 2s each). Give a number to override either.")
    p.add_argument("--sort", "--sort-by", dest="sort", default="relevance",
                   choices=("relevance", "views", "popularity", "least-viewed",
                            "date", "oldest"),
                   help="Order the search results: relevance (default), views "
                        "(popularity), least-viewed (reverse popularity), "
                        "date (newest first) or oldest.")
    p.add_argument("--match", dest="match", default="related",
                   choices=("related", "exact"),
                   help="How wide a net the search casts: related (default) uses "
                        "YouTube's own results page, so near-matches come too - "
                        "the same story from another channel, the opposite angle, "
                        "another language; exact uses yt-dlp's narrower "
                        "ytsearch and returns far fewer.")
    p.add_argument("--this-month", action="store_true", dest="this_month",
                   help="Search window: since the 1st of the current month.")
    p.add_argument("--no-dates", action="store_true", dest="no_dates",
                   help=f"Do not fetch publish dates for search results. They "
                        f"cost about 2.1s per video and are fetched only for "
                        f"the results that survive the filters, up to "
                        f"{DATE_RESOLVE_MAX} of them.")
    p.add_argument("--shorts", action="store_true",
                   help="Include short-form videos (Shorts). Left out by default.")
    p.add_argument("--min-duration", type=count, default=SHORTS_SECONDS, metavar="SECONDS",
                   dest="min_duration",
                   help=f"Shortest video to keep, in seconds (default "
                        f"{SHORTS_SECONDS} - a Short is at most 3 minutes). "
                        f"0 = any length, same as --shorts.")
    p.add_argument("--cookies", metavar="FILE",
                   help="Use this cookies.txt instead of <directory>/cookies.txt.")
    p.add_argument("--links", metavar="FILE",
                   help="Read the links from FILE instead of <directory>/links.txt.")
    p.add_argument("--no-subs", action="store_true",
                   help="Do NOT build transcripts (transcripts are built by default).")
    p.add_argument("--sub-lang", default="en",
                   help="Transcript language code (default en).")
    p.add_argument("--transcript", action="store_true",
                   help="Transcript only: write trans_/words_ .txt and never "
                        "download the video.")
    p.add_argument("--extract-comments", action="store_true",
                   dest="extract_comments",
                   help="Also save each video's comments to comments_<name>.txt.")
    p.add_argument("--comment-limit", type=count, default=100, metavar="N",
                   dest="comment_limit",
                   help="Top-level comments per video (default 100; 0 = every one).")
    p.add_argument("--comment-replies", action="store_true",
                   dest="comment_replies",
                   help="Also fetch the replies under those comments (off by default).")
    p.add_argument("--comment-sort", choices=("top", "new"), default="top",
                   dest="comment_sort",
                   help="Comment order: top (YouTube's ranking, default) or new.")
    p.add_argument("--no-thumbnail", action="store_true",
                   help="Do NOT save the thumbnail (saved by default).")
    p.add_argument("--no-description", action="store_true",
                   help="Do NOT save the description (saved by default).")
    p.add_argument("--dry-run", action="store_true",
                   help="Show the filtered download plan and exit without downloading.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the confirmation prompt and start downloading immediately.")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Show yt-dlp's full output instead of the progress bar.")
    # Kept hidden for compatibility with older commands. The diagnostic trace
    # is now always written; no flag is needed.
    p.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    args = p.parse_args(argv)
    if args.search is not None and not args.search.strip():
        # An empty --search used to fall through to links.txt, which downloads
        # something quite different from what was asked for.
        p.error("--search needs something to search for.")
    if args.in_channel and not args.search:
        p.error("--in-channel says where to search; it needs --search QUERY too.")
    if args.in_channel and not is_channel_url(args.in_channel):
        p.error(f"--in-channel wants a channel link, not {args.in_channel!r}.")
    if args.days and args.this_month:
        p.error("--days and --this-month are two different windows. Pick one.")
    if args.this_month and not args.search:
        # --this-month is a search window and nothing else reads it, so without
        # --search it would be accepted and then ignored - and the run would
        # download every video in links.txt, month or no month.
        p.error("--this-month is a search window; it needs --search QUERY too. "
                "To limit a download by date use --days N.")
    if args.transcript and args.no_subs:
        # --transcript asks for a transcript and --no-subs says build none.
        # Together the run would fetch metadata for every video and write
        # nothing, so it is an error rather than a silent no-op.
        p.error("--transcript and --no-subs contradict each other: "
                "--transcript IS the transcript. Drop one.")
    return args


def use_utf8_output():
    """Print UTF-8 regardless of the console's code page.

    Video titles routinely contain characters (＂ ： ？ …) that Windows' default
    cp1252 cannot encode, and printing one there raises UnicodeEncodeError the
    moment output is piped or redirected to a file.

    Line buffering is turned on for the same reason it matters here: yt-dlp
    writes straight to the same handle, so without it our own lines sit in a
    buffer and land out of order in a redirected log.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, OSError):
            pass


def stop_on_sigterm(signum, frame):
    """A plain kill ends the run like an exit, so the private cookie copy is removed.

    yt-dlp and its aria2c get it too. A kill sent to this process alone would leave
    aria2c downloading - and holding the output pipe open, so the wait for yt-dlp
    would never end. Only a process leading its own group signals the group: one
    started inside someone else's would signal them as well.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    STOPPING[0] = True                    # and a Ctrl+C now does not cut the stop short
    if os.name == "posix" and os.getpgrp() == os.getpid():
        try:
            os.killpg(os.getpgrp(), signal.SIGTERM)
        except OSError:
            pass
    sys.exit(128 + signum)


def main():
    global LINKS_FILE, COOKIES_FILE, DOWNLOAD_DIR, MAX_HEIGHT, MIN_HEIGHT, FORMAT
    global DOWNLOAD_SUBS, SUB_LANG, CHANNEL_MODE, DONE_INDEX
    global SAVE_THUMBNAIL, SAVE_DESCRIPTION, VERBOSE
    global MIN_VIEWS, LIMIT, DAYS, SKIP_FILE, SKIP_IDS, TRANSCRIPT_ONLY
    global EXTRACT_COMMENTS, COMMENT_LIMIT, COMMENT_REPLIES, COMMENT_SORT
    global SEARCH_QUERY, SEARCH_COUNT, SORT_BY, THIS_MONTH, IN_CHANNEL
    global SEARCH_MATCH, INCLUDE_SHORTS, MIN_DURATION, RESOLVE_DATES

    use_utf8_output()
    args = parse_args()
    DOWNLOAD_DIR  = os.path.expanduser(args.directory)
    LOG_JSON[0]   = os.path.join(DOWNLOAD_DIR, "download_log.json")   # a stop during startup has a summary
    signal.signal(signal.SIGTERM, stop_on_sigterm)
    COOKIES_FILE  = (os.path.expanduser(args.cookies) if args.cookies
                     else os.path.join(DOWNLOAD_DIR, "cookies.txt"))
    LINKS_FILE    = (os.path.expanduser(args.links) if args.links
                     else os.path.join(DOWNLOAD_DIR, "links.txt"))
    MIN_HEIGHT    = args.min_height
    MAX_HEIGHT    = args.max_height
    DOWNLOAD_SUBS = not args.no_subs
    SUB_LANG      = args.sub_lang
    CHANNEL_MODE  = args.channel
    TRANSCRIPT_ONLY = args.transcript
    EXTRACT_COMMENTS = args.extract_comments
    COMMENT_LIMIT = max(0, args.comment_limit)
    COMMENT_REPLIES = args.comment_replies
    COMMENT_SORT = args.comment_sort
    SEARCH_QUERY = args.search
    # None means "pick by route"; see search_depth().
    SEARCH_COUNT = max(1, args.search_count) if args.search_count else None
    # "popularity" is what a person calls it; "views" is what it sorts on.
    SORT_BY = "views" if args.sort == "popularity" else args.sort
    THIS_MONTH = args.this_month
    IN_CHANNEL = args.in_channel
    SEARCH_MATCH = args.match
    RESOLVE_DATES = not args.no_dates
    INCLUDE_SHORTS = args.shorts
    MIN_DURATION = args.min_duration
    # Nobody asking for a transcript wants a .jpg; the description is kept
    # because it is written from metadata already fetched and costs no request.
    SAVE_THUMBNAIL = not args.no_thumbnail and not TRANSCRIPT_ONLY
    SAVE_DESCRIPTION = not args.no_description
    VERBOSE       = args.verbose
    MIN_VIEWS     = args.views
    LIMIT         = args.limit
    DAYS          = args.days
    FORMAT        = build_format()
    SKIP_FILE     = os.path.join(DOWNLOAD_DIR, "skip.txt")
    SKIP_IDS      = read_skips(SKIP_FILE)

    print("=" * 60)
    print(f"  YouTube Downloader (yt-dlp) - {MAX_HEIGHT}p priority / {MIN_HEIGHT}p floor")
    print("=" * 60)
    print(f"  Folder  : {DOWNLOAD_DIR}")
    print(f"  Cookies : {COOKIES_FILE if os.path.exists(COOKIES_FILE) else 'none (optional)'}")
    print(f"  Links   : {LINKS_FILE}")
    print(f"  Speed   : {'aria2c, 16 connections' if ARIA2C else 'built-in (install aria2c for a big speedup)'}")
    print(f"  PO token: {'bgutil script (headless)' if BGUTIL_SCRIPT else 'none found - some videos may fail with 403'}")
    print(f"  Layout  : {'<Channel Name>/<Video Title>/' if CHANNEL_MODE else '<Video Title>/'}")
    if TRANSCRIPT_ONLY:
        print("  Output  : transcript only - no video is downloaded")
    if EXTRACT_COMMENTS:
        print(f"  Comments: {COMMENT_LIMIT or 'all'} top-level, sort {COMMENT_SORT}"
              f", replies {'on' if COMMENT_REPLIES else 'off'}")
    if SEARCH_QUERY:
        where = f" in {IN_CHANNEL}" if IN_CHANNEL else " on YouTube"
        depth = search_depth(needs_dated_route())
        print(f"  Search  : {SEARCH_QUERY}{where}  (scan {depth}"
              f"{'' if SEARCH_COUNT else ' by default'}, sort {SORT_BY})")
        if not IN_CHANNEL:
            print(f"  Match   : {SEARCH_MATCH}"
                  f"{' - near-matches come too' if SEARCH_MATCH == 'related' else ' - tight matches only'}")
    floor = duration_floor()
    if floor:
        # "(no Shorts)" only when the floor IS the Shorts ceiling; a floor the
        # user chose is a length rule, and calling a 4-minute video a Short
        # would be wrong. The scope is said out loud: a link you name yourself
        # is never length-filtered.
        print(f"  Length  : at least {human_time(floor)}"
              f"{' (no Shorts)' if floor == SHORTS_SECONDS else ''}"
              f" - searches and channel listings only")
    else:
        print("  Length  : any, short-form included")
    if MIN_VIEWS or LIMIT or DAYS:
        print(f"  Channels: {f'>= {MIN_VIEWS:,} views' if MIN_VIEWS else 'every video'}"
              f"{f', newest {LIMIT} each' if LIMIT else ''}"
              f"{f', last {DAYS} days' if DAYS else ''}")
    print(f"  Skip    : {f'skip.txt ({len(SKIP_IDS)} video(s))' if SKIP_IDS else 'none'}")
    print(f"  Transcript: {'yes (' + SUB_LANG + ', trans_ + words_ .txt)' if DOWNLOAD_SUBS else 'no'}")
    extras = [n for n, on in (("thumbnail", SAVE_THUMBNAIL),
                              ("description", SAVE_DESCRIPTION)) if on]
    print(f"  Extras  : {', '.join(extras) if extras else 'none'}")
    print(f"  Log     : download_log.txt + download_log.json + download_debug.jsonl"
          f"{'  (--verbose: full output on screen)' if VERBOSE else ''}")

    if not os.path.isdir(DOWNLOAD_DIR):
        print(f"\nERROR: directory not found: {DOWNLOAD_DIR}")
        sys.exit(1)

    LOG_FILE[0] = os.path.join(DOWNLOAD_DIR, "download_log.txt")
    LOG_JSON[0] = os.path.join(DOWNLOAD_DIR, "download_log.json")
    LOG_JSONL[0] = os.path.join(DOWNLOAD_DIR, "download_log.jsonl")
    LOG_DEBUG[0] = os.path.join(DOWNLOAD_DIR, "download_debug.jsonl")
    RUN_ID[0] = (datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
                 + f"-{os.getpid()}")
    try:
        open(LOG_JSONL[0], "w", encoding="utf-8").close()   # this run's lines only
    except OSError:
        LOG_JSONL[0] = None
    try:
        with open(LOG_FILE[0], "w", encoding="utf-8") as f:
            f.write(f"# run started {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except OSError as e:
        print(f"\nNOTE: could not open the log file ({e}); continuing without it.")
        LOG_FILE[0] = None
    try:
        open(LOG_DEBUG[0], "w", encoding="utf-8").close()
    except OSError as e:
        print(f"NOTE: could not open the diagnostic log ({e}); continuing without it.")
        LOG_DEBUG[0] = None

    log_debug("run_start", directory=DOWNLOAD_DIR, links_file=LINKS_FILE,
              cookies_present=os.path.exists(COOKIES_FILE), channel=CHANNEL_MODE,
              min_height=MIN_HEIGHT, max_height=MAX_HEIGHT,
              transcripts=DOWNLOAD_SUBS, sub_lang=SUB_LANG,
              thumbnail=SAVE_THUMBNAIL, description=SAVE_DESCRIPTION,
              yt_dlp=shutil.which("yt-dlp"), ffmpeg=shutil.which("ffmpeg"),
              aria2c=ARIA2C, bgutil_script=bool(BGUTIL_SCRIPT))

    missing = [tool for tool in ("yt-dlp", "ffmpeg") if not shutil.which(tool)]
    if missing:
        print(f"\nERROR: not installed or not on PATH: {', '.join(missing)}")
        print("  macOS:   brew install " + " ".join(missing))
        print("  Windows: see requirements.txt")
        sys.exit(1)

    if not os.path.exists(COOKIES_FILE):
        print("\nNOTE: no cookies.txt in the folder - that is fine for public "
              "videos.\n      Add one only for private / members-only / "
              "age-restricted videos.")

    # A search replaces links.txt as the source: it writes its results to a
    # links file and the rest of the run - skip list, plan, preflight, download -
    # proceeds exactly as if that file had been there all along.
    if SEARCH_QUERY:
        found = run_search(args)
        if not found:
            write_empty_summary()       # every return from main() leaves a summary
            return
        LINKS_FILE = found

    links = read_links(LINKS_FILE)
    if not links:
        print(f"No links found in {LINKS_FILE}. Add one link per line.")
        write_empty_summary()
        return
    source_links = len(links)

    # A channel link stands for many videos, so it is expanded before anything
    # else counts links. skip.txt is applied to what comes out (channel listings
    # already leave those videos out, so this catches the plain video links).
    try:
        links = expand_sources(links)
    except KeyboardInterrupt:
        scan_done()
        print("\n\n  Stopped while reading the channel list; nothing was downloaded.")
        write_empty_summary(stopped=True)
        return
    links, excluded = drop_skipped(links)
    excluded += skipped_by_list[0]      # the channel listings applied it too
    if excluded:
        print(f"  Excluded {excluded} video(s) listed in skip.txt.")
    if not links:
        print("\nNothing left to download.")
        write_empty_summary(excluded=excluded)
        return

    # Resume: one scan of what is already finished (either layout), keyed by
    # video id. Everything after this is a dictionary lookup.
    # Each mode has its own idea of "already done": a transcript run is finished
    # with a video once its trans_ file exists, with or without the video file.
    DONE_INDEX = (index_transcribed(DOWNLOAD_DIR) if TRANSCRIPT_ONLY
                  else index_downloaded(DOWNLOAD_DIR))
    if DONE_INDEX:
        print(f"  Resuming: {len(DONE_INDEX)} video(s) already downloaded here.")

    pending, already_downloaded = plan_links(links)
    print_download_plan(source_links, links, pending, already_downloaded, excluded)
    log_debug("download_plan", source_links=source_links, eligible_links=len(links),
              planned_downloads=len(pending), already_downloaded=already_downloaded,
              excluded=excluded, dry_run=args.dry_run)
    if args.dry_run:
        write_plan_summary(source_links, len(links), len(pending), already_downloaded,
                           excluded=excluded, dry_run=True, confirmed=False)
        return
    selected_links = choose_download_links(args, links, pending)
    if selected_links is None:
        write_plan_summary(source_links, len(links), len(pending), already_downloaded,
                           excluded=excluded, dry_run=False, confirmed=False,
                           selected_downloads=0)
        return
    links = selected_links
    selected_downloads = len(links) - already_downloaded

    total = len(links)
    run_started = time.monotonic()
    counts = {"ok": 0, "skip": 0}
    failed = []

    gone = []

    def run(batch, sweep=False):
        """Download a list of links. `failed` holds the ones worth trying again, kept
        current as each video ends, so a stop mid-batch still counts them."""
        skip_groups = set()
        for i, (url, group) in enumerate(batch, 1):
            if group and group in skip_groups:
                counts["skip"] += 1
                continue
            try:
                result = download_one(url, i, len(batch))
            except OSError as e:
                # A full disk, a name the filesystem refuses: this video fails,
                # the batch goes on.
                print(f"    ! {url}: {e}")
                log_record(url=url, status="failed", error=str(e))
                result = "fail"
            
            if result == "skip_date" and group:
                skip_groups.add(group)
                result = "skip"

            if sweep and result != "fail":
                failed.remove((url, group))    # the sweep settled it
            if result in counts:
                counts[result] += 1
            elif result == "gone":
                gone.append((url, group))      # nothing a retry could change
            elif not sweep:
                failed.append((url, group))

    stopped = False
    RUN_LOOP_STARTED[0] = True
    try:
        run(links)

        # A final sweep. Whatever was wrong earlier — a throttled stretch, a
        # stale token, a stream that dropped — has usually passed by the end of
        # a long batch, and these videos get a fresh set of attempts.
        if failed:
            print("\n" + "=" * 60)
            print(f"  Final sweep: retrying {len(failed)} video(s) that failed.")
            print("=" * 60)
            time.sleep(RETRY_DELAYS[-1])
            run(list(failed), sweep=True)
    except KeyboardInterrupt:
        # Ctrl+C should leave a readable summary, not a stack trace. Finished
        # videos are already on disk, and the next run resumes from there.
        stopped = True
        print("\n\n  Stopped. Finished videos are saved; re-run to continue.")
    # The totals below are the run's answer; a Ctrl+C now must not cut them short.
    STOPPING[0] = True

    print("\n" + "=" * 60)
    done = counts["ok"] + counts["skip"] + len(failed) + len(gone)
    made = "transcribed" if TRANSCRIPT_ONLY else "downloaded"
    print(f"  Summary: {counts['ok']} {made}, {counts['skip']} skipped "
          f"(already done), {len(failed) + len(gone)} failed  (of "
          f"{done if stopped else total} links"
          f"{f'; stopped early, {total - done} not reached' if stopped else ''}).")
    if gone:
        print("  Not retried (unavailable, or YouTube refused this IP):")
        for u in gone:
            print(f"    - {u}")
    if not use_cookies[0] and os.path.exists(COOKIES_FILE):
        print("  NOTE: this run went without cookies.txt (see the NOTE above). Export")
        print("        a fresh one from a private browser window if you need it.")
    elif any(is_bot_gated(r.get("error")) for r in LOG_RECORDS):
        print("  NOTE: YouTube bot-gated this IP. That is what a datacenter IP")
        print("        (Colab, a VPS) looks like to it. Put a cookies.txt in the")
        print("        folder, or run from a home connection.")
    if failed:
        print("  Failed links (see each folder's videoinfo.txt for details):")
        for u in failed:
            print(f"    - {u}")

    if counts["ok"] and TRANSCRIPT_ONLY:
        # No media was fetched, so "Downloaded N video(s), 0 B" would describe
        # a different run from the one that just happened.
        print(f"  Wrote {TRANSCRIPT_COUNT[0]} transcript(s) - no video was downloaded")
    elif counts["ok"]:
        rate = (f"  ({human_size(STATS['bytes'] / STATS['seconds'])}/s average)"
                if STATS["seconds"] > 0 else "")
        print(f"  Downloaded {counts['ok']} video(s), {human_size(STATS['bytes'])} "
              f"in {human_time(STATS['seconds'])} of downloading{rate}")
    if COMMENT_COUNT[0]:
        print(f"  Saved {COMMENT_COUNT[0]} comment(s)")
    run_seconds = time.monotonic() - run_started
    print(f"  Total run time: {human_time(run_seconds)}")
    print("=" * 60)

    write_json_log({
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layout": "channel" if CHANNEL_MODE else "flat",
        "min_views": MIN_VIEWS or None,
        "limit": LIMIT or None,
        "days": DAYS or None,
        "excluded_by_skip_list": excluded,
        "source_links": source_links,
        "links": total,
        "planned_downloads": len(pending),
        "selected_downloads": selected_downloads,
        "already_downloaded": already_downloaded,
        **mode_fields(),
        "downloaded": 0 if TRANSCRIPT_ONLY else counts["ok"],
        "skipped": counts["skip"],
        "failed": len(failed) + len(gone)
                  + sum(1 for r in LOG_RECORDS if r.get("status") == "channel_failed"),
        "stopped_early": stopped,
        "bytes": STATS["bytes"],
        "size": human_size(STATS["bytes"]),
        "download_seconds": round(STATS["seconds"], 1),
        "run_seconds": round(run_seconds, 1),
        "average_speed": (f"{human_size(STATS['bytes'] / STATS['seconds'])}/s"
                          if STATS["seconds"] > 0 else None),
        "debug_log": os.path.basename(LOG_DEBUG[0]) if LOG_DEBUG[0] else None,
    })


if __name__ == "__main__":
    signal.signal(signal.SIGINT, on_sigint)
    try:
        main()
    except KeyboardInterrupt:          # before the run loop, which has its own handler
        scan_done()
        if not SUMMARY_WRITTEN[0] and not RUN_LOOP_STARTED[0]:
            folder = ""
            if not LOG_JSON[0]:           # so early that main() had not read the arguments yet
                try:
                    folder = os.path.expanduser(parse_args().directory)
                except (SystemExit, Exception):   # the arguments themselves were the problem
                    pass
            if not LOG_JSON[0] and os.path.isdir(folder):
                LOG_JSON[0] = os.path.join(folder, "download_log.json")
            print("\n\n  Stopped before anything was downloaded.")
            write_empty_summary(stopped=True)
