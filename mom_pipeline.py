#!/usr/bin/env python3
"""
mom_pipeline.py — calendar-driven meeting-minutes pipeline.

Each run:
  1. Reads the "meets" Google Calendar (secret iCal URL).
  2. Finds meetings that are "due" (end time + delay, or next-day time) and not yet processed.
  3. Matches each to a recording/transcript in the inbox folder by timestamp.
  4. Transcribes audio via Groq Whisper (skipped for .txt), summarises via a Groq-hosted open model.
  5. Writes the minutes into the shared Google Doc, into the right tab.
  6. Marks the meeting processed and moves the file to the done folder.

Idempotent and safe to trigger as often as you like (login, wake, file-appeared, hourly).
ffmpeg is needed only for recordings > 24 MB (auto-chunking).
"""

import fcntl
import json
import logging
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml
from icalendar import Calendar
import recurring_ical_events
from openai import OpenAI
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

HERE = Path(__file__).resolve().parent
CFG = yaml.safe_load((HERE / "config.yaml").read_text())
TZ = ZoneInfo(CFG.get("timezone", "Asia/Kolkata"))

INBOX = Path(CFG["inbox_dir"]).expanduser()
DONE = Path(CFG["done_dir"]).expanduser()
STATE = HERE / "processed.json"
LOCK = HERE / ".lock"
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".mp4", ".aac", ".ogg", ".webm"}
TEXT_EXT = {".txt", ".md"}
MAX_UPLOAD_BYTES = 24 * 1024 * 1024

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(HERE / "mom.log")],
)
log = logging.getLogger("mom")

groq = OpenAI(api_key=CFG["groq_api_key"], base_url="https://api.groq.com/openai/v1")


# ----------------------------------------------------------------------------- helpers
def now():
    return datetime.now(TZ)


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state):
    STATE.write_text(json.dumps(state, indent=2))


def as_local(dt):
    """Make a datetime tz-aware in the configured timezone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


# ----------------------------------------------------------------------------- calendar
def load_events():
    """Return recent events from the meets calendar as dicts. All-day events are skipped.
    Prefers the Calendar API via the service account (`calendar_id` in config); the
    secret iCal URL is a fallback, since Google rate-limits it on shared networks."""
    lookback = now() - timedelta(days=CFG.get("lookback_days", 7))
    horizon = now() + timedelta(hours=1)
    if CFG.get("calendar_id"):
        return _events_from_api(lookback, horizon)
    return _events_from_ical(lookback, horizon)


def _events_from_api(lookback, horizon):
    creds = service_account.Credentials.from_service_account_file(
        str(Path(CFG["service_account_json"]).expanduser()),
        scopes=["https://www.googleapis.com/auth/calendar.readonly"],
    )
    cal = build("calendar", "v3", credentials=creds, cache_discovery=False)
    resp = cal.events().list(
        calendarId=CFG["calendar_id"],
        timeMin=lookback.isoformat(),
        timeMax=horizon.isoformat(),
        singleEvents=True,          # expands recurring events into instances
        orderBy="startTime",
        maxResults=250,
    ).execute()
    events = []
    for item in resp.get("items", []):
        start_s = item.get("start", {}).get("dateTime")
        if not start_s:  # all-day event
            continue
        end_s = item.get("end", {}).get("dateTime") or start_s
        start = as_local(datetime.fromisoformat(start_s.replace("Z", "+00:00")))
        end = as_local(datetime.fromisoformat(end_s.replace("Z", "+00:00")))
        events.append({
            "uid": f"{item['id']}::{start.isoformat()}",
            "summary": (item.get("summary") or "").strip(),
            "start": start,
            "end": end,
        })
    return events


def _events_from_ical(lookback, horizon):
    ics = requests.get(CFG["ical_url"], timeout=30).content
    cal = Calendar.from_ical(ics)
    events = []
    for ev in recurring_ical_events.of(cal).between(lookback, horizon):
        start = ev.get("DTSTART").dt
        end = ev.get("DTEND").dt if ev.get("DTEND") else start
        if not isinstance(start, datetime):  # all-day event
            continue
        start, end = as_local(start), as_local(end)
        events.append({
            "uid": f"{ev.get('UID')}::{start.isoformat()}",  # unique per recurrence instance
            "summary": str(ev.get("SUMMARY", "")).strip(),
            "start": start,
            "end": end,
        })
    return events


def due_at(ev):
    """When this meeting becomes eligible for processing, per config `run_after`.
    Formats: "2h", "30m", "same_day 22:00", "next_day 07:00"."""
    rule = str(CFG.get("run_after", "2h")).strip()
    m = re.fullmatch(r"(\d+)\s*([hm])", rule)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return ev["end"] + (timedelta(hours=n) if unit == "h" else timedelta(minutes=n))
    m = re.fullmatch(r"(same_day|next_day)\s+(\d{1,2}):(\d{2})", rule)
    if m:
        hh, mm = int(m.group(2)), int(m.group(3))
        t = ev["end"].replace(hour=hh, minute=mm, second=0, microsecond=0)
        if m.group(1) == "next_day" or t <= ev["end"]:
            t += timedelta(days=1)
        return t
    raise ValueError(f"Bad run_after rule: {rule}")


def _doc_routes():
    """Doc routing table from config. Supports the legacy single-doc keys."""
    if CFG.get("docs"):
        return [d for d in CFG["docs"] if not str(d.get("doc_id", "")).startswith("FILL")]
    return [{
        "doc_id": CFG["doc_id"],
        "match": [],
        "section_map": CFG.get("section_map", {}),
        "default_section": CFG.get("default_section", "General"),
    }]


def route_for(ev):
    """Pick (doc_id, section) for an event: the first doc with a `match` keyword in the
    title wins; otherwise the catch-all doc (empty `match`). Within the doc, the tab is
    chosen by its section_map, falling back to its default_section."""
    title = ev["summary"].lower()
    docs = _doc_routes()
    doc = next((d for d in docs if any(k.lower() in title for k in d.get("match", []))), None)
    if doc is None:
        doc = next((d for d in docs if not d.get("match")), None)
    if doc is None:
        return None, None
    section = doc.get("default_section", "General")
    for tab, keywords in doc.get("section_map", {}).items():
        if any(k.lower() in title for k in keywords):
            section = tab
            break
    return doc["doc_id"], section


# ----------------------------------------------------------------------------- inbox
_TS_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[_T \-]?(\d{2})[:\-.]?(\d{2})")


def file_timestamp(p: Path):
    """Timestamp from filename (YYYY-MM-DD_HHMM etc.), else file mtime."""
    m = _TS_RE.search(p.stem)
    if m:
        y, mo, d, hh, mm = map(int, m.groups())
        return datetime(y, mo, d, hh, mm, tzinfo=TZ)
    return datetime.fromtimestamp(p.stat().st_mtime, TZ)


def is_stable(p: Path):
    """True if the file isn't still being synced (size unchanged over a short wait)."""
    import time
    s1 = p.stat().st_size
    time.sleep(3)
    return s1 == p.stat().st_size and s1 > 0


def find_recording(ev):
    """Find the inbox file recorded during this meeting (with slack either side)."""
    lo = ev["start"] - timedelta(minutes=CFG.get("match_slack_before_min", 20))
    hi = ev["end"] + timedelta(minutes=CFG.get("match_slack_after_min", 120))
    candidates = []
    for p in INBOX.iterdir():
        if p.name.startswith(".") or p.suffix.lower() not in AUDIO_EXT | TEXT_EXT:
            continue
        ts = file_timestamp(p)
        if lo <= ts <= hi:
            candidates.append((ts, p))
    if not candidates:
        return None
    candidates.sort()
    # Prefer a text transcript if both audio and text exist for the same meeting.
    for _, p in candidates:
        if p.suffix.lower() in TEXT_EXT:
            return p
    return candidates[0][1]


# ----------------------------------------------------------------------------- transcription
def split_audio(src: Path, workdir: Path):
    """Chunk with ffmpeg into ~10-minute low-bitrate pieces so each is under the upload limit."""
    if not shutil.which("ffmpeg"):
        raise RuntimeError(f"{src.name} exceeds 24 MB and ffmpeg is not installed to split it")
    pattern = workdir / "chunk_%03d.m4a"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-i", str(src), "-f", "segment",
         "-segment_time", "600", "-c:a", "aac", "-b:a", "48k", "-ac", "1", str(pattern)],
        check=True,
    )
    return sorted(workdir.glob("chunk_*.m4a"))


def transcribe(p: Path) -> str:
    if p.suffix.lower() in TEXT_EXT:
        return p.read_text(errors="ignore")

    with tempfile.TemporaryDirectory() as td:
        parts = [p] if p.stat().st_size <= MAX_UPLOAD_BYTES else split_audio(p, Path(td))
        out = []
        for part in parts:
            log.info("transcribing %s (%.1f MB)", part.name, part.stat().st_size / 1e6)
            names = ", ".join(CFG.get("team_names", []))
            w_prompt = CFG.get("whisper_prompt", "")
            if names:
                w_prompt = f"{w_prompt} Participants: {names}."
            with open(part, "rb") as f:
                text = groq.audio.transcriptions.create(
                    model=CFG.get("whisper_model", "whisper-large-v3-turbo"),
                    file=f,
                    response_format="text",
                    language=CFG.get("language", "en"),
                    prompt=w_prompt,
                )
            out.append(text if isinstance(text, str) else text.text)
        return "\n".join(out)


# ----------------------------------------------------------------------------- summarisation
def summarise(transcript: str, ev, section: str) -> str:
    system = CFG["mom_prompt"].format(
        section=section,
        title=ev["summary"],
        date=ev["start"].strftime("%A %d %b %Y"),
        start=ev["start"].strftime("%H:%M"),
        end=ev["end"].strftime("%H:%M"),
        names=", ".join(CFG.get("team_names", [])) or "none provided",
    )
    out = ""
    for attempt in range(1, 4):
        resp = groq.chat.completions.create(
            model=CFG.get("llm_model", "openai/gpt-oss-120b"),
            temperature=0.2,
            max_tokens=8192,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"TRANSCRIPT:\n\n{transcript}"},
            ],
        )
        out = (resp.choices[0].message.content or "").strip()
        finish = resp.choices[0].finish_reason
        # complete output = clean finish + action table with at least one data row
        data_rows = [l for l in out.splitlines()
                     if l.lstrip().startswith("|")
                     and not all(set(c.strip()) <= set("-: ")
                                 for c in l.strip().strip("|").split("|"))]
        if finish == "stop" and len(data_rows) >= 2:
            return out
        log.warning("summary attempt %d incomplete (finish_reason=%s, table rows=%d); retrying",
                    attempt, finish, len(data_rows))
    log.error("summary still incomplete after retries; using last attempt")
    return out


# ----------------------------------------------------------------------------- google docs
def docs_service():
    creds = service_account.Credentials.from_service_account_file(
        str(Path(CFG["service_account_json"]).expanduser()),
        scopes=["https://www.googleapis.com/auth/documents"],
    )
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _walk_tabs(tabs):
    for t in tabs:
        yield t
        yield from _walk_tabs(t.get("childTabs", []))


def create_child_tab(docs, doc_id: str, section_tab_id: str, title: str) -> str:
    """Create a child tab under the section tab and return its tabId."""
    resp = docs.documents().batchUpdate(
        documentId=doc_id,
        body={"requests": [{"addDocumentTab": {
            "tabProperties": {"title": title, "parentTabId": section_tab_id}}}]},
    ).execute()
    return resp["replies"][0]["addDocumentTab"]["tabProperties"]["tabId"]


_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _parse_markdown(md: str):
    """Parse the constrained minutes markdown ("## " headings, "- " bullets, **bold**,
    one trailing pipe table) into (text, h2s, bolds, lists, table_rows). Span offsets
    are relative to the start of `text`; the table is returned as rows of cells and is
    rendered separately as a real Docs table."""
    parts, h2s, bolds, lists, table_rows = [], [], [], [], []
    pos = 0
    list_start = None

    def emit(line):
        nonlocal pos
        clean, cursor = [], 0
        for m in _MD_BOLD_RE.finditer(line):
            clean.append(line[cursor:m.start()])
            s = pos + sum(len(c) for c in clean)
            bolds.append((s, s + len(m.group(1))))
            clean.append(m.group(1))
            cursor = m.end()
        clean.append(line[cursor:])
        out = "".join(clean) + "\n"
        parts.append(out)
        start = pos
        pos += len(out)
        return start, pos

    def close_list():
        nonlocal list_start
        if list_start is not None:
            lists.append((list_start, pos))
            list_start = None

    for raw in md.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("|"):
            close_list()
            cells = [c.strip().replace("**", "")
                     for c in line.strip().strip("|").split("|")]
            if cells and not all(set(c) <= set("-: ") for c in cells):  # skip |---|
                table_rows.append(cells)
            continue
        if line.startswith("## "):
            close_list()
            if parts:
                emit("")  # blank line before each heading
            s, e = emit(line[3:].replace("**", ""))
            h2s.append((s, e))
            continue
        stripped = line.lstrip()
        if stripped.startswith(("- ", "* ")):
            s, e = emit(stripped[2:])
            if list_start is None:
                list_start = s
            continue
        close_list()
        if line:
            emit(line)
    close_list()
    return "".join(parts), h2s, bolds, lists, table_rows


def _append_rendered(docs, doc_id, tab_id, insert_at, minutes_md, sep=False, lead=None):
    """Insert the rendered minutes at `insert_at` in the tab (optionally preceded by a
    blank line and a HEADING_1 `lead` line), then append the action-items table at the
    end of the tab."""
    text, h2s, bolds, lists, table_rows = _parse_markdown(minutes_md)
    prefix = ("\n" if sep else "") + (lead + "\n" if lead else "")
    shift = len(prefix)
    text = prefix + text

    def rng(s, e):
        return {"tabId": tab_id, "startIndex": insert_at + shift + s,
                "endIndex": insert_at + shift + e}

    requests_ = [{"insertText": {"location": {"tabId": tab_id, "index": insert_at},
                                 "text": text}}]
    if lead:
        lead_start = insert_at + (1 if sep else 0)
        requests_.append({"updateParagraphStyle": {
            "range": {"tabId": tab_id, "startIndex": lead_start,
                      "endIndex": insert_at + shift},
            "paragraphStyle": {"namedStyleType": "HEADING_1"},
            "fields": "namedStyleType"}})
    for s, e in h2s:
        requests_.append({"updateParagraphStyle": {
            "range": rng(s, e), "paragraphStyle": {"namedStyleType": "HEADING_2"},
            "fields": "namedStyleType"}})
    for s, e in lists:
        requests_.append({"createParagraphBullets": {
            "range": rng(s, e), "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}})
    for s, e in bolds:
        requests_.append({"updateTextStyle": {
            "range": rng(s, e), "textStyle": {"bold": True}, "fields": "bold"}})
    docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests_}).execute()

    if table_rows:
        cols = max(len(r) for r in table_rows)
        rows = [(r + [""] * cols)[:cols] for r in table_rows]
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": [
            {"insertTable": {"rows": len(rows), "columns": cols,
                             "endOfSegmentLocation": {"tabId": tab_id}}}]}).execute()
        _fill_last_table(docs, doc_id, tab_id, rows)


def _fill_last_table(docs, doc_id, tab_id, rows):
    """Fill the most recently inserted table in the tab, bolding the header row.
    Cells are filled in reverse document order so earlier indices stay valid."""
    doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    tab = next(t for t in _walk_tabs(doc.get("tabs", []))
               if t["tabProperties"]["tabId"] == tab_id)
    table = None
    for el in tab["documentTab"]["body"]["content"]:
        if "table" in el:
            table = el["table"]
    if table is None:
        return
    cells = []
    for r, trow in enumerate(table["tableRows"]):
        for c, tcell in enumerate(trow["tableCells"]):
            cells.append((r, c, tcell["content"][-1]["endIndex"] - 1))
    requests_ = []
    for r, c, idx in sorted(cells, key=lambda x: -x[2]):
        if r < len(rows) and c < len(rows[r]) and rows[r][c]:
            requests_.append({"insertText": {
                "location": {"tabId": tab_id, "index": idx}, "text": rows[r][c]}})
            if r == 0:
                requests_.append({"updateTextStyle": {
                    "range": {"tabId": tab_id, "startIndex": idx,
                              "endIndex": idx + len(rows[r][c])},
                    "textStyle": {"bold": True}, "fields": "bold"}})
    if requests_:
        docs.documents().batchUpdate(documentId=doc_id, body={"requests": requests_}).execute()


def write_to_doc(doc_id: str, section: str, heading: str, minutes: str):
    """Write minutes into a dated child tab under the section's tab (e.g. "29 Sep"),
    reusing the child tab if it already exists, else creating it. If tab creation is
    rejected by the API, fall back to appending a dated H1 to the section tab."""
    docs = docs_service()
    doc = docs.documents().get(documentId=doc_id, includeTabsContent=True).execute()
    tabs = list(_walk_tabs(doc.get("tabs", [])))

    section_tab = next(
        (t for t in tabs if t["tabProperties"]["title"].strip().lower() == section.lower()), None
    )
    if section_tab is None:
        raise RuntimeError(f"No tab named '{section}' in the doc; tabs are: "
                           + ", ".join(t["tabProperties"]["title"] for t in tabs))

    prefix = CFG.get("draft_prefix", "")
    existing = next(
        (t for t in section_tab.get("childTabs", [])
         if t["tabProperties"]["title"].strip().lower()
         in (heading.lower(), (prefix + heading).lower())),
        None,
    )
    if existing is not None:
        tab_id = existing["tabProperties"]["tabId"]
        end_index = existing["documentTab"]["body"]["content"][-1]["endIndex"] - 1
        _append_rendered(docs, doc_id, tab_id, end_index, minutes, sep=True)
        log.info("appended minutes to existing tab '%s' under '%s'",
                 existing["tabProperties"]["title"], section)
        return

    try:
        tab_id = create_child_tab(docs, doc_id, section_tab["tabProperties"]["tabId"],
                                  prefix + heading)
    except HttpError as e:
        log.warning("child-tab creation failed (%s); falling back to heading", e)
        tab_id = None

    if tab_id is not None:
        # a fresh tab holds one empty paragraph; its body starts at index 1
        _append_rendered(docs, doc_id, tab_id, 1, minutes)
        log.info("created child tab '%s' under '%s'", prefix + heading, section)
        return

    tab_id = section_tab["tabProperties"]["tabId"]
    end_index = section_tab["documentTab"]["body"]["content"][-1]["endIndex"] - 1
    _append_rendered(docs, doc_id, tab_id, end_index, minutes,
                     sep=True, lead=prefix + heading)
    log.info("wrote minutes under dated heading in tab '%s'", section)


# ----------------------------------------------------------------------------- main
def process(ev, state):
    doc_id, section = route_for(ev)
    if doc_id is None:
        log.info("no doc route for '%s'; configure a catch-all doc to process it",
                 ev["summary"])
        return
    rec = find_recording(ev)
    if rec is None:
        log.info("no recording yet for '%s' (%s); will retry next run", ev["summary"], ev["start"])
        return
    if not is_stable(rec):
        log.info("%s still syncing; will retry", rec.name)
        return

    log.info("processing '%s' -> section %s, file %s", ev["summary"], section, rec.name)
    transcript = transcribe(rec)
    minutes = summarise(transcript, ev, section)

    heading = ev["start"].strftime(CFG.get("heading_format", "%d %b"))  # e.g. "29 Sep"
    write_to_doc(doc_id, section, heading, minutes)

    DONE.mkdir(parents=True, exist_ok=True)
    stem = f"{section}_{ev['start'].strftime('%Y-%m-%d_%H%M')}"
    shutil.move(str(rec), DONE / f"{stem}{rec.suffix}")
    (DONE / f"{stem}.transcript.txt").write_text(transcript)
    (DONE / f"{stem}.minutes.md").write_text(minutes)

    state[ev["uid"]] = {
        "processed_at": now().isoformat(), "section": section, "file": rec.name,
    }
    save_state(state)


def main():
    INBOX.mkdir(parents=True, exist_ok=True)
    with open(LOCK, "w") as lockf:
        try:
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("another run is in progress; exiting")
            return

        state = load_state()
        try:
            events = load_events()
        except Exception as e:
            log.error("calendar fetch failed: %s", e)
            return
        pending = [e for e in events if e["uid"] not in state and due_at(e) <= now()]
        if not pending:
            log.info("nothing due (%d recent events, %d processed)", len(events), len(state))
            return
        for ev in sorted(pending, key=lambda e: e["start"]):
            try:
                process(ev, state)
            except Exception:
                log.exception("failed on '%s' %s", ev["summary"], ev["start"])
                (INBOX / f"{ev['start']:%Y-%m-%d_%H%M}.error").write_text(
                    f"{ev['summary']}: see mom.log")


if __name__ == "__main__":
    main()
