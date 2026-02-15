#!/usr/bin/env python3
"""
YouTube Channel Transcript Monitor (yt-dlp)
=============================================
Monitors a YouTube channel every 15 minutes for new videos
and downloads their transcripts into organized folders.

Requirements:
    pip install yt-dlp

Usage:
    python youtube_transcript_monitor.py --channel @mkbhd
    python youtube_transcript_monitor.py --channel "https://www.youtube.com/@mkbhd" --once
    python youtube_transcript_monitor.py --channel UCBcRF18a7Qf58cCRy5xuWwQ --interval 300
"""

import argparse
import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ─── Configuration ───────────────────────────────────────────────────────────

DEFAULT_CHECK_INTERVAL = 900  # 15 minutes
DEFAULT_OUTPUT_DIR = "transcripts"
STATE_FILE = ".monitor_state.json"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(message)s"
MAX_VIDEOS_PER_CHECK = 20

# ─── Logging ─────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("yt-monitor")

# ─── Graceful shutdown ───────────────────────────────────────────────────────

shutdown_requested = False


def handle_signal(signum, frame):
    global shutdown_requested
    logger.info("Shutdown requested. Finishing current cycle...")
    shutdown_requested = True


signal.signal(signal.SIGINT, handle_signal)
signal.signal(signal.SIGTERM, handle_signal)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def sanitize_filename(name: str) -> str:
    """Remove or replace characters unsafe for filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:150]


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def build_channel_url(identifier: str) -> str:
    """Normalize a channel identifier into a URL yt-dlp can use."""
    if identifier.startswith(("http://", "https://")):
        return identifier
    if re.match(r"^UC[\w-]{22}$", identifier):
        return f"https://www.youtube.com/channel/{identifier}"
    if identifier.startswith("@"):
        return f"https://www.youtube.com/{identifier}"
    return f"https://www.youtube.com/@{identifier}"


# ─── yt-dlp interaction ─────────────────────────────────────────────────────


def fetch_channel_info(channel_url: str) -> str:
    """Get the channel name via yt-dlp."""
    cmd = [
        "yt-dlp",
        "--playlist-items", "1",
        "--flat-list",
        "--print", "%(channel)s",
        "--no-warnings",
        f"{channel_url}/videos",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    name = result.stdout.strip().split("\n")[0]
    return sanitize_filename(name) if name else "Unknown Channel"


def fetch_recent_video_ids(channel_url: str, max_count: int = MAX_VIDEOS_PER_CHECK) -> list[str]:
    """Fetch the most recent video IDs from a channel."""
    cmd = [
        "yt-dlp",
        "--flat-list",
        "--playlist-items", f"1:{max_count}",
        "--print", "%(id)s",
        "--no-warnings",
        f"{channel_url}/videos",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        logger.error(f"yt-dlp error: {result.stderr.strip()}")
        return []
    ids = [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]
    return ids


def fetch_video_metadata(video_id: str) -> dict | None:
    """Get title, upload date, description for a single video."""
    cmd = [
        "yt-dlp",
        "--skip-download",
        "--no-warnings",
        "--print",
        '{"title":%(title)j,"upload_date":%(upload_date)j,"description":%(description)j,"channel":%(channel)j}',
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        logger.warning(f"Could not fetch metadata for {video_id}: {result.stderr.strip()}")
        return None
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        logger.warning(f"Bad metadata JSON for {video_id}")
        return None


def fetch_transcript(video_id: str, languages: list[str] | None = None) -> list[dict] | None:
    """
    Download subtitles via yt-dlp and parse the resulting JSON3 file.
    Prefers manual subs, falls back to auto-generated.
    """
    tmp_dir = Path(f"/tmp/yt_subs_{video_id}")
    tmp_dir.mkdir(exist_ok=True)

    lang_arg = ",".join(languages) if languages else "en,en-US"

    # Try manual subtitles first, then auto-generated
    for sub_flag in ("--write-subs", "--write-auto-subs"):
        cmd = [
            "yt-dlp",
            "--skip-download",
            "--no-warnings",
            sub_flag,
            "--sub-langs", lang_arg,
            "--sub-format", "json3",
            "--output", str(tmp_dir / "%(id)s.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=120)

        # Look for downloaded subtitle files
        sub_files = list(tmp_dir.glob("*.json3"))
        if sub_files:
            transcript = parse_json3(sub_files[0])
            # Cleanup
            for f in tmp_dir.iterdir():
                f.unlink()
            tmp_dir.rmdir()
            return transcript

    # Cleanup on failure
    for f in tmp_dir.iterdir():
        f.unlink()
    tmp_dir.rmdir()
    return None


def parse_json3(filepath: Path) -> list[dict]:
    """Parse YouTube's json3 subtitle format into simple segments."""
    with open(filepath, "r", encoding="utf-8") as f:
        data = json.load(f)

    segments = []
    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        text = "".join(s.get("utf8", "") for s in segs).strip()
        text = text.replace("\n", " ")
        if not text:
            continue

        start_ms = event.get("tStartMs", 0)
        dur_ms = event.get("dDurationMs", 0)
        segments.append(
            {
                "start": start_ms / 1000.0,
                "duration": dur_ms / 1000.0,
                "text": text,
            }
        )
    return segments


# ─── Saving transcripts ─────────────────────────────────────────────────────


def save_transcript(transcript: list[dict], video_id: str, metadata: dict, output_dir: Path):
    """
    Save transcript in multiple formats:

    transcripts/
    └── Channel Name/
        └── 2025/
            └── 2025-01-15/
                ├── Video Title.txt              (plain readable text)
                ├── Video Title.timestamped.txt   (with timestamps)
                └── Video Title.json              (raw data)
    """
    upload_date = metadata.get("upload_date", "19700101")
    try:
        pub_date = datetime.strptime(upload_date, "%Y%m%d")
    except ValueError:
        pub_date = datetime.now()

    year_str = str(pub_date.year)
    date_str = pub_date.strftime("%Y-%m-%d")
    safe_title = sanitize_filename(metadata.get("title", video_id))

    day_dir = output_dir / year_str / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # 1. Plain text
    plain_path = day_dir / f"{safe_title}.txt"
    with open(plain_path, "w", encoding="utf-8") as f:
        f.write(f"Title: {metadata.get('title', 'Unknown')}\n")
        f.write(f"URL: {video_url}\n")
        f.write(f"Date: {date_str}\n")
        f.write(f"{'=' * 60}\n\n")
        full_text = " ".join(seg["text"] for seg in transcript)
        sentences = re.split(r"(?<=[.!?])\s+", full_text)
        for i in range(0, len(sentences), 5):
            paragraph = " ".join(sentences[i : i + 5])
            f.write(paragraph + "\n\n")
    logger.info(f"  → {plain_path}")

    # 2. Timestamped text
    ts_path = day_dir / f"{safe_title}.timestamped.txt"
    with open(ts_path, "w", encoding="utf-8") as f:
        f.write(f"Title: {metadata.get('title', 'Unknown')}\n")
        f.write(f"URL: {video_url}\n")
        f.write(f"Date: {date_str}\n")
        f.write(f"{'=' * 60}\n\n")
        for seg in transcript:
            ts = format_timestamp(seg["start"])
            f.write(f"[{ts}] {seg['text']}\n")
    logger.info(f"  → {ts_path}")

    # 3. Raw JSON
    json_path = day_dir / f"{safe_title}.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "video_id": video_id,
                "title": metadata.get("title"),
                "upload_date": date_str,
                "channel": metadata.get("channel"),
                "url": video_url,
                "transcript": transcript,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    logger.info(f"  → {json_path}")


# ─── State persistence ───────────────────────────────────────────────────────


def load_state(state_path: Path) -> set[str]:
    if state_path.exists():
        with open(state_path, "r") as f:
            data = json.load(f)
        return set(data.get("processed_ids", []))
    return set()


def save_state(state_path: Path, processed_ids: set[str]):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w") as f:
        json.dump(
            {
                "processed_ids": sorted(processed_ids),
                "last_updated": datetime.now(timezone.utc).isoformat(),
            },
            f,
            indent=2,
        )


# ─── Main loop ───────────────────────────────────────────────────────────────


def run_check(
    channel_url: str,
    channel_name: str,
    output_dir: Path,
    state_path: Path,
    languages: list[str] | None,
):
    """Single check cycle: fetch recent videos, download new transcripts."""
    processed_ids = load_state(state_path)
    video_ids = fetch_recent_video_ids(channel_url)

    if not video_ids:
        logger.info("No videos found (or yt-dlp error).")
        return

    new_count = 0
    for vid in video_ids:
        if shutdown_requested:
            break
        if vid in processed_ids:
            continue

        metadata = fetch_video_metadata(vid)
        title = metadata.get("title", vid) if metadata else vid
        logger.info(f'New video: "{title}" ({vid})')

        transcript = fetch_transcript(vid, languages)
        if transcript:
            channel_dir = output_dir / channel_name
            save_transcript(transcript, vid, metadata or {}, channel_dir)
            new_count += 1
        else:
            logger.info("  Skipped (no transcript available)")

        processed_ids.add(vid)

    save_state(state_path, processed_ids)

    if new_count == 0:
        logger.info("No new transcripts this cycle.")
    else:
        logger.info(f"Downloaded {new_count} new transcript(s).")


def main():
    parser = argparse.ArgumentParser(
        description="Monitor a YouTube channel and download transcripts using yt-dlp."
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel @handle, ID (UCxxxx), or full URL",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory (default: {DEFAULT_OUTPUT_DIR})",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_CHECK_INTERVAL,
        help=f"Check interval in seconds (default: {DEFAULT_CHECK_INTERVAL})",
    )
    parser.add_argument(
        "--languages",
        nargs="+",
        default=None,
        help="Preferred subtitle languages, e.g. --languages en es (default: en)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit instead of looping",
    )
    args = parser.parse_args()

    # Check yt-dlp is installed
    if subprocess.run(["which", "yt-dlp"], capture_output=True).returncode != 0:
        logger.error("yt-dlp not found. Install it: pip install yt-dlp")
        sys.exit(1)

    channel_url = build_channel_url(args.channel)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Resolving channel: {channel_url}")
    channel_name = fetch_channel_info(channel_url)
    state_path = output_dir / channel_name / STATE_FILE

    logger.info(f"Monitoring: {channel_name}")
    logger.info(f"Output dir: {output_dir / channel_name}")
    logger.info(f"Interval: {args.interval}s ({args.interval // 60} min)")

    if args.once:
        run_check(channel_url, channel_name, output_dir, state_path, args.languages)
    else:
        while not shutdown_requested:
            run_check(channel_url, channel_name, output_dir, state_path, args.languages)
            if shutdown_requested:
                break
            logger.info(f"Sleeping {args.interval // 60} minutes...")
            for _ in range(args.interval):
                if shutdown_requested:
                    break
                time.sleep(1)

    logger.info("Monitor stopped.")


if __name__ == "__main__":
    main()
