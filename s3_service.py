import os
import posixpath
import logging
from pathlib import Path
from typing import Dict, Tuple, Optional, Union, List
from concurrent.futures import ThreadPoolExecutor, as_completed
import boto3
from botocore.config import Config as BotoConfig
from boto3.s3.transfer import TransferConfig

import config

logger = logging.getLogger("worker.s3")


MIME_TYPES = {
    ".m3u8": "application/vnd.apple.mpegurl",
    ".ts": "video/mp2t",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".mp4": "video/mp4",
}


class S3Service:
    """Manages AWS S3 downloads, multipart segment uploads, and parallel playlist publishing."""

    def __init__(self, s3_config=None):
        self.access_key_id = getattr(s3_config, "access_key_id", config.AWS_ACCESS_KEY_ID)
        self.secret_access_key = getattr(s3_config, "secret_access_key", config.AWS_SECRET_ACCESS_KEY)
        self.region = getattr(s3_config, "region", getattr(config, "S3_REGION", config.AWS_REGION))
        self.default_bucket = getattr(s3_config, "bucket", getattr(config, "S3_BUCKET", config.AWS_S3_BUCKET))
        self.endpoint_url = getattr(s3_config, "endpoint_url", config.AWS_ENDPOINT_URL)
        self.upload_concurrency = getattr(config, "S3_UPLOAD_CONCURRENCY", 8)

        # High-performance multipart transfer config
        self.transfer_config = TransferConfig(
            multipart_threshold=8 * 1024 * 1024,
            max_concurrency=10,
            multipart_chunksize=8 * 1024 * 1024,
            use_threads=True,
        )

        self.client = self._create_client()

    def _create_client(self):
        kwargs = {
            "region_name": self.region,
            "config": BotoConfig(
                retries={"max_attempts": 5, "mode": "standard"},
                max_pool_connections=50,
            ),
        }
        if self.access_key_id and self.secret_access_key:
            kwargs["aws_access_key_id"] = self.access_key_id
            kwargs["aws_secret_access_key"] = self.secret_access_key
        if self.endpoint_url:
            kwargs["endpoint_url"] = self.endpoint_url

        return boto3.client("s3", **kwargs)


    def test_connection(self, bucket: Optional[str] = None) -> bool:
        """Verifies S3 credentials and bucket access."""
        target_bucket = bucket or self.default_bucket
        if not target_bucket:
            logger.error("Cannot test S3 connection: S3_BUCKET is not configured.")
            return False
        try:
            self.client.head_bucket(Bucket=target_bucket)
            return True
        except Exception as e:
            logger.error(f"S3 access check failed for bucket '{target_bucket}': {e}")
            return False


    def download_file(self, bucket: str, key: str, local_dest: Union[str, Path]) -> Path:
        """Downloads source file from S3 to local workspace."""
        dest_path = Path(local_dest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        target_bucket = bucket or self.default_bucket

        logger.info(f"[S3] Downloading s3://{target_bucket}/{key} -> {dest_path} ...")
        self.client.download_file(
            Bucket=target_bucket,
            Key=key,
            Filename=str(dest_path),
            Config=self.transfer_config,
        )

        size_mb = dest_path.stat().st_size / (1024 * 1024)
        logger.info(f"[S3] Downloaded successfully: {dest_path} ({size_mb:.2f} MB)")
        return dest_path

    def upload_file(
        self,
        local_path: Union[str, Path],
        bucket: str,
        key: str,
        content_type: Optional[str] = None,
    ):
        """Uploads a single file to S3 with correct Content-Type header."""
        path_obj = Path(local_path)
        target_bucket = bucket or self.default_bucket
        if not content_type:
            ext = path_obj.suffix.lower()
            content_type = MIME_TYPES.get(ext, "application/octet-stream")

        extra_args = {"ContentType": content_type}
        logger.debug(f"[S3] Uploading {path_obj.name} -> s3://{target_bucket}/{key} ({content_type})")

        self.client.upload_file(
            Filename=str(path_obj),
            Bucket=target_bucket,
            Key=key,
            ExtraArgs=extra_args,
            Config=self.transfer_config,
        )

    def upload_directory(
        self,
        local_dir: Union[str, Path],
        bucket: str,
        prefix: str,
    ) -> int:
        """
        Uploads all files in a local directory to S3 concurrently using a thread pool.
        Significantly speeds up multi-segment HLS uploads (5x-10x speedup).
        """
        local_path = Path(local_dir)
        target_bucket = bucket or self.default_bucket
        prefix_clean = prefix.strip("/")

        tasks: List[Tuple[Path, str, str]] = []
        for root, _, files in os.walk(local_path):
            for filename in files:
                full_file = Path(root) / filename
                rel_file = full_file.relative_to(local_path).as_posix()
                s3_key = f"{prefix_clean}/{rel_file}"
                ext = full_file.suffix.lower()
                c_type = MIME_TYPES.get(ext, "application/octet-stream")
                tasks.append((full_file, s3_key, c_type))

        if not tasks:
            return 0

        uploaded_count = 0
        with ThreadPoolExecutor(max_workers=self.upload_concurrency) as executor:
            futures = {
                executor.submit(self.upload_file, f_path, target_bucket, s_key, c_type): f_path
                for (f_path, s_key, c_type) in tasks
            }
            for future in as_completed(futures):
                file_ref = futures[future]
                try:
                    future.result()
                    uploaded_count += 1
                except Exception as exc:
                    logger.error(f"[S3] Failed to upload {file_ref.name}: {exc}")
                    raise

        logger.info(f"[S3] Uploaded directory {local_path} ({uploaded_count} files) to s3://{target_bucket}/{prefix_clean}/")
        return uploaded_count

    def upload_rendition_assets(
        self,
        local_rendition_dir: Path,
        bucket: str,
        storage_key: str,
    ) -> Tuple[int, int]:
        """
        Uploads an entire HLS rendition folder (.ts segments and playlist.m3u8).
        Calculates and returns (total_bytes, total_files).
        """
        target_bucket = bucket or self.default_bucket
        s3_folder_prefix = posixpath.dirname(storage_key)

        total_bytes = 0
        total_files = 0

        # Upload all .ts segments and playlist concurrently
        total_files = self.upload_directory(local_rendition_dir, target_bucket, s3_folder_prefix)
        for f in local_rendition_dir.glob("*"):
            if f.is_file():
                total_bytes += f.stat().st_size

        logger.info(
            f"[S3] Uploaded rendition to s3://{target_bucket}/{s3_folder_prefix}/ "
            f"({total_files} files, {total_bytes / (1024*1024):.2f} MB)"
        )
        return total_bytes, total_files

    def upload_master_playlists(
        self,
        local_master_file: Path,
        bucket: str,
        video_id: str,
    ):
        """Uploads master.m3u8 to both standard and compat player locations."""
        target_bucket = bucket or self.default_bucket
        key1 = f"videos/{video_id}/master.m3u8"
        key2 = f"videos/{video_id}/hls/master.m3u8"
        self.upload_file(local_master_file, target_bucket, key1, MIME_TYPES[".m3u8"])
        self.upload_file(local_master_file, target_bucket, key2, MIME_TYPES[".m3u8"])
        logger.info(f"[S3] Published master playlists to s3://{target_bucket}/{key1} and {key2}")
