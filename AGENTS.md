# ytmeer — operating manual for an AI agent

A YouTube **research** toolkit built on yt-dlp. Read this before using it; it is
written to be sufficient on its own. `README.md` is the human version.

```
downloader.py   the command line — one folder per run
api.py          a local REST API over the same engine
mcp_server.py   the same engine as MCP tools, for an AI code editor — §11
```

---

## 0. THE RULE: long-form video only

**This tool is for long videos. Short-form is excluded from every list, count,
average and ranking.** The floor is `--min-duration`, default **180 seconds**,
because a Short is at most three minutes and duration is the only signal a
YouTube listing actually carries.

A channel's metrics are computed over its **long videos only** and say so
(`metrics_over: "long videos only"`). Its Shorts are counted and set aside, never
blended in — a channel posting both formats has two different view distributions
and an average across them describes neither.

Two deliberate exemptions, and only two:

| Exemption | Why |
|---|---|
| A link to a channel's **`/shorts` tab** | Asking for that tab is asking for Shorts. A Shorts listing also reports no duration at all, so the floor would drop every entry. This holds for the channel listings **and** for `--details` / `--channel-videos`, which report `shorts_tab: true` and `metrics_over: "every video"` when they take it. |
| A **video link you named yourself** in `links.txt` | Naming a video is asking for that video. |

To turn the floor off: `--shorts`, or `--min-duration 0`. Over the API:
`shorts=true` or `min_duration=0`. `--shorts` overrides `--min-duration`.

A listing or search that kept nothing **says which filter emptied it** and names
the flag that undoes it. Never read an empty result as "there is nothing there"
without checking that line.

---

## 1. What it does

**Ten research answers. None writes a video, a folder or a data file.** A
report writes only its own logs — `report_log.txt`, `report_log.json`,
`report_log.jsonl`, `report_debug.jsonl` — under those names precisely so it
never touches the `download_*` logs of a download that ran in the same folder.

| # | Answer | CLI | REST |
|---|---|---|---|
| 1 | A video's details | `--details` *(video link)* | `GET /api/video?url=` |
| 2 | A channel's details | `--details` *(channel link)* | `GET /api/channel?channel=` |
| 3 | A channel's long videos | `--channel-videos` | `GET /api/channel/videos?channel=` |
| 4 | A channel's outliers | *(in 2 and 3)* | *(in 2 and 3)* |
| 5 | A channel's playlists | `--playlists` | `GET /api/channel/playlists?channel=` |
| 6 | Videos like this one | `--similar` | `GET /api/similar?url=` |
| 7 | The channel behind any link | `--resolve` | `GET /api/resolve?url=` |
| 8 | A YouTube search | `--search QUERY` | `GET /api/search?query=` |
| 9 | A video's transcript | `--transcript` *(writes files)* | `GET /api/transcript?url=` |
| 10 | A video's comments | `--extract-comments` *(writes files)* | `GET /api/comments?url=` |

**Batch needs no flag.** Put many links in `links.txt`; a report loops over all
of them and one bad link does not stop the rest.

**Downloading** is the default mode and the only one that writes a video. It is a
**job**: `POST /api/jobs` answers at once and the work continues.

**The design rule:** output that *is a file* (a video, and the transcript /
thumbnail / description / comments beside it) is a job that writes a folder.
Output that is **data** comes back in the answer and leaves nothing behind.

## 2. Install

```bash
pip install -r requirements.txt       # yt-dlp, bgutil PO-token provider, curl_cffi
brew install ffmpeg aria2 node        # macOS; requirements.txt covers Windows
```

`ffmpeg` and `yt-dlp` required. `aria2c` makes downloads ~4x faster. `node` or
`deno` is needed for YouTube's "n" challenge. The bgutil PO-token provider is
headless and prevents HTTP 403 on some videos.

No cookies for public videos. For private / members-only / age-restricted, put a
`cookies.txt` in the run folder.

## 3. The command line

```bash
python3 downloader.py "<folder>" [flags]
```

`<folder>` holds `links.txt` (one link per line, `#` comments allowed) and
optionally `cookies.txt` and `skip.txt`. Downloads land in the same folder.

### 3.1 The reports

`--details  --resolve  --channel-videos  --playlists  --similar`

Each prints and stops. **Nothing is downloaded, and nothing is written except
the report's own `report_*` logs** (§7).
**One at a time** — two reports together is an error, and so is a report with
`--transcript` or `--extract-comments` (those exist to write files).

Reports run **before** channel expansion, so a channel link stays a channel
rather than becoming its 200 videos. `--details` dispatches on the link: a video
link gets the video report, a channel link the channel report.

**A report refuses the flags it cannot honour** rather than accepting and
ignoring them: `--search`, `--days`, `--this-month`, `--views` and
`--in-channel` are all errors with a report. What each one does use:

| Report | Uses |
|---|---|
| `--details` | `--shorts` / `--min-duration` |
| `--resolve` | nothing but the links |
| `--channel-videos` | `--limit`, `--shorts` / `--min-duration`, `skip.txt` |
| `--playlists` | `--shorts` / `--min-duration` |
| `--similar` | `--limit`, `--shorts` / `--min-duration`, `skip.txt`, `--match`, `--sort`, `--search-count`, `--no-dates` |

The run prints this as a `Uses :` line so there is no guessing.

### 3.2 Every flag

**Length — see §0**
| Flag | Default | Meaning |
|---|---|---|
| `--min-duration S` | **180** | Shortest video to keep, in seconds |
| `--shorts` | off | Include short-form (overrides `--min-duration`) |

**Which videos**
| Flag | Default | Meaning |
|---|---|---|
| `--channel` | off | Group downloads into `<Channel>/<Video>/` |
| `--views N` | 0 | Channel/search: skip videos under N views |
| `--limit N` | 0 | Keep only the newest/top N |
| `--days N` | 0 | Only videos from the last N days |
| `--this-month` | off | Since the 1st of this month. **Needs `--search`** |
| `--links FILE` | `<folder>/links.txt` | Read links from elsewhere |
| `--cookies FILE` | `<folder>/cookies.txt` | Another cookie jar |

**Search**
| Flag | Default | Meaning |
|---|---|---|
| `--search QUERY` | — | Search YouTube; the results become the worklist |
| `--match related\|exact` | `related` | How wide a net — **§5.1** |
| `--sort ORDER` | `relevance` | `relevance`, `views`/`popularity`, `least-viewed`, `date`, `oldest` |
| `--search-count N` | *by route* | 150 normally, 40 for a date sort. **Over the API this query parameter is `count`** (the job field is `search_count`) |
| `--in-channel URL` | — | Search one channel's own search tab |
| `--no-dates` | off | Skip publish dates — **§5.3** |

**Files a download writes**
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
| `--dry-run` | Show the download plan and exit. **Use before any download.** |
| `--yes` | Skip the confirmation prompt (required unattended) |
| `-v`, `--verbose` | yt-dlp's full output instead of the progress bar |

**What happens to a link in `links.txt`.** A bare 11-character id becomes a
watch link. A watch link copied from a playlist loses its `&list=` and
`&index=`; a `youtu.be` link its `?t=`; a share link its `?si=`/`&feature=` —
none of those name a different video, and `&list=` used to make yt-dlp walk the
whole playlist. A YouTube link that is **neither a video nor a channel** (the
uploads playlist `playlist?list=UU...`, a bare legacy vanity URL, a `/results`
page) and any **non-YouTube link** is refused with a message, counted in the
summary as `refused_links`, and makes the run exit non-zero. A UTF-8 BOM,
zero-width characters, CRLF and UTF-16 ("Unicode" in Notepad) are all handled;
a line containing a NUL byte is dropped with a message.

A `--cookies` path that does not exist is **not** fatal — cookies are optional —
but the run says so loudly and the banner shows `NOT FOUND`, rather than
reading "none (optional)" as before.

**Aliases** you may meet in existing scripts: `--view` and `--min-views` both
mean `--views`; `--sort-by` means `--sort`.

Contradictions are **refused with a message**, never silently ignored: two
reports at once; a report with `--transcript`/`--extract-comments`;
`--transcript` + `--no-subs`; `--days` + `--this-month`; `--this-month` or
`--in-channel` without `--search`; `--search ""`; any negative count.

## 4. The REST API

```bash
python3 api.py                  # http://127.0.0.1:8000, prints the key
python3 api.py --port 9000 --outdir ~/Videos/YT
```

Key goes in an `X-API-Key` header. Docs at `/api/docs`, schema at
`/api/openapi.json`.

### 4.1 The lookups — answer in the response, no job, no files

```bash
K="X-API-Key: $KEY"; B=http://localhost:8000
curl -H "$K" "$B/api/video?url=dQw4w9WgXcQ"
curl -H "$K" "$B/api/transcript?url=dQw4w9WgXcQ"
curl -H "$K" "$B/api/comments?url=dQw4w9WgXcQ&limit=100"
curl -H "$K" "$B/api/resolve?url=dQw4w9WgXcQ"
curl -H "$K" "$B/api/channel?channel=https://www.youtube.com/@X"
curl -H "$K" "$B/api/channel/videos?channel=https://www.youtube.com/@X&limit=20"
curl -H "$K" "$B/api/channel/playlists?channel=https://www.youtube.com/@X"
curl -H "$K" "$B/api/similar?url=dQw4w9WgXcQ&limit=20"
curl -H "$K" -G "$B/api/search" --data-urlencode 'query=a video title'
curl -H "$K" "$B/api/preview?channel=https://www.youtube.com/@X"
```

A link or a bare 11-character id both work where a video is wanted.

`shorts` and `min_duration` are accepted by every route that lists videos —
`/api/channel`, `/api/channel/videos`, `/api/channel/playlists`, `/api/similar`,
`/api/search`, `/api/preview`. `/api/resolve` takes neither. `skip` (links or
ids to leave out, as `skip.txt` would) is accepted by `/api/search`,
`/api/preview`, `/api/channel/videos` and `/api/similar`.

Every lookup refuses, with a `422` naming `url`/`channel`, any link whose host
is not YouTube's, and any YouTube link that is neither a video nor a channel —
**before** the engine is touched. That is the SSRF guard: the id pattern alone
used to accept any host carrying `v=<11 chars>`.

**Status codes:** `422` the request was wrong — the body names the field in
`detail[0].loc`; `502` YouTube would not answer, **including a search whose
engine failed and found nothing** (a bot check, a timeout — formerly a `200`
with `found: 0`); `503` another in-process request holds the engine; `401` bad
or missing key, including a non-ASCII one.

A **partial** answer stays a `200`: results with a noted `error` or
`date_error` (say, the publish dates could not be fetched) is still an answer.

**One lookup, search or preview at a time.** They share the engine's module
globals. A second gets `503` — retry, do not queue. Every request resets every
global it can set, so nothing leaks between them.

Every route sets a **120s wall** (`/api/preview` keeps its own 45s). The
paid-per-video work honours it: the metadata lookup, date fetching, the window
walk and the playlist probes all stop at it, and a metadata **timeout is not
retried** (it used to be, three times). A channel *listing* is bounded instead
by its own 300s-per-request timeout and by `CHANNEL_VIDEO_CAP`, so a very large
channel can still outlast the wall — `listing_capped` says when only part of it
was read.

### 4.2 Jobs — when the files are the point

```bash
curl -X POST "$B/api/jobs" -H 'content-type: application/json' -H "$K" -d '{
  "search":"a video title","sort":"popularity","limit":20,
  "transcript":true,"extract_comments":true }'
```

Returns a `job_id` at once. Poll `GET /api/jobs/{id}`; collect finished videos
from `GET /api/jobs/{id}/videos?since=N`, passing back the `next` it returns —
each video appears as soon as its folder is complete. Files from
`GET /api/jobs/{id}/files` and `/files/{path}` (Range works). Stop with
`POST /api/jobs/{id}/stop`.

Request fields mirror the flag names (§3.2) with `_` for `-`, plus `links`,
`skip`, `outdir`, `verbose`, `dates`. The **negated** flags are positive
booleans here: `--no-subs` is `subs: false`, `--no-thumbnail` is
`thumbnail: false`, `--no-description` is `description: false`, `--no-dates` is
`dates: false`. Two more do not mirror: there is no job field
for the report flags (a job downloads; use the lookup routes to report), and
the **lookup** routes name their scan depth `count` rather than `search_count`. `links` and `search` are two different worklists — sending
both is refused. `this_month`, `match`, `search_count` and `dates` mean nothing
without `search` and are refused without it.

## 5. The four things an agent gets wrong

### 5.1 `--match related` vs `exact` is not a sort

They ask YouTube **different questions**. Measured on one long video title:

| | Results |
|---|---|
| `--match exact` (yt-dlp's `ytsearch`) | **2** |
| `--match related` (YouTube's results page) | **150** |

`related` is the default and is usually right: relevance keeps the
**near-matches** — the same story from another channel, the opposite angle, the
same formula in another language — and those are the research material. `--sort`
then orders what came back, **locally**. Asking YouTube to sort by views instead
collapses onto the exact phrase and loses all of it.

### 5.2 An outlier ratio is relative to its own channel

`outlier` on a channel's video, and the `outliers` list, are `views ÷ the
channel's average views over its long videos`. `2.0` means twice the channel's
own average, not twice anything global. The average is always taken over **every**
long video **before** `--limit`, so the ratio compares a video with its channel
rather than with the others on the page.

### 5.3 Publish dates cost ~2.1s per video

A flat YouTube listing carries views, duration and channel but **no upload
date**. So:

- `--channel-videos` returns **no dates at all** — dating 276 videos would be
  ten minutes. Use `--details` on the channel for first/last upload, which dates
  exactly two videos.
- A search fetches dates for the results that **survived the filters**, in one
  call: 20 in 42.6s.
- A date **window** (`--days`, `--this-month`) is applied **locally**: the full
  relevance list is fetched, sorted, then dated *down the list in order*,
  stopping as soon as `--limit` is filled. On one title this found **20** videos
  for the month where YouTube's own date search returned **3**.
- A date **sort** (`--sort date`/`oldest`) needs every date up front, so it asks
  YouTube and scans less deep (40, not 150).
- At most **100** videos are dated per CLI run, **20** per API request.

**`found: 0` with `date_capped: true` means "did not look far enough", not
"nothing was published."** A date fetch that *fails* on a windowed search is
reported as an `error`; on an unwindowed one the results stand and the reason is
in `date_error`.

Two edges worth knowing. Without a window, dating is **all-or-nothing** past the
cap: more survivors than the cap (20 over the API) and *none* are dated — raise
`limit` to get them. And `--no-dates` / `dates=false` **with** a window is not
"skip the dates": with no local dates to filter on, the window is handed back to
YouTube's own date search, which scans only 40 deep and returns far fewer.

### 5.4 Budget from the measured costs

| Operation | Measured |
|---|---|
| Flat search scan | ~0.07s per result |
| Dated search scan | ~2s per result |
| Publish date, per video | ~2.1s |
| Video details | ~3s |
| Channel details (278 videos) | ~23s |
| Channel videos (278 listed) | ~11s |
| Playlists (5, with counts) | ~8s |
| Similar videos (no dates) | ~24s |
| Similar videos (with dates, 20 kept) | ~65s |
| Transcript | ~9s |
| 100 comments | ~7s (~19s with replies) |
| Channel listing | one request per 100 videos |

## 6. What comes back

### 6.1 `GET /api/video` — and `--details` on a video

`id`, `link`, `title`, `description`, `channel`, `channel_id`, `channel_url`,
`subscribers`, `views`, `likes`, `comment_count`, `duration`, `duration_text`,
`uploaded` (PKT), `upload_epoch`, `category`, `categories`, `tags`, `thumbnail`,
`live_status`, `availability`, `age_limit`, `captions`.

**A value YouTube does not publish is `null`, never `"NA"`** — the only way to
tell "no likes" from "likes hidden". Read `captions.auto` before asking for a
transcript: only an auto-generated track has word timings.

### 6.2 `GET /api/channel` — and `--details` on a channel

Counted from the channel's own **long** videos, not estimated:

`videos_listed`, `long_videos`, `shorts`, `unknown_length`, `live_or_upcoming`,
`excluded`, `metrics_over`, `shorts_tab`, `listing_capped`, `views_listed`,
`average_views`, `median_views`, `best_views`, `average_length`,
`average_length_text`, `uploads_per_week`, `first_upload`, `last_upload`,
`date_error`, `outliers` (**every** long video at 2x the channel average or
better, with its multiplier — the printer shows ten, the data carries them
all), `outlier_count`, `listing_error`.

`shorts` is what was *measured* under the floor; `unknown_length` is what had
no duration to measure; `live_or_upcoming` is a stream in progress or a
premiere, left out exactly as the download path leaves them out; and `excluded`
is everything set aside. `date_error` is also set when only *one* of the two
dated edges came back. `median_views` is the true median (the two
middles averaged on an even count). The first and last upload are dated from
the **long** videos, the same population every other number uses, and
`date_error` says when that fetch failed rather than leaving the three date
fields silently null.

Plus `id`, `name`, `handle`, `link`, `subscribers`, `verified`, `description`,
`tags`, `availability` — and `created`, `lifetime_views`, `country`, which are
**scraped from the About page** because yt-dlp exposes none of them. That scrape
is the only one in this tool and is strict best effort: any failure leaves those
three `null` and the rest of the report stands.

At most 2000 videos are read; `listing_capped` says when it stopped there, and
the averages then describe only that slice.

### 6.3 `GET /api/channel/videos`

`videos` (each with `rank`, `id`, `link`, `title`, `views`, `duration`,
`duration_text`, `outlier`), `listed`, `long_videos`, `short_form_dropped`,
`live_or_upcoming`, `excluded_by_skip_list`, `shorts_tab`, `listing_capped`,
`average_views`, `listing_error`. No publish dates — see §5.3.

`average_views` is over every long video **before** `limit`, so `outlier`
compares a video with its channel. Past `listing_capped` it describes only the
slice that was read, and so does every ratio built on it.

### 6.4 `GET /api/channel/playlists`

`playlists` (each `id`, `title`, `link`, `videos`, `long_videos`, `shorts`,
`unknown_length`, `split_over`, `split_capped`), `counted`, `count_capped`,
`count_failures`, `out_of_time`, `listing_capped`.

The split is counted over at most **200** entries of each playlist
(`split_over` says how many, `split_capped` when it stopped there), while
`videos` is yt-dlp's full total — so on a huge playlist the parts do not add up
to the total, and the row says so. `out_of_time` means the request's wall
stopped the probes early.

One listing names them all; the split needs a request per playlist, so it is
filled in for the first **25** and is `null` after. **`videos` is yt-dlp's own
total and counts Shorts**; `long_videos` and `shorts` are counted here, which
is the only honest answer for a Shorts compilation. `count_capped` means there
were more than 25, `listing_capped` that the list itself was cut at 200, and
`count_failures` how many probes YouTube refused.

### 6.5 `GET /api/resolve`

`id`, `name`, `handle`, `link`, `videos_tab`, `subscribers`, `from` (`"channel
link"` or `"video link"`). Takes a handle, any channel tab, or a video link.
Use it to get the one channel id everything else keys off.

If no channel id can be found it is a `502`, not a `200` full of nulls.
A YouTube link that is neither a video nor a channel — its uploads playlist
(`playlist?list=UU...`), a bare legacy vanity URL, a `/results` page — is
**refused everywhere**, including in `links.txt`, rather than treated as a
named video.

### 6.6 `GET /api/similar`

`id`, `link`, `title`, `views`, `query`, `scanned`, `found`, `similar` (the same
row shape as a search result), plus `dates_fetched`, `dates_missing`,
`date_capped`, `date_error` and `filtered_out` (per-reason counts).

YouTube's related feed is not reachable through yt-dlp, so this **searches for
the video's own title** and leaves the video itself out. It needs a **video**
link — a channel or playlist link is refused rather than answered with that
channel's own uploads. `skip.txt` still applies.

### 6.7 `GET /api/search`

```json
{ "query": "...", "match": "related", "sort": "views",
  "window": "since 01 Oct 2026", "depth": 150, "min_duration": 180,
  "scanned": 149, "dates_fetched": 57, "dates_missing": 0,
  "date_capped": false, "date_error": null, "found": 20,
  "results": [ { "rank": 1, "id": "...", "link": "...", "title": "...",
                 "channel": "...", "views": 46701, "duration": 1685,
                 "duration_text": "28m 05s",
                 "published": "18 Oct 2026, 09:00:21 PM PKT",
                 "published_epoch": 1792339221 } ],
  "error": null }
```

`scanned` is what the search returned, `found` what survived. `match` is `null`
for an `in_channel` search, which uses that channel's own tab.

### 6.8 `GET /api/transcript` and `/api/comments`

Transcript: `text` is the grouped form with `[hh:mm:ss]` stamps, `words` is one
word per line with milliseconds. `words` is `null` with the reason in `note`
when YouTube has no auto-generated track.

Comments: `total_on_video` vs `fetched` shows what the limit left behind.
`threads` is nested — each top-level comment carries `author`, `likes`,
`published` (PKT), `text`, and its `replies`.

## 7. Reading the logs instead of re-running

Every run writes four files into the folder. A download writes `download_*`;
a report writes `report_*` under the same four names, so the two never
overwrite each other:

| File | What it is |
|---|---|
| `download_debug.jsonl` / `report_debug.jsonl` | **the structured trace — start here** |
| `download_log.json` / `report_log.json` | the run summary (`run_type`, `failed`, `refused_links`, `stopped_early`) |
| `download_log.txt` / `report_log.txt` | the human log |
| `download_log.jsonl` / `report_log.jsonl` | one record per video, streamed as it finishes |

The trace's **first line is `run_start`** and carries `settings_snapshot()` —
*every* setting a flag can change, read from the live globals, so you never have
to guess how a run was invoked. Then one event per stage: `sources_start`,
`report_start` / `report_finish`, `details_start` / `details_finish`,
`link_refused` (one per link `read_links` dropped), `resolve`,
`channel_details_start` / `channel_details_finish`,
`channel_videos`, `channel_playlists`, `about_page`, `similar_videos`,
`channel_start` / `channel_finish` (with the full per-reason tally),
`search_start` / `search_finish`, `window_block` / `window_finish`,
`search_dates`, `video_start` / `video_finish`, `caption_*`, `comments_*`,
`extras_*`, `stale_stub_removed` (the final sweep succeeded, so the failed
attempt's `video_<id>` folder went — `stale_stub_kept` says when one could not
be), and `command_start` / `command_finish` / `command_timeout` for
**every** subprocess, with cookies and tokens redacted from the command line.

```bash
# what did this run actually do?
python3 -c "import json;[print(json.loads(l)['event']) for l in open('download_debug.jsonl')]"
# how was it configured?
head -1 download_debug.jsonl | python3 -m json.tool
# why did the search keep so little?
grep search_finish download_debug.jsonl | python3 -m json.tool
```

## 8. Recipes

```bash
R=run; mkdir -p $R

# Is this channel working? (created, first upload, uploads/week, outliers)
echo "https://www.youtube.com/@SomeChannel" > $R/links.txt
python3 downloader.py $R --details

# Which of its videos beat its own average, newest first
python3 downloader.py $R --channel-videos --limit 30

# What it groups its content into
python3 downloader.py $R --playlists

# The channel id behind a video, for everything else
echo "https://youtu.be/VIDEOID" > $R/links.txt
python3 downloader.py $R --resolve

# The whole angle on one title: 150 near-matches, best performers first
python3 downloader.py $R --search "<a video title>" --sort views --limit 20 --dry-run

# Who tried that angle and failed
python3 downloader.py $R --search "<a video title>" --sort least-viewed --limit 10 --dry-run

# This month only, keeping the full breadth
python3 downloader.py $R --search "<a video title>" --sort views --this-month --limit 20 --dry-run

# Videos like one that worked
echo "https://youtu.be/VIDEOID" > $R/links.txt
python3 downloader.py $R --similar --limit 20

# Transcripts and comments for search results, no video files
python3 downloader.py $R --search "<a video title>" --limit 5 \
    --transcript --extract-comments --comment-limit 50 --yes

# Many channels at once — batch needs no flag
printf '%s\n' "https://youtube.com/@A" "https://youtube.com/@B" > $R/links.txt
python3 downloader.py $R --details
```

**`--dry-run` first** on anything that could download. Add `--yes` only after
seeing the plan, or when running unattended.

## 9. Exit codes

The CLI exits **0** only when the run did what was asked. **1** when any link
failed, any link was refused, or the worklist was empty (an empty `links.txt`,
a search that matched nothing, a report with nothing to report). **130** when
it was stopped with Ctrl+C. **75** when another run of the same kind already
holds the folder (§12) — the only code that means *nothing was attempted*, so
it is the only one worth simply retrying. A `--dry-run` that finds nothing new is still 0.
`download_log.json` / `report_log.json` carries the same facts as `failed`,
`refused_links` and `stopped_early`.

## 10. Limits — know these before promising anything

- **Long videos only** (§0). Short-form is excluded from every number.
- **One download per folder, across every process** (§12). A second one exits
  75 rather than mixing its logs into the first's. A report may still run
  alongside a download; two reports may not.
- **One in-process API request at a time** (§4.1).
- **`--channel-videos` returns no publish dates** at all (§5.3).
- **`published` is `null`** on a search unless dates were fetched (§5.3).
- **`created`, `lifetime_views` and `country` are scraped** and can go `null`
  without warning (§6.2).
- **At most 2000 videos** are read per channel; **25** playlists get counts.
- **`--this-month` is a search window**, not a download filter. Use `--days N`
  to limit a download by date.
- A date sort cannot reach further back than its scan depth.
- **YouTube links only.** Anything else is refused, not fetched (§3.2, §4.1).
- **Public data only.** There is no access to anyone's private analytics,
  revenue, retention or demographics. Revenue and RPM are not estimated here at
  all — if a number like that is wanted, this tool does not have it.

## 11. MCP — the same toolkit inside an editor

`mcp_server.py` exposes this engine as **17 MCP tools** over stdio. It is not a
server to host: the editor launches it as a child process and talks over
stdin/stdout. Needs `mcp>=2.3` (`pip install --user --break-system-packages mcp`).

```bash
claude mcp add -s user -t stdio ytmeer -- python3 "$PWD/mcp_server.py"
```

The same split as the REST API — an answer comes back, a file is a job:

| Tools | Maps to |
|---|---|
| `yt_health` | `GET /api/health` |
| `yt_search` | `GET /api/search` |
| `yt_video`, `yt_transcript`, `yt_comments`, `yt_resolve` | the video lookups |
| `yt_channel`, `yt_channel_videos`, `yt_channel_playlists`, `yt_similar`, `yt_preview` | the channel lookups |
| `yt_download`, `yt_job_status`, `yt_job_files`, `yt_job_stop`, `yt_jobs`, `yt_job_forget` | `/api/jobs*` |

Three things differ from the REST API, deliberately:

- **Absolute paths, not URLs.** The editor is on this machine, so
  `yt_job_status` hands over `/Users/…/Title.mp4` rather than
  `/jobs/{id}/files/…`. There is no file-download tool; the editor opens the path.
- **A busy engine waits, it does not fail.** `YTMEER_BUSY_WAIT` (default 90s)
  replaces the API's 5s, which existed because a queued HTTP request would hit a
  proxy timeout. Over stdio there is no proxy, and an agent comparing two
  channels fires both calls at once.
- **§0, §5.1, §5.2 and §5.3 are written into the tool descriptions**, so a model
  in another editor follows them without having read this file.

Everything else is unchanged: the long-form floor, one in-process request at a
time, the cost table in §5.4, and the limits in §10 all apply exactly as they do
over HTTP.

**Remote.** `--http` serves the same tools as streamable HTTP for a client that
cannot launch a local process (claude.ai in a browser). It **refuses to start
without `YTMEER_HTTP_TOKEN`**, and then requires
`Authorization: Bearer <token>` on every request — no OAuth, just `/mcp`.
`--no-auth` is loopback-only and refused on any other address. stdio never
consults the token. Over a browser the data tools are the useful ones: a
download still lands on *this* disk, and `yt_job_status` returns paths the
browser cannot open. See the README's "Remote" section for the tunnel (it must
pass SSE) and the connector setup.

## 12. One run per folder

Two runs of the same kind in one folder would each truncate that kind's three
logs at startup and then append to them, so both runs' records interleave and
neither summary describes one run. So a run takes an exclusive lock on
`<folder>/.ytmeer-<kind>.lock` before it touches a single log, and exits **75**
if it cannot (§9).

**One lock per KIND of run, not per folder.** A report writes only its own
`report_*` logs (§1), so it collides with nothing a download writes:

| | second run | |
|---|---|---|
| download | download | **refused**, exit 75 |
| report | report | **refused**, exit 75 |
| download | report | allowed, deliberately |

`--dry-run` takes the lock too, because it writes the plan into
`download_log.json`. A report's lookups over the REST API take nothing: they
write no files at all (§1).

The lock is advisory and lives on an open descriptor, so it **dies with the
process** — a crashed or `SIGKILL`ed run leaves nothing stale to clean up, and
there is no lock file to delete by hand. A folder that cannot be written is not
reported as busy: the lock guards a rare collision and must never become a new
way to fail.

**Over the REST API and MCP.** `POST /api/jobs` and `yt_download` ask before
starting, so a folder another process holds is refused up front — a `409`, the
same answer as a clash with one of that server's own jobs, rather than a job
that starts and immediately errors. Their in-memory job tables are per process
and cannot see each other, which is exactly the hole this closes: two editors
each running `mcp_server.py`, or a plain CLI run beside a server's job. If the
lock is lost in the race between asking and starting, the job ends `error` with
`another download run holds <folder>, so this job wrote nothing`.
