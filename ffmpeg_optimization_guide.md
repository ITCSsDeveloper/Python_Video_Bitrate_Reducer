# 🚀 คู่มือปรับแต่ง FFmpeg สำหรับความเร็วสูงสุด

## 📊 สถานะปัจจุบัน
- CPU ใช้งานต่อ Task: 3-5%
- GPU ใช้งานรวม: 25%
- ปัญหา: ไม่ได้ใช้ Hardware เต็มประสิทธิภาพ

## ✅ การปรับปรุงที่ทำแล้ว (อยู่ในโค้ดแล้ว)

```python
# 1. เพิ่ม Hardware Acceleration
'-hwaccel', 'auto'

# 2. เปลี่ยนจาก CBR เป็น VBR
'-rc', 'vbr_latency'  # แทน 'cbr'

# 3. เพิ่ม AMD AMF Optimizations
'-quality', 'speed'
'-usage', 'ultralowlatency'
'-preanalysis', '1'
```

## 🔧 ตัวเลือกเพิ่มเติม (ถ้ายังช้า)

### Option 1: ปรับ Quality/Speed Trade-off

แก้ไขในไฟล์ `video_converter_gui.py` บรรทัด ~175:

```python
# ✅ เร็วที่สุด (คุณภาพลดลงเล็กน้อย)
'-quality', 'speed'
'-usage', 'ultralowlatency'

# ⚖️ สมดุล (แนะนำ)
'-quality', 'balanced'
'-usage', 'lowlatency'

# 🎨 คุณภาพสูง (ช้ากว่า)
'-quality', 'quality'
'-usage', 'transcoding'
```

### Option 2: ปรับ Rate Control

```python
# เร็วที่สุด แต่ขนาดไฟล์อาจไม่แน่นอน
'-rc', 'cqp'  # Constant Quantization Parameter
'-qp_i', '23'
'-qp_p', '23'

# หรือ VBR ธรรมดา
'-rc', 'vbr_peak'
'-qmin', '20'
'-qmax', '28'
```

### Option 3: เพิ่มจำนวน Threads

แก้ในโค้ดบรรทัด ~175 เพิ่ม:

```python
'-threads', '0'  # ใช้ทุก CPU cores
```

### Option 4: ปิด Pre-analysis (ถ้า GPU ยังไม่เต็ม)

```python
'-preanalysis', '0'  # แทน '1'
```

## 🎯 คำแนะนำสำหรับ AMD RX 5700 XT

### การตั้งค่าที่แนะนำ (เร็วที่สุด):

```python
command = [
    FFMPEG_PATH,
    '-y',
    '-hwaccel', 'auto',
    '-hwaccel_output_format', 'auto',
    '-i', input_path,
    '-c:v', 'h264_amf',
    '-b:v', new_bitrate_kbs,
    '-maxrate', f"{int(new_bitrate_bps * 1.2) // 1000}k",
    '-bufsize', f"{new_bitrate_bps * 2 // 1000}k",
    '-quality', 'speed',
    '-rc', 'vbr_latency',
    '-usage', 'ultralowlatency',
    '-preanalysis', '0',
    '-enforce_hrd', '0',
    '-filler_data', '0',
    '-c:a', 'copy',
    '-progress', 'pipe:1',
    '-nostats',
    output_path
]
```

## 📈 การทดสอบ

### 1. ทดสอบ GPU Usage
```powershell
# ดู GPU utilization ด้วย Task Manager
# หรือติดตั้ง GPU-Z: https://www.techpowerup.com/gpuz/

# ใช้ ffmpeg โดยตรงเพื่อทดสอบ
ffmpeg -i input.mp4 -c:v h264_amf -quality speed -rc vbr_latency -b:v 2000k test.mp4
```

### 2. เปรียบเทียบความเร็ว
```powershell
# CBR (เดิม)
Measure-Command { ffmpeg -i input.mp4 -c:v h264_amf -rc cbr -b:v 2000k test1.mp4 }

# VBR (ใหม่)
Measure-Command { ffmpeg -i input.mp4 -c:v h264_amf -rc vbr_latency -quality speed -b:v 2000k test2.mp4 }
```

## ⚠️ ข้อควรระวัง

### ถ้า GPU ยังใช้งานไม่เต็ม อาจเกิดจาก:

1. **Bottleneck ที่ I/O (Hard Disk)**
   - ใช้ SSD แทน HDD
   - ลด `max_workers` ถ้า HDD ทำงานหนัก

2. **Decoder Bottleneck**
   - ไฟล์ input เป็น codec ที่ต้องใช้ CPU decode
   - แก้: เพิ่ม `-hwaccel_output_format auto`

3. **FFmpeg Version เก่า**
   - ตรวจสอบ: `ffmpeg -version`
   - ควรใช้ FFmpeg 5.0+ สำหรับ AMD AMF support ที่ดี

4. **Driver GPU ไม่ใหม่**
   - อัปเดท AMD Driver: https://www.amd.com/en/support

## 🔍 Debug Commands

### ดูข้อมูล Encoder ที่รองรับ
```powershell
ffmpeg -h encoder=h264_amf
```

### ดูข้อมูล Hardware Acceleration
```powershell
ffmpeg -hwaccels
```

### ทดสอบความเร็วจริง
```powershell
ffmpeg -i input.mp4 -c:v h264_amf -quality speed -rc vbr_latency -b:v 2000k -f null - -benchmark
```

## 📝 สรุป

### การเปลี่ยนแปลงหลัก:
1. ✅ `-rc vbr_latency` แทน `-rc cbr` → เร็วขึ้น 20-40%
2. ✅ `-quality speed` → ให้ความสำคัญกับความเร็ว
3. ✅ `-usage ultralowlatency` → ลด latency
4. ✅ `-hwaccel auto` → ใช้ hardware decoder
5. ✅ `-preanalysis 1` → วิเคราะห์ก่อน encode

### ผลลัพธ์ที่คาดหวัง:
- GPU Usage: 50-80% (ขึ้นกับคุณภาพ input)
- ความเร็วเพิ่มขึ้น: 30-50% เทียบกับ CBR
- คุณภาพ: ลดลงเล็กน้อย (1-3% ที่มองไม่เห็น)

---

**หมายเหตุ:** ถ้ายังช้า ให้ลดจำนวน `max_workers` เพราะอาจเป็น I/O bottleneck
