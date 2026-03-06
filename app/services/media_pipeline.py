import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from app.core.config import settings


class PipelineError(Exception):
    def __init__(self, message: str, code: str = "pipeline_error") -> None:
        super().__init__(message)
        self.code = code


class MediaMetadata(dict):
    @property
    def title(self) -> str:
        return str(self.get("title") or "Unknown Title")

    @property
    def artist(self) -> str:
        return str(self.get("uploader") or self.get("channel") or "Unknown Artist")

    @property
    def thumbnail_url(self) -> Optional[str]:
        thumb = self.get("thumbnail")
        if isinstance(thumb, str) and thumb.strip():
            return thumb
        thumbs = self.get("thumbnails")
        if isinstance(thumbs, list):
            for item in reversed(thumbs):
                if isinstance(item, dict):
                    url = item.get("url")
                    if isinstance(url, str) and url.strip():
                        return url
        return None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise PipelineError(stderr or "Command failed", code="command_failed")
    return result


def _ensure_binary(binary: str) -> None:
    if shutil.which(binary) is None:
        raise PipelineError(f"Missing required binary: {binary}", code="missing_dependency")


def fetch_video_metadata(url: str) -> MediaMetadata:
    _ensure_binary("yt-dlp")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--skip-download",
        "--dump-single-json",
        url,
    ]
    result = _run(cmd)
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise PipelineError("Unable to parse video metadata", code="metadata_parse_failed") from exc
    return MediaMetadata(raw)


def download_audio_source(
    url: str,
    work_dir: Path,
    base_name: str,
    preferred_thumbnail_path: Optional[Path] = None,
    concurrent_fragments: int = 4,
    on_progress: Optional[Callable[[float, Optional[str]], None]] = None,
) -> tuple[Path, Optional[Path]]:
    _ensure_binary("yt-dlp")
    output_template = str(work_dir / f"{base_name}.%(ext)s")
    cmd = [
        "yt-dlp",
        "-f",
        "bestaudio/best",
        "--no-playlist",
        "--newline",
        "--concurrent-fragments",
        str(max(1, concurrent_fragments)),
        "-o",
        output_template,
        url,
    ]
    if not (preferred_thumbnail_path and preferred_thumbnail_path.exists()):
        cmd[5:5] = ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    _run_with_progress(cmd, _parse_ytdlp_progress, on_progress)

    matches = list(work_dir.glob(f"{base_name}.*"))
    if not matches:
        raise PipelineError("Downloaded file not found", code="download_missing")
    source_file = next((p for p in matches if p.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}), None)
    if source_file is None:
        raise PipelineError("Downloaded audio file not found", code="download_missing")

    thumb = preferred_thumbnail_path if preferred_thumbnail_path and preferred_thumbnail_path.exists() else _find_thumbnail(
        work_dir,
        base_name,
    )
    return source_file, thumb


def normalize_to_mp3_128k(
    source_path: Path,
    output_path: Path,
    target_i: int = -16,
    mode: str = "two_pass",
    ffmpeg_threads: int = 0,
    title: Optional[str] = None,
    artist: Optional[str] = None,
    album: Optional[str] = None,
    cover_path: Optional[Path] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[float], None]] = None,
) -> None:
    _ensure_binary("ffmpeg")
    _ensure_binary("ffprobe")

    mode = (mode or "two_pass").strip().lower()
    if mode not in {"one_pass", "two_pass"}:
        mode = "two_pass"

    duration_seconds = _probe_duration_seconds(source_path)

    if mode == "two_pass":
        # Pass 1: measure loudness statistics.
        if on_stage:
            on_stage("Analyzing source loudness (pass 1/2)")
        pass1 = [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(source_path),
            "-af",
            f"loudnorm=I={target_i}:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ]
        if ffmpeg_threads > 0:
            pass1[1:1] = ["-threads", str(ffmpeg_threads)]

        result1 = subprocess.run(pass1, capture_output=True, text=True)
        if result1.returncode != 0:
            raise PipelineError(result1.stderr.strip() or "ffmpeg pass1 failed", code="normalize_pass1_failed")

        stats = _extract_loudnorm_json(result1.stderr)
        if on_stage:
            on_stage("Applying normalization and encoding (pass 2/2)")
        afilter = (
            f"loudnorm=I={target_i}:TP=-1.5:LRA=11:"
            f"measured_I={stats['input_i']}:"
            f"measured_LRA={stats['input_lra']}:"
            f"measured_TP={stats['input_tp']}:"
            f"measured_thresh={stats['input_thresh']}:"
            f"offset={stats['target_offset']}:"
            "linear=true:print_format=summary"
        )
    else:
        if on_stage:
            on_stage("Applying normalization and encoding (fast 1-pass)")
        afilter = f"loudnorm=I={target_i}:TP=-1.5:LRA=11:print_format=summary"

    pass2 = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(source_path),
    ]

    if cover_path and cover_path.exists():
        pass2.extend(["-i", str(cover_path)])

    pass2.extend(
        [
            "-nostats",
            "-progress",
            "pipe:1",
            "-map",
            "0:a:0",
            "-af",
            afilter,
            "-codec:a",
            "libmp3lame",
            "-b:a",
            "128k",
        ]
    )
    if ffmpeg_threads > 0:
        pass2.extend(["-threads", str(ffmpeg_threads), "-filter_threads", str(ffmpeg_threads)])

    if title:
        pass2.extend(["-metadata", f"title={title}"])
    if artist:
        pass2.extend(["-metadata", f"artist={artist}"])
    if album:
        pass2.extend(["-metadata", f"album={album}"])

    if cover_path and cover_path.exists():
        pass2.extend(
            [
                "-map",
                "1:v:0",
                "-c:v",
                "mjpeg",
                "-id3v2_version",
                "3",
                "-metadata:s:v",
                "title=Album cover",
                "-metadata:s:v",
                "comment=Cover (front)",
            ]
        )

    pass2.append(str(output_path))
    _run_ffmpeg_with_progress(pass2, duration_seconds, on_progress)


def _extract_loudnorm_json(stderr_text: str) -> dict:
    start = stderr_text.rfind("{")
    end = stderr_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise PipelineError("Unable to parse loudnorm measurements", code="loudnorm_parse_failed")

    raw = stderr_text[start : end + 1]
    data = json.loads(raw)

    required = ["input_i", "input_lra", "input_tp", "input_thresh", "target_offset"]
    for key in required:
        if key not in data:
            raise PipelineError(f"Missing loudnorm key: {key}", code="loudnorm_parse_failed")

    return data


def _probe_duration_seconds(source_path: Path) -> float:
    probe_cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(source_path),
    ]
    result = subprocess.run(probe_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise PipelineError(result.stderr.strip() or "ffprobe failed", code="duration_probe_failed")
    try:
        return max(0.0, float(result.stdout.strip()))
    except ValueError as exc:
        raise PipelineError("Unable to parse media duration", code="duration_probe_failed") from exc


def _run_with_progress(
    cmd: list[str],
    line_parser: Callable[[str], Optional[tuple[float, Optional[str]]]],
    on_progress: Optional[Callable[[float, Optional[str]], None]],
) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    captured: list[str] = []
    last_percent = -1.0

    assert proc.stdout is not None
    for line in proc.stdout:
        captured.append(line)
        parsed = line_parser(line)
        if on_progress and parsed is not None:
            pct, note = parsed
            if pct - last_percent >= 0.5:
                on_progress(pct, note)
                last_percent = pct

    proc.wait()
    if proc.returncode != 0:
        stderr = "".join(captured).strip()
        raise PipelineError(stderr or "Command failed", code="command_failed")


def _parse_ytdlp_progress(line: str) -> Optional[tuple[float, Optional[str]]]:
    # Example: [download]  35.6% of ... at ... ETA 00:21
    match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%.*?(ETA\s+[0-9:]+)?", line)
    if not match:
        return None
    pct = float(match.group(1))
    eta = (match.group(2) or "").strip()
    note = f"Downloading source audio ({pct:.1f}%)"
    if eta:
        note = f"{note} - {eta}"
    return pct, note


def _run_ffmpeg_with_progress(
    cmd: list[str],
    duration_seconds: float,
    on_progress: Optional[Callable[[float], None]],
) -> None:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    last_percent = -1.0

    assert proc.stdout is not None
    for line in proc.stdout:
        stdout_lines.append(line)
        if on_progress and duration_seconds > 0 and line.startswith("out_time_ms="):
            try:
                out_time_ms = int(line.split("=", 1)[1].strip())
                pct = min(100.0, max(0.0, (out_time_ms / 1_000_000.0) / duration_seconds * 100.0))
                if pct - last_percent >= 0.5:
                    on_progress(pct)
                    last_percent = pct
            except ValueError:
                continue

    assert proc.stderr is not None
    stderr_lines.extend(proc.stderr.readlines())
    proc.wait()

    if proc.returncode != 0:
        error_text = "".join(stderr_lines).strip()
        raise PipelineError(error_text or "ffmpeg pass2 failed", code="normalize_pass2_failed")

    if on_progress:
        on_progress(100.0)


def _find_thumbnail(work_dir: Path, base_name: str) -> Optional[Path]:
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = work_dir / f"{base_name}{ext}"
        if candidate.exists():
            if ext == ".webp":
                # Try to convert webp to jpg for MP3 cover embedding.
                jpg_target = work_dir / f"{base_name}.jpg"
                if _try_convert_image(candidate, jpg_target):
                    return jpg_target
            return candidate
    return None


def _try_convert_image(source: Path, target: Path) -> bool:
    if not source.exists():
        return False
    _ensure_binary("ffmpeg")
    cmd = ["ffmpeg", "-hide_banner", "-y", "-i", str(source), str(target)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode == 0 and target.exists()


def sanitize_filename_component(value: str, fallback: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9]+", "_", value.strip())
    clean = clean.strip("_").upper()
    return clean or fallback


def build_output_filename(artist: str, album: str) -> str:
    artist_part = sanitize_filename_component(artist, "UNKNOWN_ARTIST")
    album_part = sanitize_filename_component(album, "UNKNOWN_ALBUM")
    return f"{artist_part}_{album_part}.mp3"


def ensure_unique_output_path(directory: Path, desired_name: str, suffix: str) -> Path:
    candidate = directory / desired_name
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    ext = candidate.suffix or ".mp3"
    return directory / f"{stem}_{suffix}{ext}"


def job_work_dir(job_id: str) -> Path:
    work = settings.temp_dir / job_id
    work.mkdir(parents=True, exist_ok=True)
    return work
