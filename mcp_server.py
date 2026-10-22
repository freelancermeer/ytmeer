#!/usr/bin/env python3
"""
YTmeer - the YouTube research toolkit as MCP tools.

An MCP server is not a web server. There is no port, no URL and no key: the
editor launches this file as a child process and talks to it over stdin/stdout,
the way it already launches a language server. Nothing is hosted, and the
downloads happen on this machine, into this machine's folders.

    python3 mcp_server.py              # stdio, what an editor runs
    python3 mcp_server.py --http       # streamable HTTP, for a remote client

Register it once per editor - see the MCP section of README.md:

    claude mcp add -s user -t stdio ytmeer -- python3 "<this file>"

Seventeen tools, in two groups, which is the same split the REST API makes:

    answers   yt_search, yt_video, yt_channel, yt_channel_videos,
              yt_channel_playlists, yt_similar, yt_resolve, yt_transcript,
              yt_comments, yt_preview, yt_health
              - fetched and returned here and now; no files, no job

    downloads yt_download, yt_job_status, yt_job_files, yt_job_stop, yt_jobs,
              yt_job_forget
              - a video file has to live somewhere, so it is a job: yt_download
                answers at once and yt_job_status is polled

Environment, all optional:

    YTDL_OUTDIR        where downloads go, and where cookies.txt is looked for.
                       Read by api.py at import, so set it in the editor's
                       "env" block. Defaults to ./downloads beside this file.
    YTMEER_BUSY_WAIT   seconds a tool waits for the engine when another tool
                       holds it (default 90). The REST API uses 5, because a
                       queued HTTP request would hit a proxy timeout anyway;
                       over stdio there is no proxy, and an agent comparing two
                       channels fires both calls at once.
    YTMEER_MAX_JOBS    finished jobs kept in memory (default 50). Nothing else
                       evicts them in a long-lived process. Files are untouched.
"""

import argparse
import asyncio
import atexit
import contextlib
import os
import secrets
import signal
import sys
import threading
from typing import Annotated, Any, AsyncIterator

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

# A GUI editor launches its MCP servers from a login-less environment, whose
# PATH usually has no /opt/homebrew/bin. shutil.which() would then find no
# yt-dlp, ffmpeg or aria2c, and every download would fail with an opaque
# non-zero exit. This runs BEFORE api is imported because downloader.py resolves
# aria2c and the PO-token script at import time, once.
_EXTRA_PATH = ("/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin",
               "/usr/local/sbin", os.path.expanduser("~/.local/bin"),
               "/usr/bin", "/bin", "/usr/sbin", "/sbin")


def _widen_path():
    """Put the usual install locations on PATH, keeping what is already there
    first so an editor's own override still wins."""
    seen = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for path in _EXTRA_PATH:
        if path not in seen and os.path.isdir(path):
            seen.append(path)
    os.environ["PATH"] = os.pathsep.join(seen)


_widen_path()

import api  # noqa: E402  - must follow _widen_path(); see above

SERVER_NAME = "ytmeer"
VERSION = "1.0"

# A tool that holds the engine waits this long rather than failing. See the
# module docstring. downloader_globals() reads api.PREVIEW_WAIT at call time, so
# setting it here is all that is needed.
BUSY_WAIT = max(1, int(os.environ.get("YTMEER_BUSY_WAIT") or 90))
api.PREVIEW_WAIT = BUSY_WAIT

MAX_JOBS = max(1, int(os.environ.get("YTMEER_MAX_JOBS") or 50))

# The shared secret the HTTP transport requires, and nothing else uses. Set it
# and --http demands "Authorization: Bearer <it>" on every request; leave it
# unset and --http refuses to start (see main). stdio never consults it: the
# SDK's run_stdio_async does not read the verifier or the auth settings at all,
# and the spec says a stdio server should take credentials from the
# environment rather than follow the authorization spec. So an editor's config
# can carry it harmlessly, or leave it out entirely.
HTTP_TOKEN = os.environ.get("YTMEER_HTTP_TOKEN") or ""


class StaticTokenVerifier(TokenVerifier):
    """One shared secret, compared in constant time. No OAuth.

    The spec makes authorization optional and its OAuth machinery opt-in, and
    claude.ai's custom connectors can send a fixed Authorization header, so a
    bearer token is the whole of what is needed here. Paired with
    AuthSettings(resource_server_url=None) the SDK adds no OAuth routes - no
    /authorize, /token, /register, and no protected-resource metadata
    document - just the one /mcp endpoint behind an auth gate.
    """

    def __init__(self, token):
        if not token:
            raise ValueError("a bearer token must not be empty")
        self._expected = token.encode("utf-8", "surrogateescape")

    async def verify_token(self, token):
        # compare_digest, so a wrong token cannot be found a character at a
        # time by timing the replies.
        if not secrets.compare_digest(token.encode("utf-8", "surrogateescape"),
                                      self._expected):
            return None
        return AccessToken(token=token, client_id="ytmeer-static", scopes=[])


def _http_auth(token):
    """(token_verifier, AuthSettings) for the HTTP transport, or (None, None).

    issuer_url is a required field that nothing reads in this configuration -
    it is used only by the OAuth route builders, which are not built because
    there is no auth_server_provider. resource_server_url MUST stay None:
    giving it a real URL is what switches RFC 9728 discovery on.
    """
    if not token:
        return None, None
    return (StaticTokenVerifier(token),
            AuthSettings(issuer_url="https://localhost/ytmeer-no-oauth",
                         resource_server_url=None))

INSTRUCTIONS = f"""\
YTmeer researches YouTube with yt-dlp and downloads videos, on this machine.

THE ONE RULE: long-form video only. Short-form is excluded from every list,
count, average and ranking. The floor is min_duration, default 180 seconds.
Two exemptions: a link to a channel's /shorts tab, and a video link named
directly. Pass shorts=true or min_duration=0 to turn the floor off.

A listing that kept nothing says which filter emptied it. Never read an empty
result as "there is nothing there" without reading that line.

Answers (yt_search, yt_video, yt_channel, yt_channel_videos,
yt_channel_playlists, yt_similar, yt_resolve, yt_transcript, yt_comments,
yt_preview) return data and write nothing. Downloading is a job: yt_download
answers at once with a job_id, then poll yt_job_status.

One answer-tool runs at a time; a second waits up to {BUSY_WAIT}s for the engine.
Costs worth budgeting: a publish date is ~2.1s per video, video details ~3s,
channel details ~23s, a transcript ~9s, 100 comments ~7s.

Downloads go to {api.DEFAULT_OUT} - outdir names a folder inside it.
"""


# --------------------------------------------------------------- the plumbing
@contextlib.asynccontextmanager
async def _lifespan(_server) -> AsyncIterator[dict]:
    """Make a stray print() harmless, then hand the server its run.

    By the time this runs the stdio transport has claimed fd 1 and serves the
    wire from a private duplicate, so sys.stdout is no longer the protocol.
    Pointing it at stderr means a print() anywhere - downloader.py has many,
    for cookie notes and scan progress - can never reach the wire. Without
    this, a print() is block-buffered and flushed at exit, by which time the
    transport has restored fd 1 and the bytes land on the protocol as garbage.
    The fd-level diversion the SDK does already covers raw writes and children.
    """
    sys.stdout.flush()
    sys.stdout = sys.stderr
    # Deliberately never restored. The lifespan's exit runs INSIDE
    # stdio_server(), so a restore here puts sys.stdout back on fd 1 while the
    # transport has not yet dup2'd the wire away from it - and _call's worker
    # threads cannot be cancelled, so one can still be printing. Nothing reads
    # sys.stdout after this point, so the restore buys nothing and only
    # re-arms the hazard this hook exists to remove.
    yield {}


_VERIFIER, _AUTH = _http_auth(HTTP_TOKEN)

mcp = MCPServer(SERVER_NAME, title="YTmeer", version=VERSION,
                instructions=INSTRUCTIONS, lifespan=_lifespan,
                token_verifier=_VERIFIER, auth=_AUTH)

# Every file kind api.folder_files reports. Named here so a video that is not
# yet done still carries all of them as None - api.describe hands back an empty
# files dict for an unfinished video, and a bare spread would make the keys
# vanish, turning record["video"] into a KeyError instead of None. Anything
# api.py grows beyond this list is still passed through, unnamed.
FILE_KEYS = ("video", "transcript", "words", "thumbnail", "description",
             "comments", "info")

# One engine-bound tool at a time, queued ON THE EVENT LOOP rather than in the
# thread pool. api.py's own lock would do the excluding, but every tool reaches
# it through asyncio.to_thread, which shares one ThreadPoolExecutor with at
# most min(32, cpu+4) workers: a burst of searches would park that many threads
# each waiting BUSY_WAIT seconds for the engine, and yt_download or yt_job_stop
# - which never touch the engine - would then wait behind them for a thread.
_ENGINE = asyncio.Semaphore(1)

READS = ToolAnnotations(read_only_hint=True, open_world_hint=True)
READS_LOCAL = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                         idempotent_hint=False, open_world_hint=True)
EDITS = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                        idempotent_hint=True, open_world_hint=False)


async def _call(fn, *args, **kwargs):
    """Run one blocking api.py call off the event loop, with its output
    contained and its exceptions turned into tool errors.

    Off the loop because a channel listing can take minutes - channel_page's
    own subprocess timeout is 300s - and blocking the loop would freeze the
    whole editor connection. redirect_stdout is belt to the lifespan's braces:
    it is safe because the transport does not use sys.stdout.
    """
    def work():
        with contextlib.redirect_stdout(sys.stderr):
            return fn(*args, **kwargs)

    try:
        async with _ENGINE:                   # queue on the loop, not in the pool
            return await asyncio.to_thread(work)
    except api.PreviewBusy as e:
        raise ToolError(f"the engine is busy: {e}") from None
    except api.LookupFailed as e:
        raise ToolError(f"YouTube would not answer: {e}") from None
    except api.BadField as e:                 # a ValueError that names its field
        raise ToolError(f"{e.field}: {e}") from None
    except ValueError as e:
        raise ToolError(str(e)) from None


def _find_job(job_id: str) -> dict:
    # The known-ids list is read under the same lock: iterating api.JOBS while
    # another thread's job starts or is evicted raises RuntimeError.
    with api.LOCK:
        job = api.JOBS.get(job_id)
        known = sorted(api.JOBS) if job is None else []
    if not job:
        raise ToolError(f"no such job: {job_id!r}"
                        + (f"; this server knows {', '.join(known[:10])}" if known
                           else "; this server has started none yet"))
    return job


def _evict_jobs():
    """Keep the job table bounded. Only finished jobs go, oldest first, and
    only the record - every file stays on disk."""
    with api.LOCK:
        extra = len(api.JOBS) - MAX_JOBS
        if extra <= 0:
            return
        done = sorted((j for j in api.JOBS.values() if j["state"] not in api.ACTIVE),
                      key=lambda j: j.get("finished") or j.get("created") or 0)
        for job in done[:extra]:
            api.JOBS.pop(job["job_id"], None)


def _status(job: dict, since: int, tail: int) -> dict:
    """A job's whole status answer, built in one worker.

    api.public reads and json-parses the job's download_log.jsonl off disk, so
    it does not belong on the event loop. state, videos_done and failures all
    come from that one read and `done` is derived from the same `state`, so
    they cannot contradict each other; only the video rows come from a second
    read, and those can only be further ahead, never behind.
    """
    answer = api.public(job, tail=tail)
    _state, records = api.job_records(job)
    videos = api.video_records(records)
    answer.update({
        "done": answer["state"] not in api.ACTIVE,
        "next": len(videos),
        "videos": [_describe(r, job["outdir"]) for r in videos[since:]],
    })
    return answer


def _all_jobs() -> list[dict]:
    with api.LOCK:
        jobs = list(api.JOBS.values())
    return [api.public(j, tail=0) for j in jobs]


def _local_files(outdir: str, job_id: str) -> list[dict]:
    """api.list_files, with the HTTP path swapped for the absolute one. The
    editor is on this machine, so a path it can open beats a URL it cannot."""
    rows = []
    for row in api.list_files(outdir, job_id):
        rows.append({"path": os.path.join(outdir, row["path"].replace("/", os.sep)),
                     "bytes": row["bytes"]})
    return rows


def _describe(record: dict, outdir: str) -> dict:
    """api.describe, flattened for a model: the per-file paths come up to the
    top level and the HTTP "downloads" mirror is dropped.

    FILE_KEYS are always present, as None when absent, so a caller can read
    record["video"] on an unfinished video without a KeyError. Anything api.py
    reports beyond them comes through too - a hardcoded list silently dropped
    "comments" the moment api.py grew it, and a job asked for comments then
    handed back no path to them.
    """
    seen = api.describe(record, outdir)      # no job_id: no URLs are built
    files = seen.pop("files", None) or {}
    seen.pop("downloads", None)
    seen.update({key: files.get(key) for key in FILE_KEYS})
    seen.update({k: v for k, v in files.items() if k not in FILE_KEYS})
    return seen


# ------------------------------------------------------------------- answers
@mcp.tool(annotations=READS_LOCAL)
async def yt_health() -> dict[str, Any]:
    """Check this machine before downloading: which external tools are on PATH,
    where downloads go, whether a cookies.txt is in place, and how many jobs
    are live.

    Worth one call before the first download of a session, so a failure can be
    explained rather than guessed at. yt-dlp and ffmpeg are required; aria2c
    makes downloads about 4x faster; node or deno is needed for YouTube's "n"
    challenge. Anything in missing_required is a `brew install` away.
    """
    tools = await _call(api.tools_present)
    with api.LOCK:
        states: dict[str, int] = {}
        for job in api.JOBS.values():
            states[job["state"]] = states.get(job["state"], 0) + 1
    cookies = os.path.join(api.DEFAULT_OUT, "cookies.txt")
    return {
        "download_root": api.DEFAULT_OUT,
        "tools": tools,
        "missing_required": [t for t in ("yt-dlp", "ffmpeg", "ffprobe")
                             if not tools.get(t)],
        "js_runtime": bool(tools.get("node") or tools.get("deno")),
        "fast_downloader": bool(tools.get("aria2c")),
        "cookies_file": cookies if os.path.isfile(cookies) else None,
        "jobs": states,
        "code": api.CODE_VERSION,
        "api_version": api.VERSION,
        "server_version": VERSION,
        "busy_wait_seconds": BUSY_WAIT,
    }


@mcp.tool(annotations=READS)
async def yt_search(
    query: Annotated[str, Field(description="What to search YouTube for. A video title works well as a query.")],
    in_channel: Annotated[str, Field(description="Search only inside this channel, using its own search tab. A channel link.")] = "",
    count: Annotated[int, Field(description="Results to scan before filtering. 0 follows the route: 150 normally, 40 when sort is date or oldest, because that scan costs about thirty times more per result.", ge=0, le=10000)] = 0,
    match: Annotated[str, Field(description="How wide a net: 'related' (YouTube's own results page, so near-matches come too) or 'exact' (the far narrower ytsearch route). Ignored with in_channel.")] = "related",
    sort: Annotated[str, Field(description="relevance, views, popularity or least-viewed order what came back, locally - the same request goes to YouTube either way. date and oldest are NOT that: they force YouTube's dated route, which scans only 40 results instead of 150, so a date sort cannot see further back than that depth. Raise count to scan deeper there, at about 2s per result.")] = "relevance",
    dates: Annotated[bool, Field(description="Fetch each surviving result's publish date. About 2.1s per video, at most 20 per call. Leave it true whenever days or this_month is set: with a window, dates=false does not merely skip the dates, it hands the window to YouTube's own date search, which scans 40 deep instead of 150 and asks a much narrower question - and that answer carries no date_capped warning.")] = True,
    shorts: Annotated[bool, Field(description="Include short-form video. Off by default; overrides min_duration.")] = False,
    min_duration: Annotated[int, Field(description="Shortest video to keep, in seconds. 0 = any length.", ge=0, le=86400)] = 180,
    days: Annotated[int, Field(description="Only videos from the last N days. 0 = any time.", ge=0, le=100000)] = 0,
    this_month: Annotated[bool, Field(description="Only videos since the 1st of this month. Cannot be combined with days.")] = False,
    views: Annotated[int, Field(description="Drop results under N views.", ge=0)] = 0,
    limit: Annotated[int, Field(description="Keep only the first N results. Keep it small when dates is true.", ge=0, le=1000000)] = 0,
    skip: Annotated[str, Field(description="Links or 11-character ids to leave out, whitespace separated.")] = "",
) -> dict[str, Any]:
    """Search YouTube and get each video's link, title, views, channel, length
    and publish date. Nothing is downloaded.

    match='related' is the default and is usually right: it keeps YouTube's
    relevance ordering, which carries the near-matches - the same story from
    another channel, the opposite angle, the same formula in another language -
    and those are the research material. On one measured title, 'related'
    returned 150 results where 'exact' returned 2. Asking YouTube to sort by
    views instead collapses onto the exact phrase and loses all of it, so sort
    orders what came back, locally.

    A date window (days, this_month) is applied here rather than by YouTube,
    over dates fetched for the results that survived the filters - which keeps
    the breadth YouTube's own date search throws away. On one title this found
    20 videos for the month where YouTube's date search returned 3.

    READ THIS BEFORE REPORTING AN EMPTY RESULT: found=0 with date_capped=true
    means "did not look far enough", NOT "nothing was published". The 20-date
    budget ran out before the window was filled, and raising limit does NOT
    extend it - the walk dates the same first 20 results whatever limit says,
    so retrying with a bigger limit returns the identical answer. Narrow the
    query so the in-window videos rank higher, or use sort='date', which asks
    YouTube for the window itself. scanned is what YouTube returned, found what
    survived; the response does not break found down by which filter dropped
    what, so say which filters were set rather than claiming nothing exists.
    """
    answer = await _call(api.search_youtube, query, in_channel, count, sort,
                         days, this_month, views, limit, skip, match, shorts,
                         min_duration, dates)
    # The engine failed - a bot check, a timeout - and found nothing. /api/search
    # answers that with a 502 for exactly this reason: handed back as a success
    # with found=0, an agent reports "nothing matched" when YouTube never
    # answered. A partial answer (results, with a noted error) still stands.
    if isinstance(answer, dict) and answer.get("error") and not answer.get("results"):
        raise ToolError(f"the search did not run: {answer['error']}")
    return answer


@mcp.tool(annotations=READS)
async def yt_video(
    url: Annotated[str, Field(description="A video link, or a bare 11-character video id.")],
) -> dict[str, Any]:
    """One video's details: title, description, channel, subscribers, views,
    likes, comment count, duration, upload time in PKT, category, tags,
    thumbnail, live status, age limit and which caption tracks exist.

    A value YouTube does not publish comes back null, never "NA" - that is the
    only way to tell "no likes" from "likes hidden". Read captions.auto before
    asking for a transcript: only an auto-generated track has word timings.
    """
    return await _call(api.video_info, url)


@mcp.tool(annotations=READS)
async def yt_channel(
    channel: Annotated[str, Field(description="A channel link: @handle, /c/Name, /channel/UC..., /user/Name, with or without a trailing tab.")],
    shorts: Annotated[bool, Field(description="Include short-form video in every count and average.")] = False,
    min_duration: Annotated[int, Field(description="Shortest video to count, in seconds. 0 = any length.", ge=0, le=86400)] = 180,
) -> dict[str, Any]:
    """A channel's full picture, counted from its own long videos rather than
    estimated: average and median views, best video, average length, uploads
    per week, first and last upload, subscribers, and every video at 2x the
    channel's own average or better.

    outliers are relative to THIS channel: 2.0 means twice this channel's own
    average views, not twice anything global. The average is taken over every
    long video, so the ratio compares a video with its channel.

    created, lifetime_views and country are scraped from the About page because
    yt-dlp exposes none of them; any failure leaves those three null and the
    rest of the report stands. At most 2000 videos are read - listing_capped
    says when it stopped there, and the averages then describe only that slice.
    Costs about 23s on a 278-video channel.
    """
    return await _call(api.channel_info, channel, shorts, min_duration)


@mcp.tool(annotations=READS)
async def yt_channel_videos(
    channel: Annotated[str, Field(description="A channel link.")],
    limit: Annotated[int, Field(description="Keep only the newest N. 0 = every long video.", ge=0, le=1000000)] = 0,
    shorts: Annotated[bool, Field(description="Include short-form video.")] = False,
    min_duration: Annotated[int, Field(description="Shortest video to keep, in seconds. 0 = any length.", ge=0, le=86400)] = 180,
    skip: Annotated[str, Field(description="Links or 11-character ids to leave out.")] = "",
) -> dict[str, Any]:
    """A channel's long videos, newest first, each with its ratio to the
    channel's own average views - so you can see which of its videos beat it.

    NO PUBLISH DATES. A flat YouTube listing carries views, duration and
    channel but no upload date, and dating 276 videos would take ten minutes.
    Use yt_channel for first and last upload, which dates exactly two videos.

    average_views is computed over every long video BEFORE limit is applied,
    so each outlier ratio compares a video with its channel rather than with
    the others on the page. Costs about 11s for 278 videos.
    """
    return await _call(api.channel_video_rows, channel, limit, shorts,
                       min_duration, skip)


@mcp.tool(annotations=READS)
async def yt_channel_playlists(
    channel: Annotated[str, Field(description="A channel link.")],
    shorts: Annotated[bool, Field(description="Include short-form video in the split.")] = False,
    min_duration: Annotated[int, Field(description="Shortest video to count as long-form, in seconds.", ge=0, le=86400)] = 180,
) -> dict[str, Any]:
    """A channel's playlists - how it groups its own content - each split into
    long videos and short-form.

    videos is yt-dlp's own total and COUNTS SHORTS; long_videos and shorts are
    counted here, which is the only honest answer for a Shorts compilation. The
    split is counted over at most 200 entries per playlist, so on a huge
    playlist the parts will not add up to the total and split_capped says so.
    One listing names them all, but the split needs a request per playlist, so
    it is filled in for the first 25 and is null after - count_capped says when
    there were more. Costs about 8s for 5 playlists.
    """
    return await _call(api.channel_playlist_rows, channel, shorts, min_duration)


@mcp.tool(annotations=READS)
async def yt_similar(
    url: Annotated[str, Field(description="A VIDEO link or 11-character id. A channel or playlist link is refused.")],
    limit: Annotated[int, Field(description="How many to keep. 0 = every match, but keep it at 20 or fewer while dates is true: past 20 survivors no publish date is fetched at all and date_capped comes back true.", ge=0, le=1000000)] = 0,
    shorts: Annotated[bool, Field(description="Include short-form video.")] = False,
    min_duration: Annotated[int, Field(description="Shortest video to keep, in seconds.", ge=0, le=86400)] = 180,
    dates: Annotated[bool, Field(description="Fetch publish dates, ~2.1s each. All or nothing: 20 survivors or fewer are all dated, more than 20 and NONE are.")] = True,
    skip: Annotated[str, Field(description="Links or ids to leave out.")] = "",
) -> dict[str, Any]:
    """Long videos like this one - who else tried this angle, and how it went.

    YouTube's related feed is not reachable through yt-dlp, so this searches
    for the video's OWN TITLE and leaves the video itself out. That is why it
    needs a video link and refuses a channel one. filtered_out gives per-reason
    counts, so a thin result explains itself. About 24s without dates, 65s with
    them for 20 kept.
    """
    return await _call(api.similar_to, url, limit, shorts, min_duration, dates, skip)


@mcp.tool(annotations=READS)
async def yt_resolve(
    url: Annotated[str, Field(description="Any YouTube link: a handle, a channel tab, or a video.")],
) -> dict[str, Any]:
    """The canonical channel behind any link: its id, name, handle, videos tab
    and subscriber count.

    Use this first to get the one channel id that every other channel tool
    keys off. Takes a video link too, and says which it was given in "from".
    A YouTube link that is neither a video nor a channel - an uploads playlist
    (playlist?list=UU...), a bare legacy vanity URL, a /results page - is
    refused rather than answered with nulls.
    """
    return await _call(api.resolve_link, url)


@mcp.tool(annotations=READS)
async def yt_transcript(
    url: Annotated[str, Field(description="A video link, or a bare 11-character video id.")],
    sub_lang: Annotated[str, Field(description="Transcript language code, such as en or pt-BR.")] = "en",
    include_words: Annotated[bool, Field(description="Also return the word-level transcript. It is very long - one word per line with millisecond stamps - so it is left out unless you need exact word timings.")] = False,
) -> dict[str, Any]:
    """A video's transcript as text, with [hh:mm:ss] stamps - no files written.

    This is the tool to reach for before summarising a video, pulling its
    structure, or writing from it: it is one call and about 9s, where
    downloading the video would be a job and many megabytes.

    words is null, with the reason in note, when YouTube has no auto-generated
    track for the language - only auto-generated tracks carry word timings.
    By default the word-level form is omitted entirely; pass include_words=true
    for it.
    """
    answer = await _call(api.video_transcript, url, sub_lang)
    if not include_words and isinstance(answer, dict) and answer.get("words"):
        answer = dict(answer)
        answer["words"] = None
        answer["words_omitted"] = "pass include_words=true for the word-level form"
    return answer


@mcp.tool(annotations=READS)
async def yt_comments(
    url: Annotated[str, Field(description="A video link, or a bare 11-character video id.")],
    limit: Annotated[int, Field(description="Top-level comments to fetch. 0 = all of them. Replies are extra.", ge=0, le=100000)] = 100,
    replies: Annotated[bool, Field(description="Also fetch replies. Roughly triples the time.")] = False,
    sort: Annotated[str, Field(description="'top' or 'new'.")] = "top",
) -> dict[str, Any]:
    """A video's comments, threaded - what its audience actually said.

    Each top-level comment carries author, likes, published (PKT), text and its
    replies. total_on_video against fetched shows what the limit left behind.
    About 7s for 100 comments, 19s with replies.
    """
    return await _call(api.video_comments, url, limit, replies, sort)


@mcp.tool(annotations=READS)
async def yt_preview(
    channel: Annotated[str, Field(description="A channel link.")],
    views: Annotated[int, Field(description="Skip videos under N views.", ge=0)] = 0,
    limit: Annotated[int, Field(description="How many matching videos to list. A preview is a look, not the channel.", ge=1, le=500)] = 20,
    skip: Annotated[str, Field(description="Links or ids to leave out.")] = "",
    shorts: Annotated[bool, Field(description="Include short-form video, as a job would not.")] = False,
    min_duration: Annotated[int, Field(description="Shortest video to keep, in seconds.", ge=0, le=86400)] = 180,
) -> dict[str, Any]:
    """The newest videos a channel link would download, up to limit - the dry
    run to show a user before starting a job.

    IT IS NOT THE WHOLE JOB. limit defaults to 20 here, while a yt_download
    job's limit defaults to every video, and the walk stops the moment limit is
    satisfied. So capped=true means "there are more" - never report this as the
    full list of what a download would fetch, and never present matched as the
    channel's video count. Raise limit (up to 500) to see further.

    It takes the same length knobs a job does, on purpose: a preview using a
    different floor from the job it previews would answer a different
    question. The floor it used comes back as min_duration, so a short answer
    is never a mystery. videos are bare links; use yt_channel_videos when you
    need titles and view counts to choose from.
    """
    answer = await _call(api.preview_channel, channel, views, limit, skip,
                         shorts, min_duration)
    if isinstance(answer, dict):
        # The walk stops at LIMIT without saying so, so say it here. A deadline
        # cut shows up instead as a set "error", which the agent also needs.
        answer["limit"] = limit
        answer["capped"] = bool(answer.get("matched", 0) >= limit
                                or answer.get("error"))
    return answer


# ----------------------------------------------------------------- downloads
@mcp.tool(annotations=WRITES)
async def yt_download(
    links: Annotated[list[str] | None, Field(description="Video links, channel links, or bare 11-character ids - mixed freely. A channel link is expanded into its videos. Leave this out when using search.")] = None,
    search: Annotated[str | None, Field(description="Let a YouTube search supply the worklist instead of links. The two cannot be combined.")] = None,
    outdir: Annotated[str | None, Field(description="A folder name such as 'batch1', created inside the download root. Leave out for the root itself. One job per folder at a time.")] = None,
    channel: Annotated[bool | None, Field(description="Group downloads into <Channel>/<Video>/ folders. On by default; pass false for flat <Video>/ folders. Predicted paths are wrong if you assume flat.")] = None,
    transcript: Annotated[bool | None, Field(description="Transcript only: write the transcript and never fetch the video. Much faster and smaller.")] = None,
    extract_comments: Annotated[bool | None, Field(description="Also write each video's comments.")] = None,
    comment_limit: Annotated[int | None, Field(description="Top-level comments per video. 0 = all.", ge=0, le=100000)] = None,
    comment_replies: Annotated[bool | None, Field(description="Also fetch comment replies.")] = None,
    comment_sort: Annotated[str | None, Field(description="'top' or 'new'.")] = None,
    views: Annotated[int | None, Field(description="Channel links and search: skip videos under N views.", ge=0)] = None,
    limit: Annotated[int | None, Field(description="Channel links and search ONLY: keep the newest/top N, counted per channel rather than per run. It is IGNORED for named video links - to download fewer of those, send fewer links.", ge=0, le=1000000)] = None,
    days: Annotated[int | None, Field(description="Only videos from the last N days. This is the download date filter; this_month is a search window.", ge=0, le=100000)] = None,
    shorts: Annotated[bool | None, Field(description="Include short-form video.")] = None,
    min_duration: Annotated[int | None, Field(description="Shortest video to download, in seconds. 0 = any length.", ge=0, le=86400)] = None,
    min_height: Annotated[int | None, Field(description="Quality floor in pixels; never download below it.", ge=1, le=10000)] = None,
    max_height: Annotated[int | None, Field(description="Quality ceiling in pixels.", ge=1, le=10000)] = None,
    subs: Annotated[bool | None, Field(description="Build transcripts beside each video. On by default.")] = None,
    sub_lang: Annotated[str | None, Field(description="Transcript language code, such as en or pt-BR.")] = None,
    thumbnail: Annotated[bool | None, Field(description="Save the thumbnail. On by default.")] = None,
    description: Annotated[bool | None, Field(description="Save the description. On by default.")] = None,
    in_channel: Annotated[str | None, Field(description="With search: search only this channel's own tab.")] = None,
    search_count: Annotated[int | None, Field(description="With search: results to scan before filtering. 0 lets the route pick.", ge=0, le=10000)] = None,
    sort: Annotated[str | None, Field(description="With search: relevance, views, popularity, least-viewed, date or oldest.")] = None,
    match: Annotated[str | None, Field(description="With search: 'related' or 'exact'.")] = None,
    this_month: Annotated[bool | None, Field(description="With search: only since the 1st of this month. Use days to limit a download by date.")] = None,
    dates: Annotated[bool | None, Field(description="With search: fetch publish dates.")] = None,
    skip: Annotated[list[str] | None, Field(description="Videos never to fetch - links or bare ids. Replaces the folder's skip.txt; leave out to keep it.")] = None,
    verbose: Annotated[bool | None, Field(description="Keep yt-dlp's full output in the job log.")] = None,
) -> dict[str, Any]:
    """Start downloading. Answers AT ONCE with a job_id; the work goes on in the
    background. Poll yt_job_status until done is true.

    Call yt_preview or yt_channel_videos first and show the user what would come
    down - a channel link with no limit means the whole channel. Set
    transcript=true when only the text is wanted: no video file is fetched.

    Only options actually passed are sent on, so a flag that means nothing
    without search is refused rather than silently ignored. links and search are
    two different worklists and cannot be combined. Videos already on disk are
    skipped without a network call, so re-running the same links is cheap and
    safe. One job per folder at a time - give outdir another name to run two.
    """
    sent: dict[str, Any] = {}
    for key, value in (("links", links), ("search", search), ("outdir", outdir),
                       ("channel", channel), ("transcript", transcript),
                       ("extract_comments", extract_comments),
                       ("comment_limit", comment_limit),
                       ("comment_replies", comment_replies),
                       ("comment_sort", comment_sort), ("views", views),
                       ("limit", limit), ("days", days), ("shorts", shorts),
                       ("min_duration", min_duration), ("min_height", min_height),
                       ("max_height", max_height), ("subs", subs),
                       ("sub_lang", sub_lang), ("thumbnail", thumbnail),
                       ("description", description), ("in_channel", in_channel),
                       ("search_count", search_count), ("sort", sort),
                       ("match", match), ("this_month", this_month),
                       ("dates", dates), ("skip", skip), ("verbose", verbose)):
        if value is not None:
            sent[key] = value

    # normalize_request refuses match, search_count, dates, this_month and
    # in_channel without a search, but not sort - it validates it and then only
    # emits it inside the search branch, so a links worklist takes it and drops
    # it. This tool's docstring promises nothing is silently ignored, so close
    # the one hole here rather than weaken the promise.
    if "sort" in sent and not sent.get("search"):
        raise ToolError("sort orders search results; it needs search too. "
                        "A links worklist downloads in the order given")

    # normalize_request judges some fields on whether they were SENT, not on the
    # filled-in value, so only what the caller actually passed goes in.
    try:
        opts = api.normalize_request(sent)
    except ValueError as e:
        raise ToolError(str(e)) from None
    try:
        job = await asyncio.to_thread(api.start_job, opts)
    except RuntimeError as e:                 # another job holds that folder
        raise ToolError(str(e)) from None
    except OSError as e:
        raise ToolError(f"could not prepare {opts['outdir']}: {e}") from None
    _evict_jobs()

    answer = await asyncio.to_thread(api.public, job, 0)
    answer["poll"] = "call yt_job_status with this job_id until done is true"
    return answer


@mcp.tool(annotations=READS_LOCAL)
async def yt_job_status(
    job_id: Annotated[str, Field(description="The job_id yt_download returned.")],
    since: Annotated[int, Field(description="Pass back the 'next' from your previous call to get only what finished since then.", ge=0)] = 0,
    tail: Annotated[int, Field(description="Lines of the job's recent output to include.", ge=0, le=2000)] = 5,
) -> dict[str, Any]:
    """How far a job got, which videos are finished, where their files are, and
    what failed. This answers both "is it done" and "where are my transcripts".

    Every finished video carries ABSOLUTE paths: video, transcript, words,
    thumbnail, description, info. Act on status 'ok' and 'skipped' (already on
    disk). A 'failed' video can still come back as 'ok' - a run retries its
    failures once at the end - so read the job's failures list for what finally
    did not come down. 'unavailable' is gone for good; 'blocked' means YouTube
    refused this machine's IP and says nothing about the video, so give the
    download folder a cookies.txt or run from another network.

    Pass back next as since to page through long runs.
    """
    job = _find_job(job_id)
    return await asyncio.to_thread(_status, job, since, tail)


@mcp.tool(annotations=READS_LOCAL)
async def yt_job_files(
    job_id: Annotated[str, Field(description="The job_id yt_download returned.")],
) -> dict[str, Any]:
    """Every finished file in a job's folder, with its absolute path and size -
    including videos from earlier runs in the same folder.

    Only what the downloader made inside a video folder is listed: never
    cookies.txt, the run logs, or anything still being written.
    """
    job = _find_job(job_id)
    files = await asyncio.to_thread(_local_files, job["outdir"], job_id)
    return {"job_id": job_id, "folder": job["outdir"], "count": len(files),
            "total_bytes": sum(f["bytes"] for f in files), "files": files}


@mcp.tool(annotations=EDITS)
async def yt_job_stop(
    job_id: Annotated[str, Field(description="The job_id to stop.")],
) -> dict[str, Any]:
    """Stop a running job, the way Ctrl+C would - the yt-dlp and ffmpeg it is
    running stop too. Videos already finished stay on disk.

    stopping=false means it had already ended; call yt_job_status for the real
    outcome. Re-running the same links later picks up where it left off.
    """
    job = _find_job(job_id)
    stopping = await asyncio.to_thread(api.stop_job, job)
    return {"job_id": job_id, "stopping": stopping,
            "note": ("stopping; finished videos are kept" if stopping
                     else "it had already ended - call yt_job_status for the outcome")}


@mcp.tool(annotations=READS_LOCAL)
async def yt_jobs() -> dict[str, Any]:
    """Every job this server has started, with its state and folder.

    The table lives in this process, so it is empty after the editor restarts
    and is not shared with another editor's copy of the server. The files on
    disk are, and yt_job_files still finds them.
    """
    jobs = await asyncio.to_thread(_all_jobs)
    return {"count": len(jobs), "max_kept": MAX_JOBS, "jobs": jobs}


@mcp.tool(annotations=EDITS)
async def yt_job_forget(
    job_id: Annotated[str, Field(description="The job_id to drop from this server's memory.")],
) -> dict[str, Any]:
    """Forget a finished job. Its files stay exactly where they are.

    Only needed to tidy up: finished jobs are evicted automatically once the
    table passes its cap.
    """
    job = _find_job(job_id)
    if job["state"] in api.ACTIVE:
        raise ToolError(f"job {job_id} is still {job['state']}; stop it first")
    with api.LOCK:
        api.JOBS.pop(job_id, None)
    return {"job_id": job_id, "forgotten": True, "files_kept": job["outdir"]}


# -------------------------------------------------------------------- startup
def _install_shutdown():
    """Stop every running download when this process goes away.

    api.main() registers this for the REST server, but importing api registers
    nothing - so without it a download would keep running, unseen, into a
    folder the next server thinks is free. Each downloader runs in its own
    process group, so nothing else would stop it.

    The handler does the stop itself and leaves with os._exit. sys.exit does
    NOT work here, which was measured: SystemExit unwinds mcp.run(), and the
    interpreter then blocks in threading._shutdown() joining anyio's
    non-daemon worker and the pool every asyncio.to_thread call shares. atexit
    runs only after that join - 18s later with a 20s call in flight, and never
    at all while the client's stdin stays open - so an editor that sends
    SIGTERM then SIGKILL kills the process first and the jobs are orphaned. A
    channel listing can hold a worker for its own 300s subprocess timeout.

    The stop runs off the main thread because a signal can land while a
    coroutine holds api.LOCK, which shutdown_jobs takes as well; the join is
    bounded so the handler cannot outlive a normal SIGKILL grace period. The
    atexit registration stays as the clean-return path.
    """
    atexit.register(api.shutdown_jobs)
    leaving = threading.Event()

    def terminate(_signum, _frame):
        if leaving.is_set():                  # told twice: go now
            os._exit(0)
        leaving.set()
        stopper = threading.Thread(target=api.shutdown_jobs, kwargs={"timeout": 4},
                                   daemon=True, name="ytmeer-shutdown")
        stopper.start()
        stopper.join(5)
        os._exit(0)                           # skips the join that blocks atexit

    for name in ("SIGTERM", "SIGHUP"):
        number = getattr(signal, name, None)
        if number is not None:
            with contextlib.suppress(OSError, ValueError):
                signal.signal(number, terminate)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="mcp_server.py",
        description="ytmeer as MCP tools. Stdio by default, which is what an "
                    "editor runs; nothing is hosted.")
    parser.add_argument("--http", action="store_true",
                        help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default=None, help="HTTP bind address "
                                                     "(default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None,
                        help="HTTP port (default 8765)")
    parser.add_argument("--no-auth", action="store_true",
                        help="serve HTTP with no token. Loopback only, for a "
                             "local test; never behind a tunnel")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    # Refused rather than ignored: silently accepting --port on the stdio path
    # leaves someone waiting on a port nothing is listening to.
    if not args.http and (args.host is not None or args.port is not None
                          or args.no_auth):
        parser.error("--host, --port and --no-auth need --http")

    host = args.host or "127.0.0.1"
    if args.http:
        # Fail closed. This process shells out to yt-dlp, reads a cookies.txt
        # holding a live YouTube session and reports paths from this disk, so an
        # unauthenticated HTTP transport is not a default worth having - the
        # earlier version of this file simply bound to localhost and left the
        # token to the README, which is one forgotten flag away from an open
        # server.
        if not HTTP_TOKEN and not args.no_auth:
            parser.error(
                "--http needs a token. Make one and put it in the environment:\n"
                "    export YTMEER_HTTP_TOKEN=\"$(python3 -c "
                "'import secrets;print(secrets.token_urlsafe(32))')\"\n"
                "  Callers then send it as:  Authorization: Bearer <token>\n"
                "  For a local test with no token at all, pass --no-auth "
                "(loopback only).")
        if args.no_auth and host not in ("127.0.0.1", "::1", "localhost"):
            parser.error(f"--no-auth serves no token, so it is loopback only; "
                         f"--host {host} would put an open server on the network")

    _install_shutdown()
    if args.http:
        port = args.port or 8765
        guard = ("token required (Authorization: Bearer ...)" if HTTP_TOKEN
                 else "NO AUTH - loopback only")
        print(f"ytmeer MCP on http://{host}:{port}/mcp  [{guard}]", file=sys.stderr)
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
