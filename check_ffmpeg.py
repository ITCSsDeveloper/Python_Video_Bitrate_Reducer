import subprocess
import sys
import os

def check_command(cmd_name, cmd_path=None):
    """ตรวจสอบว่าคำสั่งมีอยู่ในระบบหรือไม่"""
    try:
        # ถ้ามี path เฉพาะ ใช้ path นั้น
        if cmd_path and os.path.exists(cmd_path):
            result = subprocess.run(
                [cmd_path, '-version'],
                capture_output=True,
                text=True,
                check=True
            )
            version_line = result.stdout.split('\n')[0]
            print(f"✅ {cmd_name} พบแล้ว (Local): {cmd_path}")
            print(f"   {version_line}")
            return True
        
        # ลองหาจาก PATH
        result = subprocess.run(
            [cmd_name, '-version'],
            capture_output=True,
            text=True,
            check=True
        )
        version_line = result.stdout.split('\n')[0]
        print(f"✅ {cmd_name} พบแล้ว (System PATH): {version_line}")
        return True
    except FileNotFoundError:
        print(f"❌ {cmd_name} ไม่พบในระบบ!")
        return False
    except Exception as e:
        print(f"⚠️ {cmd_name} มีปัญหา: {e}")
        return False

print("=" * 60)
print("ตรวจสอบการติดตั้ง FFmpeg และ FFprobe")
print("=" * 60)

# ตรวจสอบในโฟลเดอร์เดียวกับสคริปต์
script_dir = os.path.dirname(os.path.abspath(__file__))
local_ffmpeg = os.path.join(script_dir, 'ffmpeg.exe')
local_ffprobe = os.path.join(script_dir, 'ffprobe.exe')

print(f"\nตรวจสอบในโฟลเดอร์โปรแกรม: {script_dir}")
print("-" * 60)

ffmpeg_found = check_command('ffmpeg', local_ffmpeg)
ffprobe_found = check_command('ffprobe', local_ffprobe)

print("\n" + "=" * 60)
if ffmpeg_found and ffprobe_found:
    print("✅ ระบบพร้อมใช้งาน!")
else:
    print("❌ กรุณาติดตั้ง FFmpeg:")
    print("\n📋 วิธีที่ 1: วางไฟล์ในโฟลเดอร์โปรแกรม (แนะนำ)")
    print(f"   1. ดาวน์โหลดจาก: https://www.gyan.dev/ffmpeg/builds/")
    print(f"   2. เลือก 'ffmpeg-release-essentials.zip'")
    print(f"   3. แตกไฟล์และคัดลอก ffmpeg.exe, ffprobe.exe")
    print(f"   4. วางไฟล์ที่: {script_dir}")
    print("\n📋 วิธีที่ 2: เพิ่มใน System PATH")
    print("   1. ดาวน์โหลดและแตกไฟล์ตามด้านบน")
    print("   2. ย้ายโฟลเดอร์ไปที่ C:\\ffmpeg\\")
    print("   3. เพิ่ม C:\\ffmpeg\\bin ใน System PATH")
    print("   4. รีสตาร์ท PowerShell")
    print("\n📋 วิธีที่ 3: ใช้ Chocolatey")
    print("   choco install ffmpeg")
print("=" * 60)
