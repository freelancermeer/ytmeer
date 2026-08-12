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
    --min-height N       Quality floor   (default: 720)
    --max-height N       Quality ceiling (default: 1080)
    --no-subs            Do NOT build transcripts (built by default)
    --sub-lang CODE      Transcript language code (default: en)

Examples:
    python3 downloader.py "/Users/me/Videos/YT"
    python3 downloader.py ~/Downloads/batch --max-height 1080 --min-height 720

- Downloads in 1080p (priority) down to 720p (floor) — never below the floor
- Each video -> its own folder named after the video title
- Inside each folder:
    videoinfo.txt        (title, link, quality, status/error)
    trans_<name>.txt     English transcript, grouped lines  -> [hh:mm:ss] text
    words_<name>.txt     word-level transcript              -> [hh:mm:ss.mmm] word
  (transcript formatting adapted from ../Transcript with timestamps/transcribe.py)
"""

import argparse
import collections
import json
import os
import re
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


def sanitize(name: str) -> str:
    """Make a string safe to use as a folder/file name on macOS/Windows."""
    name = re.sub(r'[\\/:*?"<>|]', "_", name)   # forbidden chars
    name = re.sub(r"\s+", " ", name).strip()      # collapse whitespace
    name = name.rstrip(". ")                       # no trailing dots/spaces
    return name[:150] if len(name) > 150 else name  # keep it filesystem-safe


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

    fd = proc.stdout.fileno()
    chunks = []
    while True:
        data = os.read(fd, 4096)      # returns as soon as any bytes are available
        if not data:
            break
        sys.stdout.buffer.write(data)  # write raw bytes -> preserves \r progress bar
        sys.stdout.buffer.flush()
        chunks.append(data)
    proc.wait()
    last_returncode[0] = proc.returncode
    return b"".join(chunks).decode("utf-8", "replace")


def base_cmd():
    """Common yt-dlp arguments (cookies added only if the file exists)."""
    cmd = ["yt-dlp"]
    # Let yt-dlp fetch its EJS challenge-solver script so it can solve YouTube's
    # JS "n" challenge (needs a JS runtime like deno/node installed). Without this
    # some videos fail with "This video is not available" / missing formats.
    cmd += ["--remote-components", "ejs:github"]
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


def group_words_into_lines(words):
    """words: list of (start, end, word_text) -> list of (start, line_text)."""
    lines, current, start = [], [], None
    for w_start, _w_end, text in words:
        if start is None:
            start = w_start
        candidate = ("".join(current) + text).strip()
        if current and len(candidate) > TRANSCRIPT_WRAP:
            lines.append((start, "".join(current).strip()))
            current, start = [text], w_start
        else:
            current.append(text)
    if current:
        lines.append((start, "".join(current).strip()))
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
            text = s.get("utf8", "")
            if not text or not text.strip():   # skip blanks / newline separators
                continue
            start = (base + (s.get("tOffsetMs") or 0)) / 1000.0
            words.append((start, None, text))
    return words


def write_trans_txt(out_txt, words):
    """Default style: grouped, wrapped lines with [hh:mm:ss] timestamps."""
    with open(out_txt, "w", encoding="utf-8") as f:
        for start, text in group_words_into_lines(words):
            if text:
                f.write(f"{format_timestamp(start)} {text}\n")


def write_words_txt(out_txt, words):
    """Oneword style: one word per line with [hh:mm:ss.mmm] timestamps."""
    with open(out_txt, "w", encoding="utf-8") as f:
        for start, _end, text in words:
            word = text.strip()
            if word:
                f.write(f"{format_timestamp_ms(start)} {word}\n")


# ============================ transcript helpers ===========================
def sub_flags():
    """yt-dlp flags to fetch the English AUTO-caption as json3 (word-level).
    Only auto-generated captions are fetched (no manual subs)."""
    if not DOWNLOAD_SUBS:
        return []
    return ["--write-auto-subs", "--sub-langs", SUB_LANG, "--sub-format", "json3"]


def find_json3(folder):
    """Return path to a downloaded .json3 caption in the folder, if any."""
    try:
        for name in os.listdir(folder):
            if name.lower().endswith(".json3"):
                return os.path.join(folder, name)
    except OSError:
        pass
    return None


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
    j = find_json3(folder)
    if not j:
        return None, None
    words = parse_json3_words(j)
    try:
        os.remove(j)
    except OSError:
        pass
    if not words:
        return None, None
    base = os.path.basename(folder.rstrip("/"))
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
    cmd = base_cmd() + ["--skip-download"] + sub_flags() + [
        "--no-warnings", "-o", os.path.join(folder, "%(title)s.%(ext)s"), url,
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
    """Return the path to the downloaded video file in a folder, if any."""
    exts = (".mp4", ".mkv", ".webm", ".mov")
    for name in os.listdir(folder):
        if name.lower().endswith(exts) and name != "videoinfo.txt":
            return os.path.join(folder, name)
    return None


def choose_folder(base, folder_name, vid, url):
    """Pick the output folder for a video.

    - Normally: <base>/<title>  (clean name).
    - If that folder already exists but belongs to a DIFFERENT video (its
      videoinfo.txt references another link/id), use "<title> [<id>]" so two
      different videos with the same title don't clobber each other.
    - Same video re-run -> reuse the existing folder (download overwrites).
    """
    folder = os.path.join(base, folder_name)
    info = os.path.join(folder, "videoinfo.txt")
    if os.path.isdir(folder) and vid and os.path.exists(info):
        try:
            with open(info, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            content = ""
        if url not in content and vid not in content:
            return os.path.join(base, f"{folder_name} [{vid}]")
    return folder


def already_done(base, url):
    """If this url was already downloaded OK (videoinfo Status: OK + video file
    still present), return that folder path so we can skip re-downloading.
    Scans existing folders WITHOUT any network call."""
    try:
        entries = os.listdir(base)
    except OSError:
        return None
    for name in entries:
        folder = os.path.join(base, name)
        info = os.path.join(folder, "videoinfo.txt")
        if not (os.path.isdir(folder) and os.path.exists(info)):
            continue
        try:
            with open(info, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError:
            continue
        link_ok = any(line.strip() == f"Link:    {url}" or
                      line.strip().startswith("Link:") and url in line
                      for line in content.splitlines())
        status_ok = any(line.strip() == "Status:  OK" for line in content.splitlines())
        if link_ok and status_ok and find_video_file(folder):
            return folder
    return None


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


def download_one(url, index, total):
    """Download a single video into its own folder.
    Returns one of: "ok", "skip", "fail"."""
    print(f"\n[{index}/{total}] {url}")

    # 0) Skip if this exact video is already downloaded OK in this folder.
    done = already_done(DOWNLOAD_DIR, url)
    if done:
        print(f"    Already downloaded -> {os.path.basename(done)}/  (skipping)")
        # Backfill the transcripts if the video is there but no trans_*.txt yet.
        if DOWNLOAD_SUBS and not find_transcript_file(done):
            trans, words = download_subs(done, url)
            update_info_transcripts(done, trans, words)
            if trans:
                print(f"      + {os.path.basename(trans)} + "
                      f"{os.path.basename(words) if words else 'words_*.txt'}")
            else:
                print("      (no transcript available)")
        return "skip"

    # 1) Get the title first so we can name the folder correctly.
    info, err = fetch_info(url)
    title = (info or {}).get("title") or "Unknown Title"
    vid   = (info or {}).get("id") or ""
    safe_title = sanitize(title)

    # Folder = video title. Reuse the folder for the SAME video (re-run overwrites);
    # only append the video id when a DIFFERENT video already claimed this exact title.
    folder_name = safe_title or (f"video_{vid}" if vid else "video")
    folder = choose_folder(DOWNLOAD_DIR, folder_name, vid, url)
    os.makedirs(folder, exist_ok=True)

    # If we couldn't even fetch info, record the error and stop here.
    if info is None:
        err = (err or "Failed to fetch video info").replace("ERROR:", "").strip()
        print(f"    ! Failed to fetch info: {err}")
        write_info(folder, title, url, "FAILED", "ERROR", err)
        return "fail"

    # 2) Download best 720p-1080p video+audio (merged mp4) + English json3 caption.
    out_template = os.path.join(folder, "%(title)s.%(ext)s")
    cmd = base_cmd() + [
        "-f", FORMAT,
        "--merge-output-format", "mp4",
        "--no-playlist",
        "--progress",          # show the live download progress bar
        "-o", out_template,
        url,
    ] + sub_flags()
    print(f"    Downloading (>= {MIN_HEIGHT}p, <= {MAX_HEIGHT}p) -> {folder_name}/\n")

    # Run yt-dlp and tee its output: show the native live progress bar (%, size,
    # speed, ETA) in real time AND keep the text so we can extract any error.
    text = run_streaming(cmd)

    if text is None or last_returncode[0] != 0:
        lines = re.split(r"[\r\n]+", text or "")
        err = next((l for l in reversed(lines) if "ERROR" in l),
                   next((l for l in reversed(lines) if l.strip()), "Download failed"))
        err = err.replace("ERROR:", "").strip()
        print(f"\n    ! Download failed: {err}")
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
    return "ok"


def parse_args():
    p = argparse.ArgumentParser(
        description="YouTube batch downloader (yt-dlp) — 1080p priority, 720p floor.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("directory",
                   help="Folder containing cookies.txt and links.txt (videos download here too).")
    p.add_argument("--min-height", type=int, default=720, help="Quality floor (default 720).")
    p.add_argument("--max-height", type=int, default=1080, help="Quality ceiling (default 1080).")
    p.add_argument("--no-subs", action="store_true",
                   help="Do NOT build transcripts (transcripts are built by default).")
    p.add_argument("--sub-lang", default="en",
                   help="Transcript language code (default en).")
    return p.parse_args()


def main():
    global LINKS_FILE, COOKIES_FILE, DOWNLOAD_DIR, MAX_HEIGHT, MIN_HEIGHT, FORMAT
    global DOWNLOAD_SUBS, SUB_LANG

    args = parse_args()
    DOWNLOAD_DIR  = os.path.expanduser(args.directory)
    COOKIES_FILE  = os.path.join(DOWNLOAD_DIR, "cookies.txt")
    LINKS_FILE    = os.path.join(DOWNLOAD_DIR, "links.txt")
    MIN_HEIGHT    = args.min_height
    MAX_HEIGHT    = args.max_height
    DOWNLOAD_SUBS = not args.no_subs
    SUB_LANG      = args.sub_lang
    FORMAT        = build_format()

    print("=" * 60)
    print(f"  YouTube Downloader (yt-dlp) — {MAX_HEIGHT}p priority / {MIN_HEIGHT}p floor")
    print("=" * 60)
    print(f"  Folder  : {DOWNLOAD_DIR}")
    print(f"  Cookies : {COOKIES_FILE}")
    print(f"  Links   : {LINKS_FILE}")
    print(f"  Transcript: {'yes (' + SUB_LANG + ', trans_ + words_ .txt)' if DOWNLOAD_SUBS else 'no'}")

    if not os.path.isdir(DOWNLOAD_DIR):
        print(f"\nERROR: directory not found: {DOWNLOAD_DIR}")
        sys.exit(1)

    if not os.path.exists(COOKIES_FILE):
        print(f"\nNOTE: cookies.txt not found in the folder — "
              "running without cookies (some videos may fail).")

    links = read_links(LINKS_FILE)
    if not links:
        print(f"No links found in {LINKS_FILE}. Add one link per line.")
        return

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
