import abc
import json
import logging
import subprocess
import time
from pathlib import Path


logger = logging.getLogger(__name__)

HANDLER_CLASSES = {}


def register_handler(cls):
    if cls.TASK_TYPE:
        HANDLER_CLASSES[cls.TASK_TYPE] = cls
    return cls


def create_handler(task_type: str, task_id: str) -> "TaskHandler":
    if task_type not in HANDLER_CLASSES:
        raise ValueError(f"Unknown task type: {task_type}")
    return HANDLER_CLASSES[task_type](task_id=task_id)


class TaskHandler(abc.ABC):
    TASK_TYPE = None

    def __init__(self, task_id: str):
        self.task_id = task_id
        self.status = "idle"

    @property
    @abc.abstractmethod
    def id(self) -> str:
        pass

    @abc.abstractmethod
    def execute(self, files: dict, job: dict, lock: object):
        pass

    @abc.abstractmethod
    def cancel(self):
        pass


class EncodeTask(TaskHandler):
    TASK_TYPE = "ENCODE"

    FFMPEG_CONFIG = {
        "resolution": 3840,
        "codec": "libx264",
        "audio_codec": "aac",
        "profile": "high",
        "level": "4.2",
    }

    @property
    def id(self) -> str:
        return "ENCODE"

    def execute(self, files: dict, job: dict, lock: object):
        video_path = Path(files["video"])
        srt_path = Path(files.get("srt")) if "srt" in files else None
        output_file = video_path.parent / "output.mp4"

        job["progress"] = 0.0

        options_file = Path(files["options"])
        with open(options_file, "r") as f:
            options = json.load(f)

        ffmpeg_preset = options.get("ffmpeg_preset", "medium")
        stereo = options.get("stereo", True)
        audio_bitrate = str(options.get("audio_bitrate", 128))
        channels = "2" if stereo else "1"
        resolution = self.FFMPEG_CONFIG["resolution"]

        vf_res = f"min({resolution},iw)"
        if srt_path and srt_path.exists():
            srt_escaped = str(srt_path).replace("\\", "\\\\").replace(":", "\\:")
            vf_arg = f"scale='{vf_res}':-2,subtitles='{srt_escaped}'"
        else:
            vf_arg = f"scale='{vf_res}':-2"

        command = [
            "ffmpeg",
            "-y",
            "-i", str(video_path),
            "-vf", vf_arg,
            "-loglevel", "error",
            "-c:v", self.FFMPEG_CONFIG["codec"],
            "-progress", "pipe:1",
            "-nostats",
            "-preset", ffmpeg_preset,
            "-profile:v", self.FFMPEG_CONFIG["profile"],
            "-level", str(self.FFMPEG_CONFIG["level"]),
            "-movflags", "+faststart",
            "-c:a", self.FFMPEG_CONFIG["audio_codec"],
            "-b:a", f"{audio_bitrate}k",
            "-ac", channels,
            str(output_file)
        ]

        logger.warning(f"[EncodeTask] Starting | ticket_id={self.task_id} input={video_path}")

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            duration = self._get_duration(video_path)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("out_time_ms="):
                    out_time_ms = int(line.split("=")[1])
                    if duration > 0:
                        progress = min(out_time_ms / (duration * 1_000_000), 1.0)
                        with lock:
                            job["progress"] = progress
                elif line == "progress=end":
                    with lock:
                        job["progress"] = 1.0

            return_code = proc.wait()

            with lock:
                if return_code == 0 and output_file.exists():
                    job["status"] = "done"
                    job["output_file"] = output_file
                else:
                    captured_stderr = proc.stdout.getvalue() if hasattr(proc, 'stdout') and hasattr(proc.stdout, 'getvalue') else None
                    job["status"] = "failed"
                    job["error"] = str(captured_stderr).strip() if captured_stderr else "Unknown FFmpeg error"

        except Exception as e:
            with lock:
                job["status"] = "failed"
                job["error"] = str(e)

        logger.warning(f"[EncodeTask] Done | ticket_id={self.task_id} status={job['status']}")

    def cancel(self):
        self.status = "cancelled"

    @staticmethod
    def _get_duration(path: Path):
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                str(path)
            ],
            capture_output=True,
            text=True
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])


register_handler(EncodeTask)


class TestTask(TaskHandler):
    TASK_TYPE = "TEST"

    def execute(self, files: dict, job: dict, lock: object):
        job["progress"] = 0.0

        for i in range(1, 101):
            time.sleep(1)
            with lock:
                job["progress"] = i / 100

        job["status"] = "done"
        logger.warning(f"[TestTask] Done | ticket_id={self.task_id}")

    @property
    def id(self) -> str:
        return "TEST"

    def cancel(self):
        self.status = "cancelled"

register_handler(TestTask)