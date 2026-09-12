#!/usr/bin/env python3
"""Gradio front-end for downloader.py — built for Google Colab.

This is a separate way in, not a change to how downloading works. The engine is
driven as a subprocess exactly as it is from a terminal, so the retry ladder, the
resume index, the folder layout and both logs behave identically; this file only
collects the arguments, streams the output back, and hands the files over.

Two entry points, both on the one URL Gradio prints:
  - the web UI
  - the HTTP API, callable with gradio_client — see api_help()

A WORD ABOUT COLAB. YouTube gates datacenter IPs, which is what Colab is, so
without a cookies.txt most videos fail with "Sign in to confirm you're not a
bot". downloader.py now recognises that and gives up on a video immediately
instead of working through every retry, but only a cookies.txt (or a home
connection) actually lifts it. Upload one in the UI.
"""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import zipfile

import gradio as gr

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADER = os.path.join(HERE, "downloader.py")
ON_COLAB = os.path.isdir("/content")
DEFAULT_OUT = "/content/downloads" if ON_COLAB else os.path.join(HERE, "downloads")

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


def _write(path, text):
    """Write one of the downloader's input files, or remove it if empty."""
    text = (text or "").strip()
    if not text:
        if os.path.exists(path):
            os.remove(path)
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    return True


def prepare(outdir, links, skips, cookies):
    """Lay out the folder downloader.py expects: links.txt, skip.txt, cookies.txt."""
    os.makedirs(outdir, exist_ok=True)
    if not _write(os.path.join(outdir, "links.txt"), links):
        raise gr.Error("No links given. Put one link per line — video links, "
                       "channel links, or both.")
    _write(os.path.join(outdir, "skip.txt"), skips)
    # A cookies.txt is uploaded as a temp file; it has to sit in the folder under
    # that exact name, because that is where base_cmd() looks for it.
    target = os.path.join(outdir, "cookies.txt")
    if cookies:
        shutil.copyfile(cookies, target)
    elif os.path.exists(target):
        os.remove(target)


def build_argv(outdir, channel, views, limit, min_h, max_h, subs, sub_lang,
               thumbnail, description, verbose):
    """The command line a person would have typed."""
    argv = [sys.executable, "-u", DOWNLOADER, outdir]
    if channel:
        argv.append("--channel")
    if views and int(views) > 0:
        argv += ["--views", str(int(views))]
    if limit and int(limit) > 0:
        argv += ["--limit", str(int(limit))]
    argv += ["--min-height", str(int(min_h)), "--max-height", str(int(max_h))]
    if not subs:
        argv.append("--no-subs")
    else:
        argv += ["--sub-lang", sub_lang or "en"]
    if not thumbnail:
        argv.append("--no-thumbnail")
    if not description:
        argv.append("--no-description")
    if verbose:
        argv.append("--verbose")
    return argv


def zip_results(outdir):
    """Zip the finished folders. Returns the path, or None if there is nothing."""
    folders = [n for n in sorted(os.listdir(outdir))
               if os.path.isdir(os.path.join(outdir, n))]
    if not folders:
        return None
    path = os.path.join(outdir, "downloads.zip")
    if os.path.exists(path):
        os.remove(path)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in folders:
            for root, _dirs, files in os.walk(os.path.join(outdir, folder)):
                for name in files:
                    full = os.path.join(root, name)
                    z.write(full, os.path.relpath(full, outdir))
    return path


def download(links, skips, cookies, outdir, channel, views, limit, min_h, max_h,
             subs, sub_lang, thumbnail, description, verbose, make_zip):
    """Run a batch, streaming the log. Yields (log, summary, zip, json file)."""
    outdir = (outdir or DEFAULT_OUT).strip()
    prepare(outdir, links, skips, cookies)
    argv = build_argv(outdir, channel, views, limit, min_h, max_h, subs,
                      sub_lang, thumbnail, description, verbose)

    lines = ["$ " + " ".join(argv[2:]), ""]
    yield "\n".join(lines), {"state": "running"}, None, None

    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace")
    current["proc"] = proc
    painted = 0.0
    try:
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
            # Repainting on every line makes a long batch crawl; a person cannot
            # read faster than this anyway.
            now = time.monotonic()
            if now - painted > 0.3:
                painted = now
                yield "\n".join(lines), {"state": "running"}, None, None
        proc.wait()
    finally:
        current["proc"] = None

    summary = {"state": "finished", "exit_code": proc.returncode, "folder": outdir}
    log_json = os.path.join(outdir, "download_log.json")
    if os.path.exists(log_json):
        try:
            with open(log_json, encoding="utf-8") as f:
                summary["run"] = json.load(f).get("run")
        except (OSError, json.JSONDecodeError):
            pass

    archive = None
    if make_zip:
        lines += ["", "Zipping..."]
        yield "\n".join(lines), summary, None, None
        archive = zip_results(outdir)
        if archive:
            size = os.path.getsize(archive) / (1024 * 1024)
            lines.append(f"Zipped -> {archive}  ({size:.1f} MB)")
        else:
            lines.append("Nothing to zip.")

    zip_out, json_out = servable(archive), servable(log_json)
    for path, got in ((archive, zip_out), (log_json, json_out)):
        if path and os.path.exists(path) and not got:
            lines.append(f"({os.path.basename(path)} is at {path} - outside the "
                         f"folders this app may serve, so open it from there.)")
    summary["served_from"] = ALLOWED_ROOTS
    yield "\n".join(lines), summary, zip_out, json_out


def stop():
    """Interrupt the running batch the way Ctrl+C would, so it still summarises."""
    proc = current.get("proc")
    if not proc or proc.poll() is not None:
        return "Nothing is running."
    proc.send_signal(signal.SIGINT)
    return "Sent Ctrl+C — finished videos are kept; it will print its summary."


def preview(channel_url, views, limit, skips):
    """What a channel link expands to, without downloading anything.

    Worth doing before a bulk run: it is one cheap listing request per 100
    videos and it tells you exactly what the criteria selected.
    """
    if not (channel_url or "").strip():
        raise gr.Error("Give a channel link to preview.")
    sys.path.insert(0, HERE)
    import downloader as d
    d.MIN_VIEWS = int(views or 0)
    d.LIMIT = int(limit or 0)
    d.SKIP_IDS = {v for v in (d.video_id_from_url(l) or
                              (l if d.BARE_ID_RE.match(l) else None)
                              for l in (skips or "").split())
                  if v}
    url = channel_url.strip().splitlines()[0].strip()
    if not d.is_channel_url(url):
        return {"error": "That is not a channel link.", "url": url}
    found, name, err = d.list_channel_videos(url)
    return {"channel": name or None, "tab_read": d.channel_videos_url(url),
            "error": err, "matched": len(found), "videos": found}


def api_help(url):
    """The snippet to drive this from code."""
    return (
        "from gradio_client import Client\n"
        f'c = Client("{url}")\n'
        "\n"
        "# preview what a channel would give you\n"
        'print(c.predict("https://www.youtube.com/@SomeChannel", 1000, 3, "",\n'
        '                api_name="/preview"))\n'
        "\n"
        "# run a batch (same order as the UI fields)\n"
        'print(c.predict("https://www.youtube.com/@SomeChannel", "", None,\n'
        f'                "{DEFAULT_OUT}", True, 1000, 3, 720, 1080,\n'
        '                True, "en", True, True, False, False,\n'
        '                api_name="/download"))\n'
    )


def build_ui():
    with gr.Blocks(title="YouTube Downloader") as demo:
        gr.Markdown(
            "# YouTube Downloader\n"
            "Channel links and video links, mixed. `--views` / `--limit` apply to "
            "channel links only.\n\n"
            "**On Colab, upload a `cookies.txt`** — YouTube gates datacenter IPs, "
            "and without one most videos fail the bot check."
        )
        with gr.Row():
            with gr.Column(scale=2):
                links = gr.Textbox(
                    label="links.txt — one per line",
                    lines=8,
                    placeholder="https://www.youtube.com/@SomeChannel\n"
                                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                )
                with gr.Row():
                    views = gr.Number(label="Min views (0 = any)", value=0, precision=0)
                    limit = gr.Number(label="Limit per channel (0 = all)", value=0,
                                      precision=0)
                with gr.Row():
                    min_h = gr.Number(label="Min height", value=720, precision=0)
                    max_h = gr.Number(label="Max height", value=1080, precision=0)
                channel = gr.Checkbox(label="Group into <Channel>/ folders (--channel)",
                                      value=True)
                with gr.Accordion("Extras, skip list, cookies", open=False):
                    skips = gr.Textbox(
                        label="skip.txt — links or bare ids never to download",
                        lines=4)
                    cookies = gr.File(label="cookies.txt (Netscape format)",
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
            outputs=[log, summary, archive, jsonlog],
            api_name="download",
        )
        halt.click(stop, outputs=[log], api_name="stop", cancels=[run])
        peek.click(preview, inputs=[links, views, limit, skips], outputs=[summary],
                   api_name="preview")
    return demo


def launch(share=True, **kwargs):
    """Start the UI and the API, and print the snippet to drive it from code.

    Returns without blocking, so the API help below actually gets printed: a
    plain launch() blocks the thread outside a notebook, which would make every
    line after it dead code. __main__ blocks explicitly instead.
    """
    for tool in ("yt-dlp", "ffmpeg"):
        if not shutil.which(tool):
            print(f"WARNING: {tool} is not on PATH - run the setup cell first.")
    demo = build_ui()
    demo.queue()
    kwargs.setdefault("allowed_paths", ALLOWED_ROOTS)
    kwargs.setdefault("prevent_thread_lock", True)
    demo.launch(share=share, **kwargs)
    url = (getattr(demo, "share_url", None) or getattr(demo, "local_url", "") or "")
    print("\n" + "=" * 68)
    print("  API - drive this from code")
    print("=" * 68)
    print(api_help(url.rstrip("/") if url else "<the URL above>"), flush=True)
    return demo


if __name__ == "__main__":
    launch(share="--no-share" not in sys.argv).block_thread()
