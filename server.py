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
TEMP_DIR.mkdir(exist_ok=True)
JOBS = {}  # job_id -> {"status": str, "folder": Path, "output_file": Path | None, "error": str | None, "worker": str | None}
JOB_QUEUE = queue.Queue()
PROCESSING_THREAD = None


# -------------------------------
# Job Processor
# -------------------------------
def job_worker():
    """Background thread: process one job at a time."""
    while True:
        ticket_id = JOB_QUEUE.get()
        job = JOBS.get(ticket_id)
        if not job:
            continue

        print(f'Working on {ticket_id}')

        # 🆕 record which thread is handling this job
        job["worker"] = threading.current_thread().name

        job["status"] = "processing"
        folder = job["folder"]
        input_file = folder / "input_file"
        srt_file = folder / "input.srt"
        output_file = folder / "output.mp4"

        # Load options
        options_file = folder / "options.json"
        with open(options_file, "r") as f:
            options = json.load(f)

        ffmpeg_preset = options.get("ffmpeg_preset", "medium")
        crf = str(options.get("constant_rate_factor", 23))
        stereo = options.get("stereo", True)
        audio_bitrate = str(options.get("audio_bitrate", 128))
        channels = "2" if stereo else "1"

        if not os.path.exists(srt_file):
            vf_arg = "scale='min(3840,iw)':-2"
        else:

            srt_path = Path(srt_file).resolve()

            # Escape special chars for ffmpeg filter syntax
            srt_escaped = str(srt_path).replace("\\", "\\\\").replace(":", "\\:")

            vf_arg = f"scale='min(3840,iw)':-2,subtitles='{srt_escaped}'"

        command = [
            "ffmpeg",
            "-y",
            "-i", str(input_file),
            "-vf", vf_arg,
            "-c:v", "libx264",
            "-preset", ffmpeg_preset,
            "-profile:v", "high",
            "-level", "4.2",
            "-pix_fmt", "yuv420p",
            "-crf", crf,
            "-movflags", "+faststart",
            "-c:a", "aac",
            "-b:a", f"{audio_bitrate}k",
            "-ac", channels,
            str(output_file)
        ]

        print(command)

        try:
            result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if result.returncode == 0:
                job["status"] = "done"
                job["output_file"] = output_file
            else:
                print(result.stderr)
                job["status"] = "failed"
                job["error"] = result.stderr
        except Exception as e:
            job["status"] = "failed"
            job["error"] = str(e)

        JOB_QUEUE.task_done()


# -------------------------------
# Routes
# -------------------------------

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

        # Create job entry
        JOBS[ticket_id] = {
            "status": "queued",
            "folder": job_folder,
            "output_file": None,
            "error": None,
            "worker": None
        }

        JOB_QUEUE.put(ticket_id)

        return jsonify({"ticket_id": ticket_id, "status": "queued"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/encode/status/<ticket_id>", methods=["GET"])
def encode_status(ticket_id):
    job = JOBS.get(ticket_id)
    if not job:
        return jsonify({"error": "Invalid ticket"}), 404

    return jsonify({
        "ticket_id": ticket_id,
        "status": job["status"],
        "worker": job.get("worker"),
        "error": job.get("error")
    })


@app.route("/encode/result/<ticket_id>", methods=["GET"])
def encode_result(ticket_id):
    job = JOBS.get(ticket_id)
    if not job:
        return jsonify({"error": "Invalid ticket"}), 404

    if job["status"] != "done" or not job["output_file"]:
        return jsonify({"error": "Job not completed"}), 400

    output_path = job["output_file"]
    folder = job["folder"]

    @after_this_request
    def cleanup(response):
        # Delay slightly to ensure file handles are released (especially on Windows)
        def delayed_cleanup():
            time.sleep(1)
            try:
                shutil.rmtree(folder, ignore_errors=True)
                JOBS.pop(ticket_id, None)
                print(f"[CLEANUP] Removed {folder}")
            except Exception as e:
                print(f"[CLEANUP ERROR] {e}")

        threading.Thread(target=delayed_cleanup, daemon=True).start()
        return response

    return send_file(
        output_path,
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
