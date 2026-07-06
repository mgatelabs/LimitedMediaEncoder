import json
import logging
import logging.handlers
import os
import platform
import queue
import shutil
import threading
import time
import uuid
from pathlib import Path

# Enable VT100/ANSI processing on Windows 10+
if platform.system() == 'Windows':
    import msvcrt
    import ctypes
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)


import handlers

from flask import Flask, request, jsonify, send_file, after_this_request
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = None  # disable upload limit entirely
app.request_class.max_form_memory_size = None  # allow unlimited multi-part uploads to disk

# -------------------------------
# Configuration
# -------------------------------
BASE_DIR = Path(os.getcwd())
TEMP_DIR = BASE_DIR / "temp"
shutil.rmtree(TEMP_DIR, ignore_errors=True)
TEMP_DIR.mkdir(exist_ok=True)

JOBS_LOCK = threading.Lock()
ACTIVE_WORKERS = set()
RECENTLY_COMPLETED = {}  # ticket_id -> (job, timestamp) for 10s after completion

JOBS = {}

# -------------------------------
# Job Processor
# -------------------------------
JOB_QUEUE = queue.Queue()


def job_worker():
    while True:
        ticket_id = JOB_QUEUE.get()
        with JOBS_LOCK:
            job = JOBS.get(ticket_id)
        if not job:
            continue

        job["worker_name"] = threading.current_thread().name
        job["status"] = "active"
        ACTIVE_WORKERS.add(threading.current_thread().name)

        folder = job["folder"]
        
        files = {
            "video": str(folder / "input_file"),
            "options": str(folder / "options.json"),
        }
        if os.path.exists(folder / "input.srt"):
            files["srt"] = str(folder / "input.srt")

        try:
            handler = handlers.create_handler(job["task_type"], ticket_id)
            handler.execute(files, job, JOBS_LOCK)
        finally:
            JOB_QUEUE.task_done()

@app.route("/encode/start", methods=["POST"])
def encode_start():
    try:
        ticket_id = str(uuid.uuid4())
        job_folder = TEMP_DIR / ticket_id
        job_folder.mkdir(parents=True, exist_ok=True)

        logging.warning(f"[encode_start] New job created | ticket_id={ticket_id} folder={job_folder}")

        input_file = request.files.get("input_file")
        if not input_file:
            return jsonify({"error": "Missing input_file"}), 400

        input_path = job_folder / "input_file"
        input_file.save(input_path)
        file_size = input_path.stat().st_size
        logging.warning(f"[encode_start] Input received | filename={input_file.filename} size={file_size}")

        srt_file = request.files.get("srt_file")
        if srt_file:
            srt_path = job_folder / "input.srt"
            srt_file.save(srt_path)
            logging.warning(f"[encode_start] SRT received | filename={srt_file.filename}")

        options_raw = request.form.get("options", "{}")
        try:
            options = json.loads(options_raw)
        except json.JSONDecodeError:
            options = {}

        with open(job_folder / "options.json", "w") as f:
            json.dump(options, f, indent=2)

        job_id_short = ticket_id[:8]
        start_time = time.time()

        with JOBS_LOCK:
            JOBS[ticket_id] = {
                "ticket_id": ticket_id,
                "task_type": "ENCODE",
                "status": "queued",
                "progress": None,
                "folder": job_folder,
                "output_file": None,
                "error": None,
                "worker_name": None,
                "job_id_short": job_id_short,
                "start_time": start_time,
            }

        JOB_QUEUE.put(ticket_id)
        return jsonify({"ticket_id": ticket_id, "status": "queued"})

    except Exception as e:
        logging.exception(e)
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
        "worker": job.get("worker_name"),
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
            logging.warning(f"[encode_result] Invalid request | ticket_id={ticket_id} status={job.get('status', 'N/A')}")
            return jsonify({"error": "Job not completed"}), 400

        output_file = job.get("output_file")
        folder = job.get("folder")
        if not output_file or not folder:
            logging.warning(f"[encode_result] Missing data | ticket_id={ticket_id} output_file={output_file}")
            return jsonify({"error": "Job not completed"}), 400

    @after_this_request
    def cleanup(response):
        cleanup_folder = folder
        def delayed_cleanup():
            time.sleep(10)
            try:
                shutil.rmtree(cleanup_folder, ignore_errors=True)
                with JOBS_LOCK:
                    JOBS.pop(ticket_id, None)
                    #print(f"[CLEANUP] Removed {output_path}")
            except Exception as e:
                pass
                #print(f"[CLEANUP ERROR] {e}")

        threading.Thread(target=delayed_cleanup, daemon=True).start()
        return response

    return send_file(
        output_file,
        as_attachment=True,
        download_name=f"{ticket_id}.mp4"
    )


# -------------------------------
# Console Status Monitor
# -------------------------------
def clear_console():
    """Clear terminal and reposition cursor to line 1."""
    print('\033[H\033[J', end='')


ASCII_LOGO = """
░██         ░██                ░██   ░██                      ░██    ░███     ░███                   ░██ ░██           
░██                                  ░██                      ░██    ░████   ░████                   ░██               
░██         ░██░█████████████  ░██░████████  ░███████   ░████████    ░██░██ ░██░██  ░███████   ░████████ ░██ ░██████   
░██         ░██░██   ░██   ░██ ░██   ░██    ░██    ░██ ░██    ░██    ░██ ░████ ░██ ░██    ░██ ░██    ░██ ░██      ░██  
░██         ░██░██   ░██   ░██ ░██   ░██    ░█████████ ░██    ░██    ░██  ░██  ░██ ░█████████ ░██    ░██ ░██ ░███████  
░██         ░██░██   ░██   ░██ ░██   ░██    ░██        ░██   ░███    ░██       ░██ ░██        ░██   ░███ ░██░██   ░██  
░██████████ ░██░██   ░██   ░██ ░██    ░████  ░███████   ░█████░██    ░██       ░██  ░███████   ░█████░██ ░██ ░█████░██ 
                                                                                                                       
                                                                                                                       
                                                                                                                       
░██████████                                         ░██                                                                
░██                                                 ░██                                                                
░██         ░████████   ░███████   ░███████   ░████████  ░███████  ░██░████                                            
░█████████  ░██    ░██ ░██    ░██ ░██    ░██ ░██    ░██ ░██    ░██ ░███                                                
░██         ░██    ░██ ░██        ░██    ░██ ░██    ░██ ░█████████ ░██                                                 
░██         ░██    ░██ ░██    ░██ ░██    ░██ ░██   ░███ ░██        ░██                                                 
░██████████ ░██    ░██  ░███████   ░███████   ░█████░██  ░███████  ░██                                                 
"""

LEGAL = """
 Limited Media Encoder
 Licensed under MIT | github.com/mgatelabs/LimitedMediaEncoder"""


def print_banner():
    print(ASCII_LOGO + RESET)
    print('\033[90m' + LEGAL + '\033[0m')
    time.sleep(2)
    clear_console()


STATUS_COLORS = {
    'active': '\033[92m',
    'queued': '\033[93m',
    'done':   '\033[94m',
    'failed': '\033[91m',
}

RESET = '\033[0m'


def elapsed_str(start_time):
    mins, secs = divmod(int(time.time() - start_time), 60)
    return f'{mins:02d}:{secs:02d}'


SPINNER_FRAMES = ['⠋', '⠙', '⠸', '⠰', '⠴', '⠲']
_spinner_frame = 0

def draw_bar(pct, width=30, frame_off=0):
    filled = int(pct * width)
    if filled >= width:
        return '[' + '=' * filled + ']'
    fi = (_spinner_frame + frame_off) % len(SPINNER_FRAMES)
    return '[' + '=' * filled + SPINNER_FRAMES[fi] + ' ' * (width - filled - 1) + ']'


def status_monitor():
    while True:
        # Check for Escape key at the top of each iteration
        if msvcrt.kbhit():
            key = msvcrt.getch()
            if key == b'\x1b':  # Escape
                print('\n[INFO] Shutting down via Escape key...')
                os._exit(0)

        time.sleep(2.0)

        with JOBS_LOCK:
            active_jobs = [(tid, j) for tid, j in JOBS.items() if j['status'] == 'active']
            queued_jobs = [(tid, j) for tid, j in JOBS.items() if j['status'] == 'queued']
            done_jobs = [(tid, j) for tid, j in JOBS.items() if j['status'] == 'done']
            failed_jobs = [(tid, j) for tid, j in JOBS.items() if j['status'] == 'failed']

            n_active = len(active_jobs)
            n_queued = len(queued_jobs)
            n_done = len(done_jobs)
            n_failed = len(failed_jobs)

            job_list = []
            for tid, j in active_jobs + queued_jobs:
                status_color = STATUS_COLORS.get(j['status'], RESET)
                worker = j['worker_name'] or '-'
                short = j.get('job_id_short', tid[:8])
                progress = j.get('progress')
                
                if progress is None:
                    prog_str = '...'
                    bar = '─' * 16
                    elapsed = '    --  '
                else:
                    pct = min(progress * 100, 100)
                    prog_str = f'{pct:5.1f}%'
                    bar = draw_bar(progress)
                    t_start = j.get('start_time') or time.time()
                    elapsed = elapsed_str(t_start)

                job_list.append((status_color, short, worker, status_color.upper(), bar, prog_str, elapsed))

        clear_console()

        print(f'🎬 Server {time.strftime("%H:%M:%S")} | Active: {n_active}  Queued: {n_queued}  Done: {n_done}  Failed: {n_failed}')

        if job_list:
            for color, short, worker, status, bar, prog_str, elapsed in job_list:
                print(f'{status.lower():<6}[{bar}]  {short} ({worker:<10})  {prog_str:>7s}  {elapsed}')
        else:
            print('   (idle)')


# -------------------------------
# App Startup
# -------------------------------
def setup_logging(base_dir):
    log_file = base_dir / "server.log"
    log1_file = base_dir / "server1.log"

    # Rotate: erase old server1.log, rename current to server1.log
    if log1_file.exists():
        os.remove(log1_file)
    if log_file.exists():
        log_file.rename(log1_file)

    fmt = logging.Formatter('[%(asctime)s] %(levelname)s - %(name)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    handler_current = logging.FileHandler(log_file, mode='w')
    handler_current.setLevel(logging.WARNING)
    handler_current.setFormatter(fmt)
    logging.getLogger().setLevel(logging.WARNING)
    logging.getLogger().addHandler(handler_current)

def main(port: int = 8080, num_workers: int = 3):
    setup_logging(BASE_DIR)

    for i in range(num_workers):
        t = threading.Thread(target=job_worker, daemon=True, name=f"JobWorker-{i + 1}")
        t.start()

    print_banner()
    
    mon = threading.Thread(target=status_monitor, daemon=True, name='StatusMonitor')
    mon.start()
    logging.warning('[INFO] Status monitor started')

    logging.getLogger('werkzeug').setLevel(logging.WARNING)

    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Simple Flask-based encoding server")
    parser.add_argument("--port", type=int, default=8080, help="Port to run the server on")
    parser.add_argument("--workers", type=int, default=3, help="Number of background encoding threads")
    parser.add_argument("--test", action="store_true", help="Create a simulated test task for progress/status testing")
    args = parser.parse_args()
    
    if args.test:
        ticket_id = str(uuid.uuid4())
        job_folder = TEMP_DIR / ticket_id
        job_folder.mkdir(parents=True, exist_ok=True)
        with JOBS_LOCK:
            JOBS[ticket_id] = {
                "ticket_id": ticket_id,
                "task_type": "TEST",
                "status": "queued",
                "progress": None,
                "folder": job_folder,
                "output_file": None,
                "error": None,
                "worker_name": None,
                "job_id_short": ticket_id[:8],
                "start_time": time.time(),
            }
        JOB_QUEUE.put(ticket_id)
        print(f"[INFO] Test task created | ticket_id={ticket_id}")
    
    main(args.port, args.workers)
