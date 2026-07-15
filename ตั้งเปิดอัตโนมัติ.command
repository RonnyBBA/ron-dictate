#!/bin/bash
# ดับเบิลคลิกไฟล์นี้ 1 ครั้ง = Ron Dictate จะเปิดเองทุกครั้งที่เปิดเครื่อง/ล็อกอิน (ตลอดไป)
cd "$(dirname "$0")"
PLIST=~/Library/LaunchAgents/com.rondictate.app.plist
mkdir -p ~/Library/LaunchAgents
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.rondictate.app</string>
    <key>ProgramArguments</key>
    <array><string>/Users/ronaeng/Documents/ron-dictate/.venv/bin/python</string><string>$PWD/dictate.py</string></array>
    <key>WorkingDirectory</key><string>$PWD</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>StandardOutPath</key><string>$PWD/dictate.log</string>
    <key>StandardErrorPath</key><string>$PWD/dictate.log</string>
</dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null
pkill -f "dictate.py" 2>/dev/null
sleep 1
launchctl load "$PLIST"
echo ""
echo "✅ เสร็จแล้ว! ตั้งแต่นี้เปิดเครื่องปุ๊บ ไอคอน 💤 จะโผล่บนเมนูบาร์เอง"
echo "   ไม่ต้องเปิดเอง ไม่ต้องสั่งใครอีก — ปิดหน้าต่างนี้ได้เลย"
read -p "กด Enter เพื่อปิด"
