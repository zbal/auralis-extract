from fastapi import HTTPException

from app.providers.youtube.parsing import YouTubePlaylistRef, YouTubeRef, parse_youtube_playlist_ref, parse_youtube_ref


YOUTUBE_INPUT_ERROR = "Invalid YouTube input. Use a full URL or a valid 11-character video ID."
YOUTUBE_PLAYLIST_INPUT_ERROR = "Invalid YouTube playlist input. Use a playlist URL or valid playlist ID."


def resolve_youtube_input_or_400(raw_value: str) -> YouTubeRef:
    ref = parse_youtube_ref(raw_value)
    if ref is None:
        raise HTTPException(status_code=400, detail=YOUTUBE_INPUT_ERROR)
    return ref


def resolve_youtube_playlist_input_or_400(raw_value: str) -> YouTubePlaylistRef:
    ref = parse_youtube_playlist_ref(raw_value)
    if ref is None:
        raise HTTPException(status_code=400, detail=YOUTUBE_PLAYLIST_INPUT_ERROR)
    return ref
