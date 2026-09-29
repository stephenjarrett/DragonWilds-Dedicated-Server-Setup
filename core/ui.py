"""Desktop interface. All widget access stays on Tk's main thread."""
from datetime import datetime
import copy
import os
from pathlib import Path
import queue
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import webbrowser
import customtkinter as ctk
from PIL import Image, ImageDraw

from core import installer, scheduler, settings
from core.service import ManagerService

VERSION = "3.0.0"
C = {"bg": "#292B30", "sidebar": "#202226", "panel": "#24262B", "input": "#1D1F23",
     "border": "#3A3D45", "text": "#F2F3F7", "muted": "#A2A6B3", "accent": "#8278F7",
     "accent_dark": "#343044", "warn": "#E9BE75", "error": "#EF969F", "blue": "#B5B0FC",
     "success": "#81D6B0"}
ctk.set_appearance_mode("dark")


def nav_icon(kind):
    """Small original line icons, drawn at 3x for crisp display scaling."""
    image = Image.new("RGBA", (60, 60))
    draw = ImageDraw.Draw(image)
    ink = "#C7CAD5"
    if kind == "Overview":
        for x in (9, 33):
            for y in (9, 33):
                draw.rounded_rectangle((x, y, x + 17, y + 17), radius=3, outline=ink, width=3)
    elif kind == "Automation":
        draw.ellipse((7, 7, 53, 53), outline=ink, width=3)
        draw.line((30, 16, 30, 31, 40, 37), fill=ink, width=3)
    elif kind == "Backups":
        draw.rounded_rectangle((8, 13, 52, 49), radius=4, outline=ink, width=3)
        draw.line((8, 23, 52, 23), fill=ink, width=3)
        draw.line((23, 32, 37, 32), fill=ink, width=3)
    elif kind == "Server settings":
        for y, x in ((15, 21), (30, 39), (45, 26)):
            draw.line((9, y, 51, y), fill=ink, width=3)
            draw.ellipse((x - 5, y - 5, x + 5, y + 5), fill=C["sidebar"], outline=ink, width=3)
    else:
        draw.line((5, 32, 17, 32, 25, 13, 34, 48, 42, 27, 54, 27), fill=ink, width=3)
    return ctk.CTkImage(light_image=image, dark_image=image, size=(20, 20))


def timestamp(value, fallback="Not yet"):
    try:
        return datetime.fromtimestamp(value).strftime("%b %d, %I:%M %p") if value else fallback
    except (TypeError, ValueError, OSError):
        return fallback


def label(parent, text="", size=10, color="text", bold=False, **kwargs):
    return tk.Label(parent, text=text, bg=parent.cget("bg"), fg=C.get(color, color),
                    font=("Segoe UI", size, "bold" if bold else "normal"), **kwargs)


class ScrollPage(tk.Frame):
    def __init__(self, parent):
        super().__init__(parent, bg=C["bg"])
        self.canvas = tk.Canvas(self, bg=C["bg"], highlightthickness=0)
        bar = ctk.CTkScrollbar(self, orientation="vertical", command=self.canvas.yview,
                              fg_color=C["bg"], button_color=C["border"], button_hover_color="#555966", width=10)
        self.body = tk.Frame(self.canvas, bg=C["bg"])
        self.window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))


class DragonwildsManager(tk.Tk):
    def __init__(self, minimized=False, show_window=True):
        super().__init__()
        self.withdraw()
        self.title(f"Dragonwilds Server Manager · {VERSION}")
        self.configure(bg=C["bg"])
        self.minsize(1000, 720)
        self.geometry("1180x860")
        self.prefs = settings.load()
        self.events = queue.Queue()
        self.snapshot = {}
        self.action_buttons, self.inputs, self.log_lines = [], [], []
        self.dirty = False
        self.current_page = "Overview"
        self.variables = {key: tk.BooleanVar(value=value) if isinstance(value, bool) else tk.StringVar(value=str(value))
                          for key, value in self.prefs.items()}
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TProgressbar", background=C["accent"], troughcolor=C["input"], borderwidth=0)
        style.configure("Treeview", background=C["panel"], fieldbackground=C["panel"], foreground=C["text"],
                        rowheight=34, borderwidth=0, font=("Segoe UI", 10))
        style.configure("Treeview.Heading", background=C["panel"], foreground=C["muted"],
                        bordercolor=C["border"], lightcolor=C["border"], darkcolor=C["border"],
                        relief="flat", font=("Segoe UI", 9, "bold"))
        style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        style.map("Treeview", background=[("selected", C["accent_dark"])], foreground=[("selected", C["accent"])])
        self._build()
        for var in self.variables.values():
            var.trace_add("write", self._mark_dirty)
        self.service = ManagerService(self.prefs, self.events)
        self.service.start()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind_all("<MouseWheel>", self._scroll)
        self.bind("<Control-s>", lambda e: self._save())
        if show_window:
            self.deiconify()
            self.after(120, self._dark_titlebar)
            if minimized:
                self.after(100, self.iconify)

    def _button(self, parent, text, command, primary=False, danger=False, tracked=True):
        button = ctk.CTkButton(parent, text=text, command=command, corner_radius=6,
                               height=36, width=max(76, len(text) * 7 + 28), border_spacing=10,
                               fg_color=C["accent"] if primary else C["panel"],
                               text_color=C["error"] if danger else C["text"],
                               hover_color="#9289FF" if primary else "#34373F",
                               border_width=0 if primary else 1, border_color=C["border"],
                               text_color_disabled="#686C79", font=("Segoe UI", 13, "bold"))
        if tracked:
            self.action_buttons.append(button)
        return button

    def _card(self, parent, title=None, subtitle=None):
        shell = ctk.CTkFrame(parent, fg_color=C["panel"], border_color=C["border"], border_width=1, corner_radius=10)
        shell.pack(fill="x", pady=(0, 16))
        body = tk.Frame(shell, bg=C["panel"], padx=12, pady=8)
        body.pack(fill="both", expand=True, padx=10, pady=10)
        if title:
            label(body, title, size=12, bold=True).pack(anchor="w", pady=(0, 4))
        if subtitle:
            label(body, subtitle, color="muted", wraplength=730, justify="left").pack(anchor="w", pady=(0, 12))
        return body

    def _heading(self, parent, title, description):
        label(parent, title, size=22, bold=True).pack(anchor="w", pady=(4, 6))
        label(parent, description, color="muted", wraplength=760, justify="left").pack(anchor="w", pady=(0, 24))

    def _dark_titlebar(self):
        if os.name == "nt":
            import ctypes
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            enabled = ctypes.c_int(1)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(enabled), 4)

    def _build(self):
        sidebar = tk.Frame(self, bg=C["sidebar"], width=218)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand_row = tk.Frame(sidebar, bg=C["sidebar"])
        brand_row.pack(fill="x", padx=20, pady=(24, 28))
        brand = tk.Canvas(brand_row, width=34, height=36, bg=C["sidebar"], highlightthickness=0)
        brand.pack(side="left", padx=(0, 11))
        brand.create_polygon(17, 2, 31, 10, 31, 26, 17, 34, 3, 26, 3, 10,
                             fill=C["accent_dark"], outline=C["accent"], width=2)
        brand.create_text(17, 18, text="D", fill=C["blue"], font=("Segoe UI", 17, "bold"))
        title = tk.Frame(brand_row, bg=C["sidebar"])
        title.pack(side="left")
        label(title, "Dragonwilds", size=13, bold=True).pack(anchor="w")
        label(title, "SERVER MANAGER", size=7, color="muted").pack(anchor="w", pady=(3, 0))
        label(sidebar, "WORKSPACE", size=8, bold=True, color="muted").pack(anchor="w", padx=24, pady=(0, 8))
        self.nav, self.nav_icons = {}, {}
        for name in ("Overview", "Automation", "Backups", "Activity", "Server settings"):
            if name == "Server settings":
                tk.Frame(sidebar, bg=C["border"], height=1).pack(fill="x", padx=20, pady=16)
            icon = nav_icon(name)
            self.nav_icons[name] = icon
            button = self._button(sidebar, name, lambda n=name: self._show(n), tracked=False)
            button.configure(anchor="w", fg_color=C["sidebar"], border_width=0,
                             image=icon, compound="left", height=42, font=("Segoe UI", 13))
            button.pack(fill="x", padx=12, pady=3)
            self.nav[name] = button
        footer = tk.Frame(sidebar, bg=C["sidebar"])
        footer.pack(side="bottom", fill="x", padx=20, pady=22)
        self.automation_badge = label(footer, "●  AUTOMATION OFF", size=8, color="muted", bold=True)
        self.automation_badge.pack(anchor="w", pady=(0, 14))
        label(footer, f"Dragonwilds Manager  {VERSION}", size=8, color="muted").pack(anchor="w")
        link = label(footer, "Based on Obietek's project  ↗", size=8, color="muted", cursor="hand2")
        link.pack(anchor="w", pady=(5, 0))
        link.bind("<Button-1>", lambda e: webbrowser.open("https://github.com/Obietek/DragonWilds-Dedicated-Server-Setup"))
        right = tk.Frame(self, bg=C["bg"])
        right.pack(side="left", fill="both", expand=True)
        holder = tk.Frame(right, bg=C["bg"])
        holder.pack(fill="both", expand=True, padx=(28, 18), pady=(26, 0))
        self.pages = {name: ScrollPage(holder) for name in self.nav}
        for name, build in (("Overview", self._overview), ("Server settings", self._server),
                            ("Automation", self._automation), ("Backups", self._backups), ("Activity", self._activity)):
            build(self.pages[name].body)
        tk.Frame(right, bg=C["border"], height=1).pack(fill="x", padx=28, pady=(12, 0))
        bottom = tk.Frame(right, bg=C["bg"], padx=28, pady=12)
        bottom.pack(fill="x")
        self.save_button = self._button(bottom, "Save changes", self._save, primary=True)
        self.save_button.pack(side="right")
        self.save_status = label(bottom, "All changes saved", color="muted", size=9)
        self.save_status.pack(side="right", padx=14)
        self.progress = ttk.Progressbar(bottom, mode="indeterminate", length=85)
        self.operation = label(bottom, "Ready", size=9, color="muted", anchor="w", wraplength=430)
        self.operation.pack(side="left", fill="x", expand=True)
        self._show("Overview")

    def _overview(self, page):
        heading = tk.Frame(page, bg=C["bg"])
        heading.pack(fill="x", pady=(0, 24))
        text = tk.Frame(heading, bg=C["bg"])
        text.pack(side="left")
        label(text, "Server overview", size=23, bold=True).pack(anchor="w")
        self.overview_summary = label(text, "Connect a server to get started", color="muted")
        self.overview_summary.pack(anchor="w", pady=(7, 0))
        self._button(heading, "Check updates", lambda: self._action("check_update")).pack(side="right", anchor="center", padx=(10, 8))

        self.empty_section = tk.Frame(page, bg=C["bg"])
        self.empty_section.pack(fill="x")
        empty = self._card(self.empty_section)
        emblem = tk.Canvas(empty, width=66, height=68, bg=C["panel"], highlightthickness=0)
        emblem.pack(pady=(22, 14))
        emblem.create_oval(8, 8, 58, 58, fill=C["accent_dark"], outline=C["accent"], width=2)
        emblem.create_polygon(22, 39, 31, 21, 35, 21, 45, 39, fill="", outline=C["blue"], width=2)
        emblem.create_line(28, 34, 38, 34, fill=C["blue"], width=2)
        label(empty, "A home for your Dragonwilds server", size=18, bold=True).pack()
        label(empty, "Connect your existing installation to manage updates and backups.\nYour world stays right where it is.",
              color="muted", justify="center", wraplength=620).pack(pady=(12, 22))
        actions = tk.Frame(empty, bg=C["panel"])
        actions.pack(pady=(0, 16))
        self._button(actions, "Connect existing server", self._connect_existing, primary=True).pack(side="left", padx=(0, 10))
        self._button(actions, "Set up a new server", lambda: self._show("Server settings")).pack(side="left")
        label(empty, "Automatic maintenance is off until you enable it.", color="muted", size=9).pack(pady=(0, 18))

        self.server_section = tk.Frame(page, bg=C["bg"])
        hero = self._card(self.server_section)
        self.server_badge = label(hero, "●  SERVER OFFLINE", size=9, color="muted", bold=True)
        self.server_badge.pack(anchor="w", pady=(2, 8))
        self.server_title = label(hero, "Dragonwilds dedicated server", size=20, bold=True)
        self.server_title.pack(anchor="w")
        self.server_detail = label(hero, "", color="muted", wraplength=730, justify="left")
        self.server_detail.pack(anchor="w", pady=(6, 18))
        tk.Frame(hero, bg=C["border"], height=1).pack(fill="x", pady=(0, 18))
        stats = tk.Frame(hero, bg=C["panel"])
        stats.pack(fill="x")
        self.metrics = {}
        for column, (key, title) in enumerate((("uptime", "UPTIME"), ("players", "PLAYERS"), ("build", "INSTALLED BUILD"), ("latest", "LATEST BUILD"))):
            stats.columnconfigure(column, weight=1, uniform="metric")
            frame = tk.Frame(stats, bg=C["panel"])
            frame.grid(row=0, column=column, sticky="ew")
            label(frame, title, size=8, color="muted", bold=True).pack(anchor="w")
            self.metrics[key] = label(frame, "—", size=16, bold=True)
            self.metrics[key].pack(anchor="w", pady=(5, 2))
        actions = tk.Frame(hero, bg=C["panel"])
        actions.pack(fill="x", pady=(22, 2))
        self.start_button = self._button(actions, "Start server", lambda: self._action("start"), primary=True)
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = self._button(actions, "Stop", lambda: self._confirm("stop"), danger=True)
        self.stop_button.pack(side="left", padx=(0, 8))
        self.restart_button = self._button(actions, "Restart", lambda: self._confirm("restart"))
        self.restart_button.pack(side="left", padx=(0, 8))
        self._button(actions, "Open server log", self._open_server_log, tracked=False).pack(side="right")

        self.overview_bottom = tk.Frame(page, bg=C["bg"])
        self.overview_bottom.pack(fill="x")
        self.overview_bottom.columnconfigure(0, weight=1, uniform="summary")
        self.overview_bottom.columnconfigure(1, weight=1, uniform="summary")
        left = tk.Frame(self.overview_bottom, bg=C["bg"])
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right = tk.Frame(self.overview_bottom, bg=C["bg"])
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        maintenance = self._card(left, "Updates & protection")
        self.update_status = label(maintenance, "Check for a new public build.", color="muted", wraplength=330, justify="left", anchor="w", height=3)
        self.update_status.pack(fill="x", pady=(0, 12))
        buttons = tk.Frame(maintenance, bg=C["panel"])
        buttons.pack(fill="x")
        self._button(buttons, "Apply update", lambda: self._confirm("apply_update")).pack(side="left", padx=(0, 8))
        self._button(buttons, "Back up now", lambda: self._action("backup")).pack(side="left")
        schedule = self._card(right, "Automation")
        self.schedule_text = label(schedule, "Schedules are off", color="muted", justify="left", wraplength=330)
        self.schedule_text.pack(anchor="w", pady=(5, 12))
        self._button(schedule, "Manage schedules", lambda: self._show("Automation"), tracked=False).pack(anchor="w")
        self.last_result = label(page, "", color="muted", wraplength=750, justify="left")
        self.last_result.pack(anchor="w", pady=(0, 16))

    def _connect_existing(self):
        self._show("Server settings")
        self._browse("server_exe", "exe")

    def _field(self, parent, key, title, hint=None, secret=False, browse=None):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x", pady=7)
        label(row, title, color="muted", anchor="w", width=21).pack(side="left", anchor="n", pady=6)
        right = tk.Frame(row, bg=row.cget("bg"))
        right.pack(side="left", fill="x", expand=True)
        line = tk.Frame(right, bg=row.cget("bg"))
        line.pack(fill="x")
        entry = ctk.CTkEntry(line, textvariable=self.variables[key], fg_color=C["input"], text_color=C["text"],
                             font=("Segoe UI", 13), show="•" if secret else "", width=220, height=36,
                             corner_radius=6, border_color=C["border"], border_width=1)
        entry.pack(side="left", fill="x", expand=True)
        self.inputs.append(entry)
        if browse:
            self._button(line, "Browse", lambda: self._browse(key, browse)).pack(side="left", padx=(8, 0))
        if secret:
            self._button(line, "Show", lambda e=entry: e.configure(show="" if e.cget("show") else "•"), tracked=False).pack(side="left", padx=(8, 0))
        if hint:
            label(right, hint, color="muted", size=9, wraplength=490, justify="left").pack(anchor="w", pady=(5, 0))

    def _toggle(self, parent, key, title):
        check = ctk.CTkSwitch(parent, text=title, variable=self.variables[key], text_color=C["text"],
                              fg_color=C["border"], progress_color=C["accent"], button_color="#E4E4ED",
                              button_hover_color="#FFFFFF", font=("Segoe UI", 13), switch_width=36, switch_height=20)
        check.pack(fill="x", anchor="w", pady=10)
        self.inputs.append(check)

    def _server(self, page):
        self._heading(page, "Server settings", "Connect an existing installation or set up a new world.")
        install = self._card(page, "Installation")
        self._field(install, "install_dir", "Install folder", browse="directory")
        self._field(install, "server_exe", "Server executable", "Selecting an executable also sets its installation folder.", browse="exe")
        row = tk.Frame(install, bg=C["panel"])
        row.pack(fill="x", pady=(10, 0))
        self._button(row, "Install / repair", lambda: self._confirm("install"), primary=True).pack(side="left", padx=(0, 8))
        self._button(row, "Configure firewall", lambda: self._action("firewall")).pack(side="left")
        game = self._card(page, "World configuration", "Save changes stores your preferences. Write game config applies them while the server is stopped.")
        for key, title, hint, secret in (
            ("owner_id", "Owner player ID", "In-game Settings → Player ID", False),
            ("server_name", "Server name", None, False), ("world_name", "World name", "Find your world using this exact name.", False),
            ("admin_pass", "Admin password", None, True), ("world_pass", "World password", "Optional; leave blank for a public server.", True),
            ("port", "UDP port", "Default: 7777. Router port forwarding is configured separately.", False)):
            self._field(game, key, title, hint, secret)
        row = tk.Frame(game, bg=C["panel"])
        row.pack(fill="x", pady=(14, 0))
        self._button(row, "Write game config", lambda: self._action("write_config"), primary=True).pack(side="left", padx=(0, 8))
        self._button(row, "Load existing config", self._load_config).pack(side="left")

    def _automation(self, page):
        self._heading(page, "Automation", "Set a schedule for updates and backups. Keep the manager open or minimized to run them.")
        updates = self._card(page, "Automatic updates", "Checks the Steam public build. Downloads and restarts only when a different build is available.")
        self._toggle(updates, "auto_update", "Enable automatic update checks and installation")
        self._field(updates, "update_minutes", "Check every (minutes)", "Minimum 5 minutes. Deferred updates are checked again in 5 minutes.")
        self._toggle(updates, "wait_for_empty", "Wait until the server has been empty for 60 seconds")
        label(updates, "Unreadable player activity defers automatic updates. Counts are inferred from the server log.", color="muted", size=9, wraplength=700, justify="left").pack(anchor="w", pady=(0, 10))
        self._toggle(updates, "maintenance_window", "Only install updates inside a daily maintenance window")
        self._field(updates, "window_start", "Window start", "Local Windows time, 24-hour HH:MM. Windows spanning midnight are supported.")
        self._field(updates, "window_end", "Window end")
        backup = self._card(page, "Automatic backups", "Timestamped ZIP archives with saved worlds, configuration and a SHA-256 file inventory. Maintenance also creates a backup before changes.")
        self._toggle(backup, "auto_backup", "Enable scheduled backups")
        self._field(backup, "backup_hours", "Back up every (hours)")
        self._field(backup, "backup_keep", "Backups to keep", "Keeps the newest archives for this installation. Unrelated files are never pruned.")
        self._field(backup, "backup_dir", "Backup folder", browse="directory")
        label(backup, "Live snapshots capture saved files on disk. A changing file cancels that snapshot and schedules a retry.", color="muted", size=9, wraplength=700, justify="left").pack(anchor="w", pady=(10, 0))
        shutdown = self._card(page, "Restart handling", "Requests a console shutdown for servers launched by this manager and waits for exit. An unsuccessful shutdown cancels maintenance.")
        self._toggle(shutdown, "allow_force_stop", "Allow forced stop after shutdown fails (may lose unsaved progress)")
        self._button(page, "Save automation settings", self._save, primary=True).pack(anchor="w", pady=(0, 20))

    def _backups(self, page):
        self._heading(page, "Backups", "Your saved worlds and configuration, with a clear history of every snapshot.")
        card = self._card(page, "Backup history")
        row = tk.Frame(card, bg=C["panel"])
        row.pack(fill="x", pady=(8, 16))
        self._button(row, "Back up now", lambda: self._action("backup"), primary=True).pack(side="left", padx=(0, 8))
        self._button(row, "Open backup folder", lambda: self._open_path(self.prefs["backup_dir"]), tracked=False).pack(side="left", padx=(0, 8))
        self._button(row, "Refresh", lambda: self._action("refresh_backups")).pack(side="left")
        self.backup_table = ttk.Treeview(card, columns=("created", "reason", "mode", "size"), show="headings", height=9)
        for key, title, width in (("created", "CREATED", 175), ("reason", "REASON", 200), ("mode", "SNAPSHOT", 100), ("size", "SIZE", 90)):
            self.backup_table.heading(key, text=title)
            self.backup_table.column(key, width=width, minwidth=75, anchor="w")
        self.backup_table.pack(fill="both", expand=True)
        self.backup_empty = label(card, "No snapshots yet. Create your first backup above.", color="muted")
        self.backup_empty.pack(anchor="w", pady=(12, 0))
        restore = self._card(page, "Restoring a world")
        label(restore, "1. Stop the server and back up its current files.\n"
              "2. Open the chosen ZIP. Its manifest lists the original source folders.\n"
              "3. Copy the desired SaveGames files back to the corresponding folder.\n"
              "4. Start the server and confirm that the expected world loads.\n\n"
              "Keep the original save filenames. Configuration backups contain server passwords; store archives privately.",
              color="muted", justify="left", wraplength=720).pack(anchor="w")

    def _activity(self, page):
        self._heading(page, "Activity", "Live output from installations, backups and scheduled maintenance.")
        card = self._card(page)
        row = tk.Frame(card, bg=C["panel"])
        row.pack(fill="x", pady=(0, 12))
        self.errors_only = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Errors and warnings only", variable=self.errors_only, command=self._render_log,
                        fg_color=C["accent"], hover_color=C["accent_dark"], text_color=C["text"],
                        border_color=C["border"], checkbox_width=18, checkbox_height=18,
                        font=("Segoe UI", 12)).pack(side="left")
        self._button(row, "Open log folder", lambda: self._open_path(settings.app_dir()), tracked=False).pack(side="right")
        self._button(row, "Copy log", self._copy_log, tracked=False).pack(side="right", padx=8)
        self.log_view = tk.Text(card, height=24, wrap="word", bg=C["input"], fg=C["text"], font=("Consolas", 10), relief="flat", bd=12, state="disabled")
        bar = ctk.CTkScrollbar(card, command=self.log_view.yview, fg_color=C["panel"],
                               button_color=C["border"], button_hover_color="#555966", width=10)
        self.log_view.configure(yscrollcommand=bar.set)
        self.log_view.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        for tag, color in (("ERROR", "error"), ("WARN", "warn"), ("OK", "success"), ("INFO", "muted")):
            self.log_view.tag_configure(tag, foreground=C[color])

    def _show(self, name):
        self.current_page = name
        for key, page in self.pages.items():
            page.pack_forget()
            self.nav[key].configure(fg_color=C["accent_dark"] if key == name else C["sidebar"],
                                    text_color=C["text"] if key == name else C["muted"])
        self.pages[name].pack(fill="both", expand=True)

    def _scroll(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        if isinstance(widget, (tk.Text, ttk.Treeview)):
            return
        canvas = self.pages[self.current_page].canvas
        if canvas.bbox("all") and canvas.bbox("all")[3] > canvas.winfo_height():
            canvas.yview_scroll(int(-event.delta / 120), "units")

    def _mark_dirty(self, *_):
        self.dirty = True
        self.save_status.configure(text="Unsaved settings", fg=C["warn"])
        if not getattr(self, "_busy", False):
            self.save_button.configure(state="normal")

    def _gather(self):
        prefs = dict(self.prefs)
        for key, var in self.variables.items():
            value = var.get()
            if type(settings.DEFAULTS[key]) is int:
                try:
                    value = int(value)
                except ValueError:
                    raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a whole number.")
            elif isinstance(value, str):
                value = value.strip()
            prefs[key] = value
        errors = scheduler.validate(prefs)
        if errors:
            raise ValueError("\n".join(errors))
        return prefs

    def _save(self):
        self._action("save_settings", include_prefs=True)

    def _action(self, action, include_prefs=False):
        try:
            if action == "start" and not self.snapshot.get("online"):
                candidate = self.variables["server_exe"].get()
                paths = [installer.CONFIG_FILE, *installer._dedicated_config_paths_from_exe(candidate)]
                if not any(path.is_file() for path in paths):
                    raise ValueError("Write the game configuration in Server settings before first launch.")
            if self.dirty and not include_prefs:
                raise ValueError("Save your edited settings before running this action.")
            prefs = self._gather() if include_prefs else None
            if not self.service.submit(action, prefs):
                raise ValueError("Another operation is running. Wait for it to finish.")
            self._set_busy(True)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Action needs attention", str(exc), parent=self)

    def _confirm(self, action):
        descriptions = {
            "stop": "Stop the server now? Connected players will be disconnected.",
            "restart": "Back up and restart the server now? Connected players will be disconnected.",
            "apply_update": "Check Steam and apply an available update now? This manual action can disconnect players. A backup is required before maintenance.",
            "install": "Install or validate server files using SteamCMD? The server must be stopped. An existing installation will be backed up first.",
        }
        if messagebox.askyesno("Confirm maintenance", descriptions[action], parent=self):
            self._action(action)

    def _browse(self, key, kind):
        value = (filedialog.askopenfilename(title="Select RSDragonwildsServer.exe", filetypes=[("Server launcher", "RSDragonwildsServer.exe")], parent=self)
                 if kind == "exe" else filedialog.askdirectory(title="Choose folder", parent=self))
        if value:
            self.variables[key].set(value)
            if kind == "exe":
                self.variables["install_dir"].set(str(Path(value).parent))
                paths = [installer.CONFIG_FILE, *installer._dedicated_config_paths_from_exe(value)]
                if any(path.is_file() for path in paths):
                    self._load_config()

    def _load_config(self):
        try:
            paths = [installer.CONFIG_FILE, *installer._dedicated_config_paths_from_exe(self.variables["server_exe"].get())]
            found = next((path for path in paths[1:] if path.is_file()), paths[0])
            if not found.is_file():
                raise ValueError("No DedicatedServer.ini found for this installation.")
            for key, value in installer.read_config(found).items():
                self.variables[key].set(value)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Cannot load configuration", str(exc), parent=self)

    def _open_path(self, path):
        try:
            path = Path(path)
            if not path.exists():
                raise ValueError(f"This path does not exist yet:\n{path}")
            os.startfile(str(path))
        except (ValueError, OSError) as exc:
            messagebox.showinfo("Open folder or file", str(exc), parent=self)

    def _open_server_log(self):
        root = Path(self.prefs.get("server_exe") or self.prefs["install_dir"])
        if root.suffix.casefold() == ".exe":
            root = root.parent
        paths = [root / "RSDragonwilds/Saved/Logs/RSDragonwilds.log", root / "Saved/Logs/RSDragonwilds.log"]
        self._open_path(next((path for path in paths if path.is_file()), paths[0]))

    def _set_busy(self, busy):
        controls = (busy, self.snapshot.get("online", False), self.dirty)
        if controls == getattr(self, "_control_state", None):
            return
        self._control_state = controls
        was_busy = getattr(self, "_busy", False)
        self._busy = busy
        for button in self.action_buttons:
            button.configure(state="disabled" if busy else "normal")
        for widget in self.inputs:
            widget.configure(state="disabled" if busy else "normal")
        if busy and not was_busy:
            self.progress.pack(side="left", padx=(0, 12), before=self.operation)
            self.progress.start(12)
        elif not busy:
            self.progress.stop()
            self.progress.pack_forget()
        if not busy:
            online = self.snapshot.get("online", False)
            self.start_button.configure(state="disabled" if online else "normal")
            self.stop_button.configure(state="normal" if online else "disabled")
            self.restart_button.configure(state="normal" if online else "disabled")
            self.save_button.configure(state="normal" if self.dirty else "disabled")

    def _drain(self):
        changed = False
        for _ in range(500):
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log_lines.append(payload)
                self.log_lines = self.log_lines[-2000:]
                changed = True
            elif kind == "status":
                self._status(payload)
            elif kind == "preferences":
                self.prefs = payload
                for key, var in self.variables.items():
                    var.set(payload[key])
                self.dirty = False
                self.save_status.configure(text="All changes saved", fg=C["muted"])
            elif kind == "failure" and not payload["scheduled"]:
                messagebox.showerror("Operation failed", payload["message"], parent=self)
        if changed:
            self._render_log()
        self.after(150, self._drain)

    def _status(self, data):
        self.snapshot = copy.deepcopy(data)
        self.prefs = data["prefs"]
        state = data["state"]
        self._set_busy(data["busy"])
        seconds = max(0, int(datetime.now().timestamp() - data["started"])) if data["started"] else 0
        uptime = f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}" if data["online"] else "—"
        if self.metrics["uptime"].cget("text") != uptime:
            self.metrics["uptime"].configure(text=uptime)
        # A monitoring tick is not a layout change. Keep scrolling, focus and
        # geometry intact, and redraw only when the underlying state changes.
        if data == getattr(self, "_last_rendered_status", None):
            return
        self._last_rendered_status = copy.deepcopy(data)
        configured = bool(self.prefs.get("server_exe"))
        if configured != getattr(self, "_overview_mode", None):
            self._overview_mode = configured
            if configured:
                self.empty_section.pack_forget()
                self.server_section.pack(fill="x", before=self.overview_bottom)
            else:
                self.server_section.pack_forget()
                self.empty_section.pack(fill="x", before=self.overview_bottom)
        self.overview_summary.configure(text=("1 server connected  ·  " + ("Online" if data["online"] else "Offline"))
                                        if configured else "Connect a server to get started")
        automated = self.prefs["auto_update"] or self.prefs["auto_backup"]
        self.automation_badge.configure(text="●  AUTOMATION ACTIVE" if automated else "●  AUTOMATION OFF",
                                        fg=C["success"] if automated else C["muted"])
        self.server_badge.configure(text="●  SERVER PROCESS ONLINE" if data["online"] else "●  SERVER OFFLINE",
                                    fg=C["success"] if data["online"] else C["muted"])
        self.server_title.configure(text=self.prefs.get("server_name") or "Dragonwilds dedicated server")
        detail = f"World: {self.prefs.get('world_name') or 'Not configured'}   ·   UDP {self.prefs['port']}"
        if data["online"]:
            detail += f"   ·   PID {', '.join(map(str, data['pids']))}"
        self.server_detail.configure(text=data.get("monitor_error") or detail)
        self.metrics["players"].configure(text=str(len(data["players"])) if data["players_known"] else "Unknown" if data["online"] else "—")
        self.metrics["build"].configure(text=state.get("installed_build") or "Unknown")
        self.metrics["latest"].configure(text=state.get("available_build") or "Not checked")
        self.update_status.configure(text=state.get("update_status", "Check Steam to see whether an update is available."))
        self.schedule_text.configure(text=(
            f"Update checks       {timestamp(state.get('next_check_update'), 'Off')}\n"
            f"Next backup          {timestamp(state.get('next_backup'), 'Off')}\n"
            f"Last backup           {timestamp(state.get('last_backup'))}\n"
            f"Last Steam check   {timestamp(state.get('last_check'))}"))
        result = state.get("last_result", "Ready when you are.")
        self.last_result.configure(text=result, fg=C["error"] if state.get("last_result_level") == "ERROR" else C["muted"])
        self.operation.configure(text=state.get("activity", "Idle") if data["busy"] else "Ready" if not state.get("last_result") else result[:110])
        records = data["backups"]
        signature = tuple(record["path"] for record in records)
        if signature != getattr(self, "_backup_signature", None):
            self._backup_signature = signature
            for item in self.backup_table.get_children():
                self.backup_table.delete(item)
            for record in records:
                created = datetime.fromisoformat(record["created"]).astimezone().strftime("%b %d, %I:%M %p")
                self.backup_table.insert("", "end", values=(created, record["reason"], "Live" if record["live"] else "Stopped",
                                                            f"{record['size'] / 1024 / 1024:.2f} MB"))
        self.backup_empty.configure(text=f"{len(records)} archive(s) for this installation." if records else "No snapshots yet. Create your first backup above.")

    def _render_log(self):
        at_bottom = self.log_view.yview()[1] >= 0.98
        position = self.log_view.yview()[0]
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        for stamp, message, level in self.log_lines:
            if self.errors_only.get() and level not in ("ERROR", "WARN"):
                continue
            self.log_view.insert("end", f"{datetime.fromtimestamp(stamp):%H:%M:%S}  {level:<5}  {message}\n", level)
        self.log_view.configure(state="disabled")
        if at_bottom:
            self.log_view.see("end")
        else:
            self.log_view.yview_moveto(position)

    def _copy_log(self):
        self.clipboard_clear()
        self.clipboard_append(self.log_view.get("1.0", "end-1c"))

    def _close(self):
        if self.service.busy:
            messagebox.showinfo("Operation in progress", "Wait for the current operation to finish. You can minimize the manager in the meantime.", parent=self)
            return
        if self.dirty and not messagebox.askyesno("Unsaved settings", "Close without saving your edited settings?", parent=self):
            return
        if (self.prefs["auto_update"] or self.prefs["auto_backup"]) and not messagebox.askyesno(
                "Pause automation?", "Closing pauses scheduled updates and backups. The server keeps running.\n\nClose now? Choose No and minimize to keep automation running.", parent=self):
            return
        if self.service.close():
            self.destroy()
