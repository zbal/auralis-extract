from pathlib import Path
import os


class Settings:
    app_name: str = "auralis-extract"
    redis_url: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
    jobs_dir: Path = Path(os.getenv("JOBS_DIR", "/data/jobs"))
    playlists_dir: Path = Path(os.getenv("PLAYLISTS_DIR", "/data/playlists"))
    output_dir: Path = Path(os.getenv("OUTPUT_DIR", "/data/output"))
    temp_dir: Path = Path(os.getenv("TEMP_DIR", "/tmp/auralis-extract"))
    metadata_cache_dir: Path = Path(os.getenv("METADATA_CACHE_DIR", "/data/cache/metadata"))
    thumbnail_cache_dir: Path = Path(os.getenv("THUMBNAIL_CACHE_DIR", "/data/cache/thumbnails"))
    ffmpeg_threads: int = int(os.getenv("FFMPEG_THREADS", "0"))
    normalization_mode: str = os.getenv("NORMALIZATION_MODE", "two_pass").strip().lower()
    target_lufs: int = int(os.getenv("TARGET_LUFS", "-14"))
    download_concurrent_fragments: int = int(os.getenv("DOWNLOAD_CONCURRENT_FRAGMENTS", "2"))
    ytdlp_socket_timeout_seconds: int = int(os.getenv("YTDLP_SOCKET_TIMEOUT_SECONDS", "20"))
    ytdlp_sleep_requests_seconds: float = float(os.getenv("YTDLP_SLEEP_REQUESTS_SECONDS", "1.0"))
    ytdlp_sleep_interval_seconds: float = float(os.getenv("YTDLP_SLEEP_INTERVAL_SECONDS", "1.0"))
    ytdlp_max_sleep_interval_seconds: float = float(os.getenv("YTDLP_MAX_SLEEP_INTERVAL_SECONDS", "3.0"))
    github_url: str = os.getenv("GITHUB_URL", "https://github.com/zbal/auralis-extract")
    downloads_page_size: int = int(os.getenv("DOWNLOADS_PAGE_SIZE", "10"))
    max_job_retries: int = int(os.getenv("MAX_JOB_RETRIES", "2"))
    job_timeout_seconds: int = int(os.getenv("JOB_TIMEOUT_SECONDS", "7200"))


settings = Settings()

settings.jobs_dir.mkdir(parents=True, exist_ok=True)
settings.playlists_dir.mkdir(parents=True, exist_ok=True)
settings.output_dir.mkdir(parents=True, exist_ok=True)
settings.temp_dir.mkdir(parents=True, exist_ok=True)
settings.metadata_cache_dir.mkdir(parents=True, exist_ok=True)
settings.thumbnail_cache_dir.mkdir(parents=True, exist_ok=True)
