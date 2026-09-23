"""
Taskbar Media Overlay

A lightweight always-on-top widget that sits on your taskbar and shows whatever
song/video is currently playing (title, artist, album art, progress bar).

It reads from the Windows System Media Transport Controls (SMTC) API, which
is the same thing that feeds the little media controls you see in the
volume flyout. That means it works with Spotify, YouTube in a browser tab,
VLC, etc. without needing any API keys - Windows already knows what's
playing, we're just asking it.

Everything is controlled from a tray icon (right-click it) instead of a
menu on the widget itself, since the widget is click-through and doesn't
intercept mouse clicks.
"""

import asyncio
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import winreg
from io import BytesIO

import tkinter as tk
import win32api
import win32con
import win32gui
import pystray
from PIL import Image, ImageDraw, ImageFont, ImageTk
from winrt.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as PlaybackStatus,
)
from winrt.windows.storage.streams import Buffer, DataReader, InputStreamOptions

POLL_INTERVAL_MS = 1000  # how often we ask Windows what's playing
TOPMOST_REASSERT_MS = 250  # how often we re-pin the widget on top
IDLE_HIDE_SECONDS = 300  # hide the widget after 5 min of no playback

WIDTH = 220
PAD = 6
PROGRESS_HEIGHT = 3
ART_CORNER_RADIUS = 4

FG_TITLE = (255, 255, 255)
FG_ARTIST = (179, 179, 179)
PROGRESS_BG = (90, 90, 90)
PROGRESS_FG = (30, 215, 96)

# Any pixel drawn in this color becomes transparent.
TRANSPARENT_RGB = (1, 2, 3)
TRANSPARENT_HEX = "#%02x%02x%02x" % TRANSPARENT_RGB

FONTS_DIR = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")

POSITION_CENTER = "center"
POSITION_RIGHT = "right"
TRAY_ICONS_WIDTH = 160  # space to leave clear for the system tray icons

MEDIA_SOURCE_SPOTIFY_ONLY = "spotify_only"
MEDIA_SOURCE_SPOTIFY_AND_BROWSERS = "spotify_and_browsers"
MEDIA_SOURCE_ALL = "all"
MEDIA_SOURCE_LABELS = {
    MEDIA_SOURCE_SPOTIFY_AND_BROWSERS: "Spotify + Browsers",
    MEDIA_SOURCE_SPOTIFY_ONLY: "Spotify Only",
    MEDIA_SOURCE_ALL: "All Media",
}
BROWSER_APP_IDS = ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe")
# Firefox reports a random ID instead of its exe name, so it won't match
# BROWSER_APP_IDS - resolve_friendly_name() below looks up its real name
# instead, and this is what we match that name against.
BROWSER_NAME_HINTS = ("firefox", "chrome", "edge", "brave", "opera", "vivaldi")

_friendly_name_cache = {}


def resolve_friendly_name(app_id):
    """Looks up the human-readable app name Windows shows in the Start
    Menu for an app id, using the same lookup Get-StartApps uses. Cached
    since it spawns a PowerShell process and is a bit slow."""
    if app_id in _friendly_name_cache:
        return _friendly_name_cache[app_id]
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", f"(Get-StartApps | Where-Object {{$_.AppID -eq '{app_id}'}}).Name"],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        name = result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None  # don't cache a failure - it might just be transient
    _friendly_name_cache[app_id] = name
    return name

SETTINGS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "TaskbarMediaOverlay")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")

STARTUP_APP_NAME = "TaskbarMediaOverlay"
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def load_font(size_px, bold=False):
    filename = "segoeuib.ttf" if bold else "segoeui.ttf"
    try:
        return ImageFont.truetype(os.path.join(FONTS_DIR, filename), size_px)
    except OSError:
        return ImageFont.load_default()


def fit_text(draw, text, font, max_width):
    """Shortens text with an ellipsis until it fits in max_width."""
    if draw.textlength(text, font=font) <= max_width:
        return text
    ellipsis = "\u2026"
    while text and draw.textlength(text + ellipsis, font=font) > max_width:
        text = text[:-1]
    return text + ellipsis


def rounded_mask(size, radius):
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def make_process_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except OSError:
        pass


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_settings(data):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f)


def persist_settings(state):
    save_settings(
        {
            "selected_monitors": sorted(state.get_selected_monitors()),
            "hide_on_fullscreen": state.get_hide_on_fullscreen(),
            "position_mode": state.get_position_mode(),
            "media_source_mode": state.get_media_source_mode(),
        }
    )


def _startup_command():
    # pythonw.exe runs without popping up a console window.
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def is_launch_at_startup_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            winreg.QueryValueEx(key, STARTUP_APP_NAME)
            return True
    except OSError:
        return False


def set_launch_at_startup(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, STARTUP_APP_NAME, 0, winreg.REG_SZ, _startup_command())
        else:
            try:
                winreg.DeleteValue(key, STARTUP_APP_NAME)
            except FileNotFoundError:
                pass


def get_monitors():
    """Returns a list of {device, rect, primary, label} for every monitor."""
    monitors = []
    for index, (hmonitor, _, _) in enumerate(win32api.EnumDisplayMonitors(), start=1):
        info = win32api.GetMonitorInfo(hmonitor)
        left, top, right, bottom = info["Monitor"]
        is_primary = bool(info["Flags"] & win32con.MONITORINFOF_PRIMARY)
        label = f"Monitor {index} ({right - left}x{bottom - top})"
        if is_primary:
            label += " - Primary"
        monitors.append({"device": info["Device"], "rect": (left, top, right, bottom), "primary": is_primary, "label": label})
    monitors.sort(key=lambda m: (not m["primary"], m["device"]))
    return monitors


def get_taskbar_rects():
    """Returns {monitor_device: (left, top, right, bottom)} for every
    monitor that currently has a taskbar on it."""
    rects = {}

    def callback(hwnd, _):
        class_name = win32gui.GetClassName(hwnd)
        if class_name not in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            return True
        hmonitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        device = win32api.GetMonitorInfo(hmonitor)["Device"]
        rects[device] = win32gui.GetWindowRect(hwnd)
        return True

    win32gui.EnumWindows(callback, None)
    return rects


# These window classes cover a whole monitor without being a fullscreen app.
_NOT_FULLSCREEN = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}

DWMWA_CLOAKED = 14


def _is_cloaked(hwnd):
    # Some background UWP windows (e.g. "Windows Input Experience") report
    # as visible and full-monitor-sized even though nothing is drawn -
    # DWM's cloaked flag is how Windows itself tells them apart from a
    # window that's actually on screen.
    cloaked = ctypes.c_int(0)
    ctypes.windll.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return cloaked.value != 0


def monitor_has_fullscreen_window(monitor_rect):
    """True if some window's bounds exactly match the monitor."""
    found = False

    def callback(hwnd, _):
        nonlocal found
        if found or not win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
            return True
        if win32gui.GetClassName(hwnd) in _NOT_FULLSCREEN:
            return True
        if win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE) & win32con.WS_EX_TOOLWINDOW:
            return True
        if win32gui.GetWindowRect(hwnd) != monitor_rect:
            return True
        if _is_cloaked(hwnd):
            return True
        found = True
        return True

    win32gui.EnumWindows(callback, None)
    return found


def media_source_matches(app_id, friendly_name, mode):
    app_id = (app_id or "").lower()
    if not app_id:
        return False
    if mode == MEDIA_SOURCE_ALL:
        return True
    is_spotify = "spotify" in app_id
    if mode == MEDIA_SOURCE_SPOTIFY_ONLY:
        return is_spotify
    name = (friendly_name or "").lower()
    is_browser = any(b in app_id for b in BROWSER_APP_IDS) or any(b in name for b in BROWSER_NAME_HINTS)
    return is_spotify or is_browser


class NowPlaying:
    """Holds the latest media info plus user settings, shared between the
    background polling thread and the Tk main thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._title = ""
        self._artist = ""
        self._app_id = ""
        self._friendly_name = None
        self._playing = False
        self._thumbnail = None
        self._position_seconds = 0.0
        self._duration_seconds = 0.0
        self._selected_monitors = set()
        self._hide_on_fullscreen = True
        self._position_mode = POSITION_CENTER
        self._media_source_mode = MEDIA_SOURCE_SPOTIFY_AND_BROWSERS

    def update(self, title, artist, app_id, friendly_name, playing, thumbnail, position_seconds, duration_seconds):
        with self._lock:
            self._title = title
            self._artist = artist
            self._app_id = app_id
            self._friendly_name = friendly_name
            self._playing = playing
            self._thumbnail = thumbnail
            self._position_seconds = position_seconds
            self._duration_seconds = duration_seconds

    def snapshot(self):
        with self._lock:
            return (
                self._title,
                self._artist,
                self._app_id,
                self._friendly_name,
                self._playing,
                self._thumbnail,
                self._position_seconds,
                self._duration_seconds,
            )

    def set_monitor_selected(self, device, selected):
        with self._lock:
            if selected:
                self._selected_monitors.add(device)
            else:
                self._selected_monitors.discard(device)

    def get_selected_monitors(self):
        with self._lock:
            return set(self._selected_monitors)

    def set_hide_on_fullscreen(self, value):
        with self._lock:
            self._hide_on_fullscreen = value

    def get_hide_on_fullscreen(self):
        with self._lock:
            return self._hide_on_fullscreen

    def set_position_mode(self, mode):
        with self._lock:
            self._position_mode = mode

    def get_position_mode(self):
        with self._lock:
            return self._position_mode

    def set_media_source_mode(self, mode):
        with self._lock:
            self._media_source_mode = mode

    def get_media_source_mode(self):
        with self._lock:
            return self._media_source_mode


async def read_thumbnail(thumb_ref):
    if thumb_ref is None:
        return None
    try:
        stream = await thumb_ref.open_read_async()
        if stream.size == 0:
            return None
        buf = Buffer(stream.size)
        await stream.read_async(buf, stream.size, InputStreamOptions.READ_AHEAD)
        reader = DataReader.from_buffer(buf)
        data = bytearray(stream.size)
        reader.read_bytes(data)
        return bytes(data)
    except OSError:
        return None


def read_timeline(session):
    try:
        timeline = session.get_timeline_properties()
        duration = timeline.end_time.total_seconds()
        if duration <= 0:
            return 0.0, 0.0
        return max(timeline.position.total_seconds(), 0.0), duration
    except OSError:
        return 0.0, 0.0


async def pick_session(manager, mode):
    """Windows can track several media sessions at once (e.g. Spotify and a
    browser tab), but get_current_session() only ever returns one of them,
    picked by Windows' own idea of "current" - which can be a paused app
    while something else is actually playing. Prefer whichever session is
    actually playing instead - and if more than one is playing at once,
    prefer whichever one actually matches the current Media Source filter,
    so e.g. a live YouTube stream playing alongside Spotify doesn't get
    hidden just because the other one happened to be picked first. When
    both are playing and both would match, Spotify wins the tie."""
    sessions = list(manager.get_sessions())
    playing = []
    for session in sessions:
        try:
            if session.get_playback_info().playback_status == PlaybackStatus.PLAYING:
                playing.append(session)
        except OSError:
            continue

    playing.sort(key=lambda s: "spotify" not in (s.source_app_user_model_id or "").lower())

    if len(playing) > 1:
        for session in playing:
            app_id = session.source_app_user_model_id or ""
            friendly_name = await asyncio.to_thread(resolve_friendly_name, app_id)
            if media_source_matches(app_id, friendly_name, mode):
                return session

    if playing:
        return playing[0]
    return manager.get_current_session()


async def poll_loop(state, stop_event):
    manager = await MediaManager.request_async()
    last_title, last_artist, last_thumbnail = None, None, None
    last_app_id, last_friendly_name = None, None

    while not stop_event.is_set():
        try:
            session = await pick_session(manager, state.get_media_source_mode())
            if session is None:
                last_title = last_artist = last_thumbnail = None
                last_app_id = last_friendly_name = None
                state.update("", "", "", None, False, None, 0.0, 0.0)
            else:
                info = await session.try_get_media_properties_async()
                playback_info = session.get_playback_info()
                playing = playback_info is not None and playback_info.playback_status == PlaybackStatus.PLAYING
                title = info.title or ""
                artist = info.artist or ""
                app_id = session.source_app_user_model_id or ""

                # Only re-fetch the thumbnail when the track actually changed.
                if title != last_title or artist != last_artist:
                    last_thumbnail = await read_thumbnail(info.thumbnail)
                    last_title, last_artist = title, artist

                # resolve_friendly_name() shells out to PowerShell, so only
                # do it when the app changed, off the main loop's thread.
                if app_id != last_app_id:
                    last_friendly_name = await asyncio.to_thread(resolve_friendly_name, app_id)
                    last_app_id = app_id

                position, duration = read_timeline(session)
                state.update(
                    title=title,
                    artist=artist,
                    app_id=app_id,
                    friendly_name=last_friendly_name,
                    playing=playing,
                    thumbnail=last_thumbnail,
                    position_seconds=position,
                    duration_seconds=duration,
                )
        except Exception:
            # A single bad read (e.g. a session closing mid-read) shouldn't
            # kill this loop forever - just try again next tick.
            pass

        await asyncio.sleep(POLL_INTERVAL_MS / 1000)


def start_background_loop(state, stop_event):
    thread = threading.Thread(target=lambda: asyncio.run(poll_loop(state, stop_event)), daemon=True)
    thread.start()
    return thread


def make_click_through(hwnd):
    """Makes a window ignore mouse clicks and keeps it out of the
    taskbar/alt-tab list."""
    style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
    style |= win32con.WS_EX_TRANSPARENT | win32con.WS_EX_TOOLWINDOW
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)


class OverlayWindow(tk.Toplevel):
    """A borderless, always-on-top, click-through window that shows a
    single image. Pixels drawn in TRANSPARENT_HEX are see-through."""

    def __init__(self, master, x, y, width, height):
        super().__init__(master)
        self.overrideredirect(True)
        self.configure(bg=TRANSPARENT_HEX)
        self.attributes("-topmost", True)
        self.attributes("-transparentcolor", TRANSPARENT_HEX)
        self.geometry(f"{width}x{height}+{x}+{y}")

        self.label = tk.Label(self, bg=TRANSPARENT_HEX, bd=0)
        self.label.place(x=0, y=0, width=width, height=height)
        self._photo = None  # keep a reference alive so Tk doesn't drop it

        self.update_idletasks()
        make_click_through(self.winfo_id())

    def render(self, pil_image):
        self._photo = ImageTk.PhotoImage(pil_image)
        self.label.configure(image=self._photo)
        self.deiconify()

    def move(self, x, y):
        self.geometry(f"+{x}+{y}")

    def hide(self):
        self.withdraw()

    def force_topmost(self):
        self.attributes("-topmost", True)
        self.lift()


class MonitorOverlay:
    """One widget (art + title + artist + progress bar) anchored to one
    monitor's taskbar."""

    def __init__(self, master, device, x, y, width, height, art_size):
        self.device = device
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.art_size = art_size
        self.window = OverlayWindow(master, x, y, width, height)

        title_px = max(round(height * 0.30), 11)
        artist_px = max(round(height * 0.24), 10)
        self.title_font = load_font(title_px, bold=True)
        self.artist_font = load_font(artist_px, bold=False)
        self.text_x = PAD + art_size + PAD
        self.text_width = max(width - self.text_x - PAD, 40)

        self._current_art_bytes = None
        self._art_image = None
        self._art_mask = None
        self._current_title = None
        self._current_artist = None
        self._last_fraction = 0.0
        self._last_has_progress = True
        self.visible = False
        self.hidden_for_fullscreen = False

    def load_artwork(self, data):
        if data == self._current_art_bytes:
            return
        self._current_art_bytes = data
        if not data:
            self._art_image = None
            self._art_mask = None
            return
        try:
            img = Image.open(BytesIO(data)).convert("RGB")
            self._art_image = img.resize((self.art_size, self.art_size), Image.LANCZOS)
            self._art_mask = rounded_mask(self._art_image.size, ART_CORNER_RADIUS)
        except (OSError, TypeError):
            self._art_image = None
            self._art_mask = None

    def render(self, title, artist, fraction, has_progress):
        self._current_title = title
        self._current_artist = artist
        self._last_fraction = fraction
        self._last_has_progress = has_progress
        self.visible = True

        canvas = Image.new("RGB", (self.width, self.height), TRANSPARENT_RGB)
        art_y = (self.height - PROGRESS_HEIGHT - self.art_size) // 2
        if self._art_image is not None:
            canvas.paste(self._art_image, (PAD, art_y), self._art_mask)

        draw = ImageDraw.Draw(canvas)
        title_text = fit_text(draw, title, self.title_font, self.text_width)
        draw.text((self.text_x, art_y), title_text, font=self.title_font, fill=FG_TITLE)

        if artist:
            artist_text = fit_text(draw, artist, self.artist_font, self.text_width)
            artist_y = art_y + round(self.title_font.size * 1.3)
            draw.text((self.text_x, artist_y), artist_text, font=self.artist_font, fill=FG_ARTIST)

        # Some sources never report a real duration, so skip the bar then.
        if has_progress:
            bar_y = self.height - PROGRESS_HEIGHT
            bar_x0, bar_x1 = PAD, self.width - PAD
            draw.rectangle((bar_x0, bar_y, bar_x1, self.height - 1), fill=PROGRESS_BG)
            fill_x1 = bar_x0 + round((bar_x1 - bar_x0) * fraction)
            if fill_x1 > bar_x0:
                draw.rectangle((bar_x0, bar_y, fill_x1, self.height - 1), fill=PROGRESS_FG)

        self.window.render(canvas)

    def hide(self):
        self.visible = False
        self.window.hide()

    def set_hidden_for_fullscreen(self, hidden):
        if hidden == self.hidden_for_fullscreen:
            return
        self.hidden_for_fullscreen = hidden
        if hidden:
            self.window.hide()
        elif self.visible:
            self.render(self._current_title, self._current_artist, self._last_fraction, self._last_has_progress)

    def force_topmost(self):
        self.window.force_topmost()

    def destroy(self):
        self.window.destroy()


class OverlayApp:
    def __init__(self, state, stop_event):
        self.state = state
        self.stop_event = stop_event
        self._quitting = False
        self.overlays = {}  # device -> MonitorOverlay

        # This root window is never shown, it just drives the after() loop.
        self.root = tk.Tk()
        self.root.withdraw()

        self._last_active_time = time.monotonic()

        self._sync_monitors()
        self.root.after(200, self._refresh)
        self.root.after(200, self._force_topmost)

    def _sync_monitors(self):
        selected = self.state.get_selected_monitors()
        monitors = {m["device"]: m for m in get_monitors()}
        taskbars = get_taskbar_rects()

        for device in list(self.overlays.keys()):
            if device not in selected or device not in monitors:
                self.overlays.pop(device).destroy()

        for device in selected:
            if device not in monitors:
                continue
            x, y, width, height, art_size = self._compute_geometry(monitors[device], taskbars.get(device))
            if device in self.overlays:
                overlay = self.overlays[device]
                if (x, y) != (overlay.x, overlay.y):
                    overlay.window.move(x, y)
                    overlay.x, overlay.y = x, y
            else:
                self.overlays[device] = MonitorOverlay(self.root, device, x, y, width, height, art_size)

        return monitors

    def _compute_geometry(self, monitor, taskbar_rect):
        width = WIDTH
        if taskbar_rect:
            t_left, t_top, t_right, t_bottom = taskbar_rect
            height = max(t_bottom - t_top - 4, 30)
            y = t_top + (t_bottom - t_top - height) // 2
            if self.state.get_position_mode() == POSITION_RIGHT:
                x = max(t_right - TRAY_ICONS_WIDTH - width, t_left)
            else:
                x = (t_left + t_right) // 2 - width // 2
        else:
            m_left, m_top, m_right, m_bottom = monitor["rect"]
            height = 40
            y = m_bottom - height - 8
            x = (m_left + m_right) // 2 - width // 2

        art_size = max(height - PROGRESS_HEIGHT - 12, 16)
        return x, y, width, height, art_size

    def _force_topmost(self):
        if self._quitting:
            return
        # Right-clicking a widget can pop the taskbar's own menu on top of
        # it, so keep re-pinning ourselves on top
        for overlay in self.overlays.values():
            if overlay.visible and not overlay.hidden_for_fullscreen:
                overlay.force_topmost()
        self.root.after(TOPMOST_REASSERT_MS, self._force_topmost)

    def quit(self):
        if self._quitting:
            return
        self._quitting = True
        self.stop_event.set()
        for overlay in self.overlays.values():
            overlay.destroy()
        self.overlays.clear()
        self.root.destroy()

    def _refresh(self):
        if self._quitting:
            return

        try:
            monitors = self._sync_monitors()
            hide_on_fullscreen = self.state.get_hide_on_fullscreen()
            title, artist, app_id, friendly_name, playing, thumbnail, position, duration = self.state.snapshot()
            source_matches = media_source_matches(app_id, friendly_name, self.state.get_media_source_mode())

            now = time.monotonic()
            if source_matches and title and playing:
                self._last_active_time = now
            idle = (now - self._last_active_time) > IDLE_HIDE_SECONDS

            if source_matches and title and not idle:
                has_progress = duration > 0
                fraction = max(0.0, min((position / duration) if has_progress else 0.0, 1.0))
                for device, overlay in self.overlays.items():
                    overlay.load_artwork(thumbnail)
                    monitor = monitors.get(device)
                    fullscreen = (
                        hide_on_fullscreen and monitor is not None and monitor_has_fullscreen_window(monitor["rect"])
                    )
                    overlay.set_hidden_for_fullscreen(fullscreen)
                    if not fullscreen:
                        overlay.render(title, artist, fraction, has_progress)
            else:
                if not (source_matches and title):
                    self._last_active_time = now
                for overlay in self.overlays.values():
                    overlay.hide()
        except Exception:
            pass

        self.root.after(POLL_INTERVAL_MS, self._refresh)

    def run(self):
        self.root.mainloop()


def build_tray_icon_image():
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    green = (30, 215, 96, 255)
    bar_w, gap = 9, 7
    heights = (0.4, 0.85, 0.6)
    total_w = bar_w * len(heights) + gap * (len(heights) - 1)
    x0 = (size - total_w) / 2
    y1 = size * 0.76
    for i, h in enumerate(heights):
        x = x0 + i * (bar_w + gap)
        y0 = y1 - size * 0.5 * h
        draw.rounded_rectangle((x, y0, x + bar_w, y1), radius=3, fill=green)
    return img


def build_tray_icon(state, app):
    def toggle_monitor(device):
        def handler(icon, item):
            state.set_monitor_selected(device, device not in state.get_selected_monitors())
            persist_settings(state)

        return handler

    def toggle_hide_on_fullscreen(icon, item):
        state.set_hide_on_fullscreen(not state.get_hide_on_fullscreen())
        persist_settings(state)

    def set_position_mode(mode):
        def handler(icon, item):
            state.set_position_mode(mode)
            persist_settings(state)

        return handler

    def set_media_source_mode(mode):
        def handler(icon, item):
            state.set_media_source_mode(mode)
            persist_settings(state)

        return handler

    def toggle_launch_at_startup(icon, item):
        set_launch_at_startup(not is_launch_at_startup_enabled())

    def on_quit(icon, item):
        icon.stop()
        app.root.after(0, app.quit)

    monitors = get_monitors()
    menu_items = []

    if monitors:
        monitor_menu = pystray.Menu(
            *(
                pystray.MenuItem(
                    m["label"],
                    toggle_monitor(m["device"]),
                    checked=lambda item, d=m["device"]: d in state.get_selected_monitors(),
                )
                for m in monitors
            )
        )
        menu_items.append(pystray.MenuItem("Monitors", monitor_menu))

    menu_items.append(
        pystray.MenuItem(
            "Hide on Fullscreen Monitors",
            toggle_hide_on_fullscreen,
            checked=lambda item: state.get_hide_on_fullscreen(),
        )
    )

    position_menu = pystray.Menu(
        pystray.MenuItem(
            "Centered",
            set_position_mode(POSITION_CENTER),
            checked=lambda item: state.get_position_mode() == POSITION_CENTER,
            radio=True,
        ),
        pystray.MenuItem(
            "Right Side",
            set_position_mode(POSITION_RIGHT),
            checked=lambda item: state.get_position_mode() == POSITION_RIGHT,
            radio=True,
        ),
    )
    menu_items.append(pystray.MenuItem("Position", position_menu))

    media_source_menu = pystray.Menu(
        *(
            pystray.MenuItem(
                MEDIA_SOURCE_LABELS[mode],
                set_media_source_mode(mode),
                checked=lambda item, m=mode: state.get_media_source_mode() == m,
                radio=True,
            )
            for mode in (MEDIA_SOURCE_SPOTIFY_AND_BROWSERS, MEDIA_SOURCE_SPOTIFY_ONLY, MEDIA_SOURCE_ALL)
        )
    )
    menu_items.append(pystray.MenuItem("Media Source", media_source_menu))

    menu_items.append(
        pystray.MenuItem(
            "Launch at Startup", toggle_launch_at_startup, checked=lambda item: is_launch_at_startup_enabled()
        )
    )

    menu_items.append(pystray.Menu.SEPARATOR)
    menu_items.append(pystray.MenuItem("Quit", on_quit))

    return pystray.Icon("taskbar-media-overlay", build_tray_icon_image(), "Taskbar Media Overlay", pystray.Menu(*menu_items))


def main():
    make_process_dpi_aware()

    state = NowPlaying()
    saved = load_settings()
    monitors = get_monitors()
    monitor_devices = {m["device"] for m in monitors}
    saved_monitors = [d for d in saved.get("selected_monitors", []) if d in monitor_devices]

    if saved_monitors:
        for device in saved_monitors:
            state.set_monitor_selected(device, True)
    else:
        primary = next((m for m in monitors if m["primary"]), monitors[0] if monitors else None)
        if primary:
            state.set_monitor_selected(primary["device"], True)

    if "hide_on_fullscreen" in saved:
        state.set_hide_on_fullscreen(saved["hide_on_fullscreen"])
    if saved.get("position_mode") in (POSITION_CENTER, POSITION_RIGHT):
        state.set_position_mode(saved["position_mode"])
    if saved.get("media_source_mode") in MEDIA_SOURCE_LABELS:
        state.set_media_source_mode(saved["media_source_mode"])

    stop_event = threading.Event()
    start_background_loop(state, stop_event)

    app = OverlayApp(state, stop_event)
    tray_icon = build_tray_icon(state, app)
    threading.Thread(target=tray_icon.run, daemon=True).start()

    try:
        app.run()
    finally:
        stop_event.set()
        tray_icon.stop()


if __name__ == "__main__":
    main()
