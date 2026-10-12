import os
import sys
import time
import json
import shutil
import logging
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Any, Union

import config

logger = logging.getLogger("worker.transcoder")


def find_ffmpeg(custom_path: Optional[str] = None) -> str:
    """Locates ffmpeg executable on the host system."""
    if custom_path and os.path.exists(custom_path):
        return custom_path
    if config.FFMPEG_PATH and os.path.exists(config.FFMPEG_PATH):
        return config.FFMPEG_PATH

    in_path = shutil.which("ffmpeg")
    if in_path:
        return in_path

    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        "C:\\ffmpeg\\bin\\ffmpeg.exe",
        "C:\\tools\\ffmpeg\\bin\\ffmpeg.exe",
        os.path.join(os.getcwd(), "ffmpeg.exe"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    # Search winget packages folder
    winget_packages_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
    if os.path.isdir(winget_packages_dir):
        for root, _, files in os.walk(winget_packages_dir):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")

    raise FileNotFoundError("FFmpeg executable was not found on your system.")


def find_ffprobe(custom_path: Optional[str] = None) -> str:
    """Locates ffprobe executable on the host system."""
    if custom_path and os.path.exists(custom_path):
        return custom_path
    if config.FFPROBE_PATH and os.path.exists(config.FFPROBE_PATH):
        return config.FFPROBE_PATH

    in_path = shutil.which("ffprobe")
    if in_path:
        return in_path

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    candidates = [
        os.path.join(local_app_data, "Microsoft", "WinGet", "Links", "ffprobe.exe"),
        "C:\\ffmpeg\\bin\\ffprobe.exe",
        "C:\\tools\\ffmpeg\\bin\\ffprobe.exe",
        os.path.join(os.getcwd(), "ffprobe.exe"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand

    # Check same directory as ffmpeg
    try:
        ffmpeg_bin = find_ffmpeg()
        sibling_probe = Path(ffmpeg_bin).parent / ("ffprobe.exe" if sys.platform == "win32" else "ffprobe")
        if sibling_probe.is_file():
            return str(sibling_probe)
    except Exception:
        pass

    # Search winget packages folder
    winget_packages_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
    if os.path.isdir(winget_packages_dir):
        for root, _, files in os.walk(winget_packages_dir):
            if "ffprobe.exe" in files:
                return os.path.join(root, "ffprobe.exe")

    raise FileNotFoundError("FFprobe executable was not found on your system.")


def run_command(cmd: List[str], desc: str, timeout: Optional[int] = None) -> float:
    """Executes a subprocess command with timing, timeout protection, and error handling."""
    logger.info(f"[EXEC] {desc}...")
    start_time = time.time()
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as te:
        duration = time.time() - start_time
        logger.error(f"[TIMEOUT] {desc} timed out after {duration:.2f}s (limit: {timeout}s)")
        raise RuntimeError(f"Command timed out after {timeout}s: {desc}") from te

    duration = time.time() - start_time
    if result.returncode != 0:
        logger.error(f"[ERROR] {desc} failed after {duration:.2f}s:")
        logger.error(result.stderr)
        raise RuntimeError(f"Command failed (code {result.returncode}): {result.stderr[-500:]}")
    logger.info(f"[SUCCESS] {desc} finished in {duration:.2f}s.")
    return duration


def probe_video(video_path: Union[str, Path], ffprobe_bin: Optional[str] = None) -> Dict[str, Any]:
    """
    Executes ffprobe -v quiet -print_format json -show_format -show_streams
    Extracts duration, dimensions, codecs, frame rate.
    """
    path_obj = Path(video_path)
    ffprobe_exec = ffprobe_bin or find_ffprobe()

    cmd = [
        ffprobe_exec,
        "-v", "quiet",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(path_obj),
    ]

    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"FFprobe timed out after 60s inspecting {path_obj}")

    if res.returncode != 0:
        raise RuntimeError(f"FFprobe failed on {path_obj}: {res.stderr}")

    data = json.loads(res.stdout)

    # 1. Format metadata
    format_info = data.get("format", {})
    duration_str = format_info.get("duration")
    duration = float(duration_str) if duration_str else 0.0
    size_bytes = int(format_info.get("size", path_obj.stat().st_size if path_obj.exists() else 0))
    fmt_name = format_info.get("format_name", "mp4").split(",")[0]

    # 2. Streams metadata
    video_stream = None
    audio_stream = None

    for s in data.get("streams", []):
        codec_type = s.get("codec_type")
        if codec_type == "video" and not video_stream:
            video_stream = s
        elif codec_type == "audio" and not audio_stream:
            audio_stream = s

    width = int(video_stream.get("width")) if video_stream and video_stream.get("width") else None
    height = int(video_stream.get("height")) if video_stream and video_stream.get("height") else None
    video_codec = video_stream.get("codec_name") if video_stream else None
    audio_codec = audio_stream.get("codec_name") if audio_stream else None

    # Frame rate calculation
    frame_rate = None
    if video_stream:
        fps_str = video_stream.get("avg_frame_rate") or video_stream.get("r_frame_rate")
        if fps_str and "/" in fps_str:
            num, den = fps_str.split("/")
            try:
                if float(den) > 0:
                    frame_rate = round(float(num) / float(den), 3)
            except Exception:
                pass
        elif fps_str:
            try:
                frame_rate = round(float(fps_str), 3)
            except Exception:
                pass

    logger.info(
        f"[PROBE] {path_obj.name}: {width}x{height}, duration={duration:.2f}s, "
        f"video={video_codec}, audio={audio_codec}, fps={frame_rate}"
    )

    return {
        "duration": duration,
        "duration_seconds": duration,
        "size_bytes": size_bytes,
        "width": width,
        "height": height,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "frame_rate": frame_rate,
        "format": fmt_name,
        "has_audio": audio_stream is not None,
    }


def transcode_to_hls_rendition(
    ffmpeg_bin: str,
    input_file: Union[str, Path],
    output_dir: Union[str, Path],
    width: int,
    height: int,
    video_bitrate_bps: int,
    audio_bitrate_bps: int,
    has_audio: bool = True,
    segment_duration: int = 6,
) -> Path:
    """Transcodes video to target HLS rendition with 6-second .ts segments and playlist.m3u8."""
    out_dir_path = Path(output_dir)
    out_dir_path.mkdir(parents=True, exist_ok=True)
    playlist_path = out_dir_path / "playlist.m3u8"
    segment_pattern = str(out_dir_path / "segment_%03d.ts")

    # Scaling with aspect ratio preservation and padding
    scale_filter = (
        f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    )

    video_bitrate_k = max(int(video_bitrate_bps // 1000), 200)
    audio_bitrate_k = max(int(audio_bitrate_bps // 1000), 64)
    maxrate_k = int(video_bitrate_k * 1.07)
    bufsize_k = int(video_bitrate_k * 1.5)

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(input_file),
        "-vf", scale_filter,
        "-c:v", "libx264",
        "-profile:v", "main",
        "-crf", "20",
        "-preset", "medium",
        "-b:v", f"{video_bitrate_k}k",
        "-maxrate", f"{maxrate_k}k",
        "-bufsize", f"{bufsize_k}k",
        "-pix_fmt", "yuv420p",
        "-g", "48",
        "-keyint_min", "48",
        "-sc_threshold", "0",
    ]

    ffmpeg_threads = getattr(config, "FFMPEG_THREADS", 0)
    if ffmpeg_threads and ffmpeg_threads > 0:
        cmd.extend(["-threads", str(ffmpeg_threads)])

    if has_audio:
        cmd.extend([
            "-c:a", "aac",
            "-b:a", f"{audio_bitrate_k}k",
            "-ar", "44100",
            "-ac", "2",
        ])
    else:
        cmd.append("-an")

    cmd.extend([
        "-hls_time", str(segment_duration),
        "-hls_playlist_type", "vod",
        "-hls_segment_filename", segment_pattern,
        str(playlist_path),
    ])

    timeout = int(getattr(config, "FFMPEG_TIMEOUT_SECONDS", 3600))
    run_command(cmd, f"Transcoding HLS rendition {height}p ({out_dir_path.name})", timeout=timeout)
    return playlist_path


def generate_master_playlist(
    renditions: List[Dict[str, Any]],
    output_master_file: Union[str, Path],
) -> Path:
    """Generates multivariant master.m3u8 adaptive HLS playlist."""
    out_master_path = Path(output_master_file)
    out_master_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "",
    ]

    sorted_renditions = sorted(renditions, key=lambda r: r.get("height", 0))

    for r in sorted_renditions:
        w = r.get("width")
        h = r.get("height")
        v_bps = r.get("videoBitrateBps") or r.get("video_bitrate", 0)
        a_bps = r.get("audioBitrateBps") or r.get("audio_bitrate", 0)
        bandwidth = v_bps + a_bps
        rel_uri = r.get("relative_uri") or f"renditions/{r.get('profileKey', h)}/playlist.m3u8"

        lines.append(f"#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},RESOLUTION={w}x{h}")
        lines.append(rel_uri)
        lines.append("")

    content = "\n".join(lines).strip() + "\n"
    out_master_path.write_text(content, encoding="utf-8")
    logger.info(f"[HLS] Generated master playlist at {out_master_path}")
    return out_master_path


def extract_thumbnail(
    ffmpeg_bin: str,
    input_file: Union[str, Path],
    output_thumbnail_file: Union[str, Path],
    duration: float = 0.0,
) -> Path:
    """Extracts poster thumbnail image from input video."""
    out_thumb_path = Path(output_thumbnail_file)
    out_thumb_path.parent.mkdir(parents=True, exist_ok=True)

    if duration >= 3.0:
        seek_time = "00:00:03"
    elif duration > 0.5:
        seek_sec = duration / 2.0
        seek_time = f"00:00:0{seek_sec:.2f}"
    else:
        seek_time = "00:00:00"

    cmd = [
        ffmpeg_bin,
        "-y",
        "-ss", seek_time,
        "-i", str(input_file),
        "-vframes", "1",
        "-q:v", "2",
        str(out_thumb_path),
    ]

    run_command(cmd, f"Extracting thumbnail from {Path(input_file).name}")
    return out_thumb_path


class Transcoder:
    """Class wrapper providing object-oriented access to video probe and transcoding operations."""

    def __init__(self, ffmpeg_path: Optional[str] = None, ffprobe_path: Optional[str] = None):
        self.ffmpeg_bin = find_ffmpeg(ffmpeg_path)
        self.ffprobe_bin = find_ffprobe(ffprobe_path)

    def probe(self, input_file: Union[str, Path]) -> Dict[str, Any]:
        """Probes technical metadata for input file."""
        return probe_video(input_file, self.ffprobe_bin)

    def encode_hls(
        self,
        input_file: Union[str, Path],
        output_dir: Union[str, Path],
        profile: str,
        width: int,
        height: int,
        video_bitrate: int,
        audio_bitrate: int,
        segment_duration: int = 6,
    ) -> Dict[str, Any]:
        """Encodes video to HLS rendition folder and computes byte size and duration."""
        out_dir = Path(output_dir)
        playlist_path = transcode_to_hls_rendition(
            ffmpeg_bin=self.ffmpeg_bin,
            input_file=input_file,
            output_dir=out_dir,
            width=width,
            height=height,
            video_bitrate_bps=video_bitrate,
            audio_bitrate_bps=audio_bitrate,
            segment_duration=segment_duration,
        )

        # Calculate total byte size of generated rendition files (.ts segments + playlist.m3u8)
        total_size = sum(f.stat().st_size for f in out_dir.glob("*") if f.is_file())

        # Probe the generated rendition for exact duration
        probed = probe_video(input_file, self.ffprobe_bin)
        duration = probed.get("duration", 0.0)

        return {
            "size_bytes": total_size,
            "duration": duration,
            "playlist_path": str(playlist_path),
        }

    def create_master_playlist(
        self,
        work_dir: Union[str, Path],
        completed_renditions: List[Dict[str, Any]],
    ) -> str:
        """Generates master.m3u8 adaptive playlist in the work_dir."""
        master_path = Path(work_dir) / "master.m3u8"
        formatted_renditions = []

        for rend in completed_renditions:
            w = rend.get("width")
            h = rend.get("height")
            profile = rend.get("profileKey", "")
            if not h and "p" in profile:
                try:
                    h = int(profile.split("p")[0].split("_")[-1])
                except Exception:
                    h = 720
            if not w and h:
                w = int(h * 16 / 9)

            rel_uri = f"renditions/{profile}/playlist.m3u8"
            formatted_renditions.append({
                "width": w or 1280,
                "height": h or 720,
                "videoBitrateBps": rend.get("videoBitrateBps", 2800000),
                "audioBitrateBps": rend.get("audioBitrateBps", 128000),
                "relative_uri": rel_uri,
            })

        generate_master_playlist(formatted_renditions, master_path)
        return str(master_path)

    def extract_thumbnail(
        self,
        input_file: Union[str, Path],
        output_path: Union[str, Path],
        duration: float = 0.0,
    ) -> str:
        """Extracts poster frame to output_path and returns the path string."""
        thumb_path = extract_thumbnail(
            ffmpeg_bin=self.ffmpeg_bin,
            input_file=input_file,
            output_thumbnail_file=output_path,
            duration=duration,
        )
        return str(thumb_path)
