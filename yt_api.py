from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import subprocess
import os
import re
import urllib.parse
import time
import glob
import json
import hashlib
import base64

app = FastAPI(title="YT-DLP API")

# Absolute path in container where downloads are mounted
downloads_path = "/app/downloads"
os.makedirs(downloads_path, exist_ok=True)

# Cache file (maps cache_key -> final_filename)
cache_file = os.path.join(downloads_path, ".cache.json")
if os.path.exists(cache_file):
    try:
        with open(cache_file, "r", encoding="utf-8") as f:
            url_cache = json.load(f)
    except Exception:
        url_cache = {}
else:
    url_cache = {}

# mount static so files served at /downloads/<filename>
app.mount("/downloads", StaticFiles(directory=downloads_path), name="downloads")


class DownloadRequest(BaseModel):
    url: str
    type: str = "audio"  # "audio" or "video"


def save_cache():
    try:
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(url_cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def sanitize_title(title: str, max_len: int = 120) -> str:
    """
    Make filename-safe title:
      - remove filesystem-unsafe chars (including |, /, ⧸)
      - replace groups of whitespace/punctuation with underscore
      - collapse repeated underscores
      - trim length
    """
    if not isinstance(title, str):
        title = str(title or "")
    # Replace unicode fraction slash and similar with underscore
    title = title.replace("\u29f8", "_").replace("\u2044", "_")
    # Replace common separators that break filenames
    # Keep alnum, dot, dash, underscore
    safe = re.sub(r"[<>:\"/\\|?*\n\r\t]", "_", title)
    # replace any sequence of chars not in \w\-\._ with underscore
    safe = re.sub(r"[^\w\-\._]+", "_", safe)
    # collapse underscores
    safe = re.sub(r"_+", "_", safe)
    safe = safe.strip("_.-")
    if not safe:
        safe = f"file_{int(time.time())}"
    if len(safe) > max_len:
        safe = safe[:max_len].rstrip("_.-")
    return safe


def run_cmd_stdout(cmd: list, timeout: int = 30) -> str:
    """Run a command and return stdout (stripped). May raise CalledProcessError."""
    res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
    return res.stdout.strip()


# Sites behind anti-bot challenges (TikTok in particular) intermittently reject
# the JS-challenge cookie, so a single extraction attempt fails a large fraction
# of the time even on the newest yt-dlp. The failures are transient: the very
# next attempt on the same URL usually succeeds. yt-dlp's own --retries only
# covers download/fragment HTTP errors, not extractor errors, so retry here.
EXTRACT_ATTEMPTS = 4
EXTRACT_BACKOFF = 2.0

TRANSIENT_EXTRACT_MARKERS = (
    "Unexpected response from webpage request",
    "Unable to extract universal data for rehydration",
    "Unable to extract webpage",
    "Unable to extract video data",
    "Unable to extract initial state",
    "HTTP Error 5",
)


def is_transient_extract_error(stderr: str) -> bool:
    return any(m in (stderr or "") for m in TRANSIENT_EXTRACT_MARKERS)


def run_cmd_retry(cmd: list, timeout: int = 30, attempts: int = EXTRACT_ATTEMPTS):
    """Run a yt-dlp command, retrying transient extractor failures."""
    last_exc = None
    for n in range(1, attempts + 1):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)
        except subprocess.CalledProcessError as e:
            last_exc = e
            if n == attempts or not is_transient_extract_error(e.stderr):
                raise
            print(f"Transient extractor error (attempt {n}/{attempts}), retrying: "
                  f"{(e.stderr or '').strip()[:160]}")
            time.sleep(EXTRACT_BACKOFF * n)
    raise last_exc


def extract_video_id(url: str) -> str:
    """
    Extract YouTube video ID from various URL formats.
    """
    patterns = [
        r"(?:v=|/)([0-9A-Za-z_-]{11})(?:\S+)?",
        r"youtu\.be/([0-9A-Za-z_-]{11})(?:\S+)?",
        r"/embed/([0-9A-Za-z_-]{11})(?:\S+)?",
        r"/v/([0-9A-Za-z_-]{11})(?:\S+)?"
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    # If no video ID found, create a hash of the URL as fallback
    return hashlib.md5(url.encode()).hexdigest()[:11]


def get_video_info(url: str):
    """
    Use yt-dlp to extract video id and title (without downloading).
    """
    # Fetch id and title in ONE extraction instead of two. Every extraction is a
    # fresh anti-bot challenge, so halving the number of round-trips roughly
    # halves the chance of the request failing before the download even starts.
    try:
        out = run_cmd_retry([
            "yt-dlp",
            "--no-check-certificates",
            "--no-warnings",
            "--no-playlist",
            "--print", "%(id)s",
            "--print", "%(title)s",
            url
        ], timeout=60).stdout.strip().splitlines()

        video_id = out[0].strip() if out else ""
        if not video_id:
            raise ValueError("empty id from yt-dlp")
        title = out[1].strip() if len(out) > 1 and out[1].strip() else f"video_{video_id}"
    except Exception as e:
        print(f"get_video_info failed, falling back to URL parsing: {e}")
        video_id = extract_video_id(url)
        title = f"video_{video_id}"

    return video_id, title


def find_existing_file(video_id: str, file_type: str = "audio"):
    """
    Find existing file for this video_id and type.
    Returns filename or None.
    """
    if file_type == "audio":
        extensions = ["mp3", "m4a", "wav", "flac", "ogg"]
    else:
        extensions = ["mp4", "webm", "mkv", "avi", "mov"]

    # Check cache first
    cache_key = f"{video_id}_{file_type}"
    if cache_key in url_cache:
        cached_file = url_cache[cache_key]
        if os.path.exists(os.path.join(downloads_path, cached_file)):
            return cached_file

    # Look for files with video_id in the name
    for ext in extensions:
        # Pattern 1: files ending with __VIDEO_ID.ext
        pattern = os.path.join(downloads_path, f"*__{video_id}.{ext}")
        matches = glob.glob(pattern)
        if matches:
            return os.path.basename(matches[0])

        # Pattern 2: files starting with VIDEO_ID
        pattern = os.path.join(downloads_path, f"{video_id}*.{ext}")
        matches = glob.glob(pattern)
        if matches:
            return os.path.basename(matches[0])

    return None


def create_lock(key: str):
    """Create a lock file for the given key."""
    lockfile = os.path.join(downloads_path, f".{key}.lock")
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return lockfile
    except FileExistsError:
        return None
    except Exception:
        return None


def remove_lock(lockfile: str):
    try:
        if os.path.exists(lockfile):
            os.unlink(lockfile)
    except Exception:
        pass


def download_and_get_file(url: str, file_type: str):
    """
    Common download logic used by both /get and /download-base64 endpoints.
    Returns tuple of (success: bool, result: str or dict, error_response: JSONResponse or None)
    """
    # Extract video info
    try:
        video_id, title = get_video_info(url)
    except Exception as e:
        return False, None, JSONResponse({"status": "error", "error": f"Failed to get video info: {str(e)}"}, status_code=400)

    cache_key = f"{video_id}_{file_type}"

    print(f"Processing request - Video ID: {video_id}, Type: {file_type}, Title: {title}")

    # 1) Check if file already exists
    existing_file = find_existing_file(video_id, file_type)
    if existing_file:
        print(f"Found existing file: {existing_file}")
        # Update cache
        url_cache[cache_key] = existing_file
        save_cache()
        return True, existing_file, None

    # 2) Try to acquire lock for this video_id and type
    lock = create_lock(cache_key)
    if lock is None:
        # Another process is downloading, wait for result
        print(f"Waiting for concurrent download of {cache_key}")
        for _ in range(180):  # Wait up to 3 minutes
            time.sleep(1)
            existing_file = find_existing_file(video_id, file_type)
            if existing_file:
                url_cache[cache_key] = existing_file
                save_cache()
                return True, existing_file, None

        return False, None, JSONResponse({"status": "error", "error": "Timeout waiting for concurrent download"}, status_code=500)

    # 3) We have the lock, proceed with download
    try:
        print(f"Starting download for {cache_key}")

        # Clean filename
        sanitized_title = sanitize_title(title)

        # Determine output format and extension
        if file_type == "audio":
            extension = "mp3"
            format_args = [
                "-x",
                "--audio-format", "mp3",
                "--audio-quality", "192K",  # Optimized: 192K provides near-transparent quality with faster encoding
                "--postprocessor-args", "ffmpeg:-threads 2"  # Enable multi-threading for faster conversion
            ]
        else:
            extension = "mp4"
            # Optimized format selection: prefer native MP4 to avoid re-encoding
            # This saves 3-5 seconds by downloading MP4 directly instead of transcoding
            format_args = [
                "-f", "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best[height<=720]/best",
                "--merge-output-format", "mp4",
                # Removed --recode-video to avoid unnecessary re-encoding
            ]

        # Create output template with video_id to ensure uniqueness
        output_template = os.path.join(downloads_path, f"{sanitized_title}__{video_id}.%(ext)s")

        # Build and run yt-dlp command with performance optimizations
        cmd = [
            "yt-dlp",
            "--no-check-certificates",
            "--no-playlist",
            "--no-warnings",
            "--prefer-insecure",
            # NOTE: do not force a User-Agent here. Extractors that impersonate a
            # browser (TikTok) set their own consistent headers via curl_cffi; an
            # overridden UA contradicts the TLS fingerprint and gets flagged as a bot.
            # Performance optimizations (Phase 1 & 2)
            "--concurrent-fragments", "4",  # Download multiple fragments in parallel (saves 2-4 seconds)
            "--buffer-size", "16K",  # Optimize I/O buffer size
            # Reliability improvements (Phase 2)
            "--retries", "3",  # Retry failed downloads up to 3 times
            "--fragment-retries", "3",  # Retry failed fragments up to 3 times
            "--continue",  # Resume partial downloads instead of restarting
            "--no-part",  # Don't use .part files (cleaner, slightly faster)
        ] + format_args + [
            "-o", output_template,
            url
        ]

        print(f"Running command: {' '.join(cmd)}")

        result = run_cmd_retry(cmd, timeout=300)
        print(f"Download completed successfully")
        if result.stderr:
            print(f"yt-dlp stderr: {result.stderr}")

        # Find the downloaded file
        downloaded_file = find_existing_file(video_id, file_type)
        if not downloaded_file:
            return False, None, JSONResponse({"status": "error", "error": "Download completed but file not found"}, status_code=500)

        # Rename to cleaner filename if needed
        current_path = os.path.join(downloads_path, downloaded_file)
        clean_filename = f"{sanitized_title}.{extension}"
        clean_path = os.path.join(downloads_path, clean_filename)

        # Only rename if the clean filename doesn't already exist
        if downloaded_file != clean_filename and not os.path.exists(clean_path):
            try:
                os.rename(current_path, clean_path)
                downloaded_file = clean_filename
                print(f"Renamed to: {clean_filename}")
            except Exception as e:
                print(f"Could not rename file: {e}")

        # Set proper permissions
        try:
            os.chmod(os.path.join(downloads_path, downloaded_file), 0o644)
        except Exception:
            pass

        # Update cache
        url_cache[cache_key] = downloaded_file
        save_cache()

        return True, downloaded_file, None

    except subprocess.CalledProcessError as e:
        error_msg = e.stderr.strip() if e.stderr else str(e)
        print(f"Download failed: {error_msg}")
        return False, None, JSONResponse({"status": "error", "error": error_msg}, status_code=500)
    except subprocess.TimeoutExpired:
        print(f"Download timeout for {cache_key}")
        return False, None, JSONResponse({"status": "error", "error": "Download timeout - video may be too long or connection too slow"}, status_code=500)
    except Exception as e:
        print(f"Unexpected error: {str(e)}")
        return False, None, JSONResponse({"status": "error", "error": f"Unexpected error: {str(e)}"}, status_code=500)
    finally:
        # Always remove lock
        if lock:
            remove_lock(lock)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def get_version():
    """Get yt-dlp version"""
    try:
        version = run_cmd_stdout(["yt-dlp", "--version"], timeout=5)
        return {"version": version}
    except Exception as e:
        return {"error": str(e)}


@app.post("/download")
def download_media(req: DownloadRequest):
    """
    Download media and return public URL. Reuse existing file for same video ID and type.
    """
    url = req.url.strip()
    file_type = (req.type or "audio").lower()

    if file_type not in ("audio", "video"):
        file_type = "audio"

    # Use common download logic
    success, result, error = download_and_get_file(url, file_type)
    
    if not success:
        return error
    
    # result is the filename
    encoded = urllib.parse.quote(result, safe="")
    return {"status": "success", "type": file_type, "file": f"https://yt-dlp.fiverse.my/dl/{encoded}", "cached": True}


@app.post("/download-base64")
def download_media_base64(req: DownloadRequest):
    """Download media and return base64-encoded file data with Data URI format"""
    url = req.url.strip()
    file_type = (req.type or "audio").lower()

    if file_type not in ("audio", "video"):
        file_type = "audio"

    # Use common download logic
    success, result, error = download_and_get_file(url, file_type)
    
    if not success:
        return error
    
    # result is the filename
    file_path = os.path.join(downloads_path, result)
    
    try:
        # Read file and encode to base64
        with open(file_path, "rb") as f:
            file_data = base64.b64encode(f.read()).decode('utf-8')
        
        # Determine mimetype based on file extension
        extension = result.split('.')[-1].lower()
        
        if file_type == "audio":
            mimetype_map = {
                "mp3": "audio/mpeg",
                "m4a": "audio/mp4",
                "wav": "audio/wav",
                "flac": "audio/flac",
                "ogg": "audio/ogg"
            }
            mimetype = mimetype_map.get(extension, "audio/mpeg")
        else:
            mimetype_map = {
                "mp4": "video/mp4",
                "webm": "video/webm",
                "mkv": "video/x-matroska",
                "avi": "video/x-msvideo",
                "mov": "video/quicktime"
            }
            mimetype = mimetype_map.get(extension, "video/mp4")
        
        # Get file size
        file_size = os.path.getsize(file_path)
        
        print(f"Returning base64 data - File: {result}, Size: {file_size} bytes, Mimetype: {mimetype}")
        
        return {
            "status": "success",
            "type": file_type,
            "filename": result,
            "mimetype": mimetype,
            "data": f"data:{mimetype};base64,{file_data}",
            "size": file_size
        }
    
    except Exception as e:
        print(f"Error encoding file to base64: {str(e)}")
        return JSONResponse({"status": "error", "error": f"Failed to encode file: {str(e)}"}, status_code=500)


@app.post("/get")
def get_media(req: DownloadRequest):
    """Download media and return the binary file directly"""
    url = req.url.strip()
    file_type = (req.type or "audio").lower()

    if file_type not in ("audio", "video"):
        file_type = "audio"

    # Use common download logic
    success, result, error = download_and_get_file(url, file_type)
    
    if not success:
        return error
    
    # result is the filename, return as FileResponse
    file_path = os.path.join(downloads_path, result)
    return FileResponse(file_path, media_type="application/octet-stream", filename=result)


@app.get("/dl/{filename}")
def force_download(filename: str):
    """Serve file as force-download"""
    from urllib.parse import unquote
    safe_name = unquote(filename)
    file_path = os.path.join(downloads_path, safe_name)
    if not os.path.exists(file_path):
        return JSONResponse({"status": "error", "message": "File not found"}, status_code=404)
    return FileResponse(file_path, media_type="application/octet-stream", filename=safe_name)


@app.get("/cache")
def show_cache():
    """Debug endpoint to show current cache contents"""
    return {"cache": url_cache, "files": os.listdir(downloads_path)}


@app.delete("/cache")
def clear_cache():
    """Clear the cache"""
    global url_cache
    url_cache = {}
    save_cache()
    return {"status": "cache cleared"}