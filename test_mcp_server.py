"""Tests for mcp_server.py - the MCP face of the toolkit.

Offline by default. The tests that talk to YouTube are marked `network` and are
deselected unless you ask for them:

    python3 -m pytest test_mcp_server.py -q
    python3 -m pytest test_mcp_server.py -q -m network     # also hits YouTube

What is worth testing here is not the engine - test_downloader.py and
test_api.py cover that - but the three things this layer is responsible for:
the wire stays clean, api.py's exceptions become tool errors, and the shapes
handed to a model are the ones its descriptions promise.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
VID = "dQw4w9WgXcQ"

pytest.importorskip("mcp", reason="the MCP server needs `pip install mcp>=2.3`")

import mcp_server                                            # noqa: E402
from mcp import Client, StdioServerParameters               # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError        # noqa: E402


def tool_names():
    return sorted(t.name for t in asyncio.run(mcp_server.mcp.list_tools()))


# ------------------------------------------------------------------ registry
def test_every_tool_registers_with_a_schema():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert len(tools) == 17
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"
        assert tool.input_schema.get("type") == "object"
        # dict[str, Any] returns, so the model gets structuredContent as well
        # as the JSON dump - a bare `dict` annotation would silently lose it.
        assert tool.output_schema, f"{tool.name} lost its output schema"


def test_read_only_tools_are_marked_read_only():
    writes = {"yt_download", "yt_job_stop", "yt_job_forget"}
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        hint = getattr(tool.annotations, "read_only_hint", None)
        assert hint is (tool.name not in writes), f"{tool.name} is mis-annotated"


def test_search_parameters_carry_their_descriptions():
    tool = next(t for t in asyncio.run(mcp_server.mcp.list_tools())
                if t.name == "yt_search")
    props = tool.input_schema["properties"]
    assert set(props) >= {"query", "match", "sort", "dates", "min_duration",
                          "this_month", "in_channel", "count"}
    for name, spec in props.items():
        assert spec.get("description"), f"yt_search.{name} has no description"
    # The two descriptions a wrong answer used to come from.
    assert "40" in props["sort"]["description"], "sort must warn about the dated route"
    assert "narrower" in props["dates"]["description"]


# ------------------------------------------------------- the stdio contract
def rpc(lines, timeout=60):
    """Drive the server over a real pipe and return (stdout, stderr)."""
    done = subprocess.run([sys.executable, SERVER],
                          input="".join(json.dumps(m) + "\n" for m in lines),
                          capture_output=True, text=True, timeout=timeout)
    return done.stdout, done.stderr


HANDSHAKE = [
    {"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"}}},
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
]


def test_nothing_but_json_rpc_reaches_the_wire():
    """The one failure that corrupts the protocol rather than one answer.

    downloader.py print()s in several places a lookup reaches. A print is
    block-buffered on sys.stdout and flushed at interpreter exit - after the
    transport has restored fd 1 - so without the lifespan guard those bytes
    land on the wire. Assert the whole of stdout parses.
    """
    out, _err = rpc(HANDSHAKE + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "yt_health", "arguments": {}}},
    ])
    for line in out.splitlines():
        if line.strip():
            json.loads(line)        # raises if anything else got printed


def test_sigterm_leaves_promptly_with_a_call_in_flight():
    """atexit is not a shutdown path here.

    It runs only after CPython joins the pool every asyncio.to_thread call
    shares, so a tool in flight held the process past an editor's SIGKILL -
    and with the client's stdin still open it never exited at all, leaving
    downloads running into a folder the next server thinks is free.
    """
    proc = subprocess.Popen([sys.executable, SERVER],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True)
    try:
        for message in HANDSHAKE:
            proc.stdin.write(json.dumps(message) + "\n")
        proc.stdin.flush()
        proc.stdout.readline()                       # the initialize reply
        # A call that parks a worker thread for a while.
        proc.stdin.write(json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "yt_preview", "arguments": {
                 "channel": "https://www.youtube.com/@RickAstleyYT"}}}) + "\n")
        proc.stdin.flush()
        time.sleep(3)
        proc.send_signal(signal.SIGTERM)
        # stdin stays open: that is the case that used to hang for ever.
        assert proc.wait(timeout=20) == 0, "the handler did not run"
    finally:
        if proc.poll() is None:
            proc.kill()


# ------------------------------------------------------------ the plumbing
def test_describe_always_carries_every_file_key():
    """A record for a video that is not finished still answers record["video"].

    api.describe hands back an empty files dict for one, so spreading it would
    make the keys vanish and turn a None into a KeyError.
    """
    seen = mcp_server._describe({"url": "x", "status": "failed", "folder": None},
                                HERE)
    for key in mcp_server.FILE_KEYS:
        assert key in seen, f"{key} went missing"
        assert seen[key] is None
    assert "downloads" not in seen and "files" not in seen


def test_unknown_job_names_what_is_known():
    with pytest.raises(ToolError) as caught:
        mcp_server._find_job("job_nope")
    assert "no such job" in str(caught.value)


def test_path_is_widened_before_api_is_imported():
    """A GUI editor starts its servers without /opt/homebrew/bin on PATH, and
    downloader.py resolves aria2c at import time, once."""
    on_path = os.environ["PATH"].split(os.pathsep)
    for wanted in mcp_server._EXTRA_PATH:
        if os.path.isdir(wanted):
            assert wanted in on_path


def test_jobs_are_evicted_oldest_finished_first():
    api = mcp_server.api
    kept, mcp_server.MAX_JOBS = mcp_server.MAX_JOBS, 2
    try:
        with api.LOCK:
            api.JOBS.clear()
            for n in range(4):
                api.JOBS[f"job_{n}"] = {"job_id": f"job_{n}", "state": "finished",
                                        "finished": float(n), "created": float(n),
                                        "outdir": HERE}
            api.JOBS["job_live"] = {"job_id": "job_live", "state": "running",
                                    "finished": None, "created": 9.0,
                                    "outdir": HERE}
        mcp_server._evict_jobs()
        with api.LOCK:
            left = set(api.JOBS)
        assert "job_live" in left, "a running job must never be evicted"
        assert "job_0" not in left and "job_1" not in left
        assert len(left) == 2
    finally:
        mcp_server.MAX_JOBS = kept
        with api.LOCK:
            api.JOBS.clear()


# ------------------------------------------------------------------- argv
@pytest.mark.parametrize("argv", [["--port", "9000"], ["--host", "1.2.3.4"]])
def test_host_and_port_need_http(argv):
    """Accepting them on the stdio path leaves someone waiting on a port that
    nothing is listening to."""
    with pytest.raises(SystemExit) as caught:
        mcp_server.main(argv)
    assert caught.value.code == 2


def test_a_bad_port_is_a_usage_error_not_a_traceback():
    with pytest.raises(SystemExit):
        mcp_server.main(["--http", "--port", "not-a-number"])


# --------------------------------------------------------- refusals, offline
def call(name, arguments, timeout=180):
    async def go():
        params = StdioServerParameters(command=sys.executable, args=[SERVER])
        async with Client(params) as client:
            await client.list_tools()
            return await asyncio.wait_for(
                client.call_tool(name, arguments), timeout=timeout)
    return asyncio.run(go())


@pytest.mark.parametrize("arguments, expected", [
    ({"links": ["x"], "search": "y"}, "two different worklists"),
    ({}, "links is required"),
    ({"links": [VID], "match": "exact"}, "needs search too"),
    # normalize_request refuses match/search_count/dates/this_month without a
    # search but not sort, which it validates and then drops.
    ({"links": [VID], "sort": "views"}, "needs search too"),
])
def test_download_refuses_rather_than_ignoring(arguments, expected):
    answer = call("yt_download", arguments)
    assert answer.is_error
    assert expected in answer.content[0].text


@pytest.mark.parametrize("url", [
    "https://evil.example/watch?v=dQw4w9WgXcQ",      # SSRF: a chosen host
    "not a link",
    "https://www.youtube.com/playlist?list=UU_x5XG1OV2P6uZZ5FSM9Ttw",
])
def test_lookups_refuse_a_link_that_is_not_a_youtube_video(url):
    answer = call("yt_video", {"url": url})
    assert answer.is_error


def test_health_answers_without_a_network():
    answer = call("yt_health", {})
    assert not answer.is_error
    health = json.loads(answer.content[0].text)
    assert health["download_root"] and "tools" in health
    assert set(health["tools"]) >= {"yt-dlp", "ffmpeg", "node"}


# ------------------------------------------------------------- with YouTube
@pytest.mark.network
def test_resolve_and_video_agree_on_the_channel():
    resolved = json.loads(call("yt_resolve", {"url": VID}).content[0].text)
    details = json.loads(call("yt_video", {"url": VID}).content[0].text)
    assert resolved["id"] == details["channel_id"]
    assert details["id"] == VID


@pytest.mark.network
def test_preview_says_when_it_stopped_short():
    answer = json.loads(call("yt_preview", {
        "channel": "https://www.youtube.com/@RickAstleyYT",
        "limit": 3}).content[0].text)
    assert answer["matched"] == 3
    # Without this an agent reports 3 videos as everything a job would fetch.
    assert answer["capped"] is True and answer["limit"] == 3


@pytest.mark.network
def test_transcript_omits_the_word_level_form_unless_asked():
    plain = json.loads(call("yt_transcript", {"url": VID}).content[0].text)
    assert plain["text"] and plain["words"] is None
    assert "include_words" in plain["words_omitted"]
    full = json.loads(call("yt_transcript",
                           {"url": VID, "include_words": True}).content[0].text)
    assert full["words"]
