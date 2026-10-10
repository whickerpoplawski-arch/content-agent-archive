import hashlib
import time
from pathlib import Path

from fastapi import APIRouter
from app.core.config import settings
from app.models.schemas import HealthResponse

router = APIRouter()

_BOOT_TS = time.time()

# 13.3: fingerprint the code that can silently drift from what a long-running
# reload=False process loaded at boot. Any edit to these files changes the hash,
# so /health reports "memory code != disk code" without a restart.
_FINGERPRINT_GLOBS = [
    "app/agent/*.py",
    "app/services/*.py",
    "app/prompts/*.txt",
    "app/api/*.py",
    "app/models/*.py",
    "app/core/*.py",
]


def code_fingerprint() -> str:
    digest = hashlib.sha256()
    try:
        for glob in _FINGERPRINT_GLOBS:
            for path in sorted(Path(".").glob(glob)):
                digest.update(path.read_bytes())
    except OSError:
        pass
    return digest.hexdigest()[:12]


@router.get("/health", response_model=HealthResponse, tags=["Health"])
def health_check():
    return HealthResponse(
        status="ok",
        app_name=settings.app_name,
        version=settings.app_version,
        code_fingerprint=code_fingerprint(),
        uptime_seconds=round(time.time() - _BOOT_TS, 1),
    )


@router.get("/health/version", tags=["Health"])
def health_version():
    """13.3: version + code fingerprint so a reload=False long-running process
    can be checked against the on-disk code (ghost-old-code guard)."""
    return {
        "version": settings.app_version,
        "code_fingerprint": code_fingerprint(),
        "uptime_seconds": round(time.time() - _BOOT_TS, 1),
        "reload": settings.debug,
    }
