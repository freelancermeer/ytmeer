# YouTube Video Downloader — Plan & Brainstorm

## 1. Goal (what you asked for)

A Python script that downloads a **list of YouTube videos in 1080p** using **yt-dlp**, with your **own cookies.txt** for authentication. Each video gets its **own folder** named after the video, and inside that folder a **`videoinfo.txt`** holds the video title and link.

---

## 2. Requirements (restated clearly)

| # | Requirement | Detail |
|---|-------------|--------|
| 1 | Downloader engine | `yt-dlp` |
| 2 | Auth | User-provided `cookies.txt` (Netscape format) |
| 3 | Input | A text file of video links, **one link per line** |
| 4 | Quality | **1080p** (with sensible fallback if 1080p missing) |
| 5 | Output path | User provides **one base path**; everything downloads there |
| 6 | Per-video folder | Each video → its **own new folder** |
| 7 | Folder name | = **video title** (sanitized for filesystem) |
| 8 | Info file | Inside each folder → `videoinfo.txt` with **title + link** |

---

## 3. Folder structure (what the output looks like)

```
<base_path>/
├── Amazing Cat Video/
│   ├── Amazing Cat Video.mp4
│   └── videoinfo.txt
├── Best Python Tutorial/
│   ├── Best Python Tutorial.mp4
│   └── videoinfo.txt
└── ...
```

`videoinfo.txt` contents (success):
```
Title:   Amazing Cat Video
Link:    https://www.youtube.com/watch?v=XXXXXXXX
Quality: 1080p
Status:  OK
```

`videoinfo.txt` contents (failure):
```
Title:   (unknown / whatever we could fetch)
Link:    https://www.youtube.com/watch?v=XXXXXXXX
Quality: FAILED
Status:  ERROR
Error:   Requested format not available (no 720p-1080p stream)
```

---

## 4. How it works (flow)

1. Read config: `links.txt`, `cookies.txt`, base download path.
2. Loop over each link (skip blank lines / comments).
3. For each link:
   - Fetch metadata first (get the real video **title**).
   - Create folder `<base_path>/<sanitized title>/`.
   - Download best video+audio capped at **1080p**, merge to MP4.
   - Save `videoinfo.txt` with title + link.
   - Print progress (`[2/10] Downloading ...`).
4. On error for one video → log it, continue to the next (don't crash the whole batch).
5. At the end → print a summary (succeeded / failed).

---

## 5. Key technical decisions

### Format selection (1080p priority, 720p floor)
```
-f "bv*[height<=1080][height>=720]+ba/b[height<=1080][height>=720]"
```
- Picks best video between **720p and 1080p** → so 1080p if available, else 720p (or anything in between).
- `+ba` = best audio, merged into **mp4** (`--merge-output-format mp4`).
- **Never goes below 720p.** If nothing in the 720–1080 range exists → yt-dlp errors → we catch it and write the error into `videoinfo.txt` instead of downloading a lower quality.
- Actual downloaded resolution is verified with **ffprobe** and written into `videoinfo.txt`.

### Getting the title BEFORE downloading (for folder name)
- Use yt-dlp's Python API `extract_info(url, download=False)` to grab the title, then build the folder path, then download. This guarantees folder name = exact video title.

### Filename sanitization
- Windows/macOS forbidden chars (`\ / : * ? " < > |`) get stripped/replaced.
- yt-dlp's `--restrict-filenames` is an option, but we want the *real* readable title, so we'll sanitize manually and keep it readable.

### cookies
- Pass `cookies.txt` via `--cookies cookies.txt` (or `cookiefile` in Python API).

---

## 6. Two implementation options

### Option A — Python script using yt-dlp as a **library** (recommended)
- `import yt_dlp` and use its Python API.
- Pros: clean access to metadata (title) before download, easy error handling, one process.
- Cons: needs `pip install yt-dlp`.

### Option B — Python script that **shells out** to the `yt-dlp` CLI
- Uses `subprocess` to call the `yt-dlp` binary.
- Pros: no import needed, uses whatever yt-dlp is installed.
- Cons: parsing title is clunkier (`--print` / `--get-title`).

**Recommendation: Option A** — cleaner and more reliable for the "folder = title" requirement.

---

## 7. Config — how you'll set it up

A small block at the top of the script (easy to edit):

```python
LINKS_FILE   = "links.txt"      # one YouTube link per line
COOKIES_FILE = "cookies.txt"    # your exported cookies
DOWNLOAD_DIR = "/path/you/give" # base folder for all downloads
MAX_HEIGHT   = 1080             # quality cap
```

`links.txt` example:
```
https://www.youtube.com/watch?v=aaaaaaaaaaa
https://www.youtube.com/watch?v=bbbbbbbbbbb
https://youtu.be/ccccccccccc
```

---

## 8. Edge cases to handle

- [ ] 1080p not available → fall back to next best ≤1080p.
- [ ] Duplicate video titles → append video ID to avoid folder collision.
- [ ] Invalid / private / deleted video → log error, continue.
- [ ] Empty lines / `#` comments in links.txt → skip.
- [ ] Very long titles → truncate for filesystem safety.
- [ ] Illegal filename characters → sanitize.
- [ ] Re-running → skip already-downloaded videos (optional).

---

## 9. Dependencies

- Python 3.8+
- `yt-dlp` → `pip install yt-dlp`
- **ffmpeg** (required to merge 1080p video+audio into MP4) → must be installed on the system.

---

## 10. Deliverables

1. `downloader.py` — the main script.
2. `links.txt` — template for your links.
3. `README.md` — short how-to-run guide.
4. (You provide `cookies.txt` and the download path.)

---

## 11. Open questions for you (confirm before I build)

1. **ffmpeg** — is it installed? (Needed for 1080p merge.) If not, I'll add a check + install note.
2. Output container: **MP4** okay? (Most compatible.)
3. If 1080p is unavailable for a video — download **best available below 1080p**, or **skip**? (Default: download best below.)
4. Should `videoinfo.txt` include anything else (duration, channel, upload date), or just **title + link** as you said?
5. Skip videos already downloaded on re-run? (Default: yes, skip.)
