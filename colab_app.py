#!/usr/bin/env python3
"""Gradio front-end for downloader.py - built for Google Colab.

Another way in, not a change to how downloading works: the engine is driven as a
subprocess exactly as it is from a terminal, so the retry ladder, the resume
index, the folder layout and both logs behave identically.

One URL carries three things:
  - the web UI
  - the REST API on /api (the same routes api.py serves on its own; see api.py)
  - Gradio's own predict API, for gradio_client

The shared machinery - writing links.txt, building the command line, reading the
run's JSON log - lives in api.py, so the UI and the REST API cannot drift apart.

COOKIES are optional. Public videos download on Colab without an account. If
YouTube ever does gate the runtime ("Sign in to confirm you're not a bot"),
downloader.py recognises it, gives up on that video at once instead of working
through every retry, and says so in the summary - upload a cookies.txt then.
"""

import json
import os
import signal
import subprocess
import sys
import time

import gradio as gr

import api

HERE = api.HERE
DEFAULT_OUT = api.DEFAULT_OUT
ON_COLAB = api.ON_COLAB

# Gradio hands a file to the browser only if it sits under one of these, so they
# are passed to launch() as allowed_paths. On Colab "/content" covers both the
# default folder and a mounted Drive at /content/drive/MyDrive/...
ALLOWED_ROOTS = [HERE, DEFAULT_OUT] + (["/content"] if ON_COLAB else [])

# The running download, so Stop can reach it.
current = {"proc": None}


def servable(path):
    """The path if Gradio may serve it, else None.

    Returning a file from outside ALLOWED_ROOTS raises inside Gradio and takes
    the whole response down with it - log and summary included - even though the
    download itself finished. Better to check, and say where the file is.
    """
    if not path or not os.path.exists(path):
        return None
    real = os.path.realpath(path)
    for root in ALLOWED_ROOTS:
        root = os.path.realpath(root)
        if real == root or real.startswith(root + os.sep):
            return path
    return None


def download(links, skips, cookies, outdir, channel, views, limit, min_h, max_h,
             subs, sub_lang, thumbnail, description, verbose, make_zip):
    """Run a batch, streaming the log. Yields (log, summary, zip, json file)."""
    try:
        opts = api.normalize_request({
            "links": links, "skip": skips, "outdir": outdir or DEFAULT_OUT,
            "channel": channel, "views": views, "limit": limit,
            "min_height": min_h, "max_height": max_h, "subs": subs,
            "sub_lang": sub_lang, "thumbnail": thumbnail,
            "description": description, "verbose": verbose, "zip": make_zip,
        })
    except ValueError as e:
        raise gr.Error(str(e))

    outdir = opts["outdir"]
    try:
        api.write_inputs(outdir, opts["links"], opts["skip"], cookies)
    except OSError as e:
        raise gr.Error(f"could not prepare {outdir}: {e}")
    argv = api.build_argv(outdir, opts)

    lines = ["$ " + " ".join(argv[2:]), ""]
    yield "\n".join(lines), {"state": "running"}, None, None

    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1,
                                encoding="utf-8", errors="replace")
    except OSError as e:
        raise gr.Error(f"could not start the downloader: {e}")
    current["proc"] = proc
    painted = 0.0
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            # Repainting on every line makes a long batch crawl, and nobody reads
            # faster than this anyway.
            now = time.monotonic()
            if now - painted > 0.3:
                painted = now
                yield "\n".join(lines), {"state": "running"}, None, None
        proc.wait()
    finally:
        current["proc"] = None

    summary = {"state": "finished", "exit_code": proc.returncode,
               "folder": outdir, "failures": []}
    log_json = os.path.join(outdir, "download_log.json")
    if os.path.exists(log_json):
        try:
            with open(log_json, encoding="utf-8") as f:
                logged = json.load(f)
            summary["run"] = logged.get("run")
            summary["failures"] = api.failures_in(logged)
        except (OSError, json.JSONDecodeError) as e:
            summary["log_error"] = f"could not read download_log.json: {e}"

    if summary["failures"]:
        lines.append("")
        lines.append(f"{len(summary['failures'])} did not come down:")
        for bad in summary["failures"]:
            lines.append(f"  [{bad['status']}] {bad.get('title') or bad['url']}")
            lines.append(f"      {bad.get('error') or 'no error recorded'}")

    archive = None
    if opts["zip"]:
        lines += ["", "Zipping..."]
        yield "\n".join(lines), summary, None, None
        archive = api.zip_results(outdir)
        if archive:
            lines.append(f"Zipped -> {archive}  "
                         f"({os.path.getsize(archive) / (1024 * 1024):.1f} MB)")
        else:
            lines.append("Nothing to zip.")

    zip_out, json_out = servable(archive), servable(log_json)
    for path, got in ((archive, zip_out), (log_json, json_out)):
        if path and os.path.exists(path) and not got:
            lines.append(f"({os.path.basename(path)} is at {path} - outside the "
                         f"folders this app may serve, so open it from there.)")
    yield "\n".join(lines), summary, zip_out, json_out


def stop():
    """Interrupt the running batch the way Ctrl+C would, so it still summarises."""
    proc = current.get("proc")
    if not proc or proc.poll() is not None:
        return "Nothing is running."
    proc.send_signal(signal.SIGINT)
    return "Sent Ctrl+C - finished videos are kept; it will print its summary."


def preview(channel_url, views, limit, skips):
    """What a channel link expands to, without downloading anything."""
    try:
        return api.preview_channel(channel_url, views, limit, skips)
    except ValueError as e:
        raise gr.Error(str(e))


def build_ui():
    with gr.Blocks(title="YouTube Downloader") as demo:
        gr.Markdown(
            "# YouTube Downloader\n"
            "Channel links and video links, mixed freely. **Min views** and "
            "**limit** apply to channel links only.\n\n"
            "Everything here is on the REST API too - the base URL, the key and "
            "some curl to copy are printed under the notebook cell. "
            "`cookies.txt` is optional: add one only for private / members-only "
            "videos, or if YouTube starts asking this runtime to confirm it is "
            "not a bot."
        )
        with gr.Row():
            with gr.Column(scale=2):
                links = gr.Textbox(
                    label="links.txt - one per line", lines=8,
                    placeholder="https://www.youtube.com/@SomeChannel\n"
                                "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
                with gr.Row():
                    views = gr.Number(label="Min views (0 = any)", value=0, precision=0)
                    limit = gr.Number(label="Limit per channel (0 = all)", value=0,
                                      precision=0)
                with gr.Row():
                    min_h = gr.Number(label="Min height", value=720, precision=0)
                    max_h = gr.Number(label="Max height", value=1080, precision=0)
                channel = gr.Checkbox(label="Group into <Channel>/ folders", value=True)
                with gr.Accordion("Extras, skip list, cookies", open=False):
                    skips = gr.Textbox(
                        label="skip.txt - links or bare ids never to download", lines=4)
                    cookies = gr.File(label="cookies.txt (optional, Netscape format)",
                                      type="filepath", file_types=[".txt"])
                    outdir = gr.Textbox(label="Output folder", value=DEFAULT_OUT)
                    with gr.Row():
                        subs = gr.Checkbox(label="Transcripts", value=True)
                        sub_lang = gr.Textbox(label="Sub language", value="en",
                                              scale=0, min_width=90)
                    with gr.Row():
                        thumbnail = gr.Checkbox(label="Thumbnail", value=True)
                        description = gr.Checkbox(label="Description", value=True)
                    with gr.Row():
                        verbose = gr.Checkbox(label="Verbose yt-dlp", value=False)
                        make_zip = gr.Checkbox(label="Zip when done", value=False)
                with gr.Row():
                    go = gr.Button("Download", variant="primary", scale=2)
                    halt = gr.Button("Stop")
                    peek = gr.Button("Preview channel")
            with gr.Column(scale=3):
                log = gr.Textbox(label="Output", lines=26, max_lines=26)
                summary = gr.JSON(label="Result")
                with gr.Row():
                    archive = gr.File(label="downloads.zip")
                    jsonlog = gr.File(label="download_log.json")

        run = go.click(
            download,
            inputs=[links, skips, cookies, outdir, channel, views, limit, min_h,
                    max_h, subs, sub_lang, thumbnail, description, verbose, make_zip],
            outputs=[log, summary, archive, jsonlog], api_name="download")
        halt.click(stop, outputs=[log], api_name="stop", cancels=[run])
        peek.click(preview, inputs=[links, views, limit, skips], outputs=[summary],
                   api_name="preview")
    return demo


def gradio_client_help(url):
    """The snippet for Gradio's own predict API, for people already using it."""
    return (
        "from gradio_client import Client\n"
        f'c = Client("{url}")\n'
        'c.predict("https://www.youtube.com/@SomeChannel", 1000, 3, "",\n'
        '          api_name="/preview")\n'
        'c.predict("https://www.youtube.com/@SomeChannel", "", None,\n'
        f'          "{DEFAULT_OUT}", True, 1000, 3, 720, 1080,\n'
        '          True, "en", True, True, False, False, api_name="/download")\n'
    )


def launch(share=True, key=None, auth=True, **kwargs):
    """Start the UI, mount the REST API on the same URL, and print how to use it.

    Returns without blocking, so everything printed below actually runs: a plain
    launch() blocks the thread outside a notebook, which would make it all dead
    code. __main__ blocks explicitly instead.
    """
    missing = [t for t, p in api.tools_present().items() if not p and t != "aria2c"]
    if missing:
        print(f"WARNING: not on PATH - run the setup cell first: {', '.join(missing)}")

    api.configure_key(key, enabled=auth)
    demo = build_ui()
    demo.queue()
    kwargs.setdefault("allowed_paths", ALLOWED_ROOTS)
    kwargs.setdefault("prevent_thread_lock", True)
    # Without this, anything that goes wrong reaches an API caller as "the
    # upstream Gradio app has raised an exception but has not enabled verbose
    # error reporting", which says nothing about what actually broke.
    kwargs.setdefault("show_error", True)
    demo.launch(share=share, **kwargs)

    # Gradio's app only exists once it has launched; its own routes live under
    # /gradio_api, so /api is free for ours.
    api.attach(demo.app)

    url = (getattr(demo, "share_url", None) or getattr(demo, "local_url", "") or "")
    url = url.rstrip("/")
    print("\n" + "=" * 70)
    print("  URL - the UI, the REST API and gradio_client all live here")
    print("=" * 70)
    print(f"\n    {url or '<see the link above>'}\n")
    print(api.banner(url))
    print("=" * 70)
    print("  ...or with gradio_client")
    print("=" * 70)
    print(gradio_client_help(url or "<the URL above>"), flush=True)
    return demo


if __name__ == "__main__":
    launch(share="--no-share" not in sys.argv,
           auth="--no-key" not in sys.argv).block_thread()
