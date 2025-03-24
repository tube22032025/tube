#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import uuid
import logging
import time
import re
import shutil
from datetime import datetime
from flask import Flask, request, render_template, send_from_directory, jsonify, url_for
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
import atexit

# Cấu hình logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("youtube.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Kiểm tra thư viện
try:
    from pytubefix import YouTube
    from pytubefix.cli import on_progress
    USING_PYTUBEFIX = True
    logger.info("Sử dụng pytubefix để tải video YouTube")
except ImportError:
    try:
        from pytube import YouTube
        USING_PYTUBEFIX = False
        logger.warning("Cảnh báo: Đang sử dụng pytube thay vì pytubefix. Có thể gặp lỗi HTTP 400.")
    except ImportError:
        logger.error("Thư viện pytubefix và pytube chưa được cài đặt. Vui lòng cài đặt: pip install pytubefix")
        exit(1)

# Khởi tạo Flask app
app = Flask(__name__)

FILE_CLEANUP_THRESHOLD = 7200  # Xóa file sau 2 giờ (7200 giây)
MAX_WORKERS = int(os.environ.get('MAX_WORKERS', 5))  # Số lượng worker tối đa cho tải song song
MAX_REQUESTS_PER_MINUTE = 30  # Giới hạn số yêu cầu mỗi phút

app.config.update(
    DOWNLOAD_FOLDER=os.environ.get('DOWNLOAD_FOLDER', 'downloads'),
    TEMP_FOLDER=os.environ.get('TEMP_FOLDER', 'temp'),
    FILE_CLEANUP_THRESHOLD=FILE_CLEANUP_THRESHOLD,
    SECRET_KEY=os.environ.get('SECRET_KEY', os.urandom(24).hex()),
    DEBUG=os.environ.get('DEBUG', 'False').lower() == 'true',
    MAX_CONTENT_LENGTH=500 * 1024 * 1024  # Giới hạn kích thước tải lên 500MB
)

# Tạo thư mục nếu chưa tồn tại
for folder in [app.config['DOWNLOAD_FOLDER'], app.config['TEMP_FOLDER']]:
    os.makedirs(folder, exist_ok=True)

# Biến toàn cục để theo dõi số lượng yêu cầu
request_count = 0
last_request_time = time.time()

def get_disk_usage_percent(path):
    """Lấy phần trăm sử dụng của ổ đĩa chứa path"""
    try:
        total, used, free = shutil.disk_usage(path)
        return (used / total) * 100
    except Exception as e:
        logger.error("Không thể lấy thông tin dung lượng ổ đĩa", exc_info=True)
        return 0

def is_valid_youtube_url(url):
    youtube_regex = r'^(https?\:\/\/)?(www\.)?(youtube\.com|youtu\.?be)\/.+$'
    return re.match(youtube_regex, url) is not None

def generate_unique_filename(title):
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c==' ']).rstrip()
    safe_title = safe_title.replace(' ', '_')
    return f"{safe_title}_{uuid.uuid4().hex[:8]}"

def format_file_size(size_bytes):
    """Cải thiện hàm hiển thị kích thước file với độ chính xác cao hơn"""
    if size_bytes < 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while size_bytes >= 1024.0 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    return f"{size_bytes:.2f} {units[i]}"

def download_youtube_video(url, resolution='720p'):
    global request_count, last_request_time

    # Kiểm tra giới hạn tốc độ
    current_time = time.time()
    elapsed_time = current_time - last_request_time
    if request_count >= MAX_REQUESTS_PER_MINUTE and elapsed_time < 60:
        sleep_time = 60 - elapsed_time
        logger.info(f"Đã đạt giới hạn tốc độ. Chờ {sleep_time:.2f} giây...")
        time.sleep(sleep_time)
    if elapsed_time >= 60:
        request_count = 0
        last_request_time = current_time

    try:
        logger.info(f"Bắt đầu tải video từ URL: {url} với độ phân giải {resolution}")
        yt_args = {'use_oauth': True, 'allow_oauth_cache': True}

        yt = YouTube(url, **yt_args)
        video_title = yt.title
        logger.info(f"Đã tìm thấy video: {video_title}")

        safe_filename = generate_unique_filename(video_title)

        # Tìm stream video phù hợp theo độ phân giải yêu cầu
        video_streams = yt.streams.filter(progressive=True, file_extension='mp4')
        video_stream = video_streams.filter(res=resolution).first() or video_streams.order_by('resolution').desc().first()

        if not video_stream:
            logger.error("Không tìm thấy stream video phù hợp")
            return None, "Không tìm thấy stream video phù hợp"

        output_path = os.path.join(app.config['DOWNLOAD_FOLDER'], f"{safe_filename}.mp4")

        # Tải video
        start_time = time.time()
        video_stream.download(output_path=app.config['DOWNLOAD_FOLDER'], filename=f"{safe_filename}.mp4")
        download_time = time.time() - start_time

        file_size = os.path.getsize(output_path)
        request_count += 1

        logger.info(f"Đã tải xong video ({format_file_size(file_size)}) trong {download_time:.2f} giây")

        return {
            "title": video_title,
            "filename": f"{safe_filename}.mp4",
            "path": output_path,
            "size": file_size,
            "formatted_size": format_file_size(file_size),
            "resolution": video_stream.resolution,
            "url": url,
            "download_time": f"{download_time:.2f} giây"
        }, None

    except Exception as e:
        logger.error(f"Lỗi khi tải video: {str(e)}", exc_info=True)
        return None, f"Lỗi khi tải video: {str(e)}"

def cleanup_old_files():
    try:
        threshold = app.config['FILE_CLEANUP_THRESHOLD']
        current_time = time.time()
        disk_usage = get_disk_usage_percent(app.config['DOWNLOAD_FOLDER'])
        logger.info(f"Dung lượng ổ đĩa hiện tại: {disk_usage:.1f}%")
        files_removed, total_space_freed = 0, 0

        if disk_usage >= 50:
            logger.warning(f"Dung lượng ổ đĩa đã đạt {disk_usage:.1f}%, bắt đầu xóa tất cả file .mp4")
            for folder in [app.config['DOWNLOAD_FOLDER'], app.config['TEMP_FOLDER']]:
                for filename in os.listdir(folder):
                    if filename.endswith('.mp4'):
                        file_path = os.path.join(folder, filename)
                        if os.path.isfile(file_path):
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            files_removed += 1
                            total_space_freed += file_size
                            logger.info(f"Đã xóa file do ổ đĩa đầy: {file_path} (kích thước: {format_file_size(file_size)})")
            logger.warning(f"Đã xóa {files_removed} file .mp4, giải phóng {format_file_size(total_space_freed)} do ổ đĩa đạt ngưỡng 50%")
        else:
            for folder in [app.config['DOWNLOAD_FOLDER'], app.config['TEMP_FOLDER']]:
                for filename in os.listdir(folder):
                    file_path = os.path.join(folder, filename)
                    if os.path.isfile(file_path):
                        file_age = current_time - os.path.getmtime(file_path)
                        if file_age > threshold:
                            file_size = os.path.getsize(file_path)
                            os.remove(file_path)
                            files_removed += 1
                            total_space_freed += file_size
                            logger.info(f"Đã xóa file cũ: {file_path} (kích thước: {format_file_size(file_size)})")
            logger.info(f"Tổng cộng đã xóa {files_removed} file, giải phóng {format_file_size(total_space_freed)}")
        return files_removed, total_space_freed
    except Exception as e:
        logger.error(f"Lỗi khi dọn dẹp file: {str(e)}", exc_info=True)
        return 0, 0

# Lập lịch tự động dọn dẹp file mỗi 30 phút
scheduler = BackgroundScheduler()
scheduler.add_job(cleanup_old_files, 'interval', minutes=30)
scheduler.start()

# Đảm bảo scheduler được tắt khi ứng dụng kết thúc
atexit.register(lambda: scheduler.shutdown())

@app.route('/')
def index():
    cleanup_old_files()
    return render_template('index.html')

@app.route('/favicon.ico')
def favicon():
    return "", 204

@app.route('/download', methods=['POST'])
def download_video():
    try:
        url = request.form.get('url')
        resolution = request.form.get('resolution', '720p')

        if not url or not is_valid_youtube_url(url):
            return jsonify({"error": "URL không hợp lệ. Vui lòng nhập URL YouTube hợp lệ"}), 400

        video_info, error = download_youtube_video(url, resolution)
        
        if error:
            return jsonify({"error": error}), 400

        return jsonify({
            "success": True,
            "video": {
                "title": video_info["title"],
                "filename": video_info["filename"],
                "size": video_info["formatted_size"],
                "resolution": video_info["resolution"],
                "download_time": video_info["download_time"],
                "download_url": url_for('get_video', filename=video_info["filename"])
            }
        })
    except Exception as e:
        logger.error(f"Lỗi không xác định: {str(e)}", exc_info=True)
        return jsonify({"error": f"Đã xảy ra lỗi: {str(e)}"}), 500

@app.route('/videos/<path:filename>')
def get_video(filename):
    return send_from_directory(
        directory=app.config['DOWNLOAD_FOLDER'],
        path=filename,
        as_attachment=True
    )

@app.errorhandler(404)
def page_not_found(error):
    return render_template('404.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 80))
    app.run(host='0.0.0.0', port=port, debug=app.config['DEBUG'])
