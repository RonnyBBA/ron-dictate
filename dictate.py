#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ron Dictate — พูดแล้วพิมพ์ ในเครื่องคุณเอง ไม่จำกัดคำ ไม่มีรายเดือน
by Ronny BBA · https://github.com/RonnyBBA/ron-dictate

จิ้ม ⌥ Option ซ้าย 1 ที = เริ่มอัด (ปล่อยมือได้) · จิ้มอีกที = ถอดเสียง → วางข้อความตรงเคอร์เซอร์
สถานะบนเมนูบาร์: 💤 พร้อมใช้ · 🎙️ กำลังอัด · ⏳ กำลังโหลด/ถอด · 🚫 พักอยู่
"""

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

import rumps
from pynput import keyboard

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "dictate.log")

IS_APPLE_SILICON = platform.machine() == "arm64"


def find_ffmpeg():
    local = os.path.join(BASE_DIR, "bin", "ffmpeg")
    if os.path.exists(local):
        return local
    return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()
if FFMPEG:
    # mlx_whisper เรียก `ffmpeg` จาก PATH
    os.environ["PATH"] = os.path.dirname(FFMPEG) + os.pathsep + os.environ.get("PATH", "")

SOUND_START = "/System/Library/Sounds/Pop.aiff"
SOUND_STOP = "/System/Library/Sounds/Bottle.aiff"
SOUND_DONE = "/System/Library/Sounds/Glass.aiff"
SOUND_ERROR = "/System/Library/Sounds/Basso.aiff"

# preset: ชื่อในเมนู → (เอนจิน, โมเดล)
# ชิป M ใช้ GPU (mlx) เร็วกว่า CPU ~4-10 เท่า · เครื่อง Intel ใช้ faster-whisper CPU
if IS_APPLE_SILICON:
    # วัดจริง (เสียงไทย 11 วิ): turbo 1.5s · large-v3 4bit 2.5s (แม่น≈ตัวเต็ม แรม 1/3)
    PRESETS = {
        "turbo": ("mlx", "mlx-community/whisper-large-v3-turbo"),
        "large-v3": ("mlx", "mlx-community/whisper-large-v3-mlx-4bit"),
    }
else:
    PRESETS = {
        "turbo": ("faster", "large-v3-turbo"),
        "large-v3": ("faster", "large-v3"),
    }

DEFAULT_CONFIG = {
    "preset": "turbo",
    "language": "th",
    "hotkey": "alt_l",
    "mic_substring": "MacBook",
    "vocab": [],
    "corrections": {},
}


def log(msg):
    # เขียนลงไฟล์อย่างเดียว — stdout ถูก redirect มาไฟล์เดียวกัน ถ้า print ด้วยจะซ้ำ
    line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def play(sound):
    subprocess.Popen(["afplay", sound])


def check_permissions():
    """เช็ค + เด้งขอ permission ที่ต้องใช้ (Input Monitoring + Accessibility) — คืน True ถ้าครบ"""
    ok = True
    try:
        import Quartz
        if not Quartz.CGPreflightListenEventAccess():
            ok = False
            log("⚠️ ยังไม่ได้อนุญาต Input Monitoring — เด้งขอแล้ว กด Allow แล้วเปิดแอปใหม่")
            Quartz.CGRequestListenEventAccess()
    except Exception:
        log("เช็ค Input Monitoring ไม่ได้:\n" + traceback.format_exc())
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
            ok = False
            log("⚠️ ยังไม่ได้อนุญาต Accessibility — เด้งขอแล้ว กด Allow แล้วเปิดแอปใหม่")
    except Exception:
        log("เช็ค Accessibility ไม่ได้:\n" + traceback.format_exc())
    return ok


def find_mic(substring):
    """ถาม ffmpeg ว่ามีไมค์อะไรบ้าง เลือกตัวที่ชื่อตรง substring (ไม่เจอ = ตัวแรก)"""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    devices = []
    in_audio = False
    for line in (proc.stderr or "").splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if in_audio and "] [" in line:
            name = line.split("] ", 2)[-1].strip()
            devices.append(name)
    if not devices:
        return None
    for name in devices:
        if substring.lower() in name.lower():
            return name
    return devices[0]


class Dictate(rumps.App):
    def __init__(self):
        super(Dictate, self).__init__("⏳", quit_button=None)
        self.cfg = load_config()
        self.state = "loading"  # loading | idle | recording | transcribing | paused
        self.model = None
        self.preset = self.cfg.get("preset", "turbo")
        if self.preset not in PRESETS:
            self.preset = "turbo"
        self.rec_proc = None
        self.rec_path = None
        self.mic = None
        self.kb = keyboard.Controller()

        self.item_status = rumps.MenuItem("กำลังโหลดโมเดล...")
        self.item_pause = rumps.MenuItem("พักการใช้งาน", callback=self.toggle_pause)
        self.item_turbo = rumps.MenuItem("โมเดล: turbo (เร็ว+แม่น · แนะนำ)", callback=lambda _: self.switch_model("turbo"))
        self.item_large = rumps.MenuItem("โมเดล: large-v3 (แม่นสุด · ช้ากว่ามาก)", callback=lambda _: self.switch_model("large-v3"))
        self.item_about = rumps.MenuItem("Ron Dictate — by Ronny BBA", callback=self.open_about)
        self.menu = [
            self.item_status, None,
            self.item_pause, None,
            self.item_turbo, self.item_large, None,
            self.item_about,
            rumps.MenuItem("Quit", callback=self.do_quit),
        ]
        self.refresh_menu()

        rumps.Timer(self.update_title, 0.25).start()
        threading.Thread(target=self.load_model, daemon=True).start()
        threading.Thread(target=self.hotkey_listener, daemon=True).start()

    # ---------- UI ----------

    def update_title(self, _timer=None):
        icons = {"loading": "⏳", "idle": "💤", "recording": "🎙️",
                 "transcribing": "⏳", "paused": "🚫"}
        want = icons.get(self.state, "💤")
        if self.title != want:
            self.title = want

    def refresh_menu(self):
        check = lambda active: "✅ " if active else ""
        self.item_turbo.title = check(self.preset == "turbo") + "โมเดล: turbo (เร็ว+แม่น · แนะนำ)"
        self.item_large.title = check(self.preset == "large-v3") + "โมเดล: large-v3 (แม่นสุด · ช้ากว่ามาก)"
        self.item_pause.title = "เปิดใช้งานต่อ" if self.state == "paused" else "พักการใช้งาน"

    def open_about(self, _):
        subprocess.Popen(["open", "https://github.com/RonnyBBA/ron-dictate"])

    # ---------- Model ----------

    def load_model(self):
        if not FFMPEG:
            self.item_status.title = "❌ ไม่พบ ffmpeg — รัน ติดตั้ง.command ก่อน"
            log("ไม่พบ ffmpeg (bin/ffmpeg หรือใน PATH)")
            play(SOUND_ERROR)
            return
        engine, model_id = PRESETS[self.preset]
        try:
            log("โหลดโมเดล %s (%s) ..." % (self.preset, engine))
            self.item_status.title = "กำลังโหลดโมเดล %s ..." % self.preset
            t0 = time.time()
            if engine == "mlx":
                import mlx_whisper
                # อุ่นเครื่อง: ถอดเสียงเงียบสั้นๆ ให้โมเดลขึ้น GPU ค้างไว้
                silence = tempfile.mktemp(suffix=".wav", prefix="ron_dictate_warm_")
                subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                "-t", "0.3", silence], check=True)
                mlx_whisper.transcribe(silence, path_or_hf_repo=model_id,
                                       language=self.cfg.get("language", "th"))
                os.remove(silence)
                self.model = ("mlx", model_id)
            else:
                from faster_whisper import WhisperModel
                m = WhisperModel(model_id, device="cpu", compute_type="int8",
                                 cpu_threads=os.cpu_count())
                self.model = ("faster", m)
            log("โมเดลพร้อม (%.1fs)" % (time.time() - t0))
            self.mic = find_mic(self.cfg.get("mic_substring", "MacBook"))
            log("ใช้ไมค์: %s" % self.mic)
            self.item_status.title = "พร้อมใช้ — จิ้ม ⌥ ซ้าย เพื่อพูด"
            if self.state == "loading":
                self.state = "idle"
        except Exception:
            log("โหลดโมเดลพัง:\n" + traceback.format_exc())
            self.item_status.title = "❌ โหลดโมเดลไม่สำเร็จ (ดู dictate.log)"
            play(SOUND_ERROR)

    def switch_model(self, preset):
        if preset == self.preset or self.state in ("recording", "transcribing"):
            return
        self.preset = preset
        self.cfg["preset"] = preset
        save_config(self.cfg)
        self.model = None
        self.state = "loading"
        self.refresh_menu()
        threading.Thread(target=self.load_model, daemon=True).start()

    # ---------- Hotkey ----------

    def hotkey_listener(self):
        try:
            if not check_permissions():
                self.item_status.title = "⚠️ กด Allow ใน System Settings แล้วเปิดแอปใหม่"
            hotkey = getattr(keyboard.Key, self.cfg.get("hotkey", "alt_l"), keyboard.Key.alt_l)
            # "จิ้มเดี่ยว" เท่านั้นถึงทำงาน — กด ⌥ ประกอบปุ่มอื่น (⌥+ลูกศร ฯลฯ) ไม่นับ
            held = {"down": False, "combo": False}

            def on_press(key):
                if key == hotkey:
                    held["down"] = True
                    held["combo"] = False
                elif held["down"]:
                    held["combo"] = True

            def on_release(key):
                if key == hotkey:
                    was_combo = held["combo"]
                    held["down"] = False
                    if not was_combo:
                        log("จับการจิ้มปุ่มลัด (สถานะ: %s)" % self.state)
                        self.on_hotkey()

            with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                listener.join()
        except Exception:
            log("ตัวฟังปุ่มลัดพัง:\n" + traceback.format_exc())
            self.item_status.title = "❌ ปุ่มลัดใช้ไม่ได้ (ดู dictate.log)"

    def on_hotkey(self):
        if self.state == "idle":
            self.start_recording()
        elif self.state == "recording":
            threading.Thread(target=self.stop_and_transcribe, daemon=True).start()

    def toggle_pause(self, _):
        if self.state == "paused":
            self.state = "idle"
        elif self.state == "idle":
            self.state = "paused"
        self.refresh_menu()

    # ---------- Record ----------

    def start_recording(self):
        if not self.mic:
            self.mic = find_mic(self.cfg.get("mic_substring", "MacBook"))
            if not self.mic:
                self.item_status.title = "❌ หาไมค์ไม่เจอ"
                play(SOUND_ERROR)
                return
        self.rec_path = tempfile.mktemp(suffix=".wav", prefix="ron_dictate_")
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "avfoundation", "-i", ":" + self.mic,
               "-ar", "16000", "-ac", "1", self.rec_path]
        self.rec_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.state = "recording"
        log("เริ่มอัด → %s" % self.rec_path)
        play(SOUND_START)

    def stop_and_transcribe(self):
        self.state = "transcribing"
        play(SOUND_STOP)
        proc, path = self.rec_proc, self.rec_path
        self.rec_proc = self.rec_path = None
        try:
            proc.send_signal(signal.SIGINT)  # ให้ ffmpeg ปิดไฟล์ wav ให้เรียบร้อย
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            text, secs = self.transcribe(path)
            if text:
                self.paste_text(text)
                log("ถอดเสร็จ %.1fs → %d ตัวอักษร: %s" % (secs, len(text), text[:120]))
                self.item_status.title = "ล่าสุด: %s" % (text[:40] + ("…" if len(text) > 40 else ""))
                play(SOUND_DONE)
            else:
                log("ไม่ได้ยินเสียงพูด (ผลว่าง)")
                self.item_status.title = "ไม่ได้ยินเสียงพูด — ลองใหม่"
                play(SOUND_ERROR)
        except Exception:
            log("ถอดเสียงพัง:\n" + traceback.format_exc())
            self.item_status.title = "❌ ถอดเสียงพัง (ดู dictate.log)"
            play(SOUND_ERROR)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass
            self.state = "idle"

    def transcribe(self, path):
        t0 = time.time()
        engine, m = self.model
        lang = self.cfg.get("language", "th")
        # ป้อนคลังศัพท์เฉพาะของคุณ ให้โมเดลเดาคำพวกนี้ถูกขึ้น — เติมได้ใน config.json
        vocab = self.cfg.get("vocab") or []
        prompt = ("ศัพท์เฉพาะที่ใช้บ่อย: " + ", ".join(vocab)) if vocab else None
        if engine == "mlx":
            import mlx_whisper
            r = mlx_whisper.transcribe(path, path_or_hf_repo=m, language=lang,
                                       initial_prompt=prompt)
            text = r["text"].strip()
        else:
            segments, _info = m.transcribe(path, language=lang, beam_size=5,
                                           vad_filter=True, initial_prompt=prompt)
            text = "".join(seg.text for seg in segments).strip()
        # แก้คำที่ถอดเพี้ยนประจำ ตามตาราง corrections ใน config
        for wrong, right in (self.cfg.get("corrections") or {}).items():
            text = text.replace(wrong, right)
        return text, time.time() - t0

    # ---------- Paste ----------

    def paste_text(self, text):
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
        time.sleep(0.15)
        # ยิงปุ่ม V ตามตำแหน่งฮาร์ดแวร์ (vk=9) — คีย์บอร์ดภาษาไทย/อะไรก็วางเข้า
        v_key = keyboard.KeyCode.from_vk(9)
        with self.kb.pressed(keyboard.Key.cmd):
            self.kb.press(v_key)
            self.kb.release(v_key)
        # ข้อความค้างใน clipboard — วางซ้ำเองด้วย Cmd+V ได้เสมอ

    def do_quit(self, _):
        if self.rec_proc:
            self.rec_proc.kill()
        rumps.quit_application()


if __name__ == "__main__":
    log("=== Ron Dictate เริ่มทำงาน (%s) ===" % ("Apple Silicon" if IS_APPLE_SILICON else "Intel"))
    Dictate().run()
