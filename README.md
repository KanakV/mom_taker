# mom — automatic Minutes of Meeting

Record a meeting on your phone; structured minutes appear in a shared Google Doc the next morning. No servers, no paid APIs — a Python script on your laptop orchestrates free-tier **Groq** (Whisper transcription + an open LLM for summarisation) and the **Google Docs/Calendar APIs**, driven by a calendar you control.

```
phone (Voice Memos / share shortcut)
        │  .m4a → Dropbox or Google Drive  /mom-inbox
        ▼
laptop (systemd: hourly + on-login + on-file-arrival)
        │  rclone pulls the recording
        │  matches it to a calendar event by timestamp
        │  Groq Whisper → transcript
        │  Groq LLM     → structured minutes
        ▼
Google Doc — dated child tab under the right section tab,
with headings, bullets and a real Action-items table
```

## Features

- **Calendar-driven** — meetings on a dedicated Google Calendar define what gets processed, which doc it lands in, and when it becomes due (e.g. "next morning at 07:00").
- **Recording = opt-in** — an event with no recording is simply skipped, so the calendar can hold every meeting; only what you record gets minuted.
- **Multi-doc routing** — event-title keywords pick the destination doc (per project) and the tab within it (per sub-team); a catch-all doc takes everything else.
- **Real formatting** — minutes are rendered into native Google Docs structure: heading styles, bullet lists, bold, and an actual table for action items, inside a dated child tab (created via the Docs API's `addDocumentTab`).
- **Name correction** — a roster in the config steers Whisper and lets the summariser snap garbled names to correct spellings; unknown names are flagged `(?)` rather than guessed.
- **Robust by default** — idempotent runs under a lock file, per-meeting state in `processed.json`, automatic retry when the LLM truncates its output, ffmpeg chunking for recordings over 24 MB, and a laptop asleep at the due time just catches up at next login.

## Requirements

- Linux with systemd (macOS works with launchd equivalents; units not included)
- Python 3.9+, ffmpeg
- A Google account, a [Google Cloud](https://console.cloud.google.com) project (free), and a [Groq](https://console.groq.com) API key (free tier)
- [rclone](https://rclone.org) if recordings arrive via Dropbox/Google Drive

## Setup

### 1. Clone and install

```bash
git clone <this repo> ~/mom && cd ~/mom
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

### 2. Google service account

1. In Google Cloud Console, create a project and enable the **Google Docs API** and **Google Calendar API**.
2. Create a **service account** (no roles needed), add a **JSON key**, and save it as `service-account.json` next to the script (path configurable).
3. Note the service account's email — you'll share things with it below.

### 3. Calendar

1. Create a dedicated calendar (e.g. "meets") and put your meetings on it. Titles should contain a routing keyword ("ProjectX weekly", "Perception sync").
2. In the calendar's settings, **share it with the service account's email** ("See all event details") and copy the **Calendar ID** into `config.yaml`.

### 4. Google Doc(s)

1. Create a doc per project. Add a top-level tab for each section named in your `section_map` (tab names must match exactly).
2. **Share each doc with the service account as Editor** and put the doc IDs (from the URL) into the `docs:` list in `config.yaml`.

### 5. Recording inbox

The pipeline watches a local folder (`inbox_dir`). `mom_sync.sh` fills it by pulling from `dropbox:mom-inbox` and/or `gdrive:mom-inbox` and pushes processed results back to `gdrive:mom-done`; edit it to taste. Configure the remotes with `rclone config` (for Google Drive, create your own OAuth client ID — rclone's shared one is being retired; an "Internal"-type consent screen avoids verification entirely if you're on a Workspace account).

On iPhone, the frictionless combo is:

- **Start recording**: the Voice Memos control on the Lock Screen / Action Button — true background recording.
- **Upload**: a share-sheet shortcut — *Get Details of Files (Creation Date)* → *Format Date* `yyyy-MM-dd_HHmm` → *Set Name* `[Formatted Date].m4a` → *Save File* to Dropbox `/mom-inbox` (Dropbox is natively supported by Shortcuts; Google Drive needs the interactive picker).

The filename timestamp is what matches a recording to its calendar event (falling back to file mtime), with configurable slack on both sides.

### 6. Schedulers

```bash
cp systemd/* ~/.config/systemd/user/      # adjust paths inside if you didn't clone to ~/mom
systemctl --user daemon-reload
systemctl --user enable --now mom.timer mom.path mom-sync.timer
```

This runs the pipeline at login, hourly, and the moment a recording lands, and the sync every 10 minutes.

### 7. First run

```bash
./venv/bin/python mom_pipeline.py
```

A healthy run logs `nothing due (N recent events, 0 processed)`. For an end-to-end test, add a calendar event in the recent past, drop a matching recording (or a `.txt` transcript — those skip Whisper) into the inbox, temporarily set `run_after: "1m"`, and run again.

## How it decides what to do

| Question | Answer |
|---|---|
| Which meetings? | Events on the configured calendar within `lookback_days`, not yet in `processed.json` |
| When? | After `run_after` relative to the event end: `2h`, `30m`, `same_day 22:00`, `next_day 07:00` |
| Which recording? | Inbox file whose timestamp falls in `[start − slack_before, end + slack_after]`; `.txt` preferred over audio |
| Which doc? | First `docs:` entry with a `match` keyword in the event title; empty `match` = catch-all |
| Which tab? | First `section_map` keyword hit, else `default_section`; minutes go into a dated child tab (reused if it exists) |

Everything is re-read from `config.yaml` on each run — edits take effect immediately, no restarts.

## Troubleshooting

- **Calendar fetch fails with a Google "Sorry…" page** — you're using the secret iCal URL and Google is rate-limiting your network. Use `calendar_id` + service-account sharing instead (the preferred path).
- **`model_not_found` from Groq** — the configured model was retired. List current ones and update `llm_model`.
- **Minutes missing sections / empty action table** — the LLM truncated; the pipeline retries automatically and logs a warning. If it persists, shorten the transcript chunks or switch models.
- **Garbled names** — add the correct spellings to `team_names`.
- **Nothing happens** — `tail mom.log`, then `journalctl --user -u mom.service -u mom-sync.service`.

## Repo layout

```
mom_pipeline.py      the pipeline (calendar → match → transcribe → summarise → doc)
mom_sync.sh          rclone pull/push between cloud storage and the local folders
config.example.yaml  annotated template — copy to config.yaml (gitignored)
requirements.txt     python dependencies
systemd/             user units: pipeline timer/path-watcher + sync timer
```
