"""
Ultra Video Downloader Pro  (Flet + yt-dlp)
Run:  python main.py

Layout rules used in this file (to avoid the grey / white screen):
  * No Row(wrap=True)  -> buttons are laid out by explicit widths computed in Python.
  * No expand=... inside nested containers -> every width is explicit and bounded.
  * The card width is recalculated on every window resize (phone / tablet / PC).
"""
import glob
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback

import flet as ft
import yt_dlp


# ======================================================================
# Helpers (pure functions - easy to test)
# ======================================================================
class DownloadCancelledError(Exception):
    pass


CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".ultra_downloader_pro.json")

PALETTE = {
    True: dict(  # dark
        page="#0B0F19", card="#131C2E", panel="#1E293B", item="#0F172A",
        text="#E2E8F0", muted="#94A3B8", accent="#60A5FA",
    ),
    False: dict(  # light
        page="#F1F5F9", card="#FFFFFF", panel="#E2E8F0", item="#CBD5E1",
        text="#0F172A", muted="#475569", accent="#1D4ED8",
    ),
}

OK, WARN, ERR, INFO = "#4ADE80", "#FBBF24", "#F87171", "#60A5FA"


def format_bytes(size, unknown="साइज उपलब्ध नहीं"):
    if not size or size <= 0:
        return unknown
    size = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def format_duration(seconds):
    if not seconds:
        return "—"
    seconds = int(seconds)
    hrs, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    return f"{hrs}h {mins}m {secs}s" if hrs else f"{mins}m {secs}s"


def clean_ansi(text):
    return re.sub(r"\x1b\[[0-9;]*m", "", str(text)).strip()


def looks_like_url(text):
    return bool(re.match(r"^https?://\S+\.\S+", (text or "").strip(), re.I))


def friendly_error(ex):
    msg = clean_ansi(str(ex))
    low = msg.lower()
    if "sign in to confirm" in low or "not a bot" in low:
        return "YouTube ने bot-check माँगा। कुछ देर बाद या दूसरे नेटवर्क पर कोशिश करें।"
    if "private video" in low:
        return "यह वीडियो Private है, डाउनलोड नहीं हो सकता।"
    if "unsupported url" in low:
        return "यह लिंक सपोर्टेड नहीं है। सही वीडियो लिंक डालें।"
    if "http error 403" in low:
        return "सर्वर ने एक्सेस रोका (403)। yt-dlp अपडेट करें: pip install -U yt-dlp"
    if "ffmpeg" in low:
        return "FFmpeg नहीं मिला। मर्ज के लिए FFmpeg इंस्टॉल करें।"
    if "urlopen error" in low or "getaddrinfo" in low or "timed out" in low:
        return "इंटरनेट कनेक्शन की समस्या। नेटवर्क जाँचकर दोबारा कोशिश करें।"
    return msg[:110]


def quality_tag(h):
    if h >= 4320:
        return f"8K UHD ({h}p)"
    if h >= 2160:
        return f"4K Ultra HD ({h}p)"
    if h >= 1440:
        return f"2K QHD ({h}p)"
    if h >= 1080:
        return "1080p Full HD"
    if h >= 720:
        return "720p HD"
    if h >= 480:
        return "480p Standard"
    if h >= 360:
        return "360p Medium"
    return f"{h}p Data Saver"


def default_quality_items(has_ffmpeg):
    """Quality list shown before any scan (sizes unknown until 'Scan')."""
    def f(h):
        if has_ffmpeg:
            return f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/best"
        return f"best[height<={h}]/best"

    fmt_map = {
        "auto_best": "bestvideo+bestaudio/best" if has_ffmpeg else "best[ext=mp4]/best",
        "q_2160": f(2160), "q_1080": f(1080), "q_720": f(720),
        "q_480": f(480), "q_360": f(360),
        "auto_audio": "bestaudio/best",
    }
    scan_hint = "Scan दबाएं"
    items = [
        {"key": "auto_best", "title": "⚡ Auto Best Quality", "size": scan_hint, "kind": "video"},
        {"key": "q_2160", "title": "🎬 4K Ultra HD (2160p)", "size": scan_hint, "kind": "video"},
        {"key": "q_1080", "title": "🎬 1080p Full HD", "size": scan_hint, "kind": "video"},
        {"key": "q_720", "title": "🎬 720p HD", "size": scan_hint, "kind": "video"},
        {"key": "q_480", "title": "🎬 480p Standard", "size": scan_hint, "kind": "video"},
        {"key": "q_360", "title": "🎬 360p Data Saver", "size": scan_hint, "kind": "video"},
        {"key": "auto_audio", "title": "🎵 Best Audio Only", "size": "Audio", "kind": "audio"},
    ]
    return items, fmt_map, {}


def build_quality_items(info, has_ffmpeg):
    """Turn yt-dlp info into (items, format_map, extra_opts) with exact sizes."""
    duration = info.get("duration") or 0
    formats = info.get("formats") or []

    def fsize(f):
        sz = f.get("filesize") or f.get("filesize_approx")
        if not sz and duration:
            br = f.get("tbr") or f.get("vbr") or f.get("abr")
            if br:
                sz = int((br * 1000 / 8) * duration)
        return sz or 0

    audio_formats = [
        f for f in formats
        if f.get("acodec") not in ("none", None) and f.get("vcodec") in ("none", None)
    ]
    audio_formats.sort(key=lambda x: (x.get("abr") or 0, fsize(x)), reverse=True)
    best_audio = audio_formats[0] if audio_formats else None
    best_audio_size = fsize(best_audio) if best_audio else 0
    best_audio_id = best_audio.get("format_id") if best_audio else "bestaudio"

    groups = {}
    for f in formats:
        vcodec, height = f.get("vcodec"), f.get("height")
        if not vcodec or vcodec == "none" or not height:
            continue
        has_aud = f.get("acodec") not in ("none", None)
        size, tbr = fsize(f), (f.get("tbr") or f.get("vbr") or 0)
        slot = "combined" if has_aud else "vo"
        grp = groups.setdefault(height, {"combined": None, "vo": None})
        cur = grp[slot]
        if not cur or (tbr, size) > (cur["tbr"], cur["size"]):
            grp[slot] = {
                "id": f.get("format_id"), "size": size, "tbr": tbr,
                "ext": f.get("ext", "mp4"), "fps": int(f.get("fps") or 30),
            }

    items, fmt_map, extra = [], {}, {}
    for h in sorted(groups, reverse=True):
        comb, vo = groups[h]["combined"], groups[h]["vo"]
        tag = quality_tag(h)

        if vo and has_ffmpeg:
            key = f"res_{h}_merged"
            total = (vo["size"] + best_audio_size) if vo["size"] else 0
            fps = f" {vo['fps']}fps" if vo["fps"] > 30 else ""
            items.append({"key": key, "title": f"🌟 {tag}{fps} (MP4 High-Bitrate)",
                          "size": format_bytes(total), "kind": "video"})
            fmt_map[key] = (f"{vo['id']}+{best_audio_id}/bestvideo[height<={h}]"
                            f"+bestaudio/best[height<={h}]/best")
        if comb:
            key = f"res_{h}_comb"
            fps = f" {comb['fps']}fps" if comb["fps"] > 30 else ""
            items.append({"key": key, "title": f"🎬 {tag}{fps} ({str(comb['ext']).upper()} Direct)",
                          "size": format_bytes(comb["size"]), "kind": "video"})
            fmt_map[key] = f"{comb['id']}/best[height<={h}]/best"
        if vo and not has_ffmpeg and not comb:
            key = f"res_{h}_raw"
            items.append({"key": key, "title": f"📹 {tag} ({str(vo['ext']).upper()}) • बिना आवाज़",
                          "size": format_bytes(vo["size"]), "kind": "video"})
            fmt_map[key] = f"{vo['id']}/best[height<={h}]/best"

    for idx, af in enumerate(audio_formats[:2]):
        key = f"audio_{idx}"
        tag = "Best Original Audio" if idx == 0 else "Medium Saver Audio"
        items.append({"key": key,
                      "title": f"🎵 {tag} ({str(af.get('ext', 'm4a')).upper()} {int(af.get('abr') or 128)}kbps)",
                      "size": format_bytes(fsize(af)), "kind": "audio"})
        fmt_map[key] = f"{af.get('format_id')}/bestaudio/best"

    if has_ffmpeg and audio_formats:
        items.append({"key": "audio_mp3", "title": "🎧 MP3 में बदलें (192 kbps)",
                      "size": format_bytes(best_audio_size), "kind": "audio"})
        fmt_map["audio_mp3"] = "bestaudio/best"
        extra["audio_mp3"] = {"postprocessors": [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]}

    if items:
        first_video = next((i for i in items if i["kind"] == "video"), items[0])
        items.insert(0, {"key": "auto_best", "title": "⚡ Auto Best Quality (Recommended)",
                         "size": first_video["size"], "kind": "video"})
        fmt_map["auto_best"] = "bestvideo+bestaudio/best" if has_ffmpeg else "best[ext=mp4]/best"
    return items, fmt_map, extra


def img_fit():
    try:
        return ft.BoxFit.COVER
    except Exception:
        try:
            return ft.ImageFit.COVER
        except Exception:
            return None


# ======================================================================
# App
# ======================================================================
def main(page: ft.Page):
    try:
        build_app(page)
    except Exception:
        page.add(ft.Text(f"Startup Error:\n{traceback.format_exc()}",
                         color=ERR, selectable=True))
        page.update()


def build_app(page: ft.Page):
    page.title = "Ultra Video Downloader Pro"
    page.padding = 12
    for attr, val in (("theme_mode", ft.ThemeMode.DARK), ("scroll", ft.ScrollMode.AUTO),
                      ("horizontal_alignment", ft.CrossAxisAlignment.CENTER)):
        try:
            setattr(page, attr, val)
        except Exception:
            pass
    try:
        page.window.width = 560
        page.window.height = 900
        page.window.min_width = 340
        page.window.min_height = 560
    except Exception:
        pass

    is_android = os.path.exists("/storage/emulated/0")
    has_ffmpeg = shutil.which("ffmpeg") is not None

    if is_android:
        default_dir = "/storage/emulated/0/Download/UltraVideos"
    else:
        default_dir = os.path.join(os.path.expanduser("~"), "Downloads", "UltraVideos")

    state = {
        "save_dir": default_dir, "dark": True, "turbo": True,
        "delete_partial": False, "open_after": False,
        "cancel": False, "downloading": False, "scanning": False,
        "selected": "auto_best", "items": [], "fmt_map": {}, "extra": {},
        "touched": set(), "card_w": 560, "last_ui": 0.0,
    }

    # ---------- settings persistence ----------
    def load_settings():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for k in ("save_dir", "dark", "turbo", "delete_partial", "open_after"):
                if k in data:
                    state[k] = data[k]
        except Exception:
            pass

    def save_settings():
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump({k: state[k] for k in
                           ("save_dir", "dark", "turbo", "delete_partial", "open_after")}, fh)
        except Exception:
            pass

    load_settings()
    try:
        os.makedirs(state["save_dir"], exist_ok=True)
    except Exception:
        pass

    pal = lambda: PALETTE[state["dark"]]
    themed_text, themed_box = [], []

    # ---------- widget factories ----------
    def T(value="", role="text", size=13, bold=False, color=None):
        t = ft.Text(value, size=size, text_align=ft.TextAlign.CENTER,
                    weight=ft.FontWeight.BOLD if bold else None,
                    color=color or pal()[role])
        if not color:
            themed_text.append((t, role))
        return t

    def box(content, role="panel", padding=14, radius=14, width=None):
        c = ft.Container(content=content, bgcolor=pal()[role], padding=padding,
                        border_radius=radius, width=width)
        themed_box.append((c, role))
        return c

    def btn(label, bg, on_click, height=44, size=13, fg="#FFFFFF"):
        t = ft.Text(label, size=size, weight=ft.FontWeight.BOLD, color=fg,
                    text_align=ft.TextAlign.CENTER)
        return ft.Container(content=t, bgcolor=bg, height=height, border_radius=10,
                            padding=6, alignment=ft.Alignment(0, 0), ink=True,
                            on_click=on_click, data=t)

    def tell(msg, color=INFO):
        status_label.value = msg
        status_label.color = color
        page.update()

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
        except Exception:
            pass

    # ---------- responsive geometry ----------
    def inner_w():            # usable width inside the main card
        return max(220, state["card_w"] - 32)

    def panel_w():            # usable width inside a panel (card inner - panel padding)
        return max(200, inner_w() - 28)

    button_groups = []        # (host Column, [buttons], max_per_row, min_w, width_fn)

    def reg_group(buttons, max_per_row, min_w, width_fn=None, spacing=8):
        host = ft.Column(controls=[], spacing=spacing,
                         horizontal_alignment=ft.CrossAxisAlignment.CENTER)
        button_groups.append((host, buttons, max_per_row, min_w, width_fn or inner_w, spacing))
        return host

    def layout_groups():
        for host, buttons, max_per, min_w, width_fn, gap in button_groups:
            w = width_fn()
            per = max(1, min(max_per, int((w + gap) // (min_w + gap))))
            per = min(per, len(buttons))
            bw = (w - gap * (per - 1)) / per
            rows = []
            for i in range(0, len(buttons), per):
                chunk = buttons[i:i + per]
                for b in chunk:
                    b.width = bw
                rows.append(ft.Row(controls=chunk, spacing=gap,
                                   alignment=ft.MainAxisAlignment.CENTER))
            host.controls = rows

    # ---------- theme ----------
    def apply_theme():
        p = pal()
        try:
            page.theme_mode = ft.ThemeMode.DARK if state["dark"] else ft.ThemeMode.LIGHT
        except Exception:
            pass
        page.bgcolor = p["page"]
        main_card.bgcolor = p["card"]
        for c, role in themed_box:
            c.bgcolor = p[role]
        for t, role in themed_text:
            t.color = p[role]
        theme_txt.value = "☀️ Light Mode" if state["dark"] else "🌙 Dark Mode"
        render_quality_list()

    def toggle_theme(e):
        state["dark"] = not state["dark"]
        apply_theme()
        save_settings()
        page.update()

    # ---------- header ----------
    turbo_txt = ft.Text("", size=12, weight=ft.FontWeight.BOLD, color="#FFFFFF",
                        text_align=ft.TextAlign.CENTER)
    turbo_btn = ft.Container(content=turbo_txt, padding=8, border_radius=8, ink=True)

    def refresh_turbo():
        turbo_txt.value = f"⚡ Turbo 8x: {'ON' if state['turbo'] else 'OFF'}"
        turbo_btn.bgcolor = "#15803D" if state["turbo"] else "#475569"

    def toggle_turbo(e):
        state["turbo"] = not state["turbo"]
        refresh_turbo()
        save_settings()
        page.update()

    turbo_btn.on_click = toggle_turbo
    refresh_turbo()

    theme_txt = ft.Text("☀️ Light Mode", size=12, weight=ft.FontWeight.BOLD,
                        color="#93C5FD", text_align=ft.TextAlign.CENTER)
    theme_btn = ft.Container(content=theme_txt, bgcolor="#1E293B", padding=8,
                             border_radius=8, ink=True, on_click=toggle_theme)

    title_text = T("ULTRA DOWNLOADER PRO", "accent", 24, True)
    sub_text = T("सभी क्वालिटी (4K / 1080p / 720p / 480p / 360p / Audio) + सटीक डेटा साइज (MB/GB)",
                 "muted", 12)
    ffmpeg_badge = T("✓ FFmpeg Active (4K/8K Lossless Merge Ready)" if has_ffmpeg
                     else "⚡ Smart Direct Stream Mode (FFmpeg नहीं मिला)",
                     size=11, bold=True, color=OK if has_ffmpeg else WARN)

    # ---------- URL input ----------
    url_input = ft.TextField(
        label="वीडियो का लिंक यहाँ डालें (YouTube / Instagram / Facebook)",
        hint_text="https://youtu.be/...", border_radius=12,
        text_align=ft.TextAlign.CENTER,
        on_submit=lambda e: fetch_video_info(), height=56,
    )

    # ---------- preview ----------
    preview_col = ft.Column(controls=[], spacing=6,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER)
    preview_card = box(preview_col, "panel", 12, 12)
    preview_card.visible = False

    # ---------- quality list ----------
    banner = T("चुनी गई क्वालिटी: ⚡ Auto Best Quality", size=13, bold=True, color=OK)
    quality_col = ft.Column(controls=[], spacing=6,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def load_default_qualities():
        items, fmt_map, extra = default_quality_items(has_ffmpeg)
        state.update(items=items, fmt_map=fmt_map, extra=extra, selected="auto_best")

    def select_quality(key):
        state["selected"] = key
        it = next((i for i in state["items"] if i["key"] == key), None)
        if it:
            banner.value = f"✓ चुनी गई क्वालिटी: {it['title']}  [ 📦 {it['size']} ]"
        render_quality_list()
        page.update()

    def render_quality_list():
        quality_col.controls.clear()
        w = panel_w()
        compact = w < 380
        badge_w = 118
        title_w = w - 20 - (0 if compact else badge_w + 8)
        for it in state["items"]:
            sel = it["key"] == state["selected"]
            bg = "#1D4ED8" if sel else (pal()["item"])
            fg = "#FFFFFF" if sel else pal()["text"]
            title = ft.Container(
                content=ft.Text(f"{'🔘' if sel else '⚪'}  {it['title']}", size=13,
                                weight=ft.FontWeight.BOLD, color=fg,
                                text_align=ft.TextAlign.CENTER),
                width=title_w, alignment=ft.Alignment(0, 0))
            badge = ft.Container(
                content=ft.Text(f"📦 {it['size']}", size=11, weight=ft.FontWeight.BOLD,
                                color="#FFFFFF", text_align=ft.TextAlign.CENTER),
                bgcolor="#15803D" if sel else "#334155", border_radius=6, padding=6,
                width=(title_w if compact else badge_w), alignment=ft.Alignment(0, 0))
            inner = (ft.Column([title, badge], spacing=4,
                               horizontal_alignment=ft.CrossAxisAlignment.CENTER)
                     if compact else
                     ft.Row([title, badge], spacing=8,
                            alignment=ft.MainAxisAlignment.CENTER,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER))
            quality_col.controls.append(ft.Container(
                content=inner, bgcolor=bg, padding=10, border_radius=10, width=w, ink=True,
                on_click=lambda e, k=it["key"]: select_quality(k)))

    quality_box = box(ft.Column([
        T("🎞️ 1. वीडियो क्वालिटी और डेटा साइज चुनें (क्लिक करें):", "accent", 14, True),
        banner, quality_col],
        spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER))

    load_default_qualities()

    # ---------- save folder ----------
    path_label = T(f"📁 वर्तमान सेव फोल्डर: {state['save_dir']}", size=12, color="#93C5FD")
    custom_path_input = ft.TextField(
        label="कस्टम सेव फोल्डर पाथ", value=state["save_dir"],
        text_align=ft.TextAlign.CENTER, border_radius=10, visible=False)

    def apply_folder_path(new_path):
        new_path = (new_path or "").strip()
        if not new_path:
            return
        try:
            os.makedirs(new_path, exist_ok=True)
            probe = os.path.join(new_path, ".write_test")
            with open(probe, "w") as fh:
                fh.write("ok")
            os.remove(probe)
        except Exception as ex:
            tell(f"❌ यह फोल्डर उपयोग नहीं हो सकता: {str(ex)[:70]}", ERR)
            return
        state["save_dir"] = new_path
        path_label.value = f"📁 वर्तमान सेव फोल्डर: {new_path}"
        custom_path_input.value = new_path
        custom_path_input.visible = False
        save_path_btn.visible = False
        save_settings()
        tell(f"✅ सेव फोल्डर अपडेट हो गया: {new_path}", OK)

    def toggle_manual_path(e=None):
        custom_path_input.value = state["save_dir"]
        custom_path_input.visible = not custom_path_input.visible
        save_path_btn.visible = custom_path_input.visible
        page.update()

    def tk_browse():
        def _run():
            try:
                import tkinter as tk
                from tkinter import filedialog
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                chosen = filedialog.askdirectory(initialdir=state["save_dir"])
                root.destroy()
                if chosen:
                    apply_folder_path(chosen)
                return
            except Exception:
                pass
            if not custom_path_input.visible:
                toggle_manual_path()
        threading.Thread(target=_run, daemon=True).start()

    def browse_click(e=None):
        init = state["save_dir"] if os.path.isdir(state["save_dir"]) else None
        try:
            if inspect.iscoroutinefunction(ft.FilePicker.get_directory_path):   # Flet 1.x
                async def _go():
                    try:
                        p = await ft.FilePicker().get_directory_path(
                            dialog_title="वीडियो सेव फोल्डर चुनें", initial_directory=init)
                    except Exception:
                        tk_browse()
                        return
                    if p:
                        apply_folder_path(p)
                page.run_task(_go)
            else:                                                                # Flet 0.2x
                fp = ft.FilePicker(on_result=lambda r: apply_folder_path(getattr(r, "path", None)))
                page.overlay.append(fp)
                page.update()
                fp.get_directory_path(dialog_title="वीडियो सेव फोल्डर चुनें", initial_directory=init)
        except Exception:
            tk_browse()

    def open_saved_folder(e=None):
        target = state["save_dir"]
        try:
            os.makedirs(target, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(target)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            elif not is_android:
                subprocess.Popen(["xdg-open", target])
            else:
                tell(f"📁 फाइलें यहाँ हैं: {target}", INFO)
        except Exception:
            tell(f"📁 फोल्डर पाथ: {target}", INFO)

    def preset(android_path, *desktop_parts):
        return lambda e: apply_folder_path(
            android_path if is_android else os.path.join(os.path.expanduser("~"), *desktop_parts))

    save_path_btn = btn("✓ नया फोल्डर पाथ सेव करें", "#15803D",
                        lambda e: apply_folder_path(custom_path_input.value))
    save_path_btn.visible = False

    folder_main = reg_group([
        btn("📁 Browse Folder", "#1D4ED8", browse_click, 38),
        btn("✏️ Edit Path", "#334155", toggle_manual_path, 38),
        btn("📂 Open Folder", "#0F766E", open_saved_folder, 38),
    ], 3, 120, panel_w)
    folder_presets = reg_group([
        btn("📥 Downloads", "#0F172A", preset("/storage/emulated/0/Download/UltraVideos",
                                              "Downloads", "UltraVideos"), 34),
        btn("🎬 Movies", "#0F172A", preset("/storage/emulated/0/Movies/UltraVideos",
                                           "Videos", "UltraVideos"), 34),
        btn("🎵 Music", "#0F172A", preset("/storage/emulated/0/Music/UltraAudio",
                                          "Music", "UltraAudio"), 34),
    ], 3, 100, panel_w)

    folder_box = box(ft.Column([
        T("📂 2. फाइल सेव फोल्डर (Phone, Tablet & PC):", "accent", 14, True),
        path_label, folder_main, folder_presets, custom_path_input, save_path_btn],
        spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER))

    # ---------- options ----------
    def make_toggle(label, key):
        t = ft.Text("", size=12, weight=ft.FontWeight.BOLD, color="#FFFFFF",
                    text_align=ft.TextAlign.CENTER)
        c = ft.Container(content=t, height=40, padding=6, border_radius=10, ink=True,
                         alignment=ft.Alignment(0, 0))

        def refresh():
            t.value = f"{'✅' if state[key] else '⬜'} {label}"
            c.bgcolor = "#15803D" if state[key] else "#475569"

        def click(e):
            state[key] = not state[key]
            refresh()
            save_settings()
            page.update()

        c.on_click = click
        refresh()
        return c

    options_row = reg_group([
        make_toggle("Cancel पर अधूरी फाइल हटाएं", "delete_partial"),
        make_toggle("पूरा होने पर फोल्डर खोलें", "open_after"),
    ], 2, 200, panel_w)
    options_box = box(ft.Column([T("⚙️ 3. एक्स्ट्रा सेटिंग्स:", "accent", 14, True), options_row],
                                spacing=8, horizontal_alignment=ft.CrossAxisAlignment.CENTER))

    # ---------- status ----------
    progress_bar = ft.ProgressBar(value=0, visible=False)
    percent_txt = T("", "accent", 28, True)
    status_label = T("✅ ऐप तैयार है! लिंक डालकर 'Scan' या 'Download' दबाएं।",
                     size=14, bold=True, color=OK)
    stats_label = T("डेटा: --  |  स्पीड: --  |  बचा समय: --", "muted", 12)
    status_box = box(ft.Column([progress_bar, percent_txt, status_label, stats_label],
                               spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER))
    history_col = ft.Column(controls=[], spacing=6,
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    # ---------- scan ----------
    def fetch_video_info(e=None):
        if state["scanning"]:
            return
        url = (url_input.value or "").strip()
        if not looks_like_url(url):
            tell("⚠️ कृपया सही लिंक डालें (http:// या https:// से शुरू)।", ERR)
            return
        state["scanning"] = True
        tell("🔍 क्वालिटी और सटीक डेटा साइज स्कैन हो रहे हैं...", INFO)
        progress_bar.visible = True
        progress_bar.value = None
        page.update()

        def _fetch():
            try:
                with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True,
                                       "noplaylist": True}) as ydl:
                    info = ydl.extract_info(url, download=False)
                items, fmt_map, extra = build_quality_items(info, has_ffmpeg)
                if items:
                    state.update(items=items, fmt_map=fmt_map, extra=extra, selected="auto_best")
                    banner.value = (f"✓ चुनी गई क्वालिटी: {items[0]['title']}  "
                                    f"[ 📦 {items[0]['size']} ]")
                    render_quality_list()

                preview_col.controls.clear()
                thumb = info.get("thumbnail")
                if thumb and str(thumb).startswith("http"):
                    try:
                        tw = min(panel_w() - 24, 320)
                        kw = dict(src=thumb, width=tw, height=int(tw * 9 / 16), border_radius=10)
                        fit = img_fit()
                        if fit is not None:
                            kw["fit"] = fit
                        preview_col.controls.append(ft.Image(**kw))
                    except Exception:
                        pass
                preview_col.controls.append(T(f"🎬 {info.get('title', 'Video')}", "accent", 14, True))
                preview_col.controls.append(T(
                    f"चैनल: {info.get('uploader') or '—'}  •  अवधि: "
                    f"{format_duration(info.get('duration'))}  •  {len(items)} विकल्प", "muted", 12))
                preview_card.visible = True
                tell("✅ सभी क्वालिटी और डेटा साइज लोड हो गए! नीचे लिस्ट से चुनें।", OK)
            except Exception as ex:
                tell(f"❌ स्कैन एरर: {friendly_error(ex)}", ERR)
            finally:
                state["scanning"] = False
                progress_bar.visible = False
                progress_bar.value = 0
                page.update()

        threading.Thread(target=_fetch, daemon=True).start()

    # ---------- download engine ----------
    def cleanup_partial():
        """Delete ONLY partial leftovers (.part / .ytdl / -Frag*), never finished files."""
        removed = 0
        for f in list(state["touched"]):
            base = f[:-5] if f.endswith(".part") else f
            cands = {base + ".part", base + ".ytdl"}
            cands.update(glob.glob(glob.escape(base) + "*Frag*"))
            for m in cands:
                if os.path.isfile(m):
                    try:
                        os.remove(m)
                        removed += 1
                    except Exception:
                        pass
        state["touched"].clear()
        return removed

    def progress_hook(d):
        if state["cancel"]:
            raise DownloadCancelledError("Cancelled by user")
        for k in ("filename", "tmpfilename"):
            if d.get(k):
                state["touched"].add(d[k])
        now = time.time()
        if d.get("status") == "downloading":
            if now - state["last_ui"] < 0.15:
                return
            state["last_ui"] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes", 0)
            if total > 0:
                progress_bar.value = min(1.0, done / total)
                percent_txt.value = f"{done * 100 / total:.0f}%"
            speed = clean_ansi(d.get("_speed_str", "--"))
            eta = clean_ansi(d.get("_eta_str", "--"))
            status_label.value = "⬇️ डाउनलोड हो रहा है..."
            status_label.color = INFO
            stats_label.value = (f"डेटा: {format_bytes(done, '--')} / {format_bytes(total, '--')}"
                                 f"  |  स्पीड: {speed}  |  बचा समय: {eta}")
            page.update()
        elif d.get("status") == "finished":
            progress_bar.value = 1.0
            percent_txt.value = "100%"
            status_label.value = "⚙️ फाइल को सुरक्षित सेव / मर्ज किया जा रहा है..."
            status_label.color = WARN
            page.update()

    def pp_hook(d):
        if state["cancel"] and d.get("status") == "started":
            raise DownloadCancelledError("Cancelled by user")

    def set_idle_buttons():
        start_btn.data.value = "⬇️ Start / Resume Download"
        start_btn.bgcolor = "#16A34A"
        cancel_btn.bgcolor = "#7F1D1D"

    def run_download(url, key):
        try:
            os.makedirs(state["save_dir"], exist_ok=True)
        except Exception:
            pass
        fmt = state["fmt_map"].get(
            key, "bestvideo+bestaudio/best" if has_ffmpeg else "best[ext=mp4]/best")
        is_audio = key.startswith("audio") or key == "auto_audio"
        name = "%(title).150B [audio]" if is_audio else "%(title).150B [%(height)sp]"
        opts = {
            "format": fmt,
            "outtmpl": os.path.join(state["save_dir"], name + ".%(ext)s"),
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [pp_hook],
            "noplaylist": True, "continuedl": True, "quiet": True, "no_warnings": True,
            "retries": 10, "fragment_retries": 10,
            "concurrent_fragment_downloads": 8 if state["turbo"] else 2,
        }
        if state["turbo"]:
            opts["http_chunk_size"] = 10 * 1024 * 1024
        if has_ffmpeg and "+" in fmt:
            opts["merge_output_format"] = "mp4"
        opts.update(state["extra"].get(key, {}))

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            title = info.get("title", "Video")
            path = None
            for rd in info.get("requested_downloads") or []:
                path = rd.get("filepath") or path
            size = os.path.getsize(path) if path and os.path.exists(path) else (
                info.get("filesize") or info.get("filesize_approx") or 0)
            q = "Audio" if is_audio else (f"{info.get('height')}p" if info.get("height") else "Video")
            tell(f"🎉 डाउनलोड पूरा हुआ: {title}", OK)
            stats_label.value = f"सेव लोकेशन: {state['save_dir']}"
            percent_txt.value = "✅"
            history_col.controls.insert(0, ft.Container(
                content=T(f"✓ {title}  [{q} • {format_bytes(size, '—')}]", size=12, bold=True,
                          color="#86EFAC"),
                padding=10, border_radius=8, bgcolor="#14532D", width=panel_w()))
            state["touched"].clear()
            toast("✅ डाउनलोड पूरा हुआ!")
            if state["open_after"]:
                open_saved_folder()
        except DownloadCancelledError:
            _on_cancelled()
        except Exception as ex:
            if state["cancel"] or "Cancelled" in str(ex):
                _on_cancelled()
            else:
                tell(f"❌ डाउनलोड त्रुटि: {friendly_error(ex)}", ERR)
        finally:
            state["downloading"] = False
            state["cancel"] = False
            progress_bar.visible = False
            set_idle_buttons()
            page.update()

    def _on_cancelled():
        progress_bar.value = 0
        percent_txt.value = ""
        stats_label.value = "डेटा: --  |  स्पीड: Cancelled  |  बचा समय: --"
        if state["delete_partial"]:
            n = cleanup_partial()
            tell(f"🛑 डाउनलोड Cancel हुआ। अधूरी फाइलें हटाई गईं ({n}).", WARN)
        else:
            tell("🛑 डाउनलोड Cancel हुआ। दोबारा Start दबाने पर वहीं से Resume होगा।", WARN)

    def start_click(e=None):
        if state["downloading"]:
            tell("⏳ एक डाउनलोड पहले से चल रहा है!", WARN)
            return
        url = (url_input.value or "").strip()
        if not looks_like_url(url):
            tell("⚠️ कृपया सही वीडियो लिंक डालें (http:// या https://)।", ERR)
            return
        state.update(downloading=True, cancel=False)
        state["touched"].clear()
        progress_bar.visible = True
        progress_bar.value = 0
        percent_txt.value = "0%"
        start_btn.data.value = "⏳ डाउनलोड जारी है..."
        start_btn.bgcolor = "#475569"
        cancel_btn.bgcolor = "#DC2626"
        tell("🚀 डाउनलोड शुरू हो रहा है...", INFO)
        threading.Thread(target=run_download, args=(url, state["selected"]), daemon=True).start()

    def cancel_click(e=None):
        if state["downloading"]:
            state["cancel"] = True
            tell("🛑 डाउनलोड रोका जा रहा है, कृपया प्रतीक्षा करें...", WARN)
        else:
            tell("ℹ️ अभी कोई डाउनलोड नहीं चल रहा है।", "#94A3B8")

    def apply_pasted(text):
        text = (text or "").strip()
        if text:
            url_input.value = text
            page.update()
            fetch_video_info()

    def paste_click(e=None):
        try:
            if hasattr(ft, "Clipboard"):                      # Flet 1.x
                async def _go():
                    try:
                        apply_pasted(await ft.Clipboard().get())
                    except Exception:
                        tell("ℹ️ बॉक्स में Ctrl+V / लंबा दबाकर Paste करें।", INFO)
                page.run_task(_go)
                return
            if hasattr(page, "get_clipboard"):                # Flet 0.2x
                apply_pasted(page.get_clipboard())
                return
            raise RuntimeError
        except Exception:
            try:
                import tkinter as tk
                r = tk.Tk()
                r.withdraw()
                clip = r.clipboard_get()
                r.destroy()
                apply_pasted(clip)
            except Exception:
                tell("ℹ️ बॉक्स में Ctrl+V / लंबा दबाकर Paste करें।", INFO)

    def clear_click(e=None):
        if state["downloading"]:
            tell("⏳ पहले डाउनलोड Cancel करें, फिर Clear करें।", WARN)
            return
        url_input.value = ""
        preview_card.visible = False
        progress_bar.visible = False
        progress_bar.value = 0
        percent_txt.value = ""
        load_default_qualities()
        banner.value = "चुनी गई क्वालिटी: ⚡ Auto Best Quality"
        render_quality_list()
        tell("✅ साफ कर दिया गया! नया लिंक डालें।", OK)
        stats_label.value = "डेटा: --  |  स्पीड: --  |  बचा समय: --"
        page.update()

    def clear_history(e=None):
        history_col.controls.clear()
        page.update()

    # ---------- main action buttons ----------
    scan_group = reg_group([
        btn("📋 Paste & Scan", "#334155", paste_click),
        btn("🔍 Scan Quality & Size", "#2563EB", fetch_video_info),
        btn("✖ Clear", "#475569", clear_click),
    ], 3, 150)

    start_btn = btn("⬇️ Start / Resume Download", "#16A34A", start_click, 52, 14)
    cancel_btn = btn("🛑 Cancel (रोकें)", "#7F1D1D", cancel_click, 52, 14)
    action_group = reg_group([start_btn, cancel_btn], 2, 200, spacing=10)

    history_title = T("📜 हाल के डाउनलोड (History)", "muted", 13, True)
    clear_hist_btn = btn("🗑 History साफ करें", "#334155", clear_history, 34, 11)
    history_group = reg_group([clear_hist_btn], 1, 160)

    footer = T("Ultra Downloader Pro • सिर्फ़ उन्हीं वीडियो को डाउनलोड करें जिनका आपको अधिकार है।",
               "muted", 10)

    main_card = ft.Container(
        width=state["card_w"], padding=16, border_radius=18, bgcolor=pal()["card"],
        content=ft.Column(controls=[
            ft.Row([turbo_btn, theme_btn], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
            title_text, sub_text, ffmpeg_badge,
            url_input, scan_group, preview_card,
            action_group, status_box,
            quality_box, folder_box, options_box,
            history_title, history_col, history_group, footer,
        ], spacing=12, horizontal_alignment=ft.CrossAxisAlignment.CENTER),
    )

    # ---------- responsive resize ----------
    def fit(e=None):
        try:
            w = float(getattr(page, "width", None) or 0)
        except Exception:
            w = 0
        if w <= 0:
            w = 560
        new_w = int(max(300, min(600, w - 24)))
        if abs(new_w - state["card_w"]) < 6 and e is not None:
            return
        state["card_w"] = new_w
        main_card.width = new_w
        layout_groups()
        render_quality_list()
        for c in history_col.controls:
            c.width = panel_w()
        page.update()

    for hook in ("on_resize", "on_resized"):
        try:
            setattr(page, hook, fit)
        except Exception:
            pass

    set_idle_buttons()
    apply_theme()
    layout_groups()
    try:
        body = ft.SafeArea(content=ft.Row([main_card], alignment=ft.MainAxisAlignment.CENTER))
    except Exception:
        body = ft.Row([main_card], alignment=ft.MainAxisAlignment.CENTER)
    page.add(body)
    fit()
    page.update()


if __name__ == "__main__":
    print("🚀 Starting Ultra Video Downloader Pro...")
    try:
        if hasattr(ft, "run"):
            ft.run(main)
        else:
            ft.app(target=main)
    except Exception as err:
        print("Fatal Error:", err)
        traceback.print_exc()
