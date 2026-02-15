from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request

from .config import ensure_config_exists, load_config, resolve_config_path
from .openwebui_client import (
    AuthenticationError,
    ModelNotFoundError,
    OpenWebUIClient,
    RequestFailedError,
)
from .schemas import (
    AgentTaskRequest,
    AgentTaskResponse,
    AuthStatusResponse,
    ChatItem,
    ChatsResponse,
    CreateChatRequest,
    CreateChatResponse,
    DeleteChatResponse,
    HealthResponse,
    LoginRequest,
    LoginResponse,
    MessageRequest,
    MessageResponse,
    ModelItem,
    ModelsResponse,
)
from .service import AgentRuntime
from .session_store import SessionStore

LOGGER = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Service", version="0.1.0")

    app.state.runtime = None
    app.state.startup_error = None

    @app.on_event("startup")
    async def on_startup() -> None:
        project_root = Path.cwd()
        store = SessionStore(project_root)
        _configure_logging(store.log_path)
        config_path = resolve_config_path(project_root)

        if ensure_config_exists(config_path):
            app.state.startup_error = (
                f"Config created at {config_path}. Fill it and restart the service."
            )
            LOGGER.warning(app.state.startup_error)
            return

        try:
            config = load_config(config_path)
            client = OpenWebUIClient(config, store)
            runtime = AgentRuntime(config, store, client)
            await runtime.startup()
        except Exception as exc:  # noqa: BLE001
            app.state.startup_error = f"Failed to initialize service: {exc}"
            LOGGER.exception("Service startup failed")
            return

        app.state.runtime = runtime
        app.state.startup_error = None

    @app.on_event("shutdown")
    async def on_shutdown() -> None:
        runtime = app.state.runtime
        if runtime is not None:
            await runtime.shutdown()

    @app.get("/health", response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        if request.app.state.startup_error:
            return HealthResponse(status="degraded", detail=request.app.state.startup_error)
        return HealthResponse(status="ok")

    @app.post("/auth/login", response_model=LoginResponse)
    async def login(
        request: Request,
        body: LoginRequest = Body(default_factory=LoginRequest),
    ) -> LoginResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.login(body.username, body.password)
            return LoginResponse(**result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    @app.get("/auth/status", response_model=AuthStatusResponse)
    async def auth_status(request: Request) -> AuthStatusResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.auth_status()
            return AuthStatusResponse(**result)
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    @app.get("/models", response_model=ModelsResponse)
    async def models(request: Request) -> ModelsResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.list_models()
            items = [ModelItem(**model) for model in result]
            return ModelsResponse(models=items)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    @app.get("/chats", response_model=ChatsResponse)
    async def chats(request: Request) -> ChatsResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.list_chats()
            items = [ChatItem(**chat) for chat in result]
            return ChatsResponse(chats=items)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    @app.post("/chats", response_model=CreateChatResponse)
    async def create_chat(
        request: Request,
        body: CreateChatRequest = Body(default_factory=CreateChatRequest),
    ) -> CreateChatResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.create_chat(body.model_id, body.title)
            return CreateChatResponse(**result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ModelNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    @app.delete("/chats/{chat_id}", response_model=DeleteChatResponse)
    async def delete_chat(request: Request, chat_id: str) -> DeleteChatResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.delete_chat(chat_id)
            return DeleteChatResponse(**result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    @app.post("/messages", response_model=MessageResponse)
    async def send_message(
        request: Request,
        body: MessageRequest,
    ) -> MessageResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.send_message(
                message=body.message,
                model_id=body.model_id,
                chat_id=body.chat_id,
            )
            return MessageResponse(**result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ModelNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    @app.post("/agent/tasks", response_model=AgentTaskResponse)
    async def run_agent_task(
        request: Request,
        body: AgentTaskRequest,
    ) -> AgentTaskResponse:
        runtime = _get_runtime_or_503(request)

        try:
            result = await runtime.run_agent_task(
                message=body.message,
                model_id=body.model_id,
                chat_id=body.chat_id,
                auto_apply=body.auto_apply,
            )
            return AgentTaskResponse(**result)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ModelNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        except RequestFailedError as exc:
            raise HTTPException(status_code=502, detail=_request_error_detail(exc)) from exc

    return app


def _get_runtime_or_503(request: Request) -> AgentRuntime:
    startup_error = request.app.state.startup_error
    if startup_error:
        raise HTTPException(status_code=503, detail=startup_error)

    runtime = request.app.state.runtime
    if runtime is None:
        raise HTTPException(status_code=503, detail="Service runtime is not available")

    return runtime


def _request_error_detail(exc: RequestFailedError) -> str:
    if exc.status_code is None:
        return str(exc)
    return f"{exc} (upstream_status={exc.status_code})"


def _configure_logging(log_path: Path) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(message)s")

    stream_exists = any(
        isinstance(handler, logging.StreamHandler)
        and not isinstance(handler, logging.FileHandler)
        for handler in root_logger.handlers
    )
    if not stream_exists:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    file_exists = any(
        isinstance(handler, logging.FileHandler)
        and Path(getattr(handler, "baseFilename", "")) == log_path
        for handler in root_logger.handlers
    )
    if not file_exists:
        file_handler = logging.FileHandler(log_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)
