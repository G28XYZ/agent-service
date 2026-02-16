from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import ensure_config_exists, load_config, resolve_config_path
from .openwebui_client import OpenWebUIClient
from .protocol_runtime import AgentProtocolRuntime, ProtocolRuntimeError
from .service import AgentRuntime
from .session_store import SessionStore

LOGGER = logging.getLogger(__name__)


class JsonRpcError(RuntimeError):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data or {}


class JsonRpcProtocolServer:
    def __init__(
        self,
        runtime: AgentProtocolRuntime,
        *,
        server_name: str = "agent-service-protocol",
        server_version: str = "0.1.0",
    ):
        self._runtime = runtime
        self._server_name = server_name
        self._server_version = server_version
        self._shutdown_requested = False
        self._write_lock = asyncio.Lock()
        self._handlers: Mapping[str, Callable[[dict[str, Any]], Any]] = {
            "initialize": self._handle_initialize,
            "ping": self._handle_ping,
            "session.create": self._handle_session_create,
            "session.list": self._handle_session_list,
            "session.get": self._handle_session_get,
            "session.update": self._handle_session_update,
            "session.resume": self._handle_session_get,
            "session.prompt": self._handle_session_prompt,
            "session.cancel": self._handle_session_cancel,
            "run.get": self._handle_run_get,
            "shutdown": self._handle_shutdown,
        }

    async def serve(self) -> None:
        while not self._shutdown_requested:
            raw = await asyncio.to_thread(sys.stdin.buffer.readline)
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                await self._send_error(
                    request_id=None,
                    code=-32700,
                    message="Parse error",
                    data={"raw": line[:300]},
                )
                continue

            await self._handle_request(request)

    async def _handle_request(self, request: Any) -> None:
        if not isinstance(request, dict):
            await self._send_error(
                request_id=None,
                code=-32600,
                message="Invalid Request",
                data={"reason": "request must be an object"},
            )
            return

        if request.get("jsonrpc") != "2.0":
            await self._send_error(
                request_id=request.get("id"),
                code=-32600,
                message="Invalid Request",
                data={"reason": "jsonrpc must be '2.0'"},
            )
            return

        method = request.get("method")
        if not isinstance(method, str) or not method.strip():
            await self._send_error(
                request_id=request.get("id"),
                code=-32600,
                message="Invalid Request",
                data={"reason": "method is required"},
            )
            return

        params = request.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            await self._send_error(
                request_id=request.get("id"),
                code=-32602,
                message="Invalid params",
                data={"reason": "params must be an object"},
            )
            return

        handler = self._handlers.get(method)
        if handler is None:
            await self._send_error(
                request_id=request.get("id"),
                code=-32601,
                message="Method not found",
                data={"method": method},
            )
            return

        has_request_id = "id" in request
        request_id = request.get("id")
        try:
            result = await self._call_handler(handler, params)
        except JsonRpcError as exc:
            if has_request_id:
                await self._send_error(
                    request_id=request_id,
                    code=exc.code,
                    message=str(exc),
                    data=exc.data,
                )
            return
        except ProtocolRuntimeError as exc:
            if has_request_id:
                await self._send_error(
                    request_id=request_id,
                    code=self._map_runtime_error_code(exc.code),
                    message=str(exc),
                    data={"error_code": exc.code, **exc.data},
                )
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("JSON-RPC handler failed: %s", method)
            if has_request_id:
                await self._send_error(
                    request_id=request_id,
                    code=-32603,
                    message="Internal error",
                    data={"reason": str(exc), "method": method},
                )
            return

        if has_request_id:
            await self._send_result(request_id=request_id, result=result)

    @staticmethod
    async def _call_handler(handler: Callable[[dict[str, Any]], Any], params: dict[str, Any]) -> Any:
        result = handler(params)
        if result is not None and hasattr(result, "__await__"):
            return await result
        return result

    async def _handle_initialize(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "server": {
                "name": self._server_name,
                "version": self._server_version,
            },
            "protocol": {
                "name": "agent-service-jsonrpc",
                "version": "0.1",
                "transport": "stdio-lines",
            },
            "capabilities": {
                "sessions": True,
                "stream_events": True,
                "cancel": True,
                "wait_for_run": True,
            },
            "methods": sorted(self._handlers.keys()),
        }

    async def _handle_ping(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "timestamp": _utc_now_iso()}

    async def _handle_session_create(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._runtime.create_session(
            model_id=_optional_str(params.get("model_id")),
            chat_id=_optional_str(params.get("chat_id")),
            metadata=_metadata_or_empty(params.get("metadata")),
        )

    async def _handle_session_list(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"sessions": self._runtime.list_sessions()}

    async def _handle_session_get(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._runtime.get_session(_required_str(params, "session_id"))

    async def _handle_session_update(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._runtime.update_session(
            _required_str(params, "session_id"),
            model_id=_nullable_optional_str(params, "model_id"),
            chat_id=_nullable_optional_str(params, "chat_id"),
            metadata=_nullable_metadata(params, "metadata"),
        )

    async def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_str(params, "session_id")
        message = _required_str(params, "message")
        auto_apply = bool(params.get("auto_apply", True))
        wait_result = bool(params.get("wait", False))
        timeout_seconds = _optional_float(params.get("timeout_seconds"))

        run_id = await self._runtime.start_prompt(
            session_id=session_id,
            message=message,
            auto_apply=auto_apply,
            on_event=self._emit_session_event,
        )
        payload: dict[str, Any] = {
            "accepted": True,
            "session_id": session_id,
            "run_id": run_id,
        }

        if wait_result:
            payload["run"] = await self._runtime.wait_run(run_id, timeout_seconds=timeout_seconds)
        return payload

    async def _handle_session_cancel(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = _required_str(params, "session_id")
        return self._runtime.cancel_run(
            session_id=session_id,
            run_id=_optional_str(params.get("run_id")),
        )

    async def _handle_run_get(self, params: dict[str, Any]) -> dict[str, Any]:
        run_id = _required_str(params, "run_id")
        return self._runtime.get_run(run_id)

    async def _handle_shutdown(self, _params: dict[str, Any]) -> dict[str, Any]:
        self._shutdown_requested = True
        return {"ok": True}

    async def _emit_session_event(self, payload: dict[str, Any]) -> None:
        await self._send_notification(method="session.event", params=payload)

    async def _send_result(self, *, request_id: Any, result: Any) -> None:
        await self._write_json_line(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": result,
            }
        )

    async def _send_error(
        self,
        *,
        request_id: Any,
        code: int,
        message: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        error_payload: dict[str, Any] = {"code": code, "message": message}
        if data:
            error_payload["data"] = data
        await self._write_json_line(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": error_payload,
            }
        )

    async def _send_notification(self, *, method: str, params: dict[str, Any]) -> None:
        await self._write_json_line(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params,
            }
        )

    async def _write_json_line(self, payload: dict[str, Any]) -> None:
        line = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
        async with self._write_lock:
            sys.stdout.write(f"{line}\n")
            sys.stdout.flush()

    @staticmethod
    def _map_runtime_error_code(error_code: str) -> int:
        mapping = {
            "invalid_params": -32602,
            "not_found": -32004,
            "timeout": -32008,
            "run_in_progress": -32009,
        }
        return mapping.get(error_code, -32000)


async def run_protocol_server(
    *,
    project_root: Path | None = None,
    config_path: Path | None = None,
) -> None:
    resolved_root = (project_root or Path.cwd()).resolve()
    store = SessionStore(resolved_root)

    if config_path is None:
        resolved_config_path = resolve_config_path(resolved_root)
    else:
        resolved_config_path = config_path.expanduser().resolve()
        os.environ["AGENT_SERVICE_CONFIG"] = str(resolved_config_path)

    created = ensure_config_exists(resolved_config_path)
    if created:
        LOGGER.info("Config created: %s", resolved_config_path)

    config = load_config(resolved_config_path)
    client = OpenWebUIClient(config, store)
    runtime = AgentRuntime(config, store, client)
    protocol_runtime = AgentProtocolRuntime(runtime)
    protocol_server = JsonRpcProtocolServer(protocol_runtime)

    await runtime.startup()
    try:
        await protocol_server.serve()
    finally:
        await runtime.shutdown()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run agent-service JSON-RPC protocol server over stdio.")
    parser.add_argument(
        "--project-root",
        default=".",
        help="Project root used for .agent-service storage (default: current directory).",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Optional path to config.yaml (default: <project-root>/.agent-service/config.yaml).",
    )
    parser.add_argument(
        "--log-level",
        default="WARNING",
        help="Log level for stderr output (DEBUG, INFO, WARNING, ERROR).",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.WARNING),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        asyncio.run(
            run_protocol_server(
                project_root=Path(args.project_root),
                config_path=Path(args.config) if str(args.config).strip() else None,
            )
        )
    except KeyboardInterrupt:
        return 0
    return 0


def _required_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    text = str(value or "").strip()
    if not text:
        raise JsonRpcError(-32602, "Invalid params", data={"reason": f"{key} is required"})
    return text


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _nullable_optional_str(params: dict[str, Any], key: str) -> str | None:
    if key not in params:
        return None
    value = params.get(key)
    if value is None:
        return ""
    return _optional_str(value)


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        resolved = float(value)
    except (TypeError, ValueError) as exc:
        raise JsonRpcError(-32602, "Invalid params", data={"reason": "timeout_seconds must be number"}) from exc
    if resolved <= 0:
        raise JsonRpcError(-32602, "Invalid params", data={"reason": "timeout_seconds must be > 0"})
    return resolved


def _metadata_or_empty(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JsonRpcError(-32602, "Invalid params", data={"reason": "metadata must be object"})
    return dict(value)


def _nullable_metadata(params: dict[str, Any], key: str) -> dict[str, Any] | None:
    if key not in params:
        return None
    value = params.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise JsonRpcError(-32602, "Invalid params", data={"reason": f"{key} must be object"})
    return dict(value)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    raise SystemExit(main())
