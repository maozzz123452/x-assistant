"""X Assistant - a small push-to-talk voice window that plays music on YouTube or Spotify.

Press the hotkey (default F9) or click the orb, then say a song + artist:
    "around me metro boomin"                       -> plays on the default platform
    "hey x put around me by metro boomin via yt"   -> YouTube
    "play blinding lights on spotify"              -> Spotify
    "pause" / "next" / "previous"                  (Spotify, needs API setup)
"""

import audioop
import collections
import ctypes
import faulthandler
import html
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import traceback
import urllib.parse
import urllib.request
import webbrowser
from tkinter import ttk

import keyboard
import speech_recognition as sr
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageTk

APP_NAME = "X Assistant"
CREDIT = "made by zush / maoz"

# config.json lives next to the .exe (or next to this script when run with Python)
BASE_DIR = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SPOTIFY_CACHE = os.path.join(BASE_DIR, ".spotify_token")
LAST_RECORDING = os.path.join(BASE_DIR, "last_recording.wav")
CRASH_LOG = os.path.join(BASE_DIR, "crash.log")
ACTIVITY_LOG = os.path.join(BASE_DIR, "activity.log")


def log_activity(msg):
    """Plain record of what each command did (the exe has no console), capped at ~200 KB."""
    try:
        if os.path.exists(ACTIVITY_LOG) and os.path.getsize(ACTIVITY_LOG) > 200_000:
            os.replace(ACTIVITY_LOG, ACTIVITY_LOG + ".old")
        with open(ACTIVITY_LOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass

DEFAULT_MIC = "(Windows default)"
DEFAULT_CONFIG = {
    "hotkey": "f9",
    "microphone": DEFAULT_MIC,
    "capture_mode": "mme16",
    "language": "en-US",
    "default_platform": "youtube",
    "volume": 80,               # song volume 0-100 (only the song, never the Windows master volume)
    "chrome_tab_mode": "same",  # "same": replace the song in the same tab, "new": new tab each time
    "always_on_top": True,
    "spotify_client_id": "",
    "spotify_client_secret": "",
    "spotify_redirect_uri": "http://127.0.0.1:8888/callback",
    "voice_confirmation": True,       # say "Gotchu, <song> is now playing on <platform>."
    "elevenlabs_api_key": "",         # optional: most natural voice
    "elevenlabs_voice_id": "",        # empty = pick the most natural conversational female voice automatically
    # Microsoft neural voice used without ElevenLabs. Emma Multilingual speaks English *and* Hebrew (with English
    # song titles inside Hebrew sentences) naturally, so the assistant keeps one personality in both languages.
    "tts_voice": "en-US-EmmaMultilingualNeural",
}
LANGUAGES = {"English": "en-US", "Hebrew": "he-IL"}

# ---------------------------------------------------------------- crash logging (the exe has no console)

_crash_file = open(CRASH_LOG, "a", encoding="utf-8")
faulthandler.enable(_crash_file)  # native crashes (e.g. inside the audio driver)


def log_exception(exc_type, exc, tb):
    _crash_file.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    _crash_file.write("".join(traceback.format_exception(exc_type, exc, tb)))
    _crash_file.flush()


sys.excepthook = log_exception
threading.excepthook = lambda a: log_exception(a.exc_type, a.exc_value, a.exc_traceback)

# ---------------------------------------------------------------- look

FONT = "Segoe UI"
GLASS = "#000000"        # pure black = fully clear: Windows shows the blurred desktop through it
GLASS_TINT = 0x881A0A14  # acrylic tint, 0xAABBGGRR (dark purple, ~53% opaque so text stays readable)
CORNER = 8               # Windows 11 rounded-corner radius
TEXT = "#ffffff"
MUTED = "#b8a9d9"
FIELD = "#140d22"
PINK = (217, 70, 239)    # bright purple-magenta used for glow gradients
VIOLET = (124, 58, 237)

BLOBS = [  # (x, y, radius as fraction of the short side, color) - purple light inside the glass
    (0.08, 0.30, 0.70, (124, 58, 237)),
    (0.95, 1.00, 0.60, (168, 60, 247)),
    (0.75, -0.10, 0.45, (76, 29, 149)),
    (0.45, 0.55, 0.25, (40, 12, 80)),
]

ORB_STATES = {  # outer color, inner color, pulses
    "idle": ((124, 58, 237), (225, 170, 255), False),
    "listening": ((200, 50, 240), (255, 175, 245), True),
    "thinking": ((90, 80, 240), (190, 170, 255), True),
    "done": ((16, 185, 129), (140, 240, 190), False),
    "error": ((239, 68, 68), (251, 160, 60), False),
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH, encoding="utf-8-sig") as f:
        return {**DEFAULT_CONFIG, **json.load(f)}


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- rendering (PIL)

def glass_background(w, h, radius=CORNER, s=2):
    """Liquid-glass pane. Everything is drawn on black and composited additively over the blurred
    desktop: black stays clear, colours add light (purple glow, top sheen, specular rim)."""
    W, H, R = w * s, h * s, radius * s
    img = Image.new("RGB", (W, H), 0)
    d = ImageDraw.Draw(img)
    for x, y, r, col in BLOBS:
        r *= min(W, H)
        d.ellipse((x * W - r, y * H - r, x * W + r, y * H + r), fill=tuple(int(c * 0.35) for c in col))
    img = img.filter(ImageFilter.GaussianBlur(min(W, H) * 0.3))

    sheen = Image.linear_gradient("L").resize((W, H)).point(lambda v: max(0, 34 - v * 34 // 110))
    img = ImageChops.add(img, Image.merge("RGB", [sheen] * 3))

    rim = Image.new("L", (W, H), 0)  # specular edge: bright top-left fading to dim bottom-right
    ImageDraw.Draw(rim).rounded_rectangle((1, 1, W - 2, H - 2), R, outline=255, width=int(1.5 * s))
    diag = Image.linear_gradient("L").rotate(45, expand=True).resize((W, H)).point(lambda v: int(230 - v * 0.75))
    img = ImageChops.add(img, Image.merge("RGB", [ImageChops.multiply(rim, diag)] * 3))

    inner = Image.new("L", (W, H), 0)  # soft inner lensing glow along the edge
    ImageDraw.Draw(inner).rounded_rectangle((4 * s, 4 * s, W - 4 * s, H - 4 * s), max(1, R - 2 * s),
                                            outline=60, width=3 * s)
    img = ImageChops.add(img, Image.merge("RGB", [inner.filter(ImageFilter.GaussianBlur(4 * s))] * 3))
    return img.resize((w, h), Image.LANCZOS)


def orb_image(size, dia, c1, c2, glow, s=3):
    """Glowing glass sphere with a microphone glyph."""
    S, D = size * s, dia * s
    cx = cy = S / 2
    R = D / 2
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))

    g = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    gr = R * (1.0 + 0.35 * glow)
    ImageDraw.Draw(g).ellipse((cx - gr, cy - gr, cx + gr, cy + gr), fill=c1 + (int(150 + 100 * glow),))
    img = Image.alpha_composite(img, g.filter(ImageFilter.GaussianBlur(D * (0.16 + 0.1 * glow))))

    body = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    steps = 48
    for i in range(steps):
        t = i / steps
        r = R * (1 - t * 0.85)
        ox, oy = -0.22 * R * t, -0.28 * R * t
        col = tuple(int(a + (b - a) * t ** 0.9) for a, b in zip(c1, c2))
        bd.ellipse((cx + ox - r, cy + oy - r, cx + ox + r, cy + oy + r), fill=col + (255,))
    body = body.filter(ImageFilter.GaussianBlur(2 * s))
    hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(hl).ellipse((cx - 0.62 * R, cy - 0.92 * R, cx + 0.62 * R, cy - 0.18 * R), fill=(255, 255, 255, 95))
    body = Image.alpha_composite(body, hl.filter(ImageFilter.GaussianBlur(3 * s)))
    ImageDraw.Draw(body).ellipse((cx - R + s, cy - R + s, cx + R - s, cy + R - s), outline=(255, 255, 255, 90), width=s)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).ellipse((cx - R, cy - R, cx + R, cy + R), fill=255)
    img.paste(body, (0, 0), mask)

    d = ImageDraw.Draw(img)
    w, lw = D * 0.2, max(2, int(D * 0.05))
    d.rounded_rectangle((cx - w / 2, cy - D * 0.26, cx + w / 2, cy + D * 0.08), w / 2, fill="white")
    a = D * 0.19
    d.arc((cx - a, cy - a - D * 0.02, cx + a, cy + a - D * 0.02), 0, 180, fill="white", width=lw)
    d.line((cx, cy + a - D * 0.02, cx, cy + D * 0.28), fill="white", width=lw)
    d.line((cx - D * 0.1, cy + D * 0.28, cx + D * 0.1, cy + D * 0.28), fill="white", width=lw)
    return img.resize((size, size), Image.LANCZOS)


def pill_image(w, h, fill, outline=None, gradient=None, s=3):
    W, H = w * s, h * s
    if gradient:
        img = Image.linear_gradient("L").rotate(90).resize((W, H))
        layers = [img.point(lambda v, a=a, b=b: int(a + (b - a) * v / 255)) for a, b in zip(*gradient)]
        img = Image.merge("RGB", layers).convert("RGBA")
    else:
        img = Image.new("RGBA", (W, H), fill)
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, W - 1, H - 1), H / 2, fill=255)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    if outline:
        ImageDraw.Draw(out).rounded_rectangle((0, 0, W - 1, H - 1), H / 2, outline=outline, width=s)
    return out.resize((w, h), Image.LANCZOS)


def field_image(w, h, s=3):
    img = Image.new("RGBA", (w * s, h * s), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((0, 0, w * s - 1, h * s - 1), 10 * s, fill=FIELD,
                                          outline=(255, 255, 255, 45), width=s)
    return img.resize((w, h), Image.LANCZOS)


def round_corners(widget_id):
    """Windows 11 rounded corners for the top-level window containing a Tk widget id."""
    try:
        hwnd = ctypes.windll.user32.GetParent(widget_id) or widget_id
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)
    except Exception:
        pass


class Frameless:
    """Rounded, borderless glass window that still shows on the taskbar and can be dragged."""

    def setup_window(self, win, w, h, in_taskbar=True):
        self.win, self.W, self.H = win, w, h
        self._imgs = []
        win.overrideredirect(True)
        win.configure(bg=GLASS)
        self.cv = tk.Canvas(win, width=w, height=h, bg=GLASS, highlightthickness=0, bd=0)
        self.cv.pack()
        self.cv.create_image(0, 0, anchor="nw", image=self.keep(glass_background(w, h)), tags="bg")
        self.cv.bind("<ButtonPress-1>", self._drag_start)
        self.cv.bind("<B1-Motion>", self._drag_move)
        win.after(250, lambda: (in_taskbar and self._show_in_taskbar(), self._apply_glass()))

    def keep(self, pil):
        tkimg = ImageTk.PhotoImage(pil)
        self._imgs.append(tkimg)
        return tkimg

    def _hwnd(self):
        return ctypes.windll.user32.GetParent(self.win.winfo_id())

    def _show_in_taskbar(self):
        # Tk makes borderless windows WS_EX_TOOLWINDOW (no taskbar / Alt+Tab entry). Swap to WS_EX_APPWINDOW once
        # the window is mapped, and re-show it through Win32 (a Tk withdraw/deiconify would re-apply the old style).
        try:
            u32, hwnd = ctypes.windll.user32, self._hwnd()
            ex = u32.GetWindowLongW(hwnd, -20)
            u32.SetWindowLongW(hwnd, -20, (ex & ~0x80) | 0x40000)
            u32.ShowWindow(hwnd, 0)
            u32.ShowWindow(hwnd, 5)
        except Exception:
            pass

    def _apply_glass(self):
        """Real translucency: acrylic blur of whatever is behind the window + Windows 11 rounded corners."""
        class Accent(ctypes.Structure):
            _fields_ = [("state", ctypes.c_int), ("flags", ctypes.c_int), ("color", ctypes.c_uint), ("anim", ctypes.c_int)]

        class Data(ctypes.Structure):
            _fields_ = [("attr", ctypes.c_int), ("data", ctypes.c_void_p), ("size", ctypes.c_size_t)]

        try:
            hwnd = self._hwnd()
            self._accent = Accent(4, 2, GLASS_TINT, 0)  # ACCENT_ENABLE_ACRYLICBLURBEHIND
            ctypes.windll.user32.SetWindowCompositionAttribute(
                hwnd, ctypes.byref(Data(19, ctypes.addressof(self._accent), ctypes.sizeof(self._accent))))
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)  # round corners
        except Exception:
            pass

    def minimize(self):
        ctypes.windll.user32.ShowWindow(self._hwnd(), 6)  # SW_MINIMIZE (Tk refuses to iconify borderless windows)

    def handle_press(self, e):  # overridden by windows with draggable controls
        return False

    def handle_drag(self, e):
        pass

    def _drag_start(self, e):
        if self.handle_press(e):
            self._dx = None
            return
        self._dx, self._dy = e.x, e.y

    def _drag_move(self, e):
        if getattr(self, "_dx", None) is None:
            self.handle_drag(e)
            return
        self.win.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def icon_button(self, x, y, glyph, command, size=12):
        t = self.cv.create_text(x, y, text=glyph, fill=MUTED, font=("Segoe UI Symbol", size))
        self.cv.tag_bind(t, "<Enter>", lambda e: self.cv.itemconfig(t, fill=TEXT))
        self.cv.tag_bind(t, "<Leave>", lambda e: self.cv.itemconfig(t, fill=MUTED))
        self.cv.tag_bind(t, "<Button-1>", lambda e: command())
        return t


# ---------------------------------------------------------------- microphones

# One PortAudio instance for the whole app: creating/terminating several from different threads
# (main listener + settings meter) is not thread-safe and can crash the process.
_PA = None
_PA_LOCK = threading.RLock()

# How a mic is opened. "Mic check" in settings picks whichever one Google understands on this PC.
CAPTURE_MODES = {
    "mme16": "Windows MME · 16 kHz",
    "wasapi": "WASAPI · native rate",
    "mme44": "Windows MME · 44.1 kHz",
}


def pyaudio_instance():
    global _PA
    with _PA_LOCK:
        if _PA is None:
            _PA = sr.Microphone.get_pyaudio().PyAudio()
        return _PA


def list_microphones():
    """{full name: {"wasapi": (index, rate) | None, "mme": index | None}}.

    WASAPI provides full device names; MME truncates them to 31 characters, so MME devices are matched by prefix.
    """
    pa = pyaudio_instance()
    with _PA_LOCK:
        apis = {pa.get_host_api_info_by_index(i)["name"]: i for i in range(pa.get_host_api_count())}
        devices = [pa.get_device_info_by_index(i) for i in range(pa.get_device_count())]
    inputs = [d for d in devices if d["maxInputChannels"] > 0
              and not d["name"].startswith(("Primary Sound", "Microsoft Sound Mapper"))]
    mme = {d["name"]: d["index"] for d in inputs if d["hostApi"] == apis.get("MME")}
    mics = {}
    for d in inputs:
        if d["hostApi"] == apis.get("Windows WASAPI"):
            mme_index = next((i for n, i in mme.items() if d["name"].startswith(n)), None)
            mics[d["name"]] = {"wasapi": (d["index"], int(d["defaultSampleRate"])), "mme": mme_index}
    for name, index in mme.items():  # devices only visible through MME
        if not any(full.startswith(name) for full in mics):
            mics[name] = {"wasapi": None, "mme": index}
    return mics


class MicSource:
    """Minimal stand-in for sr.Microphone that uses the shared PortAudio instance."""
    SAMPLE_WIDTH = 2

    def __init__(self, name, mode="mme16"):
        info = list_microphones().get(name) if name != DEFAULT_MIC else None
        self.index, self.SAMPLE_RATE = None, 44100 if mode == "mme44" else 16000
        if info:
            if mode == "wasapi" and info["wasapi"]:
                self.index, self.SAMPLE_RATE = info["wasapi"]
            elif info["mme"] is not None:
                self.index = info["mme"]
            elif info["wasapi"]:
                self.index, self.SAMPLE_RATE = info["wasapi"]
        self.CHUNK = 1024 if self.SAMPLE_RATE > 20000 else 512
        self.stream = None

    def __enter__(self):
        pa = pyaudio_instance()
        with _PA_LOCK:
            self.stream = pa.open(format=sr.Microphone.get_pyaudio().paInt16, channels=1, rate=self.SAMPLE_RATE,
                                  input=True, input_device_index=self.index, frames_per_buffer=self.CHUNK)
        return self

    def read(self):
        return self.stream.read(self.CHUNK, exception_on_overflow=False)

    def __exit__(self, *exc):
        with _PA_LOCK:
            self.stream.stop_stream()
            self.stream.close()


def open_microphone(name, mode="mme16"):
    return MicSource(name, mode)


def recognize(audio, language, save_to=LAST_RECORDING):
    """Normalise quiet audio, resample to 16 kHz, keep a copy for debugging, and send to Google."""
    raw = audio.get_raw_data(convert_rate=16000, convert_width=2)
    peak = audioop.max(raw, 2) or 1
    if peak < 4000:
        raw = audioop.mul(raw, 2, min(4.0, 12000 / peak))
    clip = sr.AudioData(raw, 16000, 2)
    if save_to:
        with open(save_to, "wb") as f:
            f.write(clip.get_wav_data())
    try:
        result = sr.Recognizer().recognize_google(clip, language=language, show_all=True)
    except sr.UnknownValueError:
        result = None
    alts = result.get("alternative", []) if isinstance(result, dict) else []
    if not alts:
        raise sr.UnknownValueError()
    return alts[0]["transcript"]


def play_last_recording():
    import winsound
    if os.path.exists(LAST_RECORDING):
        winsound.PlaySound(LAST_RECORDING, winsound.SND_FILENAME | winsound.SND_ASYNC)


# ---------------------------------------------------------------- command parsing

WAKE_RE = re.compile(r"^\s*(hey|hi|okay|ok|היי)?[\s,]*(x|ex|eks|axe|איקס|אקס)(?=\s|,|$)[\s,]*", re.I)
PLATFORM_RE = re.compile(
    r"\s+(?:(?:via|on|in|from|using|with|through|דרך)\s+)?ב?"
    r"(youtube|you tube|yt|y\.t\.?|y t|why tea|spotify|spot|יוטיוב|ספוטיפיי|ספוטיפי)\s*$", re.I)
# filler words people say before the command ("please play…", "יאללה תנגן…")
FILLER_RE = re.compile(
    r"^((please|hey|yo|ok|okay|can you|could you|would you|just|now|בבקשה|יאללה|אפשר|תוכל|תוכלי|רגע|אחי|אחותי|"
    r"עכשיו|טוב|סבבה)[\s,.!]+)+", re.I)
PLAY_RE = re.compile(
    r"^(put|play|start|open|תשים|תשימי|שים|שימי|תנגן|תנגני|נגן|נגני|תפעיל|תפעילי|הפעל|תשמיע|תשמיעי|השמע|"
    r"תעלה|תעלי)(?=[\s,.:!]|$)[\s,.:!]*(me\s+|לי[\s,]+)?(on\s+)?(the\s+|את\s+)?(song|track|music|השיר|שיר)?\s*",
    re.I)
CONTROLS = {
    "pause": ("pause", "stop", "stop the music", "pause the music", "pause music", "עצור", "תעצור", "השהה"),
    "resume": ("resume", "continue", "unpause", "play", "המשך", "תמשיך"),
    "next": ("next", "skip", "next song", "skip song", "next track", "הבא", "השיר הבא", "דלג"),
    "previous": ("previous", "back", "previous song", "last song", "previous track", "הקודם", "השיר הקודם"),
}


VOLUME_WORD_RE = re.compile(r"\b(volume|sound|loudness)\b|ווליום|עוצמ", re.I)  # עוצמה / עוצמת
UP_RE = re.compile(r"\b(up|increase|raise|louder|higher)\b|תגביר|הגבר|תעלה", re.I)
DOWN_RE = re.compile(r"\b(down|decrease|lower|reduce|quieter)\b|תנמיך|הנמך|תוריד", re.I)
MUTE_RE = re.compile(r"^(mute|silence)( the)?( music| song| sound| volume| it)?$|^השתק|^תשתיק", re.I)
UNMUTE_RE = re.compile(r"^(unmute|un-mute|un mute)( the)?( music| song| sound| volume| it)?$|^בטל השתקה", re.I)
UNLOOP_RE = re.compile(
    r"^(stop|turn off|disable|cancel|end|no( more)?)( the)? (loop|looping|repeat|repeating)\b|^un-?loop\b|"
    r"^(בטל|תבטל|תבטלי|עצור|תעצור|תעצרי|תוריד|תורידי|בלי|הפסק|תפסיק)( את)? ?(ה)?(לופ|חזרה|ריפיט)", re.I)
LOOP_RE = re.compile(
    r"^(loop|repeat|replay)( it| this| the song| the track| this song| this track| one)?( please)?$|"
    r"^(put|set|keep|play)( it| this| the song| this song)? on (loop|repeat)$|^on repeat$|"
    r"^(לופ|ריפיט|חזרה|על חזרה|בלופ)$|^(תשים|שים|תשימי|שימי|תעשה|תעשי|עשה)( את)?( השיר)? ?(ב|על )?(לופ|ריפיט|חזרה)$|"
    r"^(תחזור|תחזרי) על (השיר|זה)$", re.I)


NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100, "half": 50, "max": 100,
    "אפס": 0, "אחד": 1, "אחת": 1, "שתיים": 2, "שניים": 2, "שלוש": 3, "ארבע": 4, "חמש": 5, "שש": 6, "שבע": 7,
    "שמונה": 8, "תשע": 9, "עשר": 10, "עשרים": 20, "שלושים": 30, "ארבעים": 40, "חמישים": 50, "שישים": 60,
    "שבעים": 70, "שמונים": 80, "תשעים": 90, "מאה": 100, "חצי": 50,
}


def find_number(text):
    """First number in the text, as digits ("40", "40%") or words ("forty five", "ארבעים וחמש")."""
    digits = re.search(r"(\d{1,3})", text)
    if digits:
        return int(digits.group(1))
    total, found = 0, False
    for word in re.findall(r"\w+", text.lower()):
        if word[:1] in "ולבה" and word[1:] in NUMBER_WORDS:  # Hebrew prefixes: "וחמש", "לארבעים"
            word = word[1:]
        if word in NUMBER_WORDS:
            total += NUMBER_WORDS[word]
            found = True
        elif found and word not in ("and", "percent"):
            break
    return total if found else None


def parse_command(text, default_platform):
    """Return (action, value, platform).
    action: play / pause / resume / next / previous / volume / volume_step / mute / unmute / None."""
    t = WAKE_RE.sub("", text.strip()).strip(" .!?")
    low = t.lower()
    if UNLOOP_RE.search(low):
        return "unloop", None, None
    if LOOP_RE.search(low):
        return "loop", None, None
    if UNMUTE_RE.search(low):
        return "unmute", None, None
    if MUTE_RE.search(low):
        return "mute", None, None
    if VOLUME_WORD_RE.search(low) or low in ("louder", "quieter", "turn it up", "turn it down"):
        num = find_number(low)
        if num is not None:
            return "volume", max(0, min(100, num)), None
        if UP_RE.search(low) or low == "turn it up":
            return "volume_step", 10, None
        if DOWN_RE.search(low) or low == "turn it down":
            return "volume_step", -10, None
        return "volume_help", None, None  # never treat a volume sentence as a song name
    for action, phrases in CONTROLS.items():
        if low in phrases:
            return action, None, None

    platform = default_platform
    m = PLATFORM_RE.search(" " + t)
    if m:
        platform = "spotify" if re.search("spot|ספוט", m.group(1), re.I) else "youtube"
        t = (" " + t)[: m.start()].strip()

    t = FILLER_RE.sub("", t)
    query = PLAY_RE.sub("", t).strip(" .!?,")
    if not query:
        return None, None, platform
    return "play", query, platform


# ---------------------------------------------------------------- unobtrusive windows
# Chrome / Spotify must never disturb what's on the primary monitor (e.g. a fullscreen game): they open on the
# secondary monitor without being activated, focus is handed straight back, and Chrome is minimised once playing.

from ctypes import wintypes as wt

_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32
_u32.GetForegroundWindow.restype = wt.HWND
_u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
_u32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
_u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
_u32.SetForegroundWindow.argtypes = [wt.HWND]
_u32.IsWindow.argtypes = [wt.HWND]
_u32.IsWindowVisible.argtypes = [wt.HWND]
_u32.IsIconic.argtypes = [wt.HWND]
_u32.IsZoomed.argtypes = [wt.HWND]
_u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
_u32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
_u32.GetWindow.argtypes = [wt.HWND, wt.UINT]
_u32.GetWindow.restype = wt.HWND
_k32.OpenProcess.restype = wt.HANDLE

SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER, SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
SW_SHOWMINNOACTIVE, SW_SHOWNOACTIVATE = 7, 4
CHROME_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitors():
    """[{"primary": bool, "work": (left, top, right, bottom)}] detected live (handles re-arranged displays)."""
    found = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, hdc, rect, lp):
        mi = _MonitorInfo()
        mi.cbSize = ctypes.sizeof(mi)
        ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi))
        w = mi.rcWork
        found.append({"primary": bool(mi.dwFlags & 1), "work": (w.left, w.top, w.right, w.bottom)})
        return True

    ctypes.windll.user32.EnumDisplayMonitors(None, None, proc(cb), 0)
    return found


def secondary_monitor():
    others = [m for m in monitors() if not m["primary"]]
    return others[0]["work"] if others else None


def _process_name(hwnd):
    pid = wt.DWORD()
    _u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _k32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(520), wt.DWORD(520)
        ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        return os.path.basename(buf.value).lower()
    finally:
        _k32.CloseHandle(h)


def _title(hwnd):
    buf = ctypes.create_unicode_buffer(512)
    _u32.GetWindowTextW(hwnd, buf, 512)
    # Chrome wraps titles in bidi marks and appends " - Google Chrome"
    title = buf.value.translate({0x202A: None, 0x202B: None, 0x202C: None, 0x200E: None, 0x200F: None}).strip()
    return title[: -len(" - Google Chrome")] if title.endswith(" - Google Chrome") else title


def app_windows(process):
    """Visible, titled, unowned top-level windows of a process (e.g. "chrome.exe")."""
    found = []
    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def cb(hwnd, lp):
        if _u32.IsWindowVisible(hwnd) and not _u32.GetWindow(hwnd, 4) and _title(hwnd) \
                and _process_name(hwnd) == process:
            found.append(hwnd)
        return True

    _u32.EnumWindows(proc(cb), 0)
    return found


def restore_focus(hwnd):
    """Give the foreground back to the window that had it (e.g. the game) if something took it."""
    if not hwnd or not _u32.IsWindow(hwnd) or _u32.GetForegroundWindow() == hwnd:
        return
    fg = _u32.GetForegroundWindow()
    me = _k32.GetCurrentThreadId()
    other = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
    target = _u32.GetWindowThreadProcessId(hwnd, None)
    for t in {other, target} - {0, me}:
        _u32.AttachThreadInput(me, t, True)
    try:
        _u32.SetForegroundWindow(hwnd)
    finally:
        for t in {other, target} - {0, me}:
            _u32.AttachThreadInput(me, t, False)


def move_to_secondary(hwnd, work):
    """Place a window on the secondary monitor without activating it or changing the Z-order of the game."""
    if _u32.IsIconic(hwnd) or _u32.IsZoomed(hwnd):
        _u32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)  # un-maximise so it can be moved
    if work:
        left, top, right, bottom = work
        _u32.SetWindowPos(hwnd, None, left, top, right - left, bottom - top, SWP_NOZORDER | SWP_NOACTIVATE)
    else:  # single monitor: keep it behind everything instead
        _u32.SetWindowPos(hwnd, wt.HWND(1), 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def open_unobtrusively(launch, process, new_window_only, settle=2.0):
    """Run `launch`, catch the resulting window of `process`, move it to the secondary monitor unactivated and
    keep handing focus back to whatever was in front before. Returns the window handle (or None)."""
    previous = _u32.GetForegroundWindow()
    work = secondary_monitor()
    before = set(app_windows(process))
    launch()

    hwnd, deadline = None, time.time() + 10
    while time.time() < deadline and not hwnd:
        current = app_windows(process)
        new = [h for h in current if h not in before]
        if new:
            hwnd = new[0]
        elif not new_window_only and current and time.time() > deadline - 9.4:
            hwnd = current[0]  # app reused its existing window
        else:
            restore_focus(previous)
            time.sleep(0.02)
    if hwnd:
        move_to_secondary(hwnd, work)
    keep_focus(previous, hwnd, work, settle)  # the app may try to grab focus a little later
    return hwnd, previous


def find_chrome():
    return next((p for p in CHROME_PATHS if os.path.exists(p)), None)


# X Assistant drives its own Chrome window (separate profile + local DevTools port) so it can change the song
# in the same tab without ever clicking or typing into Chrome - which would steal focus from a game.
CHROME_PROFILE = os.path.join(BASE_DIR, "chrome_profile")
CDP_PORT = 9223
CDP = f"http://127.0.0.1:{CDP_PORT}"


def cdp_tabs():
    """Open tabs of the X Assistant Chrome window, most recent first (None if that window isn't running)."""
    try:
        with urllib.request.urlopen(CDP + "/json", timeout=1) as r:
            return [t for t in json.load(r) if t.get("type") == "page"]
    except Exception:
        return None


def cdp_navigate(tab, url):
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Page.navigate", "params": {"url": url}}))
        ws.recv()
    finally:
        ws.close()


def cdp_new_tab(url):
    req = urllib.request.Request(CDP + "/json/new?" + urllib.parse.quote(url, safe=":/?&=%"), method="PUT")
    urllib.request.urlopen(req, timeout=5).close()


def cdp_eval(tab, expression):
    """Run JavaScript in a tab of the X Assistant window and return its (JSON-able) result."""
    import websocket
    ws = websocket.create_connection(tab["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": "Runtime.evaluate",
                            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {}).get("result", {}).get("value")
    finally:
        ws.close()


def cdp_close(tab_id):
    for method in ("PUT", "GET"):
        try:
            urllib.request.urlopen(urllib.request.Request(f"{CDP}/json/close/{tab_id}", method=method), timeout=3).close()
            return
        except Exception:
            continue


YT_STATE_JS = """(() => {
  const v = document.querySelector('video'), p = document.getElementById('movie_player');
  return {vid: new URLSearchParams(location.search).get('v'), t: v ? v.currentTime : 0,
          d: v && isFinite(v.duration) ? v.duration : 0, paused: v ? v.paused : true, r: v ? v.playbackRate : 1,
          ended: v ? v.ended : false, ad: !!(p && p.classList.contains('ad-showing')),
          ps: p && p.getPlayerState ? p.getPlayerState() : -2};  // player state 0 = the song itself ended
})()"""


def yt_volume_js(level, muted):
    # "Mute" is volume 0 rather than a real mute: Chrome pauses *muted* videos in minimised windows.
    level = 0 if muted else int(level)
    return f"""(() => {{
  const p = document.getElementById('movie_player');
  if (p && p.setVolume) {{ p.unMute(); p.setVolume({level}); return true; }}
  const v = document.querySelector('video');
  if (v) {{ v.muted = false; v.volume = {level / 100:.2f}; return true; }}
  return false;
}})()"""


def yt_window(tab):
    """The Chrome window currently showing `tab`."""
    title = html.unescape((tab or {}).get("title", ""))
    return next((h for h in app_windows("chrome.exe") if title and _title(h) == title), None)


def yt_command(tab, action):
    """Pause / resume / next / previous. Chrome ignores play() in a minimised window, so for anything that
    (re)starts playback the window is shown on the secondary monitor without focus, then minimised again."""
    if action == "pause":
        cdp_eval(tab, YT_CONTROL_JS[action])
        return
    previous = _u32.GetForegroundWindow()
    hwnd = yt_window(tab)
    hidden = bool(hwnd and _u32.IsIconic(hwnd))
    if hidden:
        move_to_secondary(hwnd, secondary_monitor())
        time.sleep(0.4)
    cdp_eval(tab, YT_CONTROL_JS[action])
    keep_focus(previous, hwnd, secondary_monitor(), 0.5)
    if hidden:
        time.sleep(2.5)
        _u32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
        restore_focus(previous)


YT_CONTROL_JS = {
    "pause": "document.getElementById('movie_player').pauseVideo()",
    "resume": "document.getElementById('movie_player').playVideo()",
    "next": "document.getElementById('movie_player').nextVideo()",
    "previous": "document.getElementById('movie_player').seekTo(0)",
}


class YouTubeWatcher:
    """Follows the song playing in the X Assistant Chrome tab using the real player state (not a blind timer):
    paused songs simply wait, a song changed early is followed or cancelled, and when the song has really
    finished only that tab is closed (with one tab left, that closes the song window)."""

    def __init__(self, on_finished, on_ready, on_playing=None):
        self.on_finished, self.on_ready, self.on_playing = on_finished, on_ready, on_playing
        self.gen = 0
        self.tab_id = None
        self.active = False

    def watch(self, video_id):
        self.gen += 1
        self.active = True
        threading.Thread(target=self._run, args=(self.gen, video_id), daemon=True).start()

    def cancel(self):
        self.gen += 1
        self.active = False

    def current_tab(self):
        tabs = cdp_tabs() or []
        return next((t for t in tabs if t["id"] == self.tab_id), tabs[0] if tabs else None)

    def close_current(self):
        tab = self.current_tab()
        self.cancel()
        if tab:
            cdp_close(tab["id"])

    def _run(self, gen, vid):
        tab_id, remaining, ready, find_until, minimized = None, None, False, time.time() + 40, False
        while gen == self.gen:
            time.sleep(1)
            tabs = cdp_tabs()
            if tabs is None:  # window/browser closed
                break
            tab = next((t for t in tabs if t["id"] == tab_id), None) or \
                next((t for t in tabs if f"v={vid}" in t.get("url", "")), None)
            if not tab:
                if tab_id or time.time() > find_until:  # the user closed it (or it never loaded)
                    break
                continue
            tab_id = self.tab_id = tab["id"]
            if "/shorts/" in tab.get("url", ""):  # no Shorts in the song window
                try:
                    cdp_eval(tab, "history.length > 1 ? history.back() : location.replace('https://www.youtube.com/')")
                except Exception:
                    pass
                continue
            try:
                st = cdp_eval(tab, YT_STATE_JS)
            except Exception:
                continue
            if not st or gen != self.gen:
                continue
            if st["vid"] != vid:
                if remaining is not None and remaining < 8:  # YouTube autoplayed the next video: ours ended
                    return self._finish(gen, tab_id)
                vid, remaining, ready = st["vid"], None, False  # the user picked another song in that window
                continue
            if ready and st.get("ps") == 0:  # song ended (also true while a post-roll ad is showing)
                return self._finish(gen, tab_id)
            if st["ad"]:
                continue
            if not ready and st["d"] > 0:
                ready = True
                self.on_ready(tab)
                if st["paused"] and st["t"] < 1:  # autoplay didn't kick in: press play
                    cdp_eval(tab, YT_CONTROL_JS["resume"])
            if ready and not minimized and not st["paused"] and st["t"] > 2:
                minimized = True  # playing for real now: get the window out of the way (no focus change)
                self.on_ready(tab)  # YouTube applies its own saved volume after loading: set ours again
                if self.on_playing:  # confirmed playing: announce it (once per song)
                    self.on_playing(tab)
                hwnd = yt_window(tab)
                if hwnd and not _u32.IsIconic(hwnd):
                    _u32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
            if st["ended"]:
                return self._finish(gen, tab_id)
            if st["d"] > 0:  # real seconds left (accounts for playback speed)
                remaining = (st["d"] - st["t"]) / max(st.get("r") or 1, 0.25)
        if gen == self.gen:
            self.active = False

    def _finish(self, gen, tab_id):
        if gen != self.gen:
            return
        self.active = False
        cdp_close(tab_id)
        self.on_finished()


def set_process_volume(process, level, muted):
    """Per-app volume in the Windows mixer (only that app, never the master volume)."""
    import comtypes
    from pycaw.pycaw import AudioUtilities
    comtypes.CoInitialize()
    try:
        found = False
        for session in AudioUtilities.GetAllSessions():
            if session.Process and session.Process.name().lower() == process:
                session.SimpleAudioVolume.SetMasterVolume(level / 100, None)
                session.SimpleAudioVolume.SetMute(int(muted), None)
                found = True
        return found
    finally:
        comtypes.CoUninitialize()


def keep_focus(previous, hwnd, work, seconds):
    """For a moment, keep handing focus back to `previous` and keep `hwnd` on the secondary monitor."""
    end = time.time() + seconds
    while time.time() < end:
        restore_focus(previous)
        if hwnd and work and _u32.IsWindow(hwnd) and not _u32.IsIconic(hwnd):
            rc = wt.RECT()
            _u32.GetWindowRect(hwnd, ctypes.byref(rc))
            if not (work[0] <= rc.left + 8 < work[2]):
                move_to_secondary(hwnd, work)
        time.sleep(0.05)


def dedicated_chrome_args(chrome, url, work):
    args = [chrome, f"--user-data-dir={CHROME_PROFILE}", f"--remote-debugging-port={CDP_PORT}",
            "--no-first-run", "--no-default-browser-check", "--new-window",
            "--autoplay-policy=no-user-gesture-required",  # songs must start without a click
            "--disable-sync"]  # only the Google/YouTube login lives here: never copy extensions, tabs or settings
    if work:
        args += [f"--window-position={work[0]},{work[1]}", f"--window-size={work[2] - work[0]},{work[3] - work[1]}"]
    return args + [url]


def cdp_browser(method, params=None):
    """Browser-level DevTools call on the X Assistant Chrome (None if it isn't running)."""
    import websocket
    try:
        with urllib.request.urlopen(CDP + "/json/version", timeout=1) as r:
            ws_url = json.load(r)["webSocketDebuggerUrl"]
    except Exception:
        return None
    ws = websocket.create_connection(ws_url, timeout=5, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg.get("result", {})
    finally:
        ws.close()


def youtube_signed_in():
    """True / False, or None if the X Assistant Chrome isn't running. Only checks that Google's login
    cookies exist - their values are never read out."""
    result = cdp_browser("Storage.getCookies")
    if result is None:
        return None
    names = {c["name"] for c in result.get("cookies", []) if c.get("domain", "").endswith(("youtube.com", "google.com"))}
    return bool(names & {"LOGIN_INFO", "SAPISID", "__Secure-3PAPISID"})


GOOGLE_SIGNIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"


def open_google_signin():
    """Open Google sign-in in its own X Assistant Chrome window on the secondary monitor and bring it to the
    front - the one case where Chrome is meant to take focus, because the user has to type into it."""
    chrome = find_chrome()
    if not chrome:
        webbrowser.open(GOOGLE_SIGNIN_URL)
        return
    work = secondary_monitor()
    before = set(app_windows("chrome.exe"))
    if cdp_browser("Target.createTarget", {"url": GOOGLE_SIGNIN_URL, "newWindow": True}) is None:
        import subprocess
        subprocess.Popen(dedicated_chrome_args(chrome, GOOGLE_SIGNIN_URL, work), close_fds=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        new = [h for h in app_windows("chrome.exe") if h not in before]
        if new:
            move_to_secondary(new[0], work)
            _u32.ShowWindow(new[0], 9)  # SW_RESTORE
            _u32.SetForegroundWindow(new[0])
            return
        time.sleep(0.1)


def open_in_chrome(url, minimize_when_title=None, minimize=True, tab_mode="same"):
    """Show `url` in the X Assistant Chrome window on the secondary monitor, never on top of the game.
    tab_mode "same" replaces what's playing in the current tab, "new" opens another tab.
    Once the page title matches (video loaded and playing) the window is minimised without taking focus."""
    chrome = find_chrome()
    if not chrome:
        webbrowser.open(url)
        return
    work = secondary_monitor()
    tabs = cdp_tabs()

    log_activity(f"  chrome: {'reuse window' if tabs else 'launch window'} -> {url[:80]}")
    if tabs:  # window already running: reuse it
        previous = _u32.GetForegroundWindow()
        old_title = html.unescape(tabs[0].get("title", ""))
        hwnd = next((h for h in app_windows("chrome.exe") if _title(h) == old_title), None)
        if hwnd:
            move_to_secondary(hwnd, work)  # also un-minimises it without activation, so the new video can start
        if tab_mode == "new":
            cdp_new_tab(url)
        else:
            cdp_navigate(tabs[0], url)
        keep_focus(previous, hwnd, work, 1.5)
    else:  # first time: start the dedicated window on the secondary monitor
        old_title = None
        args = dedicated_chrome_args(chrome, url, work)

        def launch():
            import subprocess
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = SW_SHOWNOACTIVATE
            subprocess.Popen(args, startupinfo=si, close_fds=True)

        hwnd, previous = open_unobtrusively(launch, "chrome.exe", new_window_only=True)

    if hwnd and minimize:
        check = minimize_when_title
        if check and old_title is not None:  # wait for the *new* page, not the song that was already there
            check = lambda t, c=minimize_when_title, old=old_title: c(t) and t != old
        threading.Thread(target=_minimize_when_loaded, args=(hwnd, previous, check), daemon=True).start()


def _minimize_when_loaded(hwnd, previous, title_check, timeout=20, linger=4):
    # Chrome won't start media in a tab that has never been visible, so wait until the page has loaded
    # (and the video had a moment to start) on the second monitor before minimising.
    deadline = time.time() + timeout
    while time.time() < deadline and _u32.IsWindow(hwnd):
        if title_check is None or title_check(_title(hwnd)):
            break
        time.sleep(0.25)
    time.sleep(linger)
    if _u32.IsWindow(hwnd):
        _u32.ShowWindow(hwnd, SW_SHOWMINNOACTIVE)
        restore_focus(previous)


def spotify_search_uri(query):
    # Spotify turns %20 into a literal "+" in its search box; a double-encoded space arrives as a real space
    return "spotify:search:" + urllib.parse.quote(query).replace("%20", "%2520")


def spotify_query(query):
    """'around me by metro boomin' -> 'around me metro boomin' ("by" / "של" confuse Spotify's ranking)."""
    return re.sub(r"\s+(by|של)\s+", " ", " " + query + " ", flags=re.I).strip()


def _tokens(text):
    return set(re.findall(r"\w+", text.lower()))


class FocusGuard:
    """While active, instantly hands the foreground back to `previous` whenever a window of `process` takes it.
    (Spotify activates its own window when it handles a spotify: link or an accessibility click.)"""

    def __init__(self, previous, process):
        self.previous, self.process, self.running = previous, process, True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        deadline = time.time() + 30
        while self.running and time.time() < deadline:
            fg = _u32.GetForegroundWindow()
            if fg and fg != self.previous and _process_name(fg) == self.process:
                restore_focus(self.previous)
            time.sleep(0.015)

    def stop(self, after=0.0):
        threading.Timer(after, lambda: setattr(self, "running", False)).start()


def spotify_is_playing(wait=4.0):
    """Spotify's window title turns from "Spotify Free" into "Artist - Song" while something plays."""
    idle = {"spotify", "spotify free", "spotify premium"}
    end = time.time() + wait
    while time.time() < end:
        if any(_title(h).lower() not in idle for h in app_windows("spotify.exe")):
            return True
        time.sleep(0.2)
    return False


def spotify_click_song(query, timeout=12):
    """Spotify Free can't be started through the Web API, so press play on the best matching song in the
    desktop app through Windows UI Automation (the accessibility interface): no mouse or keyboard input.
    Returns the song that was started, or None."""
    import psutil
    import uiautomation as auto

    def spotify_window():
        for w in auto.GetRootControl().GetChildren():
            try:
                if w.Name and w.ProcessId and psutil.Process(w.ProcessId).name().lower() == "spotify.exe":
                    return w
            except Exception:
                continue
        return None

    with auto.UIAutomationInitializerInThread():
        songs_on, deadline = False, time.time() + timeout
        while time.time() < deadline:
            try:
                win = spotify_window()
                if not win:
                    time.sleep(0.3)
                    continue
                box = win.ComboBoxControl(Name="What do you want to play?", searchDepth=40)
                if not box.Exists(1, 0.1) or box.GetValuePattern().Value.strip() != query:
                    time.sleep(0.2)
                    continue  # our search hasn't loaded yet
                if not songs_on:  # the "Songs" filter lists only tracks, as "Play <title> by <artists>"
                    chip = win.CheckBoxControl(Name="Songs", searchDepth=40)
                    if chip.Exists(1, 0.1):
                        if chip.GetTogglePattern().ToggleState != 1:
                            chip.GetTogglePattern().Toggle()
                        songs_on = True
                        time.sleep(0.8)
                    continue
                rows = []  # one pass over the tree; names are read now because the list re-renders on play
                for c, _depth in auto.WalkControl(win, maxDepth=60):
                    name = c.Name or ""
                    if c.ControlTypeName == "ButtonControl" and name.startswith("Play ") and " by " in name:
                        rows.append((c, name[5:]))
                        if len(rows) >= 8:
                            break
                if not rows:
                    continue
                wanted = _tokens(query)
                # best overlap between what was said and "title by artists"; ties keep Spotify's ranking
                button, name = max(enumerate(rows), key=lambda ir: (len(wanted & _tokens(ir[1][1])), -ir[0]))[1]
                button.GetInvokePattern().Invoke()
                return name
            except Exception:  # Spotify re-renders constantly: elements can vanish mid-read, just retry
                time.sleep(0.2)
    return None


def spotify_repeat(on, timeout=3):
    """Spotify's repeat button cycles "Enable repeat" -> "Enable repeat one" -> "Disable repeat" (= repeat one).
    Press it (accessibility interface, no mouse) until it reaches the wanted state. Returns True/False/None (no button,
    e.g. while an ad plays and Spotify swaps it for the ad buttons)."""
    import psutil
    import uiautomation as auto
    want = "Disable repeat" if on else "Enable repeat"
    with auto.UIAutomationInitializerInThread():
        win = next((w for w in auto.GetRootControl().GetChildren() if w.Name and w.ProcessId
                    and psutil.Process(w.ProcessId).name().lower() == "spotify.exe"), None)
        if not win:
            return None
        end = time.time() + timeout
        for _ in range(4):
            button = None
            while time.time() < end and button is None:
                for c, _d in auto.WalkControl(win, maxDepth=60):
                    if c.ControlTypeName in ("ButtonControl", "CheckBoxControl") and \
                            (c.Name or "").startswith(("Enable repeat", "Disable repeat")):
                        button = c
                        break
                else:
                    time.sleep(0.3)
            if button is None:
                return None
            if button.Name == want:
                return True
            button.GetInvokePattern().Invoke()
            time.sleep(0.5)
    return False


def open_spotify_uri(uri):
    """Open a spotify: URI in the desktop app on the secondary monitor without stealing focus."""
    # short settle: FocusGuard (in Spotify.play_in_app) keeps handing focus back for the whole operation
    open_unobtrusively(lambda: os.startfile(uri), "spotify.exe", new_window_only=False, settle=0.3)


# ---------------------------------------------------------------- spoken confirmation

def clean_song_title(raw, platform):
    """'Drake - God's Plan (Official Video) - YouTube' -> "God's Plan";
    'Around Me (feat. Don Toliver) by Metro Boomin' -> 'Around Me'."""
    t = html.unescape(raw or "")
    t = re.sub(r"^\(\d+\)\s*", "", t)                        # "(43) " notification counter
    t = re.sub(r"\s*-\s*YouTube$", "", t)
    if platform == "spotify":
        t = t.split(" by ")[0]
    t = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t)             # (Official Video), [Lyrics], (feat. X)
    parts = [x.strip() for x in re.split(r"\s[-–—|]\s", t) if x.strip()]
    if platform == "youtube" and len(parts) >= 2:
        t = parts[1]                                           # "Artist - Song"
    elif parts:
        t = parts[0]
    t = re.sub(r"\b(official|music|video|audio|lyrics?|visuali[sz]er|hd|4k|remastered)\b", "", t, flags=re.I)
    t = re.sub(r"\s+(ft\.?|feat\.?|featuring)\s.*$", "", t, flags=re.I)  # "Around Me ft. Don Toliver"
    t = re.sub(r"\s{2,}", " ", t).strip(" -–|")
    if t.isupper():  # "PASSIONFRUIT" would be spelled out letter by letter
        t = t.title()
    return t or (raw or "").strip()


def requested_title(query):
    """The song name the user asked for: "God's Plan by Drake" -> "God's Plan". Never empty."""
    t = re.sub(r"^(the\s+song\s+|השיר\s+)", "", query.strip(), flags=re.I)
    t = re.sub(r"\s+(by|של)\s+.*$", "", t, flags=re.I).strip(" .,!?")
    return t or query.strip() or "your song"


PLATFORM_SPOKEN = {"en": {"youtube": "on YouTube", "spotify": "on Spotify"},
                   "he": {"youtube": "ביוטיוב", "spotify": "בספוטיפיי"}}


def confirmation_text(language, title, platform):
    lang = "he" if language.startswith("he") else "en"
    where = PLATFORM_SPOKEN[lang][platform]
    if lang == "he":
        return f"גוטצ׳ה, {title} מתנגן עכשיו {where}."
    return f"Gotchu, {title} is now playing {where}."


def failure_text(language, title, platform):
    lang = "he" if language.startswith("he") else "en"
    where = PLATFORM_SPOKEN[lang][platform]
    if lang == "he":
        return f"סליחה, לא הצלחתי לנגן את {title} {where}."
    return f"Sorry, I couldn't play {title} {where}."


class Speaker:
    """Short spoken replies in a natural female voice: ElevenLabs if a key is set, otherwise Microsoft's
    neural voice (edge-tts). Audio is synthesised first, so the music is only ducked while she actually talks."""

    ELEVEN_FALLBACK_VOICE = "EXAVITQu4vr4xnSDxMaL"  # used only if the automatic pick fails

    def __init__(self, app):
        self.app = app
        self.lock = threading.Lock()

    def say(self, text, language="en-US"):
        threading.Thread(target=self._say, args=(text, language), daemon=True).start()

    def _say(self, text, language):
        with self.lock:  # one reply at a time, never overlapping or repeating
            path = os.path.join(os.environ.get("TEMP", BASE_DIR), f"xa_say_{int(time.time() * 1000)}.mp3")
            try:
                if not self._synth(text, path, language):
                    return
                self.app.duck_music(True)
                try:
                    self._play(path)
                finally:
                    self.app.duck_music(False)
            except Exception as e:
                log_exception(type(e), e, e.__traceback__)
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _synth(self, text, path, language):
        cfg = self.app.cfg
        if cfg.get("elevenlabs_api_key"):
            try:
                return self._elevenlabs(text, path, cfg, language)
            except Exception as e:  # bad key / quota / offline: fall back to the neural voice
                log_exception(type(e), e, e.__traceback__)
        import asyncio
        import edge_tts
        voice = cfg.get("tts_voice") or "en-US-EmmaMultilingualNeural"
        asyncio.run(edge_tts.Communicate(text, voice, rate="-4%").save(path))
        return True

    def _elevenlabs(self, text, path, cfg, language):
        key = cfg["elevenlabs_api_key"]
        voice = cfg.get("elevenlabs_voice_id") or self._pick_eleven_voice(key)
        # eleven_v3 covers Hebrew; multilingual_v2 is the most natural for English
        model = "eleven_v3" if language.startswith("he") else "eleven_multilingual_v2"
        body = json.dumps({"text": text, "model_id": model,
                           "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.15,
                                              "use_speaker_boost": True}}).encode()
        req = urllib.request.Request(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                                     data=body, method="POST",
                                     headers={"xi-api-key": key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15) as r, open(path, "wb") as f:
            f.write(r.read())
        return True

    def _pick_eleven_voice(self, key):
        """Most natural conversational female voice on the account; saved to config so it can be changed."""
        try:
            req = urllib.request.Request("https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": key})
            with urllib.request.urlopen(req, timeout=10) as r:
                voices = json.load(r).get("voices", [])

            def score(v):
                labels = " ".join(str(x) for x in (v.get("labels") or {}).values()).lower()
                desc = (v.get("description") or "").lower() + " " + labels
                return (("conversational" in desc) * 3 + ("natural" in desc) * 2
                        + sum(w in desc for w in ("warm", "calm", "casual", "friendly", "soft"))
                        - sum(w in desc for w in ("energetic", "hyped", "intense", "robotic", "narration")))

            female = [v for v in voices if (v.get("labels") or {}).get("gender", "").lower() == "female"]
            voice = max(female, key=score)["voice_id"] if female else self.ELEVEN_FALLBACK_VOICE
        except Exception:
            voice = self.ELEVEN_FALLBACK_VOICE
        self.app.cfg["elevenlabs_voice_id"] = voice
        save_config(self.app.cfg)
        return voice

    @staticmethod
    def _play(path):
        """Blocking MP3 playback through Windows MCI (no extra audio libraries)."""
        mci = ctypes.windll.winmm.mciSendStringW
        alias = f"xa{int(time.time() * 1000) % 100000}"
        if mci(f'open "{path}" type mpegvideo alias {alias}', None, 0, None) != 0:
            return
        try:
            mci(f"play {alias} wait", None, 0, None)
        finally:
            mci(f"close {alias}", None, 0, None)


# ---------------------------------------------------------------- players

def is_youtube_video_title(title):
    return title.endswith(" - YouTube") and title != "YouTube"


YT_VIDEOS_ONLY = "&sp=EgIQAQ%253D%253D"  # YouTube's "Videos" filter: no Shorts, channels or playlists


def pick_youtube_video(html):
    """First normal video in the search results: never a Short (and nothing under a minute)."""
    shorts = set(re.findall(r"/shorts/([\w-]{11})", html))
    for m in re.finditer(r'"videoRenderer":\{"videoId":"([\w-]{11})"', html):
        vid = m.group(1)
        if vid in shorts:
            continue
        chunk = html[m.end(): m.end() + 6000]
        length = re.search(r'"lengthText":\{.{0,300}?"simpleText":"([\d:]+)"', chunk)
        if length:
            parts = [int(x) for x in length.group(1).split(":")]
            seconds = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
            if seconds <= 60:
                continue
        return vid
    return None


def play_youtube(query, minimize=True, tab_mode="same"):
    url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(query) + YT_VIDEOS_ONLY
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
    html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
    vid = pick_youtube_video(html)
    ids = [vid] if vid else []
    if not ids:
        open_in_chrome(url, minimize=False, tab_mode=tab_mode)  # search results need a click
        return f"Opened YouTube search for “{query}” on your second screen", None
    # minimize=False here: YouTubeWatcher minimises the window once it sees the song actually playing
    open_in_chrome(f"https://www.youtube.com/watch?v={ids[0]}", is_youtube_video_title, False, tab_mode)
    return f"Playing “{query}” on YouTube", ids[0]


class Spotify:
    def __init__(self, cfg):
        self.cfg = cfg
        self._sp = None

    @property
    def configured(self):
        return bool(self.cfg["spotify_client_id"] and self.cfg["spotify_client_secret"])

    def client(self):
        if self._sp is None:
            import spotipy
            from spotipy.oauth2 import SpotifyOAuth

            self._sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
                client_id=self.cfg["spotify_client_id"],
                client_secret=self.cfg["spotify_client_secret"],
                redirect_uri=self.cfg["spotify_redirect_uri"],
                scope="user-read-playback-state user-modify-playback-state",
                cache_path=SPOTIFY_CACHE,
                open_browser=True,
            ))
        return self._sp

    def _device(self, sp):
        devices = sp.devices().get("devices", [])
        if not devices:
            open_spotify_uri("spotify:")  # launch the desktop app (second screen, no focus) and wait for it
            for _ in range(15):
                time.sleep(1)
                devices = sp.devices().get("devices", [])
                if devices:
                    break
        if not devices:
            return None
        active = [d for d in devices if d.get("is_active")]
        return (active or devices)[0]["id"]

    last_started = None  # title of the song this object last started successfully (for the spoken reply)

    def play(self, query):
        self.last_started = None
        if not self.configured:
            return self.play_in_app(query)
        sp = self.client()
        results = sp.search(q=query, type="track", limit=1)["tracks"]["items"]
        if not results:
            return f"Couldn't find “{query}” on Spotify"
        track = results[0]
        name = f"{track['name']} – {', '.join(a['name'] for a in track['artists'])}"
        try:
            sp.start_playback(device_id=self._device(sp), uris=[track["uri"]])
            self.last_started = track["name"]
            return f"Playing {name} on Spotify"
        except Exception:
            # Spotify Free can't be controlled through the API: press play in the desktop app instead
            return self.play_in_app(f"{track['name']} {track['artists'][0]['name']}")

    def play_in_app(self, query):
        """No API (or Spotify Free): search in the desktop app and press play on the best match."""
        query = spotify_query(query)
        guard = FocusGuard(_u32.GetForegroundWindow(), "spotify.exe")  # keep the game in front throughout
        try:
            try:
                open_spotify_uri(spotify_search_uri(query))
            except OSError:  # desktop app not installed
                open_in_chrome("https://open.spotify.com/search/" + urllib.parse.quote(query), minimize=False)
                return f"Opened “{query}” on Spotify web – click play"
            song = spotify_click_song(query)
        finally:
            guard.stop(after=2.0)
        if song and spotify_is_playing():
            self.last_started = song
            return f"Playing {song} on Spotify"
        return f"Opened “{query}” in Spotify – couldn't press play, click it there"

    def set_repeat(self, on):
        if self.configured:
            try:
                self.client().repeat("track" if on else "off")
                return "Looping this song on Spotify" if on else "Loop off"
            except Exception:
                pass  # Spotify Free: use the desktop app's repeat button
        guard = FocusGuard(_u32.GetForegroundWindow(), "spotify.exe")
        try:
            result = spotify_repeat(on)
        finally:
            guard.stop(after=1.0)
        if result is None:
            return "Spotify's repeat button isn't there right now (an ad may be playing) – try again after it"
        if not result:
            return "Couldn't change Spotify's repeat"
        return "Looping this song on Spotify" if on else "Loop off"

    def set_volume(self, level, muted):
        if self.configured:
            try:
                self.client().volume(0 if muted else int(level))
                return
            except Exception:
                pass  # no Premium / no active device: fall back to the Windows per-app mixer
        set_process_volume("spotify.exe", level, muted)

    def control(self, action):
        if not self.configured:
            return "Spotify controls need API keys in settings"
        sp = self.client()
        {"pause": sp.pause_playback, "resume": sp.start_playback,
         "next": sp.next_track, "previous": sp.previous_track}[action]()
        return {"pause": "Paused", "resume": "Resumed", "next": "Next track", "previous": "Previous track"}[action]


# ---------------------------------------------------------------- main window

class App(Frameless):
    W, H = 420, 234
    PLAT_Y = 180
    VOL_Y = 214

    def __init__(self):
        self.cfg = load_config()
        self.spotify = Spotify(self.cfg)
        self.now_playing = None          # "youtube" / "spotify" / None: where volume + controls go
        self.volume = int(self.cfg["volume"])
        self.muted = False
        self.volume_jobs = queue.Queue()
        self.youtube = YouTubeWatcher(on_finished=self._song_finished, on_ready=self._song_ready)
        self.speaker = Speaker(self)
        threading.Thread(target=self._volume_worker, daemon=True).start()
        self.events = queue.Queue()
        self.busy = False
        self.level = 0.0
        self.shown_level = 0.0
        self.state = "idle"
        self.pulse = 0
        self.reset_at = None
        self.hotkey_handle = None
        self.settings = None

        self.root = tk.Tk()
        self.root.report_callback_exception = log_exception
        self.root.title(APP_NAME)
        icon = os.path.join(RES_DIR, "icon.ico")
        if os.path.exists(icon):
            self.root.iconbitmap(default=icon)
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"{self.W}x{self.H}+{sw - self.W - 40}+60")
        self.setup_window(self.root, self.W, self.H)
        self.root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        style_ttk(self.root)

        cv = self.cv
        self.orb_frames = {}
        self.orb = cv.create_image(76, 82, image=self.orb_frame("idle", 0), tags="orb")
        cv.tag_bind("orb", "<Button-1>", lambda e: self.trigger())
        cv.tag_bind("orb", "<Enter>", lambda e: cv.config(cursor="hand2"))
        cv.tag_bind("orb", "<Leave>", lambda e: cv.config(cursor=""))

        x0 = 150
        cv.create_text(x0, 36, text="X", anchor="w", fill="#c77dff", font=(FONT, 16, "bold"))
        cv.create_text(x0 + 17, 37, text="Assistant", anchor="w", fill=TEXT, font=(FONT, 14))
        self.chip = cv.create_image(x0 + 118, 37, anchor="w")
        self.chip_text = cv.create_text(0, 37, fill=TEXT, font=(FONT, 8, "bold"))
        self.status = cv.create_text(x0, 72, text="Ready", anchor="w", fill=TEXT, font=(FONT, 13, "bold"))
        self.detail = cv.create_text(x0, 95, anchor="nw", fill=MUTED, font=(FONT, 9), width=self.W - x0 - 20)
        self.meter_track = cv.create_image(x0, 144, anchor="w", state="hidden",
                                           image=self.keep(pill_image(self.W - x0 - 24, 6, (255, 255, 255, 40))))
        self.meter_fill = cv.create_image(x0, 144, anchor="w", state="hidden")
        self.meter_cache = {}

        cv.create_text(22, 17, text=CREDIT, anchor="w", fill="#9a8cc4", font=(FONT, 7))
        self.icon_button(self.W - 100, 22, "⌨", self.toggle_typing, 13)
        self.icon_button(self.W - 74, 22, "⚙", self.open_settings, 13)
        self.icon_button(self.W - 48, 21, "–", self.minimize, 13)
        self.icon_button(self.W - 22, 22, "✕", self.quit, 11)

        # type a command instead of speaking (Enter to run, Esc to close)
        self.type_var = tk.StringVar()
        self.type_bg = cv.create_image(x0 - 6, 142, anchor="w", state="hidden",
                                       image=self.keep(field_image(self.W - x0 - 14, 30)))
        self.type_entry = tk.Entry(self.root, textvariable=self.type_var, bg=FIELD, fg=TEXT, insertbackground=TEXT,
                                   relief="flat", bd=0, font=(FONT, 10))
        self.type_win = cv.create_window(x0 + 4, 142, anchor="w", window=self.type_entry,
                                         width=self.W - x0 - 34, height=22, state="hidden")
        self.type_entry.bind("<Return>", lambda e: self.run_typed())
        self.type_entry.bind("<Escape>", lambda e: self.toggle_typing(False))

        self._build_platform_toggle()
        self._build_volume_slider()
        self.apply_config()
        self.root.after(40, self.tick)

    # ---- where songs play: this toggle is the single source of truth (Settings shows the same choice)

    def _build_platform_toggle(self):
        cv, y = self.cv, self.PLAT_Y
        cv.create_text(24, y, text="Play on", anchor="w", fill=MUTED, font=(FONT, 9))
        self.plat_imgs = {}
        self.plat_btns = {}
        for i, (key, label) in enumerate((("youtube", "YouTube"), ("spotify", "Spotify"))):
            x = 84 + i * 108
            tag = f"plat_{key}"
            img = cv.create_image(x, y, anchor="w", tags=tag)
            txt = cv.create_text(x + 50, y, text=label, fill=TEXT, font=(FONT, 9, "bold"), tags=tag)
            cv.tag_bind(tag, "<Button-1>", lambda e, k=key: self.set_platform(k))
            cv.tag_bind(tag, "<Enter>", lambda e: cv.config(cursor="hand2"))
            cv.tag_bind(tag, "<Leave>", lambda e: cv.config(cursor=""))
            self.plat_btns[key] = img
        looks = {"glass": dict(fill=(255, 255, 255, 28), outline=(255, 255, 255, 60)),
                 "youtube": dict(fill=None, gradient=((255, 40, 40), (255, 90, 60))),
                 "spotify": dict(fill=None, gradient=((20, 190, 90), (40, 225, 120)))}
        for kind, look in looks.items():
            self.plat_imgs[kind] = self.keep(pill_image(100, 26, **look))
        self.draw_platform_toggle()

    def draw_platform_toggle(self):
        for key, img in self.plat_btns.items():
            self.cv.itemconfig(img, image=self.plat_imgs[key if key == self.cfg["default_platform"] else "glass"])

    def set_platform(self, platform):
        if self.cfg["default_platform"] != platform:
            self.cfg["default_platform"] = platform
            save_config(self.cfg)
        self.draw_platform_toggle()
        if self.settings and self.settings.win.winfo_exists() and self.settings.platform != platform:
            self.settings.set_platform(platform)
        if not self.busy:
            self.set_state("idle", "Ready", self.idle_detail())

    # ---- song volume (only the song: YouTube player / Spotify, never the Windows master volume)

    def _build_volume_slider(self):
        cv, y = self.cv, self.VOL_Y
        self.vol_x0, self.vol_x1 = 54, self.W - 66
        self.vol_icon = cv.create_text(28, y, text="\uE767", fill=MUTED, font=("Segoe MDL2 Assets", 13))
        cv.tag_bind(self.vol_icon, "<Button-1>", lambda e: self.set_volume(muted=not self.muted))
        cv.tag_bind(self.vol_icon, "<Enter>", lambda e: cv.itemconfig(self.vol_icon, fill=TEXT))
        cv.tag_bind(self.vol_icon, "<Leave>", lambda e: cv.itemconfig(self.vol_icon, fill=MUTED))
        cv.create_image(self.vol_x0, y, anchor="w",
                        image=self.keep(pill_image(self.vol_x1 - self.vol_x0, 6, (255, 255, 255, 45))))
        self.vol_fill = cv.create_image(self.vol_x0, y, anchor="w")
        knob = Image.new("RGBA", (48, 48), (0, 0, 0, 0))
        ImageDraw.Draw(knob).ellipse((6, 6, 42, 42), fill=PINK + (120,))
        knob = knob.filter(ImageFilter.GaussianBlur(5))
        ImageDraw.Draw(knob).ellipse((12, 12, 36, 36), fill="white")
        self.vol_knob = cv.create_image(0, y, image=self.keep(knob.resize((24, 24), Image.LANCZOS)))
        self.vol_text = cv.create_text(self.W - 22, y, anchor="e", fill=TEXT, font=(FONT, 10, "bold"))
        self.vol_cache = {}
        cv.bind("<MouseWheel>", lambda e: self.set_volume(self.volume + (5 if e.delta > 0 else -5)))
        self._draw_volume()

    def _draw_volume(self):
        shown = 0 if self.muted else self.volume
        span = self.vol_x1 - self.vol_x0
        w = max(6, int(span * shown / 100) // 2 * 2)
        if w not in self.vol_cache:
            self.vol_cache[w] = ImageTk.PhotoImage(pill_image(w, 6, None, gradient=(VIOLET, PINK)))
        self.cv.itemconfig(self.vol_fill, image=self.vol_cache[w])
        self.cv.coords(self.vol_knob, self.vol_x0 + span * shown / 100, self.VOL_Y)
        self.cv.itemconfig(self.vol_text, text="Muted" if self.muted else f"{self.volume}%")
        self.cv.itemconfig(self.vol_icon, text="\uE74F" if self.muted else "\uE767")

    def handle_press(self, e):
        if abs(e.y - self.VOL_Y) <= 12 and self.vol_x0 - 10 <= e.x <= self.vol_x1 + 10:
            self.handle_drag(e)
            return True
        return False

    def handle_drag(self, e):
        frac = (e.x - self.vol_x0) / (self.vol_x1 - self.vol_x0)
        self.set_volume(round(max(0.0, min(1.0, frac)) * 100), muted=False)

    def set_volume(self, level=None, muted=None):
        """Main-thread entry point for the slider, mouse wheel and voice commands."""
        if level is not None:
            self.volume = int(max(0, min(100, level)))
        if muted is not None:
            self.muted = muted
        self._draw_volume()
        self.volume_jobs.put((self.volume, self.muted))
        if self.cfg.get("volume") != self.volume:
            self.cfg["volume"] = self.volume
            if getattr(self, "_save_pending", None):
                self.root.after_cancel(self._save_pending)
            self._save_pending = self.root.after(800, lambda: save_config(self.cfg))

    def _volume_worker(self):
        while True:
            job = self.volume_jobs.get()
            while not self.volume_jobs.empty():  # only the latest slider position matters
                job = self.volume_jobs.get()
            try:
                self._apply_volume(*job)
            except Exception as e:
                log_exception(type(e), e, e.__traceback__)

    def detect_now_playing(self):
        """What's playing in the background when this app didn't start it (or was restarted)."""
        tabs = cdp_tabs()
        if tabs and any("watch?v=" in t.get("url", "") for t in tabs):
            return "youtube"
        idle = {"spotify", "spotify free", "spotify premium"}
        if any(_title(h).lower() not in idle for h in app_windows("spotify.exe")):  # title = "Artist - Song"
            return "spotify"
        return None

    def _apply_volume(self, level, muted):
        if self.now_playing is None or (self.now_playing == "youtube" and not self.youtube.current_tab()):
            self.now_playing = self.detect_now_playing()
        if self.now_playing == "youtube":
            tab = self.youtube.current_tab()
            if tab:
                cdp_eval(tab, yt_volume_js(level, muted))
        elif self.now_playing == "spotify":
            self.spotify.set_volume(level, muted)

    def duck_music(self, duck):
        """Lower the song while XAssistant talks, then fade it back to the user's volume."""
        if self.muted:
            return
        low = max(4, int(self.volume * 0.3))
        steps = 4 if duck else 8
        start, end = (self.volume, low) if duck else (low, self.volume)
        for i in range(1, steps + 1):
            try:
                self._apply_volume(round(start + (end - start) * i / steps), False)
            except Exception:
                return
            time.sleep(0.05 if duck else 0.09)

    def _song_ready(self, tab):
        # a new video started: give it the slider's volume
        cdp_eval(tab, yt_volume_js(self.volume, self.muted))

    def _song_finished(self):
        self.now_playing = None
        self.ui("done", "Song finished", "Closed the song window", 6)

    # ---- visuals

    def orb_frame(self, state, level):
        key = (state, level)
        if key not in self.orb_frames:
            c1, c2, _ = ORB_STATES[state]
            self.orb_frames[key] = ImageTk.PhotoImage(orb_image(128, 64, c1, c2, 0.35 + 0.65 * level / 7))
        return self.orb_frames[key]

    def meter_image(self, frac):
        w = max(6, int((self.W - 174) * frac) // 4 * 4)
        if w not in self.meter_cache:
            self.meter_cache[w] = ImageTk.PhotoImage(pill_image(w, 6, None, gradient=(VIOLET, PINK)))
        return self.meter_cache[w]

    def set_state(self, state, status, detail="", reset_after=None):
        self.state = state
        self.cv.itemconfig(self.status, text=status)
        self.cv.itemconfig(self.detail, text=detail)
        listening = "normal" if state == "listening" else "hidden"
        self.cv.itemconfig(self.meter_track, state=listening)
        self.cv.itemconfig(self.meter_fill, state=listening)
        self.reset_at = time.time() + reset_after if reset_after else None

    def idle_detail(self):
        app = "YouTube" if self.cfg["default_platform"] == "youtube" else "Spotify"
        return f"Press {self.cfg['hotkey'].upper()} and say a song + artist (plays on {app})"

    def tick(self):
        while not self.events.empty():
            kind, *args = self.events.get()
            if kind == "trigger":
                self.trigger()
            elif kind == "volume":
                self.set_volume(*args)
            else:
                self.set_state(*args)

        if self.reset_at and time.time() > self.reset_at and not self.busy:
            self.set_state("idle", "Ready", self.idle_detail())

        pulses = ORB_STATES[self.state][2]
        if pulses:
            self.pulse = (self.pulse + 1) % 14
            glow = 7 - abs(7 - self.pulse)
            if self.state == "listening":
                glow = max(glow // 2, min(7, int(self.shown_level * 9)))
        else:
            glow = 3
        self.cv.itemconfig(self.orb, image=self.orb_frame(self.state, glow))

        self.shown_level = self.shown_level * 0.55 + self.level * 0.45
        if self.state == "listening":
            self.cv.itemconfig(self.meter_fill, image=self.meter_image(min(1.0, self.shown_level)))
        self.root.after(40, self.tick)

    # ---- config

    def apply_config(self):
        self.spotify = Spotify(self.cfg)
        self.root.attributes("-topmost", bool(self.cfg["always_on_top"]))
        label = self.cfg["hotkey"].upper()
        w = 16 + 7 * len(label)
        self.cv.itemconfig(self.chip, image=self.keep(pill_image(w, 18, (255, 255, 255, 38),
                                                                  outline=(255, 255, 255, 70))))
        self.cv.coords(self.chip_text, 150 + 118 + w / 2, 37)
        self.cv.itemconfig(self.chip_text, text=label)
        if self.hotkey_handle is not None:
            keyboard.remove_hotkey(self.hotkey_handle)
            self.hotkey_handle = None
        try:
            self.hotkey_handle = keyboard.add_hotkey(self.cfg["hotkey"], lambda: self.events.put(("trigger",)))
            self.set_state("idle", "Ready", self.idle_detail())
        except Exception as e:
            self.set_state("error", "Hotkey error", str(e))

    def open_settings(self):
        if self.settings and self.settings.win.winfo_exists():
            self.settings.win.lift()
            return
        self.settings = Settings(self)

    def quit(self):
        if self.settings and self.settings.win.winfo_exists():
            self.settings.close()
        self.root.destroy()

    # ---- listening

    def ui(self, *args):
        self.events.put(("state", *args))

    def trigger(self):
        if self.busy:
            return
        if self.cv.itemcget(self.type_win, "state") == "normal":
            self.toggle_typing(False)
        self.busy = True
        self.set_state("listening", "Listening…", "Say a song + artist, e.g. “God’s Plan by Drake”")
        threading.Thread(target=self.listen_and_run, daemon=True).start()

    def record(self, mic, start_timeout=8, max_seconds=12, end_silence=1.0):
        """Record one phrase while publishing the live input level for the meter."""
        with mic as src:
            chunk, rate, width = src.CHUNK, src.SAMPLE_RATE, src.SAMPLE_WIDTH
            per_sec = rate / chunk
            ambient = [audioop.rms(src.read(), width) for _ in range(max(1, int(per_sec * 0.35)))]
            threshold = sum(ambient) / len(ambient) * 1.6 + 180
            scale = max(threshold * 3, 2500)

            preroll = collections.deque(maxlen=int(per_sec * 0.4))
            frames, started, quiet, waited, heard = [], False, 0, 0, 0
            while True:
                buf = src.read()
                lvl = audioop.rms(buf, width)
                self.level = min(1.0, lvl / scale)
                if not started:
                    preroll.append(buf)
                    waited += 1
                    if lvl > threshold:
                        started, frames = True, list(preroll)
                    elif waited > per_sec * start_timeout:
                        raise sr.WaitTimeoutError()
                    continue
                frames.append(buf)
                heard += 1
                quiet = quiet + 1 if lvl < threshold else 0
                if quiet > per_sec * end_silence or heard > per_sec * max_seconds:
                    break
        self.level = 0.0
        return sr.AudioData(b"".join(frames), rate, width)

    def toggle_typing(self, show=None):
        show = self.cv.itemcget(self.type_win, "state") == "hidden" if show is None else show
        state = "normal" if show else "hidden"
        self.cv.itemconfig(self.type_bg, state=state)
        self.cv.itemconfig(self.type_win, state=state)
        if show:
            self.set_state("idle", "Type a command", "e.g. around me metro boomin via spotify  ↵")
            self.type_entry.focus_set()
        else:
            self.type_var.set("")
            self.set_state("idle", "Ready", self.idle_detail())

    def run_typed(self):
        text = self.type_var.get().strip()
        if not text or self.busy:
            return
        self.busy = True
        self.type_var.set("")
        self.set_state("thinking", "Working…", f"“{text}”")
        threading.Thread(target=self.run_command, args=(text,), daemon=True).start()

    def run_command(self, text):
        try:
            self.execute(text)
        except Exception as e:
            log_exception(type(e), e, e.__traceback__)
            self.ui("error", "Error", str(e)[:120], 6)
        finally:
            self.busy = False

    def execute(self, heard):
        action, query, platform = parse_command(heard, self.cfg["default_platform"])
        log_activity(f"heard {heard!r} -> {action} {query!r} (toggle: {self.cfg['default_platform']})")
        try:
            self._execute(action, query, heard)
        except Exception as e:
            log_activity(f"  FAILED: {type(e).__name__}: {e}")
            raise

    def _execute(self, action, query, heard):
        if action == "play":
            # The YouTube / Spotify toggle is the single source of truth for where it plays (and what she says).
            platform = self.cfg["default_platform"]
            title, language = requested_title(query), self.cfg["language"]
            if self.cfg.get("voice_confirmation", True):  # speak *while* the song is being opened
                self.speaker.say(confirmation_text(language, title, platform), language)
            ok, msg = False, ""
            try:
                if platform == "youtube":
                    msg, video_id = play_youtube(query, tab_mode=self.cfg["chrome_tab_mode"])
                    self.now_playing = "youtube"
                    ok = bool(video_id)
                    if video_id:
                        self.youtube.watch(video_id)
                    else:
                        self.youtube.cancel()
                else:
                    if self.youtube.active:  # switching to Spotify: the YouTube song window is no longer needed
                        self.youtube.close_current()
                    msg = self.spotify.play(query)
                    self.now_playing = "spotify"
                    self.volume_jobs.put((self.volume, self.muted))
                    ok = bool(self.spotify.last_started)
            finally:
                if not ok and self.cfg.get("voice_confirmation", True):  # never leave a false "now playing"
                    self.speaker.say(failure_text(language, title, platform), language)
            log_activity(f"  {'ok' if ok else 'NOT PLAYING'}: {msg}")
            if not ok:
                self.ui("error", "Couldn't play it", msg, 8)
                return
        elif action == "volume":
            self.events.put(("volume", query, False))
            msg = f"Song volume {query}%"
        elif action == "volume_help":
            self.ui("error", "Which volume?", "Say a number, e.g. “change song volume to 40”", 6)
            return
        elif action == "volume_step":
            level = max(0, min(100, self.volume + query))
            self.events.put(("volume", level, False))
            msg = f"Song volume {level}%"
        elif action in ("mute", "unmute"):
            self.events.put(("volume", None, action == "mute"))
            msg = "Music muted" if action == "mute" else "Music unmuted"
        elif action in ("loop", "unloop"):
            on = action == "loop"
            source = self.now_playing
            if source is None or (source == "youtube" and not self.youtube.current_tab()):
                source = self.now_playing = self.detect_now_playing()
            if source == "youtube":
                looped = cdp_eval(self.youtube.current_tab(),
                                  f"(() => {{ const v = document.querySelector('video'); if (!v) return null; "
                                  f"v.loop = {'true' if on else 'false'}; return v.loop; }})()")
                msg = ("Looping this song on YouTube" if on else "Loop off") if looped is not None \
                    else "Couldn't find the YouTube player"
            elif source == "spotify":
                msg = self.spotify.set_repeat(on)
            else:
                self.ui("error", "Nothing playing", "Start a song first, then say “loop”", 6)
                return
        elif action:
            if self.now_playing == "youtube" and self.youtube.current_tab():
                yt_command(self.youtube.current_tab(), action)
                msg = {"pause": "Paused", "resume": "Resumed", "next": "Next video",
                       "previous": "Restarted the song"}[action]
            else:
                msg = self.spotify.control(action)
        else:
            self.ui("error", "Didn't get a song", f"Heard: “{heard}”", 6)
            return
        self.ui("done", "Done ✓", msg, 8)

    def listen_and_run(self):
        try:
            audio = self.record(open_microphone(self.cfg["microphone"], self.cfg["capture_mode"]))
            self.ui("thinking", "Thinking…", "")
            heard = recognize(audio, self.cfg["language"])
            self.ui("thinking", "Heard", f"“{heard}”")
            self.execute(heard)
        except sr.WaitTimeoutError:
            self.ui("error", "Didn't hear anything",
                    f"Check the mic in ⚙ settings (now: {self.cfg['microphone']})", 6)
        except sr.UnknownValueError:
            self.ui("error", "Didn't catch that", "Run ⚙ → Mic check so the app picks a capture mode that works", 8)
        except sr.RequestError:
            self.ui("error", "No connection", "Speech service unreachable", 5)
        except OSError as e:
            self.ui("error", "Microphone problem", str(e)[:100], 6)
        except Exception as e:
            log_exception(type(e), e, e.__traceback__)
            self.ui("error", "Error", str(e)[:120], 6)
        finally:
            self.level = 0.0
            self.busy = False

    def run(self):
        if "--run" in sys.argv:  # test switch: XAssistant.exe --run "play starboy"
            text = sys.argv[sys.argv.index("--run") + 1]
            self.root.after(2000, lambda: threading.Thread(target=self.run_command, args=(text,), daemon=True).start())
        self.root.mainloop()


# ---------------------------------------------------------------- settings window

def style_ttk(root):
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure("Glass.TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT,
                 arrowcolor=TEXT, bordercolor=FIELD, lightcolor=FIELD, darkcolor=FIELD,
                 selectbackground=FIELD, selectforeground=TEXT, padding=(6, 2))
    st.map("Glass.TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)],
           background=[("active", FIELD), ("readonly", FIELD)], selectbackground=[("readonly", FIELD)])
    root.option_add("*TCombobox*Listbox.background", "#1d1233")
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", "#7c3aed")
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))


class Settings(Frameless):
    W, H = 420, 732

    def __init__(self, app):
        self.app = app
        self.cfg = dict(app.cfg)
        self.stop_test = threading.Event()
        self.test_level = 0.0
        self.shown = 0.0
        self.checking = False

        win = tk.Toplevel(app.root)
        x = app.root.winfo_x() + app.W - self.W
        y = app.root.winfo_y() + app.H + 12
        if y + self.H > win.winfo_screenheight() - 40:
            y = max(20, app.root.winfo_y() - self.H - 12)
        win.geometry(f"{self.W}x{self.H}+{max(10, x)}+{y}")
        self.setup_window(win, self.W, self.H, in_taskbar=False)
        app.root.attributes("-topmost", False)
        cv = self.cv
        L = 28
        FW = self.W - 2 * L

        cv.create_text(L, 34, text="Settings", anchor="w", fill=TEXT, font=(FONT, 15, "bold"))
        cv.create_text(self.W - 48, 34, text=CREDIT, anchor="e", fill="#9a8cc4", font=(FONT, 8))
        self.icon_button(self.W - 24, 24, "✕", self.close, 11)

        # microphone + live test meter
        self.label(L, 74, "Microphone")
        mics = [DEFAULT_MIC] + list(list_microphones())
        self.mic_var = tk.StringVar(value=self.cfg["microphone"] if self.cfg["microphone"] in mics else DEFAULT_MIC)
        self.combo(L, 100, FW, self.mic_var, mics)
        self.mic_var.trace_add("write", lambda *a: self.start_test())
        cv.create_image(L, 132, anchor="w", image=self.keep(pill_image(FW, 6, (255, 255, 255, 40))))
        self.meter = cv.create_image(L, 132, anchor="w")
        self.meter_cache = {}
        play = cv.create_text(self.W - L, 74, text="▶ play last recording", anchor="e", fill="#c77dff",
                              font=(FONT, 9, "underline"))
        cv.tag_bind(play, "<Button-1>", lambda e: play_last_recording())
        cv.tag_bind(play, "<Enter>", lambda e: cv.config(cursor="hand2"))
        cv.tag_bind(play, "<Leave>", lambda e: cv.config(cursor=""))

        # mic check: records through every capture mode at once and keeps the one Google understands
        self.check_btn = self.button(L, 168, FW, "Mic check – click, then say “one two three four five”",
                                     self.mic_check)
        self.check_text = cv.create_text(L, 200, anchor="w", fill=MUTED, font=(FONT, 8), width=FW,
                                         text=f"Current capture: {CAPTURE_MODES.get(self.cfg['capture_mode'], '?')}")

        # YouTube account: the X Assistant Chrome window keeps its own sign-in (Premium = no ads)
        self.label(L, 228, "YouTube account (sign in once – Premium means no ads)")
        half = (FW - 8) // 2
        self.button(L, 254, half, "Sign in to YouTube", self.sign_in)
        self.account_text = cv.create_text(L + half + 16, 254, anchor="w", fill=MUTED, font=(FONT, 9),
                                           text="Checking…", width=half - 8)
        self.refresh_account(repeat=0)

        # hotkey
        self.label(L, 284, "Push-to-talk key")
        self.hotkey_var = tk.StringVar(value=self.cfg["hotkey"])
        self.entry(L, 310, 220, self.hotkey_var)
        self.rec_btn = self.button(L + 232, 310, FW - 232, "Record keys", self.record_hotkey)

        # language
        self.label(L, 350, "Language")
        lang_name = next((k for k, v in LANGUAGES.items() if v == self.cfg["language"]), "English")
        self.lang_var = tk.StringVar(value=lang_name)
        self.combo(L, 376, FW, self.lang_var, list(LANGUAGES))

        # default platform
        self.label(L, 416, "Play songs on (same as the toggle on the main window)")
        self.platform = self.cfg["default_platform"]
        half = (FW - 8) // 2
        self.seg = {
            "youtube": self.button(L, 442, half, "YouTube", lambda: self.set_platform("youtube")),
            "spotify": self.button(L + half + 8, 442, half, "Spotify", lambda: self.set_platform("spotify")),
        }
        self.set_platform(self.platform)

        # always on top
        self.label(L, 486, "Keep window on top", color=TEXT, size=10)
        self.on_top = bool(self.cfg["always_on_top"])
        self.switch_bg = cv.create_image(self.W - L, 486, anchor="e", tags="switch")
        self.switch_knob = cv.create_oval(0, 0, 0, 0, fill="white", outline="", tags="switch")
        cv.tag_bind("switch", "<Button-1>", lambda e: self.toggle_top())
        self.draw_switch()

        # spotify
        self.label(L, 526, "Spotify API (optional – for autoplay)")
        self.cid_var = tk.StringVar(value=self.cfg["spotify_client_id"])
        self.sec_var = tk.StringVar(value=self.cfg["spotify_client_secret"])
        self.entry(L, 552, FW, self.cid_var, placeholder="Client ID")
        self.entry(L, 590, FW, self.sec_var, placeholder="Client Secret", secret=True)

        # spoken confirmation voice
        self.label(L, 630, "Voice reply – ElevenLabs key (optional, otherwise Microsoft neural voice)")
        self.eleven_var = tk.StringVar(value=self.cfg.get("elevenlabs_api_key", ""))
        self.entry(L, 656, FW, self.eleven_var, placeholder="ElevenLabs key", secret=True)

        self.button(L, 694, (FW - 8) // 2, "Cancel", self.close)
        self.button(L + (FW - 8) // 2 + 8, 694, (FW - 8) // 2, "Save", self.save, primary=True)

        win.protocol("WM_DELETE_WINDOW", self.close)
        win.after(50, win.focus_force)
        self.start_test()
        self.tick()

    # ---- widgets

    def label(self, x, y, text, color=MUTED, size=9):
        self.cv.create_text(x, y, text=text, anchor="w", fill=color, font=(FONT, size))

    def combo(self, x, y, w, var, values):
        self.cv.create_image(x, y, anchor="w", image=self.keep(field_image(w, 32)))
        cb = ttk.Combobox(self.win, textvariable=var, values=values, state="readonly",
                          style="Glass.TCombobox", font=(FONT, 10))
        self.cv.create_window(x + 4, y, anchor="w", window=cb, width=w - 8, height=26)
        # the dropdown list is its own popup window: flat list + Windows 11 rounded corners
        pop = cb.tk.call("ttk::combobox::PopdownWindow", cb)
        cb.tk.call(f"{pop}.f.l", "configure", "-borderwidth", 0, "-highlightthickness", 0, "-relief", "flat",
                   "-activestyle", "none")
        cb.tk.call(f"{pop}.f", "configure", "-borderwidth", 0)
        round_cmd = self.win.register(lambda: round_corners(int(str(cb.tk.call("winfo", "id", pop)), 0)))
        cb.tk.call("bind", pop, "<Map>", "+" + round_cmd)
        return cb

    def entry(self, x, y, w, var, placeholder="", secret=False):
        self.cv.create_image(x, y, anchor="w", image=self.keep(field_image(w, 32)))
        e = tk.Entry(self.win, textvariable=var, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat",
                     font=(FONT, 10), bd=0, show="•" if secret else "")
        inset = 12
        if placeholder:  # inline label inside the field (widgets would cover canvas text)
            lbl = tk.Label(self.win, text=placeholder, bg=FIELD, fg="#8c7fb3", font=(FONT, 9))
            self.cv.create_window(x + 12, y, anchor="w", window=lbl, height=22)
            inset = 104
        self.cv.create_window(x + inset, y, anchor="w", window=e, width=w - inset - 12, height=22)
        return e

    def button(self, x, y, w, text, command, primary=False):
        tag = f"btn{id(command)}{x}{y}"
        img = self.cv.create_image(x, y, anchor="w", tags=tag)
        txt = self.cv.create_text(x + w / 2, y, text=text, fill=TEXT, font=(FONT, 10, "bold"), tags=tag)
        self.cv.tag_bind(tag, "<Button-1>", lambda e: command())
        self.cv.tag_bind(tag, "<Enter>", lambda e: self.cv.config(cursor="hand2"))
        self.cv.tag_bind(tag, "<Leave>", lambda e: self.cv.config(cursor=""))
        btn = {"img": img, "txt": txt, "w": w}
        self.paint_button(btn, "primary" if primary else "glass")
        return btn

    def paint_button(self, btn, kind):
        looks = {
            "glass": dict(fill=(255, 255, 255, 30), outline=(255, 255, 255, 60)),
            "primary": dict(fill=None, gradient=(VIOLET, PINK)),
            "youtube": dict(fill=None, gradient=((255, 40, 40), (255, 90, 60))),
            "spotify": dict(fill=None, gradient=((20, 190, 90), (40, 225, 120))),
        }[kind]
        self.cv.itemconfig(btn["img"], image=self.keep(pill_image(btn["w"], 34, **looks)))

    def set_platform(self, platform):
        self.platform = platform
        for name, btn in self.seg.items():
            self.paint_button(btn, name if name == platform else "glass")
        # applies immediately (no need to press Save)
        self.cfg["default_platform"] = platform
        if self.app.cfg["default_platform"] != platform:
            self.app.set_platform(platform)  # keeps the main window's toggle in sync

    def draw_switch(self):
        on = self.on_top
        look = dict(fill=None, gradient=(VIOLET, PINK)) if on else dict(fill=(255, 255, 255, 40))
        self.cv.itemconfig(self.switch_bg, image=self.keep(pill_image(46, 26, **look)))
        x = self.W - 28 - (13 if on else 33)
        self.cv.coords(self.switch_knob, x - 10, 476, x + 10, 496)

    def sign_in(self):
        self.cv.itemconfig(self.account_text, text="Sign in on your second screen…", fill=TEXT)
        threading.Thread(target=open_google_signin, daemon=True).start()
        self.refresh_account(repeat=60)  # keep checking for ~3 minutes while they log in

    def refresh_account(self, repeat=0):
        result = queue.Queue()
        threading.Thread(target=lambda: result.put(youtube_signed_in()), daemon=True).start()

        def poll(tries=0):
            if not self.win.winfo_exists():
                return
            if result.empty():
                if tries < 50:
                    self.win.after(100, lambda: poll(tries + 1))
                return
            signed = result.get()
            if signed:
                self.cv.itemconfig(self.account_text, text="✓ Signed in", fill="#8ef0c0")
                return
            if repeat <= 0:
                self.cv.itemconfig(self.account_text, fill=MUTED, text="Not signed in yet" if signed is False
                                   else "Not checked (window closed)")
            if repeat > 0:
                self.win.after(3000, lambda: self.refresh_account(repeat - 1))
        poll()

    def toggle_top(self):
        self.on_top = not self.on_top
        self.draw_switch()

    def record_hotkey(self):
        self.cv.itemconfig(self.rec_btn["txt"], text="Press keys…")
        result = queue.Queue()
        threading.Thread(target=lambda: result.put(keyboard.read_hotkey(suppress=False)), daemon=True).start()

        def poll():
            if not self.win.winfo_exists():
                return
            if result.empty():
                self.win.after(50, poll)
                return
            self.hotkey_var.set(result.get())
            self.cv.itemconfig(self.rec_btn["txt"], text="Record keys")
        poll()

    # ---- mic check

    def mic_check(self):
        if self.checking:
            return
        self.checking = True
        self.stop_test.set()
        self.cv.itemconfig(self.check_text, fill=TEXT, text="🎙 Recording 4 seconds – say “one two three four five” now…")
        self.paint_button(self.check_btn, "primary")
        results = queue.Queue()
        name, language = self.mic_var.get(), LANGUAGES[self.lang_var.get()]
        threading.Thread(target=self._mic_check_worker, args=(name, language, results), daemon=True).start()

        def poll():
            if not self.win.winfo_exists():
                return
            if results.empty():
                self.win.after(100, poll)
                return
            kind, payload = results.get()
            if kind == "progress":
                self.cv.itemconfig(self.check_text, text=payload)
                self.win.after(100, poll)
                return
            self.checking = False
            self.paint_button(self.check_btn, "glass")
            self.cv.itemconfig(self.check_text, text=payload[1], fill=MUTED if payload[0] else "#ff8a8a")
            if payload[0]:
                self.cfg["capture_mode"] = payload[0]
                self.app.cfg["capture_mode"] = payload[0]
                self.app.cfg["microphone"] = name
                self.cfg["microphone"] = name
                save_config(self.app.cfg)
            self.start_test()
        poll()

    def _mic_check_worker(self, name, language, results):
        clips = {}

        def grab(mode):
            try:
                with open_microphone(name, mode) as src:
                    n = int(src.SAMPLE_RATE / src.CHUNK * 4)
                    clips[mode] = sr.AudioData(b"".join(src.read() for _ in range(n)), src.SAMPLE_RATE, 2)
            except Exception as e:
                clips[mode] = e

        threads = [threading.Thread(target=grab, args=(m,)) for m in CAPTURE_MODES]
        [t.start() for t in threads]
        [t.join() for t in threads]
        results.put(("progress", "Checking what Google understood from each capture mode…"))

        heard = {}
        for mode in CAPTURE_MODES:
            clip = clips.get(mode)
            if not isinstance(clip, sr.AudioData):
                continue
            try:
                heard[mode] = recognize(clip, language, save_to=os.path.join(BASE_DIR, f"mic_check_{mode}.wav"))
            except (sr.UnknownValueError, sr.RequestError):
                pass
        if heard:
            best = max(heard, key=lambda m: len(heard[m].split()))
            results.put(("done", (best, f"✓ Using {CAPTURE_MODES[best]} – heard: “{heard[best]}”")))
        else:
            results.put(("done", (None, "✗ No capture mode was understood. Check the mic's volume/mute, "
                                        "and that Language matches what you speak.")))

    # ---- mic test meter

    def start_test(self):
        self.stop_test.set()
        self.stop_test = threading.Event()
        threading.Thread(target=self.test_mic, args=(self.mic_var.get(), self.stop_test), daemon=True).start()

    def test_mic(self, name, stop):
        try:
            with open_microphone(name, self.cfg["capture_mode"]) as src:
                while not stop.is_set():
                    self.test_level = min(1.0, audioop.rms(src.read(), src.SAMPLE_WIDTH) / 3000)
        except Exception:
            pass
        self.test_level = 0.0

    def tick(self):
        if not self.win.winfo_exists():
            return
        self.shown = self.shown * 0.55 + self.test_level * 0.45
        w = max(6, int((self.W - 56) * self.shown) // 4 * 4)
        if w not in self.meter_cache:
            self.meter_cache[w] = ImageTk.PhotoImage(pill_image(w, 6, None, gradient=(VIOLET, PINK)))
        self.cv.itemconfig(self.meter, image=self.meter_cache[w])
        self.win.after(40, self.tick)

    # ---- save / close

    def save(self):
        self.cfg.update(
            microphone=self.mic_var.get(),
            hotkey=self.hotkey_var.get().strip().lower() or "f9",
            language=LANGUAGES[self.lang_var.get()],
            default_platform=self.platform,
            always_on_top=self.on_top,
            spotify_client_id=self.cid_var.get().strip(),
            spotify_client_secret=self.sec_var.get().strip(),
            elevenlabs_api_key=self.eleven_var.get().strip(),
        )
        save_config(self.cfg)
        self.app.cfg = self.cfg
        self.close()
        self.app.apply_config()

    def close(self):
        self.stop_test.set()
        self.app.root.attributes("-topmost", bool(self.app.cfg["always_on_top"]))
        self.win.destroy()


if __name__ == "__main__":
    try:
        App().run()
    except Exception:
        log_exception(*sys.exc_info())
        raise
