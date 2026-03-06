import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class YouTubeRef:
    video_id: str
    canonical_url: str


def parse_youtube_ref(raw_value: str) -> YouTubeRef | None:
    raw = (raw_value or "").strip()
    if not raw:
        return None

    if VIDEO_ID_RE.fullmatch(raw):
        return _as_ref(raw)

    candidate = raw
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", candidate):
        if candidate.startswith(("youtube.com/", "www.youtube.com/", "m.youtube.com/", "youtu.be/")):
            candidate = f"https://{candidate}"
        else:
            return None

    parsed = urlparse(candidate)
    host = (parsed.netloc or "").lower().split(":", 1)[0]
    path = parsed.path or ""
    query = parse_qs(parsed.query or "")

    video_id: str | None = None

    if host in {"youtu.be", "www.youtu.be"}:
        first = path.lstrip("/").split("/", 1)[0]
        video_id = first or None
    elif host.endswith("youtube.com"):
        v_values = query.get("v")
        if v_values:
            video_id = v_values[0]
        elif path.startswith("/shorts/") or path.startswith("/embed/"):
            parts = path.strip("/").split("/")
            if len(parts) >= 2:
                video_id = parts[1]

    if not video_id or not VIDEO_ID_RE.fullmatch(video_id):
        return None

    return _as_ref(video_id)


def _as_ref(video_id: str) -> YouTubeRef:
    return YouTubeRef(video_id=video_id, canonical_url=f"https://www.youtube.com/watch?v={video_id}")
