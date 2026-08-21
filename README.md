# YouTube Video Downloader

Batch-downloads YouTube videos in **1080p** (priority) down to **720p** (floor) using **yt-dlp** and your own **cookies.txt**. Every video gets its own folder, with an info file and two timestamped transcripts inside.

## Setup

Point the script at **one folder** that holds both of these:

```
<your folder>/
├── cookies.txt     your cookies, Netscape format, exported from the browser
└── links.txt       one YouTube link per line   (# = comment, blank = skipped)
```

Videos are downloaded into that same folder.

## Run

```bash
python3 downloader.py "/path/to/your folder"
```

Group the videos by channel instead:

```bash
python3 downloader.py "/path/to/your folder" --channel
```

| Flag | What it does | Default |
|------|--------------|---------|
| `--channel` | Group videos into `<Channel Name>/` folders | off (flat) |
| `--min-height N` | Quality floor — never download below this | `720` |
| `--max-height N` | Quality ceiling | `1080` |
| `--no-subs` | Skip the transcripts | transcripts on |
| `--sub-lang CODE` | Transcript language | `en` |

## Output

Default layout:

```
<your folder>/
└── <Video Title>/
    ├── <Video Title>.mp4
    ├── videoinfo.txt
    ├── trans_<Video Title>.txt
    └── words_<Video Title>.txt
```

With `--channel`:

```
<your folder>/
├── <Channel Name>/
│   └── <Video Title>/
│       ├── <Video Title>.mp4
│       ├── videoinfo.txt
│       ├── trans_<Video Title>.txt
│       └── words_<Video Title>.txt
└── <Another Channel>/
    └── ...
```

`videoinfo.txt` (success):
```
Title:   My Video
Link:    https://www.youtube.com/watch?v=xxxx
Quality: 1080p
Status:  OK
Transcript: trans_My Video.txt
Words:      words_My Video.txt
```

`videoinfo.txt` (failure — e.g. no 720p+ stream, private/deleted video):
```
Title:   My Video
Link:    https://www.youtube.com/watch?v=xxxx
Quality: FAILED
Status:  ERROR
Error:   Requested format not available
```

## Transcripts

Two `.txt` files are built next to each video from YouTube's auto-caption, in the same format as `../Transcript with timestamps/transcribe.py` (its `default` and `oneword` modes):

- `trans_<Video Title>.txt` — grouped, wrapped lines: `[hh:mm:ss] text`
- `words_<Video Title>.txt` — one word per line: `[hh:mm:ss.mmm] word`

**Backfill:** if a video is already downloaded but has no `trans_*.txt`, re-running builds the transcripts for it without re-downloading the video.

Note: a few videos publish closed-caption tracks that are timed per *phrase* rather than per *word*. For those, `words_*.txt` carries phrases — the finer timing simply isn't in the source.

## Resume

Finished videos are indexed **by video id** at startup, so a re-run:

- skips anything already downloaded, with no network call;
- finds them in **either layout**, so switching `--channel` on or off still resumes;
- treats `?v=ABC` and `?v=ABC&pp=…` as the same video, so duplicate links in `links.txt` are downloaded once;
- retries anything that failed or was interrupted (yt-dlp continues its partial `.part` file).

Delete a video's folder if you want a genuinely fresh pull.

## Requirements

System tools:

- **Python 3**
- **ffmpeg** — merges the 1080p video+audio streams and reads back the real quality
- **node** (or **deno** on macOS) — the JavaScript runtime yt-dlp needs to solve YouTube's "n" challenge

Python packages:

```bash
pip install -r requirements.txt
```

`curl_cffi` is optional but recommended (`pip install "curl_cffi>=0.10,<0.16"`) — it lets yt-dlp impersonate a browser.

## Windows

Windows needs extra yt-dlp flags that macOS does not. The script applies them **automatically when it runs on Windows** and never on macOS, so the Mac path stays as-is (see `windows_flags()` in `downloader.py`, and `fix_yt_dlp_windows.md`):

1. **`n challenge solving failed`** — Windows (especially inside a venv, under a Python subprocess) fails to auto-detect Node, so the absolute path is passed explicitly via `--js-runtimes node:<path>`.
2. **`HTTP Error 403: Forbidden`** — some videos are gated behind a "Proof of Origin" token. The `mweb` client plus the `yt-dlp-getpot-wpc` package mints one. Windows starts there; macOS uses it as an **automatic retry** after a 403, so the fast path stays default.
3. **Console hangs / "Bad file descriptor"** — output is streamed with `read1()` rather than a raw `os.read()` on the file descriptor, which is safe on both platforms.
4. **Paths** — folder names are stripped of both `/` and `\`.

## Notes

- **Quality:** always 720p–1080p, H.264/mp4 preferred for editor compatibility. If a video has nothing in that range it is **not** downloaded at lower quality — the error goes to `videoinfo.txt` instead.
- **Live progress bar:** yt-dlp's native progress (percent, size, speed, ETA) shows while each video downloads.
- One failed video never stops the batch; a summary (downloaded / skipped / failed) prints at the end.
