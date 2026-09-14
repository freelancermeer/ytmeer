#!/usr/bin/env python3
"""A local REST API over downloader.py, for other code to drive.

    python3 api.py                          # http://127.0.0.1:8000
    python3 api.py --port 9000 --outdir ~/Videos/YT
    python3 api.py --share                  # also a public link, through Gradio (Colab)

Every endpoint, with a form to try each one, is on one page: /api/docs

    GET    /api/ping                        alive?  (no key)
    GET    /api/health                      tools found, default folder, jobs
    GET    /api/preview?channel=URL         what a channel link would download
    POST   /api/jobs                        start downloading       -> job_id
    GET    /api/jobs                        all jobs
    GET    /api/jobs/{id}                   state, progress, summary, failures
    GET    /api/jobs/{id}/videos?since=N    finished videos, as they finish
    GET    /api/jobs/{id}/files             every file in the job's folder
    GET    /api/jobs/{id}/files/{path}      download one (Range works, so it can resume)
    POST   /api/jobs/{id}/stop              Ctrl+C it; finished videos stay
    DELETE /api/jobs/{id}                   forget a finished job

Send the key in an X-API-Key header. A generated key is saved to .api_key beside
this file, so a restart keeps the same one; --new-key replaces it.

A download is a job, not a request: POST /api/jobs answers straight away and the
work carries on in the background. /videos?since=N hands over each video as soon
as its folder is complete - video, transcript and all - so a caller can start on
the first while the rest are still downloading. Pass back the "next" it returns.
A video that failed can come back later as "ok", because a run retries its
failures once at the end: act on "ok" and "skipped".

Only build_app() and main() import fastapi; the rest is stdlib, so it imports -
and tests - without it.
"""

import collections
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import downloader  # noqa: E402  - stdlib-only, and cheap to import

DOWNLOADER = os.path.join(HERE, "downloader.py")
KEY_FILE = os.path.join(HERE, ".api_key")
VERSION = "2.1"


def _default_out():
    """$YTDL_OUTDIR, else Colab's folder or one beside this file - always absolute."""
    chosen = (os.environ.get("YTDL_OUTDIR")
              or ("/content/downloads" if os.path.isdir("/content")
                  else os.path.join(HERE, "downloads")))
    return os.path.abspath(os.path.expanduser(chosen))


DEFAULT_OUT = _default_out()

MAX_LOG_LINES = 2000                   # a job keeps its recent output; the rest is in
STREAM = "download_log.jsonl"          # download_log.txt. The stream: downloader.log_record()
ACTIVE = ("queued", "running")
DONE = ("ok", "skipped")
FAILED = ("failed", "unavailable", "blocked", "channel_failed")

# Never downloadable, wherever they turn up: a logged-in session and the keys.
PRIVATE_NAMES = {"cookies.txt", ".api_key", ".api_key_new"}
PARTIAL_RE = re.compile(r"\.(part|ytdl|aria2)$|\.part-frag\d*$", re.I)
# yt-dlp writes <name>.f137.mp4 streams and a <name>.temp.mp4 merge before the
# final <name>.mp4 - judged against the video folder's own name, so a video titled
# "My.temp" still has a downloadable "My.temp.mp4".
STREAM_PART_RE = re.compile(r"\.(temp|f\d+)\.", re.I)
PREVIEW_WAIT = 5                       # seconds a preview waits for another one to end
PREVIEW_SECONDS = 45                   # a preview stops scanning then: gradio.live cuts at 60
# A job's own run files - never downloadable, even when a job's folder is itself a
# video folder (the file endpoints only serve what the downloader made for a video).
RUN_FILES = {"download_log.json", "download_log.jsonl", "download_log.txt",
             "skip.txt", "links.txt"}
CLASH_SUFFIX_RE = re.compile(r" \[[A-Za-z0-9_-]{11}\]$")


class PreviewBusy(Exception):
    """Another channel preview still holds the lock."""

API_KEY = [None]
PUBLIC_URL = [None]                    # the Gradio share link, when served with --share
JOBS = {}
LOCK = threading.RLock()               # the job table and every job's fields
PREVIEW_LOCK = threading.Lock()        # downloader's module globals, which /preview sets


def code_version(root=HERE):
    """A short hash of api.py and downloader.py, so a caller can tell that a running
    server is older than the code on disk."""
    digest = hashlib.sha1()
    for name in ("api.py", "downloader.py"):
        with open(os.path.join(root, name), "rb") as f:
            digest.update(f.read())
    return digest.hexdigest()[:12]


CODE_VERSION = code_version()


# ------------------------------------------------------------------ the key
def _read_key_file():
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def _write_key_file(key):
    # Owner-only from the moment it exists: it is a password in all but name.
    fd = os.open(KEY_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass


def configure_key(key=None, enabled=True, persist=True, rotate=False):
    """Set the API key and return it; None when authentication is off.

    From, in order: an explicit key, $YTDL_API_KEY, the saved .api_key, and only
    then a new one - saved, so a restart keeps it. An explicit key is not saved.
    """
    if not enabled:
        API_KEY[0] = None
        return None
    chosen = key or os.environ.get("YTDL_API_KEY")
    if not chosen and persist and not rotate:
        chosen = _read_key_file()
    if not chosen:
        chosen = secrets.token_urlsafe(24)
        if persist:
            _write_key_file(chosen)
    API_KEY[0] = chosen
    return chosen


def key_ok(supplied):
    """Whether a request may proceed. No key configured means no check."""
    if not API_KEY[0]:
        return True
    return bool(supplied) and secrets.compare_digest(str(supplied), API_KEY[0])


# ----------------------------------------------------------------- requests
DEFAULTS = {
    "skip": None, "outdir": None, "channel": True, "views": 0, "limit": 0, "days": 0,
    "min_height": 720, "max_height": 1080, "subs": True, "sub_lang": "en",
    "thumbnail": True, "description": True, "verbose": False,
}
INT_FIELDS = ("views", "limit", "days", "min_height", "max_height")
# Limits to keep integer bounds safe for Pydantic v1 vs v2, and SQLite's ability to
# write it - a limit of 10**20 used to turn GET /api/jobs into a 500 for everyone.
INT_MAX = {"views": 10**12, "limit": 10**6, "days": 10**5, "min_height": 10**4, "max_height": 10**4}
SUB_LANG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,34}$")   # anchored for the schema too
BOOL_FIELDS = ("channel", "subs", "thumbnail", "description", "verbose")


def _lines(value):
    """A string, or a list of strings, as newline-separated text."""
    if isinstance(value, (list, tuple)):
        return "\n".join(str(v) for v in value)
    return str(value or "")


def normalize_request(payload):
    """Check a job request and fill in what it left out. Raises ValueError.

    An unknown key is refused rather than ignored: "view" for "views" would
    otherwise look accepted and quietly fetch the whole channel. "skip" stays
    None when it is left out, which means "keep the folder's skip.txt".
    """
    payload = dict(payload or {})
    # Comment lines are dropped here as the downloader would drop them, so a list
    # that is all comments is refused now rather than "finishing" with nothing.
    links = "\n".join(line.strip() for line in _lines(payload.pop("links", None)).splitlines()
                      if line.strip() and not line.strip().startswith("#"))
    if not links:
        raise ValueError("links is required: video links, channel links or both")
    # yt-dlp would read "--exec ..." as an option, not a link.
    bad = next((line for line in links.splitlines()
                if line.startswith("-") and not downloader.BARE_ID_RE.match(line)), None)
    if bad:
        raise ValueError(f"not a link: {bad[:100]!r}")
    unknown = sorted(set(payload) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}; "
                         f"allowed: links, {', '.join(sorted(DEFAULTS))}")

    opts = dict(DEFAULTS)
    opts.update({k: v for k, v in payload.items() if v is not None})
    opts["links"] = links
    for field in INT_FIELDS:
        try:
            opts[field] = int(opts[field] or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a whole number, got {opts[field]!r}")
        if opts[field] < 0:
            raise ValueError(f"{field} cannot be negative")
        if opts[field] > INT_MAX[field]:
            raise ValueError(f"{field} is too large (at most {INT_MAX[field]})")
    # Unlike views and limit, 0 is not "no limit" for a height: yt-dlp would find
    # no format at all, which reads like a different problem altogether.
    for field in ("min_height", "max_height"):
        if opts[field] < 1:
            raise ValueError(f"{field} must be at least 1 (a height in pixels)")
    if opts["min_height"] > opts["max_height"]:
        raise ValueError(f"min_height ({opts['min_height']}) is above "
                         f"max_height ({opts['max_height']})")
    for field in BOOL_FIELDS:
        opts[field] = bool(opts[field])
    if opts["skip"] is not None:
        opts["skip"] = _lines(opts["skip"])
    opts["sub_lang"] = str(opts["sub_lang"] or "en")
    if not SUB_LANG_RE.fullmatch(opts["sub_lang"]):
        raise ValueError(f"sub_lang must be a language code such as en or pt-BR, "
                         f"got {opts['sub_lang']!r}")
    # A relative folder goes inside the default one - not wherever the server was
    # started, which on Colab is not the Drive folder chosen in cell 1. realpath,
    # so one folder spelled two ways is still one folder to the clash check.
    folder = os.path.expanduser(str(opts["outdir"] or DEFAULT_OUT))
    opts["outdir"] = os.path.realpath(os.path.join(DEFAULT_OUT, folder))
    # Inside the download folder only. A job writes and deletes files in its folder
    # and the file endpoints read from it, so a caller-chosen /etc would hand out
    # the machine's files.
    root = os.path.realpath(DEFAULT_OUT)
    if opts["outdir"] != root and not opts["outdir"].startswith(root + os.sep):
        raise ValueError(f"outdir must be a folder inside {DEFAULT_OUT} - give a name "
                         f"such as \"batch1\"")
    return opts


def build_argv(outdir, opts, links_file=None):
    """The command line a person would have typed for these options."""
    argv = [sys.executable, "-u", DOWNLOADER, outdir]
    if links_file:
        argv += ["--links", links_file]
    # A cookies.txt in the download folder serves every job - also one in a
    # subfolder, which would otherwise look for its own. The downloader reads the
    # job folder's own cookies.txt first when there is one.
    shared = os.path.join(DEFAULT_OUT, "cookies.txt")
    if os.path.isfile(shared) and not os.path.isfile(os.path.join(outdir, "cookies.txt")):
        argv += ["--cookies", shared]
    if opts["channel"]:
        argv.append("--channel")
    if opts["views"] > 0:
        argv += ["--views", str(opts["views"])]
    if opts["limit"] > 0:
        argv += ["--limit", str(opts["limit"])]
    if opts.get("days", 0) > 0:
        argv += ["--days", str(opts["days"])]
    argv += ["--min-height", str(opts["min_height"]),
             "--max-height", str(opts["max_height"])]
    argv += ["--sub-lang", opts["sub_lang"]] if opts["subs"] else ["--no-subs"]
    if not opts["thumbnail"]:
        argv.append("--no-thumbnail")
    if not opts["description"]:
        argv.append("--no-description")
    if opts["verbose"]:
        argv.append("--verbose")
    return argv


def write_inputs(outdir, links, skip=None):
    """Make the folder, write skip.txt only when one was given, then the job's links
    to a file of their own. Returns the links file.

    The folder's links.txt is never touched - it may be a hand-kept list for the
    command line - and neither is cookies.txt. A skip list left out of the request
    leaves the folder's skip.txt alone: it is the list of videos never to fetch.
    The links file comes last, so nothing that fails before it leaves one behind.
    """
    os.makedirs(outdir, exist_ok=True)
    if skip is not None:
        path = os.path.join(outdir, "skip.txt")
        if skip.strip():
            with open(path, "w", encoding="utf-8") as f:
                f.write(skip.strip() + "\n")
        elif os.path.exists(path):
            os.remove(path)
    fd, links_file = tempfile.mkstemp(prefix="ytdl_links_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(links.strip() + "\n")
    except BaseException:
        try:
            os.remove(links_file)
        except OSError:
            pass
        raise
    return links_file

def read_stream(outdir):
    """Every whole record in outdir's stream, oldest first.

    The downloader may be halfway through writing the last line, so only lines
    that end in a newline count - a record is never handed over half-formed.
    """
    try:
        # errors="replace": a read can land in the middle of a multibyte character
        # the downloader is still writing. That damage is confined to the
        # unfinished last line, which is dropped below anyway.
        with open(os.path.join(outdir, STREAM), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    records = []
    for line in text.split("\n")[:-1]:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def video_records(records):
    """The records that are about a video; channel records describe an expansion."""
    return [r for r in records if r.get("status") not in ("channel", "channel_failed")]



def failures_from(records):
    """What finally did not come down: each link's last record, where that is a failure.

    A run retries its failures once at the end, so a first "failed" can be
    followed by an "ok" for the same link - that link did come down.
    """
    last = {}
    for r in records:
        last[r.get("url")] = r
    return [{"url": r.get("url"), "status": r.get("status"),
             "title": r.get("title"), "error": r.get("error")}
            for r in last.values() if r.get("status") in FAILED]


def folder_files(folder):
    """Absolute paths of what a video folder holds, None for anything absent."""
    if not folder or not os.path.isdir(folder):
        return {}
    words = sorted(n for n in os.listdir(folder)
                   if n.startswith("words_") and not n.startswith("words_not_found_")
                   and n.endswith(".txt"))
    info = os.path.join(folder, "videoinfo.txt")
    return {
        "video": downloader.find_video_file(folder),
        "transcript": downloader.find_transcript_file(folder),
        "words": os.path.join(folder, words[0]) if words else None,
        "thumbnail": downloader.find_thumbnail(folder),
        "description": downloader.find_description(folder),
        "info": info if os.path.exists(info) else None,
    }


def _stems(folder_name):
    """The names a video folder's files can start with: the folder's own, and - for a
    "<title> [<id>]" folder made before files took that full name - the bare title."""
    bare = CLASH_SUFFIX_RE.sub("", folder_name)
    return (folder_name, bare) if bare != folder_name else (folder_name,)


def _fold(text):
    """A name as a case- and normalization-insensitive disk (macOS, Windows) sees it."""
    return unicodedata.normalize("NFC", text).casefold()


def _unfinished(name, folder_name):
    # The downloader's own marker, as it writes it: "Incomplete_ ideas" is a title.
    if name.startswith("INCOMPLETE_") or PARTIAL_RE.search(name.lower()):
        return True
    # Folded: "VID/Vid.f137.mp4" opens the same stream part as "Vid/Vid.f137.mp4".
    folded = _fold(name)
    for stem in map(_fold, _stems(folder_name)):
        if folded.startswith(stem + "."):
            return STREAM_PART_RE.match(folded[len(stem):]) is not None
    return False


def job_file(outdir, relpath):
    """The absolute path of a file in a job's folder that may be downloaded.

    Only what the downloader made is served: files that sit in a video folder (one
    holding videoinfo.txt) inside the job's folder. That leaves out cookies.txt, the
    keys, links and logs, and anything else a folder may contain. PermissionError
    for a path that resolves outside ("..", absolute, a symlink out), a private or
    unfinished file, or one outside a video folder; FileNotFoundError when absent.
    """
    if not relpath or "\x00" in relpath:
        raise PermissionError("no file named")
    root = os.path.realpath(outdir)
    target = os.path.realpath(os.path.join(root, relpath))
    if not target.startswith(root + os.sep):
        raise PermissionError("that path is outside the job's folder")
    name, folder = os.path.basename(target), os.path.dirname(target)
    if name.lower() in PRIVATE_NAMES or name.lower() in RUN_FILES:
        raise PermissionError("that file is not served")
    if folder == root or not os.path.isfile(os.path.join(folder, "videoinfo.txt")):
        raise PermissionError("only files inside a video folder are served")
    # A case-insensitive disk opens "clip.MP4" as Clip.mp4 - only the file name as it is
    # on disk is served, so no other spelling gets past the checks on the name. (The
    # folder's spelling only matters to the stream-part check, which folds it.)
    try:
        if name not in os.listdir(folder):
            raise FileNotFoundError(relpath)
    except NotADirectoryError:
        raise FileNotFoundError(relpath)
    # Hidden files (.DS_Store and the like) are not the downloader's - but a video
    # whose title starts with a dot has files that do too, named after its folder.
    if name.startswith(".") and not any(_fold(name).startswith(_fold(stem))
                                        for stem in _stems(os.path.basename(folder))):
        raise PermissionError("that file is not served")
    if _unfinished(name, os.path.basename(folder)):
        raise PermissionError("that file is still being written, or was left incomplete")
    if not os.path.isfile(target):
        raise FileNotFoundError(relpath)
    return target


def download_url(job_id, outdir, path):
    """The API path (after /api) that downloads a file of a job's folder."""
    rel = os.path.relpath(os.path.realpath(path), os.path.realpath(outdir))
    return f"/jobs/{job_id}/files/" + urllib.parse.quote(rel.replace(os.sep, "/"))


def list_files(outdir, job_id):
    """Every file in a job's folder that can be downloaded, with its size and URL."""
    root = os.path.realpath(outdir)
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        if "videoinfo.txt" not in filenames or os.path.realpath(dirpath) == root:
            continue                      # only video folders hold downloadable files
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                job_file(root, rel)
                size = os.path.getsize(full)
            except OSError:               # refused, gone, or a symlink out of the folder
                continue
            found.append({"path": rel.replace(os.sep, "/"), "bytes": size,
                          "url": download_url(job_id, root, full)})
    return found


def describe(record, outdir, job_id=None):
    """A video record as /videos hands it over: an absolute folder, its files once it
    is done, and - for a job - the API paths that download them from anywhere. An
    already-downloaded video takes its title from videoinfo.txt."""
    folder = os.path.join(outdir, record["folder"]) if record.get("folder") else None
    done = record.get("status") in DONE
    title, quality = record.get("title"), record.get("quality")
    # Only a finished video's videoinfo.txt is worth reading: a failed one says
    # "Unknown Title", which would disagree with the job's failures. A skipped video
    # has no title or quality in its record, but videoinfo.txt has both.
    if done and folder and not (title and quality):
        info = downloader.read_info(folder)
        title, quality = title or info.get("Title"), quality or info.get("Quality")
    files = folder_files(folder) if done else {}
    downloads = {}
    for key, path in files.items():
        if job_id and path:
            try:                          # offered only if it would really be served
                job_file(outdir, os.path.relpath(os.path.realpath(path), os.path.realpath(outdir)))
            except OSError:
                continue
            downloads[key] = download_url(job_id, outdir, path)
    return {"url": record.get("url"), "status": record.get("status"), "title": title,
            "quality": quality, "error": record.get("error"),
            "folder": folder, "files": files, "downloads": downloads}


def read_videos(outdir):
    """Every video recorded so far in outdir, described."""
    return [describe(r, outdir) for r in video_records(read_stream(outdir))]


# --------------------------------------------------------------------- jobs
def job_records(job):
    """(state, records) - a finished job's snapshot, or the live stream while it runs.

    The state is checked again after reading: if the job finished meanwhile, a new
    job in the same folder may already have replaced the stream, so the snapshot
    (taken before the state changed) is the right answer.
    """
    with LOCK:
        state, snapshot = job["state"], job["records"]
    if state not in ACTIVE:
        return state, snapshot or []
    records = read_stream(job["outdir"])
    with LOCK:
        if job["state"] not in ACTIVE:
            return job["state"], job["records"] or []
    return state, records


def public(job, tail=20):
    """A job as the API reports it."""
    state, records = job_records(job)
    videos = video_records(records)
    with LOCK:
        log = list(job["log"])
        return {
            "job_id": job["job_id"], "state": state,
            "stop_requested": job["stop_requested"],
            "folder": job["outdir"], "command": " ".join(job["argv"][2:]),
            "created": job["created"], "finished": job["finished"],
            "videos_done": sum(1 for v in videos if v.get("status") in DONE),
            "failures": failures_from(records),
            # YouTube refused this server's IP: a problem with the machine, not the videos.
            "blocked": any(r.get("status") == "blocked" or (r.get("status") == "channel_failed"
                           and downloader.is_bot_gated(r.get("error"))) for r in records),
            "summary": job["summary"], "returncode": job["returncode"],
            "error": job["error"], "log_lines": job["lines"],
            "log": log[-tail:] if tail else [],
        }


def _interrupt(proc):
    """Ctrl+C the downloader and whatever it is running, the way a terminal does.

    Signalling the downloader alone leaves a busy yt-dlp or ffmpeg running, and
    writing into the folder, after the job says it stopped. The downloader runs
    in its own process group (see _pump), so the whole group gets the signal.
    Windows has no groups to signal; there the downloader is terminated.
    """
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGINT)
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _finish(job, error=None):
    """Snapshot what the run produced and take the job out of ACTIVE - always."""
    records, summary = [], None
    try:
        records = read_stream(job["outdir"])
        try:
            with open(os.path.join(job["outdir"], "download_log.json"), encoding="utf-8") as f:
                summary = json.load(f).get("run")
        except FileNotFoundError:
            pass                  # stopped, crashed, or had nothing to do before writing it
    except (OSError, ValueError) as e:
        error = error or f"could not read the run's logs: {e}"
    finally:
        try:
            os.remove(job["links_file"])
        except (OSError, TypeError):
            pass
        with LOCK:
            stopped = job["stop_requested"]
            error = job["error"] or error
            if not stopped and not error and summary is None:
                # Every run that ends normally writes one; this one could not (a
                # folder it cannot write to, say).
                last = [line.strip() for line in job["log"] if line.strip()][-3:]
                error = ("the run ended without writing its summary"
                         + (": " + " | ".join(last) if last else ""))
            # The snapshot lands in the same step as the state change, so a finished
            # job always has its full list, and a new job in this folder (which may
            # truncate the stream) cannot start until then.
            # A stop that lands while Python is still starting can be lost; the run then
            # finishes, and its summary says it was not stopped.
            missed = stopped and summary is not None and summary.get("stopped_early") is False
            job.update(records=records, summary=summary, proc=None, error=error,
                       finished=time.time(),
                       state="stopped" if stopped and not missed else ("error" if error else "finished"))


def _pump(job):
    """Run the downloader and collect its output. Runs in the job's thread."""
    error = None
    with LOCK:
        stopped = job["stop_requested"]
    if stopped:                           # stopped while queued: never started
        _finish(job)
        return
    try:
        proc = subprocess.Popen(job["argv"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                encoding="utf-8", errors="replace",
                                start_new_session=(os.name == "posix"))
    except OSError as e:
        _finish(job, error=f"could not start the downloader: {e}")
        return
    try:
        with LOCK:
            job["proc"], job["state"] = proc, "running"
            stop_now = job["stop_requested"]
        if stop_now:                      # stopped while it was still queued
            _interrupt(proc)
        with proc.stdout:
            for line in proc.stdout:
                with LOCK:
                    job["log"].append(line.rstrip("\n"))
                    job["lines"] += 1
        proc.wait()
        with LOCK:
            job["returncode"] = proc.returncode
            last = [line for line in job["log"] if line.strip()][-3:]
        # A stop that lands while the downloader is still starting up exits non-zero
        # too; that is the stop, not a crash.
        if proc.returncode != 0 and not job["stop_requested"]:
            error = (f"the downloader exited with code {proc.returncode}"
                     + (": " + " | ".join(l.strip() for l in last) if last else ""))
    except Exception as e:                # never leave a job "running" for ever
        error = f"the job broke: {e!r}"
    finally:
        _finish(job, error=error)


def _same_folder(a, b):
    """One folder, however it is spelled: macOS and Windows disks ignore letter case
    (and macOS, Unicode normalization), which a string comparison does not."""
    if a == b:
        return True
    try:
        return os.path.samefile(a, b)
    except OSError:                       # one of them does not exist yet
        return False


def start_job(opts):
    """Write the inputs and start the downloader in a thread. Returns the job."""
    outdir = opts["outdir"]
    with LOCK:
        # Two runs in one folder would mix their streams and logs.
        clash = next((j for j in JOBS.values()
                      if j["state"] in ACTIVE and _same_folder(j["outdir"], outdir)), None)
        if clash:
            raise RuntimeError(f"job {clash['job_id']} is already running in "
                               f"{outdir}; use another folder or stop it first")
        links_file = write_inputs(outdir, opts["links"], opts["skip"])
        job_id = "job_" + secrets.token_hex(4)
        try:
            for name in (STREAM, "download_log.json"):   # a previous run's, not this one's
                try:
                    os.remove(os.path.join(outdir, name))
                except FileNotFoundError:
                    pass
            job = {
                "job_id": job_id, "state": "queued",
                "outdir": outdir, "opts": opts, "links_file": links_file,
                "argv": build_argv(outdir, opts, links_file),
                "log": collections.deque(maxlen=MAX_LOG_LINES), "lines": 0,
                "created": time.time(), "finished": None, "returncode": None,
                "summary": None, "records": None, "proc": None,
                "stop_requested": False, "error": None,
            }
            JOBS[job_id] = job
            threading.Thread(target=_pump, args=(job,), daemon=True, name=job_id).start()
        except BaseException:
            # Nothing half-made may stay: no job blocking the folder, no links file.
            JOBS.pop(job_id, None)
            try:
                os.remove(links_file)
            except OSError:
                pass
            raise
    return job

def stop_job(job):
    """Stop a job, finished videos kept. True if it was queued or running.

    Only a stop that reaches a live run marks the job stopped: one that arrives
    after the downloader has exited must not relabel a finished or crashed run.
    """
    with LOCK:
        proc, state = job["proc"], job["state"]
        if state not in ACTIVE:
            return False
        if proc is None:                  # still queued: _pump stops it as it starts
            job["stop_requested"] = True
            return True
        if proc.poll() is not None:       # already exited: _finish labels it as it ended
            return False
        if job["stop_requested"]:
            # Already told once. A second Ctrl+C would land inside the downloader's
            # own stop handler and cut its summary short.
            return True
        job["stop_requested"] = True
    _interrupt(proc)
    return True


def shutdown_jobs(timeout=15):
    """Stop every running job and wait for them to wind down.

    Each downloader runs in its own process group, so nothing else would stop it
    when the server goes away - it would keep downloading, unseen, into a folder
    the next server thinks is free.
    """
    with LOCK:
        jobs = [j for j in JOBS.values() if j["state"] in ACTIVE]
    for job in jobs:
        stop_job(job)
    deadline = time.time() + timeout
    while time.time() < deadline and any(j["state"] in ACTIVE for j in jobs):
        time.sleep(0.2)

def preview_channel(channel, views=0, limit=20, skip=""):
    """What a channel link expands to, without downloading anything.

    Its own lock, not the jobs' one: a listing can take minutes, and nothing about
    the running jobs should wait for it.
    """
    url = _lines(channel).strip().split("\n")[0].strip()
    if not url:
        raise ValueError("channel is required")
    d = downloader
    if not d.is_channel_url(url):
        raise ValueError(f"not a channel link: {url}")
    # One listing at a time (they share downloader's module globals), but never a
    # queue: a request abandoned by a proxy keeps its listing running, and anything
    # waiting behind it would time out too.
    if not PREVIEW_LOCK.acquire(timeout=PREVIEW_WAIT):
        raise PreviewBusy("another channel preview is still running; try again in a "
                          "minute, or with a smaller limit")
    try:
        d.COOKIES_FILE = os.path.join(DEFAULT_OUT, "cookies.txt")   # as a job would
        d.use_cookies[0] = True           # a replaced cookies.txt gets its chance
        d.MIN_VIEWS, d.LIMIT = int(views or 0), int(limit or 0)
        d.SKIP_IDS = {v for v in (d.video_id_from_url(x) or
                                  (x if d.BARE_ID_RE.match(x) else None)
                                  for x in _lines(skip).split()) if v}
        found, name, err = d.list_channel_videos(url, deadline=time.monotonic() + PREVIEW_SECONDS)
    finally:
        PREVIEW_LOCK.release()
    return {"channel": name or None, "tab_read": d.channel_videos_url(url),
            "matched": len(found), "videos": found, "error": err}


def tools_present():
    return {t: downloader.shutil.which(t) is not None
            for t in ("yt-dlp", "ffmpeg", "ffprobe", "aria2c", "node", "deno")}


# ------------------------------------------------------------------- server
def build_app():
    from fastapi import Depends, FastAPI, HTTPException, Query, Security
    from fastapi.responses import FileResponse
    from fastapi.security import APIKeyHeader
    from pydantic import BaseModel, ConfigDict, Field

    class JobRequest(BaseModel):
        model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{
            "links": ["https://www.youtube.com/@SomeChannel"],
            "views": 1000, "limit": 3}]})
        links: str | list[str] = Field(
            ..., description="Video links, channel links or both - a list, or one per line.")
        skip: str | list[str] | None = Field(
            None, description="Never download these (links or 11-character ids). Replaces the "
                              "folder's skip.txt; leave it out to keep the one already there.")
        outdir: str | None = Field(None, description="Folder to download into: a name "
                                   "inside the server's download folder, such as batch1.")
        channel: bool = Field(True, description="Group into <Channel>/<Video>/ folders.")
        views: int = Field(0, ge=0, le=INT_MAX["views"], description="Channel links only: "
                           "skip videos under this many views. 0 = no floor.")
        limit: int = Field(0, ge=0, le=INT_MAX["limit"], description="Channel links only: "
                           "newest N matches per channel. 0 = the whole channel.")
        days: int = Field(0, ge=0, le=INT_MAX["days"], description="Only download videos "
                          "uploaded in the last N days. 0 = any time.")
        min_height: int = Field(720, ge=1, le=INT_MAX["min_height"],
                                description="Quality floor, in pixels.")
        max_height: int = Field(1080, ge=1, le=INT_MAX["max_height"],
                                description="Quality ceiling, in pixels.")
        subs: bool = Field(True, description="Write the transcript files.")
        sub_lang: str = Field("en", pattern=SUB_LANG_RE.pattern,
                              description="Transcript language code, such as en or pt-BR.")
        thumbnail: bool = Field(True, description="Save the thumbnail.")
        description: bool = Field(True, description="Save the description.")
        verbose: bool = Field(False, description="Put yt-dlp's full output in the job log.")

    header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def auth(key: str | None = Security(header)):
        if not key_ok(key):
            raise HTTPException(401, "missing or wrong API key (X-API-Key header)")

    def unprocessable(message, *loc):
        # FastAPI's own shape for a 422, so a caller handles one kind.
        return HTTPException(422, [{"type": "value_error", "loc": list(loc), "msg": message}])

    def find(job_id):
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, f"no such job: {job_id}")
        return job

    # Docs live under /api too, so they come along wherever these routes are
    # mounted - including onto Gradio's app, which has its own /openapi.json.
    app = FastAPI(title="YouTube Downloader API", version=VERSION,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None,
                  description="Start downloads, then collect each video as it finishes. "
                              "Click **Authorize** and paste your key to try the endpoints.")
    keyed = [Depends(auth)]

    @app.get("/api/ping", tags=["status"])
    def ping():
        """Alive, whether it wants a key, and whether a job is still running. Needs
        no key, and says nothing more than that."""
        with LOCK:
            busy = any(j["state"] in ACTIVE for j in JOBS.values())
        return {"ok": True, "auth_required": bool(API_KEY[0]), "busy": busy}

    @app.get("/api/health", tags=["status"], dependencies=keyed)
    def health():
        """Tools found, the default folder, the code version, and jobs by state."""
        with LOCK:
            states = collections.Counter(j["state"] for j in JOBS.values())
        return {"ok": True, "version": VERSION, "code": CODE_VERSION,
                "public_url": PUBLIC_URL[0],
                "cookies": os.path.isfile(os.path.join(DEFAULT_OUT, "cookies.txt")),
                "default_folder": DEFAULT_OUT, "tools": tools_present(),
                "jobs": dict(states)}

    @app.get("/api/preview", tags=["channels"], dependencies=keyed)
    def preview(channel: str = Query(..., description="A channel link."),
                views: int = Query(0, ge=0, le=INT_MAX["views"]),
                limit: int = Query(20, ge=1, le=500, description="How many matching "
                                   "videos to list: a preview is a look, not the channel."),
                skip: str = Query("", description="Links or ids to leave out.")):
        """The newest videos a channel link would download, up to limit. Nothing is
        downloaded. One preview runs at a time; another gets 503. On the public link a
        request must finish within about 60 s, so keep the limit small there."""
        try:
            return preview_channel(channel, views, limit, skip)
        except ValueError as e:
            raise unprocessable(str(e), "query", "channel")
        except PreviewBusy as e:
            raise HTTPException(503, str(e))

    @app.post("/api/jobs", status_code=202, tags=["jobs"], dependencies=keyed)
    def create_job(body: JobRequest):
        """Start downloading. Answers at once with a job_id; the work goes on."""
        try:
            opts = normalize_request(body.model_dump(exclude_none=True))
        except ValueError as e:
            raise unprocessable(str(e), "body")
        try:
            job = start_job(opts)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        except OSError as e:
            raise HTTPException(500, f"could not prepare {opts['outdir']}: {e}")
        return public(job, tail=0)

    @app.get("/api/jobs", tags=["jobs"], dependencies=keyed)
    def list_jobs():
        """Every job this server has run."""
        with LOCK:
            jobs = list(JOBS.values())
        return [public(j, tail=0) for j in jobs]

    @app.get("/api/jobs/{job_id}", tags=["jobs"], dependencies=keyed)
    def get_job(job_id: str, tail: int = Query(20, ge=0, le=MAX_LOG_LINES)):
        """State (queued, running, finished, stopped, error), how many videos are
        done, what finally failed, the last lines of output, and the run summary."""
        return public(find(job_id), tail=tail)

    @app.get("/api/jobs/{job_id}/videos", tags=["jobs"], dependencies=keyed)
    def get_videos(job_id: str, since: int = Query(0, ge=0)):
        """Videos finished since the last call, each with the absolute paths of its
        files. Pass back "next" as since. When "done" is true, nothing more is coming.
        A failed video can come back later as "ok": act on "ok" and "skipped"."""
        job = find(job_id)
        state, records = job_records(job)
        videos = video_records(records)
        return {"job_id": job_id, "state": state, "done": state not in ACTIVE,
                "next": len(videos),
                "videos": [describe(r, job["outdir"], job_id) for r in videos[since:]]}

    @app.get("/api/jobs/{job_id}/files", tags=["files"], dependencies=keyed)
    def get_files(job_id: str):
        """Every file in the job's folder that can be downloaded: path, size, and the
        URL (after /api) to fetch it. Includes videos from earlier runs in that folder."""
        job = find(job_id)
        return {"job_id": job_id, "folder": job["outdir"],
                "files": list_files(job["outdir"], job_id)}

    @app.api_route("/api/jobs/{job_id}/files/{path:path}", methods=["GET", "HEAD"],
                   tags=["files"], dependencies=keyed)
    def get_file(job_id: str, path: str):
        """Download one file from the job's folder. Range requests work, so a large
        video can be fetched in parts or resumed. Files still being written, hidden
        files and cookies.txt are refused."""
        job = find(job_id)
        try:
            full = job_file(job["outdir"], path)
            stat = os.stat(full)
        except FileNotFoundError:
            raise HTTPException(404, f"no such file: {path}")
        except PermissionError as e:
            raise HTTPException(403, str(e))
        # "identity" keeps Gradio's Brotli middleware off: it compresses .txt on the fly,
        # which turns Content-Length and Range parts into sizes of compressed bytes.
        return FileResponse(full, filename=os.path.basename(full), stat_result=stat,
                            headers={"Content-Encoding": "identity"})

    @app.post("/api/jobs/{job_id}/stop", tags=["jobs"], dependencies=keyed)
    def post_stop(job_id: str):
        """Ctrl+C the job - and the yt-dlp it is running. Finished videos stay."""
        return {"job_id": job_id, "stopping": stop_job(find(job_id))}

    @app.delete("/api/jobs/{job_id}", tags=["jobs"], dependencies=keyed)
    def delete_job(job_id: str):
        """Forget a finished job. Its files stay where they are."""
        if find(job_id)["state"] in ACTIVE:
            raise HTTPException(409, "stop the job before deleting it")
        with LOCK:
            JOBS.pop(job_id, None)
        return {"job_id": job_id, "deleted": True}

    return app


def banner(base, key, public=None):
    """The details another program needs, and nothing else."""
    lines = []
    if public:
        lines += [f"  public {public}/api",
                  f"  docs   {public}/api/docs      (Authorize, then paste the key)"]
    lines += [
        f"  API    {base}/api",
        f"  key    {key or '(none - authentication is off)'}      header: X-API-Key",
    ]
    if not public:
        lines.append(f"  docs   {base}/api/docs")
    lines += [
        "",
        "  POST /api/jobs                       {\"links\": [...], \"views\": 1000, \"limit\": 3}",
        "  GET  /api/jobs/{id}                  progress",
        "  GET  /api/jobs/{id}/videos?since=0   finished videos, as they finish",
        "  GET  /api/jobs/{id}/files/{path}     download a file (each video lists its own)",
    ]
    return "\n".join(lines)


def serve_public(host, port, share=True):
    """Serve the API on Gradio's server, whose share tunnel gives it a public URL.

    Nothing but a one-line page is Gradio's; the routes and their docs are this
    module's, added to Gradio's app once it is up (its own endpoints live under
    /gradio_api, so /api is free). Returns the launched Blocks.
    """
    import gradio as gr

    with gr.Blocks(title="YouTube Downloader API") as demo:
        gr.Markdown("# YouTube Downloader API\n"
                    "Everything is at [/api/docs](/api/docs): click **Authorize**, "
                    "paste the key.")
    demo.launch(share=share, server_name=host, server_port=port, quiet=True,
                prevent_thread_lock=True)
    demo.app.include_router(build_app().router)
    PUBLIC_URL[0] = (getattr(demo, "share_url", None) or "").rstrip("/") or None
    return demo


def main(argv=None):
    import argparse
    global DEFAULT_OUT
    p = argparse.ArgumentParser(description="REST API over downloader.py")
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to accept connections from other machines")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--outdir", help=f"default download folder ({DEFAULT_OUT})")
    p.add_argument("--key", help="use this key (default: $YTDL_API_KEY, then .api_key)")
    p.add_argument("--new-key", action="store_true", help="replace the saved key")
    p.add_argument("--no-key", action="store_true",
                   help="turn authentication off - only sensible on 127.0.0.1")
    p.add_argument("--share", action="store_true",
                   help="also give it a public URL through Gradio's share tunnel (needs gradio)")
    args = p.parse_args(argv)
    if args.outdir:
        DEFAULT_OUT = os.path.abspath(os.path.expanduser(args.outdir))

    key = configure_key(args.key, enabled=not args.no_key, rotate=args.new_key)
    missing = [t for t in ("yt-dlp", "ffmpeg", "ffprobe") if not tools_present()[t]]
    if missing:
        print(f"WARNING: not on PATH: {', '.join(missing)}")

    def _terminate(signum, frame):
        raise SystemExit(0)

    if args.share:
        demo = serve_public(args.host, args.port)
        print(banner(f"http://{args.host}:{args.port}", key, PUBLIC_URL[0]), flush=True)
        if not PUBLIC_URL[0]:
            print("WARNING: Gradio could not open a share link; only the local URL works.")
        signal.signal(signal.SIGTERM, _terminate)
        try:
            demo.block_thread()           # returns on Ctrl+C; SIGTERM raises SystemExit
        finally:
            shutdown_jobs()
            demo.close()
            # close() leaves the share tunnel process running; only Gradio's own
            # Ctrl+C handler kills it, and SIGTERM does not go through that.
            from gradio.tunneling import CURRENT_TUNNELS
            for tunnel in CURRENT_TUNNELS:
                tunnel.kill()
        return

    import uvicorn
    print(banner(f"http://{args.host}:{args.port}", key), flush=True)

    # uvicorn handles SIGTERM itself, then re-raises it once it has shut down; with
    # this as the handler it restores, that becomes SystemExit and the finally runs.
    signal.signal(signal.SIGTERM, _terminate)
    try:
        # timeout_graceful_shutdown: without it uvicorn waits for every open request
        # - a channel preview can take minutes - before the jobs below are stopped.
        uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning",
                    timeout_graceful_shutdown=5)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_jobs()


if __name__ == "__main__":
    main()
