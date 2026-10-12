import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Dict, Any, List, Optional
import config


logger = logging.getLogger("worker.backend")


class BackendClient:
    """HTTP client communicating worker lifecycle events to the NestJS backend with automatic retries."""

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        raw_url = base_url if base_url is not None else (config.BACKEND_URL or "")
        self.base_url = raw_url.rstrip("/")
        self.api_key = api_key if api_key is not None else (config.WORKER_API_KEY or "")
        self.session = self._create_resilient_session()

    def _create_resilient_session(self) -> requests.Session:
        """Configures a requests Session with HTTP connection pooling and exponential backoff retries."""
        session = requests.Session()
        session.headers.update({
            "x-api-key": self.api_key,
            "Content-Type": "application/json",
        })

        retry_strategy = Retry(
            total=4,
            backoff_factor=1.5,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=10,
            pool_maxsize=20,
        )
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def test_connection(self) -> bool:
        """Verifies backend API reachability."""
        if not self.base_url:
            logger.error("Cannot test backend connection: BACKEND_URL is not configured.")
            return False
        try:
            # 400 Bad Request indicates API key is valid and endpoint is responding
            resp = self.session.post(f"{self.base_url}/worker/job/start", json={}, timeout=5)

            if resp.status_code in (200, 201, 400, 404):
                return True
            logger.error(f"Backend returned unexpected status {resp.status_code}: {resp.text}")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to backend at {self.base_url}: {e}")
            return False

    def notify_job_start(self, job_id: str, worker_id: str = "python-worker-1") -> bool:
        """Notifies the backend that a worker has claimed and started this job."""
        url = f"{self.base_url}/worker/job/start"
        payload = {"jobId": job_id, "workerId": worker_id}
        logger.info(f"[BACKEND] Notifying job start: {job_id}")
        resp = self.session.post(url, json=payload, timeout=(5, 15))
        resp.raise_for_status()
        return True

    def send_probed_metadata(self, video_id: str, job_id: str, metadata: Dict[str, Any]) -> bool:
        """Sends extracted technical specs (duration, codecs, dimensions, fps) to backend."""
        url = f"{self.base_url}/worker/video/metadata"
        duration = metadata.get("duration") or metadata.get("duration_seconds")
        payload = {
            "videoId": video_id,
            "jobId": job_id,
            "sourceWidth": metadata.get("width"),
            "sourceHeight": metadata.get("height"),
            "sourceDurationSeconds": float(duration) if duration is not None else None,
            "sourceVideoCodec": metadata.get("video_codec"),
            "sourceAudioCodec": metadata.get("audio_codec"),
            "sourceFrameRate": metadata.get("frame_rate"),
            "sourceSizeBytes": metadata.get("size_bytes"),
            "sourceFormat": metadata.get("format") or "mp4",
        }
        logger.info(f"[BACKEND] Sending probed metadata for video: {video_id}")
        resp = self.session.post(url, json=payload, timeout=(5, 15))
        resp.raise_for_status()
        return True

    def send_progress(self, job_id: str, percent: int, stage: str = "") -> bool:
        """Reports progress percentage (0-100) and current stage to backend."""
        url = f"{self.base_url}/worker/job/progress"
        clamped_percent = max(0, min(100, int(percent)))
        payload = {"jobId": job_id, "percent": clamped_percent, "stage": stage}
        try:
            self.session.post(url, json=payload, timeout=(3, 5))
            logger.debug(f"[BACKEND] Progress reported for {job_id}: {clamped_percent}% - {stage}")
        except Exception as e:
            logger.warning(f"Failed to report progress to backend: {e}")
        return True

    def notify_job_complete(
        self,
        job_id: str,
        video_id: str,
        renditions: List[Dict[str, Any]],
        master_key: Optional[str] = None,
        thumb_key: Optional[str] = None,
    ) -> bool:
        """Notifies the backend that video transcoding has fully completed with all renditions."""
        url = f"{self.base_url}/worker/job/complete"
        payload = {
            "jobId": job_id,
            "videoId": video_id,
            "renditions": renditions,
            "masterPlaylistKey": master_key,
            "thumbnailKey": thumb_key,
        }
        logger.info(f"[BACKEND] Notifying job completion: {job_id} (Video: {video_id})")
        resp = self.session.post(url, json=payload, timeout=(5, 20))
        resp.raise_for_status()
        return True

    def notify_job_fail(self, job_id: str, video_id: str, error_message: str) -> bool:
        """Notifies the backend of a transcoding failure or fatal error."""
        url = f"{self.base_url}/worker/job/fail"
        payload = {
            "jobId": job_id,
            "videoId": video_id,
            "errorMessage": str(error_message)[:2000],
        }
        try:
            logger.error(f"[BACKEND] Notifying job failure for {job_id}: {error_message}")
            resp = self.session.post(url, json=payload, timeout=(5, 15))
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to report failure to backend: {e}")
        return True
