# app.py
from flask import Flask, request, send_file, jsonify, render_template
import yt_dlp
import os
import tempfile
import uuid
import re
import glob
import threading
import time
from queue import Queue

app = Flask(__name__)

# -------------------- ตั้งค่า cookie file --------------------
# แก้ path ให้ตรงกับที่คุณเซฟ youtube_cookies.txt
YOUTUBE_COOKIE_FILE = r"C:\Users\Manager\youtube_cookies.txt"

# -------------------- โครงสร้างเก็บงานในคิว --------------------

job_queue = Queue()          # คิวงาน (FIFO)
jobs = {}                    # job_id -> ข้อมูลงาน
jobs_lock = threading.Lock() # lock กันเขียนพร้อมกัน

# regex ล้างโค้ดสี ANSI ออกจากข้อความ error (เช่น [0;31m)
ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def clean_ansi(s: str) -> str:
    """ลบโค้ดสี ANSI ออกจากข้อความ"""
    return ANSI_ESCAPE.sub("", s)


def sanitize_filename(name: str) -> str:
    # ลบตัวอักษรที่ใช้ตั้งชื่อไฟล์ไม่ได้บน Windows เช่น \ / : * ? " < > |
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = name.strip()
    if not name:
        name = "video_download"
    return name


def find_downloaded_file(temp_dir: str, unique_id: str):
    """
    หาไฟล์ที่ yt-dlp โหลดมาจริง ๆ จาก temp_dir โดยดูจากชื่อที่ขึ้นต้นด้วย unique_id
    เช่น C:\\Temp\\<uuid>.mp4 หรือ .mp3
    """
    pattern = os.path.join(temp_dir, f"{unique_id}.*")
    matches = glob.glob(pattern)
    if matches:
        return matches[0]
    return None


def build_video_format_selector(quality: str) -> str:
    """
    สร้าง format string สำหรับ yt-dlp ตามความคมชัดที่เลือก
    - ถ้า <= 1080p จะพยายามใช้ progressive stream (ไฟล์เดียวจบ เร็วกว่า)
    - ถ้า > 1080p (2K/4K) ยังใช้แบบ video+audio แยกแล้ว merge
    """
    quality = (quality or "720p").lower().strip()

    quality_map = {
        "best": None,
        "2160p": 2160,
        "4k": 2160,
        "1440p": 1440,
        "2k": 1440,
        "1080p": 1080,
        "720p": 720,
        "480p": 480,
        "360p": 360,
    }

    h = quality_map.get(quality)

    if h is None:
        # best: คุณภาพดีที่สุด
        return "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    if h <= 1080:
        # ใช้ progressive stream เป็นหลัก
        return (
            f"best[ext=mp4][height<={h}]/"
            f"best[height<={h}]"
        )

    # 2K / 4K
    return (
        f"bestvideo[ext=mp4][height<={h}]+bestaudio[ext=m4a]/"
        f"bestvideo[height<={h}]+bestaudio/"
        f"best[height<={h}]"
    )

# -------------------- worker สำหรับประมวลผลคิว --------------------


def download_worker():
    print("[worker] เริ่มทำงาน worker ดาวน์โหลดแล้ว")
    while True:
        job_id = job_queue.get()   # ดึง job_id ออกจากคิว (บล็อกรอจนกว่าจะมีงาน)
        print(f"[worker] ดึงงานจากคิว: {job_id}")

        with jobs_lock:
            job = jobs.get(job_id)

        if not job:
            print(f"[worker] ไม่พบ job_id {job_id} ใน jobs dict")
            job_queue.task_done()
            continue

        # อัปเดตสถานะเป็นกำลังดาวน์โหลด
        with jobs_lock:
            job["status"] = "downloading"
            job["progress"] = "เริ่มดาวน์โหลด..."

        temp_dir = tempfile.gettempdir()
        unique_id = job["unique_id"]
        outtmpl = os.path.join(temp_dir, f"{unique_id}.%(ext)s")

        try:
            # เตรียม yt-dlp options
            common_opts = {
                "outtmpl": outtmpl,
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "retries": 10,
                "fragment_retries": 10,
                "skip_unavailable_fragments": True,
            }

            # ถ้ามี cookiefile ให้ใช้
            if os.path.exists(YOUTUBE_COOKIE_FILE):
                common_opts["cookiefile"] = YOUTUBE_COOKIE_FILE
            else:
                print(f"[worker] WARNING: ไม่พบ cookiefile ที่ {YOUTUBE_COOKIE_FILE} (จะลองแบบไม่ใช้คุกกี้)")

            if job["format"] == "mp4":
                video_format = build_video_format_selector(job["quality"])
                ydl_opts = {
                    **common_opts,
                    "format": video_format,
                    "merge_output_format": "mp4",
                    "concurrent_fragment_downloads": 4,
                }
                mimetype = "video/mp4"
            else:  # mp3
                ydl_opts = {
                    **common_opts,
                    "format": "bestaudio/best",
                    "postprocessors": [{
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }],
                }
                mimetype = "audio/mpeg"

            # hook สำหรับอัปเดต progress
            def progress_hook(d):
                if d.get("status") == "downloading":
                    percent = d.get("_percent_str", "").strip()
                    speed = d.get("_speed_str", "").strip()
                    eta = d.get("_eta_str", "").strip()
                    text = f"กำลังดาวน์โหลด... {percent} | ความเร็ว {speed} | เหลือเวลา {eta}"
                    with jobs_lock:
                        job["progress"] = text
                elif d.get("status") == "finished":
                    with jobs_lock:
                        job["progress"] = "ดาวน์โหลดเสร็จ กำลังเตรียมไฟล์..."

            ydl_opts["progress_hooks"] = [progress_hook]

            print(f"[worker] เริ่มดาวน์โหลด: {job['url']} | รูปแบบ: {job['format']} | คุณภาพ: {job['quality']}")
            # เริ่มดาวน์โหลด
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(job["url"], download=True)
                raw_title = info.get("title", "downloaded_file")
                safe_title = sanitize_filename(raw_title)

            # หาไฟล์ที่โหลดจริง
            final_path = find_downloaded_file(temp_dir, unique_id)
            if not final_path or not os.path.exists(final_path):
                raise RuntimeError("ไม่พบไฟล์ที่ดาวน์โหลดจาก yt-dlp")

            _, ext = os.path.splitext(final_path)
            ext = ext.lstrip(".") or ("mp3" if job["format"] == "mp3" else "mp4")
            download_name = f"{safe_title}.{ext}"

            # อัปเดตสถานะ job
            with jobs_lock:
                job["status"] = "done"
                job["filepath"] = final_path
                job["download_name"] = download_name
                job["mimetype"] = mimetype
                job["progress"] = "พร้อมดาวน์โหลดแล้ว"

            print(f"[worker] งานเสร็จ: {job_id} -> {download_name}")

        except Exception as e:
            msg = clean_ansi(str(e))
            print(f"[worker] ERROR งาน {job_id}: {msg}")

            # แปล error บางแบบให้เข้าใจง่าย
            if "Sign in to confirm you’re not a bot" in msg or "Sign in to confirm you're not a bot" in msg:
                human_msg = (
                    "YouTube ต้องการให้ยืนยันว่าไม่ใช่บอท/ล็อกอินสำหรับคลิปนี้\n"
                    "- ตรวจสอบว่า cookiefile ถูกต้องและยังไม่หมดอายุ\n"
                    "- ถ้ายังไม่ได้ทำ ให้ทำไฟล์ youtube_cookies.txt ใหม่ตามขั้นตอนที่ตั้งค่าไว้\n"
                )
            elif "cookiefile" in msg and "No such file or directory" in msg:
                human_msg = (
                    "ไม่พบไฟล์คุกกี้ที่ตั้งค่าไว้ใน app.py\n"
                    "ตรวจสอบ path ของ YOUTUBE_COOKIE_FILE ให้ตรงกับไฟล์ youtube_cookies.txt\n"
                )
            else:
                human_msg = f"เกิดข้อผิดพลาด: {msg}"

            with jobs_lock:
                job["status"] = "error"
                job["error"] = msg
                job["progress"] = human_msg

        job_queue.task_done()


# สตาร์ท worker 1 ตัว (ดาวน์โหลดทีละ 1 งาน)
worker_thread = threading.Thread(target=download_worker, daemon=True)
worker_thread.start()

# -------------------- Flask routes --------------------


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/enqueue", methods=["POST"])
def enqueue():
    data = request.get_json() or {}
    video_url = data.get("url", "").strip()
    file_format = data.get("format", "mp4")
    quality = data.get("quality", "720p")

    if not video_url:
        return jsonify({"error": "ไม่พบ URL วิดีโอ"}), 400

    if file_format not in ("mp4", "mp3"):
        return jsonify({"error": "รูปแบบไฟล์ไม่ถูกต้อง (รองรับเฉพาะ mp4/mp3)"}), 400

    job_id = str(uuid.uuid4())
    unique_id = str(uuid.uuid4())
    created_at = time.time()

    job = {
        "id": job_id,
        "url": video_url,
        "format": file_format,
        "quality": quality,
        "status": "queued",
        "progress": "รอคิว...",
        "filepath": None,
        "download_name": None,
        "mimetype": None,
        "error": None,
        "unique_id": unique_id,
        "created_at": created_at,
    }

    with jobs_lock:
        jobs[job_id] = job
        # คำนวณลำดับคิว (แค่ประมาณ)
        ahead = [
            j for j in jobs.values()
            if j["status"] in ("queued", "downloading")
            and j["created_at"] <= created_at
        ]
        position = len(ahead)

    # ใส่ job_id ลงคิวให้ worker ทำงานทีละอัน
    job_queue.put(job_id)

    print(f"[enqueue] งานใหม่: {job_id} | url={video_url} | format={file_format} | quality={quality} | คิวลำดับ ~{position}")

    return jsonify({
        "job_id": job_id,
        "position": position,
        "message": f"เข้าคิวเรียบร้อย ลำดับที่ {position}"
    })


@app.route("/status/<job_id>")
def status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return jsonify({"error": "ไม่พบงานนี้ในระบบคิว"}), 404

        # คำนวณลำดับคิวปัจจุบัน
        if job["status"] in ("done", "error"):
            position = 0
        else:
            ahead = [
                j for j in jobs.values()
                if j["status"] in ("queued", "downloading")
                and j["created_at"] <= job["created_at"]
            ]
            position = len(ahead)

        return jsonify({
            "job_id": job_id,
            "status": job["status"],
            "progress": job["progress"],
            "position": position,
            "error": job["error"],
        })


@app.route("/download/<job_id>")
def download(job_id):
    with jobs_lock:
        job = jobs.get(job_id)

        if not job:
            return jsonify({"error": "ไม่พบงานนี้ในระบบคิว"}), 404

        if job["status"] != "done":
            return jsonify({"error": "ไฟล์ยังไม่พร้อมดาวน์โหลด"}), 400

        filepath = job["filepath"]
        download_name = job["download_name"]
        mimetype = job["mimetype"]

    if not filepath or not os.path.exists(filepath):
        return jsonify({"error": "ไม่พบไฟล์บนดิสก์"}), 500

    print(f"[download] ส่งไฟล์ให้ client: {job_id} -> {download_name}")

    return send_file(
        filepath,
        as_attachment=True,
        download_name=download_name,
        mimetype=mimetype
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
