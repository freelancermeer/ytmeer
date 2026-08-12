# YouTube Video Downloader

Downloads a list of YouTube videos in **1080p** (priority) down to **720p** (floor) using **yt-dlp**, with your own **cookies.txt**. Each video goes into its own folder named after the video title, with a `videoinfo.txt` inside.

## What you need (already installed on this machine ✅)
- Python 3
- `yt-dlp`
- `ffmpeg` (used to merge 1080p video+audio and to read the final quality)

## Setup

1. **Put your links** in `links.txt` — one per line:
   ```
   https://www.youtube.com/watch?v=xxxxxxxxxxx
   https://youtu.be/yyyyyyyyyyy
   ```

2. **Put your cookies** in `cookies.txt` (Netscape format, exported from your browser) in this folder.

3. **Set the download path** (optional) — open `downloader.py` and edit the CONFIG block:
   ```python
   DOWNLOAD_DIR = "./downloads"   # change to any path you want
   ```

## Run

```bash
python3 downloader.py
```

## Output

```
<DOWNLOAD_DIR>/
├── <Video Title>/
│   ├── <Video Title>.mp4
│   └── videoinfo.txt
└── ...
```

`videoinfo.txt` (success):
```
Title:   My Video
Link:    https://www.youtube.com/watch?v=xxxx
Quality: 1080p
Status:  OK
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

By default it also builds **two English transcript `.txt` files** next to each video, from YouTube's word-level auto-caption. Format is the same as `../Transcript with timestamps/transcribe.py` (its `default` and `oneword` modes):

- `trans_<Video Title>.txt` — grouped, wrapped lines:  `[hh:mm:ss] text`
- `words_<Video Title>.txt` — one word per line:       `[hh:mm:ss.mmm] word`

Both are noted in `videoinfo.txt` (`Transcript:` / `Words:` lines).

- Turn transcripts off:  `python3 downloader.py "<dir>" --no-subs`
- Different language:    `python3 downloader.py "<dir>" --sub-lang es`  (e.g. Spanish)
- **Backfill:** if a video is already downloaded but has no `trans_*.txt`, re-running builds the transcripts for it (no re-download of the video).

## Notes
- **Quality:** always 720p–1080p, H.264/mp4 preferred (best compatibility). If a video has no stream in that range, it is **not** downloaded at lower quality — the error is written to `videoinfo.txt` instead.
- **Live progress bar:** yt-dlp's native progress (percent, size, speed, ETA) shows in the terminal while each video downloads.
- **Skip already-downloaded:** if you run it again on the same folder, any video whose `videoinfo.txt` says `Status: OK` (and whose file is still present) is **skipped** — it is not re-downloaded. Delete a video's folder if you want a fresh pull.
- One failed video does not stop the batch; a summary (downloaded / skipped / failed) is printed at the end.
