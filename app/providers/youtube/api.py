from fastapi import HTTPException

from app.providers.youtube.parsing import YouTubeRef, parse_youtube_ref


YOUTUBE_INPUT_ERROR = "Invalid YouTube input. Use a full URL or a valid 11-character video ID."


def resolve_youtube_input_or_400(raw_value: str) -> YouTubeRef:
    ref = parse_youtube_ref(raw_value)
    if ref is None:
        raise HTTPException(status_code=400, detail=YOUTUBE_INPUT_ERROR)
    return ref
