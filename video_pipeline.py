import os
import sys
import time
import glob
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional


def find_ffmpeg() -> str:
    """
    Locates the ffmpeg executable on the system.
    Checks PATH, imageio_ffmpeg package, environment variables, common installation directories, and winget packages.
    """
    # 1. Custom environment variable
    if os.environ.get("FFMPEG_PATH"):
        custom_path = os.environ["FFMPEG_PATH"]
        if os.path.exists(custom_path):
            return custom_path

    # 2. Standard system PATH
    ffmpeg_in_path = shutil.which("ffmpeg")
    if ffmpeg_in_path:
        return ffmpeg_in_path

    # 3. Python global package (imageio_ffmpeg)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass

    # 3. Common Windows paths (including winget packages)
    user_profile = os.environ.get("USERPROFILE", "")
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    program_files = os.environ.get("ProgramFiles", "C:\\Program Files")

    candidate_locations = [
        # WinGet links & packages
        os.path.join(local_app_data, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
        # Root directories
        "C:\\ffmpeg\\bin\\ffmpeg.exe",
        "C:\\tools\\ffmpeg\\bin\\ffmpeg.exe",
        # Current directory
        os.path.join(os.getcwd(), "ffmpeg.exe"),
    ]

    for cand in candidate_locations:
        if os.path.isfile(cand):
            return cand

    # Search winget packages folder if present
    winget_packages_dir = os.path.join(local_app_data, "Microsoft", "WinGet", "Packages")
    if os.path.isdir(winget_packages_dir):
        for root, _, files in os.walk(winget_packages_dir):
            if "ffmpeg.exe" in files:
                return os.path.join(root, "ffmpeg.exe")

    raise FileNotFoundError(
        "FFmpeg was not found on your system.\n"
        "Please ensure FFmpeg is installed and added to PATH, or set the FFMPEG_PATH environment variable."
    )


def format_duration(seconds: float) -> str:
    """Formats seconds into human-readable duration (e.g. '12.34s', '3m 15.2s', or '1h 05m 12s')."""
    if seconds < 60:
        return f"{seconds:.2f}s"
    total_seconds = int(seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    rem_seconds = seconds % 60
    if hours > 0:
        return f"{hours}h {minutes:02d}m {rem_seconds:04.1f}s ({seconds:.2f}s total)"
    return f"{minutes}m {rem_seconds:04.1f}s ({seconds:.2f}s total)"


def run_command(cmd: List[str], desc: str) -> float:
    """Executes a subprocess command with timing and clear error reporting."""
    print(f"\n[EXEC] {desc}...")
    print(f"Command: {' '.join(cmd)}")
    start_time = time.time()
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    duration = time.time() - start_time
    if result.returncode != 0:
        print(f"[ERROR] Failed during: {desc} (after {format_duration(duration)})", file=sys.stderr)
        print(f"[STDERR]\n{result.stderr}", file=sys.stderr)
        raise RuntimeError(f"FFmpeg command failed with return code {result.returncode}")
    print(f"[SUCCESS] {desc} completed in {format_duration(duration)}.")
    return duration


# ==============================================================================
# STEP 1: TRANSCODING
# ==============================================================================
def transcode_to_rendition(
    ffmpeg_bin: str,
    input_file: Path,
    output_file: Path,
    width: int,
    height: int,
    video_bitrate: str,
    audio_bitrate: str,
    keyframe_interval: int = 48,
) -> Path:
    """
    Transcodes a video file to a specific resolution and bitrate.
    Enforces closed GOP (Group of Pictures) keyframe interval for seamless HLS slicing later.
    """
    output_file.parent.mkdir(parents=True, exist_ok=True)

    # Scale filter maintains aspect ratio without stretching, fitting within width x height
    scale_filter = f"scale=w={width}:h={height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"

    cmd = [
        ffmpeg_bin,
        "-y",                     # Overwrite output without asking
        "-i", str(input_file),    # Input file
        "-vf", scale_filter,      # Video scaling + padding filter
        "-c:v", "libx264",        # H.264 video codec
        "-profile:v", "main",     # H.264 profile
        "-crf", "20",             # Constant Rate Factor (high quality)
        "-preset", "medium",      # Encoding speed vs compression preset
        "-b:v", video_bitrate,    # Target video bitrate
        "-maxrate", video_bitrate,
        "-bufsize", f"{int(video_bitrate.rstrip('k')) * 2}k",
        "-c:a", "aac",            # AAC audio codec
        "-b:a", audio_bitrate,    # Target audio bitrate
        "-ar", "44100",           # 44.1kHz audio sampling
        "-ac", "2",               # Stereo audio
        # Keyframe intervals: Ensures exact keyframes for clean HLS transmuxing
        "-g", str(keyframe_interval),
        "-keyint_min", str(keyframe_interval),
        "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",    # Broadest device compatibility
        "-movflags", "+faststart", # Move moov atom to front for instant web/player streaming of MP4
        str(output_file),
    ]

    run_command(cmd, f"Transcoding {input_file.name} to {height}p MP4 ({output_file.name})")
    return output_file


AVAILABLE_PROFILES: Dict[str, Dict] = {
    "1080p": {
        "width": 1920,
        "height": 1080,
        "video_bitrate": "5000k",
        "audio_bitrate": "192k",
        "bandwidth": 5500000,
    },
    "720p": {
        "width": 1280,
        "height": 720,
        "video_bitrate": "2800k",
        "audio_bitrate": "128k",
        "bandwidth": 3000000,
    },
    "480p": {
        "width": 854,
        "height": 480,
        "video_bitrate": "1400k",
        "audio_bitrate": "96k",
        "bandwidth": 1500000,
    },
    "360p": {
        "width": 640,
        "height": 360,
        "video_bitrate": "800k",
        "audio_bitrate": "96k",
        "bandwidth": 900000,
    },
}


def normalize_and_validate_resolutions(resolutions: Optional[List[str]]) -> List[str]:
    """
    Normalizes resolution strings (e.g. ['1080', '720p'] -> ['1080p', '720p'])
    and validates them against available profiles.
    Returns them sorted in descending order of resolution.
    """
    if not resolutions:
        return ["1080p", "720p", "480p"]

    normalized: List[str] = []
    for r in resolutions:
        clean = r.strip().lower()
        if not clean.endswith("p"):
            clean = f"{clean}p"
        if clean not in AVAILABLE_PROFILES:
            supported = ", ".join(AVAILABLE_PROFILES.keys())
            raise ValueError(f"Unsupported resolution '{r}'. Supported resolutions are: {supported}")
        if clean not in normalized:
            normalized.append(clean)

    # Sort descending by height (e.g., 1080p, 720p, 480p, 360p)
    return sorted(normalized, key=lambda x: AVAILABLE_PROFILES[x]["height"], reverse=True)


def transcode_all_resolutions(
    ffmpeg_bin: str,
    input_file: Path,
    transcode_dir: Path,
    target_resolutions: Optional[List[str]] = None,
) -> Dict[str, Dict]:
    """
    Step 1: Transcodes the input video into intermediate MP4 files for each selected resolution.
    """
    transcode_dir.mkdir(parents=True, exist_ok=True)
    video_stem = input_file.stem
    active_resolutions = normalize_and_validate_resolutions(target_resolutions)

    renditions_config: Dict[str, Dict] = {}
    for res_name in active_resolutions:
        prof = AVAILABLE_PROFILES[res_name]
        renditions_config[res_name] = {
            "width": prof["width"],
            "height": prof["height"],
            "video_bitrate": prof["video_bitrate"],
            "audio_bitrate": prof["audio_bitrate"],
            "bandwidth": prof["bandwidth"],
            "output_file": transcode_dir / f"{video_stem}_{res_name}.mp4",
        }

    res_list_str = ", ".join(active_resolutions)
    print("\n" + "=" * 60)
    print(f"STEP 1: TRANSCODING '{input_file.name}' TO {res_list_str}")
    print("=" * 60)

    for label, conf in renditions_config.items():
        transcode_to_rendition(
            ffmpeg_bin=ffmpeg_bin,
            input_file=input_file,
            output_file=conf["output_file"],
            width=conf["width"],
            height=conf["height"],
            video_bitrate=conf["video_bitrate"],
            audio_bitrate=conf["audio_bitrate"],
        )

    return renditions_config


# ==============================================================================
# STEP 2: TRANSMUXING (REPACKAGING TO HLS WITHOUT RE-ENCODING)
# ==============================================================================
def transmux_to_hls_stream(
    ffmpeg_bin: str,
    input_mp4: Path,
    hls_out_dir: Path,
    segment_duration: int = 4,
) -> Path:
    """
    Transmuxes an existing MP4 file into HLS format (index.m3u8 + .ts segments).
    Uses stream copying ('-c copy') to avoid re-encoding overhead.
    """
    hls_out_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = hls_out_dir / "index.m3u8"
    segment_pattern = str(hls_out_dir / "segment_%03d.ts")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-i", str(input_mp4),
        "-c", "copy",                    # Stream copy: transmux without re-encoding!
        "-f", "hls",                     # Format: HLS
        "-hls_time", str(segment_duration),
        "-hls_list_size", "0",           # Keep all segments in the playlist (VOD)
        "-hls_segment_type", "mpegts",   # Standard MPEG-TS segments
        "-hls_playlist_type", "vod",     # VOD playlist
        "-hls_segment_filename", segment_pattern,
        str(playlist_path),
    ]

    run_command(cmd, f"Transmuxing {input_mp4.name} to HLS ({playlist_path.name})")
    return playlist_path


def generate_master_playlist(
    renditions: Dict[str, Dict],
    master_playlist_path: Path,
) -> Path:
    """
    Generates a multivariant HLS Master Playlist (master.m3u8).
    Allows HLS video players to automatically switch between 720p and 480p.
    """
    master_playlist_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
    ]

    for label, conf in renditions.items():
        w = conf["width"]
        h = conf["height"]
        bw = conf["bandwidth"]
        # Relative path from master.m3u8 to each variant's index.m3u8
        rel_playlist_path = f"{label}/index.m3u8"
        lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={w}x{h},NAME="{label}"')
        lines.append(rel_playlist_path)

    content = "\n".join(lines) + "\n"
    master_playlist_path.write_text(content, encoding="utf-8")
    print(f"\n[SUCCESS] Generated Master Playlist at: {master_playlist_path}")
    return master_playlist_path


def transmux_all_to_hls(
    ffmpeg_bin: str,
    renditions: Dict[str, Dict],
    hls_root_dir: Path,
    segment_duration: int = 4,
) -> Path:
    """
    Step 2: Transmuxes each transcoded MP4 rendition into HLS and builds the master playlist.
    """
    hls_root_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("STEP 2: TRANSMUXING MP4 FILES TO HLS (STREAM COPY / NO RE-ENCODING)")
    print("=" * 60)

    for label, conf in renditions.items():
        variant_dir = hls_root_dir / label
        transmux_to_hls_stream(
            ffmpeg_bin=ffmpeg_bin,
            input_mp4=conf["output_file"],
            hls_out_dir=variant_dir,
            segment_duration=segment_duration,
        )

    # Build Master Playlist at the root of HLS directory
    master_playlist = hls_root_dir / "master.m3u8"
    generate_master_playlist(renditions, master_playlist)
    return master_playlist


# ==============================================================================
# PIPELINE ENTRY POINT & TEST HELPER
# ==============================================================================
def create_sample_video_if_needed(ffmpeg_bin: str, target_path: Path) -> Path:
    """Creates a 10-second synthetic test video with audio tone for quick validation."""
    if target_path.exists():
        return target_path

    print(f"\nGenerating a sample test video at: {target_path} ...")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f", "lavfi",
        "-i", "testsrc=duration=10:size=1920x1080:rate=24",
        "-f", "lavfi",
        "-i", "sine=frequency=1000:duration=10",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-shortest",
        str(target_path),
    ]
    run_command(cmd, "Generating sample video")
    return target_path


def run_full_pipeline(
    input_video: str,
    output_dir: str = "output",
    segment_duration: int = 4,
    resolutions: Optional[List[str]] = None,
) -> Path:
    """
    Orchestrates the complete workflow:
    1. Creates a dedicated folder inside `output_dir` named after the video (output/<video_name>/).
    2. Transcodes input video into requested resolutions (e.g. 1080p, 720p, 480p) MP4 files in <output_dir>/<video_name>/mp4/.
    3. Transmuxes each MP4 into HLS segments and generates the Master Playlist in <output_dir>/<video_name>/hls/.
    """
    ffmpeg_bin = find_ffmpeg()
    print(f"[INFO] Using FFmpeg executable at: {ffmpeg_bin}")

    input_path = Path(input_video).resolve()
    base_out = Path(output_dir).resolve()

    if not input_path.exists():
        raise FileNotFoundError(f"Input video not found: {input_path}")

    # Sub-directory named after the original video name (without extension)
    video_stem = input_path.stem
    video_dir = base_out / video_stem
    mp4_dir = video_dir / "mp4"
    hls_dir = video_dir / "hls"

    print(f"\n[INFO] Processing video: {input_path.name}")
    print(f"[INFO] Saving results into directory: {video_dir}")
    pipeline_start = time.time()

    # Step 1: Transcode to standalone MP4s (Permanently kept)
    renditions = transcode_all_resolutions(
        ffmpeg_bin=ffmpeg_bin,
        input_file=input_path,
        transcode_dir=mp4_dir,
        target_resolutions=resolutions,
    )

    # Step 2: Transmux each MP4 to HLS (Stream Copy / No Re-encoding)
    master_playlist = transmux_all_to_hls(
        ffmpeg_bin=ffmpeg_bin,
        renditions=renditions,
        hls_root_dir=hls_dir,
        segment_duration=segment_duration,
    )

    elapsed_time = time.time() - pipeline_start

    print("\n" + "=" * 60)
    print(f"PIPELINE SUMMARY FOR: {input_path.name}")
    print(f"Location: {video_dir}")
    print(f"Total Time: {format_duration(elapsed_time)}")
    print("=" * 60)
    print("Standalone MP4 Files (Kept permanently, web-optimized with faststart):")
    for label, conf in renditions.items():
        fpath = conf["output_file"]
        size_kb = fpath.stat().st_size / 1024 if fpath.exists() else 0
        print(f"  - [{label} MP4]: {fpath} ({size_kb:.1f} KB)")

    print("\nHLS Stream Files (Transmuxed from MP4s):")
    print(f"  - [Master Playlist]: {master_playlist}")
    for label in renditions:
        v_playlist = hls_dir / label / "index.m3u8"
        seg_count = len(list((hls_dir / label).glob("*.ts")))
        print(f"  - [{label} Playlist]: {v_playlist} ({seg_count} segments)")
    print(f"\nProcessing completed successfully for: {input_path.name} in {format_duration(elapsed_time)}!")
    return video_dir


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Transcode video to multiple resolutions (1080p, 720p, 480p) and transmux to HLS."
    )
    parser.add_argument(
        "-i", "--input",
        nargs="+",
        type=str,
        default=None,
        help="Path(s) to input video file(s) or directory containing videos. (Can also set via INPUT_VIDEO env var)",
    )
    parser.add_argument(
        "-r", "--resolutions",
        nargs="+",
        type=str,
        default=None,
        help="Target resolutions to encode (e.g. 1080 720 480 or 1080p 720p 480p). Default: 1080p 720p 480p (Can also set via RESOLUTIONS env var)",
    )
    parser.add_argument(
        "-o", "--output-dir",
        type=str,
        default="output",
        help="Base output directory where a subfolder named after each video will be created. Default: ./output (Can also set via OUTPUT_DIR env var)",
    )
    parser.add_argument(
        "--segment-duration",
        type=int,
        default=4,
        help="HLS segment duration in seconds. (Default: 4)",
    )
    parser.add_argument(
        "-w", "--worker",
        action="store_true",
        help="Run as AWS SQS background video processing worker (Default if no input video specified)",
    )
    parser.add_argument(
        "--prompt-creds", "--setup",
        action="store_true",
        help="Interactively prompt for AWS SQS and AWS S3 credentials and save to .env",
    )
    parser.add_argument(
        "--check-services",
        action="store_true",
        help="Test connectivity to Backend API, AWS SQS, and AWS S3 and exit",
    )
    parser.add_argument(
        "--test-message",
        type=str,
        default=None,
        help="Path to a JSON file containing a test video-processing message payload to run locally without SQS",
    )

    args = parser.parse_args()

    # If worker flags are explicitly given or no arguments are provided, launch AWS SQS Worker
    if args.worker or args.prompt_creds or args.check_services or args.test_message or (not args.input and not args.create_sample and not os.environ.get("INPUT_VIDEO")):
        import worker
        worker.main()
        sys.exit(0)

    # Fallback to environment variables if CLI args are omitted (useful in Docker/Kubernetes)
    if not args.input and os.environ.get("INPUT_VIDEO"):
        args.input = os.environ["INPUT_VIDEO"].split()
    if not args.resolutions and os.environ.get("RESOLUTIONS"):
        args.resolutions = os.environ["RESOLUTIONS"].split()
    if args.output_dir == "output" and os.environ.get("OUTPUT_DIR"):
        args.output_dir = os.environ["OUTPUT_DIR"]

    # If --create-sample is given
    if args.create_sample:
        ffmpeg_exec = find_ffmpeg()
        sample_file = Path("sample_video.mp4").resolve()
        create_sample_video_if_needed(ffmpeg_exec, sample_file)
        run_full_pipeline(str(sample_file), args.output_dir, args.segment_duration, args.resolutions)
    elif args.input:
        resolved_files: List[Path] = []
        video_extensions = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".wmv", ".webm", ".m4v"}

        for item in args.input:
            p = Path(item)
            if p.is_file():
                resolved_files.append(p.resolve())
            elif p.is_dir():
                found = sorted([f.resolve() for f in p.iterdir() if f.is_file() and f.suffix.lower() in video_extensions])
                resolved_files.extend(found)
            elif any(char in item for char in ["*", "?"]):
                matched = sorted([Path(f).resolve() for f in glob.glob(item) if Path(f).is_file()])
                if matched:
                    resolved_files.extend(matched)
                else:
                    print(f"[WARNING] No files matched pattern: {item}")
            else:
                resolved_files.append(p.resolve())

        if not resolved_files:
            print("[ERROR] No input files found to process.")
            sys.exit(1)

        batch_start = time.time()
        for video_file in resolved_files:
            if not video_file.exists():
                print(f"[ERROR] Input video not found: {video_file}")
                continue
            run_full_pipeline(str(video_file), args.output_dir, args.segment_duration, args.resolutions)
        if len(resolved_files) > 1:
            total_time = time.time() - batch_start
            print(f"\n[ALL DONE] Finished processing {len(resolved_files)} videos in {format_duration(total_time)}.")

