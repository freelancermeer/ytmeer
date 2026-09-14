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
    --no-thumbnail       Do NOT save the thumbnail (saved by default)
    --no-description     Do NOT save the description (saved by default)

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

IS_WINDOWS = platform.system() == "Windows"

# aria2c downloads one file over many parallel connections — the same trick IDM
# uses. YouTube throttles each single connection well below the line rate, so
# this is the difference between a few hundred KB/s and saturating the link.
ARIA2C = shutil.which("aria2c")
ARIA2C_ARGS = ("-x16 -s16 -k1M -c --file-allocation=none "
               "--console-log-level=warn --summary-interval=1")


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


def write_empty_summary(stopped=False, excluded=0):
    """The JSON log for a run that ended before downloading anything, so a caller
    always has a summary. Unreadable channels are counted as failed, since they are
    usually why there was nothing to do."""
    write_json_log({
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layout": "channel" if CHANNEL_MODE else "flat",
        "min_views": MIN_VIEWS or None, "limit": LIMIT or None,
        "excluded_by_skip_list": excluded, "links": 0, "downloaded": 0, "skipped": 0,
        "failed": sum(1 for r in LOG_RECORDS if r.get("status") == "channel_failed"),
        "stopped_early": stopped, "bytes": 0, "size": human_size(0),
        "download_seconds": 0.0, "run_seconds": 0.0, "average_speed": None,
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


def run_streaming(cmd, progress=None):
    """Run a command and capture its output. Returns the captured text.

    With --verbose the raw output goes straight to the terminal, so yt-dlp's own
    progress bar behaves as usual. Otherwise every line is written to the log
    file and only the percentage and speed are lifted out, to drive the compact
    one-line display.
    """
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        last_returncode[0] = 1
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
    return b"".join(chunks).decode("utf-8", "replace")


# The `mweb` client makes yt-dlp mint the "Proof of Origin" token that YouTube
# now demands for some videos; without one those downloads die with HTTP Error
# 403 (fix_yt_dlp_windows.md #2).
PO_TOKEN_FLAGS = ["--extractor-args", "youtube:player_client=mweb,default"]

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


def write_trans_txt(out_txt, words):
    """Default style: grouped, wrapped lines with [hh:mm:ss] timestamps."""
    with open(out_txt, "w", encoding="utf-8") as f:
        for start, text in group_words_into_lines(words):
            text = one_line(text)
            if text:
                f.write(f"{format_timestamp(start)} {text}\n")


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


def write_words_txt(out_txt, words):
    """Oneword style: one word per line with [hh:mm:ss.mmm] timestamps."""
    with open(out_txt, "w", encoding="utf-8") as f:
        for start, _end, text in words:
            word = one_line(text)
            if word:
                f.write(f"{format_timestamp_ms(start)} {word}\n")


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
    if SAVE_THUMBNAIL and not thumb:
        name = os.path.basename(folder.rstrip("/\\"))
        # Fetching a thumbnail on its own runs into the same bot check the video
        # does, so it gets the same escalation to the PO-token client.
        routes = [PO_TOKEN_FLAGS] if po_token_first[0] else [[], PO_TOKEN_FLAGS]
        for extra in routes:
            for _ in range(2):
                cmd = base_cmd() + ["--skip-download", "--no-warnings"] + thumb_flags() \
                    + list(extra) + ["-o", output_template(folder, name), "--", url]
                out = subprocess.run(cmd, capture_output=True, text=True)
                thumb = find_thumbnail(folder)
                if thumb or not drop_rejected_cookies(out.stderr):
                    break
            if thumb:
                break

    desc = find_description(folder) if SAVE_DESCRIPTION else None
    if SAVE_DESCRIPTION and not desc:
        desc = write_description(folder, info)
    return thumb, desc


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
        routes = [PO_TOKEN_FLAGS] if po_token_first[0] else [[], PO_TOKEN_FLAGS]
    for extra in routes:
        for _ in range(2):
            cmd = base_cmd() + ["--skip-download"] + flags + list(extra) + [
                "--no-warnings", "-o", output_template(folder, name), "--", url,
            ]
            out = subprocess.run(cmd, capture_output=True, text=True)
            if find_json3(folder):
                return True
            if not drop_rejected_cookies(out.stderr):
                break
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
    try:
        return find_transcripts(folder, url, info)
    finally:
        clear_captions(folder)


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

    entries = take_caption(folder)          # whatever came down with the video
    if is_word_level(entries):
        return write_transcripts(folder, entries)

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
            return write_transcripts(folder, got)
        fallback = fallback or got
    return write_transcripts(folder, fallback)


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
    """Add/refresh the Thumbnail: and Description: lines in an existing videoinfo.txt."""
    info = os.path.join(folder, "videoinfo.txt")
    if not os.path.exists(info):
        return
    try:
        with open(info, "r", encoding="utf-8") as f:
            kept = [ln for ln in f.read().splitlines()
                    if not ln.startswith(("Thumbnail:", "Description:"))]
    except OSError:
        return
    if SAVE_THUMBNAIL:
        kept.append(f"Thumbnail:  {os.path.basename(thumbnail) if thumbnail else 'none'}")
    if SAVE_DESCRIPTION:
        kept.append(f"Description: {os.path.basename(description) if description else 'none'}")
    with open(info, "w", encoding="utf-8") as f:
        f.write("\n".join(kept) + "\n")


def download_subs(folder, url):
    """Backfill: build the transcripts for an already-downloaded video."""
    if not DOWNLOAD_SUBS:
        return None, None
    existing = find_transcript_file(folder)
    if existing:
        name = os.path.basename(existing)
        for prefix in ("words_", "words_not_found_"):
            companion = os.path.join(folder, name.replace("trans_", prefix, 1))
            if os.path.exists(companion):
                return existing, companion
        return existing, None
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
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
        out = subprocess.run(
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
        vid = video_id_from_url(fields.get("Link", ""))
        if vid and vid not in index and find_video_file(folder):
            index[vid] = folder

    try:
        for name in sorted(os.listdir(base)):
            level1 = os.path.join(base, name)
            if not os.path.isdir(level1):
                continue
            consider(level1)                       # flat layout
            try:
                for sub in sorted(os.listdir(level1)):
                    level2 = os.path.join(level1, sub)
                    if os.path.isdir(level2):
                        consider(level2)           # channel layout
            except OSError:
                pass
    except OSError:
        pass
    return index


def write_info(folder, title, url, quality, status, error=None,
               transcript=None, words=None, thumbnail=None, description=None,
               info=None):
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
    if wants_thumb or wants_desc or wants_counts:
        info = None
        if wants_desc or wants_counts:
            info, _ = fetch_info(url)
        thumb, desc = build_extras(folder, url, info)
        for path in (thumb, desc):
            if path:
                note(f"      + {os.path.basename(path)}")
        update_info_extras(folder, thumb, desc)
        if wants_counts and info:
            update_info_counts(folder, info)
            note("      + views and subscriber count")
    # Recorded last: the moment this record exists it is streamed, and whoever
    # reads it may open the folder straight away - so the backfill must be done.
    log_record(url=url, status="skipped", folder=rel(folder))


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
            err = attempt(flags, fast)
            if err and drop_rejected_cookies(err):
                err = attempt(flags, fast)
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
    note(f"\n[{index}/{total}] {url}")
    bar = ProgressLine(index, total, url)

    # 0) Skip if this video is already finished — matched by video id, so a link
    #    repeated with a different tracking suffix is recognised. No network call.
    vid = video_id_from_url(url)
    if vid and vid in DONE_INDEX:
        skip_finished(DONE_INDEX[vid], url, bar)
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
        skip_finished(DONE_INDEX[vid], url, bar)
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
        return "gone" if is_permanent(err) else "fail"

    # 2) Download best 720p-1080p video+audio (merged mp4) + English json3 caption.
    # Named after the folder actually used - "<title> [<id>]" when another video
    # already has "<title>" - so every name in it (video, trans_, words_, and the
    # .f137/.temp streams yt-dlp writes on the way) shares one stem.
    out_template = output_template(folder, os.path.basename(folder))
    sub_lang, sub_source, sub_client = choose_caption(url, info)
    if sub_client:
        po_token_first[0] = True   # only that client can see the caption track

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
          + sub_flags(sub_lang, sub_source) + thumb_flags() + extra_flags + ["--", url]
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
        return "gone" if is_permanent(err) or FORMAT_GONE in err.lower() else "fail"

    # 3) Verify the actual downloaded quality with ffprobe.
    vfile = find_video_file(folder)
    height = probe_height(vfile) if vfile else None
    quality = f"{height}p" if height else "Unknown"

    # Transcripts: the json3 caption downloads alongside the video; convert it
    # into trans_<name>.txt (grouped) + words_<name>.txt (word-level).
    trans = words = None
    sub_note = ""
    if DOWNLOAD_SUBS:
        trans, words = build_transcripts(folder, url, info)
        sub_note = (f"  Transcript: {os.path.basename(trans)} + "
                    f"{os.path.basename(words)}" if trans
                    else "  Transcript: none available")
    thumb, desc = build_extras(folder, url, info)
    extras_note = "".join(f"  {label}: {os.path.basename(path)}"
                          for label, path in (("Thumbnail", thumb), ("Description", desc))
                          if path)
    size = folder_size(folder)
    STATS["bytes"] += size
    STATS["seconds"] += elapsed
    rate = f", {human_size(size / elapsed)}/s" if elapsed > 0 else ""
    note(f"    Done. Quality: {quality}  "
         f"[{human_size(size)} in {human_time(elapsed)}{rate}]{sub_note}{extras_note}")
    got = "".join(c for c, path in (("T", trans), ("W", words and "not_found" not in words),
                                    ("j", thumb), ("d", desc)) if path)
    speed = f"{human_size(size / elapsed)}/s" if elapsed > 0 else "-"
    bar.done(f"OK  {quality:>6} {human_size(size):>9} {human_time(elapsed):>9} "
             f"{speed:>10}  [{got}]  {title}")
    write_info(folder, title, url, quality, "OK", transcript=trans, words=words,
               thumbnail=thumb, description=desc, info=info)
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
                      "description": os.path.basename(desc) if desc else None})
    # Register it so a link repeated later in this same run is skipped too.
    if vid:
        DONE_INDEX[vid] = folder
    return "ok"


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
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
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


def wanted_entry(entry):
    """Whether a listed video should be downloaded: (yes, reason-if-not).

    "unknown" is kept apart from "too few views": a video YouTube listed without
    a view count cannot be shown to clear the floor, so it is left out - but
    quietly dropping it would look the same as a video that genuinely fell short.
    """
    entry = entry or {}
    if entry.get("live_status") in ("is_upcoming", "is_live", "post_live"):
        return False, "live"          # nothing finished to download yet
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
    links, seen, name = [], set(), ""
    counts = {"scanned": 0, "views": 0, "unknown": 0, "live": 0, "skipped": 0}

    cmd = base_cmd() + [
        "--print", "%(id)s|%(view_count)s|%(channel)s|%(live_status)s",
        "--dateafter", f"today-{DAYS}days",
        "--break-on-reject",
        "--no-warnings",
        "--", tab
    ]

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
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

        parts = line.split("|")
        if len(parts) < 3:
            continue
        vid, views, ch_name = parts[0], parts[1], parts[2]
        live_status = parts[3] if len(parts) >= 4 else ""

        counts["scanned"] += 1
        name = name or ch_name

        if vid in seen:
            continue

        if live_status in ("is_upcoming", "is_live", "post_live"):
            counts["live"] += 1
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
        links.append(f"https://www.youtube.com/watch?v={vid}")

        if LIMIT and len(links) >= LIMIT:
            break
        
    scan_done()
    note(f"      listed {counts['scanned']} video(s) from last {DAYS} days: kept {len(links)}"
         + (f", {counts['views']} under {MIN_VIEWS} views" if counts["views"] else "")
         + (f", {counts['skipped']} in skip.txt" if counts["skipped"] else "")
         + (f", {counts['live']} live/upcoming" if counts["live"] else ""))
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
    links, seen, name = [], set(), ""
    counts = {"scanned": 0, "views": 0, "unknown": 0, "live": 0, "skipped": 0}
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
            keep, reason = wanted_entry(entry)
            if not keep:
                counts[reason] += 1
                continue
            # skip.txt is applied here, before --limit, so a video you asked to
            # leave alone does not use up one of the N slots.
            if vid in SKIP_IDS:
                counts["skipped"] += 1
                continue
            seen.add(vid)
            links.append(f"https://www.youtube.com/watch?v={vid}")
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
         + (f", {counts['live']} live/upcoming" if counts["live"] else ""))
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
    """
    skipped_by_list[0] = 0            # one expansion, one count
    channels = [u for u in links if is_channel_url(u)]
    if channels:
        criteria = ", ".join(filter(None, [
            f">= {MIN_VIEWS:,} views" if MIN_VIEWS else "every video",
            f"newest {LIMIT} each" if LIMIT else "",
        ]))
        print(f"\n  Reading {len(channels)} channel link(s) ({criteria})...")
    elif MIN_VIEWS or LIMIT:
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

    for url in links:
        if not is_channel_url(url):
            add(url, None)
            continue
        found, name, err = list_channel_videos(url)
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


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="YouTube batch downloader (yt-dlp) - 1080p priority, 720p floor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("directory",
                   help="Folder containing cookies.txt and links.txt (videos download here too).")
    p.add_argument("--channel", action="store_true",
                   help="Group videos by channel: <dir>/<Channel Name>/<Video Title>/")
    p.add_argument("--min-height", type=int, default=720, help="Quality floor (default 720).")
    p.add_argument("--max-height", type=int, default=1080, help="Quality ceiling (default 1080).")
    p.add_argument("--views", "--view", "--min-views", type=int, default=0,
                   dest="views", metavar="N",
                   help="Channel links: only videos with at least N views.")
    p.add_argument("--limit", type=int, default=0, metavar="N",
                   help="Channel links: stop after the newest N matching "
                        "videos (default: the whole channel).")
    p.add_argument("--days", type=int, default=0, metavar="N",
                   help="Only download videos uploaded in the last N days (0 = any time).")
    p.add_argument("--cookies", metavar="FILE",
                   help="Use this cookies.txt instead of <directory>/cookies.txt.")
    p.add_argument("--links", metavar="FILE",
                   help="Read the links from FILE instead of <directory>/links.txt.")
    p.add_argument("--no-subs", action="store_true",
                   help="Do NOT build transcripts (transcripts are built by default).")
    p.add_argument("--sub-lang", default="en",
                   help="Transcript language code (default en).")
    p.add_argument("--no-thumbnail", action="store_true",
                   help="Do NOT save the thumbnail (saved by default).")
    p.add_argument("--no-description", action="store_true",
                   help="Do NOT save the description (saved by default).")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Show yt-dlp's full output instead of the progress bar.")
    return p.parse_args(argv)


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
    global MIN_VIEWS, LIMIT, DAYS, SKIP_FILE, SKIP_IDS

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
    SAVE_THUMBNAIL = not args.no_thumbnail
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
    if MIN_VIEWS or LIMIT or DAYS:
        print(f"  Channels: {f'>= {MIN_VIEWS:,} views' if MIN_VIEWS else 'every video'}"
              f"{f', newest {LIMIT} each' if LIMIT else ''}"
              f"{f', last {DAYS} days' if DAYS else ''}")
    print(f"  Skip    : {f'skip.txt ({len(SKIP_IDS)} video(s))' if SKIP_IDS else 'none'}")
    print(f"  Transcript: {'yes (' + SUB_LANG + ', trans_ + words_ .txt)' if DOWNLOAD_SUBS else 'no'}")
    extras = [n for n, on in (("thumbnail", SAVE_THUMBNAIL),
                              ("description", SAVE_DESCRIPTION)) if on]
    print(f"  Extras  : {', '.join(extras) if extras else 'none'}")
    print(f"  Log     : download_log.txt + download_log.json"
          f"{'  (--verbose: full output on screen)' if VERBOSE else ''}")

    if not os.path.isdir(DOWNLOAD_DIR):
        print(f"\nERROR: directory not found: {DOWNLOAD_DIR}")
        sys.exit(1)

    LOG_FILE[0] = os.path.join(DOWNLOAD_DIR, "download_log.txt")
    LOG_JSON[0] = os.path.join(DOWNLOAD_DIR, "download_log.json")
    LOG_JSONL[0] = os.path.join(DOWNLOAD_DIR, "download_log.jsonl")
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

    links = read_links(LINKS_FILE)
    if not links:
        print(f"No links found in {LINKS_FILE}. Add one link per line.")
        write_empty_summary()
        return

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
    DONE_INDEX = index_downloaded(DOWNLOAD_DIR)
    if DONE_INDEX:
        print(f"  Resuming: {len(DONE_INDEX)} video(s) already downloaded here.")

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
    print(f"  Summary: {counts['ok']} downloaded, {counts['skip']} skipped "
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

    if counts["ok"]:
        rate = (f"  ({human_size(STATS['bytes'] / STATS['seconds'])}/s average)"
                if STATS["seconds"] > 0 else "")
        print(f"  Downloaded {counts['ok']} video(s), {human_size(STATS['bytes'])} "
              f"in {human_time(STATS['seconds'])} of downloading{rate}")
    run_seconds = time.monotonic() - run_started
    print(f"  Total run time: {human_time(run_seconds)}")
    print("=" * 60)

    write_json_log({
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "layout": "channel" if CHANNEL_MODE else "flat",
        "min_views": MIN_VIEWS or None,
        "limit": LIMIT or None,
        "excluded_by_skip_list": excluded,
        "links": total,
        "downloaded": counts["ok"],
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
