import os
import sys
import json
import time
import shutil
import signal
import logging
import argparse
import threading
from pathlib import Path
from typing import Dict, Any, Optional, List

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError, BotoCoreError

import config
from backend_client import BackendClient
from s3_service import S3Service
from transcoder import Transcoder

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("worker")

backend = BackendClient()
s3 = S3Service()
transcoder = Transcoder()

# Global graceful shutdown flag
shutdown_event = threading.Event()


def signal_handler(signum, frame):
    """Handles SIGINT (CTRL+C) and SIGTERM (Docker/Kubernetes stop)."""
    sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
    logger.info(f"\n[SHUTDOWN] Received {sig_name}. Initiating graceful worker shutdown...")
    shutdown_event.set()


# Register signal handlers
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


def get_sqs_client():
    """Initializes and returns an AWS SQS client configured from config."""
    kwargs = {
        "region_name": config.SQS_REGION,
        "config": BotoConfig(
            retries={"max_attempts": 5, "mode": "standard"},
            max_pool_connections=25,
        ),
    }
    if config.SQS_ACCESS_KEY_ID and config.SQS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = config.SQS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = config.SQS_SECRET_ACCESS_KEY

    return boto3.client("sqs", **kwargs)


class VisibilityHeartbeat:
    """
    Background daemon thread that extends SQS message visibility
    every interval seconds while long transcoding is active.
    Prevents duplicate processing by other workers if transcode exceeds SQS visibility timeout.
    """
    def __init__(self, sqs_client, queue_url: str, receipt_handle: str, interval: int = 60, extension: int = 300):
        self.sqs_client = sqs_client
        self.queue_url = queue_url
        self.receipt_handle = receipt_handle
        self.interval = interval
        self.extension = extension
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        if self.sqs_client and self.queue_url and self.receipt_handle:
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _run(self):
        while not self.stop_event.wait(self.interval):
            if shutdown_event.is_set():
                break
            try:
                self.sqs_client.change_message_visibility(
                    QueueUrl=self.queue_url,
                    ReceiptHandle=self.receipt_handle,
                    VisibilityTimeout=self.extension,
                )
                logger.debug(f"[HEARTBEAT] Extended SQS message visibility by {self.extension}s")
            except Exception as e:
                logger.warning(f"[HEARTBEAT] Failed to extend visibility: {e}")


def check_disk_space(target_dir: Path, min_free_gb: float) -> bool:
    """Checks if the local disk has enough free space to process the job."""
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        total, used, free = shutil.disk_usage(target_dir)
        free_gb = free / (1024 ** 3)
        if free_gb < min_free_gb:
            logger.error(
                f"[DISK WARNING] Insufficient free disk space in {target_dir}: "
                f"{free_gb:.2f} GB available, {min_free_gb:.2f} GB required."
            )
            return False
        logger.debug(f"[DISK CHECK] Free disk space: {free_gb:.2f} GB (OK)")
        return True
    except Exception as e:
        logger.warning(f"[DISK CHECK] Unable to determine disk space: {e}")
        return True


def cleanup_stale_workspaces(temp_dir: Path, older_than_hours: int = 24):
    """Cleans up any orphaned transcode workspaces left over from previous crashed sessions."""
    if not temp_dir.exists():
        return
    now = time.time()
    cutoff = now - (older_than_hours * 3600)
    cleaned = 0
    for item in temp_dir.glob("transcode_*"):
        if item.is_dir():
            try:
                mtime = item.stat().st_mtime
                if mtime < cutoff:
                    shutil.rmtree(item, ignore_errors=True)
                    cleaned += 1
            except Exception:
                pass
    if cleaned > 0:
        logger.info(f"[CLEANUP] Purged {cleaned} stale temporary transcode folder(s) older than {older_than_hours}h.")


def filter_target_renditions(target_renditions: List[Dict[str, Any]], source_height: int) -> List[Dict[str, Any]]:
    """
    Prevents wasteful upscaling by filtering out renditions higher than source resolution.
    If source is 720p, 1080p will not be encoded.
    """
    if not source_height:
        return target_renditions

    valid = [r for r in target_renditions if int(r.get("height", 0)) <= source_height]
    if not valid and target_renditions:
        # If all requested renditions are higher than source, pick the smallest one
        valid = [min(target_renditions, key=lambda r: int(r.get("height", 1080)))]

    skipped = len(target_renditions) - len(valid)
    if skipped > 0:
        logger.info(
            f"[RESOLUTION FILTER] Source resolution is {source_height}p. "
            f"Filtered out {skipped} higher rendition(s) to prevent wasteful upscaling."
        )
    return valid


def delete_sqs_message(sqs_client, receipt_handle: Optional[str]):
    """Safely deletes an SQS message upon success or final failure."""
    if sqs_client and receipt_handle and config.SQS_QUEUE_URL:
        try:
            sqs_client.delete_message(
                QueueUrl=config.SQS_QUEUE_URL,
                ReceiptHandle=receipt_handle,
            )
        except Exception as e:
            logger.error(f"Failed to delete SQS message: {e}")


def release_sqs_message(sqs_client, receipt_handle: Optional[str], delay_seconds: int = 0):
    """Releases an SQS message back to the queue for retry on transient errors."""
    if sqs_client and receipt_handle and config.SQS_QUEUE_URL:
        try:
            sqs_client.change_message_visibility(
                QueueUrl=config.SQS_QUEUE_URL,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=delay_seconds,
            )
            logger.info(f"[SQS] Released message back to queue for retry (VisibilityTimeout={delay_seconds}s)")
        except Exception as e:
            logger.error(f"Failed to release SQS message: {e}")


def process_sqs_message(sqs_client, message: Dict[str, Any]):
    """Processes an incoming AWS SQS video-processing job message."""
    receipt_handle = message.get("ReceiptHandle")
    body = message.get("Body", "")
    attributes = message.get("Attributes", {})
    receive_count = int(attributes.get("ApproximateReceiveCount", 1))

    # 1. Parse JSON payload
    try:
        data = json.loads(body)
    except Exception as parse_err:
        logger.error(f"Received malformed JSON message; discarding: {parse_err}")
        delete_sqs_message(sqs_client, receipt_handle)
        return

    job_id = data.get("jobId")
    video_id = data.get("videoId")
    source = data.get("source", {})
    source_bucket = source.get("bucket") or config.S3_BUCKET
    source_key = source.get("key")
    target_renditions = data.get("renditions", [])

    if not job_id or not video_id or not source_key:
        logger.error(f"Incomplete job message payload (missing jobId, videoId, or source.key); discarding: {data}")
        delete_sqs_message(sqs_client, receipt_handle)
        return

    work_dir = Path(config.WORKER_TEMP_DIR) / f"transcode_{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    source_fmt = source.get("format", "mp4")
    input_file = work_dir / f"input.{source_fmt}"

    # 2. Pre-flight Disk Space Check
    if not check_disk_space(work_dir, config.MIN_FREE_DISK_GB):
        err_msg = f"Insufficient disk space (< {config.MIN_FREE_DISK_GB} GB free). Aborting job."
        backend.notify_job_fail(job_id, video_id, err_msg)
        # Release back to queue so a different worker with disk space can process it
        release_sqs_message(sqs_client, receipt_handle, delay_seconds=60)
        return

    # 3. Start SQS Visibility Heartbeat to prevent duplicate processing
    heartbeat = VisibilityHeartbeat(sqs_client, config.SQS_QUEUE_URL, receipt_handle)
    heartbeat.start()

    is_permanent_error = False

    try:
        # 1. Notify backend: job claimed & started
        logger.info(f"\n{'='*70}\n[JOB START] Starting job {job_id} for video {video_id} (Attempt #{receive_count})\n{'='*70}")
        backend.notify_job_start(job_id)

        # 2. Download original source from S3
        logger.info(f"[DOWNLOAD] Downloading s3://{source_bucket}/{source_key} -> {input_file}")
        backend.send_progress(job_id, 5, "Downloading source video")
        s3.download_file(source_bucket, source_key, input_file)

        if shutdown_event.is_set():
            raise KeyboardInterrupt("Shutdown requested mid-job")

        # 3. Probe file with FFprobe
        logger.info("[PROBE] Probing video specs with FFprobe...")
        backend.send_progress(job_id, 15, "Probing video metadata")
        try:
            probed_meta = transcoder.probe(input_file)
        except Exception as probe_err:
            # Corrupted video file or invalid format is a permanent failure
            is_permanent_error = True
            raise RuntimeError(f"FFprobe extraction failed (possibly invalid or corrupt video): {probe_err}") from probe_err

        backend.send_probed_metadata(video_id, job_id, probed_meta)

        # 4. Filter target renditions to avoid upscaling
        source_height = probed_meta.get("height") or 1080
        effective_renditions = filter_target_renditions(target_renditions, source_height)

        # 5. Transcode target renditions to HLS
        completed_renditions = []
        num_renditions = len(effective_renditions)

        for index, rendition in enumerate(effective_renditions):
            if shutdown_event.is_set():
                raise KeyboardInterrupt("Shutdown requested during encoding")

            profile = rendition.get("profileKey") or f"{rendition.get('height', 720)}p"
            progress_pct = 20 + int((index / max(num_renditions, 1)) * 60)
            backend.send_progress(job_id, progress_pct, f"Encoding {profile}")

            rend_out_dir = work_dir / profile
            logger.info(f"[TRANSCODE] Encoding rendition '{profile}' ({rendition.get('width')}x{rendition.get('height')})...")

            output_rendition = transcoder.encode_hls(
                input_file=input_file,
                output_dir=rend_out_dir,
                profile=profile,
                width=int(rendition.get("width", 1280)),
                height=int(rendition.get("height", 720)),
                video_bitrate=int(rendition.get("videoBitrateBps", 2800000)),
                audio_bitrate=int(rendition.get("audioBitrateBps", 128000)),
            )

            # Upload HLS directory (.ts segments and playlist.m3u8) to S3 concurrently
            s3_rend_prefix = f"videos/{video_id}/renditions/{profile}"
            logger.info(f"[UPLOAD] Uploading HLS rendition to s3://{source_bucket}/{s3_rend_prefix}/...")
            s3.upload_directory(
                local_dir=rend_out_dir,
                bucket=source_bucket,
                prefix=s3_rend_prefix,
            )

            storage_key = rendition.get("storageKey") or f"{s3_rend_prefix}/playlist.m3u8"
            completed_renditions.append({
                "renditionId": rendition.get("renditionId"),
                "profileKey": profile,
                "fileSizeBytes": output_rendition["size_bytes"],
                "durationSeconds": output_rendition["duration"],
                "storageKey": storage_key,
                "width": rendition.get("width"),
                "height": rendition.get("height"),
                "videoBitrateBps": rendition.get("videoBitrateBps"),
                "audioBitrateBps": rendition.get("audioBitrateBps"),
            })

        # 6. Generate and upload master.m3u8 playlist
        backend.send_progress(job_id, 85, "Generating master playlist")
        logger.info("[PLAYLIST] Building adaptive master.m3u8...")
        master_path = transcoder.create_master_playlist(work_dir, completed_renditions)
        s3.upload_file(master_path, source_bucket, f"videos/{video_id}/master.m3u8")
        s3.upload_file(master_path, source_bucket, f"videos/{video_id}/hls/master.m3u8")

        # 7. Notify backend: complete!
        backend.send_progress(job_id, 100, "Completed")
        backend.notify_job_complete(
            job_id=job_id,
            video_id=video_id,
            renditions=completed_renditions,
            master_key=f"videos/{video_id}/master.m3u8",
        )

        # 8. Delete message from SQS upon successful completion (ACK)
        delete_sqs_message(sqs_client, receipt_handle)
        logger.info(f"\n{'='*70}\n[JOB SUCCESS] Successfully finished job {job_id} for video {video_id}\n{'='*70}\n")

    except KeyboardInterrupt:
        logger.warning(f"[ABORT] Worker shutting down mid-job {job_id}. Releasing message back to SQS...")
        release_sqs_message(sqs_client, receipt_handle, delay_seconds=0)

    except Exception as e:
        logger.exception(f"[JOB FAILURE] Job {job_id} failed: {e}")
        backend.notify_job_fail(job_id, video_id, str(e))

        # If permanent error or retries exhausted, delete message so queue is not blocked
        if is_permanent_error or receive_count >= config.MAX_SQS_RETRIES:
            logger.error(f"[DLQ] Max retries reached or permanent error for job {job_id}. Deleting from queue.")
            delete_sqs_message(sqs_client, receipt_handle)
        else:
            # Transient error: release message for retry with 30s backoff
            logger.warning(f"[RETRY] Transient error on attempt {receive_count}/{config.MAX_SQS_RETRIES}. Re-queuing...")
            release_sqs_message(sqs_client, receipt_handle, delay_seconds=30)

    finally:
        # Stop visibility heartbeat thread
        heartbeat.stop()

        # Clean temporary workspace files
        if config.CLEANUP_TEMP and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
            logger.debug(f"[CLEANUP] Deleted workspace: {work_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="AWS SQS Video Processing Worker (Production Resilient)"
    )
    parser.add_argument(
        "--check-services",
        action="store_true",
        help="Check connectivity to AWS SQS, AWS S3, and NestJS Backend API",
    )
    parser.add_argument(
        "--test-message",
        type=str,
        default=None,
        help="Path to a JSON file containing a test video-processing message to execute locally",
    )

    args = parser.parse_args()

    config.print_summary()

    # If running worker consumer loop or test message, validate all required env vars
    if not args.check_services:
        try:
            config.validate()
        except ValueError as ve:
            logger.critical(str(ve))
            sys.exit(1)

    # Clean up stale temp directories from previous sessions
    cleanup_stale_workspaces(Path(config.WORKER_TEMP_DIR), config.STALE_TEMP_HOURS)

    sqs_client = get_sqs_client()

    # Handle service health check
    if args.check_services:
        print("Checking service connections...")
        backend_ok = backend.test_connection()
        print(f"  - Backend API: {'CONNECTED [OK]' if backend_ok else 'FAILED [ERROR]'}")
        s3_ok = s3.test_connection()
        print(f"  - AWS S3:      {'CONNECTED [OK]' if s3_ok else 'FAILED [ERROR]'}")
        if not config.SQS_QUEUE_URL:
            print("  - AWS SQS:     FAILED [ERROR] (SQS_QUEUE_URL is not configured)")
        else:
            try:
                attrs = sqs_client.get_queue_attributes(
                    QueueUrl=config.SQS_QUEUE_URL,
                    AttributeNames=["ApproximateNumberOfMessages"],
                )
                count = attrs.get("Attributes", {}).get("ApproximateNumberOfMessages", "0")
                print(f"  - AWS SQS:     CONNECTED [OK] (Messages available: {count})")
            except Exception as sqs_err:
                print(f"  - AWS SQS:     FAILED [ERROR] ({sqs_err})")
        sys.exit(0)


    # Handle test payload if provided
    if args.test_message:
        msg_file = Path(args.test_message)
        if not msg_file.exists():
            print(f"Error: Test payload file not found: {msg_file}")
            sys.exit(1)
        with open(msg_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"Executing test payload from {msg_file}...")

        dummy_msg = {
            "ReceiptHandle": "dummy-receipt-handle",
            "Body": json.dumps(data),
            "Attributes": {"ApproximateReceiveCount": "1"},
        }
        process_sqs_message(sqs_client, dummy_msg)
        return

    # Start AWS SQS long polling loop
    logger.info(f"[*] Worker is listening on AWS SQS queue: {config.SQS_QUEUE_URL}")
    logger.info("Press CTRL+C or send SIGTERM to exit.\n")

    while not shutdown_event.is_set():
        try:
            response = sqs_client.receive_message(
                QueueUrl=config.SQS_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=config.SQS_WAIT_TIME_SECONDS,
                VisibilityTimeout=config.SQS_VISIBILITY_TIMEOUT,
                AttributeNames=["All"],
            )

            messages = response.get("Messages", [])
            if not messages:
                continue

            for msg in messages:
                if shutdown_event.is_set():
                    break
                process_sqs_message(sqs_client, msg)

        except (ClientError, BotoCoreError) as aws_err:
            logger.warning(f"AWS SQS connection warning ({aws_err}). Retrying in 5 seconds...")
            time.sleep(5)
        except Exception as e:
            if shutdown_event.is_set():
                break
            logger.exception(f"Unexpected error in SQS poll loop: {e}. Retrying in 5 seconds...")
            time.sleep(5)

    logger.info("[SHUTDOWN] Worker process has cleanly exited.")


if __name__ == "__main__":
    main()
