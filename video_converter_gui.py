import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import os
import subprocess
import json
import pathlib
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import queue
import sys

# --- Helpers ---
def format_size(num_bytes):
    """Format byte count into human readable string."""
    for unit in ['B','KB','MB','GB','TB']:
        if abs(num_bytes) < 1024.0:
            return f"{num_bytes:3.2f}{unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f}PB"

# --- การตั้งค่า GPU Encoder สำหรับ AMD RX 5700 XT ---
GPU_ENCODER = 'h264_amf' 
# หาก h264_amf ไม่ทำงาน อาจต้องลอง h264_qsv (Intel) หรือ h264_nvenc (NVIDIA) แทน

# --- Preset การตั้งค่า ---
PRESETS = {
    "เร็วที่สุด (Fast)": {
        "quality": "speed",
        "rc": "vbr_latency",
        "usage": "ultralowlatency",
        "preanalysis": "0",
        "hwaccel": "auto"
    },
    "สมดุล (Balanced)": {
        "quality": "balanced",
        "rc": "vbr_peak",
        "usage": "transcoding",
        "preanalysis": "1",
        "hwaccel": "auto"
    },
    "คุณภาพสูง (Quality)": {
        "quality": "quality",
        "rc": "vbr_peak",
        "usage": "transcoding",
        "preanalysis": "1",
        "hwaccel": "auto"
    },
    "พื้นฐาน (Basic)": {
        "quality": None,
        "rc": None,
        "usage": None,
        "preanalysis": None,
        "hwaccel": "auto"
    }
}

# --- หาตำแหน่ง ffmpeg และ ffprobe ---
def find_ffmpeg_path():
    """ค้นหา ffmpeg ในโฟลเดอร์โปรแกรมก่อน ถ้าไม่มีใช้จาก system PATH"""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # ตรวจสอบในโฟลเดอร์เดียวกับโปรแกรม
    local_ffmpeg = os.path.join(script_dir, 'ffmpeg.exe')
    local_ffprobe = os.path.join(script_dir, 'ffprobe.exe')
    
    if os.path.exists(local_ffmpeg) and os.path.exists(local_ffprobe):
        return local_ffmpeg, local_ffprobe
    
    # ถ้าไม่มี ใช้จาก PATH (จะ error ถ้าไม่มี)
    return 'ffmpeg', 'ffprobe'

FFMPEG_PATH, FFPROBE_PATH = find_ffmpeg_path()

# --- ฟังก์ชันย่อย: ดึง Bitrate เดิม (ใช้ FFprobe) ---
def get_video_bitrate(video_path):
    """ใช้ ffprobe เพื่อดึงค่า Video Bitrate เดิม (เป็น bps)"""
    try:
        # วิธีที่ 1: ดึง bitrate จาก stream metadata
        command = [
            FFPROBE_PATH,
            '-v', 'error',
            '-select_streams', 'v:0',
            '-show_entries', 'stream=bit_rate',
            '-of', 'json',
            video_path
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True,
                               encoding='utf-8', errors='replace')
        data = json.loads(result.stdout)
        if 'streams' in data and len(data['streams']) > 0 and 'bit_rate' in data['streams'][0]:
            bitrate = data['streams'][0]['bit_rate']
            if bitrate and bitrate != 'N/A':
                return int(bitrate)
        
        # วิธีที่ 2: ดึง bitrate จาก format (ไฟล์ทั้งหมด) และความยาว
        command = [
            FFPROBE_PATH,
            '-v', 'error',
            '-show_entries', 'format=duration,bit_rate',
            '-of', 'json',
            video_path
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=True,
                               encoding='utf-8', errors='replace')
        data = json.loads(result.stdout)
        
        if 'format' in data:
            # ลองใช้ format bitrate ก่อน
            if 'bit_rate' in data['format'] and data['format']['bit_rate']:
                format_bitrate = int(data['format']['bit_rate'])
                # ประมาณว่า video bitrate คือ 80% ของ total (เหลือ 20% เป็น audio)
                return int(format_bitrate * 0.8)
            
            # วิธีที่ 3: คำนวณจากขนาดไฟล์และความยาว
            if 'duration' in data['format']:
                duration = float(data['format']['duration'])
                file_size = os.path.getsize(video_path)  # bytes
                total_bitrate = int((file_size * 8) / duration)  # bits per second
                # ประมาณว่า video bitrate คือ 80% ของ total
                return int(total_bitrate * 0.8)
        
        return None
    except FileNotFoundError:
        raise FileNotFoundError("FFprobe not found")
    except Exception as e:
        return None

# --- ฟังก์ชันประมวลผลวิดีโอเดียว (รันใน Thread) ---
def process_single_video(input_path, output_folder, bitrate_reduction_percent, message_queue=None, stop_event=None, encoding_settings=None):
    """ประมวลผลไฟล์เดียวและรายงานความคืบหน้าผ่าน message_queue (ถ้ามี)"""
    filename = os.path.basename(input_path)
    file_ext = pathlib.Path(filename).suffix.lower()
    
    # ใช้ค่า default ถ้าไม่ได้ส่ง encoding_settings มา
    if encoding_settings is None:
        encoding_settings = PRESETS["พื้นฐาน (Basic)"]
    
    # ตรวจสอบว่าถูกสั่งหยุดหรือไม่
    if stop_event and stop_event.is_set():
        return f"⚠️ ยกเลิก: {filename}"

    video_extensions = ['.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv']
    if not os.path.isfile(input_path) or file_ext not in video_extensions:
        return f"ข้าม: {filename} (ไม่ใช่วิดีโอที่รองรับ)"

    # ดึงความยาววิดีโอ (duration) ด้วย ffprobe
    duration = None
    try:
        cmd_dur = [
            FFPROBE_PATH,
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            input_path
        ]
        res = subprocess.run(cmd_dur, capture_output=True, text=True, check=True, 
                           encoding='utf-8', errors='replace')
        duration = float(res.stdout.strip()) if res.stdout.strip() else None
    except FileNotFoundError:
        return f"❌ Error: ไม่พบ FFmpeg/FFprobe! กรุณาติดตั้ง FFmpeg และเพิ่มใน PATH\nดาวน์โหลดได้ที่: https://ffmpeg.org/download.html"
    except Exception:
        duration = None

    try:
        original_bitrate_bps = get_video_bitrate(input_path)
    except FileNotFoundError:
        return f"❌ Error: ไม่พบ FFmpeg/FFprobe! กรุณาติดตั้ง FFmpeg และเพิ่มใน PATH\nดาวน์โหลดได้ที่: https://ffmpeg.org/download.html"

    if original_bitrate_bps is None and duration is None:
        return f"❌ ข้าม: {filename} (ไม่สามารถดึงข้อมูล Bitrate/Duration ได้)"

    # ถ้าไม่มี bitrate ให้ประมาณจากขนาดไฟล์และ duration
    if original_bitrate_bps is None and duration:
        file_size = os.path.getsize(input_path)
        total_bitrate = int((file_size * 8) / duration)
        original_bitrate_bps = int(total_bitrate * 0.8)
    
    # ตรวจสอบว่า original_bitrate_bps ไม่เป็น None ก่อนคำนวณ
    if original_bitrate_bps is None:
        return f"❌ ข้าม: {filename} (ไม่สามารถดึงข้อมูล Bitrate ได้)"

    original_bitrate_mbps = original_bitrate_bps / 1_000_000
    reduction_factor = 1.0 - (bitrate_reduction_percent / 100.0)
    new_bitrate_bps = int(original_bitrate_bps * reduction_factor)
    new_bitrate_kbs = f"{new_bitrate_bps // 1000}k"
    new_bitrate_mbps = new_bitrate_bps / 1_000_000

    # ใช้ชื่อไฟล์เดิมเลย ไม่ต่อท้าย _reduced
    output_filename = filename
    output_path = os.path.join(output_folder, output_filename)

    # สร้างคำสั่ง FFmpeg พื้นฐาน
    command = [
        FFMPEG_PATH,
        '-y'
    ]
    
    # เพิ่ม hwaccel ถ้ามี
    if encoding_settings.get("hwaccel"):
        command.extend(['-hwaccel', encoding_settings["hwaccel"]])
    
    command.extend([
        '-i', input_path,
        '-c:v', GPU_ENCODER,
        '-b:v', new_bitrate_kbs,
        '-maxrate', new_bitrate_kbs,
        '-bufsize', f"{new_bitrate_bps * 2 // 1000}k"
    ])
    
    # เพิ่ม advanced options ถ้ามี
    if encoding_settings.get("quality"):
        command.extend(['-quality', encoding_settings["quality"]])
    
    if encoding_settings.get("rc"):
        command.extend(['-rc', encoding_settings["rc"]])
    
    if encoding_settings.get("usage"):
        command.extend(['-usage', encoding_settings["usage"]])
    
    if encoding_settings.get("preanalysis"):
        command.extend(['-preanalysis', encoding_settings["preanalysis"]])
    
    # เพิ่ม audio และ progress
    command.extend([
        '-c:a', 'copy',
        '-progress', 'pipe:1',
        '-nostats',
        output_path
    ])

    try:
        # บันทึกขนาดไฟล์ต้นฉบับก่อนเริ่ม
        try:
            orig_size = os.path.getsize(input_path)
        except Exception:
            orig_size = None

        # ส่งสถานะเริ่มต้น 0%
        if message_queue:
            try:
                message_queue.put(("file_progress", filename, 0))
            except Exception:
                pass

        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, 
                                text=True, bufsize=1, encoding='utf-8', errors='replace',
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)

        out_time_ms = 0
        last_percent = -1
        if proc.stdout:
            for raw_line in proc.stdout:
                # ตรวจสอบว่าถูกสั่งหยุดหรือไม่
                if stop_event and stop_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return f"⚠️ ยกเลิก: {filename}"
                
                line = raw_line.strip()
                if not line:
                    continue
                if '=' in line:
                    k, v = line.split('=', 1)
                    if k == 'out_time_ms':
                        try:
                            out_time_ms = int(v)
                            if duration and duration > 0:
                                percent = min(100, int((out_time_ms / 1000000.0) / duration * 100))
                            else:
                                percent = 0
                        except Exception:
                            percent = 0
                        # อัพเดททุกครั้งที่เปลี่ยนแปลง (แม้แต่ 1%)
                        if message_queue and percent != last_percent and percent >= 0:
                            try:
                                message_queue.put(("file_progress", filename, percent))
                            except Exception:
                                pass
                            last_percent = percent
                    elif k == 'progress' and v == 'end':
                        if message_queue:
                            try:
                                message_queue.put(("file_progress", filename, 100))
                            except Exception:
                                pass

        # อ่าน stderr ด้วย encoding ที่ปลอดภัย
        try:
            stderr = proc.stderr.read() if proc.stderr else ''
        except UnicodeDecodeError:
            # ถ้า encoding ล้มเหลว ให้ใช้ข้อความ default
            stderr = 'Error reading stderr output (encoding issue with file path or ffmpeg output)'
        except Exception as e:
            stderr = f'Error reading stderr: {str(e)}'
        ret = proc.wait()
        
        # ส่งสถานะ 100% เมื่อเสร็จสิ้น
        if message_queue and ret == 0:
            try:
                message_queue.put(("file_progress", filename, 100))
            except Exception:
                pass
        
        if ret == 0:
            # คำนวณขนาดไฟล์ผลลัพธ์และสรุปการลด
            try:
                out_size = os.path.getsize(output_path) if os.path.exists(output_path) else None
            except Exception:
                out_size = None

            # bitrate reductions
            try:
                bitrate_diff_bps = original_bitrate_bps - new_bitrate_bps
                bitrate_diff_pct = (bitrate_diff_bps / original_bitrate_bps) * 100 if original_bitrate_bps else 0
            except Exception:
                bitrate_diff_bps = None
                bitrate_diff_pct = 0

            # size reductions
            size_summary = ''
            if orig_size is not None and out_size is not None:
                size_diff = orig_size - out_size
                try:
                    size_diff_pct = (size_diff / orig_size) * 100 if orig_size else 0
                except Exception:
                    size_diff_pct = 0
                size_summary = f" | size: {format_size(orig_size)} → {format_size(out_size)} ({size_diff_pct:.1f}% , {format_size(size_diff)} saved)"

            return f"✅ สำเร็จ: {filename} | {original_bitrate_mbps:.2f} Mbps → {new_bitrate_mbps:.2f} Mbps (-{bitrate_diff_pct:.1f}%)" + size_summary
        else:
            error_msg = stderr or 'Unknown error from ffmpeg'
            if f"Unknown encoder '{GPU_ENCODER}'" in error_msg:
                return f"❌ Error: {filename} - ไม่พบ Encoder {GPU_ENCODER}! (GPU/FFmpeg ไม่รองรับ)"
            error_lines = [l for l in error_msg.splitlines() if l.strip()]
            last_error = error_lines[-1] if error_lines else 'Unknown error'
            return f"❌ Error ขณะแปลง {filename}: {last_error}"
    except FileNotFoundError:
        return "❌ Error: ไม่พบ FFmpeg! กรุณาติดตั้ง FFmpeg และเพิ่มใน PATH\nดาวน์โหลดได้ที่: https://ffmpeg.org/download.html"

# --- ฟังก์ชันหลักสำหรับ GUI (จัดการการประมวลผล) ---
def start_conversion(input_folder, output_folder, reduction_percent, max_workers, message_queue, stop_event=None, encoding_settings=None):
    """ฟังก์ชันที่ถูกเรียกเมื่อกดปุ่มเริ่มแปลง - รันใน Background Thread"""
    
    # ใช้ค่า default ถ้าไม่ได้ส่ง encoding_settings มา
    if encoding_settings is None:
        encoding_settings = PRESETS["พื้นฐาน (Basic)"]
    
    # Require input folder to exist. Output folder will be created automatically if missing.
    if not os.path.isdir(input_folder):
        message_queue.put(("error", "Error", "กรุณาเลือก Input Folder ที่ถูกต้อง"))
        message_queue.put(("done", None, None))
        return

    try:
        reduction_percent = int(reduction_percent)
        max_workers = int(max_workers)
        if not (0 < reduction_percent < 100) or max_workers < 1:
            raise ValueError
    except ValueError:
        message_queue.put(("error", "Error", "เปอร์เซ็นต์/จำนวนงานต้องเป็นตัวเลขที่ถูกต้อง"))
        message_queue.put(("done", None, None))
        return

    # If output_folder not provided, create default 'Output' inside input_folder
    if not output_folder:
        output_folder = os.path.join(input_folder, 'Output')
        try:
            os.makedirs(output_folder, exist_ok=True)
            message_queue.put(("text", f"สร้าง Output Folder อัตโนมัติที่: {output_folder}\n", None))
        except Exception as e:
            message_queue.put(("error", "Error", f"ไม่สามารถสร้าง Output Folder: {e}"))
            message_queue.put(("done", None, None))
            return
    else:
        # ถ้าโฟลเดอร์ที่ระบุไม่มี ให้สร้างและแจ้งผู้ใช้
        if not os.path.exists(output_folder):
            try:
                os.makedirs(output_folder, exist_ok=True)
                message_queue.put(("text", f"สร้าง Output Folder: {output_folder}\n", None))
            except Exception as e:
                message_queue.put(("error", "Error", f"ไม่สามารถสร้าง Output Folder: {e}"))
                message_queue.put(("done", None, None))
                return

    # รวบรวมรายการไฟล์ที่ต้องประมวลผล
    input_files = []
    video_extensions = ['.mp4', '.mov', '.mkv', '.avi', '.webm', '.flv']
    
    # ตรวจสอบว่า input_folder เป็นไฟล์หรือโฟลเดอร์
    if os.path.isfile(input_folder):
        # ถ้าเป็นไฟล์ ให้ใช้ไฟล์นั้นเลย
        if pathlib.Path(input_folder).suffix.lower() in video_extensions:
            input_files.append(input_folder)
            input_folder = os.path.dirname(input_folder)  # ใช้ parent folder
    else:
        # ถ้าเป็นโฟลเดอร์ ให้ค้นหาไฟล์ทั้งหมด
        for filename in os.listdir(input_folder):
            input_path = os.path.join(input_folder, filename)
            if os.path.isfile(input_path) and pathlib.Path(filename).suffix.lower() in video_extensions:
                input_files.append(input_path)

    if not input_files:
        message_queue.put(("text", f"ไม่พบไฟล์วิดีโอใน: {input_folder}\n", None))
        message_queue.put(("done", None, None))
        return

    # แจ้ง GUI ให้เตรียม progress bars
    message_queue.put(("init_files", input_files, None))
    message_queue.put(("overall_progress", None, 0))  # เริ่มต้น overall progress ที่ 0%
    message_queue.put(("text", f"พบ {len(input_files)} ไฟล์. กำลังเริ่มประมวลผลพร้อมกัน {max_workers} งาน...\n", None))
    message_queue.put(("text", f"--- ใช้ GPU Encoder: {GPU_ENCODER} ---\n", None))
    
    # ใช้ ThreadPoolExecutor เพื่อรันงาน FFmpeg พร้อมกัน
    # ใช้ ThreadPoolExecutor เพื่อรันงาน FFmpeg พร้อมกัน
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {}
        for input_path in input_files:
            # ตรวจสอบว่าถูกสั่งหยุดก่อนส่งงานใหม่
            if stop_event and stop_event.is_set():
                break
            # ส่ง message_queue ให้ worker เพื่อรายงานความคืบหน้า
            future = executor.submit(process_single_video, input_path, output_folder, reduction_percent, message_queue, stop_event, encoding_settings)
            futures[future] = input_path

        # เก็บผลลัพธ์เมื่อแต่ละงานเสร็จ (as_completed จะให้ผลเมื่อเสร็จทีละงาน)
        completed = 0
        total = len(futures)
        successful = 0
        total_original_size = 0
        total_output_size = 0
        
        for fut in as_completed(futures):
            result = fut.result()
            completed += 1
            
            # อัปเดต overall progress
            try:
                overall_percent = int((completed / total) * 100)
                message_queue.put(("overall_progress", None, overall_percent))
                message_queue.put(("text", f"[{completed}/{total}] {result}\n", None))
                
                # นับไฟล์ที่สำเร็จและเก็บข้อมูลขนาดไฟล์
                if result.startswith("✅ สำเร็จ"):
                    successful += 1
                    # พยายามดึงขนาดไฟล์จาก result ถ้ามี
                    try:
                        input_path = futures[fut]
                        orig_size = os.path.getsize(input_path)
                        total_original_size += orig_size
                        
                        # หาไฟล์ output
                        filename = os.path.basename(input_path)
                        output_path = os.path.join(output_folder, filename)
                        if os.path.exists(output_path):
                            out_size = os.path.getsize(output_path)
                            total_output_size += out_size
                    except Exception:
                        pass
                        
            except Exception:
                pass

    # สรุปผลการทำงาน
    message_queue.put(("text", "\n" + "="*60 + "\n", None))
    message_queue.put(("text", "🎉 สรุปผลการแปลงไฟล์\n", None))
    message_queue.put(("text", "="*60 + "\n", None))
    message_queue.put(("text", f"📊 ไฟล์ทั้งหมด: {total} ไฟล์\n", None))
    message_queue.put(("text", f"✅ แปลงสำเร็จ: {successful} ไฟล์\n", None))
    message_queue.put(("text", f"❌ แปลงไม่สำเร็จ: {total - successful} ไฟล์\n", None))
    
    if total_original_size > 0 and total_output_size > 0:
        total_saved = total_original_size - total_output_size
        saved_percent = (total_saved / total_original_size) * 100
        message_queue.put(("text", f"💾 ขนาดไฟล์เดิมรวม: {format_size(total_original_size)}\n", None))
        message_queue.put(("text", f"💾 ขนาดไฟล์ใหม่รวม: {format_size(total_output_size)}\n", None))
        message_queue.put(("text", f"🎯 ประหยัดพื้นที่รวม: {format_size(total_saved)} ({saved_percent:.1f}%)\n", None))
    
    message_queue.put(("text", "="*60 + "\n", None))
    message_queue.put(("text", "*** การแปลงไฟล์เสร็จสมบูรณ์ ***\n", None))
    message_queue.put(("done", None, None))

# --- สร้าง GUI ด้วย Tkinter ---
class VideoConverterApp:
    def __init__(self, master):
        self.master = master
        master.title("Video Bitrate Reducer (GPU/Parallel)")

        # Variables
        self.input_folder = tk.StringVar(value="")
        self.output_folder = tk.StringVar(value="")
        self.reduction_percent = tk.StringVar(value="30")
        self.max_workers = tk.StringVar(value="4")
        
        # Queue สำหรับการสื่อสารระหว่าง Thread และ GUI
        self.message_queue = queue.Queue()
        self.is_processing = False
        self.conversion_thread = None
        self.stop_event = threading.Event()  # สำหรับยกเลิกการทำงาน
        self.active_files = []  # เก็บรายการไฟล์ที่กำลังทำงาน
        
        # ตั้งค่า Encoding
        self.preset_var = tk.StringVar(value="พื้นฐาน (Basic)")
        self.current_encoding_settings = PRESETS["พื้นฐาน (Basic)"]
        
        # ตั้งค่าการปิดโปรแกรม
        master.protocol("WM_DELETE_WINDOW", self.on_closing)

        # --- UI Elements ---
        
        # Frame 1: Input/Output Paths
        frame1 = tk.LabelFrame(master, text="โฟลเดอร์", padx=10, pady=10)
        frame1.pack(padx=10, pady=5, fill="x")

        # Input Folder or File
        tk.Label(frame1, text="Input Folder/File:").grid(row=0, column=0, sticky="w", pady=2)
        tk.Entry(frame1, textvariable=self.input_folder, width=50).grid(row=0, column=1, padx=5, pady=2)
        tk.Button(frame1, text="Browse Folder", command=lambda: self.browse_folder(self.input_folder)).grid(row=0, column=2, padx=2, pady=2)
        tk.Button(frame1, text="Browse File", command=lambda: self.browse_file(self.input_folder)).grid(row=0, column=3, padx=2, pady=2)

        # Output Folder
        tk.Label(frame1, text="Output Folder:").grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(frame1, textvariable=self.output_folder, width=50).grid(row=1, column=1, padx=5, pady=2)
        tk.Button(frame1, text="Browse", command=lambda: self.browse_folder(self.output_folder)).grid(row=1, column=2, padx=5, pady=2)

        # Frame 2: Options
        frame2 = tk.LabelFrame(master, text="ตั้งค่าการแปลง", padx=10, pady=10)
        frame2.pack(padx=10, pady=5, fill="x")

        # Reduction Percentage
        tk.Label(frame2, text="ลด Bitrate ลง (%):").grid(row=0, column=0, sticky="w", pady=2)
        tk.Entry(frame2, textvariable=self.reduction_percent, width=10).grid(row=0, column=1, padx=5, pady=2, sticky="w")
        
        # Max Workers (Parallel Processing)
        tk.Label(frame2, text="จำนวนไฟล์พร้อมกัน:").grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(frame2, textvariable=self.max_workers, width=10).grid(row=1, column=1, padx=5, pady=2, sticky="w")
        
        # Encoding Preset
        tk.Label(frame2, text="โหมดการแปลง:").grid(row=2, column=0, sticky="w", pady=2)
        preset_combo = ttk.Combobox(frame2, textvariable=self.preset_var, values=list(PRESETS.keys()), state="readonly", width=20)
        preset_combo.grid(row=2, column=1, padx=5, pady=2, sticky="w")
        preset_combo.bind("<<ComboboxSelected>>", self.on_preset_change)
        
        # ปุ่มตั้งค่าขั้นสูง
        tk.Button(frame2, text="⚙️ ตั้งค่าขั้นสูง", command=self.open_advanced_settings).grid(row=2, column=2, padx=5, pady=2, sticky="w")
        
        # แสดงสถานะ FFmpeg
        ffmpeg_status = "✅ พร้อมใช้งาน" if os.path.exists(FFMPEG_PATH) or FFMPEG_PATH == 'ffmpeg' else "❌ ไม่พบ"
        tk.Label(frame2, text=f"FFmpeg: {ffmpeg_status}").grid(row=3, column=0, sticky="w", pady=2)
        tk.Label(frame2, text=f"GPU Encoder: {GPU_ENCODER}").grid(row=3, column=1, sticky="w", pady=2)
        
        # Frame 3: Start Button & Status
        frame3 = tk.Frame(master, padx=10, pady=10)
        frame3.pack(padx=10, pady=5, fill="both", expand=True)
        
        # Start Button
        self.start_button = tk.Button(frame3, text="เริ่มแปลง (Start Conversion)", 
                  command=self.execute_conversion, 
                  font=("Helvetica", 12, "bold"),
                  bg="green", fg="white")
        self.start_button.pack(pady=5, fill="x")
        
        # Cancel Button
        self.cancel_button = tk.Button(frame3, text="ยกเลิก (Cancel)", 
                  command=self.cancel_conversion, 
                  font=("Helvetica", 10),
                  bg="red", fg="white", state=tk.DISABLED)
        self.cancel_button.pack(pady=5, fill="x")
        
        # Overall Progress
        tk.Label(frame3, text="Overall Progress:").pack(pady=5, anchor="w")
        self.overall_progress = ttk.Progressbar(frame3, orient='horizontal', length=400, mode='determinate')
        self.overall_progress.pack(fill="x", padx=5)

        # Current file progress (แสดงเฉพาะไฟล์ที่กำลังทำงาน)
        tk.Label(frame3, text="ไฟล์ที่กำลังประมวลผล:").pack(pady=5, anchor="w")
        
        # สร้าง Canvas + Scrollbar สำหรับแสดง progress bars
        canvas_frame = tk.Frame(frame3, height=150)
        canvas_frame.pack(fill="x", padx=5, pady=5)
        canvas_frame.pack_propagate(False)  # ไม่ให้ขยายตามเนื้อหา
        
        self.files_canvas = tk.Canvas(canvas_frame, height=150)
        scrollbar = tk.Scrollbar(canvas_frame, orient="vertical", command=self.files_canvas.yview)
        self.files_container = tk.Frame(self.files_canvas)
        
        self.files_container.bind(
            "<Configure>",
            lambda e: self.files_canvas.configure(scrollregion=self.files_canvas.bbox("all"))
        )
        
        self.files_canvas.create_window((0, 0), window=self.files_container, anchor="nw")
        self.files_canvas.configure(yscrollcommand=scrollbar.set)
        
        self.files_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # เก็บ progressbars ของแต่ละไฟล์
        self.file_progress_bars = {}

        # Status Text Area (log)
        tk.Label(frame3, text="สถานะการทำงาน / Log:").pack(pady=5, anchor="w")
        self.status_text = tk.Text(frame3, height=8, width=80, wrap=tk.WORD, bg="light gray")
        self.status_text.pack(fill="both", expand=True)
        
        # เริ่มตรวจสอบ Queue
        self.check_queue()
        
    def browse_folder(self, var_to_set):
        folder_selected = filedialog.askdirectory()
        if folder_selected:
            var_to_set.set(folder_selected)
    
    def browse_file(self, var_to_set):
        """เลือกไฟล์วิดีโอเดียว"""
        file_selected = filedialog.askopenfilename(
            title="เลือกไฟล์วิดีโอ",
            filetypes=[
                ("Video files", "*.mp4 *.mov *.mkv *.avi *.webm *.flv"),
                ("All files", "*.*")
            ]
        )
        if file_selected:
            var_to_set.set(file_selected)
    
    def on_preset_change(self, event=None):
        """เปลี่ยน encoding settings เมื่อเลือก preset"""
        preset_name = self.preset_var.get()
        self.current_encoding_settings = PRESETS[preset_name].copy()
        self.status_text.insert(tk.END, f"✅ เปลี่ยนโหมด: {preset_name}\n")
        self.status_text.see(tk.END)
    
    def open_advanced_settings(self):
        """เปิดหน้าต่างตั้งค่าขั้นสูง"""
        settings_window = tk.Toplevel(self.master)
        settings_window.title("ตั้งค่าการ Encode ขั้นสูง")
        settings_window.geometry("500x400")
        settings_window.resizable(False, False)
        
        # Frame หลัก
        main_frame = tk.Frame(settings_window, padx=20, pady=20)
        main_frame.pack(fill="both", expand=True)
        
        tk.Label(main_frame, text="⚙️ ตั้งค่า FFmpeg Encoder ขั้นสูง", font=("Helvetica", 14, "bold")).pack(pady=(0, 15))
        
        # Variables สำหรับ settings
        quality_var = tk.StringVar(value=self.current_encoding_settings.get("quality") or "")
        rc_var = tk.StringVar(value=self.current_encoding_settings.get("rc") or "")
        usage_var = tk.StringVar(value=self.current_encoding_settings.get("usage") or "")
        preanalysis_var = tk.StringVar(value=self.current_encoding_settings.get("preanalysis") or "")
        hwaccel_var = tk.StringVar(value=self.current_encoding_settings.get("hwaccel") or "auto")
        
        # Quality Setting
        quality_frame = tk.LabelFrame(main_frame, text="Quality (คุณภาพ)", padx=10, pady=10)
        quality_frame.pack(fill="x", pady=5)
        
        tk.Label(quality_frame, text="Quality Level:").grid(row=0, column=0, sticky="w", pady=2)
        quality_combo = ttk.Combobox(quality_frame, textvariable=quality_var, 
                                     values=["", "speed", "balanced", "quality"], state="readonly", width=15)
        quality_combo.grid(row=0, column=1, padx=5, pady=2)
        tk.Label(quality_frame, text="(speed=เร็ว, balanced=สมดุล, quality=คุณภาพ)", font=("Arial", 8)).grid(row=1, column=0, columnspan=2, sticky="w")
        
        # Rate Control
        rc_frame = tk.LabelFrame(main_frame, text="Rate Control (การควบคุม Bitrate)", padx=10, pady=10)
        rc_frame.pack(fill="x", pady=5)
        
        tk.Label(rc_frame, text="RC Mode:").grid(row=0, column=0, sticky="w", pady=2)
        rc_combo = ttk.Combobox(rc_frame, textvariable=rc_var,
                                values=["", "cbr", "vbr_latency", "vbr_peak", "cqp"], state="readonly", width=15)
        rc_combo.grid(row=0, column=1, padx=5, pady=2)
        tk.Label(rc_frame, text="(cbr=คงที่, vbr_latency=เร็ว, vbr_peak=คุณภาพ)", font=("Arial", 8)).grid(row=1, column=0, columnspan=2, sticky="w")
        
        # Usage
        usage_frame = tk.LabelFrame(main_frame, text="Usage (การใช้งาน)", padx=10, pady=10)
        usage_frame.pack(fill="x", pady=5)
        
        tk.Label(usage_frame, text="Usage Mode:").grid(row=0, column=0, sticky="w", pady=2)
        usage_combo = ttk.Combobox(usage_frame, textvariable=usage_var,
                                   values=["", "ultralowlatency", "lowlatency", "webcam", "transcoding"], state="readonly", width=15)
        usage_combo.grid(row=0, column=1, padx=5, pady=2)
        tk.Label(usage_frame, text="(ultralowlatency=เร็วสุด, transcoding=ปกติ)", font=("Arial", 8)).grid(row=1, column=0, columnspan=2, sticky="w")
        
        # Advanced Options
        adv_frame = tk.LabelFrame(main_frame, text="ตัวเลือกเพิ่มเติม", padx=10, pady=10)
        adv_frame.pack(fill="x", pady=5)
        
        tk.Label(adv_frame, text="Preanalysis:").grid(row=0, column=0, sticky="w", pady=2)
        preanalysis_combo = ttk.Combobox(adv_frame, textvariable=preanalysis_var,
                                         values=["", "0", "1"], state="readonly", width=15)
        preanalysis_combo.grid(row=0, column=1, padx=5, pady=2)
        
        tk.Label(adv_frame, text="Hardware Accel:").grid(row=1, column=0, sticky="w", pady=2)
        hwaccel_combo = ttk.Combobox(adv_frame, textvariable=hwaccel_var,
                                     values=["", "auto", "dxva2", "d3d11va"], state="readonly", width=15)
        hwaccel_combo.grid(row=1, column=1, padx=5, pady=2)
        
        # ปุ่มบันทึก
        button_frame = tk.Frame(main_frame)
        button_frame.pack(pady=(15, 0))
        
        def save_settings():
            self.current_encoding_settings = {
                "quality": quality_var.get() if quality_var.get() else None,
                "rc": rc_var.get() if rc_var.get() else None,
                "usage": usage_var.get() if usage_var.get() else None,
                "preanalysis": preanalysis_var.get() if preanalysis_var.get() else None,
                "hwaccel": hwaccel_var.get() if hwaccel_var.get() else None
            }
            self.preset_var.set("กำหนดเอง (Custom)")
            self.status_text.insert(tk.END, "✅ บันทึกการตั้งค่าขั้นสูงแล้ว\n")
            self.status_text.see(tk.END)
            settings_window.destroy()
        
        def reset_settings():
            quality_var.set("")
            rc_var.set("")
            usage_var.set("")
            preanalysis_var.set("")
            hwaccel_var.set("auto")
        
        tk.Button(button_frame, text="💾 บันทึก", command=save_settings, bg="green", fg="white", width=12).pack(side="left", padx=5)
        tk.Button(button_frame, text="🔄 รีเซ็ต", command=reset_settings, width=12).pack(side="left", padx=5)
        tk.Button(button_frame, text="❌ ยกเลิก", command=settings_window.destroy, width=12).pack(side="left", padx=5)
    
    def cancel_conversion(self):
        """ยกเลิกการแปลงไฟล์"""
        if self.is_processing:
            result = messagebox.askyesno(
                "ยืนยันการยกเลิก",
                "คุณต้องการยกเลิกการแปลงไฟล์หรือไม่?\n(ไฟล์ที่กำลังทำงานจะถูกหยุด)"
            )
            if result:
                self.stop_event.set()
                self.status_text.insert(tk.END, "\n⚠️ กำลังยกเลิกการทำงาน...\n")
                self.cancel_button.config(state=tk.DISABLED)
    
    def on_closing(self):
        """ฟังก์ชันที่ถูกเรียกเมื่อปิดโปรแกรม"""
        if self.is_processing:
            result = messagebox.askyesno(
                "ยืนยันการปิดโปรแกรม",
                "มีการแปลงไฟล์ที่กำลังทำงานอยู่\nคุณต้องการปิดโปรแกรมหรือไม่?"
            )
            if result:
                self.stop_event.set()  # ส่งสัญญาณให้หยุดทำงาน
                self.master.after(1000, self.master.destroy)  # รอ 1 วินาทีแล้วปิด
        else:
            self.master.destroy()
    
    def check_queue(self):
        """ตรวจสอบ Queue และอัพเดท UI อย่างต่อเนื่อง"""
        try:
            # ประมวลผล message หลายตัวในแต่ละรอบเพื่อป้องกันการค้าง
            processed = 0
            while processed < 50:  # จำกัดไม่ให้ประมวลผลมากเกินไปในครั้งเดียว
                msg_type, title, message = self.message_queue.get_nowait()
                processed += 1
                
                if msg_type == "text":
                    self.status_text.insert(tk.END, title)
                    self.status_text.see(tk.END)
                elif msg_type == "error":
                    messagebox.showerror(title, message)
                elif msg_type == "done":
                    self.is_processing = False
                    self.stop_event.clear()  # รีเซ็ต stop event
                    self.start_button.config(state=tk.NORMAL, text="เริ่มแปลง (Start Conversion)")
                    self.cancel_button.config(state=tk.DISABLED)
                    self.master.config(cursor="")
                    # รีเซ็ต title
                    self.master.title("Video Bitrate Reducer (GPU/Parallel) - เสร็จสิ้น!")
                elif msg_type == "init_files":
                    # title contains the list of input file full paths
                    files = title
                    # clear existing per-file widgets
                    for child in self.files_container.winfo_children():
                        child.destroy()
                    self.file_progress_bars.clear()
                    
                    # แสดงเฉพาะไฟล์ที่กำลังทำงาน (สูงสุด max_workers)
                    # เก็บรายการไฟล์ทั้งหมดไว้
                    self.active_files = [os.path.basename(fp) for fp in files]
                    
                    # สร้าง progress bars สำหรับไฟล์ที่กำลังทำงาน
                    max_display = min(len(files), int(self.max_workers.get()) if self.max_workers.get().isdigit() else 4)
                    for i in range(max_display):
                        fname = f"รอดำเนินการ... ({i+1}/{max_display})"
                        row = tk.Frame(self.files_container)
                        lbl = tk.Label(row, text=fname, width=50, anchor='w', font=("Arial", 9))
                        pb = ttk.Progressbar(row, orient='horizontal', length=300, mode='determinate', maximum=100)
                        lbl.pack(side='left', padx=(0,5))
                        pb.pack(side='left', fill='x', expand=True)
                        row.pack(fill='x', pady=2)
                        self.file_progress_bars[i] = (lbl, pb, fname)
                    
                    # reset overall progress
                    try:
                        self.overall_progress['value'] = 0
                        self.overall_progress['maximum'] = 100
                    except Exception:
                        pass
                elif msg_type == 'file_progress':
                    # title = filename, message = percent
                    fname = title
                    percent = message
                    
                    # อัปเดต progress bar ที่ว่าง หรือที่กำลังแสดงไฟล์นี้อยู่
                    updated = False
                    for idx, (lbl, pb, current_fname) in self.file_progress_bars.items():
                        if current_fname == fname or "รอดำเนินการ" in current_fname:
                            try:
                                pb['value'] = percent
                                pb.update_idletasks()
                                lbl.config(text=f"{fname} - {percent}%")
                                self.file_progress_bars[idx] = (lbl, pb, fname)
                                updated = True
                                break
                            except Exception:
                                pass
                    
                    # ถ้าไฟล์เสร็จแล้ว (100%) ให้รีเซ็ตช่องนั้นเป็น "เสร็จสิ้น"
                    if percent == 100:
                        for idx, (lbl, pb, current_fname) in self.file_progress_bars.items():
                            if current_fname == fname:
                                try:
                                    lbl.config(text=f"✅ {fname} - เสร็จสิ้น")
                                except Exception:
                                    pass
                                break
                elif msg_type == 'overall_progress':
                    overall = message
                    try:
                        self.overall_progress['value'] = overall
                        # บังคับให้อัพเดททันที
                        self.overall_progress.update_idletasks()
                        # อัพเดทชื่อ label ให้แสดงเปอร์เซ็นต์
                        self.master.title(f"Video Converter - Overall: {overall}%")
                    except Exception as e:
                        # Debug: แสดง error ถ้ามี
                        self.status_text.insert(tk.END, f"Overall progress error: {e}\n")
                        pass
                    
        except queue.Empty:
            pass
        
        # ตรวจสอบ Queue ทุก 100ms
        self.master.after(100, self.check_queue)

    def execute_conversion(self):
        """เรียกใช้ฟังก์ชัน start_conversion ใน Thread เพื่อไม่ให้ GUI ค้าง"""
        
        if self.is_processing:
            messagebox.showwarning("กำลังทำงาน", "กรุณารอให้การแปลงปัจจุบันเสร็จสิ้นก่อน")
            return
        
        input_path = self.input_folder.get()
        
        # ตรวจสอบว่าเป็นไฟล์หรือโฟลเดอร์
        if not input_path:
            messagebox.showerror("Error", "กรุณาเลือก Input Folder หรือ File")
            return
        
        # ถ้าเป็นไฟล์ ให้แปลงเป็นโฟลเดอร์และสร้าง temp list
        if os.path.isfile(input_path):
            # ใช้โฟลเดอร์เดียวกับไฟล์เป็น input folder
            input_folder = os.path.dirname(input_path)
            # เก็บชื่อไฟล์ไว้เพื่อกรองในภายหลัง
            self.single_file_mode = os.path.basename(input_path)
        elif os.path.isdir(input_path):
            input_folder = input_path
            self.single_file_mode = None
        else:
            messagebox.showerror("Error", "Input path ไม่ถูกต้อง")
            return
        
        # ล้างข้อความเก่า
        self.status_text.delete(1.0, tk.END)
        
        # รีเซ็ต stop event
        self.stop_event.clear()
        
        # เปลี่ยนสถานะปุ่ม
        self.is_processing = True
        self.start_button.config(state=tk.DISABLED, text="กำลังแปลง... (Processing)")
        self.cancel_button.config(state=tk.NORMAL)
        self.master.config(cursor="wait")
        
        # รันการแปลงใน Thread ใหม่
        self.conversion_thread = threading.Thread(
            target=self.start_conversion_wrapper,
            args=(
                input_folder,
                self.output_folder.get(),
                self.reduction_percent.get(),
                self.max_workers.get(),
                self.message_queue
            ),
            daemon=True
        )
        self.conversion_thread.start()
    
    def start_conversion_wrapper(self, input_folder, output_folder, reduction_percent, max_workers, message_queue):
        """Wrapper สำหรับ start_conversion เพื่อจัดการกับโหมดไฟล์เดียว"""
        # ถ้าเป็นโหมดไฟล์เดียว ให้กรองไฟล์ก่อน
        if hasattr(self, 'single_file_mode') and self.single_file_mode:
            # สร้างรายการไฟล์ชั่วคราวเฉพาะไฟล์ที่เลือก
            import tempfile
            import shutil
            
            # แจ้งว่ากำลังประมวลผลไฟล์เดียว
            message_queue.put(("text", f"โหมดไฟล์เดียว: {self.single_file_mode}\n", None))
            
        start_conversion(input_folder, output_folder, reduction_percent, max_workers, message_queue, self.stop_event, self.current_encoding_settings)


if __name__ == "__main__":
    root = tk.Tk()
    app = VideoConverterApp(root)
    root.mainloop()