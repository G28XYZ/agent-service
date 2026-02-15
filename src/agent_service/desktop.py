from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import shutil
import sys
import threading
import tkinter as tk
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, scrolledtext, simpledialog
from typing import Any, Callable

import yaml

from .config import AppConfig, ensure_config_exists, load_config, resolve_config_path
from .openwebui_client import OpenWebUIClient
from .service import AgentRuntime
from .session_store import SessionStore

LOGGER = logging.getLogger(__name__)
IS_DARWIN = sys.platform == "darwin"

THEMES: dict[str, dict[str, str]] = {
    "agent-dark": {
        "bg": "#050506",
        "panel": "#0b0b0c",
        "panel_alt": "#111114",
        "fg": "#ededf0",
        "muted": "#8b8b91",
        "input_bg": "#1f1f23",
        "input_fg": "#ececef",
        "button_bg": "#f1f1f3",
        "button_fg": "#191a1f",
        "button_soft_bg": "#f1f1f3",
        "button_soft_fg": "#191a1f",
        "border": "#242428",
        "accent": "#f1ad1f",
        "system": "#b7b7be",
        "user": "#f0f0f3",
        "assistant": "#dce8ff",
        "user_bubble_bg": "#1a2230",
        "assistant_bubble_bg": "#101115",
    },
}

APP_FONT_FAMILY = "Roboto"


def _ui_font(size: int, weight: str | None = None) -> tuple[str, int] | tuple[str, int, str]:
    if weight:
        return (APP_FONT_FAMILY, size, weight)
    return (APP_FONT_FAMILY, size)


class AsyncRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def submit(self, coro: Any) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def shutdown(self) -> None:
        if self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=2)


class AgentDesktopApp:
    def __init__(
        self,
        root: tk.Tk,
        *,
        launch_root: Path | None = None,
        test_mode: bool = False,
    ):
        self.root = root
        self.runner = AsyncRunner()

        self.launch_root = (launch_root or Path.cwd()).resolve()
        self.test_mode = test_mode
        self.project_root = self._default_project_root()
        self.project_root.mkdir(parents=True, exist_ok=True)
        self.store = SessionStore(self.project_root)
        self.config_path = resolve_config_path(self.launch_root)

        self.config: AppConfig | None = None
        self.runtime: AgentRuntime | None = None
        self.runtime_ready = False

        self.current_model_id: str | None = None
        self.current_chat_id: str | None = None
        self.controls_collapsed = True
        self.show_all_chats = False
        self.auth_required = True
        self.chat_preview_items: list[dict[str, Any]] = []

        self.service_status_var = tk.StringVar(value="disconnected")
        self.auth_status_var = tk.StringVar(value="unknown")
        self.model_status_var = tk.StringVar(value="not selected")
        self.chat_status_var = tk.StringVar(value="not created")

        self.theme_var = tk.StringVar(value="agent-dark")
        self.url_var = tk.StringVar(value="")
        self.username_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.model_var = tk.StringVar(value="")
        self.chat_var = tk.StringVar(value="")
        self.chat_title_var = tk.StringVar(value="Agent session")

        self.model_choices: list[str] = []
        self.chat_choices: dict[str, str] = {}
        self.chat_titles_by_id: dict[str, str] = {}
        self.chat_message_history: dict[str, list[tuple[str, str]]] = {}
        self.chat_history_loading: set[str] = set()
        self._draft_history_key = "__draft__"
        self.flat_buttons: set[tk.Label] = set()
        self.composer_enabled = False
        self.send_enabled = False
        self.pending_change_id: str | None = None
        self.pending_changes: list[dict[str, Any]] = []
        self.pending_expanded_items: set[str] = set()

        screen_h = self.root.winfo_screenheight()
        self.root.title("Agent")
        self.root.geometry(f"430x{screen_h}+0+0")
        self.root.minsize(360, 500)
        self.root.resizable(True, True)
        self.root.option_add("*Font", f"{APP_FONT_FAMILY} 10")

        self._build_ui()
        self._bind_events()
        self._apply_theme()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._bootstrap_runtime()

    def _default_project_root(self) -> Path:
        if self.test_mode:
            return (self.launch_root / "test").resolve()
        return self.launch_root.parent.resolve()

    def _resolve_project_root_from_config(self, config: AppConfig) -> Path:
        if self.test_mode:
            return (self.launch_root / "test").resolve()

        configured = (config.agent.project_path or "").strip()
        if configured:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                candidate = (self.launch_root / candidate).resolve()
            return candidate.resolve()

        return self.launch_root.parent.resolve()

    def _build_ui(self) -> None:
        self.main = tk.Frame(self.root)
        self.main.pack(fill=tk.BOTH, expand=True)

        self.header = tk.Frame(self.main)
        self.header.pack(fill=tk.X, padx=14, pady=(14, 8))

        self.header_top = tk.Frame(self.header)
        self.header_top.pack(fill=tk.X)

        self.title_label = tk.Label(self.header_top, text="AGENT", font=_ui_font(15, "bold"))
        self.title_label.pack(side=tk.LEFT, anchor="w")
        self.header_model_wrap = tk.Frame(self.header_top)
        self.header_model_wrap.pack(side=tk.RIGHT, anchor="e")
        self.header_model_label = tk.Label(self.header_model_wrap, text="Model", font=_ui_font(9))
        self.header_model_label.pack(side=tk.LEFT, padx=(0, 6))
        self.model_menu = tk.OptionMenu(self.header_model_wrap, self.model_var, "")
        self.model_menu.configure(width=26)
        self.model_menu.pack(side=tk.LEFT)
        self.subtitle_label = tk.Label(
            self.header,
            text="local OpenWebUI agent",
            font=_ui_font(9),
        )
        self.subtitle_label.pack(anchor="w", pady=(2, 0))

        self.status_panel = tk.Frame(self.main, bd=0, relief=tk.FLAT, highlightthickness=1)
        self.status_panel.pack(fill=tk.X, padx=14, pady=(0, 10))

        status_head = tk.Frame(self.status_panel)
        status_head.pack(fill=tk.X, padx=12, pady=(10, 8))
        self.chat_back_btn = self._create_flat_button(
            status_head,
            "←",
            self._leave_chat,
            width=3,
        )
        self.tasks_title_label = tk.Label(status_head, text="Задачи", font=_ui_font(15, "bold"))
        self.tasks_title_label.pack(side=tk.LEFT)
        self.tasks_title_label.bind("<Button-1>", self._rename_current_chat_title_clicked)
        self.chat_delete_btn = self._create_flat_button(
            status_head,
            "Удалить",
            self._delete_chat_clicked,
        )

        self.status_actions = tk.Frame(status_head)
        self.status_actions.pack(side=tk.RIGHT)

        self.refresh_chats_btn = self._create_flat_button(
            self.status_actions,
            "↻",
            self._refresh_chats,
            width=3,
        )
        self.refresh_chats_btn.pack(side=tk.LEFT)

        self.controls_toggle_btn = self._create_flat_button(
            self.status_actions,
            "⋯",
            self._open_settings_menu,
            width=3,
        )
        self.controls_toggle_btn.pack(side=tk.LEFT, padx=(4, 0))

        self.create_chat_btn = self._create_flat_button(
            self.status_actions,
            "✎",
            self._create_chat,
            width=3,
        )
        self.create_chat_btn.pack(side=tk.LEFT, padx=(4, 0))

        self.settings_menu = tk.Menu(self.root, tearoff=0)
        self.task_rows: list[tuple[tk.Label, tk.Label]] = []
        self.task_row_frames: list[tk.Frame] = []
        self.task_row_chat_ids: list[str | None] = [None, None, None]
        for _idx in range(3):
            row = tk.Frame(self.status_panel)
            row.pack(fill=tk.X, padx=12, pady=(0, 4))
            self.task_row_frames.append(row)
            row_title = tk.Label(
                row,
                anchor="w",
                relief=tk.FLAT,
                bd=0,
                highlightthickness=0,
                font=_ui_font(11, "bold"),
            )
            row_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
            row_title.bind("<Button-1>", lambda _event, idx=_idx: self._open_chat_by_row(idx))
            self.flat_buttons.add(row_title)
            row_meta = tk.Label(row, anchor="e", width=6, font=_ui_font(11))
            row_meta.pack(side=tk.RIGHT)
            self.task_rows.append((row_title, row_meta))

        self.view_all_btn = self._create_flat_button(
            self.status_panel,
            "Просмотреть все (0)",
            self._toggle_view_all_chats,
            anchor="w",
        )
        self.view_all_btn.pack(fill=tk.X, padx=12, pady=(2, 10))

        self.connection_panel = tk.Frame(self.main, bd=0, relief=tk.FLAT, highlightthickness=1)
        self.connection_panel.pack(fill=tk.X, padx=14, pady=(0, 8))

        tk.Label(self.connection_panel, text="OpenWebUI URL", anchor="w").pack(
            fill=tk.X, padx=10, pady=(8, 3)
        )
        self.url_entry = tk.Entry(self.connection_panel, textvariable=self.url_var)
        self.url_entry.pack(fill=tk.X, padx=10)

        conn_actions = tk.Frame(self.connection_panel)
        conn_actions.pack(fill=tk.X, padx=10, pady=(6, 8))
        self.connect_btn = tk.Button(conn_actions, text="Connect", command=self._connect_clicked)
        self.connect_btn.pack(side=tk.LEFT)

        self.theme_menu = tk.OptionMenu(conn_actions, self.theme_var, "agent-dark")
        self.theme_menu.pack(side=tk.LEFT)
        self.theme_menu.pack_forget()

        self.service_label = tk.Label(self.connection_panel, anchor="w", font=_ui_font(10))
        self.auth_label = tk.Label(self.connection_panel, anchor="w", font=_ui_font(10))
        self.service_label.pack(fill=tk.X, padx=10, pady=(0, 2))
        self.auth_label.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.auth_panel = tk.Frame(self.main, bd=0, relief=tk.FLAT, highlightthickness=1)
        self.auth_panel.pack(fill=tk.X, padx=14, pady=(0, 8))

        tk.Label(self.auth_panel, text="Authorization required", font=_ui_font(10, "bold")).pack(
            anchor="w", padx=10, pady=(8, 4)
        )
        tk.Label(self.auth_panel, text="Username", anchor="w").pack(fill=tk.X, padx=10)
        self.username_entry = tk.Entry(self.auth_panel, textvariable=self.username_var)
        self.username_entry.pack(fill=tk.X, padx=10, pady=(0, 4))

        tk.Label(self.auth_panel, text="Password", anchor="w").pack(fill=tk.X, padx=10)
        self.password_entry = tk.Entry(self.auth_panel, textvariable=self.password_var, show="*")
        self.password_entry.pack(fill=tk.X, padx=10, pady=(0, 6))

        self.login_btn = tk.Button(self.auth_panel, text="Login", command=self._login_clicked)
        self.login_btn.pack(anchor="w", padx=10, pady=(0, 8))

        self.model_panel = tk.Frame(self.main, bd=0, relief=tk.FLAT, highlightthickness=1)
        self.model_panel.pack(fill=tk.X, padx=14, pady=(0, 8))

        self.model_label = tk.Label(self.model_panel, anchor="w", font=_ui_font(10))
        self.chat_label = tk.Label(self.model_panel, anchor="w", font=_ui_font(10))
        self.model_label.pack(fill=tk.X, padx=10, pady=(8, 2))
        self.chat_label.pack(fill=tk.X, padx=10, pady=(0, 6))

        tk.Label(self.model_panel, text="Existing chat", anchor="w").pack(fill=tk.X, padx=10, pady=(6, 3))
        self.chat_menu = tk.OptionMenu(self.model_panel, self.chat_var, "")
        self.chat_menu.pack(fill=tk.X, padx=10)

        model_actions = tk.Frame(self.model_panel)
        model_actions.pack(fill=tk.X, padx=10, pady=(6, 4))
        self.refresh_models_btn = tk.Button(
            model_actions,
            text="Refresh models",
            command=self._refresh_models,
        )
        self.refresh_models_btn.pack(side=tk.LEFT)

        tk.Button(model_actions, text="Refresh chats", command=self._refresh_chats).pack(side=tk.LEFT, padx=(6, 0))

        tk.Label(self.model_panel, text="Chat title", anchor="w").pack(fill=tk.X, padx=10)
        self.chat_title_entry = tk.Entry(self.model_panel, textvariable=self.chat_title_var)
        self.chat_title_entry.pack(fill=tk.X, padx=10, pady=(0, 6))

        tk.Button(self.model_panel, text="Create chat", command=self._create_chat).pack(
            anchor="w",
            padx=10,
            pady=(0, 8),
        )

        self.result_panel = tk.Frame(self.main, bd=0, relief=tk.FLAT)
        self.result_panel.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

        self.result_text = scrolledtext.ScrolledText(
            self.result_panel,
            wrap=tk.WORD,
            height=20,
            relief=tk.FLAT,
            padx=10,
            pady=10,
            font=_ui_font(10),
        )
        self.result_text.pack(fill=tk.BOTH, expand=True)
        self.result_text.configure(insertwidth=0, exportselection=False, state=tk.NORMAL, cursor="xterm")
        self.result_context_menu = tk.Menu(self.root, tearoff=0)
        self.result_context_menu.add_command(label="Копировать", command=self._copy_result_selection)
        self.result_context_menu.add_command(label="Выделить все", command=self._select_all_result_text)

        self.empty_state_label = tk.Label(
            self.result_panel,
            text="<_>",
            font=_ui_font(26, "bold"),
        )
        self.empty_state_label.place(relx=0.5, rely=0.5, anchor="center")

        self.pending_panel = tk.Frame(self.main, bd=0, relief=tk.FLAT, highlightthickness=1)
        pending_head = tk.Frame(self.pending_panel)
        pending_head.pack(fill=tk.X, padx=10, pady=(8, 6))
        self.pending_title_label = tk.Label(
            pending_head,
            text="Изменения",
            anchor="w",
            font=_ui_font(10, "bold"),
        )
        self.pending_title_label.pack(side=tk.LEFT)
        self.pending_stats_label = tk.Label(
            pending_head,
            text="+0 -0",
            anchor="w",
            font=_ui_font(10, "bold"),
        )
        self.pending_stats_label.pack(side=tk.LEFT, padx=(10, 0))
        pending_actions = tk.Frame(pending_head)
        pending_actions.pack(side=tk.RIGHT)
        self.reject_changes_btn = self._create_flat_button(
            pending_actions,
            "Отменить",
            self._discard_pending_changes_clicked,
        )
        self.reject_changes_btn.pack(side=tk.RIGHT)

        self.pending_body = tk.Frame(self.pending_panel)
        self.pending_body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
        self.pending_canvas = tk.Canvas(
            self.pending_body,
            highlightthickness=0,
            bd=0,
            relief=tk.FLAT,
        )
        self.pending_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.pending_scrollbar = tk.Scrollbar(
            self.pending_body,
            orient=tk.VERTICAL,
            command=self.pending_canvas.yview,
        )
        self.pending_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.pending_canvas.configure(yscrollcommand=self.pending_scrollbar.set)
        self.pending_rows = tk.Frame(self.pending_canvas)
        self.pending_canvas_window = self.pending_canvas.create_window(
            (0, 0),
            window=self.pending_rows,
            anchor="nw",
        )
        self.pending_rows.bind(
            "<Configure>",
            lambda _event: self.pending_canvas.configure(scrollregion=self.pending_canvas.bbox("all")),
        )
        self.pending_canvas.bind("<Configure>", self._on_pending_canvas_configure)

        self.composer_panel = tk.Frame(self.main, bd=0, relief=tk.FLAT, highlightthickness=1)
        self.composer_panel.pack(fill=tk.X, padx=14, pady=(0, 12))

        self.composer_hint = tk.Label(
            self.composer_panel,
            text="Задайте Agent любой вопрос по проекту...",
            anchor="w",
            font=_ui_font(10, "bold"),
        )
        self.composer_hint.pack(fill=tk.X, padx=12, pady=(10, 4))
        self.message_input = tk.Text(self.composer_panel, height=6, wrap=tk.WORD, relief=tk.FLAT)
        self.message_input.pack(fill=tk.X, padx=12)

        compose_actions = tk.Frame(self.composer_panel)
        compose_actions.pack(fill=tk.X, padx=12, pady=(8, 10))
        self.send_btn = self._create_flat_button(
            compose_actions,
            "↑",
            self._send_message,
            width=3,
        )
        self.send_btn.pack(side=tk.RIGHT)

        self._set_auth_required(True)
        self._set_composer_enabled(False)
        self._refresh_status_labels()
        self._apply_controls_visibility()

    def _bind_events(self) -> None:
        self.theme_var.trace_add("write", self._on_theme_changed)
        self.model_var.trace_add("write", self._on_model_changed)
        self.chat_var.trace_add("write", self._on_chat_selected)
        self.message_input.bind("<Return>", self._on_enter_key)
        self.message_input.bind("<Control-v>", self._paste_into_message_input)
        self.message_input.bind("<Control-V>", self._paste_into_message_input)
        self.message_input.bind("<Command-v>", self._paste_into_message_input)
        self.message_input.bind("<Command-V>", self._paste_into_message_input)
        self.message_input.bind("<Shift-Insert>", self._paste_into_message_input)
        self.message_input.bind("<<Paste>>", self._paste_into_message_input)
        self.message_input.bind("<Control-KeyPress>", self._on_message_input_shortcuts, add="+")
        self.message_input.bind("<Command-KeyPress>", self._on_message_input_shortcuts, add="+")
        self.result_text.bind("<Configure>", self._on_result_text_configure)
        self.result_text.bind("<Button-1>", self._focus_result_text, add="+")
        self.result_text.bind("<Button-3>", self._open_result_context_menu)
        self.result_text.bind("<Button-2>", self._open_result_context_menu)
        self.result_text.bind("<Control-KeyPress>", self._on_result_text_shortcuts, add="+")
        self.result_text.bind("<Command-KeyPress>", self._on_result_text_shortcuts, add="+")

    def _bootstrap_runtime(self) -> None:
        created = ensure_config_exists(self.config_path)
        if created:
            self._append_system(f"Config created at {self.config_path}")

        try:
            self.config = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.service_status_var.set("config error")
            self._refresh_status_labels()
            self._append_system(f"Config load failed: {exc}")
            return

        resolved_project_root = self._resolve_project_root_from_config(self.config)
        resolved_project_root.mkdir(parents=True, exist_ok=True)
        self._hydrate_project_state_from_launch_root(resolved_project_root)
        if resolved_project_root != self.project_root:
            self.project_root = resolved_project_root
            self.store = SessionStore(self.project_root)
        if self.test_mode:
            self._append_system(f"Workspace root (test mode): {self.project_root}")
        else:
            self._append_system(f"Workspace root: {self.project_root}")

        self.url_var.set(self.config.openwebui.base_url)
        if self.config.openwebui.credentials.username:
            self.username_var.set(self.config.openwebui.credentials.username)
        default_model = self.config.agent.default_model.strip()
        if default_model:
            self.current_model_id = default_model
            self.model_var.set(default_model)
            self.model_status_var.set(default_model)

        self._connect_to_url(self.config.openwebui.base_url, persist=False)

    def _hydrate_project_state_from_launch_root(self, target_project_root: Path) -> None:
        source_storage = self.launch_root / ".agent-service"
        target_storage = target_project_root / ".agent-service"
        if source_storage.resolve() == target_storage.resolve():
            return
        if not source_storage.exists():
            return

        target_storage.mkdir(parents=True, exist_ok=True)
        source_auth_path = source_storage / "auth.json"
        source_cookies_path = source_storage / "cookies.json"
        target_auth_path = target_storage / "auth.json"
        target_cookies_path = target_storage / "cookies.json"

        source_auth = self._read_json_file(source_auth_path)
        target_auth = self._read_json_file(target_auth_path)
        source_has_session = self._auth_has_session(source_auth)
        target_has_session = self._auth_has_session(target_auth)

        copied_auth = False
        if source_has_session and not target_has_session and source_auth_path.exists():
            shutil.copy2(source_auth_path, target_auth_path)
            copied_auth = True
            try:
                target_auth_path.chmod(0o600)
            except PermissionError:
                pass

        should_copy_cookies = copied_auth or not target_cookies_path.exists()
        if should_copy_cookies and source_cookies_path.exists():
            shutil.copy2(source_cookies_path, target_cookies_path)
            try:
                target_cookies_path.chmod(0o600)
            except PermissionError:
                pass

        if copied_auth:
            self._append_system(
                "Session migrated from launch .agent-service to workspace .agent-service."
            )

    @staticmethod
    def _read_json_file(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _auth_has_session(payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        token = str(payload.get("token") or "").strip()
        authenticated = bool(payload.get("authenticated"))
        return bool(token or authenticated)

    def _connect_clicked(self) -> None:
        url = self.url_var.get().strip()
        if not url:
            self._append_system("OpenWebUI URL is required")
            return
        self._connect_to_url(url, persist=True)

    def _connect_to_url(self, raw_url: str, *, persist: bool) -> None:
        url = raw_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            self._append_system("URL must start with http:// or https://")
            return

        self.service_status_var.set("connecting")
        self.auth_status_var.set("checking")
        self._refresh_status_labels()
        self._set_composer_enabled(False)

        if self.runtime_ready and self.runtime is not None:
            old_runtime = self.runtime

            def after_shutdown(_result: Any) -> None:
                self.runtime = None
                self.runtime_ready = False
                self._start_runtime(url, persist=persist)

            self._submit(old_runtime.shutdown(), on_success=after_shutdown, action="runtime.shutdown")
            return

        self._start_runtime(url, persist=persist)

    def _start_runtime(self, url: str, *, persist: bool) -> None:
        if self.config is None:
            self._append_system("Config is not loaded")
            return

        try:
            openwebui_cfg = self.config.openwebui.model_copy(update={"base_url": url})
            self.config = self.config.model_copy(update={"openwebui": openwebui_cfg})
            if persist:
                self._save_config(self.config, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.service_status_var.set("config error")
            self._refresh_status_labels()
            self._append_system(f"Failed to apply URL: {exc}")
            return

        self.url_var.set(self.config.openwebui.base_url)

        client = OpenWebUIClient(self.config, self.store)
        self.runtime = AgentRuntime(self.config, self.store, client)

        self._submit(
            self.runtime.startup(),
            on_success=lambda _result: self._after_runtime_started(),
            action="runtime.startup",
        )

    @staticmethod
    def _save_config(config: AppConfig, path: Path | None = None) -> None:
        if path is None:
            return
        payload = config.model_dump(mode="python")
        content = yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _after_runtime_started(self) -> None:
        self.runtime_ready = True
        self.service_status_var.set("connected")
        self._refresh_status_labels()
        self._append_system(f"Connected to {self.url_var.get().strip()}")
        self._check_auth_status()

    def _check_auth_status(self) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return
        self._submit(runtime.auth_status(), on_success=self._on_auth_status, action="auth.status")

    def _on_auth_status(self, result: dict[str, Any]) -> None:
        authenticated = bool(result.get("authenticated"))
        if authenticated:
            self.auth_status_var.set("authorized")
            self._set_auth_required(False)
            self._set_composer_enabled(True)
            self._append_system("Session is authorized")
            self._refresh_models()
            self._refresh_chats()
        else:
            self.auth_status_var.set("authorization required")
            self._set_auth_required(True)
            self._set_composer_enabled(False)
            self._clear_models()
            self._clear_chats()
            self._append_system("Authorization is required")

        self._refresh_status_labels()

    def _login_clicked(self) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return

        username = self.username_var.get().strip() or None
        password = self.password_var.get().strip() or None

        self._submit(
            runtime.login(username=username, password=password),
            on_success=self._on_login_success,
            action="auth.login",
        )

    def _on_login_success(self, result: dict[str, Any]) -> None:
        self.password_var.set("")
        self.auth_status_var.set("authorized")
        self._set_auth_required(False)
        self._set_composer_enabled(True)
        self._refresh_status_labels()
        self._append_system(f"Authenticated as {result.get('username', 'unknown')}")
        self._refresh_models()
        self._refresh_chats()

    def _refresh_models(self) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return

        self._submit(runtime.list_models(), on_success=self._on_models_loaded, action="models.list")

    def _refresh_chats(self) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return

        self._submit(runtime.list_chats(), on_success=self._on_chats_loaded, action="chats.list")

    def _toggle_view_all_chats(self) -> None:
        if self.current_chat_id:
            return

        total = len(self.chat_preview_items)
        if total <= 3:
            self._refresh_chats()
            return

        self.show_all_chats = not self.show_all_chats
        self._refresh_status_labels()

    def _on_models_loaded(self, models: list[dict[str, Any]]) -> None:
        model_ids = [str(item.get("id")) for item in models if item.get("id")]
        self.model_choices = model_ids
        self._update_model_menu(model_ids)

        if not model_ids:
            self.current_model_id = None
            self.model_var.set("")
            self.model_status_var.set("no models")
            self._refresh_status_labels()
            self._append_system("No models available")
            return

        if self.current_model_id in model_ids:
            selected = self.current_model_id
        elif self.config is not None and self.config.agent.default_model in model_ids:
            selected = self.config.agent.default_model
        else:
            selected = model_ids[0]

        self.model_var.set(selected)
        self.current_model_id = selected
        self.model_status_var.set(selected)
        self._refresh_status_labels()
        self._append_system(f"Models loaded: {len(model_ids)}")

    def _on_chats_loaded(self, chats: list[dict[str, Any]]) -> None:
        self.chat_preview_items = [item for item in chats if isinstance(item, dict)]
        if len(self.chat_preview_items) <= 3:
            self.show_all_chats = False
        self.chat_titles_by_id = {}
        options: list[tuple[str, str]] = []
        for chat in chats:
            if not isinstance(chat, dict):
                continue

            chat_id = chat.get("chat_id")
            if not chat_id:
                continue

            title = (chat.get("title") or "").strip()
            model_id = chat.get("model_id")
            short_id = str(chat_id)[:8]

            resolved_title = title or f"Chat {short_id}"
            self.chat_titles_by_id[str(chat_id)] = resolved_title

            if title and model_id:
                label = f"{title} [{short_id}] ({model_id})"
            elif title:
                label = f"{title} [{short_id}]"
            elif model_id:
                label = f"{short_id} ({model_id})"
            else:
                label = f"{short_id}"

            options.append((label, str(chat_id)))

        self._update_chat_menu(options)
        if not options:
            self.current_chat_id = None
            self.chat_status_var.set("no chats")
            self._refresh_status_labels()
            return

        selected_label: str | None = None
        if self.current_chat_id:
            for label, chat_id in options:
                if chat_id == self.current_chat_id:
                    selected_label = label
                    break

        if selected_label is not None:
            self.chat_var.set(selected_label)
        elif self.chat_var.get().strip() not in self.chat_choices:
            self.chat_var.set("")
        self._append_system(f"Chats loaded: {len(options)}")
        self._refresh_status_labels()

    def _set_active_chat(self, chat_id: str) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return
        try:
            runtime.set_active_chat(chat_id)
        except Exception as exc:  # noqa: BLE001
            self._append_system(f"Failed to save active chat: {exc}")

    def _open_chat_by_row(self, row_idx: int) -> None:
        if row_idx < 0 or row_idx >= len(self.task_row_chat_ids):
            return
        chat_id = self.task_row_chat_ids[row_idx]
        if not chat_id:
            return
        self._open_chat(chat_id)

    def _open_chat(self, chat_id: str) -> None:
        clean_chat_id = str(chat_id).strip()
        if not clean_chat_id:
            return
        if clean_chat_id == self.current_chat_id and self.chat_message_history.get(clean_chat_id):
            return

        self._clear_pending_changes(discard_remote=True)
        self.current_chat_id = clean_chat_id
        self.chat_status_var.set(clean_chat_id)
        self._set_active_chat(clean_chat_id)
        self.controls_collapsed = True
        self._apply_controls_visibility()

        matching_label = next(
            (label for label, value in self.chat_choices.items() if value == clean_chat_id),
            None,
        )
        if matching_label:
            self.chat_var.set(matching_label)
        self._render_chat_history(clean_chat_id)
        self._load_chat_history(clean_chat_id)
        self._refresh_status_labels()

    def _leave_chat(self) -> None:
        if not self.current_chat_id:
            return

        self._clear_pending_changes(discard_remote=True)
        self.current_chat_id = None
        self.chat_status_var.set("not created")
        self.chat_var.set("")
        self._render_chat_history(None)
        self._refresh_status_labels()

    def _create_chat(self) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return

        model_id = self.model_var.get().strip() or None
        title = self.chat_title_var.get().strip() or None

        self._submit(
            runtime.create_chat(model_id=model_id, title=title),
            on_success=self._on_chat_created,
            action="chat.create",
        )

    def _on_chat_created(self, result: dict[str, Any]) -> None:
        chat_id = result.get("chat_id")
        model_id = result.get("model_id")

        if model_id:
            self.current_model_id = str(model_id)
            self.model_var.set(self.current_model_id)
            self.model_status_var.set(self.current_model_id)

        if chat_id:
            self.current_chat_id = str(chat_id)
            self.chat_status_var.set(self.current_chat_id)
            self._set_active_chat(self.current_chat_id)
            if self.current_chat_id not in self.chat_choices.values():
                self._refresh_chats()
            self._append_system(f"Chat created: {self.current_chat_id}")
            self._set_active_chat(self.current_chat_id)
        else:
            self._append_system("Chat created but chat_id is missing")

        self._refresh_status_labels()
        self._refresh_chats()

    def _send_message(self) -> None:
        if not self.send_enabled:
            return

        runtime = self._require_runtime()
        if runtime is None:
            return

        message = self.message_input.get("1.0", tk.END).strip()
        if not message:
            return

        model_id = self.model_var.get().strip() or self.current_model_id
        if not model_id:
            self._append_system("Model is not selected")
            return

        # Start each request with a clean "last changes" block.
        self._clear_pending_changes(discard_remote=True)
        self.message_input.delete("1.0", tk.END)
        self._append_history_entry("user", message, self.current_chat_id)
        self._render_chat_history(self.current_chat_id)

        self._submit(
            runtime.run_agent_task(
                message=message,
                model_id=model_id,
                chat_id=self.current_chat_id,
                auto_apply=False,
            ),
            on_success=self._on_message_response,
            action="agent.task",
        )

    def _on_message_response(self, result: dict[str, Any]) -> None:
        model_id = result.get("model_id")
        chat_id = result.get("chat_id")
        chat_title = str(result.get("chat_title") or "").strip()
        assistant_text_raw = (result.get("assistant_message") or "").strip() or "[empty response]"
        assistant_text = self._strip_pending_summary(assistant_text_raw) or assistant_text_raw
        pending_id_raw = result.get("pending_id")
        pending_id = str(pending_id_raw).strip() if pending_id_raw else None
        pending_changes_raw = result.get("pending_changes")
        pending_changes = (
            [item for item in pending_changes_raw if isinstance(item, dict)]
            if isinstance(pending_changes_raw, list)
            else []
        )

        if model_id:
            self.current_model_id = str(model_id)
            self.model_var.set(self.current_model_id)
            self.model_status_var.set(self.current_model_id)

        if chat_id:
            resolved_chat_id = str(chat_id)
            if chat_title:
                self.chat_titles_by_id[resolved_chat_id] = chat_title
            if not any(
                str((chat or {}).get("chat_id") or "") == resolved_chat_id for chat in self.chat_preview_items
            ):
                self.chat_preview_items.insert(
                    0,
                    {
                        "chat_id": resolved_chat_id,
                        "title": chat_title or f"Chat {resolved_chat_id[:8]}",
                        "model_id": self.current_model_id,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            self._migrate_draft_history(resolved_chat_id)
            self.current_chat_id = resolved_chat_id
            self.chat_status_var.set(self.current_chat_id)
            self._set_active_chat(self.current_chat_id)
            matching_label = next(
                (label for label, value in self.chat_choices.items() if value == self.current_chat_id),
                None,
            )
            if matching_label:
                self.chat_var.set(matching_label)
            self.controls_collapsed = True
            self._apply_controls_visibility()
        else:
            resolved_chat_id = self.current_chat_id

        self._append_history_entry("assistant", assistant_text, resolved_chat_id)
        self._render_chat_history(resolved_chat_id)
        if pending_id and pending_changes:
            self._queue_auto_apply_pending_changes(pending_id, pending_changes)
        else:
            self._set_pending_changes(None, [])
        if chat_title and resolved_chat_id:
            for chat in self.chat_preview_items:
                if str((chat or {}).get("chat_id") or "") == resolved_chat_id:
                    chat["title"] = chat_title
        self._refresh_status_labels()

    @staticmethod
    def _strip_pending_summary(text: str) -> str:
        marker = "Подготовлены изменения:"
        clean_text = text or ""
        idx = clean_text.find(f"\n\n{marker}")
        if idx >= 0:
            return clean_text[:idx].rstrip()
        idx = clean_text.find(marker)
        if idx >= 0:
            return clean_text[:idx].rstrip()
        return clean_text

    def _queue_auto_apply_pending_changes(
        self,
        pending_id: str,
        pending_changes: list[dict[str, Any]],
    ) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return
        clean_pending_id = str(pending_id or "").strip()
        clean_changes = [item for item in pending_changes if isinstance(item, dict)]
        if not clean_pending_id or not clean_changes:
            return
        self._set_flat_button_disabled(self.reject_changes_btn, True)
        self._submit(
            runtime.apply_pending_changes(clean_pending_id),
            on_success=lambda data, items=list(clean_changes): self._on_pending_changes_auto_applied(data, items),
            on_error=self._on_pending_changes_action_error,
            action="pending.apply",
        )

    def _on_pending_changes_auto_applied(
        self,
        result: dict[str, Any],
        pending_changes: list[dict[str, Any]],
    ) -> None:
        applied_change_id_raw = result.get("applied_change_id")
        applied_change_id = str(applied_change_id_raw).strip() if applied_change_id_raw else None
        if applied_change_id:
            self._set_pending_changes(applied_change_id, pending_changes)
        else:
            self._set_pending_changes(None, [])

        errors_raw = result.get("errors")
        errors = [str(item) for item in errors_raw] if isinstance(errors_raw, list) else []
        if errors:
            summary_lines = ["Автоприменение изменений выполнено с ошибками:"]
            summary_lines.extend(f"- {item}" for item in errors)
            self._append_history_entry("assistant", "\n".join(summary_lines), self.current_chat_id)
            self._render_chat_history(self.current_chat_id)

    def _submit(
        self,
        coro: Any,
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[Exception], None] | None = None,
        action: str,
    ) -> None:
        future = self.runner.submit(coro)

        def done_callback(done_future: Future[Any]) -> None:
            try:
                result = done_future.result()
            except Exception as exc:  # noqa: BLE001
                if on_error is not None:
                    self.root.after(0, lambda err=exc: on_error(err))
                self.root.after(0, lambda err=exc: self._handle_error(action, err))
                return

            if on_success is not None:
                self.root.after(0, lambda: on_success(result))

        future.add_done_callback(done_callback)

    def _handle_error(self, action: str, error: Exception) -> None:
        LOGGER.exception("Action failed: %s", action)
        text = str(error)

        if self._is_auth_error(text):
            self.auth_status_var.set("authorization required")
            self._set_auth_required(True)
            self._set_composer_enabled(False)
            self._clear_models()
            self._refresh_status_labels()

        if action in {"runtime.startup", "runtime.shutdown"}:
            self.runtime_ready = False

        self._append_system(f"{action} failed: {text}")

    @staticmethod
    def _is_auth_error(message: str) -> bool:
        lowered = message.lower()
        markers = [
            "credentials are missing",
            "rejected session",
            "authorization",
            "unauthorized",
            "login rejected",
            "status: 401",
            "status: 403",
        ]
        return any(marker in lowered for marker in markers)

    def _require_runtime(self) -> AgentRuntime | None:
        if not self.runtime_ready or self.runtime is None:
            self._append_system("Runtime is not ready")
            return None
        return self.runtime

    def _set_auth_required(self, required: bool) -> None:
        self.auth_required = required
        self._apply_controls_visibility()

    def _toggle_controls(self) -> None:
        self.controls_collapsed = not self.controls_collapsed
        self._apply_controls_visibility()

    def _open_settings_menu(self) -> None:
        self._rebuild_settings_menu()
        pos_x = self.controls_toggle_btn.winfo_rootx()
        pos_y = self.controls_toggle_btn.winfo_rooty() + self.controls_toggle_btn.winfo_height()
        try:
            self.settings_menu.tk_popup(pos_x, pos_y)
        finally:
            self.settings_menu.grab_release()

    def _rebuild_settings_menu(self) -> None:
        self.settings_menu.delete(0, tk.END)
        self.settings_menu.add_command(
            label="Показать настройки" if self.controls_collapsed else "Скрыть настройки",
            command=self._toggle_controls,
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="OpenWebUI URL...", command=self._prompt_url)
        self.settings_menu.add_command(label="Username...", command=self._prompt_username)
        self.settings_menu.add_command(label="Password...", command=self._prompt_password)
        self.settings_menu.add_command(label="Название нового чата...", command=self._prompt_chat_title)
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="Подключиться", command=self._connect_clicked)
        self.settings_menu.add_command(label="Войти", command=self._login_clicked)
        self.settings_menu.add_command(label="Обновить модели", command=self._refresh_models)
        self.settings_menu.add_command(label="Обновить чаты", command=self._refresh_chats)
        self.settings_menu.add_command(label="Новый чат", command=self._create_chat)
        self.settings_menu.add_command(
            label="Удалить чат...",
            command=self._delete_chat_clicked,
            state=tk.NORMAL if (self.current_chat_id or self.chat_choices) else tk.DISABLED,
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="Выбрать модель...", command=self._prompt_model)
        self.settings_menu.add_command(
            label="Выйти из чата",
            command=self._leave_chat,
            state=tk.NORMAL if self.current_chat_id else tk.DISABLED,
        )

    def _prompt_url(self) -> None:
        value = simpledialog.askstring(
            "OpenWebUI URL",
            "Введите URL OpenWebUI",
            initialvalue=self.url_var.get().strip(),
            parent=self.root,
        )
        if value is None:
            return
        cleaned = value.strip()
        if not cleaned:
            return
        self.url_var.set(cleaned)

    def _prompt_username(self) -> None:
        value = simpledialog.askstring(
            "Username",
            "Введите username",
            initialvalue=self.username_var.get().strip(),
            parent=self.root,
        )
        if value is None:
            return
        self.username_var.set(value.strip())

    def _prompt_password(self) -> None:
        value = simpledialog.askstring(
            "Password",
            "Введите password",
            show="*",
            parent=self.root,
        )
        if value is None:
            return
        self.password_var.set(value.strip())

    def _prompt_chat_title(self) -> None:
        value = simpledialog.askstring(
            "Chat title",
            "Введите название нового чата",
            initialvalue=self.chat_title_var.get().strip(),
            parent=self.root,
        )
        if value is None:
            return
        cleaned = value.strip()
        if not cleaned:
            return
        self.chat_title_var.set(cleaned)

    def _prompt_model(self) -> None:
        if not self.model_choices:
            self._append_system("No models loaded. Refresh models first.")
            return

        hint = ", ".join(self.model_choices[:6])
        value = simpledialog.askstring(
            "Model",
            f"Введите model_id ({hint}{'...' if len(self.model_choices) > 6 else ''})",
            initialvalue=self.model_var.get().strip() or self.current_model_id or "",
            parent=self.root,
        )
        if value is None:
            return
        cleaned = value.strip()
        if not cleaned:
            return
        if cleaned not in self.model_choices:
            self._append_system(f"Unknown model: {cleaned}")
            return
        self.model_var.set(cleaned)

    def _rename_current_chat_title_clicked(self, _event: tk.Event[Any] | None = None) -> None:
        chat_id = (self.current_chat_id or "").strip()
        if not chat_id:
            return

        runtime = self._require_runtime()
        if runtime is None:
            return

        current_title = self.chat_titles_by_id.get(chat_id) or self.tasks_title_label.cget("text")
        value = simpledialog.askstring(
            "Переименовать чат",
            "Введите новое название чата",
            initialvalue=current_title,
            parent=self.root,
        )
        if value is None:
            return
        new_title = value.strip()
        if not new_title or new_title == current_title:
            return

        self._submit(
            runtime.rename_chat(chat_id, new_title),
            on_success=self._on_chat_renamed,
            action="chat.rename",
        )

    def _on_chat_renamed(self, result: dict[str, Any]) -> None:
        chat_id = str(result.get("chat_id") or "").strip()
        title = str(result.get("title") or "").strip()
        if not chat_id or not title:
            return

        self.chat_titles_by_id[chat_id] = title
        for chat in self.chat_preview_items:
            if str((chat or {}).get("chat_id") or "") == chat_id:
                chat["title"] = title

        self._refresh_status_labels()
        self._refresh_chats()

        if not bool(result.get("remote_updated")):
            self._append_system("Chat title updated locally (remote endpoint unavailable).")

    def _delete_chat_clicked(self) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return

        target_chat_id = (self.current_chat_id or "").strip()
        if not target_chat_id:
            hint_lines: list[str] = []
            for chat in self.chat_preview_items[:6]:
                if not isinstance(chat, dict):
                    continue
                chat_id = str(chat.get("chat_id") or "").strip()
                if not chat_id:
                    continue
                title = str(chat.get("title") or "").strip() or f"Chat {chat_id[:8]}"
                hint_lines.append(f"- {title}: {chat_id}")

            hint_block = "\n".join(hint_lines) if hint_lines else ""
            value = simpledialog.askstring(
                "Удаление чата",
                "Введите chat_id для удаления:\n"
                f"{hint_block}",
                parent=self.root,
            )
            if value is None:
                return
            target_chat_id = value.strip()

        if not target_chat_id:
            self._append_system("chat_id is required for deletion")
            return

        title = self.chat_titles_by_id.get(target_chat_id, f"Chat {target_chat_id[:8]}")
        confirmed = messagebox.askyesno(
            "Удаление чата",
            f"Удалить чат '{title}'?",
            parent=self.root,
        )
        if not confirmed:
            return

        self._submit(
            runtime.delete_chat(target_chat_id),
            on_success=self._on_chat_deleted,
            action="chat.delete",
        )

    def _on_chat_deleted(self, result: dict[str, Any]) -> None:
        deleted_chat_id = str(result.get("chat_id") or "").strip()
        if not deleted_chat_id:
            self._refresh_chats()
            return

        self.chat_message_history.pop(deleted_chat_id, None)
        self.chat_history_loading.discard(deleted_chat_id)
        self.chat_titles_by_id.pop(deleted_chat_id, None)
        self.chat_preview_items = [
            item
            for item in self.chat_preview_items
            if str((item or {}).get("chat_id") or "") != deleted_chat_id
        ]

        if self.current_chat_id == deleted_chat_id:
            self.current_chat_id = None
            self.chat_status_var.set("not created")
            self.chat_var.set("")
            self._render_chat_history(None)
            self._clear_pending_changes(discard_remote=True)

        for label, chat_id in list(self.chat_choices.items()):
            if chat_id == deleted_chat_id:
                if self.chat_var.get().strip() == label:
                    self.chat_var.set("")
                self.chat_choices.pop(label, None)

        self._refresh_status_labels()
        self._append_system(f"Chat deleted: {deleted_chat_id}")
        self._refresh_chats()

    def _apply_controls_visibility(self) -> None:
        for panel in (self.connection_panel, self.auth_panel, self.model_panel):
            if panel.winfo_ismapped():
                panel.pack_forget()

        if self.current_chat_id:
            return

        if self.controls_collapsed:
            return

        self.connection_panel.pack(fill=tk.X, padx=14, pady=(0, 8), before=self.result_panel)
        if self.auth_required:
            self.auth_panel.pack(fill=tk.X, padx=14, pady=(0, 8), before=self.result_panel)
        else:
            self.model_panel.pack(fill=tk.X, padx=14, pady=(0, 8), before=self.result_panel)

    def _set_composer_enabled(self, enabled: bool) -> None:
        self.composer_enabled = enabled
        state = tk.NORMAL if enabled else tk.DISABLED
        self.message_input.configure(state=state)
        self._refresh_composer_action_state()

    def _refresh_composer_action_state(self) -> None:
        theme = THEMES.get(self.theme_var.get(), THEMES["agent-dark"])
        can_send = self.composer_enabled
        self.send_enabled = can_send
        self._set_flat_button_disabled(self.send_btn, not can_send)
        if can_send:
            self.send_btn.configure(bg=theme["button_soft_bg"], fg=theme["button_soft_fg"])
        else:
            self.send_btn.configure(bg=theme["button_bg"], fg=theme["muted"])

        has_pending = bool(self.pending_change_id and self.pending_changes)
        can_undo_pending = self.composer_enabled and has_pending
        self._set_flat_button_disabled(self.reject_changes_btn, not can_undo_pending)
        if has_pending:
            self.reject_changes_btn.configure(bg=theme["button_bg"], fg=theme["button_fg"])
        else:
            self.reject_changes_btn.configure(bg=theme["button_bg"], fg=theme["muted"])

    def _clear_models(self) -> None:
        self.model_choices = []
        self._update_model_menu([])
        self.current_model_id = None
        self.model_var.set("")
        self.model_status_var.set("not selected")

    def _clear_chats(self) -> None:
        self.chat_choices = {}
        self.chat_preview_items = []
        self.show_all_chats = False
        self.chat_titles_by_id = {}
        self.chat_history_loading.clear()
        self.task_row_chat_ids = [None for _ in self.task_rows]
        self.chat_message_history = {}
        self._update_chat_menu([])
        self.current_chat_id = None
        self.chat_var.set("")
        self.chat_status_var.set("not created")
        self._render_chat_history(None)
        self._clear_pending_changes(discard_remote=True)

    def _set_pending_changes(self, pending_id: str | None, pending_changes: list[dict[str, Any]]) -> None:
        clean_pending_id = (pending_id or "").strip() or None
        clean_changes = [item for item in pending_changes if isinstance(item, dict)]
        old_pending_id = self.pending_change_id
        if not clean_pending_id or not clean_changes:
            clean_pending_id = None
            clean_changes = []
        if clean_pending_id is None or clean_pending_id != old_pending_id:
            self.pending_expanded_items = set()
        self.pending_change_id = clean_pending_id
        self.pending_changes = clean_changes
        self._refresh_composer_action_state()
        self._refresh_pending_panel()

    def _clear_pending_changes(self, *, discard_remote: bool) -> None:
        old_pending_id = (self.pending_change_id or "").strip()
        self.pending_change_id = None
        self.pending_changes = []
        self.pending_expanded_items = set()
        self._refresh_composer_action_state()
        self._refresh_pending_panel()
        if not discard_remote or not old_pending_id:
            return

        if not self.runtime_ready or self.runtime is None:
            return
        runtime = self.runtime
        self._submit(
            runtime.discard_applied_changes(old_pending_id),
            action="changes.discard",
        )

    def _discard_pending_changes_clicked(self) -> None:
        pending_id = (self.pending_change_id or "").strip()
        if not pending_id:
            return
        runtime = self._require_runtime()
        if runtime is None:
            return
        self._set_flat_button_disabled(self.reject_changes_btn, True)
        self._submit(
            runtime.undo_applied_changes(pending_id),
            on_success=self._on_pending_changes_discarded,
            on_error=self._on_pending_changes_action_error,
            action="changes.undo",
        )

    def _on_pending_changes_discarded(self, result: dict[str, Any]) -> None:
        self._set_pending_changes(None, [])
        undone_files_raw = result.get("undone_files")
        undone_files = [str(item) for item in undone_files_raw] if isinstance(undone_files_raw, list) else []
        errors_raw = result.get("errors")
        errors = [str(item) for item in errors_raw] if isinstance(errors_raw, list) else []

        if undone_files or errors:
            lines = [f"Отмена изменений: {len(undone_files)}"]
            lines.extend(f"- {path}" for path in undone_files)
            if errors:
                lines.append("Ошибки:")
                lines.extend(f"- {item}" for item in errors)
            self._append_history_entry("assistant", "\n".join(lines), self.current_chat_id)
        else:
            self._append_history_entry("assistant", "Изменения отменены.", self.current_chat_id)
        self._render_chat_history(self.current_chat_id)

    def _on_pending_changes_action_error(self, _error: Exception) -> None:
        self._refresh_composer_action_state()

    def _on_pending_canvas_configure(self, event: tk.Event[Any]) -> None:
        try:
            self.pending_canvas.itemconfigure(self.pending_canvas_window, width=event.width)
        except tk.TclError:
            return

    def _toggle_pending_item(self, item_key: str) -> None:
        if item_key in self.pending_expanded_items:
            self.pending_expanded_items.remove(item_key)
        else:
            self.pending_expanded_items.add(item_key)
        self._refresh_pending_panel()

    def _refresh_pending_panel(self) -> None:
        theme = THEMES.get(self.theme_var.get(), THEMES["agent-dark"])
        has_pending = bool(self.pending_change_id and self.pending_changes)
        if not has_pending:
            if self.pending_panel.winfo_ismapped():
                self.pending_panel.pack_forget()
            for child in self.pending_rows.winfo_children():
                child.destroy()
            self.pending_stats_label.configure(text="+0 -0")
            self.pending_canvas.yview_moveto(0.0)
            return

        if not self.pending_panel.winfo_ismapped():
            self.pending_panel.pack(fill=tk.BOTH, padx=14, pady=(0, 10), before=self.composer_panel)

        for child in self.pending_rows.winfo_children():
            child.destroy()
        total_add = 0
        total_del = 0
        for idx, item in enumerate(self.pending_changes):
            path = str(item.get("path") or "")
            operation = str(item.get("operation") or "")
            diff_text = str(item.get("diff") or "")
            additions, deletions = self._count_diff_changes(diff_text)
            total_add += additions
            total_del += deletions
            item_key = f"{idx}:{operation}:{path}"
            expanded = item_key in self.pending_expanded_items

            row = tk.Frame(self.pending_rows, bd=0, relief=tk.FLAT, highlightthickness=1)
            row.pack(fill=tk.X, pady=(0, 6))
            row.configure(
                bg=theme["panel"],
                highlightbackground=theme["border"],
                highlightcolor=theme["border"],
            )

            head = tk.Frame(row, bg=theme["panel"])
            head.pack(fill=tk.X, padx=8, pady=(6, 6))

            arrow_label = tk.Label(
                head,
                text="▾" if expanded else "▸",
                anchor="e",
                font=_ui_font(11, "bold"),
                bg=theme["panel"],
                fg=theme["muted"],
                cursor="hand2",
            )
            arrow_label.pack(side=tk.RIGHT)

            del_label = tk.Label(
                head,
                text=f"-{deletions}",
                anchor="e",
                font=_ui_font(10, "bold"),
                bg=theme["panel"],
                fg="#ff5a76",
                cursor="hand2",
            )
            del_label.pack(side=tk.RIGHT, padx=(8, 6))

            add_label = tk.Label(
                head,
                text=f"+{additions}",
                anchor="e",
                font=_ui_font(10, "bold"),
                bg=theme["panel"],
                fg="#78d993",
                cursor="hand2",
            )
            add_label.pack(side=tk.RIGHT, padx=(10, 0))

            file_label = tk.Label(
                head,
                text=path or "<unknown>",
                anchor="w",
                font=_ui_font(10, "bold"),
                bg=theme["panel"],
                fg=theme["fg"],
                cursor="hand2",
            )
            file_label.pack(side=tk.LEFT, fill=tk.X, expand=True)

            for widget in (row, head, file_label, add_label, del_label, arrow_label):
                widget.bind("<Button-1>", lambda _event, key=item_key: self._toggle_pending_item(key))

            if not expanded:
                continue

            diff_view = tk.Text(
                row,
                wrap=tk.WORD,
                height=self._pending_diff_height(diff_text),
                relief=tk.FLAT,
                padx=8,
                pady=8,
                font=_ui_font(10),
            )
            diff_view.pack(fill=tk.X, padx=8, pady=(0, 8))
            diff_view.configure(
                bg=theme["bg"],
                fg=theme["input_fg"],
                insertbackground=theme["input_fg"],
                selectbackground=theme["accent"],
                selectforeground=theme["fg"],
                inactiveselectbackground=theme["border"],
                state=tk.NORMAL,
                cursor="xterm",
            )
            self._render_pending_diff(diff_view, path, diff_text, theme)

        self.pending_stats_label.configure(text=f"+{total_add} -{total_del}")
        bbox = self.pending_canvas.bbox("all")
        self.pending_canvas.configure(scrollregion=bbox if bbox else (0, 0, 0, 0))

    @staticmethod
    def _pending_diff_height(diff_text: str) -> int:
        line_count = len((diff_text or "").splitlines())
        return max(6, line_count + 2)

    @staticmethod
    def _render_pending_diff(
        target: tk.Text,
        path: str,
        diff_text: str,
        theme: dict[str, str],
    ) -> None:
        target.configure(state=tk.NORMAL)
        target.delete("1.0", tk.END)
        target.tag_configure("pending_file", foreground=theme["fg"], font=_ui_font(10, "bold"))
        target.tag_configure("pending_meta", foreground=theme["muted"], font=_ui_font(10))
        target.tag_configure("pending_ctx", foreground=theme["input_fg"], font=_ui_font(10))
        target.tag_configure("pending_add", foreground="#8ee6a2", font=_ui_font(10))
        target.tag_configure("pending_del", foreground="#ff8da1", font=_ui_font(10))
        if path:
            target.insert(tk.END, f"{path}\n", "pending_file")
            target.insert(tk.END, "\n", "pending_meta")

        clean_diff = (diff_text or "").rstrip()
        if not clean_diff:
            target.insert(tk.END, "Нет diff для выбранного изменения.\n", "pending_meta")
            target.configure(state=tk.DISABLED)
            return

        hunk_re = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
        old_line_no: int | None = None
        new_line_no: int | None = None
        for line in clean_diff.splitlines():
            line_tag = "pending_ctx"
            rendered = line
            if line.startswith("@@") or line.startswith(("+++ ", "--- ")):
                line_tag = "pending_meta"
                match = hunk_re.match(line)
                if match:
                    old_line_no = int(match.group(1))
                    new_line_no = int(match.group(2))
            elif line.startswith("+"):
                line_tag = "pending_add"
                number = new_line_no if new_line_no is not None else 0
                rendered = f"{number:>5} | {line}"
                if new_line_no is not None:
                    new_line_no += 1
            elif line.startswith("-"):
                line_tag = "pending_del"
                number = old_line_no if old_line_no is not None else 0
                rendered = f"{number:>5} | {line}"
                if old_line_no is not None:
                    old_line_no += 1
            elif line.startswith(" "):
                number = new_line_no if new_line_no is not None else (old_line_no if old_line_no is not None else 0)
                rendered = f"{number:>5} | {line[1:]}"
                if new_line_no is not None:
                    new_line_no += 1
                if old_line_no is not None:
                    old_line_no += 1
            target.insert(tk.END, f"{rendered}\n", line_tag)

        target.see("1.0")
        target.configure(state=tk.DISABLED)

    @staticmethod
    def _count_diff_changes(diff_text: str) -> tuple[int, int]:
        additions = 0
        deletions = 0
        for line in (diff_text or "").splitlines():
            if line.startswith(("+++", "---", "@@")):
                continue
            if line.startswith("+"):
                additions += 1
                continue
            if line.startswith("-"):
                deletions += 1
        return additions, deletions

    def _ensure_task_row_capacity(self, min_rows: int) -> None:
        while len(self.task_rows) < min_rows:
            row_idx = len(self.task_rows)
            row = tk.Frame(self.status_panel)
            self.task_row_frames.append(row)

            row_title = tk.Label(
                row,
                anchor="w",
                relief=tk.FLAT,
                bd=0,
                highlightthickness=0,
                font=_ui_font(11, "bold"),
            )
            row_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
            row_title.bind("<Button-1>", lambda _event, idx=row_idx: self._open_chat_by_row(idx))
            self.flat_buttons.add(row_title)

            row_meta = tk.Label(row, anchor="e", width=6, font=_ui_font(11))
            row_meta.pack(side=tk.RIGHT)

            self.task_rows.append((row_title, row_meta))
            self.task_row_chat_ids.append(None)

    def _history_key(self, chat_id: str | None) -> str:
        clean = (chat_id or "").strip()
        if clean:
            return clean
        return self._draft_history_key

    def _append_history_entry(self, role: str, text: str, chat_id: str | None) -> None:
        clean_text = (text or "").strip()
        if role not in {"user", "assistant"} or not clean_text:
            return
        key = self._history_key(chat_id)
        bucket = self.chat_message_history.setdefault(key, [])
        bucket.append((role, clean_text))

    def _migrate_draft_history(self, target_chat_id: str) -> None:
        target_key = self._history_key(target_chat_id)
        draft = self.chat_message_history.pop(self._draft_history_key, [])
        if not draft:
            return
        bucket = self.chat_message_history.setdefault(target_key, [])
        bucket.extend(draft)

    def _render_chat_history(self, chat_id: str | None) -> None:
        key = self._history_key(chat_id)
        messages = self.chat_message_history.get(key, [])
        self._refresh_chat_bubble_layout()

        self.result_text.delete("1.0", tk.END)

        if not messages:
            if not self.empty_state_label.winfo_ismapped():
                self.empty_state_label.place(relx=0.5, rely=0.5, anchor="center")
            return

        if self.empty_state_label.winfo_ismapped():
            self.empty_state_label.place_forget()

        for role, message in messages:
            if role == "user":
                self.result_text.insert(tk.END, "Вы\n", "user_meta")
                self._insert_markdown_bubble(message, "user_text")
                self.result_text.insert(tk.END, "\n", "chat_gap")
            else:
                self.result_text.insert(tk.END, "Agent\n", "assistant_meta")
                self._insert_markdown_bubble(message, "assistant_text")
                self.result_text.insert(tk.END, "\n", "chat_gap")

        self.result_text.see(tk.END)

    def _insert_markdown_bubble(self, text: str, base_tag: str) -> None:
        cleaned = (text or "").strip()
        if not cleaned:
            self.result_text.insert(tk.END, "    \n", base_tag)
            return

        lines = cleaned.splitlines()
        in_code = False
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code:
                    in_code = False
                    continue
                in_code = True
                language = stripped[3:].strip()
                if language:
                    self.result_text.insert(
                        tk.END,
                        f"  [{language}]  \n",
                        (base_tag, "md_code_header"),
                    )
                continue

            if in_code:
                self.result_text.insert(
                    tk.END,
                    f"  {line}  \n",
                    (base_tag, "md_code_block"),
                )
                continue

            rendered_line = self._strip_markdown_inline(line)
            if rendered_line.strip().startswith("- "):
                rendered_line = f"• {rendered_line.strip()[2:]}"
            self.result_text.insert(tk.END, f"  {rendered_line}  \n", base_tag)

    @staticmethod
    def _strip_markdown_inline(text: str) -> str:
        value = text or ""
        value = re.sub(r"\*\*(.*?)\*\*", r"\1", value)
        value = re.sub(r"__(.*?)__", r"\1", value)
        value = re.sub(r"`([^`]+)`", r"\1", value)
        return value

    def _refresh_chat_bubble_layout(self) -> None:
        width = self.result_text.winfo_width()
        if width <= 1:
            width = self.result_panel.winfo_width()
        if width <= 1:
            width = 380

        side_margin = max(40, int(width * 0.22))
        edge_margin = 12

        self.result_text.tag_configure(
            "user_meta",
            justify=tk.RIGHT,
            lmargin1=side_margin,
            lmargin2=side_margin,
            rmargin=edge_margin,
            spacing1=2,
            spacing3=2,
        )
        self.result_text.tag_configure(
            "assistant_meta",
            justify=tk.LEFT,
            lmargin1=edge_margin,
            lmargin2=edge_margin,
            rmargin=side_margin,
            spacing1=2,
            spacing3=2,
        )
        self.result_text.tag_configure(
            "user_text",
            justify=tk.RIGHT,
            lmargin1=side_margin,
            lmargin2=side_margin,
            rmargin=edge_margin,
            spacing1=0,
            spacing3=0,
        )
        self.result_text.tag_configure(
            "assistant_text",
            justify=tk.LEFT,
            lmargin1=edge_margin,
            lmargin2=edge_margin,
            rmargin=side_margin,
            spacing1=0,
            spacing3=0,
        )
        self.result_text.tag_configure("chat_gap", spacing1=0, spacing3=0)

    def _load_chat_history(self, chat_id: str, *, force_refresh: bool = False) -> None:
        runtime = self._require_runtime()
        if runtime is None:
            return

        clean_chat_id = str(chat_id or "").strip()
        if not clean_chat_id:
            return
        if clean_chat_id in self.chat_history_loading:
            return

        existing = self.chat_message_history.get(clean_chat_id, [])
        if existing and not force_refresh:
            return

        self.chat_history_loading.add(clean_chat_id)
        self._submit(
            runtime.get_chat_history(clean_chat_id),
            on_success=lambda messages, cid=clean_chat_id: self._on_chat_history_loaded(cid, messages),
            on_error=lambda _error, cid=clean_chat_id: self._on_chat_history_failed(cid),
            action="chat.history",
        )

    def _on_chat_history_loaded(self, chat_id: str, messages: list[dict[str, str]]) -> None:
        self.chat_history_loading.discard(chat_id)

        remote_history: list[tuple[str, str]] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            content = str(item.get("content") or "").strip()
            if role not in {"user", "assistant"} or not content:
                continue
            remote_history.append((role, content))

        existing = self.chat_message_history.get(chat_id, [])
        if existing and remote_history:
            merged = list(remote_history)
            for row in existing:
                if row not in merged:
                    merged.append(row)
            self.chat_message_history[chat_id] = merged
        elif remote_history:
            self.chat_message_history[chat_id] = remote_history
        else:
            self.chat_message_history.setdefault(chat_id, existing)

        if self.current_chat_id == chat_id:
            self._render_chat_history(chat_id)

    def _on_chat_history_failed(self, chat_id: str) -> None:
        self.chat_history_loading.discard(chat_id)

    def _update_model_menu(self, options: list[str]) -> None:
        menu = self.model_menu["menu"]
        menu.delete(0, "end")

        if not options:
            menu.add_command(label="No models", command=tk._setit(self.model_var, ""))
            return

        for option in options:
            menu.add_command(label=option, command=tk._setit(self.model_var, option))

    def _update_chat_menu(self, options: list[tuple[str, str]]) -> None:
        self.chat_choices = {label: chat_id for label, chat_id in options}

        menu = self.chat_menu["menu"]
        menu.delete(0, "end")

        if not options:
            menu.add_command(label="No chats", command=tk._setit(self.chat_var, ""))
            return

        for label, _chat_id in options:
            menu.add_command(label=label, command=tk._setit(self.chat_var, label))

    def _on_enter_key(self, event: tk.Event[Any]) -> str | None:
        if event.state & 0x1:
            return None
        self._send_message()
        return "break"

    def _on_message_input_shortcuts(self, event: tk.Event[Any]) -> str | None:
        action = self._resolve_shortcut_action(event)
        if action == "paste":
            return self._paste_into_message_input()
        if action == "copy":
            self.message_input.event_generate("<<Copy>>")
            return "break"
        if action == "select_all":
            self.message_input.tag_add(tk.SEL, "1.0", "end-1c")
            self.message_input.mark_set(tk.INSERT, "1.0")
            self.message_input.see(tk.INSERT)
            return "break"
        return None

    def _paste_into_message_input(self, _event: tk.Event[Any] | None = None) -> str:
        if str(self.message_input.cget("state")) != tk.NORMAL:
            return "break"
        try:
            payload = self.root.clipboard_get()
        except tk.TclError:
            return "break"
        if payload:
            self.message_input.insert(tk.INSERT, payload)
        return "break"

    def _on_result_text_configure(self, _event: tk.Event[Any]) -> None:
        self._refresh_chat_bubble_layout()

    def _focus_result_text(self, _event: tk.Event[Any] | None = None) -> None:
        self.result_text.focus_set()

    def _on_result_text_shortcuts(self, event: tk.Event[Any]) -> str | None:
        action = self._resolve_shortcut_action(event)
        if action == "copy":
            return self._copy_result_selection()
        if action == "select_all":
            return self._select_all_result_text()
        return None

    @staticmethod
    def _resolve_shortcut_action(event: tk.Event[Any]) -> str | None:
        key = (event.keysym or "").lower()
        char = (event.char or "").lower()
        tokens = {key, char}

        if tokens & {"c", "с", "cyrillic_es"}:
            return "copy"
        if tokens & {"v", "м", "cyrillic_em"}:
            return "paste"
        if tokens & {"a", "ф", "cyrillic_ef"}:
            return "select_all"

        if IS_DARWIN:
            keycode = int(getattr(event, "keycode", -1))
            if keycode == 8:
                return "copy"
            if keycode == 9:
                return "paste"
            if keycode == 0:
                return "select_all"
        return None

    def _open_result_context_menu(self, event: tk.Event[Any]) -> str:
        self.result_text.focus_set()
        try:
            self.result_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.result_context_menu.grab_release()
        return "break"

    def _copy_result_selection(self, _event: tk.Event[Any] | None = None) -> str:
        selection_ranges = self.result_text.tag_ranges(tk.SEL)
        if len(selection_ranges) != 2:
            return "break"
        selected = self.result_text.get(selection_ranges[0], selection_ranges[1])
        if not selected:
            return "break"
        self.result_text.clipboard_clear()
        self.result_text.clipboard_append(selected)
        self.result_text.update()
        return "break"

    def _select_all_result_text(self, _event: tk.Event[Any] | None = None) -> str:
        self.result_text.tag_add(tk.SEL, "1.0", "end-1c")
        self.result_text.mark_set(tk.INSERT, "1.0")
        self.result_text.see(tk.INSERT)
        return "break"

    def _on_theme_changed(self, *_args: Any) -> None:
        self._apply_theme()

    def _on_model_changed(self, *_args: Any) -> None:
        model = self.model_var.get().strip()
        self.current_model_id = model or None
        self.model_status_var.set(self.current_model_id or "not selected")
        self._persist_default_model(self.current_model_id)
        self._refresh_status_labels()

    def _persist_default_model(self, model_id: str | None) -> None:
        cleaned_model = (model_id or "").strip()
        if not cleaned_model:
            return
        if self.config is None:
            return
        if self.config.agent.default_model == cleaned_model:
            return

        try:
            agent_cfg = self.config.agent.model_copy(update={"default_model": cleaned_model})
            self.config = self.config.model_copy(update={"agent": agent_cfg})
            self._save_config(self.config, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self._append_system(f"Failed to persist default model: {exc}")

    def _on_chat_selected(self, *_args: Any) -> None:
        label = self.chat_var.get().strip()
        if not label:
            return

        chat_id = self.chat_choices.get(label)
        if not chat_id:
            return
        self._open_chat(chat_id)

    def _refresh_status_labels(self) -> None:
        self.service_label.config(text=f"Service: {self.service_status_var.get()}")
        self.auth_label.config(text=f"Auth: {self.auth_status_var.get()}")
        self.model_label.config(text=f"Model: {self.model_status_var.get()}")
        self.chat_label.config(text=f"Chat: {self.chat_status_var.get()}")

        total_chats = len(self.chat_preview_items)
        expanded = self.show_all_chats and total_chats > 3
        visible_rows = total_chats if expanded else 3
        if visible_rows < 3:
            visible_rows = 3
        self._ensure_task_row_capacity(visible_rows)

        chat_rows: list[tuple[str, str, str]] = []
        active_chat_title: str | None = None
        for chat in self.chat_preview_items:
            chat_id = chat.get("chat_id")
            if not chat_id:
                continue

            chat_id_text = str(chat_id)
            title = (chat.get("title") or "").strip() or f"Chat {chat_id_text[:8]}"
            self.chat_titles_by_id.setdefault(chat_id_text, title)
            if str(chat_id) == self.current_chat_id:
                active_chat_title = title

            updated = chat.get("updated_at") or chat.get("created_at")
            chat_rows.append((self._trim_task_text(title, 40), self._format_task_age(updated), chat_id_text))
            if not expanded and len(chat_rows) >= 3:
                break

        if self.current_chat_id and not active_chat_title:
            active_chat_title = self.chat_titles_by_id.get(
                self.current_chat_id,
                f"Chat {self.current_chat_id[:8]}",
            )

        if self.current_chat_id:
            if self.chat_back_btn.winfo_ismapped():
                self.chat_back_btn.pack_forget()
            self.chat_back_btn.pack(side=tk.LEFT, padx=(0, 6), before=self.tasks_title_label)
            self.chat_back_btn.lift()
            self.tasks_title_label.config(text=self._trim_task_text(active_chat_title or "Chat", 28))
            self.tasks_title_label.config(cursor="hand2")
            if not self.chat_delete_btn.winfo_ismapped():
                self.chat_delete_btn.pack(side=tk.LEFT, padx=(8, 0))
            if self.status_actions.winfo_ismapped():
                self.status_actions.pack_forget()
            for row_frame in self.task_row_frames:
                if row_frame.winfo_ismapped():
                    row_frame.pack_forget()
            if self.view_all_btn.winfo_ismapped():
                self.view_all_btn.pack_forget()
        else:
            if self.chat_back_btn.winfo_ismapped():
                self.chat_back_btn.pack_forget()
            if self.chat_delete_btn.winfo_ismapped():
                self.chat_delete_btn.pack_forget()
            self.tasks_title_label.config(text="Задачи")
            self.tasks_title_label.config(cursor="arrow")
            if not self.status_actions.winfo_ismapped():
                self.status_actions.pack(side=tk.RIGHT)
            if self.view_all_btn.winfo_ismapped():
                self.view_all_btn.pack_forget()
            for idx, row_frame in enumerate(self.task_row_frames):
                if idx < visible_rows:
                    if not row_frame.winfo_ismapped():
                        row_frame.pack(fill=tk.X, padx=12, pady=(0, 4))
                elif row_frame.winfo_ismapped():
                    row_frame.pack_forget()
            self.view_all_btn.pack(fill=tk.X, padx=12, pady=(2, 10))

        if not self.current_chat_id and chat_rows:
            for idx, (title_btn, meta_label) in enumerate(self.task_rows):
                if idx < len(chat_rows):
                    title, age, chat_id = chat_rows[idx]
                    marker = "▸ " if chat_id == self.current_chat_id else ""
                    title_btn.config(
                        text=f"{marker}{title}",
                        cursor="hand2",
                    )
                    meta_label.config(text=age)
                    self.task_row_chat_ids[idx] = chat_id
                elif idx < visible_rows:
                    title_btn.config(text="", cursor="arrow")
                    meta_label.config(text="")
                    self.task_row_chat_ids[idx] = None
                else:
                    title_btn.config(text="", cursor="arrow")
                    meta_label.config(text="")
                    self.task_row_chat_ids[idx] = None
        elif not self.current_chat_id:
            fallback_rows = [
                ("Подключение к OpenWebUI", self._compact_status(self.service_status_var.get())),
                ("Авторизация", self._compact_status(self.auth_status_var.get())),
                ("Выбор модели", self._compact_status(self.model_status_var.get())),
            ]
            for idx, (title_btn, meta_label) in enumerate(self.task_rows):
                if idx < len(fallback_rows) and idx < visible_rows:
                    left, right = fallback_rows[idx]
                    title_btn.config(text=left, cursor="arrow")
                    meta_label.config(text=right)
                    self.task_row_chat_ids[idx] = None
                elif idx < visible_rows:
                    title_btn.config(text="", cursor="arrow")
                    meta_label.config(text="")
                    self.task_row_chat_ids[idx] = None
                else:
                    title_btn.config(text="", cursor="arrow")
                    meta_label.config(text="")
                    self.task_row_chat_ids[idx] = None

        if not self.current_chat_id:
            if total_chats > 3:
                if expanded:
                    self.view_all_btn.config(text=f"Свернуть ({total_chats})")
                else:
                    self.view_all_btn.config(text=f"Просмотреть все ({total_chats})")
            else:
                self.view_all_btn.config(text=f"Просмотреть все ({total_chats})")

    @staticmethod
    def _trim_task_text(value: str, limit: int) -> str:
        text = value.strip()
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)]}..."

    @staticmethod
    def _compact_status(value: str) -> str:
        cleaned = (value or "").strip().lower()
        aliases = {
            "connected": "online",
            "connecting": "wait",
            "disconnected": "off",
            "checking": "wait",
            "authorized": "ok",
            "authorization required": "login",
            "not selected": "none",
            "not created": "new",
            "no chats": "none",
            "no models": "none",
        }
        resolved = aliases.get(cleaned, cleaned or "n/a")
        if len(resolved) <= 6:
            return resolved
        return resolved[:6]

    @staticmethod
    def _format_task_age(raw_value: Any) -> str:
        if raw_value is None:
            return ""
        text = str(raw_value).strip()
        if not text:
            return ""
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"

        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            return ""

        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        else:
            stamp = stamp.astimezone(timezone.utc)

        delta = datetime.now(timezone.utc) - stamp
        seconds = int(delta.total_seconds())
        if seconds < 0:
            return "now"
        if seconds < 60:
            return "now"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}m"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}h"
        days = hours // 24
        return f"{days}d"

    def _create_flat_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        width: int | None = None,
        anchor: str = "center",
    ) -> tk.Label:
        label = tk.Label(
            parent,
            text=text,
            anchor=anchor,
            width=width,
            padx=8,
            pady=4,
            cursor="hand2",
        )
        label.bind("<Button-1>", lambda _event, cb=command, widget=label: self._invoke_flat_button(widget, cb))
        self.flat_buttons.add(label)
        return label

    @staticmethod
    def _invoke_flat_button(widget: tk.Label, command: Callable[[], None]) -> None:
        if getattr(widget, "_flat_disabled", False):
            return
        command()

    @staticmethod
    def _set_flat_button_disabled(widget: tk.Label, disabled: bool) -> None:
        setattr(widget, "_flat_disabled", disabled)
        widget.configure(cursor="arrow" if disabled else "hand2")

    def _apply_theme(self) -> None:
        theme_name = self.theme_var.get()
        theme = THEMES.get(theme_name, THEMES["agent-dark"])

        self.root.configure(bg=theme["bg"])
        self.main.configure(bg=theme["bg"])
        self.header.configure(bg=theme["bg"])
        self.header_top.configure(bg=theme["bg"])
        self.header_model_wrap.configure(bg=theme["bg"])
        self.title_label.configure(bg=theme["bg"], fg=theme["fg"])
        self.header_model_label.configure(bg=theme["bg"], fg=theme["muted"])
        self.subtitle_label.configure(bg=theme["bg"], fg=theme["muted"])

        def style_panel_children(panel: tk.Frame, bg: str, fg: str) -> None:
            for child in panel.winfo_children():
                if isinstance(child, tk.Label):
                    if child in self.flat_buttons:
                        child.configure(
                            bg=theme["button_bg"],
                            fg=theme["button_fg"],
                            relief=tk.FLAT,
                            bd=0,
                            borderwidth=0,
                            highlightthickness=0,
                            highlightbackground=theme["button_bg"],
                        )
                    else:
                        child.configure(bg=bg, fg=fg)
                elif isinstance(child, tk.Frame):
                    child.configure(bg=bg)
                    for nested in child.winfo_children():
                        if isinstance(nested, tk.Label):
                            if nested in self.flat_buttons:
                                nested.configure(
                                    bg=theme["button_bg"],
                                    fg=theme["button_fg"],
                                    relief=tk.FLAT,
                                    bd=0,
                                    borderwidth=0,
                                    highlightthickness=0,
                                    highlightbackground=theme["button_bg"],
                                )
                            else:
                                nested.configure(bg=bg, fg=fg)
                        elif isinstance(nested, tk.Button):
                            button_bg = theme["button_bg"]
                            nested.configure(
                                bg=button_bg,
                                fg=theme["button_fg"],
                                activebackground=button_bg,
                                activeforeground=theme["button_fg"],
                                relief=tk.FLAT,
                                bd=0,
                                borderwidth=0,
                                overrelief=tk.FLAT,
                                highlightthickness=0,
                                highlightbackground=button_bg,
                            )
                        elif isinstance(nested, tk.Frame):
                            nested.configure(bg=bg)
                elif isinstance(child, tk.Button):
                    button_bg = theme["button_bg"]
                    child.configure(
                        bg=button_bg,
                        fg=theme["button_fg"],
                        activebackground=button_bg,
                        activeforeground=theme["button_fg"],
                        relief=tk.FLAT,
                        bd=0,
                        borderwidth=0,
                        overrelief=tk.FLAT,
                        highlightthickness=0,
                        highlightbackground=button_bg,
                    )

        for panel in (self.connection_panel, self.auth_panel, self.model_panel):
            panel.configure(
                bg=theme["panel"],
                highlightbackground=theme["border"],
                highlightcolor=theme["border"],
                highlightthickness=1,
            )
            style_panel_children(panel, theme["panel"], theme["fg"])

        self.pending_panel.configure(
            bg=theme["panel_alt"],
            highlightbackground=theme["border"],
            highlightcolor=theme["border"],
            highlightthickness=1,
        )
        style_panel_children(self.pending_panel, theme["panel_alt"], theme["fg"])
        self.pending_body.configure(bg=theme["panel_alt"])
        self.pending_canvas.configure(bg=theme["panel_alt"], highlightbackground=theme["panel_alt"])
        self.pending_rows.configure(bg=theme["panel_alt"])
        self.pending_scrollbar.configure(
            bg=theme["panel"],
            activebackground=theme["panel"],
            troughcolor=theme["panel_alt"],
            highlightthickness=0,
            bd=0,
            relief=tk.FLAT,
        )
        self.pending_title_label.configure(bg=theme["panel_alt"], fg=theme["fg"])
        self.pending_stats_label.configure(bg=theme["panel_alt"], fg=theme["muted"])

        self.status_panel.configure(
            bg=theme["panel"],
            highlightbackground=theme["accent"],
            highlightcolor=theme["accent"],
            highlightthickness=1,
        )
        style_panel_children(self.status_panel, theme["panel"], theme["fg"])
        self.tasks_title_label.configure(bg=theme["panel"], fg=theme["fg"])
        for title_label, meta_label in self.task_rows:
            title_label.configure(
                bg=theme["button_bg"],
                fg=theme["button_fg"],
                relief=tk.FLAT,
                borderwidth=0,
                highlightthickness=0,
                highlightbackground=theme["button_bg"],
            )
            meta_label.configure(bg=theme["panel"], fg=theme["muted"])

        self.composer_panel.configure(
            bg=theme["panel_alt"],
            highlightbackground=theme["border"],
            highlightcolor=theme["border"],
            highlightthickness=1,
        )
        style_panel_children(self.composer_panel, theme["panel_alt"], theme["fg"])
        self.composer_hint.configure(bg=theme["panel_alt"], fg=theme["muted"])

        self.result_panel.configure(bg=theme["bg"])
        self.service_label.configure(bg=theme["panel"], fg=theme["muted"])
        self.auth_label.configure(bg=theme["panel"], fg=theme["muted"])
        self.model_label.configure(bg=theme["panel"], fg=theme["muted"])
        self.chat_label.configure(bg=theme["panel"], fg=theme["muted"])
        self.empty_state_label.configure(bg=theme["bg"], fg=theme["button_soft_fg"])

        entries = [self.url_entry, self.username_entry, self.password_entry, self.chat_title_entry]
        for entry in entries:
            entry.configure(
                bg=theme["input_bg"],
                fg=theme["input_fg"],
                insertbackground=theme["input_fg"],
                relief=tk.FLAT,
            )

        self.message_input.configure(
            bg=theme["panel_alt"],
            fg=theme["input_fg"],
            insertbackground=theme["input_fg"],
        )

        buttons = [
            self.connect_btn,
            self.login_btn,
            self.refresh_models_btn,
            self.refresh_chats_btn,
            self.create_chat_btn,
            self.chat_back_btn,
            self.chat_delete_btn,
            self.reject_changes_btn,
            self.send_btn,
            self.controls_toggle_btn,
            self.view_all_btn,
        ]
        for button in buttons:
            button_bg = theme["button_bg"]
            button_fg = theme["button_fg"]
            if button is self.send_btn:
                button_bg = theme["button_soft_bg"]
                button_fg = theme["button_soft_fg"]
            if button is self.view_all_btn:
                button_bg = theme["button_bg"]
                button_fg = theme["button_fg"]
            button.configure(
                bg=button_bg,
                fg=button_fg,
                relief=tk.FLAT,
                bd=0,
                borderwidth=0,
                highlightthickness=0,
                highlightbackground=button_bg,
            )
            if isinstance(button, tk.Button):
                button.configure(
                    activebackground=button_bg,
                    activeforeground=button_fg,
                    overrelief=tk.FLAT,
                    disabledforeground=theme["muted"],
                )

        self.tasks_title_label.configure(bg=theme["panel"], fg=theme["fg"])
        self.view_all_btn.configure(anchor="w")

        self.theme_menu.configure(
            bg=theme["input_bg"],
            fg=theme["input_fg"],
            activebackground=theme["input_bg"],
            activeforeground=theme["input_fg"],
            relief=tk.FLAT,
            highlightthickness=0,
        )
        self.theme_menu["menu"].configure(
            bg=theme["input_bg"],
            fg=theme["input_fg"],
            activebackground=theme["button_bg"],
            activeforeground=theme["button_fg"],
        )
        self.settings_menu.configure(
            bg=theme["input_bg"],
            fg=theme["input_fg"],
            activebackground=theme["button_bg"],
            activeforeground=theme["button_fg"],
            relief=tk.FLAT,
            bd=0,
        )

        self.model_menu.configure(
            bg=theme["input_bg"],
            fg=theme["input_fg"],
            activebackground=theme["input_bg"],
            activeforeground=theme["input_fg"],
            relief=tk.FLAT,
            highlightthickness=0,
        )
        self.model_menu["menu"].configure(
            bg=theme["input_bg"],
            fg=theme["input_fg"],
            activebackground=theme["button_bg"],
            activeforeground=theme["button_fg"],
        )

        self.chat_menu.configure(
            bg=theme["input_bg"],
            fg=theme["input_fg"],
            activebackground=theme["input_bg"],
            activeforeground=theme["input_fg"],
            relief=tk.FLAT,
            highlightthickness=0,
        )
        self.chat_menu["menu"].configure(
            bg=theme["input_bg"],
            fg=theme["input_fg"],
            activebackground=theme["button_bg"],
            activeforeground=theme["button_fg"],
        )

        self.result_text.configure(
            bg=theme["bg"],
            fg=theme["input_fg"],
            insertbackground=theme["input_fg"],
            selectbackground=theme["accent"],
            selectforeground=theme["fg"],
            inactiveselectbackground=theme["border"],
        )
        self.result_text.tag_configure("system_meta", foreground=theme["muted"], font=_ui_font(9, "bold"))
        self.result_text.tag_configure(
            "user_meta",
            foreground=theme["user"],
            font=_ui_font(9, "bold"),
            background=theme["bg"],
        )
        self.result_text.tag_configure(
            "assistant_meta",
            foreground=theme["assistant"],
            font=_ui_font(9, "bold"),
            background=theme["bg"],
        )
        self.result_text.tag_configure("system_text", foreground=theme["system"])
        self.result_text.tag_configure(
            "user_text",
            foreground=theme["user"],
            background=theme["user_bubble_bg"],
            font=_ui_font(10),
        )
        self.result_text.tag_configure(
            "assistant_text",
            foreground=theme["assistant"],
            background=theme["assistant_bubble_bg"],
            font=_ui_font(10),
        )
        self.result_text.tag_configure(
            "md_code_header",
            foreground=theme["muted"],
            background=theme["panel"],
            font=_ui_font(9, "bold"),
        )
        self.result_text.tag_configure(
            "md_code_block",
            foreground=theme["input_fg"],
            background=theme["panel"],
            font=_ui_font(10),
        )
        self.result_text.tag_configure("chat_gap", background=theme["bg"])
        self.result_text.tag_raise(tk.SEL)
        self._refresh_chat_bubble_layout()
        self._refresh_pending_panel()
        self._refresh_composer_action_state()

    def _append_system(self, text: str) -> None:
        LOGGER.info("%s", text)

    def _append_message(self, role: str, text: str, meta: str) -> None:
        del meta
        self._append_history_entry(role, text, self.current_chat_id)
        self._render_chat_history(self.current_chat_id)

    def _on_close(self) -> None:
        if self.runtime_ready and self.runtime is not None:
            future = self.runner.submit(self.runtime.shutdown())
            try:
                future.result(timeout=5)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Runtime shutdown failed: %s", exc)

        self.runner.shutdown()
        self.root.destroy()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="agent_service.desktop",
        description="Agent desktop client",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["test"],
        help="Use isolated ./test workspace root.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Use isolated ./test workspace root.",
    )
    args = parser.parse_args()
    test_mode = bool(args.test or args.mode == "test")

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    root = tk.Tk()
    app = AgentDesktopApp(root, launch_root=Path.cwd(), test_mode=test_mode)
    del app
    root.mainloop()


if __name__ == "__main__":
    main()
