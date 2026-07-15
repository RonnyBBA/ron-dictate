#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ron Dictate — พูดแล้วพิมพ์ ในเครื่องคุณเอง ไม่จำกัดคำ ไม่มีรายเดือน
by Ronny BBA · https://github.com/RonnyBBA/ron-dictate

จิ้ม ⌥ Option ซ้าย 1 ที = เริ่มอัด · จิ้มอีกที = หยุด · จิ้มตอน ⏳ = ยกเลิก · มีเสียงตอบทุกจิ้ม → วางข้อความตรงเคอร์เซอร์
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
    # เขียนลงไฟล์อย่างเดียว — stdout ของ daemon ถูก redirect มาไฟล์เดียวกัน ถ้า print ด้วยจะซ้ำ
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


def wav_is_silent(path, rms_threshold=250):
    """เช็คว่าไฟล์ wav เป็นความเงียบ/เสียงจอแจเบาๆ ไหม — เงียบ = ไม่ต้องถอดเลย (กัน Whisper มโนคำ)"""
    try:
        import wave, audioop
        with wave.open(path) as w:
            frames = w.readframes(w.getnframes())
        return audioop.rms(frames, 2) < rms_threshold
    except Exception:
        return False  # เช็คไม่ได้ = ปล่อยผ่านไปถอดตามปกติ


def looks_hallucinated(text):
    """จับอาการ Whisper มโนจากความเงียบ — ทั้งคำซ้ำ ('Mess Mess ...') และอักษรติดกันยาว ('ทททท...')"""
    words = text.split()
    if len(words) >= 5 and len(set(words)) <= 2:
        return True
    for i in range(len(words) - 4):
        if len(set(words[i:i + 5])) == 1:
            return True
    # ตัวอักษรเดียวกันติดกัน ≥8 ตัว (ทททททททท / ๆๆๆๆๆๆๆๆ) = มโนแน่นอน
    run, prev = 1, ""
    for ch in text:
        run = run + 1 if ch == prev else 1
        if run >= 8:
            return True
        prev = ch
    return False


def find_mic(substring):
    """ถาม ffmpeg ว่ามีไมค์อะไรบ้าง แล้วเลือกตัวที่ชื่อตรง substring (fallback: ตัวแรก)"""
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
        self.seg_dir = None
        self.mic = None
        self.last_tap = 0.0
        self.cancel = False
        self.kb = keyboard.Controller()

        self.item_status = rumps.MenuItem("กำลังโหลดโมเดล...")
        self.item_pause = rumps.MenuItem("พักการใช้งาน", callback=self.toggle_pause)
        self.item_turbo = rumps.MenuItem("โมเดล: turbo (เร็วสุด)", callback=lambda _: self.switch_model("turbo"))
        self.item_large = rumps.MenuItem("โมเดล: large-v3 (แม่นสุด · แนะนำ)", callback=lambda _: self.switch_model("large-v3"))
        self.item_stream = rumps.MenuItem("ทยอยพิมพ์ระหว่างพูด", callback=self.toggle_streaming)
        self.menu = [
            self.item_status, None,
            self.item_pause, self.item_stream, None,
            self.item_turbo, self.item_large, None,
            rumps.MenuItem("Quit", callback=self.do_quit),
        ]
        self.refresh_menu()

        rumps.Timer(self.update_title, 0.25).start()
        rumps.Timer(self.keep_warm, 300).start()  # กันโมเดลโดนเตะไป swap ตอนเครื่องแรมตึง
        threading.Thread(target=self.load_model, daemon=True).start()
        threading.Thread(target=self.hotkey_listener, daemon=True).start()

    def keep_warm(self, _timer=None):
        if self.state != "idle" or not self.model:
            return
        threading.Thread(target=self._warm_once, daemon=True).start()

    def _warm_once(self):
        try:
            engine, m = self.model
            if engine != "mlx" or self.state != "idle":
                return
            import mlx_whisper
            silence = tempfile.mktemp(suffix=".wav", prefix="ron_dictate_warm_")
            subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                            "-t", "0.3", silence], check=True)
            mlx_whisper.transcribe(silence, path_or_hf_repo=m, language="th")
            os.remove(silence)
        except Exception:
            pass

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
        self.item_stream.title = ("✅ " if self.streaming_on() else "") + "ทยอยพิมพ์ระหว่างพูด"

    def toggle_streaming(self, _):
        self.cfg["streaming"] = not self.streaming_on()
        save_config(self.cfg)
        self.refresh_menu()

    # ---------- Model ----------

    def load_model(self):
        engine, model_id = PRESETS[self.preset]
        try:
            log("โหลดโมเดล %s (%s) ..." % (self.preset, engine))
            self.item_status.title = "กำลังโหลดโมเดล %s ..." % self.preset
            t0 = time.time()
            if engine == "mlx":
                import mlx_whisper
                # อุ่นเครื่อง: ถอดเสียงเงียบสั้นๆ 1 ครั้ง ให้โมเดลขึ้น GPU ค้างไว้
                silence = tempfile.mktemp(suffix=".wav", prefix="ron_dictate_warm_")
                subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                "-t", "0.3", silence], check=True)
                mlx_whisper.transcribe(silence, path_or_hf_repo=model_id, language="th")
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
            # "จิ้มเดี่ยว" เท่านั้นถึงทำงาน — ถ้ากด ⌥ ประกอบปุ่มอื่น (⌥+ลูกศร ฯลฯ) ไม่นับ
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
        # จิ้มทีเดียว = เริ่ม/หยุด (เผลอจิ้มไม่มีพิษแล้ว: ท่อนเงียบถูกข้าม + เงียบนานหยุดเอง)
        if self.state == "idle":
            self.start_recording()
        elif self.state == "recording":
            if self.streaming_on():
                # โหมดทยอย: แค่เปลี่ยนสถานะ stream_worker จะปิดงานเอง
                self.state = "transcribing"
                play(SOUND_STOP)
            else:
                threading.Thread(target=self.stop_and_transcribe, daemon=True).start()
        elif self.state == "transcribing":
            # ปุ่มฉุกเฉิน: จิ้มระหว่าง ⏳ = ยกเลิกทั้งหมด หยุดวางทันที
            self.cancel = True
            log("ผู้ใช้สั่งยกเลิก (จิ้มระหว่างถอด)")
            play(SOUND_ERROR)
        else:
            # loading/paused — ห้ามเงียบ ต้องมีเสียงบอกว่ายังไม่พร้อม
            log("จิ้มตอนยังไม่พร้อม (สถานะ: %s)" % self.state)
            play(SOUND_ERROR)

    def streaming_on(self):
        return bool(self.cfg.get("streaming", True))

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
        self.cancel = False
        if self.streaming_on():
            # โหมดทยอย: อัดหั่นเป็นท่อนละ 6 วิ ถอด+วางระหว่างพูดเลย
            self.seg_dir = tempfile.mkdtemp(prefix="ron_dictate_seg_")
            cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                   "-f", "avfoundation", "-i", ":" + self.mic,
                   "-ar", "16000", "-ac", "1",
                   "-f", "segment", "-segment_time", "5", "-reset_timestamps", "1",
                   os.path.join(self.seg_dir, "seg_%03d.wav")]
            self.rec_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.state = "recording"
            log("เริ่มอัด (ทยอย) → %s" % self.seg_dir)
            play(SOUND_START)
            threading.Thread(target=self.stream_worker, daemon=True).start()
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

    def stream_worker(self):
        """ถอด+วางทีละท่อนระหว่างที่ยังพูดอยู่ — พูดจบเหลือรอแค่ท่อนสุดท้าย
        กันภัย 4 ชั้น: ท่อนเงียบไม่ถอด · คำมโนซ้ำไม่วาง · เงียบนานหยุดเอง · จิ้มยกเลิกได้"""
        import glob as globmod
        proc, seg_dir = self.rec_proc, self.seg_dir
        done, text_all, ffmpeg_closed = set(), "", False
        silent_run, t_stop = 0, None
        try:
            while True:
                if self.cancel:
                    log("ยกเลิกแล้ว — ทิ้งท่อนที่เหลือทั้งหมด")
                    break
                if self.state != "recording" and not ffmpeg_closed:
                    t_stop = time.time()
                    try:
                        proc.send_signal(signal.SIGINT)
                        proc.wait(timeout=10)
                    except Exception:
                        proc.kill()
                    ffmpeg_closed = True
                files = sorted(globmod.glob(os.path.join(seg_dir, "seg_*.wav")))
                ready = files if ffmpeg_closed else files[:-1]  # ตัวท้ายยังเขียนอยู่
                for f in ready:
                    if f in done or self.cancel:
                        continue
                    done.add(f)
                    try:
                        if os.path.getsize(f) < 26000 and ffmpeg_closed and f == files[-1] and text_all:
                            continue  # เศษท้ายสั้นเกิน ข้าม
                        if wav_is_silent(f, self.cfg.get("silence_rms", 250)):
                            silent_run += 1
                            log("ท่อน %s ข้าม (เงียบ) · เงียบติดกัน %d" % (os.path.basename(f), silent_run))
                            continue
                        txt, secs = self.transcribe(f, prev=text_all)
                        if txt and looks_hallucinated(txt):
                            log("ท่อน %s ทิ้ง (มโนซ้ำ): %s" % (os.path.basename(f), txt[:40]))
                            continue
                        if txt:
                            silent_run = 0
                            self.paste_text((" " if text_all else "") + txt)
                            text_all += (" " if text_all else "") + txt
                            log("ท่อน %s ถอด %.1fs → %s" % (os.path.basename(f), secs, txt[:60]))
                        else:
                            silent_run += 1
                    except Exception:
                        log("ถอดท่อนพัง:\n" + traceback.format_exc())
                # เงียบต่อเนื่อง ~20 วิ (3 ท่อน) = คงเผลอเปิดค้าง → หยุดเอง
                if self.state == "recording" and silent_run >= 3:
                    log("เงียบนานเกิน — หยุดอัดอัตโนมัติ")
                    self.item_status.title = "หยุดเอง (ไม่ได้ยินเสียงพูดนาน)"
                    self.state = "transcribing"
                    play(SOUND_STOP)
                if ffmpeg_closed and len(done) == len(globmod.glob(os.path.join(seg_dir, "seg_*.wav"))):
                    break
                time.sleep(0.4)
            if self.cancel:
                self.item_status.title = "ยกเลิกแล้ว"
            elif text_all:
                wait = (time.time() - t_stop) if t_stop else 0
                log("จบ (ทยอย) รวม %d ตัวอักษร · รอหลังกดหยุด %.1fs" % (len(text_all), wait))
                self.item_status.title = "ล่าสุด: %s" % (text_all[:40] + ("…" if len(text_all) > 40 else ""))
                play(SOUND_DONE)
            else:
                log("ไม่ได้ยินเสียงพูด (ทยอย)")
                self.item_status.title = "ไม่ได้ยินเสียงพูด — ลองใหม่"
                play(SOUND_ERROR)
        finally:
            if not ffmpeg_closed:
                try:
                    proc.kill()
                except Exception:
                    pass
            shutil.rmtree(seg_dir, ignore_errors=True)
            self.rec_proc = None
            self.cancel = False
            self.state = "idle"

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
        # จิ้มติดกันเร็วเกิน (อัด < 0.8 วิ) = ไม่ถอด — กัน Whisper มโนคำจากความเงียบ
        try:
            if os.path.getsize(path) < 26000:  # 16kHz mono 16bit ≈ 32KB/วิ
                log("อัดสั้นเกิน (จิ้มพลาด) — ข้าม")
                os.remove(path)
                self.state = "idle"
                return
        except OSError:
            pass
        try:
            if wav_is_silent(path, self.cfg.get("silence_rms", 250)):
                log("ข้าม (เงียบทั้งคลิป)")
                self.item_status.title = "ไม่ได้ยินเสียงพูด — ลองใหม่"
                play(SOUND_ERROR)
                return
            text, secs = self.transcribe(path)
            if text and looks_hallucinated(text):
                log("ทิ้ง (มโนซ้ำ): %s" % text[:40])
                text = ""
            if self.cancel:
                log("ยกเลิกแล้ว — ไม่วางข้อความ")
                text = ""
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

    def transcribe(self, path, prev=""):
        t0 = time.time()
        engine, m = self.model
        lang = self.cfg.get("language", "th")
        # ป้อนคลังศัพท์เฉพาะของรอน + ข้อความท่อนก่อนหน้า (โหมดทยอย) ให้ต่อเนื่องถูกบริบท
        vocab = self.cfg.get("vocab") or []
        parts = []
        if vocab:
            parts.append("ศัพท์เฉพาะที่ใช้บ่อย: " + ", ".join(vocab))
        if prev:
            parts.append("ข้อความก่อนหน้า: " + prev[-150:])
        prompt = " · ".join(parts) if parts else None
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
        # ยิงปุ่ม V ตามตำแหน่งฮาร์ดแวร์ (vk=9) — คีย์บอร์ดภาษาไทยอยู่ก็วางเข้า
        v_key = keyboard.KeyCode.from_vk(9)
        with self.kb.pressed(keyboard.Key.cmd):
            self.kb.press(v_key)
            self.kb.release(v_key)
        # ไม่คืน clipboard เดิม — ข้อความค้างใน clipboard เผื่อวางซ้ำเองด้วย Cmd+V ได้

    def do_quit(self, _):
        if self.rec_proc:
            self.rec_proc.kill()
        rumps.quit_application()


if __name__ == "__main__":
    log("=== Ron Dictate เริ่มทำงาน ===")
    Dictate().run()
