# 🎙️ Ron Dictate

**พูดแล้วพิมพ์ (Thai-first local dictation for Mac)** — จิ้มปุ่ม ⌥ พูด แล้วข้อความพิมพ์ลงตรงเคอร์เซอร์ในแอปไหนก็ได้
ใช้ Whisper large-v3-turbo รันในเครื่อง 100% (Apple Silicon = GPU ผ่าน MLX) · **ฟรี ไม่จำกัดคำ ไม่มีรายเดือน เสียงไม่ออกจากเครื่อง**

by [Ronny BBA](https://github.com/RonnyBBA)

## ติดตั้ง

1. โหลด zip จากหน้า [Releases](../../releases) แล้วแตกไฟล์
2. คลิกขวา `ติดตั้ง.command` → เปิด (Open) → รอจนขึ้น "🎉 ติดตั้งเสร็จ"
3. กด อนุญาต/Allow ทั้ง 3 จอ (ไมค์ / Input Monitoring / Accessibility) → ดับเบิลคลิก `เปิดใช้งาน.command`

อ่านละเอียด + แก้ปัญหา: [คู่มือ.md](คู่มือ.md)

## ใช้ยังไง

**ดับเบิลจิ้ม ⌥ Option ซ้าย** (2 ทีเร็วๆ กันเผลอกด) → พูดยาวแค่ไหนก็ได้ ข้อความทยอยขึ้นระหว่างพูด → จิ้มทีเดียวเมื่อจบ

- วัดจริงบน MacBook Air (M-chip): เสียงพูด 11 วินาที ถอดเสร็จใน ~1.6 วินาที
- ไทยปนอังกฤษได้ · เติมศัพท์เฉพาะตัวเองได้ใน `config.json`
- Mac ชิป M1-M4 (เร็วสุด) หรือ Intel (ช้ากว่า) · macOS 13+

## เบื้องหลัง

`ติดตั้ง.command` จัดการทุกอย่างเอง: Python สแตนด์อโลน (ผ่าน [uv](https://github.com/astral-sh/uv) — ไม่ต้องลง Xcode) → ไลบรารี ([mlx-whisper](https://github.com/ml-explore/mlx-examples) / [faster-whisper](https://github.com/SYSTRAN/faster-whisper), pynput, rumps) → ffmpeg static → โมเดล turbo (~1.6GB)

## License

MIT — ใช้/แก้/แจกต่อได้ ขอแค่คงเครดิต
