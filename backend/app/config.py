"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_market: str = "US"
    # Where playlist metadata comes from:
    #   auto  - Web API when credentials are set (falling back to the embed player if the
    #           app may not read the playlist's tracks), otherwise the keyless embed player
    #   embed - always the keyless public embed player (max 100 tracks)
    #   api   - always the Web API (credentials required)
    spotify_metadata_source: Literal["auto", "embed", "api"] = "auto"

    max_playlist_tracks: int = Field(default=100, ge=1, le=2000)
    mp3_bitrate: int = Field(default=192, ge=64, le=320)
    max_concurrent_jobs: int = Field(default=2, ge=1, le=16)
    max_queued_jobs: int = Field(default=10, ge=0, le=500)
    track_concurrency: int = Field(default=2, ge=1, le=8)
    track_timeout_seconds: int = Field(default=240, ge=10)
    job_timeout_minutes: int = Field(default=90, ge=1)
    job_expiration_hours: float = Field(default=2, gt=0)
    cleanup_interval_seconds: int = Field(default=300, ge=10)

    # yt-dlp search prefix, e.g. "ytsearch" (YouTube) or "scsearch" (SoundCloud).
    search_prefix: str = Field(default="ytsearch", pattern=r"^[a-z0-9]+search$")
    search_results: int = Field(default=6, ge=1, le=20)
    max_download_mb: int = Field(default=80, ge=1)

    data_dir: Path = Path("./data")
    ffmpeg_path: str = "ffmpeg"
    cors_origins: str = ""
    log_level: str = "INFO"

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def uses_spotify_api(self) -> bool:
        return self.spotify_metadata_source == "api" or (
            self.spotify_metadata_source == "auto" and self.spotify_configured
        )

    @property
    def credentials_required(self) -> bool:
        return self.spotify_metadata_source == "api"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
