# ytmeer — operating manual for an AI agent

A YouTube **research** toolkit built on yt-dlp. Read this file before using it;
it is written to be sufficient on its own. `README.md` is the human version.

There are two interfaces over the same engine:

| | |
|---|---|
| `downloader.py` | the command line. One folder per run. |
| `api.py` | a local REST API over the same code. |

**The one rule that explains the whole design:** things whose output *is a file*
(a video, and the transcript/thumbnail/description/comments saved beside it) are
a **job** that writes a folder. Things whose output is **data** (a search, a
video's details, a channel's details, a transcript's text, comments) come back
**in the answer** and leave nothing on disk. Ask for the one you want.

---

## 1. Install

```bash
pip install -r requirements.txt          # yt-dlp, bgutil PO-token provider, curl_cffi
brew install ffmpeg aria2 node           # macOS; see requirements.txt for Windows
```

`ffmpeg` and `yt-dlp` are required. `aria2c` makes downloads ~4x faster. `node`
(or `deno`) is needed for YouTube's "n" challenge. The bgutil PO-token provider
is headless and prevents HTTP 403 on some videos; see README "PO tokens".

No cookies are needed for public videos. For private / members-only /
age-restricted videos put a `cookies.txt` in the run folder.

## 2. The command line

```bash
python3 downloader.py "<folder>" [flags]
```

`<folder>` holds `links.txt` (one link per line, `#` comments allowed) and
optionally `cookies.txt` and `skip.txt`. Downloads land in the same folder.

### 2.1 The five modes

| Mode | Flag | Writes | Typical cost |
|---|---|---|---|
| Download (default) | *(none)* | `<Video>/` with video + extras | minutes |
| Details | `--details` | **nothing** | ~3s video, ~25s channel |
| Transcript only | `--transcript` | `trans_*.txt`, `words_*.txt` | ~10s/video |
| Comments | `--extract-comments` | `comments_*.txt` | ~7s/100, ~19s with replies |
| Search | `--search QUERY` | `search_links.txt`, `search_results.json` | ~10s, more with dates |

`--details` accepts **both** a video link and a channel link and reports
whichever it was given. It is the only mode that writes nothing at all.

### 2.2 Every flag

**Which videos**
| Flag | Default | Meaning |
|---|---|---|
| `--channel` | off | Group into `<Channel>/<Video>/` folders |
| `--views N` | 0 | Channel/search: skip videos under N views |
| `--limit N` | 0 | Channel/search: keep only the newest/top N |
| `--days N` | 0 | Only videos from the last N days |
| `--this-month` | off | Only videos since the 1st of this month. **Needs `--search`** |
| `--min-duration S` | **180** | Shortest video to keep, seconds |
| `--shorts` | off | Include short-form videos (sets the floor to 0) |
| `--links FILE` | `<folder>/links.txt` | Read links from elsewhere |
| `--cookies FILE` | `<folder>/cookies.txt` | Use another cookie jar |

**Search**
| Flag | Default | Meaning |
|---|---|---|
| `--search QUERY` | — | Search YouTube; the results become the worklist |
| `--match related\|exact` | `related` | How wide a net — see §4.1 |
| `--sort ORDER` | `relevance` | `relevance`, `views`/`popularity`, `least-viewed`, `date`, `oldest` |
| `--search-count N` | *by route* | Results to scan: 150 normally, 40 for a date sort |
| `--in-channel URL` | — | Search inside one channel's own search tab |
| `--no-dates` | off | Skip fetching publish dates — see §4.3 |

**Quality and extras**
| Flag | Default | Meaning |
|---|---|---|
| `--min-height` / `--max-height` | 720 / 1080 | Quality floor and ceiling |
| `--no-subs` | off | Do not build transcripts |
| `--sub-lang CODE` | `en` | Transcript language |
| `--no-thumbnail`, `--no-description` | off | Skip those extras |
| `--comment-limit N` | 100 | Top-level comments per video (0 = all) |
| `--comment-replies` | off | Also fetch replies |
| `--comment-sort top\|new` | `top` | Comment order |

**How it runs**
| Flag | Meaning |
|---|---|
| `--dry-run` | Show the plan and exit. **Use this first.** |
| `--yes` | Skip the interactive confirmation (required for unattended runs) |
| `-v`, `--verbose` | yt-dlp's full output instead of the progress bar |

Contradictions are refused with a message, not silently ignored:
`--transcript` + `--no-subs`, `--days` + `--this-month`, `--this-month` or
`--in-channel` without `--search`, `--details` with `--transcript` or
`--extract-comments`, `--search ""`, and any negative count.

## 3. The REST API

```bash
python3 api.py                      # http://127.0.0.1:8000, prints the key
python3 api.py --port 9000 --outdir ~/Videos/YT
```

Send the key as an `X-API-Key` header. Interactive docs at `/api/docs`,
schema at `/api/openapi.json`.

### 3.1 Lookups — answer in the response, no job, no files

```bash
curl -H "X-API-Key: $KEY" "localhost:8000/api/video?url=dQw4w9WgXcQ"
curl -H "X-API-Key: $KEY" "localhost:8000/api/transcript?url=dQw4w9WgXcQ"
curl -H "X-API-Key: $KEY" "localhost:8000/api/comments?url=dQw4w9WgXcQ&limit=100"
curl -H "X-API-Key: $KEY" -G localhost:8000/api/search --data-urlencode 'query=cats'
curl -H "X-API-Key: $KEY" "localhost:8000/api/preview?channel=https://youtube.com/@X"
```

A link or a bare 11-character id both work. Status codes: `422` the request was
wrong (the body names the field), `502` YouTube would not answer for that video,
`503` another in-process request holds the engine, `401` bad key.

**Only one lookup, search or preview runs at a time** — they share the engine's
module globals. A second gets `503`; retry, do not queue. Every request resets
every global it can set, so nothing leaks between them. A route gives up after
120s.

### 3.2 Jobs — when the files are the point

```bash
curl -X POST localhost:8000/api/jobs -H 'content-type: application/json' \
  -H "X-API-Key: $KEY" -d '{"search":"a video title","sort":"popularity",
                            "limit":20,"transcript":true,"extract_comments":true}'
```

Returns a `job_id` at once; the work continues. Then poll
`GET /api/jobs/{id}` and collect finished videos from
`GET /api/jobs/{id}/videos?since=N`, passing back the `next` it returns — each
video appears the moment its folder is complete, so you can start on the first
while the rest download. Files come from `GET /api/jobs/{id}/files` and
`/files/{path}` (Range works).

Request fields mirror the flag names exactly (see §2.2) plus `links`, `skip`,
`outdir`, `verbose`, `dates`. `links` and `search` are two different worklists;
sending both is refused. `this_month`, `match`, `search_count` and `dates` only
mean something to a search and are refused without one.

## 4. The four things an agent gets wrong

### 4.1 `--match related` vs `exact` — this is not a sort

They ask YouTube **different questions**. Measured on one long video title:

| | Results |
|---|---|
| `--match exact` (yt-dlp's `ytsearch`) | **2** |
| `--match related` (YouTube's results page) | **150** |

`related` is the default and is usually what you want: relevance ordering keeps
the **near-matches** — the same story from another channel, the opposite angle,
the same formula in another language — and those are the research material.
`--sort` then orders what came back, **locally**. Asking YouTube to sort by views
instead collapses onto the exact phrase and loses all of it.

### 4.2 Shorts are excluded by default

The floor is **180s** (`--min-duration`), because a Short is at most 3 minutes
and duration is the only signal a listing carries. `--shorts` or
`--min-duration 0` turns it off. Two exemptions: a link to a channel's
`/shorts` tab is a request for Shorts, and a video you name yourself in
`links.txt` is never length-filtered. A listing that kept nothing says which
filter emptied it.

### 4.3 Publish dates cost ~2.1s per video

YouTube's flat listing carries views, duration and channel but **no upload
date**. Dates are fetched afterwards, in one yt-dlp call over the results that
**survived the filters** — 20 in 42.6s. So:

- A date **window** (`--days`, `--this-month`) is applied **locally**: the full
  relevance list is fetched, sorted, then dated *down the list in order*,
  stopping as soon as `--limit` is filled. On one title this found **20** videos
  for the month where YouTube's own date search returned **3**.
- A date **sort** (`--sort date`/`oldest`) needs every date up front, so it still
  asks YouTube and scans less deep (40, not 150).
- At most `DATE_RESOLVE_MAX` are dated: **100** on the CLI, **20** per API
  request. When that cap stops it, the run says so and `/api/search` returns
  `date_capped: true`.

**`found: 0` with `date_capped: true` means "this did not look far enough", not
"nothing was published."** A date fetch that *fails* is reported as an `error`,
never as an empty result.

### 4.4 Costs, so you can budget a run

| Operation | Measured |
|---|---|
| Flat search scan | ~0.07s per result |
| Dated search scan | ~2s per result |
| Publish date, per video | ~2.1s |
| Video details | ~3s |
| Channel details (278 videos) | ~23s |
| Transcript | ~9s |
| 100 comments | ~7s (~19s with replies) |
| Channel listing | one request per 100 videos |

## 5. What comes back

### 5.1 `GET /api/search` / `search_results.json`

```json
{ "query": "...", "match": "related", "sort": "views",
  "window": "since 01 Oct 2026", "depth": 150, "min_duration": 180,
  "scanned": 149, "dates_fetched": 57, "dates_missing": 0,
  "date_capped": false, "found": 20,
  "results": [ { "rank": 1, "id": "...", "link": "...", "title": "...",
                 "channel": "...", "views": 46701, "duration": 1685,
                 "duration_text": "28m 05s",
                 "published": "18 Oct 2026, 09:00:21 PM PKT",
                 "published_epoch": 1792339221 } ],
  "error": null }
```

`scanned` is what the search returned, `found` what survived. `match` is `null`
for an `in_channel` search, which uses that channel's own tab.

### 5.2 `GET /api/video` and `--details` on a video

`id`, `link`, `title`, `description`, `channel`, `channel_id`, `channel_url`,
`subscribers`, `views`, `likes`, `comment_count`, `duration`, `duration_text`,
`uploaded` (PKT), `upload_epoch`, `category`, `categories`, `tags`, `thumbnail`,
`live_status`, `availability`, `age_limit`, `captions`.

**A value YouTube does not publish is `null`, never `"NA"`** — that is the only
way to tell "no likes" from "likes hidden". `captions.auto` is worth reading
before asking for a transcript: only an auto-generated track has word timings.

### 5.3 `--details` on a channel

Counted from the channel's own videos, not estimated: `videos_listed`,
`views_listed`, `average_views`, `median_views`, `best_views`,
`average_length`, `shorts` / `long_videos`, `uploads_per_week`,
`first_upload`, `last_upload`, and `outliers` — every video at **2x the channel
average or better**, with its multiplier. Plus `subscribers`, `verified`,
`description`, `tags`, and, scraped best-effort from the About page,
`created`, `lifetime_views`, `country` (any of which may be `null`).

At most `CHANNEL_VIDEO_CAP` (2000) videos are read; `listing_capped` says when
it stopped there.

### 5.4 `GET /api/transcript`

`text` is the grouped form with `[hh:mm:ss]` stamps; `words` is one word per
line with milliseconds. `words` is `null` with the reason in `note` when YouTube
has no auto-generated track — only that track carries word timings.

### 5.5 `GET /api/comments`

`total_on_video` vs `fetched` shows what the limit left behind. `threads` is
nested: each top-level comment carries `author`, `likes`, `published` (PKT),
`text`, and its `replies`.

## 6. Reading the logs instead of re-running

Every run writes four files into the folder:

| File | What it is |
|---|---|
| `download_debug.jsonl` | **the structured trace — start here** |
| `download_log.json` | the run summary |
| `download_log.txt` | the human log |
| `download_log.jsonl` | one record per video, streamed as it finishes |

The trace's **first line is `run_start`**, and it carries
`settings_snapshot()` — *every* setting a flag can change, read from the live
globals, so you never have to guess how a run was invoked. After that, one
event per stage: `sources_start`, `channel_start` / `channel_finish` (with the
full per-reason tally), `search_start` / `search_finish`, `window_block` /
`window_finish`, `search_dates`, `details_start` / `details_finish`,
`video_start` / `video_finish`, `caption_*`, `comments_*`, `extras_*`, and
`command_start` / `command_finish` / `command_timeout` for **every** subprocess,
with the command line redacted of cookies and tokens.

```bash
# what did this run actually do?
python3 -c "import json;[print(json.loads(l)['event']) for l in open('download_debug.jsonl')]"
# why did the search keep so little?
grep search_finish download_debug.jsonl | python3 -m json.tool
```

## 7. Recipes

```bash
# What is this channel, and is it working?
echo "https://www.youtube.com/@SomeChannel" > run/links.txt
python3 downloader.py run --details

# The whole angle on a title: 150 near-matches, best performers first
python3 downloader.py run --search "<a video title>" --sort views --limit 20 --dry-run

# Who tried this angle and failed
python3 downloader.py run --search "<a video title>" --sort least-viewed --limit 10 --dry-run

# This month only, keeping the full breadth
python3 downloader.py run --search "<a video title>" --sort views --this-month --limit 20 --dry-run

# Transcripts and comments for the search results, no video files
python3 downloader.py run --search "<a video title>" --limit 5 \
    --transcript --extract-comments --comment-limit 50 --yes
```

**Always `--dry-run` first** on anything that could download. Add `--yes` only
when you have seen the plan, or when running unattended.

## 8. Limits to know before you promise something

- **One in-process request at a time** over the API (§3.1).
- **`published` is `null`** on a search unless dates were fetched (§4.3).
- **The About-page fields are scraped** and may break silently to `null` (§5.3).
- **`--this-month` is a search window**, not a download filter; use `--days N`
  to limit a download by date.
- A date sort cannot reach further back than its scan depth.
- Everything is public-data only. There is no access to anyone's private
  analytics, revenue or demographics.
