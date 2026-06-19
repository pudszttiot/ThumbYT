import os
import uuid
import random
import threading
import zipfile
import tempfile
import subprocess
import time
import shutil
import logging
from pathlib import Path
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import (
    Flask, render_template, request, jsonify,
    send_file, send_from_directory, url_for
)
from werkzeug.utils import secure_filename
import yt_dlp

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB

ALLOWED_EXTENSIONS = {'mp4', 'mov', 'avi', 'mkv', 'webm', 'flv', 'wmv'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

jobs = {}
jobs_lock = threading.Lock()
BASE_TEMP_DIR = tempfile.mkdtemp(prefix="video_thumbnails_")

# ----------------------------------------------------------------------
# yt-dlp options (low resolution)
# ----------------------------------------------------------------------
def get_ydl_opts(output_template, progress_hook=None):
    opts = {
        'format': 'worst[ext=mp4]/worst',
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'extract_flat': False,
        'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'referer': 'https://www.youtube.com/',
        'geo_bypass': True,
    }
    if progress_hook:
        opts['progress_hooks'] = [progress_hook]
    return opts

def get_video_duration(video_path):
    """Return duration in seconds using ffprobe."""
    cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
           '-of', 'default=noprint_wrappers=1:nokey=1', str(video_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except Exception as e:
        logger.error(f"ffprobe failed: {e}")
        return None

# ----------------------------------------------------------------------
# Cleanup thread
# ----------------------------------------------------------------------
def cleanup_old_jobs(max_age_seconds=3600):
    while True:
        time.sleep(1800)
        now = time.time()
        with jobs_lock:
            for job_id, job in list(jobs.items()):
                created = job.get('created_at', 0)
                if now - created > max_age_seconds:
                    dir_path = Path(job.get('dir', ''))
                    if dir_path.exists():
                        shutil.rmtree(dir_path, ignore_errors=True)
                    del jobs[job_id]
                    logger.info(f"Cleaned up old job {job_id}")

cleaner_thread = threading.Thread(target=cleanup_old_jobs, daemon=True)
cleaner_thread.start()

# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/process', methods=['POST'])
def process():
    # File upload branch
    if 'video_file' in request.files:
        file = request.files['video_file']
        if file.filename == '':
            return jsonify({"error": "No file selected"}), 400
        if not allowed_file(file.filename):
            return jsonify({"error": f"File type not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"}), 400

        num_thumbnails = request.form.get('num_thumbnails', '10')
        start_time = request.form.get('start_time', '')
        end_time = request.form.get('end_time', '')

        try:
            num_thumbnails = int(num_thumbnails)
            if not (1 <= num_thumbnails <= 20):
                raise ValueError
        except ValueError:
            return jsonify({"error": "Number of thumbnails must be between 1 and 20"}), 400

        try:
            start_sec = parse_time(start_time) if start_time else 0
            end_sec = parse_time(end_time) if end_time else None
            if start_sec < 0 or (end_sec is not None and end_sec <= start_sec):
                return jsonify({"error": "Invalid time range"}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        job_id = str(uuid.uuid4())
        job_dir = Path(BASE_TEMP_DIR) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # Save uploaded file
        original_filename = secure_filename(file.filename)
        video_path = job_dir / original_filename
        file.save(video_path)
        logger.info(f"Saved uploaded file to {video_path}")

        # Get duration
        duration = get_video_duration(str(video_path))
        if duration is None:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({"error": "Could not read video duration (ffprobe missing or corrupt file)"}), 400

        if start_sec >= duration:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({"error": "Start time exceeds video duration"}), 400
        if end_sec is not None:
            if end_sec > duration:
                end_sec = duration
        else:
            end_sec = duration

        # Create job entry
        with jobs_lock:
            jobs[job_id] = {
                "status": "starting",
                "progress": 0,
                "message": "Initializing...",
                "dir": str(job_dir),
                "thumbnails": [],
                "error": None,
                "url": file.filename,
                "num_thumbnails": num_thumbnails,
                "start_time_str": start_time,
                "end_time_str": end_time,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "created_at": time.time(),
                "is_upload": True,
                "video_path": str(video_path)
            }
        logger.info(f"Created job {job_id} for uploaded file")

        # Start processing thread
        thread = threading.Thread(
            target=process_uploaded_video,
            args=(job_id, video_path, job_dir, num_thumbnails, start_sec, end_sec)
        )
        thread.daemon = True
        thread.start()

        return jsonify({"job_id": job_id})

    else:
        # YouTube URL branch (keep your existing logic, but ensure job creation similar)
        # ... (I'll include it for completeness, but it's unchanged from previous version)
        url = request.form.get('url', '').strip()
        if not url:
            return jsonify({"error": "No URL provided"}), 400
        if not (url.startswith('https://www.youtube.com/') or
                url.startswith('https://youtu.be/') or
                url.startswith('https://m.youtube.com/')):
            return jsonify({"error": "Invalid YouTube URL"}), 400

        try:
            num_thumbnails = int(request.form.get('num_thumbnails', '10'))
            if not (1 <= num_thumbnails <= 20):
                raise ValueError
        except ValueError:
            return jsonify({"error": "Number of thumbnails must be between 1 and 20"}), 400

        start_time = request.form.get('start_time', '')
        end_time = request.form.get('end_time', '')
        try:
            start_sec = parse_time(start_time) if start_time else 0
            end_sec = parse_time(end_time) if end_time else None
            if start_sec < 0 or (end_sec is not None and end_sec <= start_sec):
                return jsonify({"error": "Invalid time range"}), 400
        except ValueError as e:
            return jsonify({"error": str(e)}), 400

        job_id = str(uuid.uuid4())
        job_dir = Path(BASE_TEMP_DIR) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        with jobs_lock:
            jobs[job_id] = {
                "status": "starting",
                "progress": 0,
                "message": "Initializing...",
                "dir": str(job_dir),
                "thumbnails": [],
                "error": None,
                "url": url,
                "num_thumbnails": num_thumbnails,
                "start_time_str": start_time,
                "end_time_str": end_time,
                "start_sec": start_sec,
                "end_sec": end_sec,
                "created_at": time.time(),
                "is_upload": False
            }
        logger.info(f"Created job {job_id} for YouTube URL")

        thread = threading.Thread(
            target=process_youtube_video,
            args=(job_id, url, job_dir, num_thumbnails, start_sec, end_sec)
        )
        thread.daemon = True
        thread.start()

        return jsonify({"job_id": job_id})

@app.route('/progress/<job_id>')
def progress(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            logger.warning(f"Progress requested for unknown job {job_id}")
            return jsonify({"error": "Invalid job ID"}), 404
        return jsonify({
            "status": job["status"],
            "progress": job["progress"],
            "message": job["message"],
            "thumbnails": job["thumbnails"],
            "error": job["error"]
        })

@app.route('/results/<job_id>')
def results(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return "Job not found or not complete", 404
    return render_template(
        'results.html',
        job_id=job_id,
        thumbnails=job["thumbnails"],
        video_url=job.get("url", ""),
        video_source_type="upload" if job.get("is_upload") else "youtube",
        num_thumbnails=job.get("num_thumbnails", 10),
        start_time_str=job.get("start_time_str", ""),
        end_time_str=job.get("end_time_str", "")
    )

@app.route('/thumbnail/<job_id>/<filename>')
def thumbnail(job_id, filename):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return "Invalid job", 404
    return send_from_directory(job["dir"], filename)

@app.route('/download/<job_id>')
def download_zip(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return "Job not ready", 404
    dir_path = Path(job["dir"])
    memory_file = BytesIO()
    with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fname in job["thumbnails"]:
            file_path = dir_path / fname
            zf.write(file_path, fname)
    memory_file.seek(0)
    return send_file(
        memory_file,
        mimetype='application/zip',
        as_attachment=True,
        download_name=f'thumbnails_{job_id}.zip'
    )

def parse_time(time_str):
    parts = time_str.strip().split(':')
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    elif len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    else:
        raise ValueError("Invalid time format. Use MM:SS or HH:MM:SS")

# ----------------------------------------------------------------------
# Processing functions
# ----------------------------------------------------------------------
def process_youtube_video(job_id, url, job_dir, num_thumbnails, start_sec, end_sec):
    try:
        update_job(job_id, status="processing", progress=0, message="Downloading video info...")
        video_path = job_dir / "video.mp4"

        def progress_hook(d):
            if d['status'] == 'downloading':
                pct = d.get('_percent_str', '0%').replace('%', '')
                try:
                    percent = float(pct)
                except:
                    percent = 0.0
                progress = min(50, percent * 0.5)
                update_job(job_id, progress=int(progress), message=f"Downloading... {pct}%")
            elif d['status'] == 'finished':
                update_job(job_id, progress=50, message="Download complete. Extracting frames...")

        ydl_opts = get_ydl_opts(str(video_path), progress_hook)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            duration = info.get('duration')
            if not duration:
                raise Exception("Could not determine video duration")
            if start_sec >= duration:
                raise Exception("Start time beyond video duration")
            if end_sec is None or end_sec > duration:
                end_sec = duration
            ydl.download([url])

        timestamps = sorted([random.uniform(start_sec, end_sec) for _ in range(num_thumbnails)])
        thumbnails = extract_frames_parallel(job_id, video_path, job_dir, timestamps, num_thumbnails, start_progress=50)
        video_path.unlink()
        update_job(job_id, status="done", progress=100, message="Complete!", thumbnails=thumbnails)
    except Exception as e:
        logger.exception(f"YouTube job {job_id} failed")
        update_job(job_id, status="error", error=str(e))
        shutil.rmtree(job_dir, ignore_errors=True)

def process_uploaded_video(job_id, video_path, job_dir, num_thumbnails, start_sec, end_sec):
    try:
        update_job(job_id, status="processing", progress=10, message="Extracting frames...")
        timestamps = sorted([random.uniform(start_sec, end_sec) for _ in range(num_thumbnails)])
        thumbnails = extract_frames_parallel(job_id, video_path, job_dir, timestamps, num_thumbnails, start_progress=10)
        update_job(job_id, status="done", progress=100, message="Complete!", thumbnails=thumbnails)
    except Exception as e:
        logger.exception(f"Upload job {job_id} failed")
        update_job(job_id, status="error", error=str(e))
        shutil.rmtree(job_dir, ignore_errors=True)

def extract_frames_parallel(job_id, video_path, job_dir, timestamps, num_thumbnails, start_progress=50):
    thumbnails = []
    with ThreadPoolExecutor(max_workers=min(num_thumbnails, 4)) as executor:
        futures = {}
        for i, ts in enumerate(timestamps):
            out_file = job_dir / f"thumb_{i+1:02d}.png"
            futures[executor.submit(extract_frame, str(video_path), ts, str(out_file))] = i

        for future in as_completed(futures):
            idx = futures[future]
            try:
                future.result()
                thumbnails.append(f"thumb_{idx+1:02d}.png")
                prog = start_progress + int((idx + 1) / num_thumbnails * (100 - start_progress))
                update_job(job_id, progress=prog, message=f"Extracted frame {idx+1}/{num_thumbnails}")
            except Exception as e:
                raise Exception(f"Frame {idx+1} failed: {e}")
    thumbnails.sort()
    return thumbnails

def extract_frame(video_path, timestamp, output_path):
    cmd = ['ffmpeg', '-y', '-ss', str(timestamp), '-i', video_path, '-vframes', '1', '-q:v', '2', output_path]
    subprocess.run(cmd, check=True, capture_output=True)

def update_job(job_id, **kwargs):
    with jobs_lock:
        job = jobs.get(job_id)
        if job:
            job.update(kwargs)
        else:
            logger.error(f"Attempted to update non-existent job {job_id}")

# ----------------------------------------------------------------------
if __name__ == '__main__':
    try:
        subprocess.run(['ffmpeg', '-version'], capture_output=True, check=True)
        subprocess.run(['ffprobe', '-version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("ERROR: ffmpeg/ffprobe not found. Please install ffmpeg.")
        exit(1)
    app.run(debug=True, threaded=True, host='0.0.0.0', port=5000)