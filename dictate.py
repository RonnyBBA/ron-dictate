#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Ron Dictate v3 — พูดแล้วพิมพ์ ในเครื่อง ไม่จำกัดคำ
จิ้ม ⌥ ซ้าย = เริ่ม (ป๊อป) · จิ้มอีกที = หยุด (ตุ๊บ) · จิ้มตอน ⏳ = แค่บอกว่ากำลังถอด · ยกเลิก = เมนูบาร์
สถาปัตยกรรม: ทุกรอบอัดมี "เลขรุ่น" (session token) — ข้อความจะวางได้ต่อเมื่อเลขรุ่นยังตรง ณ วินาทีวาง
กันมโน: ใช้ค่าความมั่นใจของ Whisper เอง (no_speech_prob + compression_ratio) แทน blacklist
"""

import json
import os
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
TRIGGER_PATH = "/tmp/ron_dictate_trigger"  # ช่องเทสภายใน: เขียนคำว่า tap ลงไฟล์นี้ = เหมือนจิ้มปุ่ม
import platform
IS_APPLE_SILICON = platform.machine() == "arm64"


def find_ffmpeg():
    local = os.path.join(BASE_DIR, "bin", "ffmpeg")
    return local if os.path.exists(local) else shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()
if FFMPEG:
    os.environ["PATH"] = os.path.dirname(FFMPEG) + os.pathsep + os.environ.get("PATH", "")

SOUND_START = "/System/Library/Sounds/Pop.aiff"
SOUND_STOP = "/System/Library/Sounds/Bottle.aiff"
SOUND_DONE = "/System/Library/Sounds/Glass.aiff"
SOUND_ERROR = "/System/Library/Sounds/Basso.aiff"
SOUND_WAIT = "/System/Library/Sounds/Tink.aiff"

# วัดจริง: turbo = เร็วสุด · large-v3 4bit = แม่นกว่านิด ช้ากว่า ~2 เท่า
if IS_APPLE_SILICON:
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
    "streaming": False,
    "silence_rms": 250,
    "vocab": [],
    "corrections": {},
}


def log(msg):
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
    ok = True
    try:
        import Quartz
        if not Quartz.CGPreflightListenEventAccess():
            ok = False
            log("⚠️ ยังไม่ได้อนุญาต Input Monitoring — เด้งขอแล้ว")
            Quartz.CGRequestListenEventAccess()
    except Exception:
        log("เช็ค Input Monitoring ไม่ได้:\n" + traceback.format_exc())
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
        if not AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True}):
            ok = False
            log("⚠️ ยังไม่ได้อนุญาต Accessibility — เด้งขอแล้ว")
    except Exception:
        log("เช็ค Accessibility ไม่ได้:\n" + traceback.format_exc())
    return ok


def wav_is_silent(path, rms_threshold=250):
    """สำรอง: วัดความดังดิบ (ใช้เมื่อ VAD ใช้ไม่ได้)"""
    try:
        import wave, audioop
        with wave.open(path) as w:
            frames = w.readframes(w.getnframes())
        return audioop.rms(frames, 2) < rms_threshold
    except Exception:
        return False


def has_speech(path, rms_threshold=250):
    """ด่านกรองที่ 1: Silero VAD (ตัวเดียวกับที่แอปดังใช้ · 0.05 วิ/ท่อน)
    กันทั้งถอดความเงียบทิ้งเปล่าๆ และกัน Whisper นั่งมโนกับเสียงรบกวนนาน 20 วิ"""
    try:
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        from faster_whisper.audio import decode_audio
        audio = decode_audio(path, sampling_rate=16000)
        return len(get_speech_timestamps(audio, VadOptions(min_speech_duration_ms=250))) > 0
    except Exception:
        return not wav_is_silent(path, rms_threshold)


def looks_hallucinated(text):
    """ชั้นเสริม: คำ/อักษรซ้ำวน (Mess Mess · ทททท)"""
    words = text.split()
    if len(words) >= 5 and len(set(words)) <= 2:
        return True
    for i in range(len(words) - 4):
        if len(set(words[i:i + 5])) == 1:
            return True
    run, prev = 1, ""
    for ch in text:
        run = run + 1 if ch == prev else 1
        if run >= 8:
            return True
        prev = ch
    return False


def clean_segments(segments):
    """ชั้นกรองหลัก: ใช้ค่าความมั่นใจของ Whisper เอง — ท่อนไม่น่าเชื่อ = ทิ้งทั้งท่อน
    no_speech_prob สูง = ไม่ใช่เสียงพูด · compression_ratio สูง = ข้อความซ้ำวน (มโนทุกแบบ)"""
    kept, dropped = [], []
    for s in segments:
        get = (lambda k, d=0: s.get(k, d)) if isinstance(s, dict) else (lambda k, d=0: getattr(s, k, d))
        txt = (get("text", "") or "")
        why = None
        if get("no_speech_prob", 0) > 0.6:
            why = "no_speech %.2f" % get("no_speech_prob", 0)
        elif get("compression_ratio", 0) > 2.4:
            why = "ซ้ำวน %.2f" % get("compression_ratio", 0)
        elif txt.strip() and looks_hallucinated(txt.strip()):
            why = "มโนซ้ำ"
        if why:
            dropped.append("%s → %s" % (why, txt.strip()[:30]))
        elif txt.strip():
            kept.append(txt)
    if dropped:
        log("กรองทิ้ง %d ท่อนย่อย: %s" % (len(dropped), " | ".join(dropped[:3])))
    return "".join(kept).strip()


def find_mic(substring):
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True,
    )
    devices, in_audio = [], False
    for line in (proc.stderr or "").splitlines():
        if "AVFoundation audio devices" in line:
            in_audio = True
            continue
        if in_audio and "] [" in line:
            devices.append(line.split("] ", 2)[-1].strip())
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
        self.session = 0      # เลขรุ่นของรอบอัด — เพิ่มทุกครั้งที่เริ่มรอบใหม่
        self.stop_gen = 0     # รอบ ≤ ค่านี้ = ถูกสั่งหยุดอัดแล้ว
        self.cancel_gen = 0   # รอบ ≤ ค่านี้ = โมฆะ ห้ามวางข้อความ
        self.prev_done = None  # Event ของ worker รอบก่อน — ไว้เรียงลำดับการวางข้าม 2 รอบ
        self.gpu_lock = threading.Lock()  # mlx/Metal ห้ามยิงพร้อมกัน 2 เธรด (เคย crash ทั้งโปรเซส)
        self.rec_proc = None
        self.mic = None
        self.last_tap = 0.0
        self.want_start = False  # จิ้มตอนกำลังโหลด = จองคิว พอพร้อมเริ่มอัดให้เอง
        self.kb = keyboard.Controller()

        self.item_status = rumps.MenuItem("กำลังโหลดโมเดล...")
        self.item_cancel = rumps.MenuItem("🛑 ยกเลิกที่ค้างอยู่", callback=self.cancel_now)
        self.item_pause = rumps.MenuItem("พักการใช้งาน", callback=self.toggle_pause)
        self.item_hybrid = rumps.MenuItem("", callback=lambda _: self.set_mode("hybrid"))
        self.item_live = rumps.MenuItem("", callback=lambda _: self.set_mode("live"))
        self.item_single = rumps.MenuItem("", callback=lambda _: self.set_mode("single"))
        self.item_turbo = rumps.MenuItem("", callback=lambda _: self.switch_model("turbo"))
        self.item_large = rumps.MenuItem("", callback=lambda _: self.switch_model("large-v3"))
        self.menu = [
            self.item_status, None,
            self.item_cancel, self.item_pause, None,
            self.item_hybrid, self.item_live, self.item_single, None,
            self.item_turbo, self.item_large, None,
            rumps.MenuItem("Quit", callback=self.do_quit),
        ]
        self.refresh_menu()

        rumps.Timer(self.update_title, 0.25).start()
        rumps.Timer(self.keep_warm, 300).start()
        rumps.Timer(self.check_trigger, 0.3).start()  # ช่องเทสภายใน
        threading.Thread(target=self.load_model, daemon=True).start()
        threading.Thread(target=self.hotkey_listener, daemon=True).start()

    # ---------- UI ----------

    def update_title(self, _timer=None):
        icons = {"loading": "⏳", "idle": "💤", "recording": "🎙️",
                 "transcribing": "⏳", "paused": "🚫"}
        want = icons.get(self.state, "💤")
        if self.title != want:
            self.title = want

    def mode(self):
        m = self.cfg.get("mode", "hybrid")
        return m if m in MODES else "hybrid"

    def refresh_menu(self):
        check = lambda a: "✅ " if a else ""
        m = self.mode()
        self.item_hybrid.title = check(m == "hybrid") + "โหมด: ไฮบริด — พูดยาวได้ วางทีเดียว รอ 2-4 วิ (แนะนำ)"
        self.item_live.title = check(m == "live") + "โหมด: ทยอยวางสดระหว่างพูด"
        self.item_single.title = check(m == "single") + "โหมด: ถอดทีเดียวตอนจบ (รอนานตามที่พูด)"
        self.item_turbo.title = check(self.preset == "turbo") + "โมเดล: turbo (เร็วสุด · แนะนำ)"
        self.item_large.title = check(self.preset == "large-v3") + "โมเดล: large-v3 (แม่นกว่านิด · ช้ากว่า 2 เท่า)"
        self.item_pause.title = "เปิดใช้งานต่อ" if self.state == "paused" else "พักการใช้งาน"

    def set_mode(self, m):
        self.cfg["mode"] = m
        save_config(self.cfg)
        self.refresh_menu()

    def toggle_pause(self, _):
        if self.state == "paused":
            self.state = "idle"
        elif self.state == "idle":
            self.state = "paused"
        self.refresh_menu()

    def cancel_now(self, _=None):
        """ยกเลิกทุกรอบที่ค้าง (จากเมนูบาร์) — ทุกงาน ≤ รุ่นปัจจุบันกลายเป็นโมฆะทันที"""
        if self.state not in ("recording", "transcribing"):
            return
        self.cancel_gen = self.session
        self.stop_gen = self.session
        proc, self.rec_proc = self.rec_proc, None
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        self.state = "idle"
        log("ยกเลิกจากเมนู (โมฆะถึงรุ่น %d)" % self.cancel_gen)
        self.item_status.title = "ยกเลิกแล้ว"
        play(SOUND_ERROR)

    # ---------- Model ----------

    def load_model(self):
        engine, model_id = PRESETS[self.preset]
        try:
            log("โหลดโมเดล %s ..." % self.preset)
            self.item_status.title = "กำลังโหลดโมเดล %s ..." % self.preset
            t0 = time.time()
            if engine == "mlx":
                import mlx_whisper
                silence = tempfile.mktemp(suffix=".wav", prefix="ron_dictate_warm_")
                subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                "-t", "0.3", silence], check=True)
                mlx_whisper.transcribe(silence, path_or_hf_repo=model_id, language="th")
                os.remove(silence)
                self.model = ("mlx", model_id)
            else:
                from faster_whisper import WhisperModel
                self.model = ("faster", WhisperModel(model_id, device="cpu",
                                                     compute_type="int8", cpu_threads=os.cpu_count()))
            log("โมเดลพร้อม (%.1fs)" % (time.time() - t0))
            self.mic = find_mic(self.cfg.get("mic_substring", "MacBook"))
            log("ใช้ไมค์: %s" % self.mic)
            self.item_status.title = "พร้อมใช้ — จิ้ม ⌥ ซ้าย เพื่อพูด"
            if self.state == "loading":
                self.state = "idle"
            if self.want_start:
                self.want_start = False
                log("เริ่มอัดให้เอง (จองคิวไว้ตอนโหลด)")
                self.start_recording()
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

    def keep_warm(self, _timer=None):
        if self.state != "idle" or not self.model:
            return
        threading.Thread(target=self._warm_once, daemon=True).start()

    def _warm_once(self):
        try:
            if self.state != "idle" or not self.model:
                return
            if not self.gpu_lock.acquire(blocking=False):
                return  # GPU มีงานอยู่ = อุ่นอยู่แล้วโดยปริยาย
            try:
                import mlx_whisper
                silence = tempfile.mktemp(suffix=".wav", prefix="ron_dictate_warm_")
                subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                                "-t", "0.3", silence], check=True)
                mlx_whisper.transcribe(silence, path_or_hf_repo=self.model[1], language="th")
                os.remove(silence)
            finally:
                self.gpu_lock.release()
        except Exception:
            pass

    # ---------- Hotkey ----------

    def hotkey_listener(self):
        try:
            if not check_permissions():
                self.item_status.title = "⚠️ กด Allow ใน System Settings แล้วเปิดแอปใหม่"
            hotkey = getattr(keyboard.Key, self.cfg.get("hotkey", "alt_l"), keyboard.Key.alt_l)
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
                        self.on_hotkey()

            with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
                listener.join()
        except Exception:
            log("ตัวฟังปุ่มลัดพัง:\n" + traceback.format_exc())
            self.item_status.title = "❌ ปุ่มลัดใช้ไม่ได้ (ดู dictate.log)"

    def check_trigger(self, _timer=None):
        """ช่องเทสภายใน: echo tap > /tmp/ron_dictate_trigger = จิ้มปุ่ม 1 ที (ให้ AI เทสแทนคนได้)"""
        try:
            if os.path.exists(TRIGGER_PATH):
                with open(TRIGGER_PATH) as f:
                    cmd = f.read().strip()
                os.remove(TRIGGER_PATH)
                log("(trigger เทส = %s)" % (cmd or "tap"))
                if cmd == "cancel":
                    self.cancel_now()
                else:
                    self.on_hotkey()
        except OSError:
            pass

    def on_hotkey(self):
        now = time.time()
        if now - self.last_tap < 0.7:  # จิ้มรัว = นับครั้งเดียว
            return
        self.last_tap = now
        log("จิ้ม (สถานะ: %s)" % self.state)
        if self.state == "idle":
            self.start_recording()
        elif self.state == "recording":
            self.stop_gen = self.session
            self._close_recorder(self.rec_proc)  # ปิดไมค์เดี๋ยวนั้นเลย ไม่รอลูป (เคยอัดเกินไป 10-30 วิ)
            self.state = "transcribing"
            play(SOUND_STOP)
        elif self.state == "transcribing":
            # ไม่ต้องรอรอบเก่าเก็บท้ายเสียง — เริ่มพูดรอบใหม่ได้ทันที (ข้อความยังเรียงลำดับถูก)
            self.start_recording()
        elif self.state == "loading":
            # จองคิวไว้ — โหลดเสร็จปุ๊บจะป๊อปแล้วเริ่มอัดให้เอง ไม่ต้องจิ้มซ้ำ
            self.want_start = True
            self.item_status.title = "กำลังโหลด... พร้อมแล้วจะเริ่มอัดให้เลย"
            play(SOUND_WAIT)
        else:
            play(SOUND_ERROR)  # paused — มีเสียงตอบเสมอ ไม่เงียบใส่

    # ---------- Record + Stream ----------

    def start_recording(self):
        if not self.mic:
            self.mic = find_mic(self.cfg.get("mic_substring", "MacBook"))
            if not self.mic:
                self.item_status.title = "❌ หาไมค์ไม่เจอ"
                play(SOUND_ERROR)
                return
        self.session += 1
        my_session = self.session
        prev_done = self.prev_done          # worker รอบก่อน (ไว้รอเรียงลำดับวาง)
        done_evt = threading.Event()
        self.prev_done = done_evt
        seg_dir = tempfile.mkdtemp(prefix="ron_dictate_seg_")
        seg_time = str(MODES[self.mode()][0])
        cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
               "-f", "avfoundation", "-i", ":" + self.mic,
               "-ar", "16000", "-ac", "1",
               "-f", "segment", "-segment_time", seg_time, "-reset_timestamps", "1",
               os.path.join(seg_dir, "seg_%03d.wav")]
        self.rec_proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.state = "recording"
        log("เริ่มอัด (รุ่น %d) → %s" % (my_session, seg_dir))
        play(SOUND_START)
        threading.Thread(target=self.stream_worker,
                         args=(my_session, self.rec_proc, seg_dir, prev_done, done_evt),
                         daemon=True).start()

    def alive(self, my_session):
        """รอบนี้ยังไม่ถูกยกเลิกใช่ไหม (รอบใหม่เริ่มได้โดยรอบเก่ายังเก็บงานต่อเบื้องหลัง)"""
        return my_session > self.cancel_gen

    @staticmethod
    def _close_recorder(proc):
        """ส่ง SIGINT ให้ ffmpeg ปิดไฟล์สวยๆ — รอในเธรดแยก ไม่บล็อกใคร"""
        if not proc or proc.poll() is not None:
            return
        def _do():
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        threading.Thread(target=_do, daemon=True).start()

    def stream_worker(self, my_session, proc, seg_dir, prev_done, done_evt):
        import glob as globmod
        _seg, paste_live, max_silent = MODES[self.mode()]
        done, text_all, ffmpeg_closed = set(), "", False
        silent_run, t_stop, waited_prev = 0, None, False
        pending_break = False  # เจอช่วงเงียบคั่นกลาง → ท่อนถัดไปขึ้นย่อหน้าใหม่
        try:
            while True:
                if not self.alive(my_session):
                    log("รุ่น %d ถูกยกเลิก — จบเงียบๆ" % my_session)
                    return
                if not ffmpeg_closed and self.stop_gen >= my_session:
                    if t_stop is None:
                        t_stop = time.time()
                    self._close_recorder(proc)  # เผื่อกรณี auto-stop ที่ยังไม่มีใครปิด
                    if proc.poll() is not None:
                        ffmpeg_closed = True  # ไฟล์ท่อนสุดท้ายปิดสมบูรณ์แล้ว
                files = sorted(globmod.glob(os.path.join(seg_dir, "seg_*.wav")))
                ready = files if ffmpeg_closed else files[:-1]
                for f in ready:
                    if f in done or not self.alive(my_session):
                        continue
                    done.add(f)
                    try:
                        # เศษท้ายสั้นกว่า ~1.2 วิ หลังจิ้มหยุด = เสียงค้าง ไม่ใช่คำพูด (ถ้ามีข้อความแล้ว)
                        if os.path.getsize(f) < 38000 and ffmpeg_closed and f == files[-1] and text_all:
                            log("ทิ้งเศษท้ายสั้น (%dKB)" % (os.path.getsize(f) // 1024))
                            continue
                        if not has_speech(f, self.cfg.get("silence_rms", 250)):
                            silent_run += 1
                            if text_all:
                                pending_break = True  # เว้นวรรคความคิด → ย่อหน้าใหม่
                            log("ท่อน %s ข้าม (ไม่มีเสียงพูด %d)" % (os.path.basename(f), silent_run))
                            continue
                        txt, secs = self.transcribe(f, prev=text_all)
                        # 🔑 เช็ค "ถูกยกเลิก?" หลังถอดเสร็จ ก่อนใช้ผลเสมอ
                        if not self.alive(my_session):
                            log("ถอดเสร็จแต่ถูกยกเลิกแล้ว — ทิ้ง: %s" % txt[:30])
                            return
                        if txt:
                            silent_run = 0
                            sep = "" if not text_all else ("\n" if pending_break else " ")
                            pending_break = False
                            if paste_live:
                                if prev_done and not waited_prev:
                                    prev_done.wait(60)
                                    waited_prev = True
                                self.paste_text(sep + txt)
                            text_all += sep + txt
                            log("ท่อน %s ถอด %.1fs → %s" % (os.path.basename(f), secs, txt[:60]))
                        else:
                            silent_run += 1
                    except Exception:
                        log("ถอดท่อนพัง:\n" + traceback.format_exc())
                if not ffmpeg_closed and silent_run >= max_silent:
                    log("เงียบนาน — หยุดอัดเอง (รุ่น %d)" % my_session)
                    self.stop_gen = max(self.stop_gen, my_session)
                    if self.session == my_session:
                        self.item_status.title = "หยุดเอง (ไม่ได้ยินเสียงพูดนาน)"
                        self.state = "transcribing"
                        play(SOUND_STOP)
                if ffmpeg_closed and len(done) == len(globmod.glob(os.path.join(seg_dir, "seg_*.wav"))):
                    break
                time.sleep(0.4)
            if text_all:
                if not paste_live:
                    # โหมดไฮบริด/ทีเดียว: วางครั้งเดียวตรงนี้ — เช็คยกเลิกรอบสุดท้ายก่อน
                    if not self.alive(my_session):
                        log("รุ่น %d ถูกยกเลิกก่อนวาง — ทิ้งทั้งหมด" % my_session)
                        return
                    if prev_done and not waited_prev:
                        prev_done.wait(60)
                    self.paste_text(text_all)
                wait = (time.time() - t_stop) if t_stop else 0
                log("จบรุ่น %d · %d ตัวอักษร · รอหลังหยุด %.1fs" % (my_session, len(text_all), wait))
                if self.session == my_session:
                    self.item_status.title = "ล่าสุด: %s" % (text_all[:40] + ("…" if len(text_all) > 40 else ""))
                play(SOUND_DONE)
            else:
                log("จบรุ่น %d — ไม่ได้ยินเสียงพูด" % my_session)
                if self.session == my_session:
                    self.item_status.title = "ไม่ได้ยินเสียงพูด — ลองใหม่"
                play(SOUND_ERROR)
        finally:
            done_evt.set()
            if not ffmpeg_closed:
                try:
                    proc.kill()
                except Exception:
                    pass
            shutil.rmtree(seg_dir, ignore_errors=True)
            if self.session == my_session:  # เฉพาะรอบล่าสุดเท่านั้นที่แตะสถานะรวม
                self.rec_proc = None
                self.state = "idle"

    # ---------- Transcribe + Paste ----------

    def transcribe(self, path, prev=""):
        t0 = time.time()
        _engine, model_id = self.model
        parts = []
        vocab = self.cfg.get("vocab") or []
        if vocab:
            parts.append("ศัพท์เฉพาะที่ใช้บ่อย: " + ", ".join(vocab))
        if prev:
            parts.append("ข้อความก่อนหน้า: " + prev[-150:])
        prompt = " · ".join(parts) or None
        with self.gpu_lock:  # ถอดทีละงาน กัน crash
            if _engine == "mlx":
                import mlx_whisper
                r = mlx_whisper.transcribe(path, path_or_hf_repo=model_id,
                                           language=self.cfg.get("language", "th"),
                                           initial_prompt=prompt)
                segs = r.get("segments", [])
            else:
                sg, _info = model_id.transcribe(path, language=self.cfg.get("language", "th"),
                                                beam_size=5, initial_prompt=prompt)
                segs = list(sg)
        text = clean_segments(segs)
        for wrong, right in (self.cfg.get("corrections") or {}).items():
            text = text.replace(wrong, right)
        return text, time.time() - t0

    def paste_text(self, text):
        p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        p.communicate(text.encode("utf-8"))
        time.sleep(0.15)
        v_key = keyboard.KeyCode.from_vk(9)  # ปุ่ม V ตามฮาร์ดแวร์ — คีย์บอร์ดไทยก็เข้า
        with self.kb.pressed(keyboard.Key.cmd):
            self.kb.press(v_key)
            self.kb.release(v_key)

    def do_quit(self, _):
        self.cancel_gen = self.session
        if self.rec_proc:
            try:
                self.rec_proc.kill()
            except Exception:
                pass
        rumps.quit_application()


if __name__ == "__main__":
    log("=== Ron Dictate v3 เริ่มทำงาน ===")
    Dictate().run()
