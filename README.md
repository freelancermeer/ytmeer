# YouTube Video Downloader

Batch-downloads YouTube videos in **1080p** (priority) down to **720p** (floor) using **yt-dlp**. Every video gets its own folder, with an info file and two timestamped transcripts inside. Runs on macOS and Windows; no cookies needed for public videos.

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
    └── words_<Video Title>.txt        (or words_not_found_<Video Title>.txt)
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

**Both files are always written** when the video has any English caption at all — and when word timings do not exist, the second one is `words_not_found_<Video Title>.txt`, which says why. See [When there is no words_ file](#when-there-is-no-words_-file).

**Backfill:** if a video is already downloaded but has no `trans_*.txt`, re-running builds the transcripts for it without re-downloading the video.

### When there is no words_ file

`words_*.txt` needs a timestamp per word, and only YouTube's **auto-generated**
(ASR) captions have those. A minority of videos — typically TV news clips — ship
the broadcast **closed-caption** tracks (`CC1`, `DTVCC1`) instead, which are
timed one *phrase* at a time. For those, the folder gets
**`words_not_found_<Video Title>.txt`** explaining why, and `trans_*.txt` is
written as usual from the phrase text.

No timings are ever invented. Spreading a phrase's words across its span was
measured against real word timings: half the words land within 0.1 s, but the
tail runs to 2.5 s out. A file that looks precise while being approximate is
worse than an honest gap, so the marker is written instead.

**This is a limit of the source, not of the script.** For the two affected videos
in a 20-video batch, verified four ways:

| Check | Result |
|---|---|
| yt-dlp metadata, every working player client | 0 auto-captions |
| An independent captions API | same phrase-timed text |
| The original version of this script | wrote no words file at all |
| **YouTube's own timedtext API, asking for `kind=asr`** | **HTTP 404 — the track does not exist** |

The marker is a last resort, not a first verdict. Before writing it the script
walks **every** English track the video has — auto-captions first, then broadcast
ones — and stops at the first with word timings. If the metadata shows no
auto-caption it asks a second player client, which sometimes sees tracks the
first does not, and it copes with YouTube listing an auto track that then serves
nothing. Only when no track anywhere carries word timings does the marker
appear, and `trans_*.txt` is still written from the best track found.

Measured over that batch: **18 with word timings, 2 without.**

To get word timings for one of those, transcribe the downloaded `.mp4` locally —
`../Transcript with timestamps/transcribe.py oneword` runs Whisper and produces
the same `[hh:mm:ss.mmm] word` format.

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

See [Retrying](#retrying) for what happens when a download does fail.

Delete a video's folder if you want a genuinely fresh pull.

### Retrying

Most YouTube failures pass on their own — a throttled stream, a token gone
stale, a fragment that 403s once. So a video is never given up on after one go:

1. Each video is tried by every **route** in turn — the default client, the
   PO-token client, then the PO-token client on a single connection.
2. That whole set repeats up to **3 times**, waiting 10s then 30s in between,
   because some failures only clear with a pause.
3. After the batch, a **final sweep** retries everything that still failed, by
   which point a bad stretch has usually passed.

Private, deleted, and unavailable videos are the exception: nothing about those
changes on a retry, so they fail immediately and are listed separately in the
summary rather than burning attempts.

If a download still fails after all that, any media it left behind is renamed
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

108 tests, no network and no downloads. Every test runs twice — once through the
macOS code path and once through the Windows one — so you can check both from
either machine. They cover naming rules, video-id matching, the resume index in
both layouts, caption-track selection, and transcript formatting.

## Notes

- **Quality:** always 720p–1080p, H.264/mp4 preferred for editor compatibility. If a video has nothing in that range it is **not** downloaded at lower quality — the error goes to `videoinfo.txt` instead.
- **Live progress bar:** yt-dlp's native progress (percent, size, speed, ETA) shows while each video downloads.
- One failed video never stops the batch; a summary (downloaded / skipped / failed) prints at the end.
