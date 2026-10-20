# ytmeer - a YouTube toolkit

A tool for **researching** YouTube, not only for downloading it. Built on
**yt-dlp**. Runs on macOS and Windows; no cookies needed for public videos.

> **Driving this from an AI agent?** Read **[AGENTS.md](AGENTS.md)** instead -
> every flag, every response shape, the measured cost of each operation, the
> four things that are easy to get wrong, and how to read the trace. It is
> written to be sufficient on its own.

Five things it does:

| What you want | How to ask for it |
|---|---|
| **Search** YouTube, or one channel | `--search` / `GET /api/search` |
| A video's **details** (views, subs, upload time, category) | `GET /api/video` |
| A video's **transcript** | `--transcript` / `GET /api/transcript` |
| A video's **comments** | `--extract-comments` / `GET /api/comments` |
| The **video itself**, in 1080p (720p floor) | the default |

**Files, or an answer.** The two are not the same request, so they do not work
the same way:

- Things whose output *is* a file - a video, and the transcript, thumbnail,
  description and comments file saved beside it - are a **job**. Each video gets
  its own folder with a `videoinfo.txt` inside.
- Things whose output is **data** - a search, a video's details, its transcript,
  its comments - come straight back **in the API response**. No job, no folder,
  nothing left on disk to read and tidy up afterwards.

So `GET /api/transcript` hands you the text; `--transcript` writes the files. Ask
for whichever one you actually want.

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

Before downloading, the command expands the channels, applies the filters, and
shows every eligible link with the number of already-completed and new videos.
The terminal selector starts with every new video selected. Use arrow keys to
move, press Space to deselect/select the highlighted video, `a` to select all,
`n` to deselect all, Enter to continue, or `q` to cancel. For example:

```bash
python3 downloader.py "/Users/macintoshhd/Desktop/test/" --channel \
  --views 10000 --days 7
```

Use `--dry-run` to print the same plan without downloading, or `--yes` to skip
the prompt in an automated terminal job:

```bash
python3 downloader.py "/Users/macintoshhd/Desktop/test/" --channel \
  --views 10000 --days 7 --dry-run
```

| Flag | What it does | Default |
|------|--------------|---------|
| `--channel` | Group videos into `<Channel Name>/` folders | off (flat) |
| `--min-height N` | Quality floor — never download below this | `720` |
| `--max-height N` | Quality ceiling | `1080` |
| `--views N` | Channel links only: skip videos under N views | no floor |
| `--limit N` | Channel links only: stop after the newest N matches | whole channel |
| `--days N` | Only download videos uploaded in the last N days | 0 (any time) |
| `--no-subs` | Skip the transcripts | transcripts on |
| `--sub-lang CODE` | Transcript language | `en` |
| `--transcript` | Transcript only, never download the video | off |
| `--extract-comments` | Also save `comments_<name>.txt` | off |
| `--comment-limit N` | Top-level comments per video (0 = all) | `100` |
| `--comment-replies` | Also fetch the replies under those comments | off |
| `--comment-sort S` | `top` (YouTube's ranking) or `new` | `top` |
| `--search QUERY` | Search YouTube and use the results as the links | off |
| `--in-channel URL` | With `--search`: search inside that channel only | YouTube-wide |
| `--search-count N` | Results to ask for before filtering | `50` |
| `--sort ORDER` | `relevance`, `views`/`popularity`, `least-viewed`, `date`, `oldest` | `relevance` |
| `--this-month` | Search window: since the 1st of this month | off |
| `--no-thumbnail` | Skip the thumbnail | thumbnail on |
| `--no-description` | Skip the description | description on |
| `--dry-run` | Show the filtered plan and exit | off |
| `--yes` | Skip the confirmation prompt | prompt on interactive CLI |
| `-v`, `--verbose` | Show yt-dlp's full output instead of the bar | quiet |

## Channel links

`links.txt` takes **channel links as well as video links**, mixed in any order. A
channel link is expanded into its videos, newest first, before anything is
downloaded:

```text
https://www.youtube.com/@SomeChannel
https://www.youtube.com/@basketballlegends67/videos
https://www.youtube.com/watch?v=dQw4w9WgXcQ
```

```bash
python3 downloader.py "/Users/macintoshhd/Desktop/test/" --channel --views 1000 --limit 3
```

That reads `@SomeChannel`, keeps the videos with **at least 1000 views**, takes
the **newest 3** of them, and downloads those — plus the plain video link, which
passes straight through untouched.

| | |
|---|---|
| `--views N` | Keep only videos with at least N views. No `--views` keeps every video. |
| `--limit N` | Stop after the newest N videos that match. **No `--limit` means the whole channel.** |
| `--days N` | Stop looking if videos are older than N days. **No `--days` means any time.** |

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

## Transcript only

`--transcript` makes the transcript the product instead of a by-product: the
metadata and the caption are fetched, the media download never runs, and the
thumbnail is skipped. On one video that was **4s against ~32s** for the full
download.

```bash
python3 downloader.py "/path/to/folder" --transcript
```

The folder records `Status: TRANSCRIPT` and `Quality: NA (transcript only)`,
which the normal resume index will not accept, so **asking for the video later
still downloads it**. The mode keeps its own index, so re-running `--transcript`
skips what it already transcribed — and a folder left by a full download counts
as transcribed too, since its `trans_` file is already there.

## Comments

```bash
python3 downloader.py "/path/to/folder" --extract-comments
```

`comments_<Video Title>.txt` holds the **top 100** comments, threaded, with
author, like count and a readable time. Replies are off by default and
`--comment-replies` turns them on.

The default is also the fastest shape, which is worth knowing because the
obvious spelling is a trap. `max_comments` counts replies toward its total:

| Asked for | Time | What came back |
|---|---|---|
| `100` total | 10.8s | **28** real comments, 72 replies |
| 100 top-level, replies off | **7.4s** | **100** real comments |
| 100 top-level + replies | 18.6s | 100 comments, 155 replies |

Comments can never fail a video: a rate limit loses the comments and keeps
everything else. A video with comments disabled reads `none`, which is a normal
state and not an error. Videos already downloaded gain their comments on a
later run without being fetched again.

## Search

Two separate questions, two separate flags:

- **`--match`** — *how wide a net*. `related` (the default) is YouTube's own
  results page, the relevance-ordered list you see in a browser. `exact` is
  yt-dlp's much narrower `ytsearch`.
- **`--sort`** — *how to order what came back*. Applied locally, always.

```bash
# the whole angle on a title: 150 near-matches, most watched first
python3 downloader.py "/path/to/folder" --search "<a video title>" --sort popularity

# only tight matches on that exact phrasing
python3 downloader.py "/path/to/folder" --search "<a video title>" --match exact

# reverse popularity: who tried this angle and failed
python3 downloader.py "/path/to/folder" --search "<a video title>" --sort least-viewed

# last 30 days, newest first
python3 downloader.py "/path/to/folder" --search "kash patel hearing" --days 30 --sort date

# inside one channel only (its own search tab; --match does not apply)
python3 downloader.py "/path/to/folder" --search "epstein" \
    --in-channel "https://www.youtube.com/@ForbesBreakingNews"
```

### Why `--match related` is the default

Measured on one long video title:

| | Results |
|---|---|
| `--match exact` (`ytsearch100:`) | **2** |
| `--match related` (the results page) | **150** |

Relevance is the point, not a compromise. Those 150 carried the same story from
other channels, the **opposite** angle ("Don't touch my *sister*", 320,590 views
against the original's 46,075), and the same formula in Japanese, German and
Arabic. Asking YouTube to sort by views instead collapses onto the exact phrase
and loses every one of them.

### Long videos only

Shorts are left out of every listing and every search, because the research is
about long-form video. The floor is **180 seconds** — a Short is at most three
minutes, and that is the only signal a listing actually carries.

```bash
--shorts                 # include them after all
--min-duration 600       # or set your own floor, in seconds
--min-duration 0         # same as --shorts
```

A link to a channel's **`/shorts` tab** is a request for Shorts, so the floor is
not applied there. Nor is it applied to a video link you name yourself in
`links.txt` — naming a video is asking for that video. A listing that kept
nothing says so, and says which flag undoes it.

The titles and links are printed, written to `search_links.txt`, and then the
run continues exactly as if that file had been your `links.txt` — so
`--dry-run`, the preflight selector, `skip.txt` and resume all apply. Add
`--transcript` or `--extract-comments` and the search feeds straight into them.

`--views` and `--limit` filter the results, and `--in-channel` uses the
channel's **own search tab**, which indexes its back catalogue rather than
whatever a site-wide search happens to surface.

### What a search gives you

Four things per video, which is what a search is for:

```
  1.       45,644 views    18 Oct 2026   Royal Hub
       DO NOT TOUCH MY BROTHER! Charlotte SHIELDS Louis From Laura Lopes...
       https://www.youtube.com/watch?v=GEWoZaUI7nU
```

Alongside the printed table the run writes two files into the folder:

- **`search_links.txt`** — the links, with each title as a comment. This is what
  the download step then reads.
- **`search_results.json`** — the same results machine-readably: `link`,
  `views`, `duration` and `duration_text`, `published` (exact time, PKT),
  `published_epoch`, `channel`, `title`, `rank`.

`--sort least-viewed` is reverse popularity: the weakest performers on a title
first. On one title this month that reads 0 views, 1 view, then 45,644 — three
channels, the same angle, wildly different results.

### Why the sorting is done locally

An earlier version of this README claimed YouTube's `sp=` search filters were
unreachable through yt-dlp. **That was wrong**, and it is corrected here:
`sp=` reaches YouTube intact. Measured on `how to learn guitar`:

| URL | Result |
|---|---|
| no `sp=` | relevance order, 3 of the first 10 entries are channel/playlist cards |
| `&sp=EgIQAQ%3D%3D` (Videos) | the cards are gone, relevance order kept |
| `&sp=CAMSAhAB` (sort by views) | 39.7M, 39.5M, 20.5M … genuinely sorted |
| `&sp=EgIIAg%3D%3D` (this week) | all fresh uploads |

The **Videos** filter is applied to the **dated route only**, and that scoping
is deliberate. It does not merely hide the non-video cards — it changes which
search YouTube runs, and by a lot in both directions, as the table above shows:
the same long title goes from 150 results to 3.

It is load-bearing exactly where it is used. A channel or playlist card is
harmless to the flat route, which leaves it as one entry with a non-video id
that gets dropped. The dated route cannot use `--flat-playlist` (it needs
timestamps) so yt-dlp **resolves** each card — and a channel card resolves into
that channel's own uploads, which then arrive as if they were search results.
Measured on `how to learn guitar --sort date` without the filter: all ten
"results" were one channel's uploads, not one of them a hit for the query, and
the run took 101s instead of 17s.

### Publish dates, without giving up the breadth

The cheap route returns no upload dates — YouTube's flat listing has none — so
they are fetched afterwards, in one yt-dlp pass over the results that **survived
the filters**, measured at about **2.1s per video** (20 in 42.6s). Dating the 20
results of a search therefore costs 42s where dating the 150 it looked at would
cost five minutes. `--no-dates` skips it.

A **date window** is applied here too, not by YouTube, and that is the whole
point: dates are fetched a block at a time *down the list already in the order
you asked for*, keeping what is in the window, stopping as soon as `--limit` is
satisfied. On one long title, `--sort views --this-month --limit 20`:

| | Results this month |
|---|---|
| YouTube's own Videos+date search | **3** |
| full relevance list, window applied locally | **20** (dated 80 of 149 to find them) |

A **date sort** (`--sort date`/`oldest`) still takes the dated route, because
ordering by date needs every date before anything can be ordered. At most
`DATE_RESOLVE_MAX` videos are dated in one run — **100** on the command line,
**20** through the REST API, which is cut at about 60s per request.

When that cap stops the walk early the run says so, and `GET /api/search`
returns `date_capped: true` alongside `dates_fetched` and `dates_missing` — so
`found: 0` with `date_capped: true` means "this did not look far enough", not
"nothing was published". A date fetch that *fails* (a bot check, a rejected
`cookies.txt`) is reported as an error rather than as an empty result, because
a window applied over dates that never arrived would call every video
out-of-window.

`--no-dates` does two things, and the second is easy to miss: it skips the date
fetch, **and** with `--days`/`--this-month` it hands the window back to YouTube,
which answers it from a much narrower search — faster, but far fewer results.

**Sorting** stays local on purpose, not for want of `sp=`. YouTube's own
sort-by-views collapses onto the exact phrase and discards the near-matches,
which are the whole reason to search at all — see `--match related` above. The
same goes for the window: **`--days 30` is exactly 30 days**, where YouTube's
"this month" covers one day on the 1st.

Two routes, chosen by whether a date is needed, because the cost gap is large:

| Route | When | Measured |
|---|---|---|
| Flat listing | no date filter or date sort | **~0.07s** per result |
| Non-flat + `--dateafter` | a date window, or `--sort date`/`oldest` | **~2s** per result |

Resolving dates one video at a time was measured at **10.8s per video** — five
times worse than letting the search extraction date them — so it is not used.
`--dateafter` was checked against filtering locally on a deterministic listing:
identical id sets, so the flag loses nothing.

**`--search-count` therefore has no single default.** Scanning 150 results takes
about 10s on the flat route but **3m 37s** once a date window forces a full
extraction per video — longer than the REST API will hold its lock for. So the
depth follows the route, and the run prints which it used:

| | Default depth |
|---|---|
| no date window | **150** |
| a date window, or `--sort date`/`oldest` | **40** |

Give `--search-count N` to override either. On one title: depth 40 found 15
videos this month in 64s; depth 150 found 44 in 3m 37s.

## The REST API

### Lookups: an answer, not a job

Four endpoints answer in the response itself. They start no job, create no
folder and leave no files - not even a temporary one.

```bash
curl -H "X-API-Key: $KEY" "localhost:8000/api/video?url=dQw4w9WgXcQ"
curl -H "X-API-Key: $KEY" "localhost:8000/api/transcript?url=dQw4w9WgXcQ"
curl -H "X-API-Key: $KEY" "localhost:8000/api/comments?url=dQw4w9WgXcQ&limit=100"
curl -H "X-API-Key: $KEY" -G localhost:8000/api/search --data-urlencode 'query=cats'
```

A link or a bare 11-character id both work.

**`GET /api/video`** - everything YouTube publishes about one video:

```json
{ "id": "dQw4w9WgXcQ", "title": "Rick Astley - Never Gonna Give You Up ...",
  "channel": "Rick Astley", "subscribers": 4550000, "views": 1828419799,
  "likes": 19489790, "comment_count": 2400000, "duration_text": "3m 33s",
  "uploaded": "25 Oct 2009, 11:57:33 AM PKT", "category": "Music",
  "captions": { "auto": ["en", "en-orig"], "manual": ["en"] },
  "tags": ["rick astley", "..."], "description": "..." }
```

A value YouTube does not publish comes back as `null`, never `"NA"`: this is
data, and `null` is the only way to tell "no likes" from "likes hidden".
`captions.auto` is worth reading before asking for a transcript - only an
auto-generated track carries word-level timings.

**`GET /api/transcript`** - the text, in both shapes, with no video downloaded:

```json
{ "word_level": true, "segments": 289, "language": "en",
  "text":  "[00:00:00] [Music] We're no strangers to love. You\n[00:00:23] know ...",
  "words": "[00:00:00.320] [Music]\n[00:00:18.800] We're\n[00:00:19.039] no ...",
  "note": null }
```

`text` is the grouped form and `words` is one word per line - the same two
shapes `trans_*.txt` and `words_*.txt` have on disk, written by the same code.
When YouTube has no auto-generated track, `words` is `null` and `note` says why
rather than leaving you to guess.

**`GET /api/comments`** - threaded, with `limit`, `replies` and `sort=top|new`:

```json
{ "total_on_video": 2400000, "fetched": 100, "top_level": 100, "replies": 0,
  "sort": "top", "replies_included": false,
  "threads": [ { "author": "@someone", "likes": 104000,
                 "published": "20 Oct 2020, 05:00 AM PKT", "text": "...",
                 "replies": [] } ] }
```

`total_on_video` against `fetched` shows what the limit left behind. 100
top-level comments take about 7s, and about 19s with their replies.

A lookup answers `422` for something that is not a video link, `502` when
YouTube will not answer for it (private, removed, bot-gated), and `503` when
another in-process request is already running - see the lock note below.

### Jobs: when the files are the point

`api.py` exposes every flag the downloader takes. Request fields mirror the
flag names: `transcript`, `extract_comments`, `comment_limit`,
`comment_replies`, `comment_sort`, `search`, `in_channel`, `search_count`,
`sort`, `this_month`.

A search job needs no `links` - the search supplies them:

```bash
curl -X POST localhost:8000/api/jobs -H 'content-type: application/json' -d '{
  "search": "kash patel hearing", "this_month": true, "sort": "popularity",
  "limit": 20, "transcript": true, "extract_comments": true
}'
```

`links` and `search` are two different worklists, so sending both is refused
rather than silently downloading one of them. `this_month`, `match` and
`search_count` only mean something to a search, so sending one without `search`
is refused rather than accepted and ignored - and the refusal is judged on what
you actually sent, so a defaulted `match` never trips it. To limit a download by
date, use `days`.

`shorts` and `min_duration` apply to downloads as well as searches, because the
length floor is about which videos the tool goes after at all.

**`GET /api/search`** searches without downloading anything and returns the
four fields per video:

```bash
curl -G localhost:8000/api/search \
  --data-urlencode 'query=kash patel hearing' \
  -d this_month=true -d sort=least-viewed -d limit=10
```

```json
{ "match": "related", "sort": "least-viewed", "window": "since 01 Oct 2026",
  "depth": 40, "min_duration": 180, "scanned": 37, "found": 3,
  "results": [ { "rank": 1, "views": 0, "duration_text": "18m 12s",
                 "published": "19 Oct 2026, 10:46:52 PM PKT",
                 "channel": "Royal Storyline",
                 "link": "https://www.youtube.com/watch?v=jrshTn5bePc" } ] }
```

The response says what was actually done, not what was asked: `depth` is how
far it scanned (see `--search-count` above), `min_duration` is the length floor
it applied, and `match` comes back `null` with `in_channel`, which searches that
channel's own tab and never uses either net. `scanned` is what the search
returned and `found` is what survived the filters - a narrow query with a month
window often finds only a handful, so raise `count` to scan further down.

`GET /api/preview` takes `shorts` and `min_duration` too, and reports the floor
it used: a preview that filtered differently from the job it previews would be
answering a different question.

**One at a time.** Every lookup and the channel preview reach into the
downloader's module globals in-process, so they share one lock: a second request
gets `503` rather than queueing behind a slow one. Each request resets every
global it can set first, so a `days=30` search cannot leak into the next
preview. A route gives up after 120s rather than holding the lock for the
downloader's own subprocess timeout.

A date window or a date sort costs about 2s per result scanned; without one a
search is about 0.15s per result.

`GET /jobs/{id}/videos` now lists `comments` alongside the video, transcript,
words, thumbnail and description.

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

Captions are required by default: the downloader does not mark a video as
finished until `trans_*.txt` exists. If YouTube temporarily returns HTTP 429,
the media is kept but the video remains retryable; the next run tries the
caption routes again. Use `--no-subs` only when captions are intentionally not
needed.

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

Three logs are written into the folder you point at:

- **`download_log.txt`** — everything yt-dlp said, for when a failure needs
  looking into.
- **`download_log.json`** — the run's totals, then a record per video: url,
  title, status, quality, bytes, seconds, folder, the files written, and the
  error if there was one. Easy to read from another script.
- **`download_log.jsonl`** — the same records, one line each, written the moment
  a video is done rather than when the run ends. This is what the API's
  `/videos` endpoint reads.
- **`download_debug.jsonl`** — the deep diagnostic trace. It records every
  metadata, media, caption, thumbnail, channel-listing and ffprobe command with
  start/finish events, duration, return code, retry route, and a redacted error
  tail. Signed URLs, PO tokens and cookie paths are removed. This file is
  generated automatically on every run.

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
**No browser window ever opens.** The downloader also uses bgutil's
video-bound **subtitle PO token** for the caption request; this is separate from
the media-download token. Install matching provider/server versions once:

```bash
BGUTIL_VERSION=2.0.0
pip install "bgutil-ytdlp-pot-provider>=${BGUTIL_VERSION},<3"
mkdir -p ~/.local/share/bgutil-pot && cd ~/.local/share/bgutil-pot
curl -sL "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/${BGUTIL_VERSION}.tar.gz" | tar xz --strip-components=1
cd server && npm install && npx tsc
```

The script is found automatically at `~/.local/share/bgutil-pot/server/build/generate_once.js`;
set `BGUTIL_SCRIPT` to point somewhere else. Keep the pip package and the server
on the **same major version**; yt-dlp will reject mismatched PO-token providers.
The startup banner shows whether the script was found.

HTTP 429 is a YouTube subtitle rate limit, not the PO-token 403 gate. The code
requests only the selected language, uses the subtitle PO-token route first, and
keeps the mweb/default routes as fallbacks.

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
| A video has no English captions | Recorded as an error and left retryable; it is not treated as a finished download while transcripts are enabled |
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

A small REST API so other code can drive the toolkit: look a video up and get
the answer back, or start a batch and collect each video the moment it finishes
while the rest keep downloading.

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
| `GET /api/search?query=...` | search YouTube: link, views, publish date, channel |
| `GET /api/video?url=...` | one video's details - **answers, no job** |
| `GET /api/transcript?url=...` | one video's transcript, as text - **no job** |
| `GET /api/comments?url=...` | one video's comments, threaded - **no job** |
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
