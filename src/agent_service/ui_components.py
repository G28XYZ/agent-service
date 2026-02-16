from __future__ import annotations

from tkinter import scrolledtext
from typing import Any, Callable

import tkinter as tk


def build_desktop_ui(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.main = tk.Frame(app.root)
    app.main.pack(fill=tk.BOTH, expand=True)

    _build_header(app, ui_font)
    _build_status_panel(app, ui_font)
    _build_connection_panel(app, ui_font)
    _build_auth_panel(app, ui_font)
    _build_model_panel(app, ui_font)
    _build_result_panel(app, ui_font)
    _build_pending_panel(app, ui_font)
    _build_composer_panel(app, ui_font)


def _build_header(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.header = tk.Frame(app.main)
    app.header.pack(fill=tk.X, padx=14, pady=(14, 8))

    app.header_top = tk.Frame(app.header)
    app.header_top.pack(fill=tk.X)

    app.title_label = tk.Label(app.header_top, text="АГЕНТ", font=ui_font(15, "bold"))
    app.title_label.pack(side=tk.LEFT, anchor="w")
    app.header_model_wrap = tk.Frame(app.header_top)
    app.header_model_wrap.pack(side=tk.RIGHT, anchor="e")
    app.header_model_row = tk.Frame(app.header_model_wrap)
    app.header_model_row.pack(anchor="e")
    app.header_model_label = tk.Label(app.header_model_row, text="Модель", font=ui_font(9, None))
    app.header_model_label.pack(side=tk.LEFT, padx=(0, 6))
    app.model_menu = tk.OptionMenu(app.header_model_row, app.model_var, "")
    app.model_menu.configure(width=26)
    app.model_menu.pack(side=tk.LEFT)
    app.header_project_row = tk.Frame(app.header_model_wrap)
    app.header_project_row.pack(anchor="e", fill=tk.X, pady=(4, 0))
    app.project_path_label = tk.Label(app.header_project_row, text="Каталог", font=ui_font(9, None))
    app.project_path_label.pack(side=tk.LEFT, padx=(0, 6))
    app.project_path_entry = tk.Entry(app.header_project_row, textvariable=app.project_path_var, width=24)
    app.project_path_entry.pack(side=tk.LEFT)
    app.project_path_apply_btn = app._create_flat_button(
        app.header_project_row,
        "Применить",
        app._project_path_apply_clicked,
    )
    app.project_path_apply_btn.pack(side=tk.LEFT, padx=(4, 0))
    app.subtitle_label = tk.Label(
        app.header,
        text="локальный агент OpenWebUI",
        font=ui_font(9, None),
    )
    app.subtitle_label.pack(anchor="w", pady=(2, 0))


def _build_status_panel(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.status_panel = tk.Frame(app.main, bd=0, relief=tk.FLAT, highlightthickness=1)
    app.status_panel.pack(fill=tk.X, padx=14, pady=(0, 10))

    status_head = tk.Frame(app.status_panel)
    status_head.pack(fill=tk.X, padx=12, pady=(10, 8))
    app.chat_back_btn = app._create_flat_button(
        status_head,
        "←",
        app._leave_chat,
        width=3,
    )
    app.tasks_title_label = tk.Label(status_head, text="Задачи", font=ui_font(15, "bold"))
    app.tasks_title_label.pack(side=tk.LEFT)
    app.tasks_title_label.bind("<Button-1>", app._rename_current_chat_title_clicked)
    app.chat_delete_btn = app._create_flat_button(
        status_head,
        "Удалить",
        app._delete_chat_clicked,
    )

    app.status_actions = tk.Frame(status_head)
    app.status_actions.pack(side=tk.RIGHT)

    app.refresh_chats_btn = app._create_flat_button(
        app.status_actions,
        "↻",
        app._refresh_chats,
        width=3,
    )
    app.refresh_chats_btn.pack(side=tk.LEFT)

    app.controls_toggle_btn = app._create_flat_button(
        app.status_actions,
        "⋯",
        app._open_settings_menu,
        width=3,
    )
    app.controls_toggle_btn.pack(side=tk.LEFT, padx=(4, 0))

    app.create_chat_btn = app._create_flat_button(
        app.status_actions,
        "✎",
        app._create_chat,
        width=3,
    )
    app.create_chat_btn.pack(side=tk.LEFT, padx=(4, 0))

    app.settings_menu = tk.Menu(app.root, tearoff=0)
    app.task_rows = []
    app.task_row_frames = []
    app.task_row_chat_ids = [None, None, None]
    for row_idx in range(3):
        row = tk.Frame(app.status_panel)
        row.pack(fill=tk.X, padx=12, pady=(0, 4))
        app.task_row_frames.append(row)
        row_title = tk.Label(
            row,
            anchor="w",
            relief=tk.FLAT,
            bd=0,
            highlightthickness=0,
            font=ui_font(11, "bold"),
        )
        row_title.pack(side=tk.LEFT, fill=tk.X, expand=True)
        row_title.bind("<Button-1>", lambda _event, idx=row_idx: app._open_chat_by_row(idx))
        app.flat_buttons.add(row_title)
        row_meta = tk.Label(row, anchor="e", width=6, font=ui_font(11, None))
        row_meta.pack(side=tk.RIGHT)
        app.task_rows.append((row_title, row_meta))

    app.view_all_btn = app._create_flat_button(
        app.status_panel,
        "Просмотреть все (0)",
        app._toggle_view_all_chats,
        anchor="w",
    )
    app.view_all_btn.pack(fill=tk.X, padx=12, pady=(2, 10))


def _build_connection_panel(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.connection_panel = tk.Frame(app.main, bd=0, relief=tk.FLAT, highlightthickness=1)
    app.connection_panel.pack(fill=tk.X, padx=14, pady=(0, 8))

    tk.Label(app.connection_panel, text="URL OpenWebUI", anchor="w").pack(fill=tk.X, padx=10, pady=(8, 3))
    app.url_entry = tk.Entry(app.connection_panel, textvariable=app.url_var)
    app.url_entry.pack(fill=tk.X, padx=10)

    conn_actions = tk.Frame(app.connection_panel)
    conn_actions.pack(fill=tk.X, padx=10, pady=(6, 8))
    app.connect_btn = tk.Button(conn_actions, text="Подключиться", command=app._connect_clicked)
    app.connect_btn.pack(side=tk.LEFT)

    app.theme_menu = tk.OptionMenu(conn_actions, app.theme_var, "agent-dark")
    app.theme_menu.pack(side=tk.LEFT)
    app.theme_menu.pack_forget()

    app.service_label = tk.Label(app.connection_panel, anchor="w", font=ui_font(10, None))
    app.auth_label = tk.Label(app.connection_panel, anchor="w", font=ui_font(10, None))
    app.service_label.pack(fill=tk.X, padx=10, pady=(0, 2))
    app.auth_label.pack(fill=tk.X, padx=10, pady=(0, 8))


def _build_auth_panel(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.auth_panel = tk.Frame(app.main, bd=0, relief=tk.FLAT, highlightthickness=1)
    app.auth_panel.pack(fill=tk.X, padx=14, pady=(0, 8))

    tk.Label(app.auth_panel, text="Требуется авторизация", font=ui_font(10, "bold")).pack(
        anchor="w",
        padx=10,
        pady=(8, 4),
    )
    tk.Label(app.auth_panel, text="Логин", anchor="w").pack(fill=tk.X, padx=10)
    app.username_entry = tk.Entry(app.auth_panel, textvariable=app.username_var)
    app.username_entry.pack(fill=tk.X, padx=10, pady=(0, 4))

    tk.Label(app.auth_panel, text="Пароль", anchor="w").pack(fill=tk.X, padx=10)
    app.password_entry = tk.Entry(app.auth_panel, textvariable=app.password_var, show="*")
    app.password_entry.pack(fill=tk.X, padx=10, pady=(0, 6))

    app.login_btn = tk.Button(app.auth_panel, text="Войти", command=app._login_clicked)
    app.login_btn.pack(anchor="w", padx=10, pady=(0, 8))


def _build_model_panel(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.model_panel = tk.Frame(app.main, bd=0, relief=tk.FLAT, highlightthickness=1)
    app.model_panel.pack(fill=tk.X, padx=14, pady=(0, 8))

    app.model_label = tk.Label(app.model_panel, anchor="w", font=ui_font(10, None))
    app.chat_label = tk.Label(app.model_panel, anchor="w", font=ui_font(10, None))
    app.model_label.pack(fill=tk.X, padx=10, pady=(8, 2))
    app.chat_label.pack(fill=tk.X, padx=10, pady=(0, 6))

    tk.Label(app.model_panel, text="Существующий чат", anchor="w").pack(fill=tk.X, padx=10, pady=(6, 3))
    app.chat_menu = tk.OptionMenu(app.model_panel, app.chat_var, "")
    app.chat_menu.pack(fill=tk.X, padx=10)

    model_actions = tk.Frame(app.model_panel)
    model_actions.pack(fill=tk.X, padx=10, pady=(6, 4))
    app.refresh_models_btn = tk.Button(
        model_actions,
        text="Обновить модели",
        command=app._refresh_models,
    )
    app.refresh_models_btn.pack(side=tk.LEFT)

    tk.Button(model_actions, text="Обновить чаты", command=app._refresh_chats).pack(side=tk.LEFT, padx=(6, 0))

    tk.Label(app.model_panel, text="Название чата", anchor="w").pack(fill=tk.X, padx=10)
    app.chat_title_entry = tk.Entry(app.model_panel, textvariable=app.chat_title_var)
    app.chat_title_entry.pack(fill=tk.X, padx=10, pady=(0, 6))

    tk.Button(app.model_panel, text="Создать чат", command=app._create_chat).pack(
        anchor="w",
        padx=10,
        pady=(0, 8),
    )


def _build_result_panel(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.result_panel = tk.Frame(app.main, bd=0, relief=tk.FLAT)
    app.result_panel.pack(fill=tk.BOTH, expand=True, padx=14, pady=(0, 10))

    app.result_text = scrolledtext.ScrolledText(
        app.result_panel,
        wrap=tk.WORD,
        height=20,
        relief=tk.FLAT,
        padx=10,
        pady=10,
        font=ui_font(10, None),
    )
    app.result_text.pack(fill=tk.BOTH, expand=True)
    app.result_text.configure(insertwidth=0, exportselection=False, state=tk.NORMAL, cursor="xterm")
    app.result_context_menu = tk.Menu(app.root, tearoff=0)
    app.result_context_menu.add_command(label="Копировать", command=app._copy_result_selection)
    app.result_context_menu.add_command(label="Выделить все", command=app._select_all_result_text)

    app.empty_state_label = tk.Label(
        app.result_panel,
        text="<_>",
        font=ui_font(26, "bold"),
    )
    app.empty_state_label.place(relx=0.5, rely=0.5, anchor="center")


def _build_pending_panel(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.pending_panel = tk.Frame(app.main, bd=0, relief=tk.FLAT, highlightthickness=1)
    pending_head = tk.Frame(app.pending_panel)
    pending_head.pack(fill=tk.X, padx=10, pady=(8, 6))
    app.pending_title_label = tk.Label(
        pending_head,
        text="Изменения",
        anchor="w",
        font=ui_font(10, "bold"),
    )
    app.pending_title_label.pack(side=tk.LEFT)
    app.pending_stats_label = tk.Label(
        pending_head,
        text="+0 -0",
        anchor="w",
        font=ui_font(10, "bold"),
    )
    app.pending_stats_label.pack(side=tk.LEFT, padx=(10, 0))
    pending_actions = tk.Frame(pending_head)
    pending_actions.pack(side=tk.RIGHT)
    app.reject_changes_btn = app._create_flat_button(
        pending_actions,
        "Отменить",
        app._discard_pending_changes_clicked,
    )
    app.reject_changes_btn.pack(side=tk.RIGHT)

    app.pending_body = tk.Frame(app.pending_panel)
    app.pending_body.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 10))
    app.pending_canvas = tk.Canvas(
        app.pending_body,
        highlightthickness=0,
        bd=0,
        relief=tk.FLAT,
    )
    app.pending_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
    app.pending_scrollbar = tk.Scrollbar(
        app.pending_body,
        orient=tk.VERTICAL,
        command=app.pending_canvas.yview,
    )
    app.pending_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
    app.pending_canvas.configure(yscrollcommand=app.pending_scrollbar.set)
    app.pending_rows = tk.Frame(app.pending_canvas)
    app.pending_canvas_window = app.pending_canvas.create_window(
        (0, 0),
        window=app.pending_rows,
        anchor="nw",
    )
    app.pending_rows.bind(
        "<Configure>",
        lambda _event: app.pending_canvas.configure(scrollregion=app.pending_canvas.bbox("all")),
    )
    app.pending_canvas.bind("<Configure>", app._on_pending_canvas_configure)


def _build_composer_panel(
    app: Any,
    ui_font: Callable[[int, str | None], tuple[str, int] | tuple[str, int, str]],
) -> None:
    app.composer_panel = tk.Frame(app.main, bd=0, relief=tk.FLAT, highlightthickness=1)
    app.composer_panel.pack(fill=tk.X, padx=14, pady=(0, 12))

    app.composer_hint = tk.Label(
        app.composer_panel,
        text=app.composer_hint_default,
        anchor="w",
        font=ui_font(10, "bold"),
    )
    app.composer_hint.pack(fill=tk.X, padx=12, pady=(10, 4))
    app.message_input = tk.Text(app.composer_panel, height=6, wrap=tk.WORD, relief=tk.FLAT)
    app.message_input.pack(fill=tk.X, padx=12)

    compose_actions = tk.Frame(app.composer_panel)
    compose_actions.pack(fill=tk.X, padx=12, pady=(8, 10))
    app.send_btn = app._create_flat_button(
        compose_actions,
        "↑",
        app._send_message,
        width=3,
    )
    app.send_btn.pack(side=tk.RIGHT)
