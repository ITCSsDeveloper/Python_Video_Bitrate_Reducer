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
def process_single_video(input_path, output_folder, bitrate_reduction_percent, message_queue=None):
    """ประมวลผลไฟล์เดียวและรายงานความคืบหน้าผ่าน message_queue (ถ้ามี)"""
    filename = os.path.basename(input_path)
    file_ext = pathlib.Path(filename).suffix.lower()

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

    # คำสั่ง FFmpeg พร้อม progress ผ่าน pipe:1
    command = [
        FFMPEG_PATH,
        '-y',
        '-i', input_path,
        '-c:v', GPU_ENCODER,
        '-b:v', new_bitrate_kbs,
        '-rc', 'cbr',
        '-c:a', 'copy',
        '-progress', 'pipe:1',
        '-nostats',
        output_path
    ]

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
def start_conversion(input_folder, output_folder, reduction_percent, max_workers, message_queue):
    """ฟังก์ชันที่ถูกเรียกเมื่อกดปุ่มเริ่มแปลง - รันใน Background Thread"""
    
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
            # ส่ง message_queue ให้ worker เพื่อรายงานความคืบหน้า
            future = executor.submit(process_single_video, input_path, output_folder, reduction_percent, message_queue)
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

        # --- UI Elements ---
        
        # Frame 1: Input/Output Paths
        frame1 = tk.LabelFrame(master, text="โฟลเดอร์", padx=10, pady=10)
        frame1.pack(padx=10, pady=5, fill="x")

        # Input Folder
        tk.Label(frame1, text="Input Folder:").grid(row=0, column=0, sticky="w", pady=2)
        tk.Entry(frame1, textvariable=self.input_folder, width=50).grid(row=0, column=1, padx=5, pady=2)
        tk.Button(frame1, text="Browse", command=lambda: self.browse_folder(self.input_folder)).grid(row=0, column=2, padx=5, pady=2)

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
        
        # แสดงสถานะ FFmpeg
        ffmpeg_status = "✅ พร้อมใช้งาน" if os.path.exists(FFMPEG_PATH) or FFMPEG_PATH == 'ffmpeg' else "❌ ไม่พบ"
        tk.Label(frame2, text=f"FFmpeg: {ffmpeg_status}").grid(row=2, column=0, sticky="w", pady=2)
        tk.Label(frame2, text=f"GPU Encoder: {GPU_ENCODER}").grid(row=2, column=1, sticky="w", pady=2)
        
        # Frame 3: Start Button & Status
        frame3 = tk.Frame(master, padx=10, pady=10)
        frame3.pack(padx=10, pady=5, fill="both", expand=True)
        
        # Start Button
        self.start_button = tk.Button(frame3, text="เริ่มแปลง (Start Conversion)", 
                  command=self.execute_conversion, 
                  font=("Helvetica", 12, "bold"),
                  bg="green", fg="white")
        self.start_button.pack(pady=10, fill="x")
        # Overall Progress
        tk.Label(frame3, text="Overall Progress:").pack(pady=5, anchor="w")
        self.overall_progress = ttk.Progressbar(frame3, orient='horizontal', length=400, mode='determinate')
        self.overall_progress.pack(fill="x", padx=5)

        # Per-file progress container
        tk.Label(frame3, text="ความคืบหน้าของแต่ละไฟล์:").pack(pady=5, anchor="w")
        self.files_container = tk.Frame(frame3)
        self.files_container.pack(fill="both", expand=False)
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
                    self.start_button.config(state=tk.NORMAL, text="เริ่มแปลง (Start Conversion)")
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
                    # create widgets for each file
                    for fp in files:
                        fname = os.path.basename(fp)
                        row = tk.Frame(self.files_container)
                        lbl = tk.Label(row, text=fname, width=40, anchor='w')
                        pb = ttk.Progressbar(row, orient='horizontal', length=300, mode='determinate', maximum=100)
                        lbl.pack(side='left', padx=(0,5))
                        pb.pack(side='left', fill='x', expand=True)
                        row.pack(fill='x', pady=2)
                        self.file_progress_bars[fname] = (lbl, pb)
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
                    if fname in self.file_progress_bars:
                        lbl, pb = self.file_progress_bars[fname]
                        try:
                            pb['value'] = percent
                            # บังคับให้อัพเดททันที
                            pb.update_idletasks()
                        except Exception:
                            pass
                        # update label to show percent
                        try:
                            lbl.config(text=f"{fname} - {percent}%")
                        except Exception:
                            pass
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
        
        # ล้างข้อความเก่า
        self.status_text.delete(1.0, tk.END)
        
        # เปลี่ยนสถานะปุ่ม
        self.is_processing = True
        self.start_button.config(state=tk.DISABLED, text="กำลังแปลง... (Processing)")
        self.master.config(cursor="wait")
        
        # รันการแปลงใน Thread ใหม่
        self.conversion_thread = threading.Thread(
            target=start_conversion,
            args=(
                self.input_folder.get(),
                self.output_folder.get(),
                self.reduction_percent.get(),
                self.max_workers.get(),
                self.message_queue
            ),
            daemon=True
        )
        self.conversion_thread.start()


if __name__ == "__main__":
    root = tk.Tk()
    app = VideoConverterApp(root)
    root.mainloop()