#!/usr/bin/env python3
"""REST API over downloader.py - the same routes locally and on Colab.

Run it on its own:

    python3 api.py                      # http://127.0.0.1:8000, prints a key
    python3 api.py --port 9000 --key mysecret
    python3 api.py --no-key             # no authentication (localhost only!)

or let colab_app.py attach these same routes to its Gradio app, so the UI and the
API share one URL - and one Colab share link.

A download is a JOB, not a request. A bulk run outlasts any sensible HTTP
timeout, so POST /api/download returns an id straight away and the work carries
on in a thread; GET /api/jobs/<id> has the log and, when it is done, the result.

    GET    /api/health
    GET    /api/preview?channel=...&views=1000&limit=3
    POST   /api/preview
    POST   /api/download                 -> {"job_id": "job_a1b2c3d4", ...}
    GET    /api/jobs                     list
    GET    /api/jobs/<id>?tail=50        state, log tail, summary, failures
    GET    /api/jobs/<id>/zip            the finished folders as one file
    POST   /api/jobs/<id>/stop           Ctrl+C it; finished videos are kept
    DELETE /api/jobs/<id>                forget a finished job

Every /api route wants the key, as an "X-API-Key" header or a "?key=" parameter.

fastapi is imported only when routes are actually built, so everything above the
route factory is plain stdlib and can be imported - and tested - without it.
"""

import collections
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADER = os.path.join(HERE, "downloader.py")
ON_COLAB = os.path.isdir("/content")

# The notebook sets YTDL_OUTDIR when Drive is mounted, so the default follows
# wherever the user chose to keep things.
DEFAULT_OUT = (os.environ.get("YTDL_OUTDIR")
               or ("/content/downloads" if ON_COLAB else os.path.join(HERE, "downloads")))

# A bulk run's log is unbounded, so only the tail is kept in memory. The full
# text is in download_log.txt in the output folder either way.
MAX_LOG_LINES = 2000

API_KEY = [None]                       # set by configure_key()
JOBS = {}                              # job id -> job dict
LOCK = threading.RLock()


# ------------------------------------------------------------------ the key
def configure_key(key=None, enabled=True):
    """Set the API key. Returns it, or None when authentication is off."""
    if not enabled:
        API_KEY[0] = None
        return None
    API_KEY[0] = key or os.environ.get("YTDL_API_KEY") or secrets.token_urlsafe(24)
    return API_KEY[0]


def key_ok(supplied):
    """Whether a request may proceed. No key configured means no check."""
    if not API_KEY[0]:
        return True
    # compare_digest keeps a wrong key from being narrowed down by timing.
    return bool(supplied) and secrets.compare_digest(str(supplied), API_KEY[0])


# --------------------------------------------------------- request handling
DEFAULTS = {
    "skip": "", "outdir": None, "channel": True, "views": 0, "limit": 0,
    "min_height": 720, "max_height": 1080, "subs": True, "sub_lang": "en",
    "thumbnail": True, "description": True, "verbose": False, "zip": False,
}
INT_FIELDS = ("views", "limit", "min_height", "max_height")


def normalize_request(payload):
    """Check a /api/download body and fill in what it left out.

    Unknown keys are rejected rather than ignored: "view" for "views" would
    otherwise look like it worked and quietly download the whole channel.
    """
    payload = dict(payload or {})
    links = payload.pop("links", None)
    if isinstance(links, (list, tuple)):
        links = "\n".join(str(l) for l in links)
    links = (links or "").strip()
    if not links:
        raise ValueError("links is required: one link per line, or a list. "
                         "Video links, channel links, or both.")

    unknown = sorted(set(payload) - set(DEFAULTS))
    if unknown:
        raise ValueError(f"unknown field(s): {', '.join(unknown)}. "
                         f"Allowed: links, {', '.join(sorted(DEFAULTS))}.")

    opts = dict(DEFAULTS)
    opts.update(payload)
    opts["links"] = links

    for field in INT_FIELDS:
        try:
            opts[field] = int(opts[field] or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a whole number, got {opts[field]!r}")
        if opts[field] < 0:
            raise ValueError(f"{field} cannot be negative")
    # downloader.py builds a format string from these and yt-dlp would simply
    # find nothing in an inverted range, which reads as "no formats available".
    if opts["min_height"] and opts["max_height"] and opts["min_height"] > opts["max_height"]:
        raise ValueError(f"min_height ({opts['min_height']}) is above "
                         f"max_height ({opts['max_height']})")
    for field in ("channel", "subs", "thumbnail", "description", "verbose", "zip"):
        opts[field] = bool(opts[field])
    opts["skip"] = str(opts["skip"] or "")
    opts["sub_lang"] = str(opts["sub_lang"] or "en")
    opts["outdir"] = str(opts["outdir"] or DEFAULT_OUT)
    return opts


def build_argv(outdir, opts):
    """The command line a person would have typed for these options."""
    argv = [sys.executable, "-u", DOWNLOADER, outdir]
    if opts["channel"]:
        argv.append("--channel")
    if opts["views"] > 0:
        argv += ["--views", str(opts["views"])]
    if opts["limit"] > 0:
        argv += ["--limit", str(opts["limit"])]
    argv += ["--min-height", str(opts["min_height"]),
             "--max-height", str(opts["max_height"])]
    if opts["subs"]:
        argv += ["--sub-lang", opts["sub_lang"]]
    else:
        argv.append("--no-subs")
    if not opts["thumbnail"]:
        argv.append("--no-thumbnail")
    if not opts["description"]:
        argv.append("--no-description")
    if opts["verbose"]:
        argv.append("--verbose")
    return argv


def write_inputs(outdir, links, skips, cookies=None):
    """Lay out the folder downloader.py reads: links.txt, skip.txt, cookies.txt."""
    os.makedirs(outdir, exist_ok=True)

    def put(name, text):
        path = os.path.join(outdir, name)
        text = (text or "").strip()
        if text:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
        elif os.path.exists(path):
            os.remove(path)          # no stale list from a previous run

    put("links.txt", links)
    put("skip.txt", skips)
    # base_cmd() looks for exactly this name and nothing else.
    target = os.path.join(outdir, "cookies.txt")
    if cookies:
        shutil.copyfile(cookies, target)
    elif os.path.exists(target):
        os.remove(target)


# ------------------------------------------------------------------- output
FAILED_STATUSES = ("failed", "unavailable", "channel_failed")


def failures_in(logged):
    """The entries in a parsed download_log.json that did not come down.

    "ok" and "skipped" are deliberately absent: a video already on disk is not a
    failure.
    """
    return [{"url": r.get("url"), "status": r.get("status"),
             "title": r.get("title"), "error": r.get("error")}
            for r in ((logged or {}).get("videos") or [])
            if r.get("status") in FAILED_STATUSES]


def zip_results(outdir):
    """Zip the video folders. Returns the path, or None if there are none."""
    try:
        folders = [n for n in sorted(os.listdir(outdir))
                   if os.path.isdir(os.path.join(outdir, n))]
    except OSError:
        return None
    if not folders:
        return None
    path = os.path.join(outdir, "downloads.zip")
    tmp = path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in folders:
            for root, _dirs, files in os.walk(os.path.join(outdir, folder)):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, outdir))
    os.replace(tmp, path)      # never leave a half-written zip at the real name
    return path


# --------------------------------------------------------------------- jobs
def public(job, tail=50):
    """A job as the API reports it - without the Popen handle."""
    with LOCK:
        log = list(job["log"])
        out = {
            "job_id": job["job_id"],
            "state": job["state"],
            "folder": job["outdir"],
            "command": " ".join(job["argv"][2:]),
            "created": job["created"],
            "finished": job["finished"],
            "returncode": job["returncode"],
            "log_lines": job["lines"],
            "log_truncated": job["lines"] > len(log),
            "summary": job["summary"],
            "failures": job["failures"],
        }
    if tail is not None:
        out["log"] = log[-int(tail):] if int(tail) > 0 else []
    else:
        out["log"] = log
    if job.get("error"):
        out["error"] = job["error"]
    return out


def _collect(job):
    """Read the run's JSON log into the job once the process has exited."""
    path = os.path.join(job["outdir"], "download_log.json")
    if not os.path.exists(path):
        return
    try:
        with open(path, encoding="utf-8") as f:
            logged = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        with LOCK:
            job["error"] = f"could not read download_log.json: {e}"
        return
    with LOCK:
        job["summary"] = logged.get("run")
        job["failures"] = failures_in(logged)


def _pump(job):
    """Run the downloader, collecting its output. Runs in the job's thread."""
    try:
        proc = subprocess.Popen(job["argv"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                encoding="utf-8", errors="replace")
    except OSError as e:
        with LOCK:
            job["state"], job["error"] = "error", f"could not start: {e}"
            job["finished"] = time.time()
        return
    with LOCK:
        job["proc"], job["state"] = proc, "running"
    try:
        for line in proc.stdout:
            with LOCK:
                job["log"].append(line.rstrip("\n"))
                job["lines"] += 1
        proc.wait()
    finally:
        with LOCK:
            job["returncode"], job["proc"] = proc.returncode, None
    _collect(job)
    if job["opts"]["zip"]:
        try:
            zip_results(job["outdir"])
        except OSError as e:
            with LOCK:
                job["error"] = f"could not zip: {e}"
    with LOCK:
        job["state"] = "stopped" if job["stop_requested"] else "finished"
        job["finished"] = time.time()


def start_job(opts, cookies=None):
    """Write the inputs, start the downloader in a thread, return the job."""
    outdir = os.path.abspath(opts["outdir"])
    with LOCK:
        # Two runs in one folder would overwrite each other's links.txt, so the
        # second would silently download the first one's list.
        clash = next((j for j in JOBS.values()
                      if j["state"] in ("queued", "running")
                      and os.path.abspath(j["outdir"]) == outdir), None)
        if clash:
            raise RuntimeError(f"job {clash['job_id']} is already running in "
                               f"{outdir}; give a different folder or stop it first")
    write_inputs(outdir, opts["links"], opts["skip"], cookies)
    job = {
        "job_id": "job_" + secrets.token_hex(4),
        "state": "queued", "outdir": outdir, "opts": opts,
        "argv": build_argv(outdir, opts),
        "log": collections.deque(maxlen=MAX_LOG_LINES), "lines": 0,
        "created": time.time(), "finished": None, "returncode": None,
        "summary": None, "failures": [], "proc": None,
        "stop_requested": False, "error": None,
    }
    with LOCK:
        JOBS[job["job_id"]] = job
    threading.Thread(target=_pump, args=(job,), daemon=True,
                     name=job["job_id"]).start()
    return job


def stop_job(job):
    """Interrupt a run the way Ctrl+C does, so it still writes its summary."""
    with LOCK:
        proc, job["stop_requested"] = job.get("proc"), True
    if not proc or proc.poll() is not None:
        return False
    proc.send_signal(signal.SIGINT)
    return True


def preview_channel(channel_url, views=0, limit=0, skips=""):
    """What a channel link expands to, without downloading anything."""
    url = (channel_url or "").strip().splitlines()
    url = url[0].strip() if url else ""
    if not url:
        raise ValueError("channel is required")
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import downloader as d
    d.MIN_VIEWS, d.LIMIT = int(views or 0), int(limit or 0)
    d.SKIP_IDS = {v for v in (d.video_id_from_url(l) or
                              (l if d.BARE_ID_RE.match(l) else None)
                              for l in (skips or "").split()) if v}
    if not d.is_channel_url(url):
        return {"url": url, "is_channel": False,
                "error": "not a channel link", "matched": 0, "videos": []}
    found, name, err = d.list_channel_videos(url)
    return {"url": url, "is_channel": True, "channel": name or None,
            "tab_read": d.channel_videos_url(url), "error": err,
            "matched": len(found), "videos": found}


def tools_present():
    """Which external tools are installed, for GET /api/health."""
    return {t: shutil.which(t) for t in
            ("yt-dlp", "ffmpeg", "ffprobe", "aria2c", "node")}


# ------------------------------------------------------------------- routes
def build_router():
    """The /api routes.

    fastapi is imported here, not at the top, so everything above can be used -
    and tested - on a machine that only has the standard library.
    """
    from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query
    from fastapi.responses import FileResponse

    def auth(x_api_key: str = Header(None), key: str = Query(None)):
        if not key_ok(x_api_key or key):
            raise HTTPException(401, "missing or wrong API key - send it as an "
                                     "X-API-Key header or a ?key= parameter")

    def find(job_id):
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, f"no such job: {job_id}")
        return job

    # No prefix here - attach() mounts the whole thing under /api, so setting it
    # on both routers would nest it and give /api/api/...
    open_router = APIRouter()
    router = APIRouter(dependencies=[Depends(auth)])

    # The one route without the key: enough to see the server is alive and
    # whether it wants a key, and nothing else.
    @open_router.get("/ping")
    def ping():
        return {"ok": True, "auth_required": bool(API_KEY[0])}

    @router.get("/health")
    def health():
        with LOCK:
            states = collections.Counter(j["state"] for j in JOBS.values())
        return {"ok": True, "colab": ON_COLAB, "default_folder": DEFAULT_OUT,
                "downloader": DOWNLOADER, "tools": tools_present(),
                "jobs": dict(states)}

    @router.get("/preview")
    def preview_get(channel: str = Query(...), views: int = Query(0),
                    limit: int = Query(0), skip: str = Query("")):
        try:
            return preview_channel(channel, views, limit, skip)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/preview")
    def preview_post(payload: dict = Body(...)):
        try:
            return preview_channel(payload.get("channel") or payload.get("links"),
                                   payload.get("views", 0),
                                   payload.get("limit", 0),
                                   payload.get("skip", ""))
        except ValueError as e:
            raise HTTPException(422, str(e))

    @router.post("/download")
    def download(payload: dict = Body(...)):
        try:
            opts = normalize_request(payload)
        except ValueError as e:
            raise HTTPException(422, str(e))
        try:
            job = start_job(opts)
        except RuntimeError as e:
            raise HTTPException(409, str(e))     # same folder, still running
        except OSError as e:
            raise HTTPException(500, f"could not prepare {opts['outdir']}: {e}")
        return public(job, tail=0)

    @router.get("/jobs")
    def list_jobs():
        with LOCK:
            ids = list(JOBS)
        return {"jobs": [public(JOBS[i], tail=0) for i in ids]}

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str, tail: int = Query(50, ge=0, le=MAX_LOG_LINES)):
        return public(find(job_id), tail=tail)

    @router.get("/jobs/{job_id}/zip")
    def get_zip(job_id: str):
        job = find(job_id)
        if job["state"] in ("queued", "running"):
            raise HTTPException(409, "the job is still running")
        path = os.path.join(job["outdir"], "downloads.zip")
        if not os.path.exists(path):
            path = zip_results(job["outdir"])
        if not path:
            raise HTTPException(404, "nothing was downloaded, so there is no zip")
        return FileResponse(path, filename="downloads.zip",
                            media_type="application/zip")

    @router.post("/jobs/{job_id}/stop")
    def post_stop(job_id: str):
        job = find(job_id)
        stopped = stop_job(job)
        return {"job_id": job_id, "stopping": stopped,
                "state": job["state"],
                "detail": ("Ctrl+C sent; finished videos are kept"
                           if stopped else "the job was not running")}

    @router.delete("/jobs/{job_id}")
    def forget(job_id: str):
        job = find(job_id)
        if job["state"] in ("queued", "running"):
            raise HTTPException(409, "stop the job before forgetting it")
        with LOCK:
            JOBS.pop(job_id, None)
        return {"job_id": job_id, "forgotten": True}

    open_router.include_router(router)
    return open_router


def attach(app, prefix="/api"):
    """Add the API routes to an existing app - Gradio's, for instance."""
    app.include_router(build_router(), prefix=prefix)
    return app


def banner(url):
    """What to print so the API can actually be used."""
    url = (url or "http://127.0.0.1:8000").rstrip("/")
    key = API_KEY[0]
    auth = f' -H "X-API-Key: {key}"' if key else ""
    lines = ["=" * 70, "  REST API", "=" * 70, "",
             f"    base    {url}/api",
             f"    key     {key or '(authentication disabled)'}", ""]
    lines += [
        "  # is it alive",
        f"  curl {url}/api/ping",
        "",
        "  # what would this channel give me? (no downloads)",
        f"  curl{auth} \\",
        f"    '{url}/api/preview?channel=https://www.youtube.com/@SomeChannel"
        f"&views=1000&limit=3'",
        "",
        "  # start a download - returns a job_id straight away",
        f"  curl -X POST{auth} -H 'Content-Type: application/json' \\",
        f"    {url}/api/download -d '{{",
        '      "links": "https://www.youtube.com/@SomeChannel",',
        '      "views": 1000, "limit": 3, "channel": true }\'',
        "",
        "  # follow it",
        f"  curl{auth} {url}/api/jobs/<job_id>",
        f"  curl{auth} -X POST {url}/api/jobs/<job_id>/stop",
        f"  curl{auth} -OJ {url}/api/jobs/<job_id>/zip",
        "",
    ]
    return "\n".join(lines)


def serve(host="127.0.0.1", port=8000, key=None, auth=True):
    """Run the API on its own, with no Gradio and no notebook."""
    import uvicorn
    from fastapi import FastAPI

    configure_key(key, enabled=auth)
    app = FastAPI(title="YouTube Downloader API",
                  description=__doc__.split("\n\n")[0])
    attach(app)
    missing = [t for t, p in tools_present().items() if not p and t != "aria2c"]
    if missing:
        print(f"WARNING: not on PATH: {', '.join(missing)}")
    print(banner(f"http://{host}:{port}"), flush=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="REST API over downloader.py")
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 to accept connections from the network")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--key", help="API key (default: $YTDL_API_KEY, else generated)")
    p.add_argument("--no-key", action="store_true",
                   help="turn authentication off (only sensible on localhost)")
    p.add_argument("--outdir", help=f"default download folder ({DEFAULT_OUT})")
    args = p.parse_args()
    if args.outdir:
        DEFAULT_OUT = os.path.abspath(os.path.expanduser(args.outdir))
    serve(args.host, args.port, args.key, auth=not args.no_key)
