#!/usr/bin/env python3
"""
bulk_chart_importer.py
======================
Bulk-imports charts downloaded from Chart Cyanvas (or any chcy downloader) into
your own Chart Cyanvas instance.

Each chart folder is expected to contain:
  - level.json       → metadata (title, rating, artists, author, tags, …)
  - jacket.jpg       → cover image
  - music.mp3        → BGM audio
  - score.usc        → chart file  (also checks for .sus / .mmws / .chs / .ccmmws)

Usage
-----
1. Log into your Chart Cyanvas instance in a browser (via Sonolus auth).
2. Open DevTools → Application → Cookies and copy the value of the `_session_id`
   cookie (or whatever cookie name your instance uses).
3. Run:

     python3 bulk_chart_importer.py \\
       --base-url  http://localhost:3100 \\
       --session   "<paste _session_id cookie value here>" \\
       --charts-dir "/run/media/carter/AC6C576C6C573076/random projects/chart-downloader/out/chcy/" \\
       --author-handle "YOUR_SONOLUS_HANDLE"

Options
-------
  --base-url        Base URL of your Chart Cyanvas instance (no trailing slash)
  --session         Value of the _session_id cookie from your browser session
  --charts-dir      Path to the directory that contains the chcy-XXXX folders
  --author-handle   Your Sonolus handle (used as the chart author)
  --visibility      public | private | scheduled  (default: public)
  --genre           vocal_synth | music_game | game | meme | pops | instrumental | others
                    (default: others — can be overridden per-chart if level.json has it)
  --delay           Seconds to wait between uploads to avoid hammering the server (default: 1.5)
  --dry-run         Print what would be uploaded without actually uploading
  --limit           Only import the first N charts (useful for testing)
  --resume-after    Skip charts until this folder name is seen, then start importing
  --log-file        Path to write a JSON-lines progress log (default: import_log.jsonl)
"""

import argparse
import json
import mimetypes
import os
import sys
import time
import traceback
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHART_EXTENSIONS = [".usc", ".sus", ".mmws", ".chs", ".vusc", ".ccmmws"]

# Map chart extension → format name understood by the uploader
EXT_TO_TYPE = {
    ".usc": "vusc",
    ".sus": "sus",
    ".mmws": "mmws",
    ".chs": "chs",
    ".vusc": "vusc",
    ".ccmmws": "ccmmws",
}

VALID_GENRES = {
    "vocal_synth", "music_game", "game", "meme", "pops", "instrumental", "others"
}

VALID_VISIBILITIES = {"public", "private", "scheduled"}

# Tag icons / extra metadata in the Sonolus level.json tags that are NOT real tags
SKIP_TAG_ICONS = {"heart"}  # tags with an 'icon' field are system tags (like likes)


def find_chart_file(folder: Path) -> Path | None:
    """Return the first chart file found in *folder*, or None."""
    for ext in CHART_EXTENSIONS:
        # Common naming patterns
        for stem in ("score", "chart", folder.name):
            candidate = folder / f"{stem}{ext}"
            if candidate.exists():
                return candidate
    # Fallback: glob any file with a chart extension
    for ext in CHART_EXTENSIONS:
        matches = list(folder.glob(f"*{ext}"))
        if matches:
            return matches[0]
    return None


def parse_level_json(level_json_path: Path) -> dict:
    """Parse a Sonolus level.json into a dict suitable for the upload API."""
    with open(level_json_path, encoding="utf-8") as f:
        level = json.load(f)

    # --- title ---
    title = level.get("title", "Unknown Title")

    # --- rating ---
    rating = level.get("rating", 20)
    rating = max(1, min(99, int(rating)))

    # --- composers / artists ---
    # "artists" field in level.json is "composer / artist"
    artists_raw = level.get("artists", "")
    if " / " in artists_raw:
        parts = artists_raw.split(" / ", 1)
        composer = parts[0].strip()
        artist = parts[1].strip()
    else:
        composer = artists_raw.strip() or "Unknown"
        artist = ""

    # --- author name (display) ---
    # level.json author field is "name#handle"
    author_raw = level.get("author", "")
    if "#" in author_raw:
        author_display_name = author_raw.split("#")[0].strip()
    else:
        author_display_name = author_raw.strip()

    # --- tags (filter out system tags that have an 'icon' key) ---
    raw_tags = level.get("tags", [])
    tags = []
    for tag in raw_tags:
        if isinstance(tag, dict):
            if tag.get("icon") in SKIP_TAG_ICONS:
                continue
            tag_title = tag.get("title", "").strip()
            if tag_title:
                tags.append(tag_title)
        elif isinstance(tag, str) and tag.strip():
            tags.append(tag.strip())
    # The API caps tags at 5
    tags = tags[:5]

    return {
        "title": title,
        "rating": rating,
        "composer": composer,
        "artist": artist,
        "author_display_name": author_display_name,
        "tags": tags,
        "description": "",
    }


def get_session_cookie_name(base_url: str, session: requests.Session) -> str:
    """
    Try to detect the cookie name by hitting /api/login/session.
    Falls back to '_session_id'.
    """
    try:
        r = session.get(f"{base_url}/api/login/session", timeout=10)
        # Check what cookie came back (if any)
        for name in session.cookies.keys():
            if "session" in name.lower():
                return name
    except Exception:
        pass
    return "_session_id"


def build_session(base_url: str, session_cookie_value: str) -> requests.Session:
    """Create a requests.Session pre-loaded with the auth cookie."""
    s = requests.Session()
    # Try the most common Rails session cookie names
    for cookie_name in ["_session_id", "_chart_cyanvas_session", "chcy_session"]:
        s.cookies.set(cookie_name, session_cookie_value, domain=_url_host(base_url))
    s.headers["User-Agent"] = "bulk_chart_importer/1.0"
    return s


def _url_host(url: str) -> str:
    from urllib.parse import urlparse
    return urlparse(url).hostname or "localhost"


def verify_session(base_url: str, session: requests.Session) -> dict | None:
    """Returns the current user dict or None if not logged in."""
    try:
        r = session.get(f"{base_url}/api/login/session", timeout=15)
        data = r.json()
        if data.get("code") == "ok":
            return data.get("user")
    except Exception as e:
        print(f"[ERROR] Could not verify session: {e}")
    return None


def upload_chart(
    base_url: str,
    session: requests.Session,
    chart_dir: Path,
    author_handle: str,
    default_genre: str,
    default_visibility: str,
    dry_run: bool,
) -> dict:
    """
    Upload a single chart. Returns a result dict with keys:
      status: "ok" | "skipped" | "error"
      message: str
      chart_name: str (if ok)
    """
    result = {"folder": chart_dir.name, "status": "error", "message": "", "chart_name": None}

    # ------------------------------------------------------------------ files
    level_json = chart_dir / "level.json"
    if not level_json.exists():
        result["message"] = "Missing level.json"
        result["status"] = "skipped"
        return result

    chart_file = find_chart_file(chart_dir)
    if chart_file is None:
        result["message"] = "No chart file found (.usc/.sus/.mmws/…)"
        result["status"] = "skipped"
        return result

    # Cover: try jacket.jpg first, then any image
    cover_file = chart_dir / "jacket.jpg"
    if not cover_file.exists():
        for ext in (".jpg", ".jpeg", ".png", ".webp"):
            candidates = list(chart_dir.glob(f"*{ext}"))
            if candidates:
                cover_file = candidates[0]
                break
    if not cover_file.exists():
        result["message"] = "No cover image found"
        result["status"] = "skipped"
        return result

    # BGM: try music.mp3 first, then any audio
    bgm_file = chart_dir / "music.mp3"
    if not bgm_file.exists():
        for ext in (".mp3", ".ogg", ".wav", ".flac", ".m4a"):
            candidates = list(chart_dir.glob(f"*{ext}"))
            if candidates:
                bgm_file = candidates[0]
                break
    if not bgm_file.exists():
        result["message"] = "No BGM audio file found"
        result["status"] = "skipped"
        return result

    # ------------------------------------------------------------ parse metadata
    try:
        meta = parse_level_json(level_json)
    except Exception as e:
        result["message"] = f"Failed to parse level.json: {e}"
        return result

    # ----------------------------------------------------------------- dry run
    if dry_run:
        print(
            f"  [DRY-RUN] Would upload: {meta['title']!r} "
            f"(rating={meta['rating']}, chart={chart_file.name}, "
            f"cover={cover_file.name}, bgm={bgm_file.name})"
        )
        result["status"] = "ok"
        result["message"] = "dry-run"
        result["chart_name"] = "(dry-run)"
        return result

    # ------------------------------------------------------------------- upload
    data_payload = {
        "title": meta["title"],
        "composer": meta["composer"],
        "artist": meta["artist"],
        "description": meta["description"],
        "rating": meta["rating"],
        "genre": default_genre,
        "tags": meta["tags"],
        "authorHandle": author_handle,
        "authorName": meta["author_display_name"] or "",
        "isChartPublic": True,
        "visibility": default_visibility,
    }

    try:
        chart_mime = mimetypes.guess_type(str(chart_file))[0] or "application/octet-stream"
        cover_mime = mimetypes.guess_type(str(cover_file))[0] or "image/jpeg"
        bgm_mime = mimetypes.guess_type(str(bgm_file))[0] or "audio/mpeg"

        with open(chart_file, "rb") as cf, open(cover_file, "rb") as cof, open(bgm_file, "rb") as bf:
            files = {
                "data": (None, json.dumps(data_payload), "application/json"),
                "chart": (chart_file.name, cf, chart_mime),
                "cover": (cover_file.name, cof, cover_mime),
                "bgm": (bgm_file.name, bf, bgm_mime),
            }
            r = session.post(
                f"{base_url}/api/charts",
                files=files,
                timeout=120,
            )

        if r.status_code == 200:
            resp = r.json()
            if resp.get("code") == "ok":
                chart_name = resp["chart"]["name"]
                result["status"] = "ok"
                result["message"] = "Uploaded successfully"
                result["chart_name"] = chart_name
            else:
                result["message"] = f"API error: {resp}"
        elif r.status_code == 401:
            result["message"] = "Not logged in — session cookie may have expired"
            result["status"] = "auth_error"
        elif r.status_code == 403:
            resp_text = r.text
            result["message"] = f"Forbidden (403): {resp_text}"
        else:
            result["message"] = f"HTTP {r.status_code}: {r.text[:300]}"

    except requests.exceptions.Timeout:
        result["message"] = "Request timed out (server may still be processing)"
    except Exception as e:
        result["message"] = f"Exception: {e}\n{traceback.format_exc()}"

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Bulk import charts into a Chart Cyanvas instance.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--base-url", default="http://localhost:3100",
                        help="Base URL of your Chart Cyanvas instance")
    parser.add_argument("--session", required=True,
                        help="Value of the _session_id cookie (from browser DevTools)")
    parser.add_argument(
        "--charts-dir",
        default="/run/media/carter/AC6C576C6C573076/random projects/chart-downloader/out/chcy/",
        help="Directory containing the chcy-XXXX chart folders",
    )
    parser.add_argument("--author-handle", required=True,
                        help="Your Sonolus handle (used as chart author)")
    parser.add_argument("--visibility", default="public", choices=list(VALID_VISIBILITIES),
                        help="Visibility for uploaded charts (default: public)")
    parser.add_argument("--genre", default="others", choices=list(VALID_GENRES),
                        help="Default genre for all charts (default: others)")
    parser.add_argument("--delay", type=float, default=1.5,
                        help="Seconds to wait between uploads (default: 1.5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview what would be uploaded without actually uploading")
    parser.add_argument("--limit", type=int, default=0,
                        help="Stop after importing this many charts (0 = no limit)")
    parser.add_argument("--resume-after", default="",
                        help="Skip all folders up to and including this folder name, then start")
    parser.add_argument("--log-file", default="import_log.jsonl",
                        help="Path to JSON-lines progress log (default: import_log.jsonl)")

    args = parser.parse_args()

    charts_dir = Path(args.charts_dir)
    if not charts_dir.exists():
        print(f"[ERROR] Charts directory not found: {charts_dir}")
        sys.exit(1)

    # ---------------------------------------------------------------- session
    print(f"Connecting to {args.base_url} …")
    session = build_session(args.base_url, args.session)

    if not args.dry_run:
        user = verify_session(args.base_url, session)
        if user is None:
            print(
                "[ERROR] Could not verify session. Make sure:\n"
                "  1. The server is running\n"
                "  2. You copied the correct session cookie value\n"
                "  3. Your --base-url is correct (try http://localhost:3100)\n"
                "\nTo get your session cookie:\n"
                "  1. Log into Chart Cyanvas in your browser\n"
                "  2. Open DevTools (F12) → Application → Cookies\n"
                "  3. Copy the value of the '_session_id' cookie"
            )
            sys.exit(1)
        print(f"Logged in as: {user.get('name', '?')} (handle: {user.get('handle', '?')})")
    else:
        print("[DRY-RUN MODE] Skipping session verification.")

    # ------------------------------------------------------- discover folders
    chart_dirs = sorted([
        d for d in charts_dir.iterdir()
        if d.is_dir() and d.name.startswith("chcy-")
    ])
    total = len(chart_dirs)
    print(f"Found {total} chart folders in {charts_dir}")

    if args.resume_after:
        skip_until = args.resume_after
        original_count = len(chart_dirs)
        skipping = True
        filtered = []
        for d in chart_dirs:
            if skipping:
                if d.name == skip_until:
                    skipping = False  # start on the NEXT one
                continue
            filtered.append(d)
        chart_dirs = filtered
        print(f"Resuming after '{skip_until}': {len(chart_dirs)} folders remaining "
              f"(skipped {original_count - len(chart_dirs)})")

    if args.limit > 0:
        chart_dirs = chart_dirs[:args.limit]
        print(f"Limiting to first {args.limit} chart(s).")

    # ------------------------------------------------------------ import loop
    log_path = Path(args.log_file)
    ok_count = 0
    skip_count = 0
    error_count = 0

    with open(log_path, "a", encoding="utf-8") as log_f:
        for i, chart_dir in enumerate(chart_dirs, 1):
            prefix = f"[{i}/{len(chart_dirs)}] {chart_dir.name}"
            print(f"\n{prefix}")

            result = upload_chart(
                base_url=args.base_url,
                session=session,
                chart_dir=chart_dir,
                author_handle=args.author_handle,
                default_genre=args.genre,
                default_visibility=args.visibility,
                dry_run=args.dry_run,
            )

            # Log result
            log_entry = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), **result}
            log_f.write(json.dumps(log_entry, ensure_ascii=False) + "\n")
            log_f.flush()

            if result["status"] == "ok":
                ok_count += 1
                chart_id = result.get("chart_name", "")
                print(f"  ✓ OK — chart ID: {chart_id}")
            elif result["status"] == "skipped":
                skip_count += 1
                print(f"  ⚠ Skipped — {result['message']}")
            elif result["status"] == "auth_error":
                print(f"  ✗ AUTH ERROR — {result['message']}")
                print("Session expired. Stopping. Re-run with a fresh --session cookie.")
                error_count += 1
                break
            else:
                error_count += 1
                print(f"  ✗ Error — {result['message']}")

            if i < len(chart_dirs) and not args.dry_run:
                time.sleep(args.delay)

    # ---------------------------------------------------------------- summary
    print(f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Import complete!
  ✓ Uploaded:  {ok_count}
  ⚠ Skipped:  {skip_count}
  ✗ Errors:   {error_count}
  Total:       {len(chart_dirs)}

Log written to: {log_path.resolve()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


if __name__ == "__main__":
    main()
