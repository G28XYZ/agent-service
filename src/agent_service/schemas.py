from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str
    detail: str | None = None


class LoginRequest(BaseModel):
    username: str | None = None
    password: str | None = None


class LoginResponse(BaseModel):
    authenticated: bool
    username: str
    details: dict[str, Any] | None = None


class AuthStatusResponse(BaseModel):
    authenticated: bool
    details: dict[str, Any] | None = None


class ModelItem(BaseModel):
    id: str
    name: str
    raw: dict[str, Any] | None = None


class ModelsResponse(BaseModel):
    models: list[ModelItem]


class CreateChatRequest(BaseModel):
    model_id: str | None = None
    title: str | None = None


class CreateChatResponse(BaseModel):
    chat_id: str
    model_id: str
    created_at: str
    raw: dict[str, Any] | None = None


class DeleteChatResponse(BaseModel):
    chat_id: str
    deleted: bool
    raw: dict[str, Any] | None = None


class ChatItem(BaseModel):
    chat_id: str
    title: str | None = None
    model_id: str | None = None
    updated_at: str | None = None
    created_at: str | None = None
    raw: dict[str, Any] | None = None


class ChatsResponse(BaseModel):
    chats: list[ChatItem]


class MessageRequest(BaseModel):
    message: str
    model_id: str | None = None
    chat_id: str | None = None


class MessageResponse(BaseModel):
    chat_id: str | None = None
    model_id: str
    assistant_message: str
    raw: dict[str, Any] | None = None


class AgentTaskRequest(BaseModel):
    message: str
    model_id: str | None = None
    chat_id: str | None = None
    auto_apply: bool = True


class AgentTaskResponse(BaseModel):
    chat_id: str | None = None
    model_id: str
    assistant_message: str
    raw: dict[str, Any] | None = None
    applied_files: list[str] = Field(default_factory=list)
    pending_id: str | None = None
    pending_changes: list[dict[str, Any]] = Field(default_factory=list)
    tool_steps: int = 0
