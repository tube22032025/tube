#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Ứng dụng tải video YouTube
Hỗ trợ tải video với độ phân giải 720p và 1080p
Tự động xóa file sau 2 giờ để tiết kiệm không gian ổ đĩa
"""

import os
import uuid
import logging
import time
import re
import shutil
import subprocess
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

def get_disk_usage_percent(path):
    """Lấy phần trăm sử dụng của ổ đĩa chứa path"""
    try:
        # Cách 1: Sử dụng thư viện shutil (cross-platform)
        total, used, free = shutil.disk_usage(path)
        return (used / total) * 100
    except:
        try:
            # Cách 2: Sử dụng lệnh df trên Linux/Mac
            result = subprocess.run(['df', path], capture_output=True, text=True)
            output = result.stdout.strip().split('\n')
            if len(output) >= 2:
                usage_percent = output[1].split()[4].replace('%', '')
                return float(usage_percent)
        except:
            logger.error("Không thể lấy thông tin dung lượng ổ đĩa", exc_info=True)
            return 0

def is_valid_youtube_url(url):
    youtube_regex = r'^(https?\:\/\/)?(www\.)?(youtube\.com|youtu\.?be)\/.+$'
    return re.match(youtube_regex, url) is not None

def generate_unique_filename(title):
    # Sử dụng phương pháp an toàn để tạo tên file
    safe_title = "".join([c for c in title if c.isalpha() or c.isdigit() or c==' ']).rstrip()
    safe_title = safe_title.replace(' ', '_')
    return f"{safe_title}_{uuid.uuid4().hex[:8]}"

def format_file_size(size_bytes):
    """
    Cải thiện hàm hiển thị kích thước file với độ chính xác cao hơn
    """
    if size_bytes < 0:
        return "0 B"
        
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    i = 0
    while size_bytes >= 1024.0 and i < len(units) - 1:
        size_bytes /= 1024.0
        i += 1
    
    # Hiển thị với 2 số thập phân cho độ chính xác
    return f"{size_bytes:.2f} {units[i]}"

def download_youtube_video(url, resolution='720p'):
    try:
        logger.info(f"Bắt đầu tải video từ URL: {url} với độ phân giải {resolution}")
        
        if USING_PYTUBEFIX:
            yt = YouTube(url, use_oauth=True, allow_oauth_cache=True, on_progress_callback=on_progress)
        else:
            yt = YouTube(url, use_oauth=True, allow_oauth_cache=True)
        
        video_title = yt.title
        logger.info(f"Đã tìm thấy video: {video_title}")
        
        safe_filename = generate_unique_filename(video_title)
        
        # Tìm stream video phù hợp
        video_stream = None
        if resolution == '1080p':
            video_stream = yt.streams.filter(progressive=True, file_extension='mp4', res='1080p').first()
            if not video_stream:
                logger.info("Không tìm thấy stream 1080p progressive, tìm kiếm stream 1080p không progressive")
                video_stream = yt.streams.filter(file_extension='mp4', res='1080p').first()
        
        if resolution == '720p' or not video_stream:
            logger.info("Tìm kiếm stream 720p progressive")
            video_stream = yt.streams.filter(progressive=True, file_extension='mp4', res='720p').first()
        
        if not video_stream:
            logger.info("Không tìm thấy stream theo yêu cầu, sử dụng stream có độ phân giải cao nhất")
            video_stream = yt.streams.filter(progressive=True, file_extension='mp4').order_by('resolution').desc().first()
        
        if not video_stream:
            logger.error("Không tìm thấy stream video phù hợp")
            return None, "Không tìm thấy stream video phù hợp"
        
        logger.info(f"Đã tìm thấy stream với độ phân giải: {video_stream.resolution}")
        
        output_path = os.path.join(app.config['DOWNLOAD_FOLDER'], f"{safe_filename}.mp4")
        
        # Tải video
        start_time = time.time()
        video_stream.download(output_path=app.config['DOWNLOAD_FOLDER'], filename=f"{safe_filename}.mp4")
        download_time = time.time() - start_time
        
        file_size = os.path.getsize(output_path)
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
        error_message = str(e)
        
        if "HTTP Error 400" in error_message:
            return None, "Lỗi kết nối đến YouTube. Vui lòng thử lại sau hoặc kiểm tra URL"
        elif "detected as a bot" in error_message:
            return None, "YouTube phát hiện yêu cầu là bot. Vui lòng thử lại sau"
        
        return None, f"Lỗi khi tải video: {error_message}"

def cleanup_old_files():
    try:
        threshold = app.config['FILE_CLEANUP_THRESHOLD']
        current_time = time.time()
        files_removed = 0
        total_space_freed = 0
        
        # Kiểm tra dung lượng ổ đĩa
        disk_usage = get_disk_usage_percent(app.config['DOWNLOAD_FOLDER'])
        logger.info(f"Dung lượng ổ đĩa hiện tại: {disk_usage:.1f}%")
        
        # Nếu dung lượng ổ đĩa >= 50%, xóa tất cả file .mp4
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
            return files_removed, total_space_freed
        
        # Nếu không, xóa file cũ theo thời gian như bình thường
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

# Lập lịch tự động dọn dẹp file
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
        
        if not url:
            return jsonify({"error": "Vui lòng nhập URL video YouTube"}), 400
            
        if not is_valid_youtube_url(url):
            return jsonify({"error": "URL không hợp lệ. Vui lòng nhập URL YouTube hợp lệ"}), 400
            
        video_info, error = download_youtube_video(url, resolution)
        
        if error:
            if "HTTP Error 400" in error:
                return jsonify({"error": "Lỗi kết nối đến YouTube. Vui lòng thử lại sau hoặc kiểm tra URL"}), 400
            if "detected as a bot" in error:
                return jsonify({"error": "YouTube phát hiện yêu cầu là bot. Vui lòng thử lại sau"}), 400
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

@app.route('/videos/<filename>')
def get_video(filename):
    return send_from_directory(
        directory=app.config['DOWNLOAD_FOLDER'],
        path=filename,
        as_attachment=True
    )

@app.route('/status')
def server_status():
    try:
        # Thông tin về server
        download_folder_size = sum(os.path.getsize(os.path.join(app.config['DOWNLOAD_FOLDER'], f)) 
                                for f in os.listdir(app.config['DOWNLOAD_FOLDER']) 
                                if os.path.isfile(os.path.join(app.config['DOWNLOAD_FOLDER'], f)))
        
        temp_folder_size = sum(os.path.getsize(os.path.join(app.config['TEMP_FOLDER'], f)) 
                            for f in os.listdir(app.config['TEMP_FOLDER']) 
                            if os.path.isfile(os.path.join(app.config['TEMP_FOLDER'], f)))
        
        download_files = len([f for f in os.listdir(app.config['DOWNLOAD_FOLDER']) 
                            if os.path.isfile(os.path.join(app.config['DOWNLOAD_FOLDER'], f))])
        
        disk_usage = get_disk_usage_percent(app.config['DOWNLOAD_FOLDER'])
        
        return jsonify({
            "status": "online",
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "download_folder_size": format_file_size(download_folder_size),
            "temp_folder_size": format_file_size(temp_folder_size),
            "download_files": download_files,
            "disk_usage": f"{disk_usage:.1f}%",
            "using_pytubefix": USING_PYTUBEFIX
        })
    except Exception as e:
        logger.error(f"Lỗi khi lấy trạng thái server: {str(e)}", exc_info=True)
        return jsonify({"status": "error", "error": str(e)}), 500

@app.route('/get_file_info', methods=['POST'])
def get_file_info():
    """API endpoint để lấy thông tin file từ URL YouTube trước khi tải"""
    try:
        url = request.form.get('url')
        
        if not url:
            return jsonify({"error": "Vui lòng nhập URL video YouTube"}), 400
            
        if not is_valid_youtube_url(url):
            return jsonify({"error": "URL không hợp lệ. Vui lòng nhập URL YouTube hợp lệ"}), 400
        
        # Chỉ lấy thông tin cơ bản mà không tải video
        if USING_PYTUBEFIX:
            yt = YouTube(url, use_oauth=True, allow_oauth_cache=True)
        else:
            yt = YouTube(url, use_oauth=True, allow_oauth_cache=True)
        
        # Lấy thông tin các stream có sẵn
        available_streams = []
        for stream in yt.streams.filter(progressive=True, file_extension='mp4'):
            available_streams.append({
                "resolution": stream.resolution,
                "mime_type": stream.mime_type,
                "estimated_size": format_file_size(stream.filesize) if hasattr(stream, 'filesize') else "Không xác định"
            })
        
        return jsonify({
            "success": True,
            "video_info": {
                "title": yt.title,
                "author": yt.author,
                "length": yt.length,
                "formatted_length": f"{yt.length // 60}:{yt.length % 60:02d}",
                "thumbnail_url": yt.thumbnail_url,
                "available_streams": available_streams
            }
        })
        
    except Exception as e:
        logger.error(f"Lỗi khi lấy thông tin video: {str(e)}", exc_info=True)
        return jsonify({"error": f"Không thể lấy thông tin video: {str(e)}"}), 500

@app.errorhandler(404)
def page_not_found(error):
    return render_template('404.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 80))
    app.run(host='0.0.0.0', port=port, debug=app.config['DEBUG'])