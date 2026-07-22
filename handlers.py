import abc
import json
import logging
import re
import shutil
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

    @staticmethod
    def _get_duration(path: Path) -> float:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        data = json.loads(result.stdout)
        return float(data["format"]["duration"])


# ---------- Defreeze helper functions ----------


def _ffmpeg_freeze_detect(file_path: Path, threshold: str) -> list[tuple[float, float]]:
    cmd = [
        "ffmpeg", "-i", str(file_path),
        "-vf", f"freezedetect=n={threshold}:d=0.5",
        "-map", "0:v:0", "-f", "null", "-"
    ]
    result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    timestamps = re.findall(r"freeze_start: (\d+\.\d+)|freeze_end: (\d+\.\d+)", result.stderr)
    freeze_ranges = []
    start = None
    for start_time, end_time in timestamps:
        if start_time:
            start = float(start_time)
        if end_time and start is not None:
            freeze_ranges.append((start, float(end_time)))
            start = None
    return freeze_ranges


def _ffmpeg_silence_detect(file_path: Path, threshold: str) -> list[tuple[float, float]]:
    cmd = [
        "ffmpeg", "-i", str(file_path),
        "-af", f"silencedetect=noise={threshold}:d=0.5",
        "-f", "null", "-"
    ]
    result = subprocess.run(cmd, stderr=subprocess.PIPE, text=True)
    timestamps = re.findall(r"silence_start: (\d+\.\d+)|silence_end: (\d+\.\d+)", result.stderr)
    silence_ranges = []
    start = None
    for start_time, end_time in timestamps:
        if start_time:
            start = float(start_time)
        if end_time and start is not None:
            silence_ranges.append((start, float(end_time)))
            start = None
    return silence_ranges


def _merge_intervals(video_gaps: list[tuple[float, float]], audio_gaps: list[tuple[float, float]], min_duration: float = 1.5) -> list[tuple[float, float]]:
    merged = []
    i, j = 0, 0
    while i < len(video_gaps) and j < len(audio_gaps):
        v_start, v_end = video_gaps[i]
        a_start, a_end = audio_gaps[j]

        overlap_start = max(v_start, a_start)
        overlap_end = min(v_end, a_end)

        if overlap_end > overlap_start and (overlap_end - overlap_start) >= min_duration:
            merged.append((overlap_start, overlap_end))

        if v_end < a_end:
            i += 1
        else:
            j += 1

    return merged


# ---------- EncodeTask ----------


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
        logger.debug(f"[EncodeTask] Command | {' '.join(command)}")

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            duration = self._get_duration(video_path)
            logger.debug(f"[EncodeTask] Duration | ticket_id={self.task_id} duration={duration:.3f}s")

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
                    logger.debug(f"[EncodeTask] FFmpeg failed | ticket_id={self.task_id} return_code={return_code} error={job['error']}")

        except Exception as e:
            logging.exception(e)
            with lock:
                job["status"] = "failed"
                job["error"] = str(e)

        logger.warning(f"[EncodeTask] Done | ticket_id={self.task_id} status={job['status']}")

    def cancel(self):
        self.status = "cancelled"

register_handler(EncodeTask)


# ---------- DefreezeTask ----------


class DefreezeTask(TaskHandler):
    TASK_TYPE = "DEFREEZE"

    FFMPEG_CONFIG = {
        "video_codec": "libx264",
        "audio_codec": "aac",
        "profile": "high",
        "level": "4.2",
        "crf": 30,
        "preset": "slower",
        "audio_bitrate": "128k",
    }

    @property
    def id(self) -> str:
        return "DEFREEZE"

    def execute(self, files: dict, job: dict, lock: object):
        video_path = Path(files["video"])
        output_file = video_path.parent / "output.mp4"

        options_file = Path(files["options"])
        with open(options_file, "r") as f:
            options = json.load(f)

        freeze_db = options.get("freeze_db", "-60dB")
        silence_noise = options.get("silence_noise", "-30dB")
        min_duration = options.get("min_duration", 1.5)
        force_encode = options.get("force_encode", False)

        with lock:
            job["progress"] = 0.0
            job["status_detail"] = "Analyzing video"

        # Stage 1: Detect freezes (0-25%)
        with lock:
            job["status_detail"] = "Detecting video freezes"
        logger.debug(f"[DefreezeTask] freeze_detect | ticket_id={self.task_id} threshold={freeze_db}")
        freeze_intervals = _ffmpeg_freeze_detect(video_path, freeze_db)

        with lock:
            job["progress"] = 0.25
            logger.warning(f"[DefreezeTask] Found {len(freeze_intervals)} freeze intervals")
        logger.debug(f"[DefreezeTask] freeze intervals | {freeze_intervals}")

        # Stage 2: Detect silence (25-50%)
        with lock:
            job["status_detail"] = "Detecting audio silence"
        logger.debug(f"[DefreezeTask] silence_detect | ticket_id={self.task_id} threshold={silence_noise}")
        silence_intervals = _ffmpeg_silence_detect(video_path, silence_noise)

        with lock:
            job["progress"] = 0.50
            logger.warning(f"[DefreezeTask] Found {len(silence_intervals)} silence intervals")
        logger.debug(f"[DefreezeTask] silence intervals | {silence_intervals}")

        # Stage 3: Merge gaps (50-60%)
        with lock:
            job["status_detail"] = "Merging gap intervals"
        gap_intervals = _merge_intervals(freeze_intervals, silence_intervals, min_duration)
        logger.debug(f"[DefreezeTask] merged gap intervals | min_duration={min_duration} count={len(gap_intervals)} intervals={gap_intervals}")

        if not gap_intervals:
            logger.warning("[DefreezeTask] No gaps found")
            if force_encode:
                cmd = [
                    "ffmpeg", "-y", "-i", str(video_path),
                    "-profile:v", self.FFMPEG_CONFIG["profile"],
                    "-level", str(self.FFMPEG_CONFIG["level"]),
                    "-crf", str(self.FFMPEG_CONFIG["crf"]),
                    "-movflags", "+faststart",
                    "-c:v", self.FFMPEG_CONFIG["video_codec"],
                    "-c:a", self.FFMPEG_CONFIG["audio_codec"],
                    "-b:a", self.FFMPEG_CONFIG["audio_bitrate"],
                    "-preset", self.FFMPEG_CONFIG["preset"],
                    str(output_file)
                ]
                subprocess.run(cmd, check=True)
            else:
                shutil.copy(video_path, output_file)

            with lock:
                job["progress"] = 1.0
                job["status_detail"] = "Complete"
                job["status"] = "done"
                job["output_file"] = output_file
            logger.warning(f"[DefreezeTask] Done | ticket_id={self.task_id} status=done (no gaps)")
            return

        # Stage 4: Cut segments via concat filter (60-100%)
        with lock:
            job["status_detail"] = "Cutting and encoding segments"

        inputs = []
        filter_inputs = []
        last_end = 0.0
        segment_index = 0

        for start, end in gap_intervals:
            if last_end > 0 and abs(last_end - start) <= 0.001:
                continue
            inputs.extend(["-ss", str(last_end), "-to", str(start), "-i", str(video_path)])
            filter_inputs.append(f"[{segment_index}:v:0][{segment_index}:a:0]")
            last_end = end
            segment_index += 1

        inputs.extend(["-ss", str(last_end), "-i", str(video_path)])
        filter_inputs.append(f"[{segment_index}:v:0][{segment_index}:a:0]")

        filter_complex = f"{''.join(filter_inputs)}concat=n={segment_index + 1}:v=1:a=1[outv][outa]"

        cmd = [
            "ffmpeg", "-y", *inputs,
            "-progress", "pipe:1",
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "[outa]",
            "-profile:v", self.FFMPEG_CONFIG["profile"],
            "-level", str(self.FFMPEG_CONFIG["level"]),
            "-crf", str(self.FFMPEG_CONFIG["crf"]),
            "-movflags", "+faststart",
            "-c:v", self.FFMPEG_CONFIG["video_codec"],
            "-c:a", self.FFMPEG_CONFIG["audio_codec"],
            "-b:a", self.FFMPEG_CONFIG["audio_bitrate"],
            "-preset", self.FFMPEG_CONFIG["preset"],
            str(output_file)
        ]

        logger.warning(f"[DefreezeTask] Starting concat encode | ticket_id={self.task_id} segments={segment_index + 1}")
        logger.debug(f"[DefreezeTask] concat command | {' '.join(cmd)}")

        try:
            duration = self._get_duration(video_path)
            logger.debug(f"[DefreezeTask] Duration | ticket_id={self.task_id} duration={duration:.3f}s")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            progress_accumulated = False

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                
                out_time_ms_str = ""
                if "out_time_ms=" in line:
                    parts = line.split("=", 1)
                    out_time_ms_str = parts[1].strip() if len(parts) > 1 else ""
                
                if out_time_ms_str:
                    try:
                        out_time_sec = float(out_time_ms_str) / 1_000_000.0
                    except (ValueError, ZeroDivisionError):
                        continue
                        
                    if duration > 0:
                        with lock:
                            progress_accumulated = True
                            job["progress"] = min(0.65 + (out_time_sec / duration) * 0.35, 0.99)
                elif line == "progress=end":
                    with lock:
                        progress_accumulated = True
                        job["progress"] = 1.0

            return_code = proc.wait()

            with lock:
                if return_code == 0 and output_file.exists():
                    job["status"] = "done"
                    job["output_file"] = output_file
                    
                    if progress_accumulated:
                        job["progress"] = 1.0
                    else:
                        logger.warning(f"[DefreezeTask] Done | ticket_id={self.task_id} status=done (no progress output)")
                        
                    job["status_detail"] = "Complete"
                    logger.warning(f"[DefreezeTask] Done | ticket_id={self.task_id} status=done")
                else:
                    job["status"] = "failed"
                    job["error"] = f"FFmpeg exited with code {return_code}"
                    logger.debug(f"[DefreezeTask] FFmpeg stderr not captured (stdout consumed by progress reader)")
                    logger.warning(f"[DefreezeTask] Failed | ticket_id={self.task_id} code={return_code}")

        except Exception as e:
            logging.exception(e)
            with lock:
                job["status"] = "failed"
                job["error"] = str(e)

    def cancel(self):
        self.status = "cancelled"


register_handler(DefreezeTask)


# ---------- TestTask ----------


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
