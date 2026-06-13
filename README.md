# 🎬 Limited Media Encoder

**Limited Media Encoder** is a lightweight, Flask-based REST service that manages distributed video encoding via `ffmpeg`.  

It allows multiple machines to share a central encoding server — submit videos over HTTP, poll for status updates, and download completed results.

---

## 🚀 Features

- Accepts video (`.mp4`, `.mkv`, etc.) and optional `.srt` subtitle uploads
- Asynchronous background processing with configurable worker threads
- Real-time progress polling via REST API
- Automatic scaling: files downloaded as HTTP attachments
- Temporary file cleanup after each job completes
- Configurable presets: resolution, codec, audio bitrate, channels

---

## 📁 Structure

```
server.py          # Main application and API endpoints
test_server.py     # End-to-end integration tests (includes test fixture)
requirements.txt   # Python dependencies
temp/              # Created at runtime: encoding job staging + output
```

---

## 🧩 Setup

### Prerequisites

- **Python 3.8+**
- `ffmpeg` and `ffprobe` installed and available in system PATH  
- Python dependencies:
  ```bash
  pip install flask requests pytest
  ```

### Running the Server

```bash
python server.py --port 8080 --workers 4
```

| Argument    | Default | Description                          |
|-------------|---------|--------------------------------------|
| `--port`    | `8080`  | Port to listen on                    |
| `--workers` | `3`     | Number of background encoding threads |

---

## 🌐 API Endpoints

### Start Encoding Job
```http
POST /encode/start
Content-Type: multipart/form-data
```

**Form fields:**
- `input_file`: Video file to encode (required)
- `srt_file`: Optional subtitle file  
- `options`: JSON string with encoding options:
  - `ffmpeg_preset`: `"ultrafast"` | `"fast"` | `"medium"` | `"slow"` | ...
  - `stereo`: `(true/false)` — mono audio output
  - `audio_bitrate`: Audio bitrate in kbps (default `128`)

**Response:**
```json
{
  "ticket_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "queued"
}
```

### Check Job Status
```http
GET /encode/status/{ticket_id}
```

**Responses:**
```json
{
  "ticket_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "done",        // queued | done | failed
  "progress_pure": 0.85,   // floating point progress
  "has_output_file": true,
  "output_path": "/path/to/output.mp4"
}
```

### Download Result
```http
GET /encode/result/{ticket_id}
```

- Downloads the encoded video as an attachment
- Trigger cleanup of temp files after download begins
- Returns `400` if job not done or not found

---

## 🧪 Running Tests

Tests spin up a real instance of the server with workers and exercise them end-to-end.

```bash
pytest test_server.py -v
```

**Test suite covers:**
| Test | What it checks |
|------|----------------|
| `test_start_and_complete_job` | Upload → encode → download → verify output stream with ffprobe |
| `test_upload_without_file_returns_400` | Missing input file validation |
| `test_invalid_ticket_status_404` | Status lookup for non-existent job |
| `test_invalid_ticket_result_400` | Result download for missing/unavailable job |

### Test Execution Flow

1. **Server starts** once per test session via `encode_server` fixture, randomly selecting a free port (8000–9999)
2. Each test sends requests to the live server using real HTTP calls
3. **Cleanup runs automatically**: worker threads finish any in-progress jobs, then the temp directory is wiped

### Test Requirements

- `ffmpeg` and `ffprobe` installed (tests verify output file validity via ffprobe)

---

## 🔧 Server Internals

The server runs a single process: `main()` initializes and starts all background worker threads (each encodes one video at a time from the shared job queue), then launches the Flask dev server that serves the REST API. All components live in the same address space, so workers update job state directly via thread-safe collections.

Each job creates its own `temp/{ticket_id}/` folder containing the input video and JSON configuration for playback
