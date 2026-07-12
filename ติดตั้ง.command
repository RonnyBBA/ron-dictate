#!/bin/bash
# 🎙️ Ron Dictate — ตัวติดตั้ง (ดับเบิลคลิกครั้งเดียว รอจนขึ้น "ติดตั้งเสร็จ")
# ทำเองทั้งหมด: Python + ไลบรารี + ffmpeg + โมเดลถอดเสียง — ไม่ต้องลงอะไรมาก่อนเลย
set -e
cd "$(dirname "$0")"

ARCH=$(uname -m)
echo "======================================"
echo "🎙️  Ron Dictate — เริ่มติดตั้ง"
echo "======================================"
if [ "$(uname)" != "Darwin" ]; then
  echo "❌ ตัวนี้ใช้ได้เฉพาะ Mac ครับ"; read -p "กด Enter เพื่อปิด"; exit 1
fi
if [ "$ARCH" = "arm64" ]; then
  echo "✅ เครื่องชิป Apple Silicon (M1-M4) — ได้ความเร็ว GPU เต็ม"
else
  echo "✅ เครื่อง Intel Mac — ใช้ได้ (ถอดเสียงช้ากว่าชิป M หน่อย)"
fi
echo ""

# ---------- 1/5 Python สแตนด์อโลน (ผ่าน uv — ไม่ต้องลง Xcode) ----------
echo "⏳ 1/5 เตรียม Python..."
export UV_INSTALL_DIR="$PWD/runtime/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/runtime/python"
export UV_CACHE_DIR="$PWD/runtime/cache"
mkdir -p runtime
if [ ! -x "runtime/uv/uv" ]; then
  curl -LsSf https://astral.sh/uv/install.sh | INSTALLER_NO_MODIFY_PATH=1 sh -s -- --quiet
fi
UV="$PWD/runtime/uv/uv"
"$UV" python install 3.12 --quiet || true
if [ ! -d ".venv" ]; then
  "$UV" venv --python 3.12 --quiet
fi

# ---------- 2/5 ไลบรารี ----------
echo "⏳ 2/5 ลงไลบรารี (2-3 นาที ขึ้นกับเน็ต)..."
if [ "$ARCH" = "arm64" ]; then
  "$UV" pip install --python .venv/bin/python --quiet mlx-whisper faster-whisper pynput rumps
else
  "$UV" pip install --python .venv/bin/python --quiet faster-whisper pynput rumps
fi

# ---------- 3/5 ffmpeg (ตัวอัดเสียงจากไมค์) ----------
echo "⏳ 3/5 โหลด ffmpeg..."
mkdir -p bin
if [ ! -x "bin/ffmpeg" ]; then
  if [ "$ARCH" = "arm64" ]; then
    FFURL="https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip"
  else
    FFURL="https://ffmpeg.martin-riedl.de/redirect/latest/macos/amd64/release/ffmpeg.zip"
  fi
  curl -Ls "$FFURL" -o bin/_ff.zip
  (cd bin && unzip -oq _ff.zip && rm -f _ff.zip && chmod +x ffmpeg)
  xattr -d com.apple.quarantine bin/ffmpeg 2>/dev/null || true
fi

# ---------- 4/5 โมเดลถอดเสียง (~1.6GB ครั้งเดียว) ----------
echo "⏳ 4/5 โหลดโมเดลถอดเสียง (~1.6GB — ครั้งแรกครั้งเดียว รอหน่อยนะครับ)..."
if [ "$ARCH" = "arm64" ]; then
  .venv/bin/python - <<'EOF'
from huggingface_hub import snapshot_download
snapshot_download("mlx-community/whisper-large-v3-turbo")
print("✅ โมเดลพร้อม")
EOF
else
  .venv/bin/python - <<'EOF'
from faster_whisper import WhisperModel
WhisperModel("large-v3-turbo", device="cpu", compute_type="int8")
print("✅ โมเดลพร้อม")
EOF
fi

# ---------- 5/5 ถามเรื่องเปิดเอง + เปิดแอป ----------
echo "⏳ 5/5 ตั้งค่าสุดท้าย..."
AUTOSTART=$(osascript -e 'button returned of (display dialog "ให้ Ron Dictate เปิดเองทุกครั้งที่เปิดเครื่องไหม?" buttons {"ไม่ต้อง", "เปิดเองเลย"} default button "เปิดเองเลย" with title "Ron Dictate")' 2>/dev/null || echo "ไม่ต้อง")
if [ "$AUTOSTART" = "เปิดเองเลย" ]; then
  PLIST=~/Library/LaunchAgents/com.rondictate.app.plist
  mkdir -p ~/Library/LaunchAgents
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.rondictate.app</string>
    <key>ProgramArguments</key>
    <array><string>$PWD/.venv/bin/python</string><string>$PWD/dictate.py</string></array>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$PWD/dictate.log</string>
    <key>StandardErrorPath</key><string>$PWD/dictate.log</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "✅ ตั้งเปิดเองตอนเปิดเครื่องแล้ว"
fi

pkill -f "$PWD/dictate.py" 2>/dev/null || true
sleep 0.5
nohup .venv/bin/python dictate.py >> dictate.log 2>&1 &

echo ""
echo "======================================"
echo "🎉 ติดตั้งเสร็จ! ดูไอคอนบนเมนูบาร์ (⏳ กำลังโหลด → 💤 พร้อมใช้)"
echo ""
echo "⚠️  สำคัญ: macOS จะเด้งขออนุญาต 2-3 จอ (ไมค์ / การช่วยการเข้าถึง / การกดแป้นพิมพ์)"
echo "   กด อนุญาต/Allow ให้ครบทุกจอ แล้วดับเบิลคลิก 'เปิดใช้งาน.command' อีก 1 รอบ"
echo "   (อ่านภาพประกอบใน คู่มือ.md)"
echo "======================================"
echo ""
echo "วิธีใช้: จิ้ม ⌥ Option ซ้าย 1 ที → พูด → จิ้มอีกที → ข้อความพิมพ์ให้เอง"
read -p "กด Enter เพื่อปิดหน้าต่างนี้"
