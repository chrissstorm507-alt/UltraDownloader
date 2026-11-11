import os
import threading
import flet as ft
import yt_dlp

# नए Flet (v0.27+/v1.0) और पुराने Flet दोनों के लिए ऑटोमैटिक कंपैटिबिलिटी
Colors = getattr(ft, "Colors", getattr(ft, "colors", None))
Icons = getattr(ft, "Icons", getattr(ft, "icons", None))
MainButton = getattr(
    ft, "FilledButton", getattr(ft, "Button", getattr(ft, "ElevatedButton", None))
)
SubButton = getattr(ft, "OutlinedButton", MainButton)
DropdownOption = getattr(
    ft,
    "DropdownOption",
    getattr(getattr(ft, "dropdown", None), "Option", None),
)


def main(page: ft.Page):
    page.title = "Ultra Video Downloader"
    page.theme_mode = ft.ThemeMode.DARK
    page.padding = 20

    try:
        page.window.width = 420
        page.window.height = 720
    except Exception:
        pass

    # Android और Windows दोनों के लिए डिफ़ॉल्ट डाउनलोड पाथ
    if os.path.exists("/storage/emulated/0/Download"):
        save_dir = "/storage/emulated/0/Download/UltraVideos"
    else:
        save_dir = os.path.join(
            os.path.expanduser("~"), "Downloads", "UltraVideos"
        )

    try:
        os.makedirs(save_dir, exist_ok=True)
    except Exception:
        pass

    title_text = ft.Text(
        "अल्ट्रा वीडियो डाउनलोडर",
        size=24,
        weight=ft.FontWeight.BOLD,
        color=Colors.BLUE_400,
    )
    sub_text = ft.Text(
        "YouTube या किसी भी वीडियो का लिंक नीचे डालें",
        size=13,
        color=Colors.GREY_400,
    )

    url_input = ft.TextField(
        label="Video URL",
        hint_text="https://youtu.be/...",
        border_radius=10,
        prefix_icon=Icons.LINK,
        width=360,
    )

    quality_dropdown = ft.Dropdown(
        label="क्वालिटी मोड चुनें",
        value="best_direct",
        width=360,
        border_radius=10,
        options=[
            DropdownOption(
                key="best_direct",
                text="Best Direct MP4 (1080p/720p + Audio)",
            ),
            DropdownOption(
                key="raw_4k",
                text="Raw 4K/8K Video Stream (Zero Loss WebM/MP4)",
            ),
            DropdownOption(
                key="audio",
                text="Best Original Audio (M4A/OPUS)",
            ),
        ],
    )

    progress_bar = ft.ProgressBar(width=360, value=0, visible=False)
    status_label = ft.Text("तैयार है...", size=14, color=Colors.GREY_300)
    path_label = ft.Text(
        f"सेव फोल्डर: {save_dir}", size=11, color=Colors.GREY_500
    )

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            if total > 0:
                progress_bar.value = downloaded / total
            pct = d.get("_percent_str", "0%").strip()
            speed = d.get("_speed_str", "--").strip()
            status_label.value = f"डाउनलोड हो रहा है: {pct} ({speed})"
            status_label.color = Colors.BLUE_300
            page.update()
        elif d["status"] == "finished":
            progress_bar.value = 1.0
            status_label.value = "फाइल सेव की जा रही है..."
            status_label.color = Colors.AMBER_400
            page.update()

    def run_download(url, mode):
        ydl_opts = {
            "outtmpl": os.path.join(
                save_dir, "%(title)s [%(resolution)s].%(ext)s"
            ),
            "progress_hooks": [progress_hook],
            "noplaylist": True,
            "retries": 10,
        }

        if mode == "best_direct":
            ydl_opts["format"] = "best[ext=mp4]/best"
        elif mode == "raw_4k":
            ydl_opts["format"] = "bestvideo"
            ydl_opts["format_sort"] = ["res", "fps", "vbr"]
        else:
            ydl_opts["format"] = "bestaudio/best"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get("title", "Video")
            status_label.value = f"डाउनलोड सफल: {title}"
            status_label.color = Colors.GREEN_400
        except Exception as e:
            status_label.value = f"त्रुटि: {str(e)[:80]}"
            status_label.color = Colors.RED_400
        finally:
            download_btn.disabled = False
            page.update()

    def start_click(e):
        url = (url_input.value or "").strip()
        if not url:
            status_label.value = "कृपया पहले वीडियो का लिंक डालें!"
            status_label.color = Colors.RED_400
            page.update()
            return

        download_btn.disabled = True
        progress_bar.visible = True
        progress_bar.value = 0
        status_label.value = "वीडियो स्ट्रीम खोजी जा रही है..."
        page.update()

        threading.Thread(
            target=run_download,
            args=(url, quality_dropdown.value or "best_direct"),
            daemon=True,
        ).start()

    async def paste_click(e):
        try:
            if hasattr(page, "clipboard"):
                clip = await page.clipboard.get()
            elif hasattr(page, "get_clipboard_async"):
                clip = await page.get_clipboard_async()
            else:
                clip = page.get_clipboard()
            if clip:
                url_input.value = clip.strip()
                page.update()
        except Exception:
            pass

    paste_btn = SubButton(
        "Paste Link", icon=Icons.CONTENT_PASTE, on_click=paste_click
    )
    download_btn = MainButton(
        "डाउनलोड शुरू करें",
        icon=Icons.DOWNLOAD,
        bgcolor=Colors.GREEN_700,
        color=Colors.WHITE,
        width=360,
        height=50,
        on_click=start_click,
    )

    page.add(
        ft.Column(
            controls=[
                title_text,
                sub_text,
                url_input,
                paste_btn,
                quality_dropdown,
                download_btn,
                progress_bar,
                status_label,
                path_label,
            ],
            spacing=15,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )
    )


if hasattr(ft, "run"):
    ft.run(main)
else:
    ft.app(target=main)