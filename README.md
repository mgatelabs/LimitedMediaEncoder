# 🎬 Limited Media Encoder

**Limited Media Encoder** is a lightweight, Flask-based REST service that accepts media encoding requests (e.g., video transcoding via `ffmpeg`).  
It supports optional subtitle embedding, runs jobs in parallel worker threads, and provides status tracking and result retrieval.

---

## 🚀 Features

- Accepts video + optional subtitle (`.srt`) uploads.
- Processes jobs asynchronously using background workers (configurable concurrency).
- Poll job status via REST.
- Retrieve encoded results via download endpoint.
- Automatically cleans up temporary files after job completion.
- Falls back to local encoding (when used with the companion client).

---

## 🧩 Requirements

- **Python 3.8+**
- `ffmpeg` installed and available in system PATH  
- Python dependencies:
  ```bash
  pip install flask
