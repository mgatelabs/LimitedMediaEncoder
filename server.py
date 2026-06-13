import json
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, request, jsonify, send_file, after_this_request

app = Flask(__name__)

# -------------------------------
# Configuration
# -------------------------------
BASE_DIR = Path(os.getcwd())
TEMP_DIR = BASE_DIR / "temp"
shutil.rmtree(TEMP_DIR, ignore_errors=True)
TEMP_DIR.mkdir(exist_ok=True)

JOBS_LOCK = threading.Lock()

FFMPEG_CONFIG = {
    "resolution": 3840,
    "codec": "libx264",
    "audio_codec": "aac",
    "profile": "high",
    "level": "4.2",
}

JOBS = {}
JOB_QUEUE = queue.Queue()

def get_duration(path):
    """Measure the duration of a media file using ffprobe.

    Args:
        path (Path): Path to the input media file.

    Returns:
        float: Duration of the video in seconds.
    """
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

# -------------------------------
# Job Processor
# -------------------------------
def job_worker():
    """Background thread: process one job at a time."""
    while True:
        ticket_id = JOB_QUEUE.get()
        with JOBS_LOCK:
            job = JOBS.get(ticket_id)
        if not job:
            continue

        print(f'Working on {ticket_id}')

        folder = job["folder"]
        input_file = folder / "input_file"
        srt_file = folder / "input.srt"
        output_file = folder / "output.mp4"

        # Load options
        options_file = folder / "options.json"
        with open(options_file, "r") as f:
            options = json.load(f)

        ffmpeg_preset = options.get("ffmpeg_preset", "medium")
        stereo = options.get("stereo", True)
        audio_bitrate = str(options.get("audio_bitrate", 128))
        channels = "2" if stereo else "1"
        resolution = FFMPEG_CONFIG["resolution"]

        vf_res = f"min({resolution},iw)"
        if os.path.exists(srt_file):
            srt_path = Path(srt_file).resolve()
            srt_escaped = str(srt_path).replace("\\", "\\\\").replace(":", "\\:")
            vf_arg = f"scale='{vf_res}':-2,subtitles='{srt_escaped}'"
        else:
            vf_arg = f"scale='{vf_res}':-2"

        command = [
            "ffmpeg",
            "-y",
            "-i", str(input_file),
            "-vf", vf_arg,
            "-loglevel", "error",
            "-c:v", FFMPEG_CONFIG["codec"],
            "-progress", "pipe:1",
            "-nostats",
            "-preset", ffmpeg_preset,
            "-profile:v", FFMPEG_CONFIG["profile"],
            "-level", str(FFMPEG_CONFIG["level"]),
            "-movflags", "+faststart",
            "-c:a", FFMPEG_CONFIG["audio_codec"],
            "-b:a", f"{audio_bitrate}k",
            "-ac", channels,
            str(output_file)
        ]

        print(f'Encoding {input_file} -> {output_file}')

        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )

            duration = get_duration(input_file)

            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("out_time_ms="):
                    out_time_ms = int(line.split("=")[1])
                    if duration > 0:
                        progress = min(out_time_ms / (duration * 1_000_000), 1.0)
                        with JOBS_LOCK:
                            job["progress"] = progress
                elif line == "progress=end":
                    with JOBS_LOCK:
                        job["progress"] = 1.0

            return_code = proc.wait()

            with JOBS_LOCK:
                if return_code == 0 and os.path.exists(output_file):
                    job["status"] = "done"
                    job["output_file"] = output_file
                else:
                    captured_stderr = proc.stdout.getvalue() if hasattr(proc, 'stdout') and hasattr(proc.stdout, 'getvalue') else None
                    job["status"] = "failed"
                    job["error"] = str(captured_stderr).strip() if captured_stderr else "Unknown FFmpeg error"

        except Exception as e:
            with JOBS_LOCK:
                job["status"] = "failed"
                job["error"] = str(e)

        JOB_QUEUE.task_done()


@app.route("/encode/start", methods=["POST"])
def encode_start():
    """Start a new encoding job."""
    try:
        ticket_id = str(uuid.uuid4())
        job_folder = TEMP_DIR / ticket_id
        job_folder.mkdir(parents=True, exist_ok=True)

        # Save input file(s)
        input_file = request.files.get("input_file")
        if not input_file:
            return jsonify({"error": "Missing input_file"}), 400

        input_path = job_folder / "input_file"
        input_file.save(input_path)

        srt_file = request.files.get("srt_file")
        if srt_file:
            srt_path = job_folder / "input.srt"
            srt_file.save(srt_path)

        # Save JSON options
        options_raw = request.form.get("options", "{}")
        try:
            options = json.loads(options_raw)
        except json.JSONDecodeError:
            options = {}
        with open(job_folder / "options.json", "w") as f:
            json.dump(options, f, indent=2)

        # Create job entry under lock
        with JOBS_LOCK:
            JOBS[ticket_id] = {
                "status": "queued",
                "progress": 0.0,
                "folder": job_folder,
                "output_file": None,
                "error": None,
                "worker": None,
                "worker_name": None,
            }

        JOB_QUEUE.put(ticket_id)
        return jsonify({"ticket_id": ticket_id, "status": "queued"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/encode/status/<ticket_id>", methods=["GET"])
def encode_status(ticket_id):
    with JOBS_LOCK:
        job = dict(JOBS.get(ticket_id, {}))
        if not job or "status" not in job:
            return jsonify({"error": "Invalid ticket"}), 404

    status = job["status"]
    progress = job.get("progress", 0.0)
    response = {
        "ticket_id": ticket_id,
        "status": status,
        "worker": job.get("worker_name") or job.get("worker"),
    }

    if progress is not None:
        response["progress"] = str(round(float(progress) * 100, 2))

    if status == "done" or status == "failed":
        output_file = job.get("output_file")
        response["has_output_file"] = bool(output_file)
        if output_file:
            response["output_path"] = str(job["output_file"])

        folder = job.get("folder")
        if folder:
            response["cleanup_path"] = str(folder)

    if "error" in job and job["error"]:
        error_msg = job["error"]
        response["error"] = str(error_msg).strip()

    return jsonify(response)


@app.route("/encode/result/<ticket_id>", methods=["GET"])
def encode_result(ticket_id):
    with JOBS_LOCK:
        job = dict(JOBS.get(ticket_id, {}))
        if not job or "status" not in job or job["status"] != "done":
            return jsonify({"error": "Job not completed"}), 400

        output_file = job.get("output_file")
        folder = job.get("folder")
        if not output_file or not folder:
            return jsonify({"error": "Job not completed"}), 400

    @after_this_request
    def cleanup(response):
        output_path = output_file
        def delayed_cleanup():
            time.sleep(10)
            try:
                shutil.rmtree(output_path, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(ticket_id, None)
                    print(f"[CLEANUP] Removed {output_path}")
            except Exception as e:
                print(f"[CLEANUP ERROR] {e}")

        threading.Thread(target=delayed_cleanup, daemon=True).start()
        return response

    return send_file(
        output_file,
        as_attachment=True,
        download_name=f"{ticket_id}.mp4"
    )


# -------------------------------
# App Startup
# -------------------------------
def main(port: int = 8080, num_workers: int = 3):
    """Start Flask app and N worker threads."""
    for i in range(num_workers):
        t = threading.Thread(target=job_worker, daemon=True, name=f"JobWorker-{i + 1}")
        t.start()
        print(f"[INFO] Started worker thread {t.name}")

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Simple Flask-based encoding server")
    parser.add_argument("--port", type=int, default=8080, help="Port to run the server on")
    parser.add_argument("--workers", type=int, default=3, help="Number of background encoding threads")
    args = parser.parse_args()
    main(args.port, args.workers)
