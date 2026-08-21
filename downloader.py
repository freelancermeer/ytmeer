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
    --min-height N       Quality floor   (default: 720)
    --max-height N       Quality ceiling (default: 1080)
    --no-subs            Do NOT build transcripts (built by default)
    --sub-lang CODE      Transcript language code (default: en)

Examples:
    python3 downloader.py "/Users/me/Videos/YT"
    python3 downloader.py "/Users/me/Videos/YT" --channel
    python3 downloader.py ~/Downloads/batch --max-height 1080 --min-height 720

LAYOUT
    default     <dir>/<Video Title>/
    --channel   <dir>/<Channel Name>/<Video Title>/

Inside every video folder:
    <Video Title>.mp4    1080p (priority) down to 720p (floor) — never lower
    videoinfo.txt        title, link, quality, status/error, transcript names
    trans_<name>.txt     English transcript, grouped lines  -> [hh:mm:ss] text
    words_<name>.txt     word-level transcript              -> [hh:mm:ss.mmm] word
  (transcript formatting adapted from ../Transcript with timestamps/transcribe.py)

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
import json
import os
import platform
import re
import shutil
import subprocess
import sys

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

IS_WINDOWS = platform.system() == "Windows"

# aria2c downloads one file over many parallel connections — the same trick IDM
# uses. YouTube throttles each single connection well below the line rate, so
# this is the difference between a few hundred KB/s and saturating the link.
ARIA2C = shutil.which("aria2c")
ARIA2C_ARGS = ("-x16 -s16 -k1M -c --file-allocation=none "
               "--console-log-level=warn --summary-interval=0")


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


def sanitize(name: str) -> str:
    """Make a string safe to use as a folder/file name on macOS *and* Windows."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)   # forbidden chars
    name = re.sub(r"[\x00-\x1f]", "", name)       # control chars
    name = re.sub(r"\s+", " ", name).strip()      # collapse whitespace
    name = name.rstrip(". ")                       # no trailing dots/spaces
    name = name[:NAME_LIMIT].rstrip(". ")          # keep the path within limits
    # Windows matches a reserved device by the part before the first dot, so the
    # underscore has to go on the stem: "aux.txt_" is still AUX, "aux_.txt" is not.
    stem, dot, rest = name.partition(".")
    if stem.upper() in WINDOWS_RESERVED:
        name = f"{stem}_{dot}{rest}"
    return name


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


# Holds the exit code of the most recent run_streaming() call.
last_returncode = [0]


def run_streaming(cmd):
    """Run a command, streaming its output live to the terminal (so yt-dlp's
    native progress bar — %, size, speed, ETA — updates in place) while also
    capturing all output as text for error reporting. Returns the captured text."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as e:
        last_returncode[0] = 1
        return f"ERROR: could not start yt-dlp: {e}"

    # read1() returns as soon as any bytes are available (so the progress bar
    # stays live) and is safe on Windows too — a low-level os.read() on the raw
    # file descriptor crashes there with "Bad file descriptor" or hangs.
    chunks = []
    while True:
        data = proc.stdout.read1(4096)
        if not data:
            break
        sys.stdout.buffer.write(data)  # write raw bytes -> preserves \r progress bar
        sys.stdout.buffer.flush()
        chunks.append(data)
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
    if os.path.exists(COOKIES_FILE):
        cmd += ["--cookies", COOKIES_FILE]
    return cmd


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
    """Parse a YouTube json3 caption file into [(start_sec, None, word_text), ...]."""
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
        for s in segs:
            # Flatten any newline/tab inside a segment to a single space: CC-style
            # tracks pack whole lines (with line breaks) into one segment, which
            # would otherwise spill across output lines with no timestamp. A
            # single leading space is preserved — word segments rely on it.
            text = re.sub(r"\s+", " ", s.get("utf8", "") or "")
            if not text.strip():               # blanks / newline-only separators
                continue
            start = (base + (s.get("tOffsetMs") or 0)) / 1000.0
            words.append((start, None, text))
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


def write_words_txt(out_txt, words):
    """Oneword style: one word per line with [hh:mm:ss.mmm] timestamps."""
    with open(out_txt, "w", encoding="utf-8") as f:
        for start, _end, text in words:
            word = one_line(text)
            if word:
                f.write(f"{format_timestamp_ms(start)} {word}\n")


# ============================ transcript helpers ===========================
def resolve_sub_lang(info):
    """Pick the one caption track to fetch, from the metadata we already have.

    Videos do not all publish English as plain "en" — it can be "en-orig", a
    regional "en-US", or a closed-caption code like "en-uYU-mmqFLq8". Matching
    with a wildcard would drag in every machine-translated "English from French"
    track too: dozens of downloads, and YouTube answers with HTTP 429. So the
    available tracks are read from the info JSON (no extra request) and exactly
    one code is chosen. Returns None when the video has no such track.
    """
    tracks = {}
    for key in ("automatic_captions", "subtitles"):
        for code, entries in ((info or {}).get(key) or {}).items():
            tracks.setdefault(code, entries)

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


def sub_flags(lang):
    """yt-dlp flags to fetch exactly one caption track as json3 (word-level).

    Auto-captions are preferred (resolve_sub_lang looks there first), but some
    videos publish their English track only as a regular caption, so both kinds
    are enabled. That is safe here because a single explicit code is requested
    rather than a wildcard.
    """
    if not DOWNLOAD_SUBS or not lang:
        return []
    return ["--write-auto-subs", "--write-subs",
            "--sub-langs", lang, "--sub-format", "json3"]


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


def make_transcripts(folder):
    """Convert a downloaded json3 caption into trans_<name>.txt + words_<name>.txt,
    then delete the json3. Returns (trans_path, words_path) or (None, None)."""
    found = find_json3(folder)
    best = pick_best_json3(found)
    if not best:
        return None, None
    words = parse_json3_words(best)
    for j in found:            # the caption files were only a means to an end
        try:
            os.remove(j)
        except OSError:
            pass
    if not words:
        return None, None
    base = os.path.basename(folder.rstrip("/\\"))   # strip both separators (Mac + Windows)
    trans_path = os.path.join(folder, f"trans_{base}.txt")
    words_path = os.path.join(folder, f"words_{base}.txt")
    write_trans_txt(trans_path, words)
    write_words_txt(words_path, words)
    return trans_path, words_path


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


def download_subs(folder, url):
    """Backfill: fetch the caption for an already-downloaded video and build the
    two transcript files. Returns (trans_path, words_path)."""
    if not DOWNLOAD_SUBS:
        return None, None
    existing = find_transcript_file(folder)
    if existing:
        words = os.path.join(folder, os.path.basename(existing).replace("trans_", "words_", 1))
        return existing, (words if os.path.exists(words) else None)
    info, _ = fetch_info(url)          # tells us which caption track to ask for
    flags = sub_flags(resolve_sub_lang(info))
    if not flags:
        return None, None
    name = os.path.basename(folder.rstrip("/\\"))
    cmd = base_cmd() + ["--skip-download"] + flags + [
        "--no-warnings", "-o", output_template(folder, name), url,
    ]
    subprocess.run(cmd, capture_output=True, text=True)
    return make_transcripts(folder)


def fetch_info(url: str):
    """Fetch video metadata (title, id) without downloading. Returns (info, error)."""
    cmd = base_cmd() + ["--skip-download", "--dump-single-json", "--no-warnings", url]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "Timed out while fetching video info"
    if out.returncode != 0:
        msg = out.stderr.strip().splitlines()
        return None, (msg[-1] if msg else "Failed to fetch video info")
    try:
        return json.loads(out.stdout), None
    except json.JSONDecodeError:
        return None, "Could not parse video info (invalid JSON from yt-dlp)"


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
    return os.path.join(folder, candidates[0]) if candidates else None


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
               transcript=None, words=None):
    """Write videoinfo.txt inside the video's folder."""
    lines = [
        f"Title:   {title}",
        f"Link:    {url}",
        f"Quality: {quality}",
        f"Status:  {status}",
    ]
    if DOWNLOAD_SUBS:
        lines.append(f"Transcript: {os.path.basename(transcript) if transcript else 'none'}")
        lines.append(f"Words:      {os.path.basename(words) if words else 'none'}")
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


def skip_finished(folder, url):
    """Report an already-finished video and backfill its transcripts if missing."""
    print(f"    Already downloaded -> {rel(folder)}/  (skipping)")
    if DOWNLOAD_SUBS and not find_transcript_file(folder):
        trans, words = download_subs(folder, url)
        update_info_transcripts(folder, trans, words)
        if trans:
            print(f"      + {os.path.basename(trans)} + "
                  f"{os.path.basename(words) if words else 'words_*.txt'}")
        else:
            print("      (no transcript available)")


def rel(path):
    """Path shown relative to the download dir (so channel folders are visible)."""
    try:
        return os.path.relpath(path, DOWNLOAD_DIR)
    except ValueError:
        return path


def download_one(url, index, total):
    """Download a single video into its own folder.
    Returns one of: "ok", "skip", "fail"."""
    print(f"\n[{index}/{total}] {url}")

    # 0) Skip if this video is already finished — matched by video id, so a link
    #    repeated with a different tracking suffix is recognised. No network call.
    vid = video_id_from_url(url)
    if vid and vid in DONE_INDEX:
        skip_finished(DONE_INDEX[vid], url)
        return "skip"

    # 1) Get the metadata first: it names the folder (title) and, in --channel
    #    mode, the channel folder it goes under.
    info, err = fetch_info(url)
    title = (info or {}).get("title") or "Unknown Title"
    vid   = (info or {}).get("id") or vid or ""
    safe_title = sanitize(title)

    # The id was unknown until now (unusual URL form) — re-check the index.
    if vid and vid in DONE_INDEX:
        skip_finished(DONE_INDEX[vid], url)
        return "skip"

    # Folder = video title, under the channel folder when --channel is on.
    parent = channel_dir(info)
    folder_name = safe_title or (f"video_{vid}" if vid else "video")
    folder = choose_folder(parent, folder_name, vid)
    os.makedirs(folder, exist_ok=True)

    # If we couldn't even fetch info, record the error and stop here.
    if info is None:
        err = (err or "Failed to fetch video info").replace("ERROR:", "").strip()
        print(f"    ! Failed to fetch info: {err}")
        write_info(folder, title, url, "FAILED", "ERROR", err)
        return "fail"

    # 2) Download best 720p-1080p video+audio (merged mp4) + English json3 caption.
    out_template = output_template(folder, folder_name)

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
            url,
        ] + (speed_flags() if fast else ["--concurrent-fragments", "8"]) \
          + sub_flags(resolve_sub_lang(info)) + extra_flags
        text = run_streaming(cmd)
        if last_returncode[0] == 0:
            return None
        lines = re.split(r"[\r\n]+", text or "")
        err = next((l for l in reversed(lines) if "ERROR" in l),
                   next((l for l in reversed(lines) if l.strip()), "Download failed"))
        return err.replace("ERROR:", "").strip()

    print(f"    Downloading (>= {MIN_HEIGHT}p, <= {MAX_HEIGHT}p) -> {rel(folder)}/\n")
    err = attempt(PO_TOKEN_FLAGS if po_token_first[0] else [])

    # Some videos are gated behind a PO token and 403 on the normal path (aria2c
    # reports that same 403 as exit code 22). Retry once through the mweb client,
    # which mints a token — the fast multi-connection download still applies.
    if err and needs_po_token(err) and not po_token_first[0]:
        print(f"\n    ! {err}\n    Retrying with the PO-token client (mweb)...\n")
        err = attempt(PO_TOKEN_FLAGS)
        if not err:
            po_token_first[0] = True   # the rest of this batch will need it too

    # A token-bound URL occasionally rejects parallel range requests; fall back
    # to a single connection rather than failing the video outright.
    if err and ARIA2C and needs_po_token(err):
        print(f"\n    ! {err}\n    Retrying once more on a single connection...\n")
        err = attempt(PO_TOKEN_FLAGS, fast=False)

    # A stalled or dropped stream is not the video's fault — try once more.
    if err and is_transient(err):
        print(f"\n    ! {err}\n    Stream problem, retrying...\n")
        err = attempt(PO_TOKEN_FLAGS if po_token_first[0] else [])

    if err:
        print(f"\n    ! Download failed: {err}")
        leftovers = mark_incomplete(folder)
        for name in leftovers:
            print(f"      partial file kept as {name}")
        write_info(folder, title, url, "FAILED", "ERROR", err)
        return "fail"

    # 3) Verify the actual downloaded quality with ffprobe.
    vfile = find_video_file(folder)
    height = probe_height(vfile) if vfile else None
    quality = f"{height}p" if height else "Unknown"

    # Transcripts: the json3 caption downloads alongside the video; convert it
    # into trans_<name>.txt (grouped) + words_<name>.txt (word-level).
    trans = words = None
    sub_note = ""
    if DOWNLOAD_SUBS:
        trans, words = make_transcripts(folder)
        sub_note = (f"  Transcript: {os.path.basename(trans)} + "
                    f"{os.path.basename(words)}" if trans
                    else "  Transcript: none available")
    print(f"\n    Done. Quality: {quality}{sub_note}")
    write_info(folder, title, url, quality, "OK", transcript=trans, words=words)
    # Register it so a link repeated later in this same run is skipped too.
    if vid:
        DONE_INDEX[vid] = folder
    return "ok"


def parse_args():
    p = argparse.ArgumentParser(
        description="YouTube batch downloader (yt-dlp) — 1080p priority, 720p floor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("directory",
                   help="Folder containing cookies.txt and links.txt (videos download here too).")
    p.add_argument("--channel", action="store_true",
                   help="Group videos by channel: <dir>/<Channel Name>/<Video Title>/")
    p.add_argument("--min-height", type=int, default=720, help="Quality floor (default 720).")
    p.add_argument("--max-height", type=int, default=1080, help="Quality ceiling (default 1080).")
    p.add_argument("--no-subs", action="store_true",
                   help="Do NOT build transcripts (transcripts are built by default).")
    p.add_argument("--sub-lang", default="en",
                   help="Transcript language code (default en).")
    return p.parse_args()


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


def main():
    global LINKS_FILE, COOKIES_FILE, DOWNLOAD_DIR, MAX_HEIGHT, MIN_HEIGHT, FORMAT
    global DOWNLOAD_SUBS, SUB_LANG, CHANNEL_MODE, DONE_INDEX

    use_utf8_output()
    args = parse_args()
    DOWNLOAD_DIR  = os.path.expanduser(args.directory)
    COOKIES_FILE  = os.path.join(DOWNLOAD_DIR, "cookies.txt")
    LINKS_FILE    = os.path.join(DOWNLOAD_DIR, "links.txt")
    MIN_HEIGHT    = args.min_height
    MAX_HEIGHT    = args.max_height
    DOWNLOAD_SUBS = not args.no_subs
    SUB_LANG      = args.sub_lang
    CHANNEL_MODE  = args.channel
    FORMAT        = build_format()

    print("=" * 60)
    print(f"  YouTube Downloader (yt-dlp) — {MAX_HEIGHT}p priority / {MIN_HEIGHT}p floor")
    print("=" * 60)
    print(f"  Folder  : {DOWNLOAD_DIR}")
    print(f"  Cookies : {COOKIES_FILE if os.path.exists(COOKIES_FILE) else 'none (optional)'}")
    print(f"  Links   : {LINKS_FILE}")
    print(f"  Speed   : {'aria2c, 16 connections' if ARIA2C else 'built-in (install aria2c for a big speedup)'}")
    print(f"  PO token: {'bgutil script (headless)' if BGUTIL_SCRIPT else 'none found — some videos may fail with 403'}")
    print(f"  Layout  : {'<Channel Name>/<Video Title>/' if CHANNEL_MODE else '<Video Title>/'}")
    print(f"  Transcript: {'yes (' + SUB_LANG + ', trans_ + words_ .txt)' if DOWNLOAD_SUBS else 'no'}")

    if not os.path.isdir(DOWNLOAD_DIR):
        print(f"\nERROR: directory not found: {DOWNLOAD_DIR}")
        sys.exit(1)

    if not os.path.exists(COOKIES_FILE):
        print("\nNOTE: no cookies.txt in the folder — that is fine for public "
              "videos.\n      Add one only for private / members-only / "
              "age-restricted videos.")

    links = read_links(LINKS_FILE)
    if not links:
        print(f"No links found in {LINKS_FILE}. Add one link per line.")
        return

    # Resume: one scan of what is already finished (either layout), keyed by
    # video id. Everything after this is a dictionary lookup.
    DONE_INDEX = index_downloaded(DOWNLOAD_DIR)
    if DONE_INDEX:
        print(f"  Resuming: {len(DONE_INDEX)} video(s) already downloaded here.")

    total = len(links)
    ok = skipped = 0
    failed = []
    for i, url in enumerate(links, 1):
        result = download_one(url, i, total)
        if result == "ok":
            ok += 1
        elif result == "skip":
            skipped += 1
        else:
            failed.append(url)

    print("\n" + "=" * 60)
    print(f"  Summary: {ok} downloaded, {skipped} skipped (already done), "
          f"{len(failed)} failed  (of {total} links).")
    if failed:
        print("  Failed links (see each folder's videoinfo.txt for details):")
        for u in failed:
            print(f"    - {u}")
    print("=" * 60)


if __name__ == "__main__":
    main()
