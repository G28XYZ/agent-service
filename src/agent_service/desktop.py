from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import re
import shutil
import sys
import threading
import tkinter as tk
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog
from typing import Any, Callable

import yaml

from .config import AppConfig, ensure_config_exists, load_config, resolve_config_path
from .openwebui_client import OpenWebUIClient
from .service import AgentRuntime
from .session_store import SessionStore
from .ui_components import build_desktop_ui

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


def _build_app_icon(size: int = 64) -> tk.PhotoImage:
    image = tk.PhotoImage(width=size, height=size)
    bg = "#08090B"
    panel = "#12141A"
    accent = "#F1AD1F"
    fg = "#ECECF0"
    muted = "#1D2533"
    stroke = max(2, size // 24)

    image.put(bg, to=(0, 0, size, size))
    image.put(accent, to=(0, 0, size, stroke))
    image.put(accent, to=(0, size - stroke, size, size))
    image.put(accent, to=(0, 0, stroke, size))
    image.put(accent, to=(size - stroke, 0, size, size))

    inner = stroke * 3
    image.put(panel, to=(inner, inner, size - inner, size - inner))
    image.put(muted, to=(inner + stroke, inner + stroke, size - inner - stroke, inner + stroke * 4))

    thickness = max(2, size // 26)
    left_start = size // 4
    right_start = size - left_start - thickness
    top = size // 3
    bottom = size - top

    for step in range(size // 6):
        y_up = top + step
        y_down = bottom - step - thickness
        x_left = left_start + step
        x_right = right_start - step
        image.put(fg, to=(x_left, y_up, x_left + thickness, y_up + thickness))
        image.put(fg, to=(x_left, y_down, x_left + thickness, y_down + thickness))
        image.put(fg, to=(x_right, y_up, x_right + thickness, y_up + thickness))
        image.put(fg, to=(x_right, y_down, x_right + thickness, y_down + thickness))

    underline_w = size // 5
    underline_h = thickness + 1
    underline_x = (size - underline_w) // 2
    underline_y = bottom - thickness
    image.put(accent, to=(underline_x, underline_y, underline_x + underline_w, underline_y + underline_h))

    return image


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
        self.app_icon = _build_app_icon()
        try:
            self.root.iconphoto(True, self.app_icon)
        except tk.TclError:
            pass

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

        self.service_status_var = tk.StringVar(value="отключено")
        self.auth_status_var = tk.StringVar(value="неизвестно")
        self.model_status_var = tk.StringVar(value="не выбрана")
        self.chat_status_var = tk.StringVar(value="не создан")

        self.theme_var = tk.StringVar(value="agent-dark")
        self.url_var = tk.StringVar(value="")
        self.username_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.model_var = tk.StringVar(value="")
        self.project_path_var = tk.StringVar(value="")
        self.chat_var = tk.StringVar(value="")
        self.chat_title_var = tk.StringVar(value="Сессия агента")

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
        self.composer_hint_default = "Напиши агенту запрос..."
        self.request_in_progress = False
        self.pending_response_chat_key: str | None = None
        self._stream_event_queue: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._stream_poll_job: str | None = None
        self._stream_chat_key: str | None = None
        self._stream_assistant_buffer = ""
        self._stream_reasoning_buffer = ""
        self._stream_status_lines: list[str] = []

        screen_h = self.root.winfo_screenheight()
        # Keep top-level window inside visible screen bounds (taskbar + window frame).
        startup_h = max(500, min(900, screen_h - 80))
        self.root.title("Агент")
        self.root.geometry(f"430x{startup_h}+0+0")
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
        build_desktop_ui(self, _ui_font)

        self._set_auth_required(True)
        self._set_composer_enabled(False)
        self._refresh_status_labels()
        self._apply_controls_visibility()

    def _bind_events(self) -> None:
        """Привязывает обработчики UI-событий к элементам интерфейса."""
        self.theme_var.trace_add("write", self._on_theme_changed)
        self.model_var.trace_add("write", self._on_model_changed)
        self.chat_var.trace_add("write", self._on_chat_selected)
        self.project_path_entry.bind("<Return>", lambda _event: self._project_path_apply_clicked())
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
            self._append_system(f"Конфиг создан: {self.config_path}")

        try:
            self.config = load_config(self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.service_status_var.set("ошибка конфига")
            self._refresh_status_labels()
            self._append_system(f"Не удалось загрузить конфиг: {exc}")
            return

        resolved_project_root = self._resolve_project_root_from_config(self.config)
        resolved_project_root.mkdir(parents=True, exist_ok=True)
        self._hydrate_project_state_from_launch_root(resolved_project_root)
        if resolved_project_root != self.project_root:
            self.project_root = resolved_project_root
            self.store = SessionStore(self.project_root)
        if self.test_mode:
            self._append_system(f"Корень проекта (test-режим): {self.project_root}")
        else:
            self._append_system(f"Корень проекта: {self.project_root}")

        self.url_var.set(self.config.openwebui.base_url)
        if self.config.openwebui.credentials.username:
            self.username_var.set(self.config.openwebui.credentials.username)
        default_model = self.config.agent.default_model.strip()
        if default_model:
            self.current_model_id = default_model
            self.model_var.set(default_model)
            self.model_status_var.set(default_model)

        self.project_path_var.set(str(resolved_project_root))
        if self.test_mode:
            self.project_path_entry.configure(state=tk.DISABLED)
            self._set_flat_button_disabled(self.project_path_apply_btn, True)
        else:
            self.project_path_entry.configure(state=tk.NORMAL)
            self._set_flat_button_disabled(self.project_path_apply_btn, False)

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
                "Сессия перенесена из стартового .agent-service в workspace .agent-service."
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
        """Обрабатывает нажатие кнопки подключения к OpenWebUI."""
        url = self.url_var.get().strip()
        if not url:
            self._append_system("Нужно указать URL OpenWebUI")
            return
        self._connect_to_url(url, persist=True)

    def _project_path_apply_clicked(self) -> None:
        """Применяет и сохраняет путь каталога проекта из поля ввода."""
        if self.config is None:
            self._append_system("Конфиг не загружен")
            return

        if self.test_mode:
            fixed_path = (self.launch_root / "test").resolve()
            self.project_path_var.set(str(fixed_path))
            self._append_system("В test-режиме используется только каталог ./test")
            return

        raw_value = self.project_path_var.get().strip()
        if not raw_value:
            self._append_system("Укажите каталог проекта")
            return

        try:
            candidate = Path(raw_value).expanduser()
            if not candidate.is_absolute():
                candidate = (self.launch_root / candidate).resolve()
            resolved_path = candidate.resolve()
            resolved_path.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001
            self._append_system(f"Не удалось подготовить каталог проекта: {exc}")
            return

        try:
            agent_cfg = self.config.agent.model_copy(update={"project_path": str(resolved_path)})
            self.config = self.config.model_copy(update={"agent": agent_cfg})
            self._save_config(self.config, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self._append_system(f"Не удалось сохранить каталог проекта: {exc}")
            return

        self._hydrate_project_state_from_launch_root(resolved_path)
        self.project_root = resolved_path
        self.store = SessionStore(self.project_root)
        self.project_path_var.set(str(resolved_path))
        self._append_system(f"Каталог проекта установлен: {self.project_root}")

        target_url = self.url_var.get().strip() or self.config.openwebui.base_url
        self._connect_to_url(target_url, persist=False)

    def _connect_to_url(self, raw_url: str, *, persist: bool) -> None:
        """Переподключает runtime к указанному URL и при необходимости сохраняет его в конфиг."""
        url = raw_url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            self._append_system("URL должен начинаться с http:// или https://")
            return

        self.service_status_var.set("подключение")
        self.auth_status_var.set("проверка")
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
        """Создает клиент/runtime, применяет URL и запускает инициализацию сервиса."""
        if self.config is None:
            self._append_system("Конфиг не загружен")
            return

        try:
            openwebui_cfg = self.config.openwebui.model_copy(update={"base_url": url})
            self.config = self.config.model_copy(update={"openwebui": openwebui_cfg})
            if persist:
                self._save_config(self.config, self.config_path)
        except Exception as exc:  # noqa: BLE001
            self.service_status_var.set("ошибка конфига")
            self._refresh_status_labels()
            self._append_system(f"Не удалось применить URL: {exc}")
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
        """Завершает этап запуска runtime и инициирует проверку авторизации."""
        self.runtime_ready = True
        self.service_status_var.set("подключено")
        self._refresh_status_labels()
        self._append_system(f"Подключено к {self.url_var.get().strip()}")
        self._check_auth_status()

    def _check_auth_status(self) -> None:
        """Запрашивает текущий статус авторизации у OpenWebUI."""
        runtime = self._require_runtime()
        if runtime is None:
            return
        self._submit(runtime.auth_status(), on_success=self._on_auth_status, action="auth.status")

    def _on_auth_status(self, result: dict[str, Any]) -> None:
        """Обрабатывает результат проверки авторизации и переключает состояние UI."""
        authenticated = bool(result.get("authenticated"))
        if authenticated:
            self.auth_status_var.set("авторизован")
            self._set_auth_required(False)
            self._set_composer_enabled(True)
            self._append_system("Сессия авторизована")
            self._refresh_models()
            self._refresh_chats()
        else:
            self.auth_status_var.set("нужна авторизация")
            self._set_auth_required(True)
            self._set_composer_enabled(False)
            self._clear_models()
            self._clear_chats()
            self._append_system("Требуется авторизация")

        self._refresh_status_labels()

    def _login_clicked(self) -> None:
        """Обрабатывает нажатие кнопки входа и отправляет запрос авторизации."""
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
        """Обновляет UI после успешного входа и запускает загрузку моделей/чатов."""
        self.password_var.set("")
        self.auth_status_var.set("авторизован")
        self._set_auth_required(False)
        self._set_composer_enabled(True)
        self._refresh_status_labels()
        self._append_system(f"Вход выполнен: {result.get('username', 'неизвестно')}")
        self._refresh_models()
        self._refresh_chats()

    def _refresh_models(self) -> None:
        """Запрашивает список доступных моделей."""
        runtime = self._require_runtime()
        if runtime is None:
            return

        self._submit(runtime.list_models(), on_success=self._on_models_loaded, action="models.list")

    def _refresh_chats(self) -> None:
        """Запрашивает список чатов."""
        runtime = self._require_runtime()
        if runtime is None:
            return

        self._submit(runtime.list_chats(), on_success=self._on_chats_loaded, action="chats.list")

    def _toggle_view_all_chats(self) -> None:
        """Переключает режим показа всех чатов в панели задач."""
        if self.current_chat_id:
            return

        total = len(self.chat_preview_items)
        if total <= 3:
            self._refresh_chats()
            return

        self.show_all_chats = not self.show_all_chats
        self._refresh_status_labels()

    def _on_models_loaded(self, models: list[dict[str, Any]]) -> None:
        """Обрабатывает загруженные модели и выбирает активную модель."""
        model_ids = [str(item.get("id")) for item in models if item.get("id")]
        self.model_choices = model_ids
        self._update_model_menu(model_ids)

        if not model_ids:
            self.current_model_id = None
            self.model_var.set("")
            self.model_status_var.set("нет моделей")
            self._refresh_status_labels()
            self._append_system("Нет доступных моделей")
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
        self._append_system(f"Моделей загружено: {len(model_ids)}")

    def _on_chats_loaded(self, chats: list[dict[str, Any]]) -> None:
        """Обрабатывает список чатов и обновляет данные для выбора/превью."""
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

            resolved_title = title or f"Чат {short_id}"
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
            self.chat_status_var.set("нет чатов")
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
        self._append_system(f"Чатов загружено: {len(options)}")
        self._refresh_status_labels()

    def _set_active_chat(self, chat_id: str) -> None:
        """Сохраняет активный чат в runtime/store."""
        runtime = self._require_runtime()
        if runtime is None:
            return
        try:
            runtime.set_active_chat(chat_id)
        except Exception as exc:  # noqa: BLE001
            self._append_system(f"Не удалось сохранить активный чат: {exc}")

    def _open_chat_by_row(self, row_idx: int) -> None:
        """Открывает чат по индексу строки в блоке задач."""
        if row_idx < 0 or row_idx >= len(self.task_row_chat_ids):
            return
        chat_id = self.task_row_chat_ids[row_idx]
        if not chat_id:
            return
        self._open_chat(chat_id)

    def _open_chat(self, chat_id: str) -> None:
        """Переключает интерфейс на выбранный чат и загружает его историю."""
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
        """Выходит из текущего чата и возвращает экран списка чатов."""
        if not self.current_chat_id:
            return

        self._finish_stream_preview()
        self._clear_pending_changes(discard_remote=True)
        self.current_chat_id = None
        self.chat_status_var.set("не создан")
        self.chat_var.set("")
        self._render_chat_history(None)
        self._refresh_status_labels()

    def _create_chat(self) -> None:
        """Создает новый чат с выбранной моделью и заголовком."""
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
        """Обрабатывает результат создания чата и делает его активным."""
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
            self._append_system(f"Чат создан: {self.current_chat_id}")
            self._set_active_chat(self.current_chat_id)
        else:
            self._append_system("Чат создан, но идентификатор чата (chat_id) отсутствует")

        self._refresh_status_labels()
        self._refresh_chats()

    def _send_message(self) -> None:
        """Отправляет сообщение пользователя в текущий чат агента."""
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
            self._append_system("Модель не выбрана")
            return

        # Start each request with a clean "last changes" block.
        self._clear_pending_changes(discard_remote=True)
        self.message_input.delete("1.0", tk.END)
        self._append_history_entry("user", message, self.current_chat_id)
        active_chat_key = self._history_key(self.current_chat_id)
        self._start_stream_preview(active_chat_key)
        self._set_request_in_progress(True, chat_key=active_chat_key)
        self._render_chat_history(self.current_chat_id)

        self._submit(
            runtime.run_agent_task(
                message=message,
                model_id=model_id,
                chat_id=self.current_chat_id,
                auto_apply=False,
                stream_callback=self._enqueue_stream_event,
            ),
            on_success=self._on_message_response,
            action="agent.task",
        )

    def _on_message_response(self, result: dict[str, Any]) -> None:
        """Обрабатывает ответ агента, обновляет историю и блок изменений."""
        self._finish_stream_preview()
        self._set_request_in_progress(False)
        model_id = result.get("model_id")
        chat_id = result.get("chat_id")
        chat_title = str(result.get("chat_title") or "").strip()
        assistant_text_raw = (result.get("assistant_message") or "").strip() or "[пустой ответ]"
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
                        "title": chat_title or f"Чат {resolved_chat_id[:8]}",
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
        """Запускает автоприменение подготовленных изменений."""
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
        """Обрабатывает результат автоприменения и обновляет блок изменений."""
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
        """Выполняет coroutine в фоне и возвращает результат в UI-поток."""
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
        """Единая обработка ошибок асинхронных операций."""
        LOGGER.exception("Action failed: %s", action)
        text = str(error)

        if action == "agent.task":
            self._finish_stream_preview()
            self._set_request_in_progress(False)
            self._render_chat_history(self.current_chat_id)

        if self._is_auth_error(text):
            self.auth_status_var.set("нужна авторизация")
            self._set_auth_required(True)
            self._set_composer_enabled(False)
            self._clear_models()
            self._refresh_status_labels()

        if action in {"runtime.startup", "runtime.shutdown"}:
            self.runtime_ready = False

        action_label = self._localize_action_name(action)
        self._append_system(f"{action_label}: ошибка: {text}")

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
            "нужна авторизация",
            "не авторизован",
        ]
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _localize_action_name(action: str) -> str:
        aliases = {
            "runtime.startup": "Инициализация",
            "runtime.shutdown": "Остановка runtime",
            "auth.status": "Проверка авторизации",
            "auth.login": "Вход",
            "models.list": "Загрузка моделей",
            "chats.list": "Загрузка чатов",
            "chat.create": "Создание чата",
            "chat.rename": "Переименование чата",
            "chat.delete": "Удаление чата",
            "chat.history": "Загрузка истории",
            "agent.task": "Запрос к агенту",
            "pending.apply": "Применение изменений",
            "changes.discard": "Сброс изменений",
            "changes.undo": "Отмена изменений",
        }
        return aliases.get((action or "").strip(), action)

    def _require_runtime(self) -> AgentRuntime | None:
        if not self.runtime_ready or self.runtime is None:
            self._append_system("Сервис еще не готов")
            return None
        return self.runtime

    def _set_auth_required(self, required: bool) -> None:
        self.auth_required = required
        self._apply_controls_visibility()

    def _toggle_controls(self) -> None:
        """Сворачивает или разворачивает дополнительные панели настроек."""
        self.controls_collapsed = not self.controls_collapsed
        self._apply_controls_visibility()

    def _open_settings_menu(self) -> None:
        """Открывает выпадающее меню настроек рядом с кнопкой."""
        self._rebuild_settings_menu()
        pos_x = self.controls_toggle_btn.winfo_rootx()
        pos_y = self.controls_toggle_btn.winfo_rooty() + self.controls_toggle_btn.winfo_height()
        try:
            self.settings_menu.tk_popup(pos_x, pos_y)
        finally:
            self.settings_menu.grab_release()

    def _rebuild_settings_menu(self) -> None:
        """Пересобирает содержимое выпадающего меню настроек."""
        self.settings_menu.delete(0, tk.END)
        self.settings_menu.add_command(
            label="Показать настройки" if self.controls_collapsed else "Скрыть настройки",
            command=self._toggle_controls,
        )
        self.settings_menu.add_separator()
        self.settings_menu.add_command(label="URL OpenWebUI...", command=self._prompt_url)
        self.settings_menu.add_command(label="Логин...", command=self._prompt_username)
        self.settings_menu.add_command(label="Пароль...", command=self._prompt_password)
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
        """Открывает диалог для редактирования URL OpenWebUI."""
        value = simpledialog.askstring(
            "URL OpenWebUI",
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
        """Открывает диалог для редактирования логина."""
        value = simpledialog.askstring(
            "Логин",
            "Введите логин",
            initialvalue=self.username_var.get().strip(),
            parent=self.root,
        )
        if value is None:
            return
        self.username_var.set(value.strip())

    def _prompt_password(self) -> None:
        """Открывает диалог для редактирования пароля."""
        value = simpledialog.askstring(
            "Пароль",
            "Введите пароль",
            show="*",
            parent=self.root,
        )
        if value is None:
            return
        self.password_var.set(value.strip())

    def _prompt_chat_title(self) -> None:
        """Открывает диалог для ввода заголовка нового чата."""
        value = simpledialog.askstring(
            "Название чата",
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
        """Открывает диалог ручного выбора модели по id."""
        if not self.model_choices:
            self._append_system("Модели не загружены. Сначала обновите список моделей.")
            return

        hint = ", ".join(self.model_choices[:6])
        value = simpledialog.askstring(
            "Модель",
            f"Введите id модели ({hint}{'...' if len(self.model_choices) > 6 else ''})",
            initialvalue=self.model_var.get().strip() or self.current_model_id or "",
            parent=self.root,
        )
        if value is None:
            return
        cleaned = value.strip()
        if not cleaned:
            return
        if cleaned not in self.model_choices:
            self._append_system(f"Неизвестная модель: {cleaned}")
            return
        self.model_var.set(cleaned)

    def _rename_current_chat_title_clicked(self, _event: tk.Event[Any] | None = None) -> None:
        """Обрабатывает переименование активного чата по клику на его заголовок."""
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
        """Обновляет локальный список чатов после переименования."""
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
            self._append_system("Название чата обновлено локально (удаленная API-точка недоступна).")

    def _delete_chat_clicked(self) -> None:
        """Обрабатывает удаление выбранного чата с подтверждением пользователя."""
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
                title = str(chat.get("title") or "").strip() or f"Чат {chat_id[:8]}"
                hint_lines.append(f"- {title}: {chat_id}")

            hint_block = "\n".join(hint_lines) if hint_lines else ""
            value = simpledialog.askstring(
                "Удаление чата",
                "Введите идентификатор чата (chat_id) для удаления:\n"
                f"{hint_block}",
                parent=self.root,
            )
            if value is None:
                return
            target_chat_id = value.strip()

        if not target_chat_id:
            self._append_system("Для удаления нужен идентификатор чата (chat_id)")
            return

        title = self.chat_titles_by_id.get(target_chat_id, f"Чат {target_chat_id[:8]}")
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
        """Удаляет чат из локального состояния после ответа сервиса."""
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
            self.chat_status_var.set("не создан")
            self.chat_var.set("")
            self._render_chat_history(None)
            self._clear_pending_changes(discard_remote=True)

        for label, chat_id in list(self.chat_choices.items()):
            if chat_id == deleted_chat_id:
                if self.chat_var.get().strip() == label:
                    self.chat_var.set("")
                self.chat_choices.pop(label, None)

        self._refresh_status_labels()
        self._append_system(f"Чат удален: {deleted_chat_id}")
        self._refresh_chats()

    def _apply_controls_visibility(self) -> None:
        """Переключает видимость блоков подключения/авторизации/моделей."""
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
        """Включает или отключает композер сообщения."""
        self.composer_enabled = enabled
        self._refresh_composer_action_state()

    def _refresh_composer_action_state(self) -> None:
        """Синхронизирует состояние кнопок отправки и отмены изменений."""
        theme = THEMES.get(self.theme_var.get(), THEMES["agent-dark"])
        can_send = self.composer_enabled and not self.request_in_progress
        self.send_enabled = can_send
        self._set_flat_button_disabled(self.send_btn, not can_send)
        if can_send:
            self.send_btn.configure(bg=theme["button_soft_bg"], fg=theme["button_soft_fg"])
        else:
            self.send_btn.configure(bg=theme["button_bg"], fg=theme["muted"])

        input_state = tk.NORMAL if can_send else tk.DISABLED
        self.message_input.configure(state=input_state)

        has_pending = bool(self.pending_change_id and self.pending_changes)
        can_undo_pending = self.composer_enabled and has_pending and not self.request_in_progress
        self._set_flat_button_disabled(self.reject_changes_btn, not can_undo_pending)
        if has_pending:
            self.reject_changes_btn.configure(bg=theme["button_bg"], fg=theme["button_fg"])
        else:
            self.reject_changes_btn.configure(bg=theme["button_bg"], fg=theme["muted"])

    def _set_request_in_progress(self, active: bool, *, chat_key: str | None = None) -> None:
        """Обновляет UI-состояние при старте/завершении запроса к агенту."""
        if active:
            self.request_in_progress = True
            self.pending_response_chat_key = chat_key or self._history_key(self.current_chat_id)
            if self._stream_poll_job is None:
                self._stream_poll_job = self.root.after(35, self._drain_stream_events)
        else:
            self.request_in_progress = False
            self.pending_response_chat_key = None
            self.composer_hint.configure(text=self.composer_hint_default)

        self._refresh_composer_action_state()

    def _start_stream_preview(self, chat_key: str) -> None:
        """Подготавливает временный блок стриминга для текущего запроса."""
        self._stream_chat_key = chat_key
        self._stream_assistant_buffer = ""
        self._stream_reasoning_buffer = ""
        self._stream_status_lines = ["Запрос отправлен в модель..."]
        self.composer_hint.configure(text="Запрос отправлен в модель...")

    def _finish_stream_preview(self) -> None:
        """Очищает временный стрим-блок после завершения запроса."""
        self._stream_chat_key = None
        self._stream_assistant_buffer = ""
        self._stream_reasoning_buffer = ""
        self._stream_status_lines = []
        while True:
            try:
                self._stream_event_queue.get_nowait()
            except queue.Empty:
                break

    def _enqueue_stream_event(self, event: dict[str, Any]) -> None:
        """Принимает stream-событие из фонового потока и ставит его в очередь UI."""
        if not isinstance(event, dict):
            return
        self._stream_event_queue.put(event)

    def _drain_stream_events(self) -> None:
        """Доставляет накопленные stream-события в UI-потоке."""
        self._stream_poll_job = None
        changed = False
        while True:
            try:
                event = self._stream_event_queue.get_nowait()
            except queue.Empty:
                break
            changed = self._apply_stream_event(event) or changed

        if changed:
            self._render_chat_history(self.current_chat_id)

        if self.request_in_progress or not self._stream_event_queue.empty():
            self._stream_poll_job = self.root.after(35, self._drain_stream_events)

    def _apply_stream_event(self, event: dict[str, Any]) -> bool:
        """Применяет одно stream-событие к временному состоянию интерфейса."""
        event_type = str(event.get("type") or "").strip().lower()
        if self._stream_chat_key is None:
            return False
        changed = False

        if event_type == "assistant_delta":
            chunk = str(event.get("text") or "")
            if chunk:
                self._stream_assistant_buffer = f"{self._stream_assistant_buffer}{chunk}"
                changed = True
            return changed

        if event_type == "reasoning_delta":
            chunk = str(event.get("text") or "")
            if chunk:
                combined = f"{self._stream_reasoning_buffer}{chunk}"
                # Keep reasoning preview bounded to avoid very large UI payload.
                self._stream_reasoning_buffer = combined[-6000:]
                changed = True
            return changed

        if event_type == "tool_call":
            name = str(event.get("name") or "tool").strip() or "tool"
            self._push_stream_status(f"Модель вызвала инструмент: {name}")
            return True

        if event_type == "tool_start":
            text = str(event.get("text") or "").strip() or "Выполняю инструмент..."
            self._push_stream_status(text)
            return True

        if event_type == "tool_result":
            text = str(event.get("text") or "").strip() or "Инструмент завершен"
            self._push_stream_status(text)
            return True

        if event_type == "status":
            text = str(event.get("text") or "").strip()
            if text:
                self._push_stream_status(text)
                return True
            return False

        return False

    def _push_stream_status(self, text: str) -> None:
        """Добавляет строку в live-статус текущего запроса и обновляет hint."""
        clean = str(text or "").strip()
        if not clean:
            return
        if self._stream_status_lines and self._stream_status_lines[-1] == clean:
            self.composer_hint.configure(text=clean)
            return
        self._stream_status_lines.append(clean)
        if len(self._stream_status_lines) > 8:
            self._stream_status_lines = self._stream_status_lines[-8:]
        self.composer_hint.configure(text=clean)

    def _clear_models(self) -> None:
        """Очищает список моделей и выбранную модель."""
        self.model_choices = []
        self._update_model_menu([])
        self.current_model_id = None
        self.model_var.set("")
        self.model_status_var.set("не выбрана")

    def _clear_chats(self) -> None:
        """Очищает чаты, историю переписки и текущий активный чат."""
        self._finish_stream_preview()
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
        self.chat_status_var.set("не создан")
        self._render_chat_history(None)
        self._clear_pending_changes(discard_remote=True)

    def _set_pending_changes(self, pending_id: str | None, pending_changes: list[dict[str, Any]]) -> None:
        """Запоминает текущий набор изменений для визуализации diff."""
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
        """Сбрасывает локальный блок изменений и опционально очищает их в runtime."""
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
        """Обрабатывает нажатие кнопки отмены примененных изменений."""
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
        """Добавляет в историю результат отмены изменений и обновляет чат."""
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
        """Восстанавливает состояние кнопок после ошибки операций с изменениями."""
        self._refresh_composer_action_state()

    def _on_pending_canvas_configure(self, event: tk.Event[Any]) -> None:
        """Поддерживает ширину внутреннего фрейма панели изменений равной canvas."""
        try:
            self.pending_canvas.itemconfigure(self.pending_canvas_window, width=event.width)
        except tk.TclError:
            return

    def _toggle_pending_item(self, item_key: str) -> None:
        """Сворачивает или разворачивает diff выбранного файла."""
        if item_key in self.pending_expanded_items:
            self.pending_expanded_items.remove(item_key)
        else:
            self.pending_expanded_items.add(item_key)
        self._refresh_pending_panel()

    def _refresh_pending_panel(self) -> None:
        """Полностью перерисовывает панель ожидающих/примененных изменений."""
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
        """Гарантирует, что в панели задач создано не меньше `min_rows` строк."""
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
        """Отрисовывает историю сообщений активного чата в области ответа."""
        key = self._history_key(chat_id)
        messages = self.chat_message_history.get(key, [])
        show_stream_preview = (
            self.request_in_progress
            and self.pending_response_chat_key == key
            and self._stream_chat_key == key
            and bool(
                self._stream_assistant_buffer
                or self._stream_reasoning_buffer
                or self._stream_status_lines
            )
        )
        self._refresh_chat_bubble_layout()

        self.result_text.delete("1.0", tk.END)

        if not messages and not show_stream_preview:
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
                self.result_text.insert(tk.END, "Агент\n", "assistant_meta")
                self._insert_markdown_bubble(message, "assistant_text")
                self.result_text.insert(tk.END, "\n", "chat_gap")

        if show_stream_preview and self._stream_reasoning_buffer.strip():
            self.result_text.insert(tk.END, "Агент (размышления)\n", "assistant_meta")
            self._insert_markdown_bubble(self._stream_reasoning_buffer, "assistant_progress")
            self.result_text.insert(tk.END, "\n", "chat_gap")

        if show_stream_preview and self._stream_assistant_buffer.strip():
            self.result_text.insert(tk.END, "Агент\n", "assistant_meta")
            self._insert_markdown_bubble(self._stream_assistant_buffer, "assistant_text")
            self.result_text.insert(tk.END, "\n", "chat_gap")

        if show_stream_preview and (not self._stream_assistant_buffer.strip()):
            self.result_text.insert(tk.END, "Агент\n", "assistant_meta")
            status_lines = self._stream_status_lines[-4:] if self._stream_status_lines else ["Ожидание ответа..."]
            for line in status_lines:
                self.result_text.insert(tk.END, f"  {line}\n", "assistant_progress")
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
        """Пересчитывает отступы пузырей сообщений при изменении ширины области."""
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
        self.result_text.tag_configure(
            "assistant_progress",
            justify=tk.LEFT,
            lmargin1=edge_margin,
            lmargin2=edge_margin,
            rmargin=side_margin,
            spacing1=0,
            spacing3=0,
        )
        self.result_text.tag_configure("chat_gap", spacing1=0, spacing3=0)

    def _load_chat_history(self, chat_id: str, *, force_refresh: bool = False) -> None:
        """Запрашивает историю чата с сервера при необходимости."""
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
        """Обрабатывает загруженную историю чата и объединяет ее с локальной."""
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
        """Снимает флаг загрузки истории после ошибки запроса."""
        self.chat_history_loading.discard(chat_id)

    def _update_model_menu(self, options: list[str]) -> None:
        menu = self.model_menu["menu"]
        menu.delete(0, "end")

        if not options:
            menu.add_command(label="Нет моделей", command=tk._setit(self.model_var, ""))
            return

        for option in options:
            menu.add_command(label=option, command=tk._setit(self.model_var, option))

    def _update_chat_menu(self, options: list[tuple[str, str]]) -> None:
        self.chat_choices = {label: chat_id for label, chat_id in options}

        menu = self.chat_menu["menu"]
        menu.delete(0, "end")

        if not options:
            menu.add_command(label="Нет чатов", command=tk._setit(self.chat_var, ""))
            return

        for label, _chat_id in options:
            menu.add_command(label=label, command=tk._setit(self.chat_var, label))

    def _on_enter_key(self, event: tk.Event[Any]) -> str | None:
        """Обрабатывает Enter в поле ввода: Enter отправляет, Shift+Enter переносит строку."""
        if event.state & 0x1:
            return None
        self._send_message()
        return "break"

    def _on_message_input_shortcuts(self, event: tk.Event[Any]) -> str | None:
        """Обрабатывает горячие клавиши поля ввода (копировать/вставить/выделить все)."""
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
        """Вставляет текст из буфера обмена в поле ввода."""
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
        """Переcчитывает отступы сообщений при изменении размера области ответа."""
        self._refresh_chat_bubble_layout()

    def _focus_result_text(self, _event: tk.Event[Any] | None = None) -> None:
        """Переводит фокус на область истории сообщений."""
        self.result_text.focus_set()

    def _on_result_text_shortcuts(self, event: tk.Event[Any]) -> str | None:
        """Обрабатывает горячие клавиши в области ответа (копировать/выделить все)."""
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
        state = int(getattr(event, "state", 0))
        ctrl_pressed = bool(state & 0x4)
        cmd_pressed = IS_DARWIN and bool(state & 0x8)
        keycode = int(getattr(event, "keycode", -1))

        # Some Tk builds report Ctrl-combos via control characters in event.char.
        if char == "\x03":
            return "copy"
        if char == "\x16":
            return "paste"
        if char == "\x01":
            return "select_all"

        if not (ctrl_pressed or cmd_pressed):
            return None

        if tokens & {"c", "с", "cyrillic_es"}:
            return "copy"
        if tokens & {"v", "м", "cyrillic_em"}:
            return "paste"
        if tokens & {"a", "ф", "cyrillic_ef"}:
            return "select_all"

        # Windows virtual-key fallback for C/V/A under Ctrl with locale/layout variations.
        if keycode in {67, 86, 65}:
            return {67: "copy", 86: "paste", 65: "select_all"}[keycode]

        if IS_DARWIN:
            if keycode == 8:
                return "copy"
            if keycode == 9:
                return "paste"
            if keycode == 0:
                return "select_all"
        return None

    def _open_result_context_menu(self, event: tk.Event[Any]) -> str:
        """Открывает контекстное меню для области ответа."""
        self.result_text.focus_set()
        try:
            self.result_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.result_context_menu.grab_release()
        return "break"

    def _copy_result_selection(self, _event: tk.Event[Any] | None = None) -> str:
        """Копирует выделенный фрагмент из области ответа в буфер обмена."""
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
        """Выделяет весь текст в области ответа."""
        self.result_text.tag_add(tk.SEL, "1.0", "end-1c")
        self.result_text.mark_set(tk.INSERT, "1.0")
        self.result_text.see(tk.INSERT)
        return "break"

    def _on_theme_changed(self, *_args: Any) -> None:
        """Обрабатывает смену темы приложения."""
        self._apply_theme()

    def _on_model_changed(self, *_args: Any) -> None:
        """Обрабатывает смену модели и сохраняет выбор по умолчанию."""
        model = self.model_var.get().strip()
        self.current_model_id = model or None
        self.model_status_var.set(self.current_model_id or "не выбрана")
        self._persist_default_model(self.current_model_id)
        self._refresh_status_labels()

    def _persist_default_model(self, model_id: str | None) -> None:
        """Сохраняет выбранную модель как модель по умолчанию в конфиг."""
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
            self._append_system(f"Не удалось сохранить модель по умолчанию: {exc}")

    def _on_chat_selected(self, *_args: Any) -> None:
        """Открывает чат, выбранный в выпадающем списке."""
        label = self.chat_var.get().strip()
        if not label:
            return

        chat_id = self.chat_choices.get(label)
        if not chat_id:
            return
        self._open_chat(chat_id)

    def _refresh_status_labels(self) -> None:
        """Обновляет статусные подписи и блок задач/чатов в верхней панели."""
        self.service_label.config(text=f"Сервис: {self.service_status_var.get()}")
        self.auth_label.config(text=f"Авторизация: {self.auth_status_var.get()}")
        self.model_label.config(text=f"Модель: {self.model_status_var.get()}")
        self.chat_label.config(text=f"Чат: {self.chat_status_var.get()}")

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
            title = (chat.get("title") or "").strip() or f"Чат {chat_id_text[:8]}"
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
                f"Чат {self.current_chat_id[:8]}",
            )

        if self.current_chat_id:
            if self.chat_back_btn.winfo_ismapped():
                self.chat_back_btn.pack_forget()
            self.chat_back_btn.pack(side=tk.LEFT, padx=(0, 6), before=self.tasks_title_label)
            self.chat_back_btn.lift()
            self.tasks_title_label.config(text=self._trim_task_text(active_chat_title or "Чат", 28))
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
            "подключено": "онлайн",
            "подключение": "ожид.",
            "отключено": "выкл.",
            "проверка": "ожид.",
            "неизвестно": "неизв.",
            "ошибка конфига": "ошибк.",
            "авторизован": "ok",
            "нужна авторизация": "вход",
            "не выбрана": "нет",
            "не создан": "нов.",
            "нет чатов": "нет",
            "нет моделей": "нет",
            "connected": "онлайн",
            "connecting": "ожид.",
            "disconnected": "выкл.",
            "checking": "ожид.",
            "authorized": "ok",
            "authorization required": "вход",
            "not selected": "нет",
            "not created": "нов.",
            "no chats": "нет",
            "no models": "нет",
        }
        resolved = aliases.get(cleaned, cleaned or "н/д")
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
            return "сейчас"
        if seconds < 60:
            return "сейчас"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes}м"
        hours = minutes // 60
        if hours < 24:
            return f"{hours}ч"
        days = hours // 24
        return f"{days}д"

    def _create_flat_button(
        self,
        parent: tk.Misc,
        text: str,
        command: Callable[[], None],
        *,
        width: int | None = None,
        anchor: str = "center",
    ) -> tk.Label:
        """Создает стилизованную кнопку на базе Label с единым поведением клика."""
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
        """Применяет текущую тему ко всем элементам интерфейса."""
        theme_name = self.theme_var.get()
        theme = THEMES.get(theme_name, THEMES["agent-dark"])

        self.root.configure(bg=theme["bg"])
        self.main.configure(bg=theme["bg"])
        self.header.configure(bg=theme["bg"])
        self.header_top.configure(bg=theme["bg"])
        self.header_model_wrap.configure(bg=theme["bg"])
        self.header_model_row.configure(bg=theme["bg"])
        self.header_project_row.configure(bg=theme["bg"])
        self.title_label.configure(bg=theme["bg"], fg=theme["fg"])
        self.header_model_label.configure(bg=theme["bg"], fg=theme["muted"])
        self.project_path_label.configure(bg=theme["bg"], fg=theme["muted"])
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

        entries = [
            self.url_entry,
            self.username_entry,
            self.password_entry,
            self.chat_title_entry,
            self.project_path_entry,
        ]
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
            self.project_path_apply_btn,
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
            "assistant_progress",
            foreground=theme["muted"],
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
        """Аккуратно завершает runtime и закрывает окно приложения."""
        if self._stream_poll_job is not None:
            try:
                self.root.after_cancel(self._stream_poll_job)
            except tk.TclError:
                pass
            self._stream_poll_job = None
        self._set_request_in_progress(False)
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
        description="Desktop-клиент агента",
    )
    parser.add_argument(
        "mode",
        nargs="?",
        choices=["test"],
        help="Использовать изолированный рабочий каталог ./test.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Использовать изолированный рабочий каталог ./test.",
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
