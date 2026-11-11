# =====================================================================
#  Ultra Downloader Pro  v3.0   (Flet + yt-dlp)
#  नियम / RULE: कोई भी "video" विकल्प आवाज़ के बिना नहीं बनता।
#               Every "video" choice is audio-guaranteed by construction.
#  चलाएँ / Run   :  python main.py
#  जाँच / Verify :  python main.py --selftest
# =====================================================================
from __future__ import annotations

import asyncio
import glob
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

import flet as ft
import yt_dlp

APP_NAME = "Ultra Downloader Pro"
APP_VER = "3.0.0"

IS_ANDROID = hasattr(sys, "getandroidapilevel") or os.path.exists("/system/build.prop")
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

OK, WARN, ERR, INFO = "#22C55E", "#F59E0B", "#EF4444", "#3B82F6"

PAL = {
    True: dict(page="#0B0F19", card="#131C2E", panel="#1E293B", item="#0F172A",
               text="#E2E8F0", muted="#94A3B8", accent="#60A5FA", line="#334155"),
    False: dict(page="#F1F5F9", card="#FFFFFF", panel="#E2E8F0", item="#F8FAFC",
                text="#0F172A", muted="#475569", accent="#1D4ED8", line="#CBD5E1"),
}


# =====================================================================
#  भाग 1 / PART 1 — शुद्ध फंक्शन (आसानी से टेस्ट होते हैं)
#                 Pure helpers (unit-testable, no UI, no network)
# =====================================================================
def clean_ansi(t: Any) -> str:
    return re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", str(t or "")).strip()


def human_size(n, unknown="साइज़ अज्ञात"):
    try:
        n = float(n or 0)
    except Exception:
        return unknown
    if n <= 0:
        return unknown
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.2f} TB"


def human_dur(s):
    if not s:
        return "—"
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def normalize_url(t: str) -> str:
    """स्कीम के बिना लिंक भी स्वीकार / Accept scheme-less links too."""
    t = (t or "").strip().strip('"').strip("'")
    if not t:
        return ""
    if re.match(r"^https?://", t, re.I):
        return t
    if re.match(r"^[\w-]+(\.[\w-]+)+/\S*$", t):
        return "https://" + t
    return t


def is_url(t: str) -> bool:
    t = normalize_url(t)
    return bool(re.match(r"^https?://[\w-]+(\.[\w-]+)+\S*$", t, re.I))


def quality_tag(h: int) -> str:
    h = int(h or 0)
    if h >= 4320: return f"8K UHD ({h}p)"
    if h >= 2160: return f"4K Ultra HD ({h}p)"
    if h >= 1440: return f"2K QHD ({h}p)"
    if h >= 1080: return "1080p Full HD"
    if h >= 720:  return "720p HD"
    if h >= 480:  return "480p"
    if h >= 360:  return "360p"
    return f"{h}p डेटा सेवर"


def friendly_error(ex) -> str:
    m = clean_ansi(str(ex))
    low = m.lower()
    table = [
        (("sign in to confirm", "not a bot"),
         "YouTube ने bot-जाँच माँगी। कुछ देर रुकें, दूसरा नेटवर्क लें, या Settings में cookies फाइल दें।"),
        (("private video",), "यह वीडियो Private है — डाउनलोड संभव नहीं।"),
        (("video unavailable", "has been removed"), "वीडियो उपलब्ध नहीं / हटा दिया गया है।"),
        (("unsupported url",), "यह लिंक समर्थित नहीं है। सही वीडियो लिंक डालें।"),
        (("requested format is not available",),
         "चुनी गई क्वालिटी इस वीडियो में नहीं है। दोबारा Scan करके दूसरी क्वालिटी चुनें।"),
        (("ffmpeg is not installed", "ffmpeg not found", "won't be merged"),
         "FFmpeg नहीं मिला — मर्ज नहीं हुआ। Settings में FFmpeg पाथ दें या APK में FFmpeg बाँधें।"),
        (("http error 403", "forbidden"), "सर्वर ने रोका (403)। yt-dlp अपडेट करें: pip install -U yt-dlp"),
        (("http error 429", "too many requests"), "बहुत ज़्यादा अनुरोध (429)। Turbo कम करें, थोड़ी देर रुकें।"),
        (("permission denied",), "फोल्डर की अनुमति नहीं। दूसरा सेव फोल्डर चुनें।"),
        (("no space left",), "स्टोरेज भर गया है। जगह खाली करें।"),
        (("urlopen error", "getaddrinfo", "timed out", "connection reset"),
         "इंटरनेट/नेटवर्क समस्या। कनेक्शन जाँचकर फिर कोशिश करें।"),
        (("copyright",), "कॉपीराइट ब्लॉक के कारण यह वीडियो उपलब्ध नहीं।"),
        (("geo",), "यह वीडियो आपके देश में ब्लॉक है (geo-block)।"),
    ]
    for keys, msg in table:
        if any(k in low for k in keys):
            return msg
    return m[:160] or "अज्ञात त्रुटि"


# =====================================================================
#  भाग 2 / PART 2 — FFmpeg खोज (Android सहित)
#                 FFmpeg discovery (Android included)
# =====================================================================
@dataclass
class FF:
    exe: Optional[str] = None
    probe: Optional[str] = None
    source: str = "नहीं मिला / not found"
    version: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.exe)

    @property
    def folder(self) -> Optional[str]:
        return os.path.dirname(self.exe) if self.exe else None


def _exe_version(path: str) -> str:
    """असल में चलाकर देखो — केवल फाइल देखना काफ़ी नहीं / Actually exec it."""
    if not path or not os.path.isfile(path):
        return ""
    try:
        if not IS_WIN:
            try:
                os.chmod(path, 0o755)
            except Exception:
                pass
        r = subprocess.run([path, "-version"], capture_output=True, timeout=10, text=True)
        if r.returncode == 0 and r.stdout:
            return r.stdout.splitlines()[0][:90]
    except Exception:
        pass
    return ""


def _android_native_dirs() -> List[str]:
    """
    Android 10+ ऐप के data dir से exec() बंद है, पर nativeLibraryDir खुला है।
    /proc/self/maps से libflutter.so का फोल्डर निकालकर वहीँ libffmpeg.so देखो।
    Android 10+ blocks exec() from app data dir, but nativeLibraryDir is allowed.
    Derive it from /proc/self/maps (libflutter.so) and look for libffmpeg.so.
    """
    out = set()
    try:
        with open("/proc/self/maps", "r") as fh:
            for line in fh:
                if "/lib/" in line and line.rstrip().endswith(".so"):
                    p = line.split()[-1]
                    if os.path.isfile(p):
                        out.add(os.path.dirname(p))
    except Exception:
        pass
    return sorted(out)


def discover_ffmpeg(custom: str = "") -> FF:
    cands: List[tuple] = []
    if custom:
        cands.append((custom, "कस्टम पाथ / custom path"))
    env = os.environ.get("ULTRA_FFMPEG") or os.environ.get("FFMPEG_BINARY")
    if env:
        cands.append((env, "env"))
    w = shutil.which("ffmpeg")
    if w:
        cands.append((w, "PATH"))
    try:
        import imageio_ffmpeg  # वैकल्पिक / optional
        cands.append((imageio_ffmpeg.get_ffmpeg_exe(), "imageio-ffmpeg"))
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    for d in _android_native_dirs():
        cands.append((os.path.join(d, "libffmpeg.so"), "APK jniLibs"))
        cands.append((os.path.join(d, "libffprobe.so"), "APK jniLibs"))
    data = os.environ.get("FLET_APP_STORAGE_DATA") or ""
    if data:
        cands.append((os.path.join(data, "ffmpeg", "ffmpeg"), "app storage"))
    for p in (os.path.join(here, "ffmpeg.exe" if IS_WIN else "ffmpeg"),
              os.path.join(here, "bin", "ffmpeg.exe" if IS_WIN else "ffmpeg"),
              "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg",
              r"C:\ffmpeg\bin\ffmpeg.exe"):
        cands.append((p, "सामान्य पाथ / common path"))

    for path, src in cands:
        if not path or path.endswith("libffprobe.so"):
            continue
        ver = _exe_version(path)
        if ver:
            probe = None
            sib = os.path.join(os.path.dirname(path),
                               "ffprobe.exe" if IS_WIN else
                               ("libffprobe.so" if path.endswith(".so") else "ffprobe"))
            if os.path.isfile(sib):
                probe = sib
            elif shutil.which("ffprobe"):
                probe = shutil.which("ffprobe")
            return FF(exe=path, probe=probe, source=src, version=ver)
    return FF()


# =====================================================================
#  भाग 3 / PART 3 — फाइल में आवाज़ है? (शुद्ध Python सत्यापन)
#                 Does the file really contain audio? (pure Python)
# =====================================================================
def _boxes(fh, end: int):
    while True:
        pos = fh.tell()
        if pos + 8 > end:
            return
        head = fh.read(8)
        if len(head) < 8:
            return
        size = struct.unpack(">I", head[:4])[0]
        typ = head[4:8]
        hdr = 8
        if size == 1:
            ext = fh.read(8)
            if len(ext) < 8:
                return
            size = struct.unpack(">Q", ext)[0]
            hdr = 16
        elif size == 0:
            size = end - pos
        if size < hdr or pos + size > end:
            return
        yield typ, pos + hdr, pos + size
        fh.seek(pos + size)


def _find_soun(fh, start: int, end: int, depth: int = 0) -> bool:
    if depth > 6:
        return False
    fh.seek(start)
    for typ, s, e in _boxes(fh, end):
        if typ in (b"moov", b"trak", b"mdia"):
            if _find_soun(fh, s, e, depth + 1):
                return True
        elif typ == b"hdlr":
            fh.seek(s)
            data = fh.read(min(24, e - s))
            if len(data) >= 12 and data[8:12] == b"soun":
                return True
    return False


def probe_audio(path: str):
    """True = आवाज़ है, False = नहीं है, None = पता नहीं (TS आदि)।"""
    try:
        size = os.path.getsize(path)
        if size < 512:
            return None
        with open(path, "rb") as fh:
            head = fh.read(16)
            if head[4:8] in (b"ftyp", b"moov", b"mdat", b"free", b"skip"):
                return _find_soun(fh, 0, size)
            if head[:4] == b"\x1a\x45\xdf\xa3":          # Matroska / WebM
                fh.seek(0)
                blob = fh.read(min(size, 4 * 1024 * 1024))
                return b"\x83\x81\x02" in blob            # TrackType = audio
            if head[:3] == b"ID3" or (head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
                return True                               # MP3 / ADTS-AAC
            fh.seek(0)
            ts = fh.read(188 * 3)
            if len(ts) >= 188 and ts[0] == 0x47 and (len(ts) < 376 or ts[188] == 0x47):
                return None                               # MPEG-TS: अपुष्ट
    except Exception:
        return None
    return None


# =====================================================================
#  भाग 4 / PART 4 — क्वालिटी इंजन (आवाज़ की गारंटी यहीं बनती है)
#                 Quality engine (audio guarantee is built here)
# =====================================================================
@dataclass
class Choice:
    key: str
    kind: str                    # "video" | "audio"
    title: str
    meta: str
    size: int
    fmt: str
    height: int = 0
    container: str = "mp4"
    needs_ffmpeg: bool = False
    available: bool = True
    reason: str = ""
    experimental: bool = False
    pp: Optional[list] = None


def _vc(f): return f.get("vcodec") or "none"
def _ac(f): return f.get("acodec") or "none"


def _fsize(f, dur):
    sz = f.get("filesize") or f.get("filesize_approx")
    if not sz and dur:
        br = f.get("tbr") or ((f.get("vbr") or 0) + (f.get("abr") or 0))
        if br:
            sz = int(br * 1000 / 8 * dur)
    return int(sz or 0)


def pick_container(vcodec: str, acodec: str, pref: str) -> str:
    if pref == "mp4":
        return "mp4"
    if pref == "mkv":
        return "mkv"
    if vcodec.startswith("avc1") and acodec.startswith("mp4a"):
        return "mp4"
    if vcodec.startswith(("vp9", "vp09")) and acodec.startswith("opus"):
        return "webm"
    return "mkv"


def _codec_rank(vcodec: str, h: int, mode: str) -> int:
    if mode == "quality":
        return 0
    if h <= 1080:
        return {"avc1": 3}.get(vcodec[:4], 2 if vcodec.startswith(("vp9", "vp09")) else 1)
    return 3 if vcodec.startswith(("vp9", "vp09")) else (2 if vcodec.startswith("av01") else 1)


def build_choices(info: dict, ff_ok: bool, prefs: dict):
    """
    returns (choices, blocked_heights)
    नियम: kind == "video" और available == True  =>  आवाज़ पक्की।
    RULE: kind == "video" and available == True  =>  audio guaranteed.
    """
    dur = info.get("duration") or 0
    formats = info.get("formats") or []
    mode = prefs.get("codec", "compat")
    cpref = prefs.get("container", "mp4")

    prog, vonly, aonly, hls = [], [], [], []
    for f in formats:
        v, a = _vc(f), _ac(f)
        if v != "none" and a != "none":
            prog.append(f)
            if str(f.get("protocol", "")).startswith("m3u8"):
                hls.append(f)
        elif v != "none":
            vonly.append(f)
        elif a != "none":
            aonly.append(f)

    aonly.sort(key=lambda f: (f.get("abr") or 0, _fsize(f, dur)), reverse=True)
    aac = [f for f in aonly
           if f.get("ext") in ("m4a", "mp4") or str(f.get("acodec", "")).startswith("mp4a")]
    merge_a = (aac[0] if aac else (aonly[0] if aonly else None)) if mode == "compat" \
        else (aonly[0] if aonly else None)

    def group(lst, rank_fn):
        g = {}
        for f in lst:
            h = int(f.get("height") or 0)
            if not h:
                continue
            r = rank_fn(f, h)
            if h not in g or r > g[h][0]:
                g[h] = (r, f)
        return {h: v[1] for h, v in g.items()}

    gv = group(vonly, lambda f, h: (_codec_rank(_vc(f), h, mode),
                                    f.get("fps") or 0, f.get("tbr") or 0, _fsize(f, dur)))
    gp = group(prog, lambda f, h: (1 if f.get("ext") == "mp4" else 0,
                                   f.get("tbr") or 0, _fsize(f, dur)))
    gh = group(hls, lambda f, h: (f.get("tbr") or 0, _fsize(f, dur)))

    choices: List[Choice] = []
    blocked: List[int] = []

    for h in sorted(set(gv) | set(gp) | set(gh), reverse=True):
        vo, pr, hl = gv.get(h), gp.get(h), gh.get(h)
        tag = quality_tag(h)
        made = False

        if ff_ok and vo and merge_a:
            cont = pick_container(_vc(vo), _ac(merge_a), cpref)
            fps = int(vo.get("fps") or 0)
            total = _fsize(vo, dur) + _fsize(merge_a, dur)
            choices.append(Choice(
                key=f"v{h}m", kind="video",
                title=f"🎬 {tag}" + (f" • {fps}fps" if fps > 30 else "") + " • 🔊 आवाज़ पक्की",
                meta=f"{human_size(total)} • {cont.upper()} • "
                     f"{_vc(vo)[:4]}+{_ac(merge_a)[:4]} • मर्ज (FFmpeg)",
                size=total, height=h, container=cont, needs_ffmpeg=True,
                fmt=(f"{vo['format_id']}+{merge_a['format_id']}/"
                     f"bestvideo[height<={h}]+bestaudio/best[height<={h}]")))
            made = True

        if pr:
            fps = int(pr.get("fps") or 0)
            choices.append(Choice(
                key=f"v{h}d", kind="video",
                title=f"🎬 {tag}" + (f" • {fps}fps" if fps > 30 else "") + " • 🔊 एक ही फाइल",
                meta=f"{human_size(_fsize(pr, dur))} • {str(pr.get('ext','mp4')).upper()} • "
                     f"Direct (FFmpeg की ज़रूरत नहीं)",
                size=_fsize(pr, dur), height=h,
                container=str(pr.get("ext", "mp4")),
                fmt=f"{pr['format_id']}/best[height<={h}][acodec!=none]/best"))
            made = True

        if hl and not made and prefs.get("hls", True):
            choices.append(Choice(
                key=f"v{h}h", kind="video",
                title=f"🎬 {tag} • 🔊 HLS (प्रयोगात्मक)",
                meta=f"{human_size(_fsize(hl, dur))} • TS→MP4 • FFmpeg के बिना HD का रास्ता",
                size=_fsize(hl, dur), height=h, container="mp4",
                experimental=True,
                fmt=f"{hl['format_id']}/best[height<={h}][protocol*=m3u8]/best"))
            made = True

        if not made:
            blocked.append(h)
            choices.append(Choice(
                key=f"lock{h}", kind="video",
                title=f"🔒 {tag} • आवाज़ के साथ उपलब्ध नहीं",
                meta="FFmpeg इंस्टॉल/बाँधें — तब यह क्वालिटी आवाज़ सहित मिलेगी",
                size=(_fsize(vo, dur) + (_fsize(merge_a, dur) if merge_a else 0)) if vo else 0,
                height=h, available=False, reason="FFmpeg चाहिए"))

    # ---- Auto Best / ऑटो सर्वोत्तम ----
    vids = [c for c in choices if c.kind == "video" and c.available]
    if vids:
        if ff_ok:
            afmt = "bestvideo+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
            acont = "mp4" if cpref == "mp4" else ("mkv" if cpref == "mkv" else "mkv")
            ameta = "सबसे ऊँची उपलब्ध क्वालिटी • आवाज़ मर्ज होगी"
        else:
            afmt = "best[acodec!=none]/best"
            acont = "mp4"
            ameta = "सबसे ऊँची combined क्वालिटी • FFmpeg नहीं, इसलिए सीमा लागू"
        choices.insert(0, Choice(
            key="auto", kind="video", title="⚡ Auto Best • 🔊 आवाज़ पक्की",
            meta=f"{ameta} • अनुमानित {human_size(vids[0].size)}",
            size=vids[0].size, height=vids[0].height, container=acont,
            needs_ffmpeg=ff_ok, fmt=afmt))

    # ---- सिर्फ़ आवाज़ / AUDIO ONLY (अलग सेक्शन) ----
    if aac:
        f = aac[0]
        choices.append(Choice(
            key="a_m4a", kind="audio",
            title=f"🎵 मूल आवाज़ • M4A/AAC {int(f.get('abr') or 128)}kbps",
            meta=f"{human_size(_fsize(f, dur))} • हर प्लेयर में चलेगी",
            size=_fsize(f, dur), container="m4a",
            fmt=f"{f['format_id']}/bestaudio[ext=m4a]/bestaudio"))
    if aonly and (not aac or aonly[0]["format_id"] != aac[0]["format_id"]):
        f = aonly[0]
        choices.append(Choice(
            key="a_best", kind="audio",
            title=f"🎵 सर्वोच्च बिटरेट • {str(f.get('ext','webm')).upper()} "
                  f"{int(f.get('abr') or 160)}kbps",
            meta=f"{human_size(_fsize(f, dur))} • सबसे छोटी फाइल, अच्छी गुणवत्ता",
            size=_fsize(f, dur), container=str(f.get("ext", "webm")),
            fmt=f"{f['format_id']}/bestaudio/best"))
    if aonly or prog:
        base = _fsize(aonly[0], dur) if aonly else 0
        choices.append(Choice(
            key="a_mp3", kind="audio", title="🎧 MP3 में बदलें • 192kbps",
            meta=(f"लगभग {human_size(int(192*1000/8*(dur or 0)))} • FFmpeg से conversion"
                  if ff_ok else "FFmpeg इंस्टॉल करें — तब MP3 बन सकेगी"),
            size=base or int(192 * 1000 / 8 * (dur or 0)), container="mp3",
            needs_ffmpeg=True, available=ff_ok, reason="FFmpeg चाहिए",
            fmt="bestaudio/best",
            pp=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": "192"}]))
    return choices, blocked


# =====================================================================
#  भाग 5 / PART 5 — डाउनलोड इंजन (कतार + सत्यापन + मरम्मत)
#                 Download engine (queue + verification + repair)
# =====================================================================
class YdlLog:
    def __init__(self, sink): self.sink = sink
    def debug(self, m):
        m = clean_ansi(m)
        if m and not m.startswith("[debug]"):
            self.sink("debug", m)
    def info(self, m): self.debug(m)
    def warning(self, m): self.sink("warning", clean_ansi(m))
    def error(self, m): self.sink("error", clean_ansi(m))


class Cancelled(Exception):
    pass


class Downloader:
    def __init__(self, emit):
        self.emit = emit
        self.q = deque()
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.busy = False

    # ---- कतार / queue ----
    def submit(self, task: dict):
        with self.lock:
            self.q.append(task)
            start = self.worker is None or not self.worker.is_alive()
            if start:
                self.worker = threading.Thread(target=self._loop, daemon=True)
        self.emit({"type": "queue", "n": len(self.q)})
        if start:
            self.worker.start()

    def cancel(self):
        self.stop.set()

    def drop_queue(self):
        with self.lock:
            self.q.clear()
        self.emit({"type": "queue", "n": 0})

    def _loop(self):
        while True:
            with self.lock:
                if not self.q:
                    self.busy = False
                    return
                task = self.q.popleft()
                self.busy = True
            self.stop.clear()
            self.emit({"type": "queue", "n": len(self.q)})
            try:
                self._run(task)
            except Cancelled:
                self.emit({"type": "cancelled", "task": task})
            except Exception as ex:
                if self.stop.is_set():
                    self.emit({"type": "cancelled", "task": task})
                else:
                    self.emit({"type": "error", "msg": friendly_error(ex), "task": task})
            finally:
                self.emit({"type": "idle"})

    # ---- विकल्प / options ----
    def _opts(self, task, ff: FF):
        ch: Choice = task["choice"]
        prefs = task["prefs"]
        save = task["save_dir"]
        if ch.needs_ffmpeg and not ff.ok:
            raise RuntimeError("FFmpeg नहीं मिला — यह विकल्प अभी संभव नहीं।")
        if "+" in ch.fmt and not ff.ok:
            raise RuntimeError("मर्ज के लिए FFmpeg अनिवार्य है (बेआवाज़ फाइल नहीं बनाएँगे)।")

        tagname = f"[{ch.height}p]" if ch.height else "[video]"
        if ch.kind == "audio":
            tagname = "[audio]"
        opts = {
            "format": ch.fmt,
            "outtmpl": os.path.join(save, "%(title).120B " + tagname + ".%(ext)s"),
            "outtmpl_na_placeholder": "best",
            "noplaylist": True, "continuedl": True, "quiet": True, "noprogress": True,
            "logger": YdlLog(lambda lvl, m: self.emit({"type": "log", "lvl": lvl, "msg": m})),
            "retries": 10, "fragment_retries": 10, "extractor_retries": 3,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": int(prefs.get("frags", 8)),
            "progress_hooks": [self._hook],
            "postprocessor_hooks": [self._pp],
            "windowsfilenames": IS_WIN,
        }
        if prefs.get("turbo", True):
            opts["http_chunk_size"] = 10 * 1024 * 1024
        if ff.ok:
            opts["ffmpeg_location"] = ff.folder
        if ch.needs_ffmpeg and "+" in ch.fmt:
            opts["merge_output_format"] = ch.container
        if ch.pp:
            opts["postprocessors"] = ch.pp
        ck = prefs.get("cookies") or ""
        if ck and os.path.isfile(ck):
            opts["cookiefile"] = ck
        if task.get("partial_dir"):
            opts["paths"] = {"temp": task["partial_dir"]}
        return opts

    def _hook(self, d):
        if self.stop.is_set():
            raise Cancelled()
        st = d.get("status")
        if st == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            self.emit({"type": "progress",
                       "p": (done / total) if total else None,
                       "done": done, "total": total,
                       "speed": clean_ansi(d.get("_speed_str") or "--"),
                       "eta": clean_ansi(d.get("_eta_str") or "--"),
                       "file": d.get("tmpfilename") or d.get("filename") or ""})
        elif st == "finished":
            self.emit({"type": "phase", "phase": "merge",
                       "file": d.get("filename") or ""})

    def _pp(self, d):
        if self.stop.is_set() and d.get("status") == "started":
            raise Cancelled()
        self.emit({"type": "phase", "phase": "merge"})

    def _run(self, task):
        ch: Choice = task["choice"]
        ff: FF = task["ff"]
        os.makedirs(task["save_dir"], exist_ok=True)
        self.emit({"type": "phase", "phase": "download", "title": task.get("title", "Video")})
        with yt_dlp.YoutubeDL(self._opts(task, ff)) as ydl:
            info = ydl.extract_info(task["url"], download=True)

        path = None
        for rd in (info.get("requested_downloads") or []):
            path = rd.get("filepath") or path
        if not path or not os.path.exists(path):
            files = glob.glob(os.path.join(task["save_dir"], "*"))
            path = max(files, key=os.path.getmtime) if files else None
        size = os.path.getsize(path) if path and os.path.exists(path) else 0

        audio = True if ch.kind == "audio" else probe_audio(path)
        entry = {"title": info.get("title") or task.get("title", "Video"),
                 "path": path or "", "size": size, "kind": ch.kind,
                 "quality": (f"{ch.height}p" if ch.height else ch.container.upper()),
                 "audio": audio, "url": task["url"], "ts": time.time(),
                 "choice": ch.key}
        self.emit({"type": "done", "entry": entry})

    # ---- 🔇 मरम्मत / repair ----
    def repair(self, entry, ff: FF, prefs):
        def work():
            try:
                vp = entry.get("path") or ""
                if not (ff.ok and os.path.isfile(vp)):
                    self.emit({"type": "error", "msg": "मरम्मत के लिए FFmpeg और मूल फाइल चाहिए।"})
                    return
                self.emit({"type": "phase", "phase": "repair", "title": entry["title"]})
                tmp = os.path.join(os.path.dirname(vp), ".ultra_fix")
                os.makedirs(tmp, exist_ok=True)
                with yt_dlp.YoutubeDL({
                    "format": "bestaudio[ext=m4a]/bestaudio/best",
                    "outtmpl": os.path.join(tmp, "a.%(ext)s"),
                    "quiet": True, "noprogress": True, "noplaylist": True,
                    "ffmpeg_location": ff.folder,
                    "progress_hooks": [self._hook],
                }) as ydl:
                    ai = ydl.extract_info(entry["url"], download=True)
                ap = None
                for rd in (ai.get("requested_downloads") or []):
                    ap = rd.get("filepath") or ap
                if not ap:
                    got = glob.glob(os.path.join(tmp, "a.*"))
                    ap = got[0] if got else None
                if not ap:
                    raise RuntimeError("ऑडियो ट्रैक डाउनलोड नहीं हो सका।")
                out = os.path.splitext(vp)[0] + " [fixed].mp4"
                cmd = [ff.exe, "-y", "-i", vp, "-i", ap,
                       "-map", "0:v:0", "-map", "1:a:0",
                       "-c", "copy", "-shortest", out]
                r = subprocess.run(cmd, capture_output=True, timeout=1800)
                shutil.rmtree(tmp, ignore_errors=True)
                if r.returncode != 0 or not os.path.exists(out):
                    raise RuntimeError("FFmpeg मर्ज विफल: " +
                                       clean_ansi(r.stderr.decode("utf-8", "ignore"))[-140:])
                e2 = dict(entry, path=out, size=os.path.getsize(out),
                          audio=probe_audio(out), ts=time.time())
                self.emit({"type": "done", "entry": e2, "repaired": True})
            except Exception as ex:
                self.emit({"type": "error", "msg": friendly_error(ex)})
            finally:
                self.emit({"type": "idle"})
        threading.Thread(target=work, daemon=True).start()


# =====================================================================
#  भाग 6 / PART 6 — सेल्फ-टेस्ट (मैं यहाँ कोड चला नहीं सकता, आप चलाएँ)
#                 SELF-TEST (I cannot execute code here; you run it)
# =====================================================================
def _box(t, p): return struct.pack(">I", len(p) + 8) + t + p
def _trak(kind): return _box(b"trak", _box(b"mdia",
                             _box(b"hdlr", b"\0" * 8 + kind + b"\0" * 12)))
def _fake_mp4(kinds):
    return (_box(b"ftyp", b"isom" + b"\0" * 4 + b"isom") +
            _box(b"mdat", b"\0" * 64) +
            _box(b"moov", b"".join(_trak(k) for k in kinds)))


def selftest() -> int:
    import tempfile
    fails = []

    def check(name, cond):
        print(("✅ " if cond else "❌ ") + name)
        if not cond:
            fails.append(name)

    d = tempfile.mkdtemp()
    v = os.path.join(d, "v.mp4"); va = os.path.join(d, "va.mp4")
    open(v, "wb").write(_fake_mp4([b"vide"]))
    open(va, "wb").write(_fake_mp4([b"vide", b"soun"]))
    check("probe_audio: video-only => False", probe_audio(v) is False)
    check("probe_audio: video+audio => True", probe_audio(va) is True)
    check("human_size MB", human_size(5 * 1024 * 1024) == "5.0 MB")
    check("normalize_url", normalize_url("youtu.be/abc") == "https://youtu.be/abc")
    check("is_url reject", is_url("hello world") is False)
    check("quality_tag 2160", "4K" in quality_tag(2160))

    info = {"duration": 100, "formats": [
        {"format_id": "137", "ext": "mp4", "height": 1080, "vcodec": "avc1",
         "acodec": "none", "tbr": 4000, "fps": 30},
        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 160},
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a.40.2", "abr": 128},
        {"format_id": "18", "ext": "mp4", "height": 360, "vcodec": "avc1",
         "acodec": "mp4a.40.2", "tbr": 600},
    ]}
    prefs = {"container": "mp4", "codec": "compat", "hls": True}

    ch_ff, blocked_ff = build_choices(info, True, prefs)
    vids = [c for c in ch_ff if c.kind == "video" and c.available]
    check("FFmpeg: 1080p merged मौजूद", any(c.key == "v1080m" for c in vids))
    check("FFmpeg: हर video में '+' या combined",
          all(("+" in c.fmt) or ("acodec!=none" in c.fmt) or c.key.endswith("d")
              for c in vids))
    check("FFmpeg: कोई locked नहीं", not blocked_ff)
    check("FFmpeg: audio सेक्शन मौजूद",
          any(c.kind == "audio" and c.key == "a_mp3" and c.available for c in ch_ff))

    ch_no, blocked_no = build_choices(info, False, prefs)
    vids_no = [c for c in ch_no if c.kind == "video" and c.available]
    check("बिना FFmpeg: कोई '+' नहीं (बेआवाज़ फाइल असंभव)",
          all("+" not in c.fmt for c in vids_no))
    check("बिना FFmpeg: 1080p locked दिखेगा", 1080 in blocked_no)
    check("बिना FFmpeg: 360p direct मौजूद", any(c.key == "v360d" for c in vids_no))
    check("बिना FFmpeg: MP3 disabled",
          any(c.key == "a_mp3" and not c.available for c in ch_no))
    check("container auto: vp9+opus => webm",
          pick_container("vp9", "opus", "auto") == "webm")
    check("container auto: avc1+aac => mp4",
          pick_container("avc1.640028", "mp4a.40.2", "auto") == "mp4")
    shutil.rmtree(d, ignore_errors=True)
    print(f"\n{'🎉 सब पास' if not fails else '⚠️ विफल: ' + ', '.join(fails)}")
    return 1 if fails else 0


# =====================================================================
#  भाग 7 / PART 7 — UI (Flet)
# =====================================================================
def config_dir() -> str:
    base = (os.environ.get("FLET_APP_STORAGE_DATA") if IS_ANDROID else None) or \
        os.path.join(os.path.expanduser("~"), ".ultra_downloader_pro")
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        base = os.path.expanduser("~")
    return base


CFG = os.path.join(config_dir(), "settings.json")
HIST = os.path.join(config_dir(), "history.json")

DEF_PREFS = {"dark": True, "save_dir": "", "container": "mp4", "codec": "compat",
             "turbo": True, "frags": 8, "delete_partial": False, "open_after": False,
             "hls": True, "cookies": "", "ff_custom": ""}


def load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return fallback


def save_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
    except Exception:
        pass


def default_save_dir() -> str:
    if IS_ANDROID:
        return "/storage/emulated/0/Download/UltraVideos"
    return os.path.join(os.path.expanduser("~"), "Downloads", "UltraVideos")


def img_fit():
    for attr in ("BoxFit", "ImageFit"):
        o = getattr(ft, attr, None)
        if o is not None and hasattr(o, "COVER"):
            return o.COVER
    return None


def main(page: ft.Page):
    try:
        build_app(page)
    except Exception:
        page.add(ft.Text("Startup Error:\n" + traceback.format_exc(),
                         color=ERR, selectable=True))
        page.update()


def build_app(page: ft.Page):
    prefs = dict(DEF_PREFS, **load_json(CFG, {}))
    if not prefs.get("save_dir"):
        prefs["save_dir"] = default_save_dir()
    ff = discover_ffmpeg(prefs.get("ff_custom", ""))

    if IS_ANDROID:
        try:
            import certifi
            os.environ.setdefault("SSL_CERT_FILE", certifi.where())
        except Exception:
            pass

    st = {"tab": "download", "choices": [], "blocked": [], "selected": "auto",
          "meta": {}, "phase": "idle", "p": None, "done": 0, "total": 0,
          "speed": "--", "eta": "--", "status": "✅ तैयार — लिंक डालकर Scan दबाएँ।",
          "scol": OK, "queue": 0, "rev": 0, "hist": load_json(HIST, []),
          "scanning": False, "card_w": 560, "last_warn": ""}
    stop = threading.Event()

    page.title = f"{APP_NAME} {APP_VER}"
    page.padding = 10
    try:
        page.theme_mode = ft.ThemeMode.DARK if prefs["dark"] else ft.ThemeMode.LIGHT
        page.scroll = ft.ScrollMode.AUTO
        page.horizontal_alignment = ft.CrossAxisAlignment.CENTER
    except Exception:
        pass
    if not IS_ANDROID:
        try:
            page.window.width, page.window.height = 580, 940
            page.window.min_width, page.window.min_height = 340, 560
        except Exception:
            pass

    pal = lambda: PAL[bool(prefs["dark"])]
    static_texts, static_boxes = [], []

    # ---------- factories ----------
    def T(v="", role="text", size=13, bold=False, color=None, reg=False,
          align=ft.TextAlign.LEFT):
        t = ft.Text(v, size=size, color=color or pal()[role],
                    weight=ft.FontWeight.BOLD if bold else None, text_align=align)
        if reg and not color:
            static_texts.append((t, role))
        return t

    def panel(content, role="panel", pad=12, radius=14, reg=False, visible=True):
        c = ft.Container(content=content, bgcolor=pal()[role], padding=pad,
                         border_radius=radius, visible=visible)
        if reg:
            static_boxes.append((c, role))
        return c

    def button(label, bg, click, h=44, size=13, fg="#FFFFFF", enabled=True):
        t = ft.Text(label, size=size, weight=ft.FontWeight.BOLD, color=fg,
                    text_align=ft.TextAlign.CENTER)
        c = ft.Container(content=t, bgcolor=bg, height=h, border_radius=10,
                         padding=6, alignment=ft.Alignment(0, 0), ink=enabled,
                         on_click=click if enabled else None,
                         opacity=1.0 if enabled else 0.45, data=t)
        return c

    groups = []

    def grp(buttons, max_per, min_w, gap=8):
        host = ft.Column(controls=[], spacing=gap,
                         horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        groups.append((host, buttons, max_per, min_w, gap))
        return host

    def layout():
        w = max(240, st["card_w"] - 56)
        for host, bs, mx, mw, gap in groups:
            per = max(1, min(mx, int((w + gap) // (mw + gap)), len(bs)))
            bw = (w - gap * (per - 1)) / per
            rows = []
            for i in range(0, len(bs), per):
                chunk = bs[i:i + per]
                for b in chunk:
                    b.width = bw
                rows.append(ft.Row(chunk, spacing=gap,
                                   alignment=ft.MainAxisAlignment.CENTER))
            host.controls = rows

    def say(msg, col=INFO):
        st["status"], st["scol"] = msg, col

    def toast(msg):
        try:
            bar = ft.SnackBar(ft.Text(msg))
            if hasattr(page, "show_dialog"):
                page.show_dialog(bar)
            elif hasattr(page, "open"):
                page.open(bar)
            else:
                page.snack_bar = bar
                bar.open = True
            page.update()
        except Exception:
            pass

    def persist():
        save_json(CFG, prefs)

    # ---------- header + nav ----------
    title_t = T("VICONGENUMBA", "accent", 22, True, reg=True,
                align=ft.TextAlign.CENTER)
    sub_t = T("हर वीडियो में आवाज़ अनिवार्य • अलग से सिर्फ़-ऑडियो सेक्शन",
              "muted", 11, reg=True, align=ft.TextAlign.CENTER)
    ff_t = ft.Text("", size=11, weight=ft.FontWeight.BOLD, color="#FFFFFF",
                   text_align=ft.TextAlign.CENTER)
    ff_chip = ft.Container(content=ff_t, padding=7, border_radius=20, ink=True,
                           on_click=lambda e: set_tab("settings"))

    def refresh_ff():
        ff_t.value = (f"✓ FFmpeg सक्रिय • {ff.source}" if ff.ok
                      else "⚠ FFmpeg नहीं मिला • HD सीमित (टैप करें)")
        ff_chip.bgcolor = "#15803D" if ff.ok else "#B45309"

    refresh_ff()

    nav_specs = [("download", "⬇️ डाउनलोड"), ("history", "📜 इतिहास"),
                 ("settings", "⚙️ सेटिंग्स")]
    nav_btns = {}
    for k, lab in nav_specs:
        t = ft.Text(lab, size=12, weight=ft.FontWeight.BOLD,
                    text_align=ft.TextAlign.CENTER)
        c = ft.Container(content=t, padding=9, border_radius=10, ink=True,
                         alignment=ft.Alignment(0, 0),
                         on_click=lambda e, kk=k: set_tab(kk))
        nav_btns[k] = (c, t)
    nav_row = grp([nav_btns[k][0] for k, _ in nav_specs], 3, 110)

    theme_btn = button("☀️/🌙", "#334155", lambda e: toggle_theme(), 34, 12)

    def refresh_nav():
        for k, (c, t) in nav_btns.items():
            on = st["tab"] == k
            c.bgcolor = "#1D4ED8" if on else pal()["item"]
            t.color = "#FFFFFF" if on else pal()["muted"]

    # ---------- URL + scan ----------
    url_field = ft.TextField(label="वीडियो लिंक (YouTube / Instagram / Facebook…)",
                             hint_text="https://youtu.be/…", border_radius=12,
                             text_align=ft.TextAlign.LEFT, height=58,
                             on_submit=lambda e: do_scan())

    preview_col = ft.Column(controls=[], spacing=6,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    preview_card = panel(preview_col, reg=True, visible=False)

    warn_t = T("", "text", 12, True)
    warn_card = ft.Container(content=warn_t, bgcolor="#7C2D12", padding=10,
                             border_radius=12, visible=False)

    sel_t = T("", "text", 12, True, color=OK)
    quality_col = ft.Column(controls=[], spacing=7,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    folder_t = T("", "muted", 11)
    progress_bar = ft.ProgressBar(value=0, visible=False)
    pct_t = T("", "accent", 26, True, align=ft.TextAlign.CENTER)
    status_t = T(st["status"], "text", 13, True, color=OK, align=ft.TextAlign.CENTER)
    stats_t = T("डेटा: --  •  स्पीड: --  •  बचा समय: --", "muted", 11,
                align=ft.TextAlign.CENTER)
    queue_t = T("", "muted", 11, align=ft.TextAlign.CENTER)

    hist_col = ft.Column(controls=[], spacing=7,
                         horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    set_col = ft.Column(controls=[], spacing=9,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    # ---------- rendering ----------
    def render_preview():
        preview_col.controls.clear()
        m = st["meta"]
        if not m:
            preview_card.visible = False
            return
        th = m.get("thumbnail")
        if th and str(th).startswith("http"):
            try:
                tw = min(max(220, st["card_w"] - 90), 340)
                kw = dict(src=th, width=tw, height=int(tw * 9 / 16), border_radius=10)
                f = img_fit()
                if f is not None:
                    kw["fit"] = f
                preview_col.controls.append(ft.Image(**kw))
            except Exception:
                pass
        preview_col.controls.append(T(m.get("title", "Video"), "accent", 14, True,
                                      align=ft.TextAlign.CENTER))
        preview_col.controls.append(T(
            f"{m.get('uploader') or '—'} • {human_dur(m.get('duration'))} • "
            f"{m.get('extractor', '')}", "muted", 11, align=ft.TextAlign.CENTER))
        preview_card.visible = True

    def render_choices():
        quality_col.controls.clear()
        w = max(240, st["card_w"] - 72)
        last = None
        for c in st["choices"]:
            if c.kind != last:
                last = c.kind
                quality_col.controls.append(ft.Container(
                    content=T("🎬 वीडियो + 🔊 आवाज़ (एक ही फाइल)" if last == "video"
                              else "🎵 सिर्फ़ आवाज़ (अलग डाउनलोड)",
                              "accent", 13, True, align=ft.TextAlign.CENTER),
                    width=w, padding=4, alignment=ft.Alignment(0, 0)))
            sel = c.key == st["selected"] and c.available
            bg = "#1D4ED8" if sel else (pal()["item"] if c.available else pal()["panel"])
            fg = "#FFFFFF" if sel else pal()["text"]
            mark = "🔘" if sel else ("⚪" if c.available else "🔒")
            col = ft.Column([
                ft.Text(f"{mark}  {c.title}", size=13, weight=ft.FontWeight.BOLD,
                        color=fg if c.available else pal()["muted"]),
                ft.Text(f"📦 {c.meta}", size=11,
                        color="#DCFCE7" if sel else pal()["muted"]),
            ], spacing=3)
            quality_col.controls.append(ft.Container(
                content=col, bgcolor=bg, padding=10, border_radius=11, width=w,
                ink=c.available, opacity=1.0 if c.available else 0.6,
                on_click=(lambda e, k=c.key: pick_choice(k)) if c.available else None))
        cur = next((c for c in st["choices"] if c.key == st["selected"]), None)
        sel_t.value = (f"✓ चुना गया: {cur.title}" if cur else "कोई विकल्प नहीं")
        if st["blocked"] and not ff.ok:
            warn_t.value = ("⚠ FFmpeg नहीं मिला, इसलिए " +
                            ", ".join(f"{h}p" for h in st["blocked"][:4]) +
                            " आवाज़ के साथ नहीं मिल सकती। सेटिंग्स में FFmpeg जोड़ें "
                            "या 🔒 वाली क्वालिटी के बजाय उपलब्ध विकल्प चुनें।")
            warn_card.visible = True
        elif st["last_warn"]:
            warn_t.value = "⚠ " + st["last_warn"]
            warn_card.visible = True
        else:
            warn_card.visible = False

    def render_history():
        hist_col.controls.clear()
        w = max(240, st["card_w"] - 72)
        if not st["hist"]:
            hist_col.controls.append(T("अभी कोई डाउनलोड नहीं।", "muted", 12,
                                       align=ft.TextAlign.CENTER))
            return
        for i, h in enumerate(st["hist"][:60]):
            a = h.get("audio")
            badge = "🔊 आवाज़ सत्यापित" if a is True else (
                "🔇 आवाज़ नहीं मिली!" if a is False else "❔ सत्यापन संभव नहीं")
            bcol = "#166534" if a is True else ("#7F1D1D" if a is False else "#334155")
            rows = [
                ft.Text("✓ " + str(h.get("title", ""))[:90], size=12,
                        weight=ft.FontWeight.BOLD, color=pal()["text"]),
                ft.Text(f"{h.get('quality','')} • {human_size(h.get('size'))} • "
                        f"{time.strftime('%d %b %H:%M', time.localtime(h.get('ts', 0)))}",
                        size=10, color=pal()["muted"]),
                ft.Container(content=ft.Text(badge, size=10, color="#FFFFFF",
                                             weight=ft.FontWeight.BOLD),
                             bgcolor=bcol, padding=5, border_radius=6),
            ]
            acts = [button("📂 फोल्डर", "#334155",
                           lambda e, p=h.get("path", ""): open_folder_of(p), 32, 10)]
            if a is False and ff.ok:
                acts.append(button("🔧 आवाज़ ठीक करें", "#B45309",
                                   lambda e, idx=i: do_repair(idx), 32, 10))
            acts.append(button("🗑 हटाएँ", "#475569",
                               lambda e, idx=i: del_hist(idx), 32, 10))
            rows.append(grp(acts, 3, 110))
            hist_col.controls.append(ft.Container(
                content=ft.Column(rows, spacing=5), bgcolor=pal()["item"],
                padding=10, border_radius=11, width=w))
        layout()

    def chip(label, active, click, enabled=True):
        return ft.Container(
            content=ft.Text(label, size=11, weight=ft.FontWeight.BOLD,
                            color="#FFFFFF" if active else pal()["muted"],
                            text_align=ft.TextAlign.CENTER),
            bgcolor="#1D4ED8" if active else pal()["item"],
            padding=8, border_radius=20, ink=enabled, opacity=1 if enabled else .5,
            on_click=click if enabled else None)

    path_field = ft.TextField(label="सेव फोल्डर पाथ", value=prefs["save_dir"],
                              border_radius=10, visible=False)
    ff_field = ft.TextField(label="FFmpeg का पूरा पाथ (खाली = ऑटो खोज)",
                            value=prefs.get("ff_custom", ""), border_radius=10)
    cookie_field = ft.TextField(label="cookies.txt का पाथ (वैकल्पिक, लॉगिन के लिए)",
                                value=prefs.get("cookies", ""), border_radius=10)

    def render_settings():
        set_col.controls.clear()

        def switch(label, key, after=None):
            def on_ch(e):
                prefs[key] = bool(e.control.value)
                persist()
                if after:
                    after()
                render_settings()
                page.update()
            return ft.Switch(label=label, value=bool(prefs[key]), on_change=on_ch)

        set_col.controls.append(T("🎨 दिखावट / Appearance", "accent", 13, True))
        set_col.controls.append(switch("डार्क मोड", "dark", apply_theme))

        set_col.controls.append(T("📂 सेव फोल्डर / Save folder", "accent", 13, True))
        set_col.controls.append(T(prefs["save_dir"], "muted", 11))
        set_col.controls.append(grp([
            button("📁 चुनें", "#1D4ED8", lambda e: browse(), 38, 12),
            button("✏️ पाथ लिखें", "#334155", lambda e: toggle_path(), 38, 12),
            button("📂 खोलें", "#0F766E", lambda e: open_folder(prefs["save_dir"]), 38, 12),
        ], 3, 110))
        set_col.controls.append(path_field)
        set_col.controls.append(button("✓ पाथ सेव करें", "#15803D",
                                       lambda e: apply_path(path_field.value), 36, 12))
        set_col.controls.append(grp([
            button("📥 Downloads", "#0F172A", lambda e: preset("Download", "Downloads"), 32, 10),
            button("🎬 Movies", "#0F172A", lambda e: preset("Movies", "Videos"), 32, 10),
            button("🎵 Music", "#0F172A", lambda e: preset("Music", "Music"), 32, 10),
        ], 3, 100))

        set_col.controls.append(T("🎞️ कंटेनर / Container", "accent", 13, True))
        set_col.controls.append(grp([
            chip("MP4 (सब जगह चले)", prefs["container"] == "mp4",
                 lambda e: set_pref("container", "mp4")),
            chip("MKV (सुरक्षित मर्ज)", prefs["container"] == "mkv",
                 lambda e: set_pref("container", "mkv")),
            chip("Auto (codec अनुसार)", prefs["container"] == "auto",
                 lambda e: set_pref("container", "auto")),
        ], 3, 130))

        set_col.controls.append(T("🎯 पसंद / Codec preference", "accent", 13, True))
        set_col.controls.append(grp([
            chip("संगतता (H.264+AAC)", prefs["codec"] == "compat",
                 lambda e: set_pref("codec", "compat")),
            chip("गुणवत्ता (VP9/AV1+Opus)", prefs["codec"] == "quality",
                 lambda e: set_pref("codec", "quality")),
        ], 2, 160))

        set_col.controls.append(T("⚡ गति / Speed", "accent", 13, True))
        set_col.controls.append(switch("Turbo (10MB chunks)", "turbo"))
        set_col.controls.append(T(f"समानांतर fragments: {prefs['frags']}", "muted", 11))

        def frag_ch(e):
            prefs["frags"] = int(e.control.value)
            persist()
            render_settings()
            page.update()
        set_col.controls.append(ft.Slider(min=1, max=16, divisions=15,
                                          value=float(prefs["frags"]),
                                          label="{value}", on_change=frag_ch))

        set_col.controls.append(T("⚙️ व्यवहार / Behaviour", "accent", 13, True))
        set_col.controls.append(switch("Cancel पर अधूरी फाइल हटाएँ", "delete_partial"))
        set_col.controls.append(switch("पूरा होने पर फोल्डर खोलें", "open_after"))
        set_col.controls.append(switch("HLS प्रयोगात्मक विकल्प दिखाएँ", "hls"))

        set_col.controls.append(T("🧰 FFmpeg", "accent", 13, True))
        set_col.controls.append(T(ff.version or "नहीं मिला — HD मर्ज बंद रहेगा।",
                                  "muted", 11))
        set_col.controls.append(ff_field)
        set_col.controls.append(grp([
            button("🔎 दोबारा खोजें", "#1D4ED8", lambda e: retest_ff(), 38, 12),
            button("✓ पाथ लागू करें", "#15803D", lambda e: apply_ff_path(), 38, 12),
        ], 2, 150))

        set_col.controls.append(T("🔐 लॉगिन / Cookies", "accent", 13, True))
        set_col.controls.append(cookie_field)
        set_col.controls.append(button("✓ cookies सेव करें", "#15803D",
                                       lambda e: apply_cookies(), 36, 12))
        set_col.controls.append(T(
            f"{APP_NAME} {APP_VER} • केवल उन्हीं वीडियो को डाउनलोड करें जिनका "
            "आपको अधिकार है; प्लेटफ़ॉर्म की शर्तों का पालन करें।", "muted", 10))
        layout()

    # ---------- actions ----------
    def apply_theme():
        p = pal()
        try:
            page.theme_mode = ft.ThemeMode.DARK if prefs["dark"] else ft.ThemeMode.LIGHT
        except Exception:
            pass
        page.bgcolor = p["page"]
        root.bgcolor = p["card"]
        for c, r in static_boxes:
            c.bgcolor = p[r]
        for t, r in static_texts:
            t.color = p[r]
        theme_btn.data.value = "☀️ Light" if prefs["dark"] else "🌙 Dark"
        refresh_nav()
        render_choices()
        render_history()

    def toggle_theme():
        prefs["dark"] = not prefs["dark"]
        persist()
        apply_theme()
        page.update()

    def set_tab(k):
        st["tab"] = k
        refresh_nav()
        dl_body.visible = k == "download"
        hist_body.visible = k == "history"
        set_body.visible = k == "settings"
        if k == "history":
            render_history()
        if k == "settings":
            render_settings()
        page.update()

    def set_pref(k, v):
        prefs[k] = v
        persist()
        if st["meta"]:
            st["choices"], st["blocked"] = build_choices(st["meta_raw"], ff.ok, prefs)
            ensure_selection()
        render_choices()
        render_settings()
        page.update()

    def ensure_selection():
        keys = [c.key for c in st["choices"] if c.available]
        if st["selected"] not in keys:
            st["selected"] = keys[0] if keys else ""

    def pick_choice(k):
        st["selected"] = k
        render_choices()
        page.update()

    def toggle_path():
        path_field.value = prefs["save_dir"]
        path_field.visible = not path_field.visible
        page.update()

    def apply_path(p):
        p = (p or "").strip()
        if not p:
            return
        try:
            os.makedirs(p, exist_ok=True)
            probe = os.path.join(p, ".ultra_write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except Exception as ex:
            say(f"❌ यह फोल्डर लिखने योग्य नहीं: {str(ex)[:80]}", ERR)
            if IS_ANDROID:
                say("❌ Android पर public फोल्डर के लिए अनुमति चाहिए। "
                    "ऐप-निजी फोल्डर आज़माएँ (नीचे प्रीसेट)।", ERR)
            page.update()
            return
        prefs["save_dir"] = p
        persist()
        path_field.visible = False
        folder_t.value = f"📁 {p}"
        say(f"✅ सेव फोल्डर सेट: {p}", OK)
        render_settings()
        page.update()

    def preset(android_sub, desktop_sub):
        if IS_ANDROID:
            appdir = os.environ.get("FLET_APP_STORAGE_DATA")
            p = (os.path.join(appdir, android_sub) if appdir
                 else f"/storage/emulated/0/{android_sub}/UltraVideos")
        else:
            p = os.path.join(os.path.expanduser("~"), desktop_sub, "UltraVideos")
        apply_path(p)

    def open_folder(p):
        try:
            os.makedirs(p, exist_ok=True)
            if IS_WIN:
                os.startfile(p)
            elif IS_MAC:
                subprocess.Popen(["open", p])
            elif not IS_ANDROID:
                subprocess.Popen(["xdg-open", p])
            else:
                toast(f"📁 {p}")
        except Exception:
            toast(f"📁 {p}")

    def open_folder_of(path):
        open_folder(os.path.dirname(path) if path else prefs["save_dir"])

    def browse():
        init = prefs["save_dir"] if os.path.isdir(prefs["save_dir"]) else None
        try:
            if hasattr(ft.FilePicker, "get_directory_path") and \
                    asyncio.iscoroutinefunction(ft.FilePicker.get_directory_path):
                async def go():
                    try:
                        p = await ft.FilePicker().get_directory_path(
                            dialog_title="सेव फोल्डर चुनें", initial_directory=init)
                        if p:
                            apply_path(p)
                    except Exception:
                        toggle_path()
                page.run_task(go)
            else:
                fp = ft.FilePicker(on_result=lambda r: apply_path(getattr(r, "path", None)))
                page.overlay.append(fp)
                page.update()
                fp.get_directory_path(dialog_title="सेव फोल्डर चुनें",
                                      initial_directory=init)
        except Exception:
            toggle_path()

    def retest_ff():
        nonlocal ff
        ff = discover_ffmpeg(ff_field.value.strip() or prefs.get("ff_custom", ""))
        refresh_ff()
        if st.get("meta_raw"):
            st["choices"], st["blocked"] = build_choices(st["meta_raw"], ff.ok, prefs)
            ensure_selection()
        render_choices()
        render_settings()
        say(("✅ FFmpeg मिल गया: " + ff.version) if ff.ok else
            "⚠ FFmpeg नहीं मिला — HD मर्ज उपलब्ध नहीं।", OK if ff.ok else WARN)
        page.update()

    def apply_ff_path():
        prefs["ff_custom"] = ff_field.value.strip()
        persist()
        retest_ff()

    def apply_cookies():
        prefs["cookies"] = cookie_field.value.strip()
        persist()
        say("✅ cookies पाथ सेव हुआ।", OK)
        page.update()

    def do_clear():
        if engine.busy:
            say("⏳ पहले डाउनलोड रोकें, फिर Clear करें।", WARN)
            page.update()
            return
        url_field.value = ""
        st.update(meta={}, choices=[], blocked=[], selected="auto", p=None,
                  done=0, total=0, speed="--", eta="--", last_warn="")
        st["meta_raw"] = None
        preview_card.visible = False
        warn_card.visible = False
        quality_col.controls.clear()
        sel_t.value = "कोई विकल्प नहीं"
        say("✅ साफ़ — नया लिंक डालें।", OK)
        page.update()

    def do_paste():
        def use(txt):
            t = normalize_url(txt)
            if t:
                url_field.value = t
                page.update()
                do_scan()
        try:
            if hasattr(ft, "Clipboard"):
                async def go():
                    try:
                        use(await ft.Clipboard().get())
                    except Exception:
                        say("ℹ️ बॉक्स में Ctrl+V / लंबा दबाकर Paste करें।", INFO)
                        page.update()
                page.run_task(go)
                return
            if hasattr(page, "get_clipboard"):
                use(page.get_clipboard())
                return
            raise RuntimeError
        except Exception:
            say("ℹ️ बॉक्स में Ctrl+V / लंबा दबाकर Paste करें।", INFO)
            page.update()

    def do_scan():
        if st["scanning"]:
            return
        url = normalize_url(url_field.value)
        url_field.value = url
        if not is_url(url):
            say("⚠️ सही लिंक डालें (http/https या youtu.be/…)।", ERR)
            page.update()
            return
        st["scanning"] = True
        say("🔍 क्वालिटी + साइज़ स्कैन हो रहे हैं…", INFO)
        st["p"] = None
        page.update()

        def work():
            try:
                o = {"quiet": True, "noplaylist": True, "skip_download": True,
                     "logger": YdlLog(lambda l, m: None)}
                if ff.ok:
                    o["ffmpeg_location"] = ff.folder
                if prefs.get("cookies") and os.path.isfile(prefs["cookies"]):
                    o["cookiefile"] = prefs["cookies"]
                with yt_dlp.YoutubeDL(o) as ydl:
                    info = ydl.extract_info(url, download=False)
                chs, blocked = build_choices(info, ff.ok, prefs)
                st["meta_raw"] = info
                st["meta"] = {"title": info.get("title"), "thumbnail": info.get("thumbnail"),
                              "uploader": info.get("uploader"), "duration": info.get("duration"),
                              "extractor": info.get("extractor_key", "")}
                st["choices"], st["blocked"] = chs, blocked
                st["selected"] = "auto" if any(c.key == "auto" for c in chs) else \
                    next((c.key for c in chs if c.available), "")
                say(f"✅ {len([c for c in chs if c.available])} विकल्प मिले। "
                    "हर वीडियो विकल्प में आवाज़ शामिल है।", OK)
            except Exception as ex:
                say("❌ स्कैन: " + friendly_error(ex), ERR)
            finally:
                st["scanning"] = False
                st["p"] = 0
                st["rev"] += 1
        threading.Thread(target=work, daemon=True).start()

    def do_start():
        url = normalize_url(url_field.value)
        if not is_url(url):
            say("⚠️ सही लिंक डालें।", ERR)
            page.update()
            return
        ch = next((c for c in st["choices"] if c.key == st["selected"] and c.available), None)
        if ch is None:
            say("⚠️ पहले Scan करें और उपलब्ध क्वालिटी चुनें।", ERR)
            page.update()
            return
        if ch.needs_ffmpeg and not ff.ok:
            say("❌ यह विकल्प FFmpeg माँगता है — सेटिंग्स में FFmpeg जोड़ें।", ERR)
            page.update()
            return
        engine.submit({"url": url, "choice": ch, "prefs": dict(prefs),
                       "save_dir": prefs["save_dir"], "ff": ff,
                       "title": st["meta"].get("title", "Video")})
        say("🚀 कतार में जोड़ा गया…", INFO)
        page.update()

    def do_cancel():
        if engine.busy or st["queue"]:
            engine.cancel()
            engine.drop_queue()
            say("🛑 रोक रहा है, प्रतीक्षा करें…", WARN)
        else:
            say("ℹ️ अभी कोई डाउनलोड नहीं चल रहा।", INFO)
        page.update()

    def del_hist(i):
        try:
            st["hist"].pop(i)
            save_json(HIST, st["hist"])
            render_history()
            page.update()
        except Exception:
            pass

    def do_repair(i):
        try:
            engine.repair(st["hist"][i], ff, dict(prefs))
            say("🔧 आवाज़ जोड़ी जा रही है…", WARN)
            page.update()
        except Exception as ex:
            say("❌ " + friendly_error(ex), ERR)

    def cleanup_partial(path):
        if not path:
            return 0
        base = path[:-5] if path.endswith(".part") else path
        cands = {base + ".part", base + ".ytdl"}
        cands |= set(glob.glob(glob.escape(base) + "*Frag*"))
        n = 0
        for f in cands:
            if os.path.isfile(f):
                try:
                    os.remove(f)
                    n += 1
                except Exception:
                    pass
        return n

    # ---------- engine events ----------
    def on_event(ev):
        t = ev.get("type")
        if t == "progress":
            st["p"], st["done"], st["total"] = ev["p"], ev["done"], ev["total"]
            st["speed"], st["eta"] = ev["speed"], ev["eta"]
            st["cur_file"] = ev.get("file", "")
            st["phase"] = "download"
        elif t == "phase":
            st["phase"] = ev["phase"]
            if ev.get("file"):
                st["cur_file"] = ev["file"]
            say({"download": "⬇️ डाउनलोड हो रहा है…",
                 "merge": "⚙️ मर्ज/कन्वर्ट हो रहा है (आवाज़ जोड़ी जा रही है)…",
                 "repair": "🔧 आवाज़ की मरम्मत हो रही है…"}.get(ev["phase"], "…"),
                INFO if ev["phase"] == "download" else WARN)
        elif t == "log":
            if ev["lvl"] in ("warning", "error"):
                m = ev["msg"]
                if any(k in m.lower() for k in ("merg", "ffmpeg")):
                    st["last_warn"] = m[:160]
                    st["rev"] += 1
        elif t == "queue":
            st["queue"] = ev["n"]
        elif t == "done":
            e = ev["entry"]
            st["hist"].insert(0, e)
            st["hist"] = st["hist"][:200]
            save_json(HIST, st["hist"])
            st["phase"] = "done"
            st["p"] = 1.0
            if e["kind"] == "audio":
                say(f"🎉 ऑडियो सेव हुआ: {e['title'][:60]}", OK)
            elif e["audio"] is True:
                say(f"🎉 पूरा हुआ • 🔊 आवाज़ सत्यापित: {e['title'][:60]}", OK)
            elif e["audio"] is False:
                say(f"⚠️ फाइल बनी पर 🔇 आवाज़ नहीं मिली! इतिहास में 'आवाज़ ठीक करें' दबाएँ।", ERR)
            else:
                say(f"✅ पूरा हुआ (सत्यापन संभव नहीं): {e['title'][:60]}", WARN)
            st["rev"] += 1
            if prefs["open_after"]:
                open_folder_of(e.get("path"))
        elif t == "cancelled":
            st["phase"] = "cancelled"
            st["p"] = 0
            if prefs["delete_partial"]:
                n = cleanup_partial(st.get("cur_file", ""))
                say(f"🛑 रोका गया • अधूरी फाइलें हटाईं ({n})।", WARN)
            else:
                say("🛑 रोका गया • दोबारा Start दबाने पर Resume होगा।", WARN)
        elif t == "error":
            st["phase"] = "error"
            say("❌ " + ev["msg"], ERR)
        elif t == "idle":
            if st["phase"] not in ("error", "cancelled"):
                st["phase"] = "idle"
            st["rev"] += 1

    engine = Downloader(on_event)

    # ---------- bodies ----------
    scan_row = grp([
        button("📋 Paste & Scan", "#334155", lambda e: do_paste()),
        button("🔍 Scan", "#2563EB", lambda e: do_scan()),
        button("✖ Clear", "#475569", lambda e: do_clear()),
    ], 3, 140)

    start_btn = button("⬇️ डाउनलोड शुरू / Resume", "#16A34A", lambda e: do_start(), 52, 14)
    cancel_btn = button("🛑 रोकें", "#7F1D1D", lambda e: do_cancel(), 52, 14)
    action_row = grp([start_btn, cancel_btn], 2, 190, 10)

    dl_body = ft.Column(controls=[
        url_field, scan_row, preview_card, warn_card,
        panel(ft.Column([T("🎞️ क्वालिटी चुनें (टैप करें)", "accent", 14, True,
                           reg=True, align=ft.TextAlign.CENTER),
                         sel_t, quality_col], spacing=8,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER), reg=True),
        panel(ft.Column([folder_t,
                         button("📂 फोल्डर बदलें", "#334155",
                                lambda e: set_tab("settings"), 34, 11)],
                        spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
              reg=True),
        action_row,
        panel(ft.Column([progress_bar, pct_t, status_t, stats_t, queue_t], spacing=6,
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER), reg=True),
    ], spacing=11, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    hist_body = ft.Column(controls=[
        T("📜 हाल के डाउनलोड (सुरक्षित इतिहास)", "accent", 14, True, reg=True,
          align=ft.TextAlign.CENTER),
        button("🗑 पूरा इतिहास साफ़ करें", "#334155",
               lambda e: (st["hist"].clear(), save_json(HIST, []),
                          render_history(), page.update()), 36, 11),
        hist_col,
    ], spacing=10, visible=False, horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    set_body = ft.Column(controls=[set_col], spacing=10, visible=False,
                         horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    root = ft.Container(
        width=st["card_w"], padding=14, border_radius=18, bgcolor=pal()["card"],
        content=ft.Column(controls=[
            ft.Row([ff_chip, theme_btn], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            title_t, sub_t, nav_row, dl_body, hist_body, set_body,
        ], spacing=11, horizontal_alignment=ft.CrossAxisAlignment.CENTER))

    # ---------- UI pump: सारा UI केवल यहीं बदलता है / all UI writes here ----------
    def paint():
        busy = engine.busy or st["scanning"]
        progress_bar.visible = bool(busy or st["phase"] in ("download", "merge", "repair"))
        if st["scanning"] or (busy and st["p"] is None):
            progress_bar.value = None
            pct_t.value = "…"
        else:
            p = st["p"] or 0
            progress_bar.value = p
            pct_t.value = ("✅" if st["phase"] == "done" else f"{p*100:.0f}%")
        status_t.value, status_t.color = st["status"], st["scol"]
        stats_t.value = (f"डेटा: {human_size(st['done'],'--')} / "
                         f"{human_size(st['total'],'--')}  •  स्पीड: {st['speed']}"
                         f"  •  बचा समय: {st['eta']}")
        queue_t.value = (f"कतार में {st['queue']} और बाकी" if st["queue"] else "")
        folder_t.value = f"📁 {prefs['save_dir']}"
        start_btn.data.value = ("⏳ डाउनलोड जारी…" if engine.busy
                                else "⬇️ डाउनलोड शुरू / Resume")
        start_btn.bgcolor = "#475569" if engine.busy else "#16A34A"
        cancel_btn.bgcolor = "#DC2626" if engine.busy else "#7F1D1D"

    last_rev = [-1]

    def tick():
        if st["rev"] != last_rev[0]:
            last_rev[0] = st["rev"]
            render_preview()
            render_choices()
            if st["tab"] == "history":
                render_history()
            layout()
        paint()
        try:
            page.update()
        except Exception:
            pass

    async def pump():
        while not stop.is_set():
            try:
                tick()
            except Exception:
                pass
            await asyncio.sleep(0.2)

    def fit(e=None):
        try:
            w = float(getattr(page, "width", None) or 0)
        except Exception:
            w = 0
        if w <= 0:
            w = 580
        nw = int(max(300, min(660, w - 22)))
        if abs(nw - st["card_w"]) < 6 and e is not None:
            return
        st["card_w"] = nw
        root.width = nw
        layout()
        st["rev"] += 1

    for hook in ("on_resize", "on_resized"):
        try:
            setattr(page, hook, fit)
        except Exception:
            pass
    for hook in ("on_disconnect", "on_close"):
        try:
            setattr(page, hook, lambda e: stop.set())
        except Exception:
            pass

    apply_theme()
    refresh_nav()
    render_settings()
    render_history()
    layout()
    try:
        body = ft.SafeArea(content=ft.Row([root], alignment=ft.MainAxisAlignment.CENTER))
    except Exception:
        body = ft.Row([root], alignment=ft.MainAxisAlignment.CENTER)
    page.add(body)
    fit()
    tick()

    if hasattr(page, "run_task"):
        page.run_task(pump)
    else:
        def loop():
            while not stop.is_set():
                tick()
                time.sleep(0.2)
        threading.Thread(target=loop, daemon=True).start()


# =====================================================================
if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(f"🚀 {APP_NAME} {APP_VER}")
    try:
        if hasattr(ft, "run"):
            ft.run(main)
        else:
            ft.app(target=main)
    except Exception as err:
        print("Fatal:", err)
        traceback.print_exc()
