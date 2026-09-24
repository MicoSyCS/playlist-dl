"""HTTP API.

POST /api/jobs                 create a job from a Spotify playlist URL
GET  /api/jobs/{job_id}        job status (poll this)
GET  /api/jobs/{job_id}/download   the ZIP, once complete or partial
GET  /api/health               readiness details (no secrets)
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, get_settings
from .downloader import AudioEngine, resolve_ffmpeg
from .errors import UserFacingError
from .jobs import JobManager, PlaylistSource
from .spotify import SpotifyClient
from .spotify_embed import EMBED_TRACK_CAP, ApiWithEmbedFallback, SpotifyEmbedClient

log = logging.getLogger("app")


class CreateJobRequest(BaseModel):
    url: str = Field(..., max_length=2048)


def build_manager(settings: Settings) -> JobManager:
    ffmpeg = resolve_ffmpeg(settings.ffmpeg_path)
    if ffmpeg is None:
        log.error("ffmpeg not found (FFMPEG_PATH=%r); every track will fail conversion", settings.ffmpeg_path)
    source: PlaylistSource
    embed = SpotifyEmbedClient()
    if not settings.uses_spotify_api:
        log.info("playlist metadata: spotify public embed player (no api credentials)")
        source = embed
    else:
        if not settings.spotify_configured:
            log.warning("SPOTIFY_METADATA_SOURCE=api but credentials are not set; job creation will be refused")
        api = SpotifyClient(settings.spotify_client_id, settings.spotify_client_secret, market=settings.spotify_market)
        if settings.spotify_metadata_source == "auto":
            log.info("playlist metadata: spotify web api, falling back to the embed player")
            source = ApiWithEmbedFallback(api, embed)
        else:
            log.info("playlist metadata: spotify web api")
            source = api
    engine = AudioEngine(
        ffmpeg=ffmpeg or settings.ffmpeg_path,
        search_prefix=settings.search_prefix,
        search_results=settings.search_results,
        max_download_mb=settings.max_download_mb,
        bitrate_kbps=settings.mp3_bitrate,
    )
    return JobManager(settings, source, engine)


def create_app(settings: Settings | None = None, manager: JobManager | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        mgr = manager or build_manager(settings)
        app.state.manager = mgr
        mgr.start()
        try:
            yield
        finally:
            mgr.shutdown()

    app = FastAPI(title="7pairs playlist packager", lifespan=lifespan, docs_url=None, redoc_url=None)

    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type"],
        )

    @app.exception_handler(UserFacingError)
    async def user_error_handler(_: Request, exc: UserFacingError) -> JSONResponse:
        return JSONResponse({"error": exc.message}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {"error": 'invalid request. send json like {"url": "https://open.spotify.com/playlist/..."}.'},
            status_code=422,
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({"error": str(exc.detail).lower()}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
        return JSONResponse({"error": "internal server error."}, status_code=500)

    def mgr(request: Request) -> JobManager:
        manager_: JobManager = request.app.state.manager
        return manager_

    @app.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        m = mgr(request)
        return {
            "ok": True,
            "spotify_configured": settings.spotify_configured,
            "metadata_source": "api" if settings.uses_spotify_api else "embed",
            "credentials_required": settings.credentials_required,
            "ffmpeg_available": resolve_ffmpeg(settings.ffmpeg_path) is not None,
            "limits": {
                "max_playlist_tracks": settings.max_playlist_tracks
                if settings.uses_spotify_api
                else min(settings.max_playlist_tracks, EMBED_TRACK_CAP),
                "mp3_bitrate": settings.mp3_bitrate,
                "max_concurrent_jobs": settings.max_concurrent_jobs,
                "job_expiration_hours": settings.job_expiration_hours,
            },
            "search_source": m.settings.search_prefix,
        }

    @app.post("/api/jobs", status_code=202)
    def create_job(body: CreateJobRequest, request: Request) -> dict[str, Any]:
        m = mgr(request)
        job = m.create(body.url)
        return m.snapshot(job)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, request: Request) -> JSONResponse:
        m = mgr(request)
        job = m.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found. it may have expired.")
        return JSONResponse(m.snapshot(job), headers={"Cache-Control": "no-store"})

    @app.get("/api/jobs/{job_id}/download")
    def download(job_id: str, request: Request) -> FileResponse:
        m = mgr(request)
        job = m.get(job_id)
        if job is None:
            raise HTTPException(404, "job not found. it may have expired.")
        snap = m.snapshot(job)
        if not snap["download_ready"] or job.zip_path is None or not job.zip_path.is_file():
            raise HTTPException(409, "the archive is not ready yet.")
        return FileResponse(
            job.zip_path,
            media_type="application/zip",
            filename=job.zip_name or "playlist.zip",
            headers={"Cache-Control": "no-store"},
        )

    return app


app = create_app()
