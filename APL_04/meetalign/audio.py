import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


def get_duration_seconds(path):
    """오디오 길이(초). ffprobe, mutagen 순으로 시도하고 측정할 수 없으면 None."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe:
        try:
            result = subprocess.run(
                [ffprobe, "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(float(result.stdout.strip()))
        except (OSError, ValueError, subprocess.SubprocessError):
            logger.warning("ffprobe failed for %s", path, exc_info=True)
    try:
        import mutagen
    except ImportError:
        return None
    try:
        audio = mutagen.File(str(path))
        if audio is not None and audio.info is not None:
            return int(audio.info.length)
    except Exception:
        logger.warning("mutagen failed for %s", path, exc_info=True)
    return None
