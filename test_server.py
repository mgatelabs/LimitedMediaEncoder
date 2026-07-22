import json
import socket
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager

import pytest


def _find_free_port():
    """Find the first available TCP port between 8000 and 9999."""
    for port in range(8000, 9999):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError("No free port found between 8000 and 9999")


def _is_server_ready(port, retries=60, delay=1.0):
    """Check if the server is accepting connections on the given port."""
    for _ in range(retries):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1)
                sock.connect(("127.0.0.1", port))
                return True
        except (OSError, ConnectionRefusedError):
            time.sleep(delay)
    return False


@pytest.fixture(scope="session")
def encode_server():
    """Run the Flask encoding server in a background thread for the entire test session."""
    import server as srv

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    def run_server():
        srv.main(port=port, num_workers=3, quiet=True, verbose=True)

    server_thread = threading.Thread(target=run_server, daemon=True, name="EncodeServer")
    server_thread.start()

    if not _is_server_ready(port):
        pytest.fail("Encoding server failed to start within 60 seconds")

    yield base_url

    # Give workers a moment to finish any in-flight jobs
    time.sleep(2)

    # Clean up temp directory left by server process to avoid file buildup
    import shutil
    try:
        if srv.TEMP_DIR.exists():
            shutil.rmtree(srv.TEMP_DIR)
    except Exception:
        pass  # Workers or OS may have already cleaned up some files


def _poll_status(base_url, ticket_id, timeout=120, interval=0.5):
    """Poll the encoding status endpoint until the job completes or times out."""
    import requests

    start = time.time()
    while time.time() - start < timeout:
        response = requests.get(f"{base_url}/status/{ticket_id}")
        assert response.status_code == 200, f"Status request failed: {response.text}"
        data = response.json()

        if data.get("status") in ("done", "failed"):
            return data

        time.sleep(interval)

    raise TimeoutError(f"Job {ticket_id} did not complete within {timeout} seconds")


# --- Test Functions ---


def test_start_and_complete_job(encode_server):
    """End-to-end: upload a video, encode it, download the result."""
    import requests

    base_url = encode_server

    # Step 1: Upload for encoding
    with open("sample.mp4", "rb") as f:
        files = {"input_file": ("sample.mp4", f, "video/mp4")}
        data = {"options": json.dumps({"ffmpeg_preset": "ultrafast"})}

        response = requests.post(f"{base_url}/start/encode", files=files, data=data)

    assert response.status_code == 200
    ticket_id = response.json()["ticket_id"]
    assert response.json()["status"] == "queued"

    # Step 2: Wait for the job to complete
    status_data = _poll_status(base_url, ticket_id)
    assert status_data["status"] == "done", f"Encoding failed: {status_data.get('error')}"
    assert float(status_data["progress"]) == 100.0

    # Step 3: Download the encoded file
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        dl_response = requests.get(f"{base_url}/result/{ticket_id}")
        assert dl_response.status_code == 200
        assert "attachment" in dl_response.headers.get("Content-Disposition", "")
        tmp.write(dl_response.content)
        downloaded_path = tmp.name

    # Step 4: Verify the downloaded file is a valid video with streams
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", str(downloaded_path)
        ], capture_output=True, text=True
    )

    assert result.returncode == 0, f"ffprobe failed: {result.stderr}"
    probe_data = json.loads(result.stdout)

    streams = probe_data.get("streams", [])
    assert len(streams) >= 1, "Output file has no streams"

    video_streams = [s for s in streams if s["codec_type"] == "video"]
    assert len(video_streams) >= 1, f"No video stream found in output.\n{probe_data}"


def test_upload_without_file_returns_400(encode_server):
    """Uploading without an input file should return 400."""
    import requests

    base_url = encode_server

    response = requests.post(f"{base_url}/start/encode")
    assert response.status_code == 400

    body = response.json()
    assert "error" in body
    assert "input_file" in body["error"] or "Missing" in body["error"]


def test_invalid_ticket_status_404(encode_server):
    """Checking status of a non-existent ticket should return 404."""
    import requests

    base_url = encode_server

    response = requests.get(f"{base_url}/status/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 404
    assert "error" in response.json()


def test_invalid_ticket_result_400(encode_server):
    """Requesting result of a non-existent/invalid ticket should return 400."""
    import requests

    base_url = encode_server

    response = requests.get(f"{base_url}/result/00000000-0000-0000-0000-000000000000")
    assert response.status_code == 400
    assert "error" in response.json()


def test_start_and_complete_defreeze_job(encode_server):
    """End-to-end: upload a video, defreeze it, download the result."""
    import requests

    base_url = encode_server

    # Step 1: Upload for defreezing
    with open("sample_2.mp4", "rb") as f:
        files = {"input_file": ("sample_2.mp4", f, "video/mp4")}
        data = {"options": json.dumps({"force_encode": True})}

        response = requests.post(f"{base_url}/start/defreeze", files=files, data=data)

    assert response.status_code == 200
    ticket_id = response.json()["ticket_id"]
    assert response.json()["status"] == "queued"

    # Step 2: Wait for the job to complete
    status_data = _poll_status(base_url, ticket_id)
    assert status_data["status"] == "done", f"Defreeze failed: {status_data.get('error')}"
    assert float(status_data["progress"]) == 100.0

    # Step 3: Download the processed file
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        dl_response = requests.get(f"{base_url}/result/{ticket_id}")
        assert dl_response.status_code == 200
        assert "attachment" in dl_response.headers.get("Content-Disposition", "")
        tmp.write(dl_response.content)
        downloaded_path = tmp.name

    # Step 4: Verify the downloaded file is a valid video with streams
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", str(downloaded_path)
        ], capture_output=True, text=True
    )

    assert result.returncode == 0, f"ffprobe failed: {result.stderr}"
    probe_data = json.loads(result.stdout)

    streams = probe_data.get("streams", [])
    assert len(streams) >= 1, "Output file has no streams"

    video_streams = [s for s in streams if s["codec_type"] == "video"]
    assert len(video_streams) >= 1, f"No video stream found in output.\n{probe_data}"

