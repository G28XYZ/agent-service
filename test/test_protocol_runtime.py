from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_service.protocol_runtime import AgentProtocolRuntime
from agent_service.session_store import SessionStore


class FakeAgentRuntime:
    project_root = Path.cwd()

    async def run_agent_task(
        self,
        message: str,
        model_id: str | None = None,
        chat_id: str | None = None,
        *,
        auto_apply: bool = True,
        stream_callback: Any = None,
        tool_policy: Any = None,
    ) -> dict[str, Any]:
        policy_payload = (
            tool_policy("delete_file", {"path": "README.md"})
            if callable(tool_policy)
            else {"decision": "approve", "reason": "", "source": "default"}
        )
        denied = str(policy_payload.get("decision") or "").lower() == "deny"
        if stream_callback is not None:
            stream_callback({"type": "status", "text": "Шаг 1: запрос к модели"})
            stream_callback({"type": "tool_start", "name": "delete_file"})
            stream_callback(
                {
                    "type": "tool_result",
                    "name": "delete_file",
                    "ok": not denied,
                    "error": "denied by policy" if denied else "",
                    "policy": policy_payload,
                }
            )
            stream_callback({"type": "assistant_delta", "text": "Готово"})
        return {
            "chat_id": chat_id or "chat-1",
            "model_id": model_id or "model-x",
            "assistant_message": f"done: {message}",
            "applied_files": ["README.md"] if auto_apply else [],
            "pending_changes": [],
            "tool_steps": 1,
            "chat_title": "Task",
            "raw": {"ignored": True},
        }


class SlowFakeAgentRuntime:
    project_root = Path.cwd()

    async def run_agent_task(
        self,
        message: str,
        model_id: str | None = None,
        chat_id: str | None = None,
        *,
        auto_apply: bool = True,
        stream_callback: Any = None,
        tool_policy: Any = None,
    ) -> dict[str, Any]:
        del tool_policy
        if stream_callback is not None:
            stream_callback({"type": "status", "text": "waiting"})
        await asyncio.sleep(10)
        return {
            "chat_id": chat_id or "chat-slow",
            "model_id": model_id or "model-slow",
            "assistant_message": message,
            "applied_files": [],
            "pending_changes": [],
            "tool_steps": 0,
        }


class ProtocolRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_run_emits_cycle_phases(self) -> None:
        runtime = AgentProtocolRuntime(FakeAgentRuntime())  # type: ignore[arg-type]
        session = runtime.create_session(model_id="model-x")
        events: list[dict[str, Any]] = []

        run_id = await runtime.start_prompt(
            session_id=session["session_id"],
            message="прочитай README.md",
            auto_apply=True,
            on_event=lambda payload: events.append(payload),
        )
        run = await runtime.wait_run(run_id, timeout_seconds=1)

        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["result"]["assistant_message"], "done: прочитай README.md")
        refreshed_session = runtime.get_session(session["session_id"])
        self.assertEqual(refreshed_session["chat_id"], "chat-1")

        phases = {event.get("phase") for event in events}
        self.assertIn("plan", phases)
        self.assertIn("act", phases)
        self.assertIn("verify", phases)
        self.assertIn("final", phases)
        tool_events = [event for event in events if event.get("event") == "tool.result"]
        self.assertTrue(tool_events)
        self.assertEqual(tool_events[0]["tool"]["policy"]["decision"], "approve")

    async def test_cancel_active_run(self) -> None:
        runtime = AgentProtocolRuntime(SlowFakeAgentRuntime())  # type: ignore[arg-type]
        session = runtime.create_session(model_id="model-slow")

        run_id = await runtime.start_prompt(
            session_id=session["session_id"],
            message="long task",
            on_event=None,
        )
        await asyncio.sleep(0.05)
        cancel_result = runtime.cancel_run(session_id=session["session_id"], run_id=run_id)
        self.assertTrue(cancel_result["cancelled"])

        run = await runtime.wait_run(run_id, timeout_seconds=1)
        self.assertEqual(run["status"], "cancelled")

    async def test_tool_policy_deny_emits_tool_result_deny(self) -> None:
        runtime = AgentProtocolRuntime(FakeAgentRuntime())  # type: ignore[arg-type]
        session = runtime.create_session(model_id="model-x")
        events: list[dict[str, Any]] = []

        run_id = await runtime.start_prompt(
            session_id=session["session_id"],
            message="try delete",
            tool_policy={"deny_tools": ["delete_file"]},
            on_event=lambda payload: events.append(payload),
        )
        run = await runtime.wait_run(run_id, timeout_seconds=1)
        self.assertEqual(run["status"], "completed")
        tool_events = [event for event in events if event.get("event") == "tool.result"]
        self.assertTrue(tool_events)
        self.assertEqual(tool_events[0]["tool"]["policy"]["decision"], "deny")

    async def test_verify_commands_are_executed(self) -> None:
        runtime = AgentProtocolRuntime(FakeAgentRuntime())  # type: ignore[arg-type]
        session = runtime.create_session(model_id="model-x")
        run_id = await runtime.start_prompt(
            session_id=session["session_id"],
            message="verify",
            verify_commands=["echo protocol-verify-ok"],
        )
        run = await runtime.wait_run(run_id, timeout_seconds=2)
        verification = run["result"]["verification"]
        self.assertEqual(verification["workspace_summary"]["total"], 1)
        self.assertEqual(verification["workspace_summary"]["failed"], 0)

    async def test_sessions_and_runs_persist_between_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project_root = Path(tmp)
            store = SessionStore(project_root)
            runtime = AgentProtocolRuntime(
                FakeAgentRuntime(),  # type: ignore[arg-type]
                store=store,
            )
            session = runtime.create_session(model_id="model-x")
            run_id = await runtime.start_prompt(
                session_id=session["session_id"],
                message="persist me",
            )
            await runtime.wait_run(run_id, timeout_seconds=1)

            reloaded = AgentProtocolRuntime(
                FakeAgentRuntime(),  # type: ignore[arg-type]
                store=store,
            )
            loaded_session = reloaded.get_session(session["session_id"])
            loaded_run = reloaded.get_run(run_id)
            self.assertEqual(loaded_session["session_id"], session["session_id"])
            self.assertEqual(loaded_run["status"], "completed")


if __name__ == "__main__":
    unittest.main()
