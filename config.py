import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from the current directory if it exists
ENV_PATH = Path(__file__).resolve().parent / ".env"
if ENV_PATH.is_file():
    load_dotenv(dotenv_path=ENV_PATH, override=False)
else:
    # Also attempt standard load_dotenv search in working directory
    load_dotenv(override=False)

# ==============================================================================
# Backend API (NO HARDCODED DEFAULTS)
# ==============================================================================
BACKEND_URL = os.getenv("BACKEND_URL")
WORKER_API_KEY = os.getenv("WORKER_API_KEY")

# ==============================================================================
# AWS S3 Storage (NO HARDCODED DEFAULTS)
# ==============================================================================
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_ACCESS_KEY_ID = S3_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY = S3_SECRET_ACCESS_KEY
S3_REGION = os.getenv("S3_REGION") or os.getenv("AWS_REGION")
AWS_REGION = S3_REGION
S3_BUCKET = os.getenv("S3_BUCKET") or os.getenv("AWS_S3_BUCKET")
AWS_S3_BUCKET = S3_BUCKET
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL")

# ==============================================================================
# AWS SQS Queue (NO HARDCODED DEFAULTS)
# ==============================================================================
SQS_ACCESS_KEY_ID = os.getenv("SQS_ACCESS_KEY_ID") or os.getenv("AWS_SQS_ACCESS_KEY_ID")
SQS_SECRET_ACCESS_KEY = os.getenv("SQS_SECRET_ACCESS_KEY") or os.getenv("AWS_SQS_SECRET_ACCESS_KEY")
AWS_SQS_ACCESS_KEY_ID = SQS_ACCESS_KEY_ID
AWS_SQS_SECRET_ACCESS_KEY = SQS_SECRET_ACCESS_KEY
SQS_REGION = os.getenv("SQS_REGION") or os.getenv("AWS_SQS_REGION") or S3_REGION
AWS_SQS_REGION = SQS_REGION
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL") or os.getenv("AWS_SQS_QUEUE_URL")
AWS_SQS_QUEUE_URL = SQS_QUEUE_URL
SQS_WAIT_TIME_SECONDS = int(os.getenv("SQS_WAIT_TIME_SECONDS", "20"))
SQS_VISIBILITY_TIMEOUT = int(os.getenv("SQS_VISIBILITY_TIMEOUT", "7200"))

# ==============================================================================
# Operational Worker Settings (Internal tuning parameters)
# ==============================================================================
WORKER_TEMP_DIR = os.getenv("WORKER_TEMP_DIR") or str(Path(os.getenv("TEMP", "/tmp")) / "video-jobs")
CLEANUP_TEMP = os.getenv("CLEANUP_TEMP", "true").lower() in ("true", "1", "yes")
FFMPEG_PATH = os.getenv("FFMPEG_PATH")
FFPROBE_PATH = os.getenv("FFPROBE_PATH")
FFMPEG_THREADS = int(os.getenv("FFMPEG_THREADS", "0"))
FFMPEG_TIMEOUT_SECONDS = int(os.getenv("FFMPEG_TIMEOUT_SECONDS", "3600"))
S3_UPLOAD_CONCURRENCY = int(os.getenv("S3_UPLOAD_CONCURRENCY", "8"))
MIN_FREE_DISK_GB = float(os.getenv("MIN_FREE_DISK_GB", "5.0"))
STALE_TEMP_HOURS = int(os.getenv("STALE_TEMP_HOURS", "24"))
MAX_SQS_RETRIES = int(os.getenv("MAX_SQS_RETRIES", "3"))


def validate() -> None:
    """
    Validates that all required infrastructure environment variables are set.
    Fails fast with a clear error if .env was not provided or variables are missing.
    """
    missing = []
    if not BACKEND_URL:
        missing.append("BACKEND_URL")
    if not WORKER_API_KEY:
        missing.append("WORKER_API_KEY")
    if not SQS_QUEUE_URL:
        missing.append("SQS_QUEUE_URL (or AWS_SQS_QUEUE_URL)")
    if not S3_BUCKET:
        missing.append("S3_BUCKET (or AWS_S3_BUCKET)")
    if not S3_REGION:
        missing.append("S3_REGION (or AWS_REGION)")

    if missing:
        error_msg = (
            "\n" + "!" * 70 + "\n"
            "CRITICAL CONFIGURATION ERROR: Missing required environment variables!\n"
            "The worker cannot start because the following settings are not defined:\n"
            + "\n".join(f"  - {var}" for var in missing)
            + "\n\nPlease provide a valid .env file (or inject these environment variables).\n"
            + "!" * 70 + "\n"
        )
        raise ValueError(error_msg)


def print_summary():
    """Prints a configuration summary without revealing secret credentials."""
    def mask(s) -> str:
        if not s:
            return "<NOT CONFIGURED>"
        s = str(s)
        if len(s) <= 4:
            return "***"
        return s[:2] + "***" + s[-2:]

    print("\n" + "=" * 65)
    print("       VIDEO PROCESSING WORKER CONFIGURATION (NO DIRECT DB)")
    print("=" * 65)
    print(f"Backend API URL: {BACKEND_URL or '<NOT CONFIGURED>'}")
    print(f"Worker API Key : {mask(WORKER_API_KEY)}")
    print("-" * 65)
    print(f"AWS SQS Queue  : {SQS_QUEUE_URL or '<NOT CONFIGURED>'}")
    print(f"AWS SQS Region : {SQS_REGION or '<NOT CONFIGURED>'}")
    print(f"AWS SQS Access : {mask(SQS_ACCESS_KEY_ID)}")
    print(f"AWS SQS Secret : {'[CONFIGURED]' if SQS_SECRET_ACCESS_KEY else '[NOT CONFIGURED]'}")
    print(f"SQS Wait Time  : {SQS_WAIT_TIME_SECONDS}s (Long Polling)")
    print(f"SQS Timeout    : {SQS_VISIBILITY_TIMEOUT}s (Visibility Timeout)")
    print(f"Max SQS Retry  : {MAX_SQS_RETRIES} attempts before DLQ")
    print("-" * 65)
    print(f"AWS S3 Bucket  : {S3_BUCKET or '<NOT CONFIGURED>'}")
    print(f"AWS S3 Region  : {S3_REGION or '<NOT CONFIGURED>'}")
    print(f"AWS S3 Access  : {mask(AWS_ACCESS_KEY_ID)}")
    print(f"AWS S3 Secret  : {'[CONFIGURED]' if AWS_SECRET_ACCESS_KEY else '[NOT CONFIGURED]'}")
    print(f"S3 Concurrency : {S3_UPLOAD_CONCURRENCY} parallel upload threads")
    print("-" * 65)
    print(f"Temp Workspace : {WORKER_TEMP_DIR}")
    print(f"Clean Temp Dir : {CLEANUP_TEMP}")
    print(f"Min Free Disk  : {MIN_FREE_DISK_GB} GB required")
    print(f"FFmpeg Threads : {FFMPEG_THREADS if FFMPEG_THREADS > 0 else 'auto'}")
    print(f"FFmpeg Timeout : {FFMPEG_TIMEOUT_SECONDS}s")
    print("=" * 65 + "\n")
