#!/bin/bash
# ดับเบิลคลิก = เปิด Ron Dictate (ไอคอนโผล่บนเมนูบาร์ · ⏳ โหลด ~20 วิ → 💤 พร้อมใช้)
cd "$(dirname "$0")"
if [ ! -x ".venv/bin/python" ]; then
  echo "❌ ยังไม่ได้ติดตั้ง — ดับเบิลคลิก 'ติดตั้ง.command' ก่อนครับ"
  read -p "กด Enter เพื่อปิด"; exit 1
fi
pkill -f "$PWD/dictate.py" 2>/dev/null
sleep 0.5
nohup .venv/bin/python dictate.py >> dictate.log 2>&1 &
echo "🎙️ Ron Dictate เปิดแล้ว — ดูไอคอนบนเมนูบาร์"
echo "จิ้ม ⌥ Option ซ้าย 1 ที → พูด → จิ้มอีกที → ข้อความพิมพ์ให้เอง"
echo "ปิดหน้าต่างนี้ได้เลย"
