# YouTube Video Downloader

Batch-downloads YouTube videos in **1080p** (priority) down to **720p** (floor) using **yt-dlp** and your own **cookies.txt**. Every video gets its own folder, with an info file and two timestamped transcripts inside.

## Setup

Point the script at **one folder**:

```
<your folder>/
├── links.txt       one YouTube link per line   (# = comment, blank = skipped)
└── cookies.txt     OPTIONAL — see "Cookies" below
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

## Speed

YouTube throttles any single connection far below your line rate. When **aria2c**
is installed the script hands downloads to it with 16 parallel connections — the
same technique IDM uses — and yt-dlp fetches DASH/HLS fragments 8 at a time.

Measured on this machine (line ceiling ≈ 1.8 MB/s):

| | Speed |
|---|---|
| Built-in downloader | ~0.47 MB/s |
| aria2c, 16 connections | **~1.9 MB/s** |

Install it once and the script picks it up automatically:

```bash
brew install aria2
```

Without aria2c everything still works, just at the slower built-in speed — the
startup banner tells you which one is in use.

## PO tokens (the 403 fix)

YouTube gates some videos behind a "Proof of Origin" token. Without one those
downloads fail with `HTTP Error 403` (aria2c reports the same thing as
`exited with code 22`).

The script handles this on its own: it tries the normal, fast path first, and
only if that 403s does it retry through the `mweb` client, which mints a token.
Once one video in a batch needs it, the rest go straight there — no wasted
attempts.

The token comes from the **bgutil** provider, which runs a small JS script.
**No browser window ever opens.** Install it once:

```bash
pip install bgutil-ytdlp-pot-provider
mkdir -p ~/.local/share/bgutil-pot && cd ~/.local/share/bgutil-pot
curl -sL https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/1.3.2.tar.gz | tar xz --strip-components=1
cd server && npm install && npx tsc
```

The script is found automatically at `~/.local/share/bgutil-pot/server/build/generate_once.js`;
set `BGUTIL_SCRIPT` to point somewhere else. Keep the pip package and the server
on the **same version**. The startup banner shows whether it was found.

> Avoid the browser-based providers (e.g. `yt-dlp-getpot-wpc`) — they open a
> Chrome window for every video that needs a token, and cannot run headless.

## Cookies

**Optional.** Public videos download fine without any `cookies.txt`, so you can
leave it out entirely. Add one only for videos that genuinely need an account:
private, members-only, or age-restricted.

Two things worth knowing if you do use cookies: they expire quickly (YouTube
rotates them, and a stale file causes confusing failures), and heavy downloading
on a logged-in account carries a real risk to that account. Skipping cookies
avoids both.

## Resume

Finished videos are indexed **by video id** at startup, so a re-run:

- skips anything already downloaded, with no network call;
- finds them in **either layout**, so switching `--channel` on or off still resumes;
- treats `?v=ABC` and `?v=ABC&pp=…` as the same video, so duplicate links in `links.txt` are downloaded once;
- retries anything that failed or was interrupted (yt-dlp continues its partial `.part` file).

Delete a video's folder if you want a genuinely fresh pull.

A stalled or dropped stream (`Did not get any data blocks`, a 5xx, a timeout) is
retried once on its own — one flaky moment should not cost you a video in a long
batch. If a download still fails after that, any media it left behind is renamed
`INCOMPLETE_…`: a half-finished file is the right size and plays, so without the
marker a video with no sound looks exactly like a good one. Marked files are
ignored by resume, so the next run downloads them properly.

## Requirements

System tools:

- **Python 3**
- **ffmpeg** — merges the 1080p video+audio streams and reads back the real quality
- **node** (or **deno** on macOS) — the JavaScript runtime yt-dlp needs to solve YouTube's "n" challenge
- **aria2c** — optional but strongly recommended; see [Speed](#speed)

Python packages:

```bash
pip install -r requirements.txt
```

`curl_cffi` is optional but recommended (`pip install "curl_cffi>=0.10,<0.16"`) — it lets yt-dlp impersonate a browser.

## Windows

Everything works on both macOS and Windows from the same file. Platform
differences are handled automatically, so the Mac path stays exactly as it is
(see `windows_flags()` and `sanitize()` in `downloader.py`, plus
`fix_yt_dlp_windows.md`):

1. **`n challenge solving failed`** — Windows (especially inside a venv, under a Python subprocess) fails to auto-detect Node, so the absolute path is passed explicitly via `--js-runtimes node:<path>`.
2. **`HTTP Error 403: Forbidden`** — see [PO tokens](#po-tokens-the-403-fix). Windows starts on the `mweb` client directly; macOS uses it as an automatic retry after a 403, so the fast path stays the default.
3. **Console hangs / "Bad file descriptor"** — output is streamed with `read1()` rather than a raw `os.read()` on the file descriptor.
4. **Reserved names** — Windows refuses `CON`, `NUL`, `AUX`, `COM1`…`LPT9` as file or folder names, matching on the part before the first dot. A video titled one of those gets an underscore on the stem (`aux.txt` → `aux_.txt`).
5. **260-character path limit** — name components are capped at 60 characters on Windows (150 on macOS), and the media file is named after its folder rather than left to yt-dlp's raw title, which keeps `<base>\<channel>\<video>\<file>` inside the limit.
6. **Console encoding** — output is forced to UTF-8. Video titles routinely contain characters that Windows' default cp1252 cannot encode, which otherwise raises `UnicodeEncodeError` as soon as output is piped to a file.
7. **Paths** — both `/` and `\` are handled throughout.

On Windows, install the requirements the same way, and make sure `node`,
`ffmpeg`, and (optionally) `aria2c` are on `PATH`.

## Tests

```bash
python3 test_downloader.py
```

74 tests, no network and no downloads. Every test runs twice — once through the
macOS code path and once through the Windows one — so you can check both from
either machine. They cover naming rules, video-id matching, the resume index in
both layouts, caption-track selection, and transcript formatting.

## Notes

- **Quality:** always 720p–1080p, H.264/mp4 preferred for editor compatibility. If a video has nothing in that range it is **not** downloaded at lower quality — the error goes to `videoinfo.txt` instead.
- **Live progress bar:** yt-dlp's native progress (percent, size, speed, ETA) shows while each video downloads.
- One failed video never stops the batch; a summary (downloaded / skipped / failed) prints at the end.
