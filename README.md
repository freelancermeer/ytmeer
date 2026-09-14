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
| `--views N` | Channel links only: skip videos under N views | no floor |
| `--limit N` | Channel links only: stop after the newest N matches | whole channel |
| `--no-subs` | Skip the transcripts | transcripts on |
| `--sub-lang CODE` | Transcript language | `en` |
| `--no-thumbnail` | Skip the thumbnail | thumbnail on |
| `--no-description` | Skip the description | description on |
| `-v`, `--verbose` | Show yt-dlp's full output instead of the bar | quiet |

## Channel links

`links.txt` takes **channel links as well as video links**, mixed in any order. A
channel link is expanded into its videos, newest first, before anything is
downloaded:

```
https://www.youtube.com/@SomeChannel
https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

```bash
python3 downloader.py "/path/to/your folder" --channel --views 1000 --limit 3
```

That reads `@SomeChannel`, keeps the videos with **at least 1000 views**, takes
the **newest 3** of them, and downloads those — plus the plain video link, which
passes straight through untouched.

| | |
|---|---|
| `--views N` | Keep only videos with at least N views. No `--views` keeps every video. |
| `--limit N` | Stop after the newest N videos that match. **No `--limit` means the whole channel.** |

Details worth knowing:

- **Long videos only.** YouTube files long-form videos, Shorts and streams under
  separate tabs, and the channel's `/videos` tab is the one read — so Shorts are
  never picked up. Point a link straight at `/shorts` or `/streams` if you do
  want one of those instead; an explicit tab is taken as given.
- **Any channel link form works** — `@handle`, `/c/Name`, `/channel/UC…`,
  `/user/Name`, with or without a `/videos` on the end.
- `--limit` counts **per channel**, not per run.
- `--limit` counts videos that match the criteria, whether or not you already
  have them. So `--limit 3` means the same 3 videos every run: the ones already
  downloaded are then skipped by [Resume](#resume) rather than replaced by older
  ones. Use `skip.txt` to move the window on.
- A high `--views` on a large channel has to read every page before it can know
  the answer, so it prints its progress as it scans. Ctrl+C stops it cleanly.
- Videos still counting down (premieres) or currently live are left out, and so
  is any video YouTube listed without a view count when `--views` is set — that
  one is reported, never dropped silently.
- Live-streamed and Shorts links you paste **directly** are still downloaded as
  normal; the tab rule only decides what a *channel* link expands to.

## Skip list

Put a `skip.txt` next to `links.txt` with the videos you want left alone — the
ones you have already made something from:

```
# already made videos on these
https://www.youtube.com/watch?v=8e6xIpf7qpk
dQw4w9WgXcQ
```

One link per line, or just the bare 11-character id. Blank lines and `#`
comments are ignored, exactly as in `links.txt`.

Those videos are never downloaded, whether they arrived from a channel link or
were listed directly. A skipped video **does not use up a `--limit` slot**: ask
for the newest 3 and put the newest one in `skip.txt`, and you get the next 3
down instead of 2. The run reports how many it left out.

## Output

Default layout:

```
<your folder>/
└── <Video Title>/
    ├── <Video Title>.mp4
    ├── <Video Title>.jpg              the thumbnail
    ├── videoinfo.txt
    ├── description_<Video Title>.txt
    ├── trans_<Video Title>.txt
    └── words_<Video Title>.txt        (or words_not_found_<Video Title>.txt)
```

With `--channel`:

```
<your folder>/
├── <Channel Name>/
│   └── <Video Title>/
│       ├── <Video Title>.mp4
│       ├── <Video Title>.jpg
│       ├── videoinfo.txt
│       ├── description_<Video Title>.txt
│       ├── trans_<Video Title>.txt
│       └── words_<Video Title>.txt
└── <Another Channel>/
    └── ...
```

Everything works the same in both layouts — downloading, resume, backfill and
the extras — so `--channel` is purely about where files land.

`videoinfo.txt` (success):
```
Title:   My Video
Link:    https://www.youtube.com/watch?v=xxxx
Channel: Some Channel
Views:   8,920
Subscribers: 7,550,000
Uploaded: 13 May 2026, 02:45:02 AM PKT
Category: News & Politics
Quality: 1080p
Status:  OK
Transcript: trans_My Video.txt
Words:      words_My Video.txt
Thumbnail:  My Video.jpg
Description: description_My Video.txt
```

Channel, views, subscribers, upload time and category all come from metadata
that is fetched anyway, so they cost nothing. **Uploaded** is the exact moment
in Pakistan time (UTC+5) on a 12-hour clock; when YouTube only reports the day
it says `12 May 2026 (time NA)` rather than implying a time it does not know.

Every one of these fields is always written, reading **NA** when YouTube does
not report it — some channels hide their subscriber count — so anything reading
the file finds the same shape each time. Videos downloaded before these existed
pick the lines up on the next run.

## Thumbnail and description

Both are saved by default, next to the video.

- **`<Video Title>.jpg`** — the thumbnail, converted from YouTube's `.webp` so it
  opens anywhere.
- **`description_<Video Title>.txt`** — the video's description, with the channel,
  upload date, duration, views and link on top. It comes from metadata already
  fetched, so it costs no extra request.

Turn either off with `--no-thumbnail` / `--no-description`.

**Backfill:** videos downloaded before these existed pick them up on the next
run, without the video being fetched again.

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

## What you see while it runs

yt-dlp is loud — many lines per video, plus a progress bar that repaints
constantly. All of that goes to a log file instead. The terminal shows one
rewriting bar for the video in hand:

```
[3/22] [############........]  61.4%   1.4 MB/s  Kash Patel Throws Tantrum Aft…
```

and one line per video once it is done:

```
[1/3] OK   1080p   46.7 MB    1m 22s   1.3 MB/s  [TWjd]  Chris Van Hollen presses…
[2/3] FAIL  [youtube] zzzzzDEADvi: Video unavailable
[3/3] SKIP  already downloaded   Chris Van Hollen presses Kash Patel during…
```

`[TWjd]` is what was saved: **T**ranscript, **W**ords, **j**pg, **d**escription.

The run ends with the totals:

```
  Summary: 1 downloaded, 1 skipped (already done), 1 failed  (of 3 links).
  Downloaded 1 video(s), 46.7 MB in 1m 22s of downloading  (1.4 MB/s average)
  Total run time: 2m 09s
```

"in 1m 22s of downloading" is the time actually spent moving bytes, so the rate
beside it is the rate you got — waits between retries are not counted against
it. **Total run time** is the whole wall clock, which also covers metadata
lookups, PO tokens, transcripts and thumbnails.

### Logs

Two files are written into the folder you point at:

- **`download_log.txt`** — everything yt-dlp said, for when a failure needs
  looking into.
- **`download_log.json`** — the run's totals, then a record per video: url,
  title, status, quality, bytes, seconds, folder, the files written, and the
  error if there was one. Easy to read from another script.
- **`download_log.jsonl`** — the same records, one line each, written the moment
  a video is done rather than when the run ends. This is what the API's
  `/videos` endpoint reads.

Redirecting the output to a file gives you the per-video lines and nothing else
— the bar is only drawn when there is a terminal to rewrite. `--verbose` puts
yt-dlp's raw output back on screen.

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

Delete a video's folder if you want a genuinely fresh pull. To stop a video
being downloaded at all — rather than merely recognising that it already was —
list it in [`skip.txt`](#skip-list).

### Retrying

Most YouTube failures pass on their own — a throttled stream, a token gone
stale, a fragment that 403s once. So a video is never given up on after one go:

1. The **metadata lookup** gets up to 3 goes, 5s apart, with the last one
   through the PO-token client. Everything about a video hangs off this call, so
   one rate-limited request must not be the end of it.
2. The **download** is then tried by every route in turn — the default client,
   the PO-token client, then the PO-token client on a single connection.
3. That whole set repeats up to **3 times**, waiting 10s then 30s in between,
   because some failures only clear with a pause.
4. After the batch, a **final sweep** retries everything that still failed, by
   which point a bad stretch has usually passed.

Private, deleted, and unavailable videos are the exception: nothing about those
changes on a retry, so they fail immediately and are listed separately in the
summary rather than burning attempts. Each keeps its own folder and
`videoinfo.txt` naming the reason, so two dead links never overwrite each
other's record.

### When something goes wrong

Nothing in a batch takes the whole run down:

| Situation | What happens |
|---|---|
| `yt-dlp` or `ffmpeg` missing | Checked at startup — one clear message with the install command, before anything downloads |
| Directory or `links.txt` missing | Named directly, and the run stops there |
| A folder cannot be created | That video fails with the reason; the batch continues |
| A video is private or deleted | Failed immediately, listed under "Unavailable" |
| A video has no English captions | `words_not_found_…` explains it; the video and everything else still download |
| **Ctrl+C** | Stops cleanly with a summary — finished videos are on disk, and the next run resumes from there |

Comments (`#`) and blank lines in `links.txt` are skipped, and a link repeated
with a different tracking suffix is downloaded once.

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
7. **Paths** — both `/` and `\` are handled throughout, and the length budget covers the longest name the tool writes (`words_not_found_<title>.txt`), not just the video.
8. **Terminal output is plain ASCII** — a console that is not in UTF-8 mode would otherwise render dashes and ellipses as mojibake.

On Windows, install the requirements the same way, and make sure `node`,
`ffmpeg`, and (optionally) `aria2c` are on `PATH`.

## API

A small REST API so other code can drive the downloader: start a batch, then
collect each video the moment it finishes while the rest keep downloading.

```bash
python3 api.py                                  # http://127.0.0.1:8000
python3 api.py --port 9000 --outdir ~/Videos/YT
python3 api.py --share                          # also a public URL, through Gradio
```

It prints the URL and the key. Every endpoint, with a form to try it, is on one
page: **`/api/docs`** (click *Authorize* and paste the key). `--share` needs
`pip install gradio`; it serves the same API and docs on a public `gradio.live`
link as well - nothing else, no UI.

| | |
|---|---|
| `GET /api/ping` | alive, and `busy` while a job runs (no key) |
| `GET /api/health` | tools found, default folder, jobs |
| `GET /api/preview?channel=URL&views=1000&limit=3` | the newest videos a channel link would download |
| `POST /api/jobs` | start downloading, returns `job_id` |
| `GET /api/jobs` | all jobs |
| `GET /api/jobs/{id}` | state, progress, summary, failures |
| `GET /api/jobs/{id}/videos?since=N` | finished videos, as they finish |
| `GET /api/jobs/{id}/files` | every downloadable file in the job's folder |
| `GET /api/jobs/{id}/files/{path}` | download one; Range works, so big videos can resume |
| `POST /api/jobs/{id}/stop` | Ctrl+C it; finished videos stay |
| `DELETE /api/jobs/{id}` | forget a finished job |

Send the key as an `X-API-Key` header. A generated key is saved in `.api_key`, so
a restart keeps it; `--new-key` replaces it.

`POST /api/jobs` takes `links` (a list, or one per line: video links, channel
links or both) plus any of the command-line options: `views`, `limit`, `channel`,
`min_height`, `max_height`, `subs`, `sub_lang`, `thumbnail`, `description`,
`verbose`, `outdir`, `skip`. An unknown field is refused, so a typo fails instead
of quietly fetching a whole channel. `outdir` is a folder inside the download
folder, such as `"batch1"`; anything outside it is refused. `skip` replaces the folder's `skip.txt`; leave it out to keep the
one already there. The links go straight to the downloader, so a `links.txt` you
keep in that folder is left alone.

Collecting videos as they finish:

```python
import requests, time

API, H = "http://127.0.0.1:8000/api", {"X-API-Key": "your-key"}

def call(method, path, **kw):
    r = requests.request(method, API + path, headers=H, timeout=60, **kw)
    if not r.ok:
        raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
    return r.json()

job = call("POST", "/jobs", json={"links": ["https://www.youtube.com/@SomeChannel"],
                                  "views": 1000, "limit": 3})
since = 0
while True:
    page = call("GET", f"/jobs/{job['job_id']}/videos", params={"since": since})
    for v in page["videos"]:
        if v["status"] in ("ok", "skipped"):      # skipped = already on disk
            analyse(v["files"]["video"], v["files"]["transcript"])
    since = page["next"]
    if page["done"]:
        break
    time.sleep(5)

job = call("GET", f"/jobs/{job['job_id']}")
print(job["state"], job["error"], job["failures"])
```

A video is handed over once its folder is complete, with absolute paths in
`files`: `video`, `transcript`, `words`, `thumbnail`, `description`, `info`
(`None` when absent) - and the same files in `downloads`, as API paths that fetch
them from anywhere: `requests.get(API + v["downloads"]["video"], headers=H,
stream=True)`. Only files inside a video folder are served - never `cookies.txt`,
the logs, or anything still being written - and nothing outside the job's folder
is reachable. A failed one has `status` and `error` and no files - and
can come back later as `ok`, because a run retries its failures once at the end.
The job's `failures` lists only what finally did not come down.

A video's `status` is `ok`, `skipped` (already on disk), `failed` (worth another
go), `unavailable` (private, deleted - gone for good), or `blocked`: YouTube refused
the server's IP ("confirm you're not a bot"), which says nothing about the video.
When that happens - to a video or to a channel listing - the job has `"blocked": true`:
give the server a cookies.txt, or run it from another IP. Channel links that could not
be read are `channel_failed`, and count in the summary's `failed`. A video with no
format between `min_height` and `max_height` is `failed` but not retried in that run:
only another height range can change it.

A job ends `finished`, `stopped`, or `error` (the downloader crashed or could not
start; the reason is in `error`). `POST /stop` interrupts the downloader and the
yt-dlp it is running, like Ctrl+C in a terminal; finished videos stay. Stopping
the server stops its jobs the same way, so no download is left running unseen.

`summary` is `null` when the downloader crashed or could not write it - the job is
then `error`, with the reason in `error` - or when the job was stopped before the
downloader had started.
A run with nothing to download still writes one (with `excluded_by_skip_list` when
skip.txt left nothing), and so does a stop while a channel was being read.

`/api/preview` lists up to `limit` matching videos (default 20, at most 500), stops
scanning after about 45 seconds so it always answers on the public link (the result
then says how far it got), and runs one at a time - a second one meanwhile gets 503.

A `cookies.txt` in the download folder is used by every job and preview, and is
never downloadable. Each job looks for it as it starts, so one added later is used
from the next job on - no restart. Your file is never changed: yt-dlp gets a private
copy beside it, `.ytdl_cookies_*.txt`, removed when the run ends (yt-dlp would
otherwise save rotated cookies back into yours; one left by a run killed outright is
removed a day later). If the file cannot be
read, or YouTube rejects it ("The page needs to be reloaded" - it was rotated after
export, or is used from another IP), the run carries on without it and says so.

## Google Colab

**[Open the notebook](https://colab.research.google.com/github/freelancermeer/ytmeer/blob/main/YouTube_Downloader_API.ipynb)** - two cells:

Open it and do **Runtime > Run all**. If Colab warns that the notebook was not
authored by Google, click **Run anyway**.

1. **Setup** installs yt-dlp, ffmpeg, aria2c and gradio, and clones this repo.
   Tick `use_drive` to keep downloads in Drive; otherwise they go to
   `/content/downloads`, which is wiped with the runtime. `api_key` is optional:
   leave it blank to keep the saved key. A new key takes effect when cell 2 next
   starts a server, so a server that is still downloading keeps its own.
2. **Start the API** runs `api.py --share` in the background, checks the public
   link from outside, and prints what to use - nothing is rendered in the cell:

   ```
     public  https://xxxxxxxx.gradio.live/api      (reachable from the internet)
     docs    https://xxxxxxxx.gradio.live/api/docs      (open it, click Authorize, paste the key)
     local   http://127.0.0.1:8000/api      (for cells in this notebook)
     key     ...
     folder  /content/downloads
   ```

   Below that it prints the example from [API](#api) with your public URL and key
   already filled in, between copy markers, ready to paste into code anywhere. In a
   cell of the notebook, `API_URL`, `PUBLIC_URL`, `API_KEY` and `HEADERS` are set too.

   Keep the tab open: the link and the server stop when the runtime disconnects,
   and the next Run all gives a new URL. The paths in `files` are on the Colab
   machine; code running anywhere else fetches the same files through `downloads`.

   It is safe to re-run. The folder, key and port are saved beside the code, so
   after a runtime restart it finds the server that is still downloading. It
   replaces the server when cell 1 pulled newer code or changed the folder or key,
   or when the public link has stopped answering - never while that server is
   still downloading, unless you tick `force_restart`, which stops its jobs first.
   If the saved port has been taken by something else, it picks another.

Cookies are optional: public videos download without an account - unless YouTube
blocks the runtime's IP, which happens to some Colab machines. Cell 2 checks that at
start and prints `youtube OK` or `youtube BLOCKED`. When blocked, either start a fresh
runtime (Runtime > Disconnect and delete runtime, then Run all) to get another IP, or
drag a `cookies.txt` into Colab's Files panel - it lands in `/content` - and run cell 2
again: it copies the file into the download folder, where every job uses it. With
`use_drive` ticked you can instead keep `cookies.txt` in the Drive folder once.

Export the cookies from a private browser window: sign in to YouTube there, export,
then close the window. A session you keep using gets its cookies rotated, which
quietly invalidates the exported copy.

## Tests

```bash
python3 test_downloader.py    # the engine, through both the macOS and Windows code paths
python3 test_api.py           # the API's logic - standard library only
python3 test_api_routes.py    # the API over HTTP against a stand-in downloader (fastapi + httpx)
```

No network and no downloads.

## Notes

- **Quality:** always 720p–1080p, H.264/mp4 preferred for editor compatibility. If a video has nothing in that range it is **not** downloaded at lower quality — the error goes to `videoinfo.txt` instead.
- **Live progress bar:** yt-dlp's native progress (percent, size, speed, ETA) shows while each video downloads.
- One failed video never stops the batch; a summary (downloaded / skipped / failed) prints at the end.
