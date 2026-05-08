import asyncio
import dataclasses
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, AsyncGenerator, Dict, List, Optional, Tuple

import httpx
import pystray
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from PIL import Image, ImageDraw


APP_NAME = "Codex AnyRouter 转发器"
APP_VERSION = "1.1.0"
APP_DIR = Path(os.getenv("APPDATA", str(Path.home()))) / "codex-anyroute"
CONFIG_PATH = APP_DIR / "config.json"
LOG_DIR = APP_DIR / "logs"
DEFAULT_GATEWAY_KEY = "codex-anyroute-local"
DEFAULT_LISTEN_PORT = 18180
SINGLE_INSTANCE_MUTEX_NAME = "Local\\CodexAnyRouteTransfer"
_single_instance_mutex: Optional[int] = None
# Keep the out-of-box Codex path on AnyRouter's native Responses endpoint. The
# Claude Opus 4.7 path needs the explicit [1m] alias on this relay and can be
# rate-limited separately, so it stays opt-in instead of being the default.
DEFAULT_UPSTREAM_MODEL = "gpt-5.5"
DEFAULT_FALLBACK_MODEL = "gpt-5.3-codex"
DEFAULT_THIRD_MODEL = "claude-opus-4-7[1m]"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_BETA_1M = "context-1m-2025-08-07"
CLAUDE_CODE_BETA = (
    "claude-code-20250219,"
    "interleaved-thinking-2025-05-14,"
    "context-management-2025-06-27,"
    "prompt-caching-scope-2026-01-05,"
    "context-1m-2025-08-07,"
    "advisor-tool-2026-03-01,"
    "effort-2025-11-24"
)
RETRYABLE_UPSTREAM_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
PANIC_STATUSES = {500, 502, 503, 504, 520, 521, 522, 523, 524}
UPSTREAM_RETRY_ATTEMPTS = 3
UPSTREAM_RETRY_BASE_DELAY = 1.5
MIN_RESPONSES_OUTPUT_TOKENS = 4096
# Models that AnyRouter serves natively on /v1/responses. For these we just
# pass through the Codex request — no translation needed. The pattern is
# anchored so unrelated names that happen to start with a single letter (e.g.
# a hypothetical "o1xxx-other-vendor") don't accidentally match: an "o" name
# must be a single digit followed by a dash, end-of-string, or a digit-suffix
# variant we know about (o1-mini, o3-pro, o4-mini, ...).
PASSTHROUGH_MODELS_PATTERN = re.compile(r"^(gpt-|o[1-9](?:$|-|\d))")


def ensure_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def now() -> str:
    return time.strftime("%H:%M:%S")


def acquire_single_instance() -> bool:
    global _single_instance_mutex
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel32.CloseHandle.restype = ctypes.c_bool
        handle = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_MUTEX_NAME)
        if not handle:
            return True
        if ctypes.get_last_error() == 183:
            if os.getenv("CODEX_ANYROUTE_SUPPRESS_DUPLICATE_MESSAGE") != "1":
                try:
                    root = tk.Tk()
                    root.withdraw()
                    messagebox.showwarning(APP_NAME, "软件已经在后台运行了，请不要重复启动。")
                    root.destroy()
                finally:
                    kernel32.CloseHandle(handle)
            else:
                kernel32.CloseHandle(handle)
            return False
        _single_instance_mutex = handle
    except Exception:
        return True
    return True


@dataclass
class AppConfig:
    api_base_url: str = "https://anyrouter.top"
    api_key: str = ""
    listen_port: int = DEFAULT_LISTEN_PORT
    gateway_key: str = DEFAULT_GATEWAY_KEY
    codex_auto_apply: bool = True
    default_model: str = DEFAULT_UPSTREAM_MODEL
    fallback_model: str = DEFAULT_FALLBACK_MODEL
    third_model: str = DEFAULT_THIRD_MODEL
    enable_fallback: bool = True
    model_map: Dict[str, str] = field(
        default_factory=lambda: {
            "gpt-5.5": DEFAULT_UPSTREAM_MODEL,
            "gpt-5.4": DEFAULT_UPSTREAM_MODEL,
            "gpt-5.4-mini": DEFAULT_UPSTREAM_MODEL,
            "gpt-5.3-codex": DEFAULT_UPSTREAM_MODEL,
            "gpt-5.2": DEFAULT_UPSTREAM_MODEL,
        }
    )

    @classmethod
    def load(cls) -> "AppConfig":
        ensure_dirs()
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = cls()
            for key, value in data.items():
                if hasattr(cfg, key):
                    setattr(cfg, key, value)
            if not cfg.gateway_key:
                cfg.gateway_key = DEFAULT_GATEWAY_KEY
            # Early builds used 18080, the same default as Codex App Transfer.
            # Move untouched configs away from that shared port so the tools can coexist.
            if data.get("listen_port") == 18080 and not data.get("allow_shared_18080"):
                cfg.listen_port = DEFAULT_LISTEN_PORT
            # Migrate older configs and legacy [1m] aliases back to AnyRouter's
            # real model id. The 1M switch is represented by headers.
            unstable_defaults = {
                "claude-opus-4-7-thinking",
                "claude-sonnet-4-5",
                "claude-sonnet-4-5-20250929",
                "gpt5.5",
                "",
            }
            if cfg.default_model in unstable_defaults:
                cfg.default_model = DEFAULT_UPSTREAM_MODEL
            if not cfg.fallback_model:
                cfg.fallback_model = DEFAULT_FALLBACK_MODEL
            if not cfg.third_model:
                cfg.third_model = DEFAULT_THIRD_MODEL
            new_map = {}
            for k, v in (cfg.model_map or {}).items():
                if v in unstable_defaults or not v:
                    new_map[k] = DEFAULT_UPSTREAM_MODEL
                else:
                    new_map[k] = v
            cfg.model_map = new_map or cls().model_map
            return cfg
        except Exception:
            return cls()

    def save(self) -> None:
        ensure_dirs()
        CONFIG_PATH.write_text(
            json.dumps(self.__dict__, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


class LogBus:
    """
    Three-way fan-out for log lines: GUI (Tk drains ``ui_queue`` every 300 ms),
    disk (a background daemon flushes ``disk_queue`` to today's log file), and
    nothing else — we deliberately keep network/telemetry out.

    Disk IO used to happen synchronously inside ``write()`` which is called
    from the asyncio event loop on every SSE event. That blocked the loop on
    each log line. Now ``write()`` is non-blocking: it only enqueues, and the
    daemon thread does the file IO. Queues are bounded to keep memory flat
    when the GUI is paused or the disk is slow.
    """

    UI_QUEUE_LIMIT = 5000
    DISK_QUEUE_LIMIT = 5000

    def __init__(self) -> None:
        self.ui_queue: "queue.Queue[str]" = queue.Queue(maxsize=self.UI_QUEUE_LIMIT)
        self._disk_queue: "queue.Queue[Optional[str]]" = queue.Queue(maxsize=self.DISK_QUEUE_LIMIT)
        self._writer_thread = threading.Thread(target=self._writer_loop, daemon=True, name="codex-anyroute-log")
        self._writer_thread.start()

    # Back-compat alias for callers using ``log_bus.lines.get_nowait()``.
    @property
    def lines(self) -> "queue.Queue[str]":
        return self.ui_queue

    def write(self, level: str, message: str) -> None:
        line = f"{now()}  {level:<7} {message}"
        # GUI queue: drop oldest if full so the newest line still shows.
        try:
            self.ui_queue.put_nowait(line)
        except queue.Full:
            try:
                self.ui_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self.ui_queue.put_nowait(line)
            except queue.Full:
                pass
        # Disk queue: same drop-oldest policy.
        try:
            self._disk_queue.put_nowait(line)
        except queue.Full:
            try:
                self._disk_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._disk_queue.put_nowait(line)
            except queue.Full:
                pass

    def _writer_loop(self) -> None:
        """Flush disk_queue → today's log file. Reopens daily so the path
        rolls over at midnight without a restart."""
        current_date = ""
        handle = None
        try:
            while True:
                item = self._disk_queue.get()
                if item is None:
                    break
                today = time.strftime("%Y-%m-%d")
                if today != current_date or handle is None:
                    if handle is not None:
                        try:
                            handle.close()
                        except Exception:
                            pass
                    try:
                        ensure_dirs()
                        path = LOG_DIR / f"proxy-{today}.log"
                        handle = path.open("a", encoding="utf-8")
                        current_date = today
                    except Exception:
                        handle = None
                        current_date = today
                if handle is not None:
                    try:
                        handle.write(item + "\n")
                        handle.flush()
                    except Exception:
                        try:
                            handle.close()
                        except Exception:
                            pass
                        handle = None
        finally:
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass


log_bus = LogBus()
config_lock = threading.Lock()
server_holder: Dict[str, Any] = {"server": None, "thread": None, "guard_stop": None, "guard_thread": None}


def normalize_base_url(raw: str) -> str:
    value = (raw or "").strip().rstrip("/")
    if value.endswith("/v1"):
        value = value[:-3].rstrip("/")
    return value or "https://anyrouter.top"


def anthropic_messages_url(cfg: AppConfig, model: str = "") -> str:
    suffix = "?beta=true" if model_needs_1m(model) else ""
    return f"{normalize_base_url(cfg.api_base_url)}/v1/messages{suffix}"


def upstream_responses_url(cfg: AppConfig) -> str:
    return f"{normalize_base_url(cfg.api_base_url)}/v1/responses"


def is_passthrough_model(model: str) -> bool:
    m = (model or "").lower().strip()
    if not m:
        return False
    return PASSTHROUGH_MODELS_PATTERN.match(m) is not None


def sanitize_responses_body_for_anyrouter(body: Dict[str, Any], *, drop_all_tools: bool = False) -> Tuple[Dict[str, Any], str]:
    """
    AnyRouter's /v1/responses accepts the native text stream, but its OpenAI
    compatibility layer is stricter about Codex App tool descriptors. In
    particular, hosted/MCP-style tool entries can trigger errors such as
    `Missing required parameter: tools[14].tools`. Keep plain function tools
    when possible and drop provider-specific descriptors that the relay cannot
    execute anyway.
    """
    out = strip_unverifiable_encrypted_content(dict(body))
    tools = out.get("tools")
    if not isinstance(tools, list):
        return out, "no_tools"

    if drop_all_tools:
        out.pop("tools", None)
        out.pop("tool_choice", None)
        out.pop("parallel_tool_calls", None)
        return out, f"dropped_all_{len(tools)}"

    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for idx, tool in enumerate(tools):
        if not isinstance(tool, dict):
            dropped.append(f"{idx}:non_object")
            continue

        tool_type = str(tool.get("type") or "")
        if tool_type == "function":
            # Responses API function-tool shape.
            if "name" in tool:
                cleaned: Dict[str, Any] = {
                    "type": "function",
                    "name": str(tool.get("name") or "tool"),
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("parameters") if isinstance(tool.get("parameters"), dict) else {"type": "object", "properties": {}},
                }
                if "strict" in tool:
                    cleaned["strict"] = bool(tool.get("strict"))
                kept.append(cleaned)
                continue

            # Chat-completions function-tool shape sometimes appears in
            # compatibility layers; normalize it for Responses.
            fn = tool.get("function")
            if isinstance(fn, dict) and fn.get("name"):
                cleaned = {
                    "type": "function",
                    "name": str(fn.get("name") or "tool"),
                    "description": str(fn.get("description") or ""),
                    "parameters": fn.get("parameters") if isinstance(fn.get("parameters"), dict) else {"type": "object", "properties": {}},
                }
                kept.append(cleaned)
                continue

        fn_name = ""
        if isinstance(tool.get("function"), dict):
            fn_name = str(tool["function"].get("name") or "")
        name = str(tool.get("name") or tool.get("server_label") or fn_name)
        dropped.append(f"{idx}:{tool_type or 'unknown'}:{name}")

    if kept:
        out["tools"] = kept
        choice = out.get("tool_choice")
        if isinstance(choice, dict):
            name = str(choice.get("name") or "")
            fn_choice = choice.get("function")
            if not name and isinstance(fn_choice, dict):
                name = str(fn_choice.get("name") or "")
            kept_names = {str(tool.get("name") or "") for tool in kept}
            if name and name not in kept_names:
                out["tool_choice"] = "auto"
        return out, f"kept_{len(kept)}_dropped_{len(dropped)}" + (f" dropped={';'.join(dropped[:8])}" if dropped else "")

    out.pop("tools", None)
    out.pop("tool_choice", None)
    out.pop("parallel_tool_calls", None)
    return out, f"dropped_all_{len(tools)}" + (f" dropped={';'.join(dropped[:8])}" if dropped else "")


def anyrouter_tool_schema_error(message: str) -> bool:
    """
    Return True if the upstream error looks like a tools-schema rejection from
    AnyRouter's compatibility layer. We use this to decide whether to retry
    the same model with tools stripped. Patterns are intentionally specific:
    "tools[" / "tool_choice" name the field, while the looser
    `<keyword> + tool` checks require a parameter-shaped phrasing so that
    plain model-availability errors that happen to mention a tool don't
    accidentally trigger a redundant retry.
    """
    raw = message or ""
    low = raw.lower()
    if "tools[" in raw or "tool_choice" in low:
        return True
    return (
        ("missing required parameter" in low and "tool" in low)
        or ("unknown parameter" in low and "tool" in low)
        or ("invalid parameter" in low and "tool" in low)
        or ("invalid value for" in low and "tool" in low)
    )


def strip_unverifiable_encrypted_content(value: Any) -> Any:
    """
    AnyRouter can echo OpenAI-style encrypted reasoning blobs, but Codex cannot
    verify those relay-generated blobs on the next turn. Remove only the
    unverifiable encrypted payload while preserving normal Responses events,
    reasoning summaries, text deltas, and function calls.
    """
    if isinstance(value, dict):
        cleaned: Dict[str, Any] = {}
        for key, item in value.items():
            if key in {"encrypted_content", "encrypted_reasoning_content"}:
                continue
            cleaned[key] = strip_unverifiable_encrypted_content(item)
        return cleaned
    if isinstance(value, list):
        return [strip_unverifiable_encrypted_content(item) for item in value]
    return value


def append_model_attempt(attempts: List[str], model: str) -> None:
    raw = (model or "").strip()
    if not raw:
        return
    normalized = normalize_anthropic_model(raw)
    if normalized and normalized not in [normalize_anthropic_model(existing) for existing in attempts]:
        attempts.append(raw)


def upstream_retry_delay(attempt: int, status_code: int = 0) -> float:
    base = UPSTREAM_RETRY_BASE_DELAY * max(1, attempt)
    if status_code in {429, 520, 521, 522, 523, 524}:
        base *= 1.6
    return min(base, 8.0)


config = AppConfig.load()


def check_gateway_auth(request: Request, cfg: AppConfig) -> bool:
    """
    Verify the local gateway key. Fail-closed: when ``gateway_key`` is set,
    the request MUST present a matching ``Authorization: Bearer <key>``
    header. Missing or non-bearer auth is rejected so other local processes
    can't reach AnyRouter through 127.0.0.1 without the configured token.
    """
    if not cfg.gateway_key:
        return True
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return False
    return auth.split(" ", 1)[1] == cfg.gateway_key


def sse(event: str, data: Dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def parse_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") in ("input_text", "text", "output_text"):
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "image_url":
                    parts.append("[image omitted]")
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content) if content is not None else ""


def responses_input_to_messages(body: Dict[str, Any]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    system_parts: List[str] = []
    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        system_parts.append(instructions.strip())

    raw_input = body.get("input", "")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            role = item.get("role", "user")
            if item_type == "message":
                content = parse_text_content(item.get("content"))
                if content:
                    messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})
            elif item_type == "function_call":
                name = item.get("name", "tool")
                args = item.get("arguments", "")
                messages.append({"role": "assistant", "content": f"[tool call: {name}]\n{args}"})
            elif item_type == "function_call_output":
                output = item.get("output", "")
                messages.append({"role": "user", "content": f"[tool result]\n{output}"})
            elif item_type == "reasoning":
                continue

    if not messages:
        messages.append({"role": "user", "content": ""})

    # Anthropic requires alternating user/assistant-ish messages. Merge adjacent same-role messages.
    merged: List[Dict[str, Any]] = []
    for msg in messages:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
        else:
            merged.append(msg)
    return ("\n\n".join(system_parts) if system_parts else None), merged


def convert_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    if not isinstance(tools, list):
        return None
    converted: List[Dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function":
            fn = tool.get("function", {})
            converted.append(
                {
                    "name": fn.get("name", "tool"),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                }
            )
    return converted or None


def claude_code_compat_tools() -> List[Dict[str, Any]]:
    # AnyRouter's Opus 4.7 1M route follows Claude Code's SDK branch. These
    # light tool declarations are enough to select that branch without binding
    # the proxy to Claude Code's local tool runtime.
    return [
        {"name": "Bash", "description": "execute shell commands", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "Edit", "description": "modify file contents in place", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "PowerShell", "description": "execute Windows PowerShell commands", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "Read", "description": "read files, images, PDFs, notebooks", "input_schema": {"type": "object", "properties": {}, "additionalProperties": False}},
    ]


def text_block(value: str, cache: bool = False) -> Dict[str, Any]:
    block: Dict[str, Any] = {"type": "text", "text": value}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def apply_claude_code_compat(out: Dict[str, Any], model: str, converted_tools: Optional[List[Dict[str, Any]]]) -> None:
    if not model_needs_1m(model):
        if converted_tools:
            out["tools"] = converted_tools
        return

    out["max_tokens"] = max(int(out.get("max_tokens") or 0), 64000)
    out["thinking"] = {"type": "adaptive"}
    out["output_config"] = {"effort": "xhigh"}
    out["context_management"] = {"edits": [{"type": "clear_thinking_20251015", "keep": "all"}]}
    out["metadata"] = {
        "user_id": json.dumps(
            {
                "device_id": "codex-anyroute-transfer",
                "account_uuid": "",
                "session_id": str(uuid.uuid4()),
            },
            ensure_ascii=False,
        )
    }

    for msg in out.get("messages") or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            msg["content"] = [text_block(msg["content"], cache=True)]

    existing_system = out.get("system")
    system_blocks = [
        text_block("x-anthropic-billing-header: cc_version=2.1.126.d1f; cc_entrypoint=sdk-cli; cch=04352;"),
        text_block("You are a Claude agent, built on Anthropic's Claude Agent SDK.", cache=True),
        text_block(f"CWD: {os.getcwd()}\nDate: {time.strftime('%Y-%m-%d')}", cache=True),
    ]
    if isinstance(existing_system, str) and existing_system.strip():
        system_blocks.append(text_block(existing_system.strip(), cache=True))
    elif isinstance(existing_system, list):
        system_blocks.extend(existing_system)
    out["system"] = system_blocks

    merged_tools: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for tool in claude_code_compat_tools() + (converted_tools or []):
        name = str(tool.get("name") or "")
        if name and name not in seen:
            merged_tools.append(tool)
            seen.add(name)
    out["tools"] = merged_tools


def resolve_upstream_model(requested_model: str, cfg: AppConfig) -> Tuple[str, str]:
    raw_model = str(cfg.model_map.get(requested_model) or cfg.default_model or requested_model)
    return raw_model, normalize_anthropic_model(raw_model)


def responses_to_anthropic_for_model(body: Dict[str, Any], raw_upstream_model: str, stream: bool) -> Dict[str, Any]:
    upstream_model = normalize_anthropic_model(raw_upstream_model)
    system, messages = responses_input_to_messages(body)
    max_tokens = body.get("max_output_tokens") or body.get("max_tokens") or 4096
    out: Dict[str, Any] = {
        "model": upstream_model,
        "messages": messages,
        "max_tokens": int(max_tokens) if isinstance(max_tokens, (int, float, str)) and str(max_tokens).isdigit() else 4096,
        "stream": stream,
    }
    if system:
        out["system"] = system
    # Opus 4.7 rejects non-default sampling parameters; keep the wire clean.
    for key in ("stop_sequences",):
        if key in body:
            out[key] = body[key]
    tools = convert_tools(body.get("tools"))
    apply_claude_code_compat(out, raw_upstream_model, tools)
    return out


def responses_to_anthropic(body: Dict[str, Any], cfg: AppConfig, stream: bool) -> Dict[str, Any]:
    requested_model = str(body.get("model") or "default")
    raw_upstream_model, _ = resolve_upstream_model(requested_model, cfg)
    return responses_to_anthropic_for_model(body, raw_upstream_model, stream)


def normalize_anthropic_model(model: str) -> str:
    raw = (model or "").strip()
    if raw.lower() == "claude-opus-4-7[1m]":
        return "claude-opus-4-7"
    return raw


def model_needs_1m(model: str) -> bool:
    raw = (model or "").lower().strip()
    return "[1m]" in raw


def auth_headers(cfg: AppConfig, model: str = "") -> Dict[str, str]:
    headers: Dict[str, str] = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "authorization": f"Bearer {cfg.api_key}",
        "anthropic-version": ANTHROPIC_VERSION,
    }
    # Only attach the 1M beta header when the model actually opts into it.
    # AnyRouter's model id stays claude-opus-4-7; the 1M flag is carried here.
    if model_needs_1m(model):
        headers["anthropic-beta"] = CLAUDE_CODE_BETA
        headers["x-api-key"] = cfg.api_key
        headers["x-app"] = "cli"
        headers["user-agent"] = "claude-cli/2.1.126 (external, sdk-cli)"
    return headers


def friendly_upstream_error(status_code: int, raw: str) -> str:
    text = (raw or "").strip()
    parsed = ""
    try:
        data = json.loads(text)
        error = data.get("error", data) if isinstance(data, dict) else data
        if isinstance(error, dict):
            parsed = str(error.get("message") or error.get("error") or "")
        elif isinstance(error, str):
            parsed = error
    except Exception:
        parsed = ""
    msg = parsed or text
    low = (msg + " " + (raw or "")).lower()
    if "1m" in low and ("context" in low or "上下文" in (raw or "")):
        return "AnyRouter 提示未启用 1M 上下文。本软件已发送 claude-opus-4-7 和 1M beta header；如果仍出现该错误，请检查 AnyRouter 控制台是否已开启 1M。"
    if "new_api_panic" in low or "panic detected" in low or "nil pointer" in low:
        return "AnyRouter/new-api 在 Claude 1M 路径返回后端 panic。若已启用模型优先级，本软件会继续尝试你设置的下一个模型。"
    if "service unavailable" in low or status_code in {503, 520, 521, 522, 523, 524}:
        return f"AnyRouter 返回 {status_code or '上游'} Service Unavailable，通常是上游瞬断、Cloudflare 520 类错误或高峰限流。本软件会自动重试，仍失败时继续切到 gpt-5.5 兜底。"
    if status_code == 429:
        return "AnyRouter 返回 429，表示当前请求被限流，请稍后重试。"
    if status_code in (401, 403):
        return "AnyRouter 拒绝了 API Key，请检查密钥是否正确。"
    return msg or f"上游 HTTP {status_code}"


def extract_anthropic_delta(event: Optional[str], data: Dict[str, Any]) -> Tuple[str, str]:
    delta = data.get("delta") if isinstance(data, dict) else {}
    if isinstance(delta, dict):
        if delta.get("type") == "text_delta":
            return "text", str(delta.get("text", ""))
        if delta.get("type") == "thinking_delta":
            return "thinking", str(delta.get("thinking", ""))
    if data.get("type") == "content_block_delta" and isinstance(delta, dict):
        return "text", str(delta.get("text") or delta.get("thinking") or "")
    if event == "content_block_delta" and isinstance(delta, dict):
        return "text", str(delta.get("text") or delta.get("thinking") or "")
    return "", ""


async def iter_anthropic_sse(resp: httpx.Response) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    event_name: Optional[str] = None
    data_lines: List[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                try:
                    yield event_name, json.loads(raw)
                except Exception:
                    yield event_name, {"raw": raw}
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


async def iter_sse_json(resp: httpx.Response) -> AsyncGenerator[Tuple[Optional[str], Dict[str, Any]], None]:
    event_name: Optional[str] = None
    data_lines: List[str] = []
    async for line in resp.aiter_lines():
        if line == "":
            if data_lines:
                raw = "\n".join(data_lines)
                if raw == "[DONE]":
                    yield event_name, {"type": "done"}
                else:
                    try:
                        yield event_name, json.loads(raw)
                    except Exception:
                        yield event_name, {"raw": raw}
            event_name = None
            data_lines = []
            continue
        if line.startswith("event:"):
            event_name = line[6:].strip()
        elif line.startswith("data:"):
            data_lines.append(line[5:].strip())


def extract_responses_delta(event: Optional[str], data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    data_type = str(data.get("type") or event or "")
    if data_type == "response.output_text.delta":
        return str(data.get("delta") or "")
    if data_type == "response.output_text.done":
        return ""
    if isinstance(data.get("delta"), str):
        return str(data.get("delta"))
    return ""


async def stream_responses(body: Dict[str, Any], cfg: AppConfig) -> AsyncGenerator[bytes, None]:
    response_id = f"resp_anyroute_{int(time.time() * 1000)}"
    model = str(body.get("model") or "gpt-5.4")
    seq = 0
    output_index = 0
    content_index = 0
    item_id = f"msg_{int(time.time() * 1000)}"
    text_open = False
    final_text = ""

    def base_response(status: str = "in_progress") -> Dict[str, Any]:
        return {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": status,
            "model": model,
            "output": [],
        }

    yield sse("response.created", {"type": "response.created", "sequence_number": seq, "response": base_response()})
    seq += 1
    yield sse(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "sequence_number": seq,
            "output_index": output_index,
            "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
        },
    )
    seq += 1

    requested_model = str(body.get("model") or "default")
    primary_raw_model, primary_model = resolve_upstream_model(requested_model, cfg)
    if is_passthrough_model(primary_model):
        log_bus.write("INFO", f"Responses 直通：{body.get('model')} -> {primary_model}")
        log_bus.write("INFO", f"Responses 请求地址：{upstream_responses_url(cfg)}")
    else:
        log_bus.write("INFO", f"Responses 转 Anthropic：{body.get('model')} -> {primary_model}")
        log_bus.write("INFO", f"Claude 请求地址：{anthropic_messages_url(cfg, primary_raw_model)}")

    # Build the user-configured model priority list.
    model_attempts: List[str] = [primary_raw_model]
    if cfg.enable_fallback:
        for candidate in (cfg.fallback_model, cfg.third_model):
            append_model_attempt(model_attempts, candidate)
    # Hard safety net: Claude 1M is useful when the relay accepts it, but if
    # that branch returns 429/503 we still want Codex to answer instead of
    # spinning forever. Keep AnyRouter gpt-5.5 as the last attempt even when
    # the UI fallback switch is off.
    append_model_attempt(model_attempts, "gpt-5.5")

    final_failure_msg: Optional[str] = None
    final_failure_status: int = 0
    upstream_done = False
    try:
        timeout = httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=120.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for model_idx, current_model in enumerate(model_attempts):
                if upstream_done:
                    break
                if model_idx > 0:
                    log_bus.write(
                        "INFO",
                        f"切换到下一个模型：{current_model}",
                    )
                current_raw_model = current_model
                current_model = normalize_anthropic_model(current_raw_model)
                attempt_body = responses_to_anthropic_for_model(body, current_raw_model, stream=True)
                attempt_body["model"] = current_model
                attempt_headers = auth_headers(cfg, current_raw_model)
                model_panic_or_5xx = False
                last_status = 0
                last_msg = ""
                if is_passthrough_model(current_model):
                    # Keep plain function tools so Codex can still execute
                    # local actions, but strip hosted/MCP tool descriptors
                    # that AnyRouter's Responses compatibility rejects.
                    responses_body, tool_filter_summary = sanitize_responses_body_for_anyrouter(body)
                    responses_body["model"] = current_model
                    responses_body["stream"] = True
                    try:
                        requested_max = int(responses_body.get("max_output_tokens") or responses_body.get("max_tokens") or 16)
                    except Exception:
                        requested_max = 16
                    responses_body["max_output_tokens"] = max(MIN_RESPONSES_OUTPUT_TOKENS, requested_max)
                    if tool_filter_summary != "no_tools":
                        log_bus.write("INFO", f"Responses 工具兼容处理：{tool_filter_summary}")
                    responses_headers = {
                        "content-type": "application/json",
                        "accept": "text/event-stream",
                        "authorization": f"Bearer {cfg.api_key}",
                    }
                    responses_url = upstream_responses_url(cfg)
                    body_variants: List[Tuple[str, Dict[str, Any]]] = [(tool_filter_summary, responses_body)]
                    if isinstance(body.get("tools"), list) and responses_body.get("tools"):
                        no_tools_body, no_tools_summary = sanitize_responses_body_for_anyrouter(body, drop_all_tools=True)
                        no_tools_body["model"] = current_model
                        no_tools_body["stream"] = True
                        no_tools_body["max_output_tokens"] = responses_body["max_output_tokens"]
                        body_variants.append((no_tools_summary, no_tools_body))

                    for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                        try:
                            variant_idx = 0
                            while variant_idx < len(body_variants):
                                variant_label, variant_body = body_variants[variant_idx]
                                async with client.stream("POST", responses_url, headers=responses_headers, json=variant_body) as resp:
                                    log_bus.write(
                                        "SUCCESS" if resp.status_code < 400 else "ERROR",
                                        f"模型 {current_model} HTTP {resp.status_code} ({attempt}/{UPSTREAM_RETRY_ATTEMPTS})，variant={variant_label}",
                                    )
                                    if resp.status_code >= 400:
                                        detail = await resp.aread()
                                        raw_msg = detail.decode("utf-8", errors="replace")[:1000]
                                        last_status = resp.status_code
                                        last_msg = friendly_upstream_error(resp.status_code, raw_msg)
                                        tool_schema_error = resp.status_code == 400 and anyrouter_tool_schema_error(raw_msg)
                                        if tool_schema_error and variant_idx + 1 < len(body_variants):
                                            log_bus.write("WARN", "上游不兼容 Codex tools，自动重试无工具请求。")
                                            variant_idx += 1
                                            continue
                                        if resp.status_code in RETRYABLE_UPSTREAM_STATUSES and attempt < UPSTREAM_RETRY_ATTEMPTS:
                                            await asyncio.sleep(upstream_retry_delay(attempt, resp.status_code))
                                            break
                                        variant_idx = len(body_variants)
                                        break

                                    async for event, data in iter_sse_json(resp):
                                        data_type = str(data.get("type") or event or "")
                                        if data_type in ("done", "response.completed"):
                                            break
                                        if data_type in ("error", "response.failed"):
                                            last_msg = friendly_upstream_error(0, json.dumps(data, ensure_ascii=False))
                                            if anyrouter_tool_schema_error(last_msg) and variant_idx + 1 < len(body_variants):
                                                log_bus.write("WARN", "上游 SSE 返回 tools schema 错误，自动重试无工具请求。")
                                                variant_idx += 1
                                                last_msg = ""
                                                break
                                            break
                                        delta = extract_responses_delta(event, data)
                                        if delta:
                                            if not text_open:
                                                yield sse(
                                                    "response.content_part.added",
                                                    {
                                                        "type": "response.content_part.added",
                                                        "sequence_number": seq,
                                                        "item_id": item_id,
                                                        "output_index": output_index,
                                                        "content_index": content_index,
                                                        "part": {"type": "output_text", "text": ""},
                                                    },
                                                )
                                                seq += 1
                                                text_open = True
                                            final_text += delta
                                            yield sse(
                                                "response.output_text.delta",
                                                {
                                                    "type": "response.output_text.delta",
                                                    "sequence_number": seq,
                                                    "item_id": item_id,
                                                    "output_index": output_index,
                                                    "content_index": content_index,
                                                    "delta": delta,
                                                },
                                            )
                                            seq += 1
                                    if not last_msg or text_open:
                                        upstream_done = True
                                    if not upstream_done and variant_idx + 1 < len(body_variants):
                                        continue
                                    variant_idx = len(body_variants)
                                    break
                            if resp.status_code in RETRYABLE_UPSTREAM_STATUSES and not upstream_done and attempt < UPSTREAM_RETRY_ATTEMPTS:
                                continue
                            break
                        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout) as net_err:
                            last_status = 0
                            last_msg = f"连接 AnyRouter 时发生网络错误：{net_err}"
                            log_bus.write("ERROR", f"模型 {current_model} 网络错误 ({attempt}): {net_err}")
                            if attempt < UPSTREAM_RETRY_ATTEMPTS:
                                await asyncio.sleep(upstream_retry_delay(attempt, last_status))
                                continue
                            break
                    if upstream_done:
                        break
                    if text_open:
                        final_failure_msg = last_msg
                        final_failure_status = last_status
                        break
                    if model_idx + 1 < len(model_attempts) and last_status in RETRYABLE_UPSTREAM_STATUSES:
                        final_failure_msg = None
                        final_failure_status = 0
                        continue
                    final_failure_msg = last_msg
                    final_failure_status = last_status
                    break
                for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                    try:
                        async with client.stream(
                            "POST",
                            anthropic_messages_url(cfg, current_raw_model),
                            headers=attempt_headers,
                            json=attempt_body,
                        ) as resp:
                            log_bus.write(
                                "SUCCESS" if resp.status_code < 400 else "ERROR",
                                f"上游 HTTP {resp.status_code}，模型={current_model}（第 {attempt}/{UPSTREAM_RETRY_ATTEMPTS} 次）",
                            )
                            if resp.status_code >= 400:
                                detail = await resp.aread()
                                raw_msg = detail.decode("utf-8", errors="replace")[:1000]
                                msg = friendly_upstream_error(resp.status_code, raw_msg)
                                log_bus.write("ERROR", f"上游返回：{raw_msg[:300]}")
                                last_status = resp.status_code
                                last_msg = msg
                                if resp.status_code in PANIC_STATUSES:
                                    model_panic_or_5xx = True
                                if resp.status_code in RETRYABLE_UPSTREAM_STATUSES and attempt < UPSTREAM_RETRY_ATTEMPTS:
                                    await asyncio.sleep(upstream_retry_delay(attempt, resp.status_code))
                                    continue
                                # Exhausted retries on this model
                                break

                            async for event, data in iter_anthropic_sse(resp):
                                data_type = data.get("type") if isinstance(data, dict) else None
                                if data_type == "error":
                                    err = data.get("error") if isinstance(data, dict) else None
                                    err_msg = ""
                                    if isinstance(err, dict):
                                        err_msg = str(err.get("message") or err)
                                    elif isinstance(err, str):
                                        err_msg = err
                                    last_msg = friendly_upstream_error(0, err_msg or json.dumps(data))
                                    last_status = 0
                                    model_panic_or_5xx = True
                                    break
                                if data_type == "message_stop" or event == "message_stop":
                                    break
                                kind, delta = extract_anthropic_delta(event, data)
                                if kind and delta:
                                    if not text_open:
                                        yield sse(
                                            "response.content_part.added",
                                            {
                                                "type": "response.content_part.added",
                                                "sequence_number": seq,
                                                "item_id": item_id,
                                                "output_index": output_index,
                                                "content_index": content_index,
                                                "part": {"type": "output_text", "text": ""},
                                            },
                                        )
                                        seq += 1
                                        text_open = True
                                    final_text += delta
                                    yield sse(
                                        "response.output_text.delta",
                                        {
                                            "type": "response.output_text.delta",
                                            "sequence_number": seq,
                                            "item_id": item_id,
                                            "output_index": output_index,
                                            "content_index": content_index,
                                            "delta": delta,
                                        },
                                    )
                                    seq += 1
                            # Stream finished normally for this model
                            if not last_msg or text_open:
                                upstream_done = True
                            break
                    except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout) as net_err:
                        log_bus.write("ERROR", f"上游网络错误，模型={current_model}（第 {attempt} 次）：{net_err}")
                        last_status = 0
                        last_msg = f"Network error talking to AnyRouter: {net_err}"
                        if attempt < UPSTREAM_RETRY_ATTEMPTS:
                            await asyncio.sleep(upstream_retry_delay(attempt, last_status))
                            continue
                        break
                if upstream_done:
                    break
                # If we already streamed some text out before the error, do not
                # restart with another model — Codex would see duplicate content.
                if text_open:
                    final_failure_msg = last_msg
                    final_failure_status = last_status
                    break
                # Decide whether to fall through to the next model.
                if model_idx + 1 < len(model_attempts) and (
                    model_panic_or_5xx or last_status in RETRYABLE_UPSTREAM_STATUSES
                ):
                    # try fallback model
                    final_failure_msg = None
                    final_failure_status = 0
                    continue
                final_failure_msg = last_msg
                final_failure_status = last_status
                break
    except Exception as e:
        log_bus.write("ERROR", f"流式请求异常：{e}")
        final_failure_msg = str(e)
        final_failure_status = 0

    if final_failure_msg:
        yield sse(
            "response.output_text.delta",
            {
                "type": "response.output_text.delta",
                "sequence_number": seq,
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "delta": "",
            },
        )
        seq += 1
        yield sse(
            "error",
            {
                "type": "error",
                "sequence_number": seq,
                "error": {"message": final_failure_msg, "code": f"upstream_{final_failure_status}" if final_failure_status else "stream_error"},
            },
        )
        seq += 1
        failed = base_response("failed")
        failed["error"] = {"message": final_failure_msg, "code": f"upstream_{final_failure_status}" if final_failure_status else "stream_error"}
        yield sse("response.failed", {"type": "response.failed", "sequence_number": seq, "response": failed})
        log_bus.write("ERROR", f"流式请求失败：{final_failure_msg}")
        return

    if not text_open:
        text_open = True
        yield sse(
            "response.content_part.added",
            {
                "type": "response.content_part.added",
                "sequence_number": seq,
                "item_id": item_id,
                "output_index": output_index,
                "content_index": content_index,
                "part": {"type": "output_text", "text": ""},
            },
        )
        seq += 1

    yield sse(
        "response.output_text.done",
        {
            "type": "response.output_text.done",
            "sequence_number": seq,
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "text": final_text,
        },
    )
    seq += 1
    yield sse(
        "response.content_part.done",
        {
            "type": "response.content_part.done",
            "sequence_number": seq,
            "item_id": item_id,
            "output_index": output_index,
            "content_index": content_index,
            "part": {"type": "output_text", "text": final_text},
        },
    )
    seq += 1
    output_item = {
        "id": item_id,
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": final_text, "annotations": []}],
    }
    yield sse(
        "response.output_item.done",
        {
            "type": "response.output_item.done",
            "sequence_number": seq,
            "output_index": output_index,
            "item": output_item,
        },
    )
    seq += 1
    completed = base_response("completed")
    completed["output"] = [output_item]
    yield sse("response.completed", {"type": "response.completed", "sequence_number": seq, "response": completed})
    preview = final_text[:80].replace("\n", "\\n")
    log_bus.write("SUCCESS", f"流式请求完成，输出长度={len(final_text)}，预览={preview!r}")


app = FastAPI()


@app.middleware("http")
async def log_incoming(request: Request, call_next):
    """
    Surface every Codex-side HTTP request in the 'Run logs' tab so the user can
    tell at a glance whether Codex is actually hitting our local port. If the
    user reports an upstream wss:// 401 but this middleware never fires, that
    means Codex bypassed our base_url entirely (env var override / OAuth / wrong
    profile), and the diagnostic wizard knows where to point them.
    """
    auth = request.headers.get("authorization", "")
    if auth:
        # Mask all but the first ~10 chars so the prefix ("Bearer codex-an")
        # is enough to identify a misconfig but the full key never leaks.
        masked = auth[:14] + "..." if len(auth) > 14 else "***"
    else:
        masked = "<none>"
    qs = ("?" + request.url.query) if request.url.query else ""
    log_bus.write(
        "INFO",
        f"<- Codex {request.method} {request.url.path}{qs}  auth={masked}",
    )
    return await call_next(request)


@app.websocket("/v1/responses")
@app.websocket("/responses")
async def deny_websocket(websocket: WebSocket) -> None:
    """
    Codex realtime mode tries to upgrade /v1/responses to wss://. We don't
    proxy WebSocket upstream, so accept the upgrade, log loudly, and close.
    Without this route the user only sees a generic 'Reconnecting…' on the
    Codex side and has no way to tell that the upgrade hit our server at all.
    """
    try:
        await websocket.accept()
    except Exception as e:
        log_bus.write("ERROR", f"WebSocket accept 失败：{e}")
        return
    client = "<?>"
    try:
        if websocket.client is not None:
            client = f"{websocket.client.host}:{websocket.client.port}"
    except Exception:
        pass
    log_bus.write(
        "ERROR",
        f"Codex 来自 {client} 的 WebSocket 升级请求被拒绝。"
        " 请在转发服务页点「一键写入 Codex 配置」让 supports_websockets=false 生效，并重启 Codex。",
    )
    try:
        await websocket.close(code=1008, reason="codex-anyroute does not support websockets")
    except Exception:
        pass


async def stream_passthrough(body: Dict[str, Any], cfg: AppConfig) -> AsyncGenerator[bytes, None]:
    """
    Forward a Codex /v1/responses request straight to AnyRouter /v1/responses.
    AnyRouter natively understands the Codex Responses API for gpt-5.5 and
    gpt-5.3-codex, so no translation is needed — we simply remap the model
    according to the user's mapping table and pipe the SSE bytes through.
    """
    requested_model = str(body.get("model") or "")
    upstream_model = cfg.model_map.get(requested_model) or cfg.default_model or requested_model
    out_body, tool_filter_summary = sanitize_responses_body_for_anyrouter(body)
    out_body["model"] = upstream_model
    out_body["stream"] = True
    try:
        requested_max = int(out_body.get("max_output_tokens") or out_body.get("max_tokens") or 16)
    except Exception:
        requested_max = 16
    out_body["max_output_tokens"] = max(MIN_RESPONSES_OUTPUT_TOKENS, requested_max)
    body_variants: List[Tuple[str, Dict[str, Any]]] = [(tool_filter_summary, out_body)]
    if isinstance(body.get("tools"), list) and out_body.get("tools"):
        no_tools_body, no_tools_summary = sanitize_responses_body_for_anyrouter(body, drop_all_tools=True)
        no_tools_body["model"] = upstream_model
        no_tools_body["stream"] = True
        no_tools_body["max_output_tokens"] = out_body["max_output_tokens"]
        body_variants.append((no_tools_summary, no_tools_body))

    headers = {
        "content-type": "application/json",
        "accept": "text/event-stream",
        "authorization": f"Bearer {cfg.api_key}",
    }
    url = upstream_responses_url(cfg)
    log_bus.write("INFO", f"Responses 直通：{requested_model} -> {upstream_model}，地址：{url}")
    if tool_filter_summary != "no_tools":
        log_bus.write("INFO", f"Responses 工具兼容处理：{tool_filter_summary}")

    response_id = f"resp_anyroute_{int(time.time() * 1000)}"
    failure_msg: Optional[str] = None
    failure_status = 0

    try:
        timeout = httpx.Timeout(connect=30.0, read=600.0, write=120.0, pool=120.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                try:
                    variant_idx = 0
                    while variant_idx < len(body_variants):
                        variant_label, variant_body = body_variants[variant_idx]
                        async with client.stream("POST", url, headers=headers, json=variant_body) as resp:
                            log_bus.write(
                                "SUCCESS" if resp.status_code < 400 else "ERROR",
                                f"上游 HTTP {resp.status_code}（第 {attempt}/{UPSTREAM_RETRY_ATTEMPTS} 次），variant={variant_label}",
                            )
                            if resp.status_code >= 400:
                                detail = await resp.aread()
                                raw_msg = detail.decode("utf-8", errors="replace")[:1000]
                                log_bus.write("ERROR", f"上游返回：{raw_msg[:300]}")
                                if resp.status_code == 400 and anyrouter_tool_schema_error(raw_msg) and variant_idx + 1 < len(body_variants):
                                    log_bus.write("WARN", "上游不兼容部分 Codex tools，自动重试无工具请求。")
                                    variant_idx += 1
                                    continue
                                if resp.status_code in RETRYABLE_UPSTREAM_STATUSES and attempt < UPSTREAM_RETRY_ATTEMPTS:
                                    await asyncio.sleep(upstream_retry_delay(attempt, resp.status_code))
                                    break
                                failure_msg = friendly_upstream_error(resp.status_code, raw_msg)
                                failure_status = resp.status_code
                                variant_idx = len(body_variants)
                                break
                            # Preserve the Responses event stream while
                            # stripping relay-generated encrypted blobs that
                            # Codex cannot verify on follow-up turns.
                            async for event, data in iter_sse_json(resp):
                                if data.get("type") == "done":
                                    continue
                                cleaned_data = strip_unverifiable_encrypted_content(data)
                                event_name = event or str(cleaned_data.get("type") or "message")
                                yield sse(event_name, cleaned_data)
                            log_bus.write("SUCCESS", "Responses 直通完成")
                            return
                        if failure_msg or (attempt < UPSTREAM_RETRY_ATTEMPTS and variant_idx == 0):
                            break
                    if failure_msg:
                        break
                except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout) as net_err:
                    log_bus.write("ERROR", f"上游网络错误（第 {attempt} 次）：{net_err}")
                    if attempt < UPSTREAM_RETRY_ATTEMPTS:
                        await asyncio.sleep(upstream_retry_delay(attempt, failure_status))
                        continue
                    failure_msg = f"连接 AnyRouter 时发生网络错误：{net_err}"
                    failure_status = 0
                    break
    except Exception as e:
        log_bus.write("ERROR", f"Responses 直通异常：{e}")
        failure_msg = str(e)
        failure_status = 0

    # Emit a clean Codex failure event sequence so the client doesn't hang.
    seq = 0
    base = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": requested_model or upstream_model,
        "output": [],
    }
    yield sse("response.created", {"type": "response.created", "sequence_number": seq, "response": base})
    seq += 1
    item_id = f"msg_{int(time.time() * 1000)}"
    yield sse(
        "response.output_item.added",
        {
            "type": "response.output_item.added",
            "sequence_number": seq,
            "output_index": 0,
            "item": {"id": item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []},
        },
    )
    seq += 1
    yield sse(
        "response.output_text.delta",
        {
            "type": "response.output_text.delta",
            "sequence_number": seq,
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": "",
        },
    )
    seq += 1
    code = f"upstream_{failure_status}" if failure_status else "stream_error"
    yield sse(
        "error",
        {
            "type": "error",
            "sequence_number": seq,
            "error": {"message": failure_msg or "未知上游错误", "code": code},
        },
    )
    seq += 1
    failed = dict(base)
    failed["status"] = "failed"
    failed["error"] = {"message": failure_msg or "未知上游错误", "code": code}
    yield sse("response.failed", {"type": "response.failed", "sequence_number": seq, "response": failed})
    log_bus.write("ERROR", f"Responses 直通失败：{failure_msg}")


@app.get("/")
async def root() -> Dict[str, str]:
    return {"name": APP_NAME, "version": APP_VERSION, "status": "ok"}


@app.get("/v1/models")
@app.get("/models")
async def models() -> Dict[str, Any]:
    with config_lock:
        cfg = config
        ids = list(cfg.model_map.keys()) or ["gpt-5.4"]
    return {"object": "list", "data": [{"id": mid, "object": "model", "owned_by": "codex-anyroute"} for mid in ids]}


@app.post("/v1/responses")
@app.post("/responses")
async def responses(request: Request) -> Response:
    with config_lock:
        cfg = dataclasses.replace(config)
    if not check_gateway_auth(request, cfg):
        return JSONResponse({"error": {"message": "invalid gateway key"}}, status_code=401)
    body = strip_unverifiable_encrypted_content(await request.json())
    stream = bool(body.get("stream", True))
    if not stream:
        body["stream"] = True
    requested_model = str(body.get("model") or "default")
    _, upstream_model = resolve_upstream_model(requested_model, cfg)
    if is_passthrough_model(upstream_model):
        return StreamingResponse(stream_passthrough(body, cfg), media_type="text/event-stream")
    return StreamingResponse(stream_responses(body, cfg), media_type="text/event-stream")


@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def chat_completions(request: Request) -> Response:
    with config_lock:
        cfg = dataclasses.replace(config)
    if not check_gateway_auth(request, cfg):
        return JSONResponse({"error": {"message": "invalid gateway key"}}, status_code=401)
    body = await request.json()
    messages = body.get("messages", [])
    responses_body = {
        "model": body.get("model", "gpt-5.4"),
        "stream": bool(body.get("stream", True)),
        "input": [{"type": "message", "role": m.get("role", "user"), "content": m.get("content", "")} for m in messages if isinstance(m, dict)],
    }
    return StreamingResponse(stream_responses(responses_body, cfg), media_type="text/event-stream")


def is_port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def start_server() -> None:
    with config_lock:
        cfg = dataclasses.replace(config)
        port = int(cfg.listen_port)
    if server_holder.get("server"):
        log_bus.write("INFO", "转发服务已经在运行")
        _start_codex_config_guard()
        if cfg.codex_auto_apply:
            ensure_codex_anyroute_config(cfg, "服务已在运行")
        else:
            sync_codex_history_for_current_mode(cfg, "服务已在运行")
        return
    if is_port_in_use(port):
        log_bus.write("ERROR", f"端口 {port} 已被占用，请先停止其它转发器或修改端口")
        _start_codex_config_guard()
        sync_codex_history_for_current_mode(cfg, "端口占用时仅同步聊天")
        return
    if cfg.codex_auto_apply:
        ensure_codex_anyroute_config(cfg, "启动转发服务")
    else:
        sync_codex_history_for_current_mode(cfg, "启动转发服务")
    uvicorn_config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
    )
    server = uvicorn.Server(uvicorn_config)
    server_holder["server"] = server

    def runner() -> None:
        log_bus.write("SUCCESS", f"转发服务已启动：http://127.0.0.1:{port}")
        try:
            asyncio.run(server.serve())
        except Exception as e:
            log_bus.write("ERROR", f"转发服务异常退出：{e}")
        log_bus.write("INFO", "转发服务已停止")
        server_holder["server"] = None

    thread = threading.Thread(target=runner, daemon=True)
    server_holder["thread"] = thread
    thread.start()
    _start_codex_config_guard()


def stop_server() -> None:
    _stop_codex_config_guard()
    server = server_holder.get("server")
    if server:
        server.should_exit = True
        log_bus.write("INFO", "正在停止转发服务...")


CODEX_BLOCK_BEGIN = "# === BEGIN codex-anyroute (Managed by Codex AnyRoute Transfer) ==="
CODEX_BLOCK_END = "# === END codex-anyroute ==="
CODEX_ROOT_BLOCK_BEGIN = "# === BEGIN codex-anyroute root ==="
CODEX_ROOT_BLOCK_END = "# === END codex-anyroute root ==="
CODEX_TABLE_BLOCK_BEGIN = "# === BEGIN codex-anyroute providers ==="
CODEX_TABLE_BLOCK_END = "# === END codex-anyroute providers ==="
CODEX_WORKSPACE_BLOCK_BEGIN = "# === BEGIN codex-anyroute workspace ==="
CODEX_WORKSPACE_BLOCK_END = "# === END codex-anyroute workspace ==="
# Older builds used a single-line marker without a closing tag. We still need
# to recognize and strip those when rewriting.
CODEX_LEGACY_MARKER = "# Managed by Codex AnyRoute Transfer"
CODEX_MANAGED_ROOT_KEYS = {
    "model",
    "model_provider",
    "model_context_window",
    "preferred_auth_method",
    "forced_login_method",
    "cli_auth_credentials_store",
}
CODEX_MANAGED_SECTIONS = {
    "[model_providers.codex-anyroute]",
    "[profiles.codex-anyroute]",
}


def _strip_marked_block(text: str, begin: str, end: str) -> str:
    while begin in text:
        start = text.index(begin)
        end_idx = text.find(end, start)
        if end_idx == -1:
            text = text[:start].rstrip() + "\n"
            break
        end_idx += len(end)
        if end_idx < len(text) and text[end_idx] == "\n":
            end_idx += 1
        text = (text[:start] + text[end_idx:]).rstrip() + "\n"
    return text


def _strip_old_codex_block(existing: str) -> str:
    """Remove any previously written codex-anyroute managed block from the toml."""
    text = existing or ""
    text = _strip_marked_block(text, CODEX_BLOCK_BEGIN, CODEX_BLOCK_END)
    text = _strip_marked_block(text, CODEX_ROOT_BLOCK_BEGIN, CODEX_ROOT_BLOCK_END)
    text = _strip_marked_block(text, CODEX_TABLE_BLOCK_BEGIN, CODEX_TABLE_BLOCK_END)
    text = _strip_marked_block(text, CODEX_WORKSPACE_BLOCK_BEGIN, CODEX_WORKSPACE_BLOCK_END)
    # Legacy marker: everything from the marker to EOF was managed.
    if CODEX_LEGACY_MARKER in text:
        text = text[: text.index(CODEX_LEGACY_MARKER)].rstrip() + "\n"
    return text


def _split_toml_root(text: str) -> Tuple[str, str]:
    lines = (text or "").splitlines(keepends=True)
    for idx, line in enumerate(lines):
        if line.strip().startswith("["):
            return "".join(lines[:idx]), "".join(lines[idx:])
    return text or "", ""


def _active_toml_key(line: str) -> Optional[str]:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    left, _, _ = stripped.partition("=")
    return left.strip().strip('"').strip("'")


def _strip_managed_root_keys(root_text: str) -> str:
    kept: List[str] = []
    for line in (root_text or "").splitlines(keepends=True):
        key = _active_toml_key(line)
        if key in CODEX_MANAGED_ROOT_KEYS:
            continue
        kept.append(line)
    return "".join(kept)


def _strip_managed_sections(table_text: str) -> str:
    kept: List[str] = []
    skipping = False
    for line in (table_text or "").splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("["):
            skipping = stripped in CODEX_MANAGED_SECTIONS
            if skipping:
                continue
        if skipping:
            continue
        kept.append(line)
    return "".join(kept)


def _build_codex_root_block(cfg: AppConfig) -> str:
    return (
        f"{CODEX_ROOT_BLOCK_BEGIN}\n"
        'model = "gpt-5.5"\n'
        'model_provider = "codex-anyroute"\n'
        "model_context_window = 1000000\n"
        'preferred_auth_method = "apikey"\n'
        f"{CODEX_ROOT_BLOCK_END}\n"
    )


def _build_codex_provider_block(cfg: AppConfig) -> str:
    port = int(cfg.listen_port)
    gateway_key = (cfg.gateway_key or DEFAULT_GATEWAY_KEY).replace("\\", "\\\\").replace('"', '\\"')
    return (
        f"{CODEX_TABLE_BLOCK_BEGIN}\n"
        "[model_providers.codex-anyroute]\n"
        'name = "Codex AnyRoute"\n'
        f'base_url = "http://127.0.0.1:{port}/v1"\n'
        'wire_api = "responses"\n'
        f'experimental_bearer_token = "{gateway_key}"\n'
        "requires_openai_auth = false\n"
        "supports_websockets = false\n"
        "request_max_retries = 4\n"
        "stream_max_retries = 5\n"
        "stream_idle_timeout_ms = 600000\n"
        "\n"
        "[profiles.codex-anyroute]\n"
        'model = "gpt-5.5"\n'
        'model_provider = "codex-anyroute"\n'
        "model_context_window = 1000000\n"
        f"{CODEX_TABLE_BLOCK_END}\n"
    )


def _toml_section_exists(text: str, section: str) -> bool:
    target = section.strip().lower()
    for line in (text or "").splitlines():
        stripped = line.strip().lower()
        if stripped == target:
            return True
    return False


def _codex_workspace_roots() -> List[Path]:
    docs_root = Path.home() / "Documents" / "Codex"
    today_root = docs_root / time.strftime("%Y-%m-%d")
    return [docs_root, today_root]


def _toml_single_quoted(value: str) -> str:
    # TOML literal strings keep Windows backslashes as-is.
    return value.replace("'", "''")


def _build_codex_workspace_block(table_text: str) -> str:
    sections: List[str] = []
    for root in _codex_workspace_roots():
        key = str(root).lower()
        header = f"[projects.'{_toml_single_quoted(key)}']"
        if _toml_section_exists(table_text, header):
            continue
        sections.append(f"{header}\ntrust_level = \"trusted\"")
    if not sections:
        return ""
    return f"{CODEX_WORKSPACE_BLOCK_BEGIN}\n" + "\n\n".join(sections) + f"\n{CODEX_WORKSPACE_BLOCK_END}\n"


def _build_codex_config_text(existing: str, cfg: AppConfig) -> str:
    cleaned = _strip_old_codex_block(existing)
    root_text, table_text = _split_toml_root(cleaned)
    root_text = _strip_managed_root_keys(root_text).strip()
    table_text = _strip_managed_sections(table_text).strip()

    parts: List[str] = [_build_codex_root_block(cfg).strip()]
    if root_text:
        parts.append(root_text)
    if table_text:
        parts.append(table_text)
    parts.append(_build_codex_provider_block(cfg).strip())
    workspace_block = _build_codex_workspace_block(table_text)
    if workspace_block.strip():
        parts.append(workspace_block.strip())
    return "\n\n".join(parts).rstrip() + "\n"


def _parse_codex_config(cfg_text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        parsed = tomllib.loads(cfg_text or "")
        if isinstance(parsed, dict):
            return parsed, None
        return None, "config.toml 解析结果不是对象"
    except Exception as e:
        return None, str(e)


def _patch_codex_auth_json(auth_path: Path, gateway_key: str) -> Tuple[bool, bool]:
    """
    Keep Codex in API-key UI mode while preserving OAuth tokens.

    Codex App's settings menu shows the official API-key account/logout page
    only when auth.json says apikey and has OPENAI_API_KEY. The actual relay
    routing is still controlled by config.toml's codex-anyroute provider block,
    so this key is only the local gateway token used to reach 127.0.0.1.
    """
    existing: Dict[str, Any] = {}
    if auth_path.exists():
        try:
            raw = auth_path.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            if isinstance(parsed, dict):
                existing = parsed
        except Exception:
            existing = {}
    oauth_residue = any(k in existing for k in ("tokens", "last_refresh", "account_id"))
    changed = False
    if existing.get("auth_mode") != "apikey":
        existing["auth_mode"] = "apikey"
        changed = True

    desired_key = gateway_key or DEFAULT_GATEWAY_KEY
    if existing.get("OPENAI_API_KEY") != desired_key:
        existing["OPENAI_API_KEY"] = desired_key
        changed = True

    if changed:
        auth_path.parent.mkdir(parents=True, exist_ok=True)
        auth_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return changed, oauth_residue


def _append_unique_string(items: Any, value: str) -> Tuple[List[str], bool]:
    if isinstance(items, list):
        out = [str(item) for item in items if isinstance(item, str)]
    else:
        out = []
    exists = any(item.lower() == value.lower() for item in out)
    if exists:
        return out, False
    out.append(value)
    return out, True


def _patch_codex_desktop_workspace_state(codex_dir: Path) -> bool:
    """
    Register the projectless Codex folder as a saved workspace.

    Codex App creates projectless chats under Documents/Codex. If only the
    per-chat child folders are trusted, the desktop app may repeat its
    workspace setup for every new API thread. Saving and trusting the parent
    root gives it a stable root to reuse without touching the model relay.
    """
    state_path = codex_dir / ".codex-global-state.json"
    if not state_path.exists():
        return False
    try:
        raw = state_path.read_text(encoding="utf-8")
        state = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        log_bus.write("WARN", f"无法读取 Codex 桌面状态，跳过工作区优化：{e}")
        return False
    if not isinstance(state, dict):
        return False

    changed = False
    workspace_root = str(Path.home() / "Documents" / "Codex")
    for key in ("electron-saved-workspace-roots", "project-order"):
        values, did_change = _append_unique_string(state.get(key), workspace_root)
        if did_change or not isinstance(state.get(key), list):
            state[key] = values
            changed = True

    active_values = state.get("active-workspace-roots")
    if not isinstance(active_values, list) or len(active_values) != 1 or str(active_values[0]).lower() != workspace_root.lower():
        state["active-workspace-roots"] = [workspace_root]
        changed = True

    persisted = state.get("electron-persisted-atom-state")
    if not isinstance(persisted, dict):
        persisted = {}
        state["electron-persisted-atom-state"] = persisted
        changed = True
    if persisted.get("electron:onboarding-projectless-completed") is not True:
        persisted["electron:onboarding-projectless-completed"] = True
        changed = True
    if persisted.get("electron:onboarding-welcome-pending") is not False:
        persisted["electron:onboarding-welcome-pending"] = False
        changed = True
    if persisted.get("electron:onboarding-primary-runtime-install-requested") is not True:
        persisted["electron:onboarding-primary-runtime-install-requested"] = True
        changed = True
    if persisted.get("electron:onboarding-primary-runtime-install-ready") is not True:
        persisted["electron:onboarding-primary-runtime-install-ready"] = True
        changed = True

    if changed:
        state_path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return changed


def _clean_codex_path(value: Any) -> str:
    path = str(value or "").strip()
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normpath(path) if path else ""


def _path_is_under(path: Any, root: Any) -> bool:
    cleaned_path = _clean_codex_path(path).lower()
    cleaned_root = _clean_codex_path(root).lower()
    if not cleaned_path or not cleaned_root:
        return False
    return cleaned_path == cleaned_root or cleaned_path.startswith(cleaned_root.rstrip("\\") + "\\")


def _codex_state_workspace_roots(state: Dict[str, Any]) -> Tuple[str, List[str], List[str]]:
    docs_root = _clean_codex_path(Path.home() / "Documents" / "Codex")
    values: List[str] = []
    for key in ("electron-saved-workspace-roots", "project-order"):
        raw_values = state.get(key)
        if isinstance(raw_values, list):
            values.extend(str(item) for item in raw_values if isinstance(item, str))
    values.append(docs_root)

    all_roots: List[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_codex_path(value)
        lowered = cleaned.lower()
        if cleaned and lowered not in seen:
            all_roots.append(cleaned)
            seen.add(lowered)

    project_roots = [root for root in all_roots if root.lower() != docs_root.lower()]
    project_roots.sort(key=len, reverse=True)
    return docs_root, project_roots, all_roots


def _match_codex_project_root(cwd: Any, project_roots: List[str]) -> Optional[str]:
    for root in project_roots:
        if _path_is_under(cwd, root):
            return root
    cleaned_cwd = _clean_codex_path(cwd).lower()
    if not cleaned_cwd:
        return None
    parts = [part for part in cleaned_cwd.split("\\") if part]
    for root in project_roots:
        root_name = Path(root).name.lower()
        if root_name and root_name in parts:
            return root
    return None


def _read_unarchived_codex_threads(codex_dir: Path) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    state_db = codex_dir / "state_5.sqlite"
    if not state_db.exists():
        return [], None
    try:
        import sqlite3

        uri = state_db.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            rows = con.execute(
                """
                SELECT id, cwd
                FROM threads
                WHERE COALESCE(archived, 0) = 0
                  AND id IS NOT NULL
                  AND TRIM(id) != ''
                ORDER BY updated_at DESC
                """
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        return [], str(e)

    threads: List[Tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        thread_id = str(row[0] or "").strip()
        cwd = str(row[1] or "").strip()
        lowered = thread_id.lower()
        if thread_id and lowered not in seen:
            threads.append((thread_id, cwd))
            seen.add(lowered)
    return threads, None


def _read_unarchived_codex_thread_ids(codex_dir: Path) -> Tuple[List[str], Optional[str]]:
    threads, err = _read_unarchived_codex_threads(codex_dir)
    return [thread_id for thread_id, _cwd in threads], err


def _codex_shared_history_coverage(codex_dir: Path) -> Tuple[int, int, Optional[str]]:
    threads, err = _read_unarchived_codex_threads(codex_dir)
    if err:
        return 0, 0, err
    if not threads:
        return 0, 0, None
    state_path = codex_dir / ".codex-global-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception as e:
        return len(threads), 0, str(e)
    if not isinstance(state, dict):
        return len(threads), 0, "Codex桌面状态不是JSON对象"
    docs_root, project_roots, _all_roots = _codex_state_workspace_roots(state)
    projectless = state.get("projectless-thread-ids")
    if isinstance(projectless, list):
        projectless_ids = {str(item).lower() for item in projectless if isinstance(item, str)}
    else:
        projectless_ids = set()
    hints = state.get("thread-workspace-root-hints")
    if not isinstance(hints, dict):
        hints = {}
    expected = 0
    covered = 0
    for thread_id, cwd in threads:
        matched_project = _match_codex_project_root(cwd, project_roots)
        if matched_project:
            expected += 1
            hinted = str(hints.get(thread_id) or "")
            if thread_id.lower() not in projectless_ids and _clean_codex_path(hinted).lower() == _clean_codex_path(matched_project).lower():
                covered += 1
        elif _path_is_under(cwd, docs_root):
            expected += 1
            hinted = str(hints.get(thread_id) or "")
            if thread_id.lower() in projectless_ids and _clean_codex_path(hinted).lower() == _clean_codex_path(docs_root).lower():
                covered += 1
    return expected, covered, None


def _patch_codex_shared_history_state(codex_dir: Path) -> Tuple[bool, int, int]:
    """
    Keep Plus and API mode looking at the same local desktop thread list.

    Codex stores conversations in state_5.sqlite, while the desktop sidebar
    also tracks a projectless thread id list in .codex-global-state.json.
    Switching auth/provider modes can leave one side invisible. We only add
    existing, unarchived thread ids to that list and point their workspace
    hint at Documents/Codex; no conversation content is copied or edited.
    """
    threads, err = _read_unarchived_codex_threads(codex_dir)
    if err:
        log_bus.write("WARN", f"无法读取 Codex 本地聊天索引，跳过Plus/API历史同步：{err}")
        return False, 0, 0
    if not threads:
        return False, 0, 0

    state_path = codex_dir / ".codex-global-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception as e:
        log_bus.write("WARN", f"无法读取 Codex 桌面状态，跳过Plus/API历史同步：{e}")
        return False, len(threads), 0
    if not isinstance(state, dict):
        log_bus.write("WARN", "Codex 桌面状态不是 JSON 对象，跳过Plus/API历史同步。")
        return False, len(threads), 0

    changed = False
    synced = 0
    docs_root, project_roots, all_roots = _codex_state_workspace_roots(state)

    projectless = state.get("projectless-thread-ids")
    if not isinstance(projectless, list):
        projectless = []
        state["projectless-thread-ids"] = projectless
        changed = True

    hints = state.get("thread-workspace-root-hints")
    if not isinstance(hints, dict):
        hints = {}
        state["thread-workspace-root-hints"] = hints
        changed = True

    project_thread_ids: set[str] = set()
    docs_thread_ids: set[str] = set()
    desired_hints: Dict[str, str] = {}
    for thread_id, cwd in threads:
        matched_project = _match_codex_project_root(cwd, project_roots)
        if matched_project:
            project_thread_ids.add(thread_id)
            desired_hints[thread_id] = matched_project
        elif _path_is_under(cwd, docs_root):
            docs_thread_ids.add(thread_id)
            desired_hints[thread_id] = docs_root

    new_projectless: List[str] = []
    existing_projectless: set[str] = set()
    for item in projectless:
        if not isinstance(item, str) or item in project_thread_ids:
            changed = True
            continue
        lowered = item.lower()
        if lowered not in existing_projectless:
            new_projectless.append(item)
            existing_projectless.add(lowered)
    for thread_id in docs_thread_ids:
        lowered = thread_id.lower()
        if lowered not in existing_projectless:
            new_projectless.append(thread_id)
            existing_projectless.add(lowered)
            changed = True
            synced += 1
    if new_projectless != projectless:
        state["projectless-thread-ids"] = new_projectless
        changed = True

    for thread_id, workspace_root in desired_hints.items():
        touched = False
        if str(hints.get(thread_id) or "").lower() != workspace_root.lower():
            hints[thread_id] = workspace_root
            changed = True
            touched = True
        if touched:
            synced += 1

    for key in ("electron-saved-workspace-roots", "project-order"):
        values = state.get(key)
        if not isinstance(values, list):
            values = []
            state[key] = values
            changed = True
        existing_roots = {_clean_codex_path(item).lower() for item in values if isinstance(item, str)}
        for root in all_roots:
            if root.lower() not in existing_roots:
                values.append(root)
                existing_roots.add(root.lower())
                changed = True

    if changed:
        state_path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return changed, len(threads), synced


def _patch_codex_threads_provider(codex_dir: Path, provider: str) -> Tuple[bool, int]:
    state_db = codex_dir / "state_5.sqlite"
    if not state_db.exists():
        return False, 0
    try:
        import sqlite3

        con = sqlite3.connect(state_db, timeout=5.0)
        try:
            cur = con.execute(
                """
                UPDATE threads
                SET model_provider = ?
                WHERE COALESCE(archived, 0) = 0
                  AND source = 'vscode'
                  AND model_provider != ?
                """,
                (provider, provider),
            )
            changed = int(cur.rowcount or 0)
            con.commit()
        finally:
            con.close()
    except Exception as e:
        log_bus.write("WARN", f"无法同步Plus/API线程provider索引：{e}")
        return False, 0
    return changed > 0, changed


def _patch_codex_rollout_providers(codex_dir: Path, provider: str) -> Tuple[bool, int]:
    state_db = codex_dir / "state_5.sqlite"
    if not state_db.exists():
        return False, 0
    try:
        import sqlite3

        con = sqlite3.connect(state_db, timeout=5.0)
        try:
            rows = con.execute(
                """
                SELECT rollout_path
                FROM threads
                WHERE COALESCE(archived, 0) = 0
                  AND source = 'vscode'
                  AND rollout_path IS NOT NULL
                  AND TRIM(rollout_path) != ''
                """
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        log_bus.write("WARN", f"无法读取Codex会话文件索引：{e}")
        return False, 0

    changed_count = 0
    seen: set[str] = set()
    for (rollout_path,) in rows:
        path = Path(str(rollout_path or ""))
        key = str(path).lower()
        if key in seen or not path.exists() or not path.is_file():
            continue
        seen.add(key)
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        changed = False
        patched_lines: List[str] = []
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:
                patched_lines.append(line)
                continue
            line_changed = False
            if isinstance(item, dict) and item.get("type") == "session_meta":
                payload = item.get("payload")
                if isinstance(payload, dict) and payload.get("model_provider") != provider:
                    payload["model_provider"] = provider
                    changed = True
                    line_changed = True
            patched_lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")) if line_changed else line)
        if not changed:
            continue
        try:
            path.write_text("\n".join(patched_lines) + "\n", encoding="utf-8")
            changed_count += 1
        except Exception as e:
            log_bus.write("WARN", f"无法同步Codex会话文件provider：{path}：{e}")
    return changed_count > 0, changed_count


def _patch_codex_thread_cwds(codex_dir: Path, project_roots: List[str]) -> Tuple[bool, int]:
    state_db = codex_dir / "state_5.sqlite"
    if not state_db.exists() or not project_roots:
        return False, 0
    try:
        import sqlite3

        con = sqlite3.connect(state_db, timeout=5.0)
        try:
            rows = con.execute(
                """
                SELECT id, cwd
                FROM threads
                WHERE COALESCE(archived, 0) = 0
                  AND source = 'vscode'
                  AND id IS NOT NULL
                  AND TRIM(id) != ''
                """
            ).fetchall()
            updates: List[Tuple[str, str]] = []
            for thread_id, cwd in rows:
                matched_project = _match_codex_project_root(cwd, project_roots)
                if not matched_project:
                    continue
                if str(cwd or "") != matched_project:
                    updates.append((matched_project, str(thread_id)))
            if updates:
                con.executemany("UPDATE threads SET cwd = ? WHERE id = ?", updates)
                con.commit()
        finally:
            con.close()
    except Exception as e:
        log_bus.write("WARN", f"无法规范化Codex项目聊天cwd：{e}")
        return False, 0
    return bool(updates), len(updates)


def _codex_thread_provider_coverage(codex_dir: Path, provider: str) -> Tuple[int, int, Optional[str]]:
    state_db = codex_dir / "state_5.sqlite"
    if not state_db.exists():
        return 0, 0, None
    try:
        import sqlite3

        uri = state_db.as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            rows = con.execute(
                """
                SELECT model_provider
                FROM threads
                WHERE COALESCE(archived, 0) = 0
                  AND source = 'vscode'
                """
            ).fetchall()
        finally:
            con.close()
    except Exception as e:
        return 0, 0, str(e)
    total = len(rows)
    covered = sum(1 for row in rows if str(row[0] or "") == provider)
    return total, covered, None


def _detect_codex_visible_provider(codex_dir: Path, cfg: AppConfig) -> str:
    config_toml = codex_dir / "config.toml"
    auth_json = codex_dir / "auth.json"
    expected_key = cfg.gateway_key or DEFAULT_GATEWAY_KEY
    expected_port = int(cfg.listen_port)

    try:
        parsed_cfg = tomllib.loads(config_toml.read_text(encoding="utf-8")) if config_toml.exists() else {}
        providers = parsed_cfg.get("model_providers") if isinstance(parsed_cfg, dict) else {}
        provider_cfg = providers.get("codex-anyroute") if isinstance(providers, dict) else None
        if (
            isinstance(provider_cfg, dict)
            and parsed_cfg.get("model_provider") == "codex-anyroute"
            and f"127.0.0.1:{expected_port}" in str(provider_cfg.get("base_url") or "")
        ):
            return "codex-anyroute"
    except Exception:
        pass

    try:
        auth_data = json.loads(auth_json.read_text(encoding="utf-8")) if auth_json.exists() else {}
        if isinstance(auth_data, dict) and auth_data.get("auth_mode") == "apikey" and auth_data.get("OPENAI_API_KEY") == expected_key:
            return "codex-anyroute"
    except Exception:
        pass
    return "openai"


def sync_codex_history_for_current_mode(cfg: AppConfig, reason: str = "") -> Tuple[str, int, int, int, int]:
    codex_dir = Path.home() / ".codex"
    state_path = codex_dir / ".codex-global-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception:
        state = {}
    if isinstance(state, dict):
        _docs_root, project_roots, _all_roots = _codex_state_workspace_roots(state)
        cwd_changed, cwd_count = _patch_codex_thread_cwds(codex_dir, project_roots)
        if cwd_changed:
            log_bus.write("SUCCESS", f"已规范化 {cwd_count} 条项目聊天cwd，确保项目侧边栏可直接查询。")
    provider = _detect_codex_visible_provider(codex_dir, cfg)
    _changed_provider, provider_count = _patch_codex_threads_provider(codex_dir, provider)
    _changed_rollouts, rollout_count = _patch_codex_rollout_providers(codex_dir, provider)
    _changed_history, total_threads, synced_threads = _patch_codex_shared_history_state(codex_dir)
    total_provider, covered_provider, provider_err = _codex_thread_provider_coverage(codex_dir, provider)
    if provider_err:
        log_bus.write("WARN", f"Plus/API实时同步检查失败：{provider_err}")
    elif provider_count or rollout_count or synced_threads:
        suffix = f"（{reason}）" if reason else ""
        label = "AnyRoute API" if provider == "codex-anyroute" else "官方Plus"
        log_bus.write(
            "SUCCESS",
            f"Plus/API实时同步完成{suffix}：当前{label}，provider={covered_provider}/{total_provider}，会话文件={rollout_count}，位置索引={total_threads}。",
        )
    return provider, total_provider, covered_provider, total_threads, synced_threads


def _codex_anyroute_state_ok(cfg: AppConfig) -> Tuple[bool, str]:
    codex_dir = Path.home() / ".codex"
    config_toml = codex_dir / "config.toml"
    auth_json = codex_dir / "auth.json"
    state_path = codex_dir / ".codex-global-state.json"
    expected_key = cfg.gateway_key or DEFAULT_GATEWAY_KEY
    expected_port = int(cfg.listen_port)
    workspace_root = str(Path.home() / "Documents" / "Codex")

    try:
        cfg_text = config_toml.read_text(encoding="utf-8")
        parsed_cfg = tomllib.loads(cfg_text)
        providers = parsed_cfg.get("model_providers") or {}
        provider_cfg = providers.get("codex-anyroute") if isinstance(providers, dict) else None
        projects = parsed_cfg.get("projects") if isinstance(parsed_cfg, dict) else None
    except Exception as e:
        return False, f"config.toml 不可用：{e}"

    if parsed_cfg.get("model_provider") != "codex-anyroute":
        return False, "model_provider 不是 codex-anyroute"
    if parsed_cfg.get("preferred_auth_method") != "apikey":
        return False, "preferred_auth_method 不是 apikey"
    if not isinstance(provider_cfg, dict):
        return False, "缺少 codex-anyroute provider"
    if f"127.0.0.1:{expected_port}" not in str(provider_cfg.get("base_url") or ""):
        return False, "provider base_url 未指向当前本地端口"
    if provider_cfg.get("experimental_bearer_token") != expected_key:
        return False, "provider bearer token 不是本地网关 Key"
    if provider_cfg.get("requires_openai_auth") is not False:
        return False, "requires_openai_auth 未关闭"
    if provider_cfg.get("supports_websockets") is not False:
        return False, "supports_websockets 未关闭"
    if not isinstance(projects, dict) or not any(str(path).lower() == workspace_root.lower() for path in projects.keys()):
        return False, "Documents\\Codex 未写入 trusted projects"

    try:
        auth_data = json.loads(auth_json.read_text(encoding="utf-8")) if auth_json.exists() else {}
    except Exception as e:
        return False, f"auth.json 不可用：{e}"
    if not isinstance(auth_data, dict):
        return False, "auth.json 不是对象"
    if auth_data.get("auth_mode") != "apikey":
        return False, "auth_mode 不是 apikey"
    if auth_data.get("OPENAI_API_KEY") != expected_key:
        return False, "auth.json 未写入本地网关 Key"

    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception as e:
        return False, f"Codex 桌面状态不可用：{e}"
    if not isinstance(state, dict):
        return False, "Codex 桌面状态不是 JSON 对象"
    saved = state.get("electron-saved-workspace-roots")
    active = state.get("active-workspace-roots")
    if not isinstance(saved, list) or not any(str(path).lower() == workspace_root.lower() for path in saved):
        return False, "Documents\\Codex 未加入保存工作区"
    if not isinstance(active, list) or len(active) != 1 or str(active[0]).lower() != workspace_root.lower():
        return False, "active-workspace-roots 未固定到 Documents\\Codex"
    total_threads, covered_threads, history_err = _codex_shared_history_coverage(codex_dir)
    if history_err:
        return False, f"Plus/API共享历史检查失败：{history_err}"
    if total_threads and covered_threads < total_threads:
        return False, f"Plus/API共享历史未同步：{covered_threads}/{total_threads}"
    total_provider, covered_provider, provider_err = _codex_thread_provider_coverage(codex_dir, "codex-anyroute")
    if provider_err:
        return False, f"Plus/API线程provider检查失败：{provider_err}"
    if total_provider and covered_provider < total_provider:
        return False, f"API侧线程provider未同步：{covered_provider}/{total_provider}"
    return True, "ok"


def ensure_codex_anyroute_config(cfg: AppConfig, reason: str = "") -> None:
    ok, detail = _codex_anyroute_state_ok(cfg)
    if ok:
        return
    suffix = f"（{reason}：{detail}）" if reason else f"（{detail}）"
    log_bus.write("WARN", "检测到 Codex 配置偏离 AnyRoute API 模式，正在自动修复" + suffix)
    write_codex_config(cfg)


def _codex_state_fingerprint() -> Tuple[Tuple[int, int], ...]:
    """
    Cheap fingerprint of the Codex state files we manage. The guard thread
    skips the full ``ensure_codex_anyroute_config`` pass when this hasn't
    changed, which keeps disk traffic flat (and prevents the guard from
    fighting an external editor mid-write).
    """
    codex_dir = Path.home() / ".codex"
    targets = [
        codex_dir / "config.toml",
        codex_dir / "auth.json",
        codex_dir / "state_5.sqlite",
        codex_dir / ".codex-global-state.json",
    ]
    out: List[Tuple[int, int]] = []
    for target in targets:
        try:
            stat = target.stat()
            out.append((int(stat.st_mtime_ns), int(stat.st_size)))
        except FileNotFoundError:
            out.append((0, 0))
        except Exception:
            out.append((-1, -1))
    return tuple(out)


def _start_codex_config_guard() -> None:
    stop_event = server_holder.get("guard_stop")
    if isinstance(stop_event, threading.Event) and not stop_event.is_set():
        return

    stop_event = threading.Event()
    server_holder["guard_stop"] = stop_event

    # Tick interval: 30s (was 3s). Combined with the fingerprint check below,
    # this stops the guard from re-reading ~/.codex/* a few hundred times per
    # minute while the user is just chatting in Codex. The faster cadence
    # never bought us anything in practice — config drift only happens after
    # an explicit Codex action (login / setting toggle / version upgrade).
    GUARD_INTERVAL_SECONDS = 30.0

    def guard() -> None:
        last_fingerprint: Optional[Tuple[Tuple[int, int], ...]] = None
        while not stop_event.wait(GUARD_INTERVAL_SECONDS):
            try:
                fingerprint = _codex_state_fingerprint()
            except Exception:
                fingerprint = None
            if fingerprint is not None and fingerprint == last_fingerprint:
                continue
            with config_lock:
                cfg = dataclasses.replace(config)
            try:
                if cfg.codex_auto_apply:
                    ensure_codex_anyroute_config(cfg, "守护检查")
                else:
                    sync_codex_history_for_current_mode(cfg, "守护检查")
                last_fingerprint = fingerprint
            except Exception as e:
                log_bus.write("WARN", f"Codex 配置守护检查失败：{e}")
                # On error, drop the cached fingerprint so the next tick
                # always retries instead of getting stuck believing the
                # files are healthy.
                last_fingerprint = None

    thread = threading.Thread(target=guard, daemon=True)
    server_holder["guard_thread"] = thread
    thread.start()


def _stop_codex_config_guard() -> None:
    stop_event = server_holder.get("guard_stop")
    if isinstance(stop_event, threading.Event):
        stop_event.set()
    server_holder["guard_stop"] = None


def write_codex_config(cfg: AppConfig) -> None:
    codex_dir = Path.home() / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    config_toml = codex_dir / "config.toml"

    existing = config_toml.read_text(encoding="utf-8") if config_toml.exists() else ""
    new_text = _build_codex_config_text(existing, cfg)
    config_toml.write_text(new_text, encoding="utf-8")

    log_bus.write("SUCCESS", f"已写入 Codex 配置：{config_toml}")
    auth_changed, oauth_present = _patch_codex_auth_json(codex_dir / "auth.json", cfg.gateway_key or DEFAULT_GATEWAY_KEY)
    if auth_changed:
        log_bus.write("SUCCESS", "已切换 Codex 到 API Key 登录外观，并保留原有 OAuth 令牌。")
    elif oauth_present:
        log_bus.write("INFO", "auth.json 已是 API Key 登录外观，OAuth 令牌仍保留。")
    else:
        log_bus.write("INFO", "auth.json 已是 API Key 登录外观。")

    if _patch_codex_desktop_workspace_state(codex_dir):
        log_bus.write("SUCCESS", "已把 Documents\\Codex 加入 Codex 保存工作区，减少新对话重复设置工作空间。")
    else:
        log_bus.write("INFO", "Codex 工作区状态无需调整或暂不可用。")

    provider_changed, provider_count = _patch_codex_threads_provider(codex_dir, "codex-anyroute")
    if provider_changed:
        log_bus.write("SUCCESS", f"已把 {provider_count} 条本地聊天索引切到 AnyRoute API 模式，确保API侧可见。")
    else:
        log_bus.write("INFO", "本地聊天索引已处于 AnyRoute API 可见状态。")

    sync_codex_history_for_current_mode(cfg, "写入API配置")
    history_changed, total_threads, synced_threads = _patch_codex_shared_history_state(codex_dir)
    if total_threads <= 0:
        log_bus.write("INFO", "未发现需要同步的 Codex 本地聊天记录。")
    elif history_changed:
        log_bus.write("SUCCESS", f"已修复Plus/API聊天索引 {synced_threads}/{total_threads} 条：项目对话回项目，无项目对话进共享列表。")
    else:
        log_bus.write("INFO", f"Plus/API本地聊天索引已是最新（{total_threads}条）。")
    _start_codex_config_guard()


def restore_codex_official_config() -> Tuple[bool, bool, bool]:
    """
    Revert the changes made by write_codex_config so Codex falls back to its
    official ChatGPT Plus / OAuth configuration. Strips the managed block from
    ~/.codex/config.toml and restores auth.json back to ChatGPT mode when
    OAuth tokens are present. The token fields themselves are left untouched,
    so the user does not have to re-login. Returns
    (config_changed, auth_changed, oauth_present).
    """
    codex_dir = Path.home() / ".codex"
    config_toml = codex_dir / "config.toml"
    auth_json = codex_dir / "auth.json"

    config_changed = False
    if config_toml.exists():
        existing = config_toml.read_text(encoding="utf-8")
        cleaned = _strip_old_codex_block(existing)
        if cleaned != existing:
            # If everything was managed by us, leave an empty file rather than
            # deleting it — Codex tolerates a missing config.toml but never
            # touch the parent directory or other state on the user's behalf.
            config_toml.write_text(cleaned if cleaned.strip() else "", encoding="utf-8")
            config_changed = True

    auth_changed = False
    oauth_present = False
    if auth_json.exists():
        try:
            raw = auth_json.read_text(encoding="utf-8")
            parsed = json.loads(raw) if raw.strip() else {}
            if not isinstance(parsed, dict):
                parsed = {}
        except Exception:
            parsed = {}
        if "OPENAI_API_KEY" in parsed:
            del parsed["OPENAI_API_KEY"]
            auth_changed = True
        oauth_present = any(k in parsed for k in ("tokens", "last_refresh", "account_id"))
        desired_auth_mode = "chatgpt" if oauth_present else None
        if desired_auth_mode:
            if parsed.get("auth_mode") != desired_auth_mode:
                parsed["auth_mode"] = desired_auth_mode
                auth_changed = True
        elif "auth_mode" in parsed:
            del parsed["auth_mode"]
            auth_changed = True
        if auth_changed:
            auth_json.write_text(
                json.dumps(parsed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    if config_changed or auth_changed:
        log_bus.write("SUCCESS", f"已恢复 Codex 官方配置：{config_toml}")
    else:
        log_bus.write("INFO", "未发现 anyroute 托管的 Codex 配置，无需恢复。")
    if oauth_present:
        log_bus.write("INFO", "auth.json 已保留 ChatGPT OAuth 令牌，重启 Codex 即可继续使用 Plus 订阅。")
    else:
        log_bus.write(
            "WARN",
            "auth.json 中未发现 ChatGPT OAuth 登录字段（tokens/account_id 等）。"
            "若需使用 ChatGPT Plus 订阅，请运行 `codex login` 完成登录。",
        )
    provider_changed, provider_count = _patch_codex_threads_provider(codex_dir, "openai")
    if provider_changed:
        log_bus.write("SUCCESS", f"已把 {provider_count} 条本地聊天索引切到官方Plus模式，确保Plus侧可见。")
    sync_codex_history_for_current_mode(AppConfig.load(), "恢复Plus配置")
    history_changed, total_threads, synced_threads = _patch_codex_shared_history_state(codex_dir)
    if total_threads > 0:
        level = "SUCCESS" if history_changed else "INFO"
        log_bus.write(level, f"已保持Plus/API本地聊天索引（修复 {synced_threads}/{total_threads} 条）。")
    return config_changed, auth_changed, oauth_present


# ---------------------------------------------------------------------------
# Codex setup diagnostics
# ---------------------------------------------------------------------------
# Surfaces the most common reasons Codex bypasses our local proxy and lands
# on `wss://api.openai.com` with a 401 — env var override, missing fields in
# config.toml, OAuth tokens in auth.json, etc. Used by the GUI button on the
# Forwarder Service page so the user can self-diagnose without reading docs.

DIAG_OK = "ok"
DIAG_WARN = "warn"
DIAG_ERROR = "error"


def _read_codex_files() -> Tuple[Optional[str], Optional[Dict[str, Any]], Path, Path]:
    codex_dir = Path.home() / ".codex"
    config_toml = codex_dir / "config.toml"
    auth_json = codex_dir / "auth.json"
    cfg_text: Optional[str] = None
    auth_data: Optional[Dict[str, Any]] = None
    try:
        if config_toml.exists():
            cfg_text = config_toml.read_text(encoding="utf-8")
    except Exception:
        cfg_text = None
    try:
        if auth_json.exists():
            raw = auth_json.read_text(encoding="utf-8").strip()
            auth_data = json.loads(raw) if raw else {}
            if not isinstance(auth_data, dict):
                auth_data = {}
    except Exception:
        auth_data = None
    return cfg_text, auth_data, config_toml, auth_json


def _toml_has_uncommented_line(text: str, needle: str) -> bool:
    """
    Lightweight scan for an active (non-commented) toml line. Avoids pulling
    in tomllib just to grep for a few keys.
    """
    if not text:
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if needle in line:
            return True
    return False


def _toml_extract_value(text: str, key: str) -> Optional[str]:
    """Return the right-hand value of `key = ...` (string, bool, or number)."""
    if not text:
        return None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        left, _, right = line.partition("=")
        if left.strip() == key:
            value = right.strip()
            # Strip trailing inline comment.
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            # Unquote string values.
            if (value.startswith('"') and value.endswith('"')) or (
                value.startswith("'") and value.endswith("'")
            ):
                value = value[1:-1]
            return value
    return None


def _detect_codex_cli() -> Tuple[bool, str]:
    """Try to run `codex --version`; returns (found, output_or_error)."""
    import shutil
    import subprocess
    import sys

    binary = shutil.which("codex")
    if not binary:
        return False, "未在 PATH 中找到 codex 命令"

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        if sys.platform == "win32":
            # On Windows shutil.which("codex") may resolve to either
            # `...\npm\codex` (npm's extension-less bash shim — list-argv
            # subprocess fails with WinError 193) or `...\npm\codex.cmd`
            # (also fails list-argv). Routing through cmd.exe with a
            # quoted-string command handles both shapes uniformly. The
            # binary path comes straight from PATH lookup so we don't have
            # to worry about user-supplied shell metacharacters.
            proc = subprocess.run(
                f'"{binary}" --version',
                capture_output=True,
                text=True,
                timeout=5,
                shell=True,
                creationflags=creationflags,
            )
        else:
            proc = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        out = (proc.stdout or proc.stderr or "").strip()
        if proc.returncode == 0:
            return True, out or binary
        return False, f"codex --version exit={proc.returncode}: {out}"
    except Exception as e:
        return False, f"调用 codex 失败：{e}"


def diagnose_codex_setup(cfg: AppConfig) -> List[Tuple[str, str, str]]:
    """
    Returns [(check_name, status, detail)] where status in {ok, warn, error}.
    The GUI renders these as a color-coded list. Order matters: highest-impact
    checks first so the user reads the most likely root cause at the top.
    """
    results: List[Tuple[str, str, str]] = []

    expected_key = cfg.gateway_key or DEFAULT_GATEWAY_KEY
    expected_port = int(cfg.listen_port)

    # 1) shell env var — silently overrides auth.json (openai/codex#15151)
    env_key = os.environ.get("OPENAI_API_KEY", "")
    if not env_key:
        results.append(("环境变量 OPENAI_API_KEY", DIAG_OK, "未设置（推荐）"))
    elif env_key == expected_key:
        results.append(
            (
                "环境变量 OPENAI_API_KEY",
                DIAG_OK,
                f"等于网关 Key（{expected_key}），不会和 auth.json 冲突。",
            )
        )
    else:
        results.append(
            (
                "环境变量 OPENAI_API_KEY",
                DIAG_ERROR,
                "shell 里设置了 OPENAI_API_KEY 且不等于本地网关 Key。"
                " 这会让 Codex 把这个值当成 OpenAI API key，请求落到 api.openai.com。\n"
                "  PowerShell 临时清除：Remove-Item Env:OPENAI_API_KEY\n"
                "  PowerShell 永久清除：[Environment]::SetEnvironmentVariable('OPENAI_API_KEY','','User')\n"
                "  随后关闭并重开终端再运行 codex。",
            )
        )

    cfg_text, auth_data, config_toml, auth_json = _read_codex_files()
    parsed_cfg_for_workspace: Optional[Dict[str, Any]] = None

    # 2) ~/.codex/config.toml exists
    if cfg_text is None:
        results.append(
            (
                "~/.codex/config.toml",
                DIAG_ERROR,
                f"未找到 {config_toml}。请先点'一键写入 Codex 配置'。",
            )
        )
    else:
        results.append(("~/.codex/config.toml", DIAG_OK, str(config_toml)))

        parsed_cfg, parse_err = _parse_codex_config(cfg_text)
        if parsed_cfg is None:
            results.append(
                (
                    "config.toml TOML 解析",
                    DIAG_ERROR,
                    f"Codex 无法解析 config.toml：{parse_err}。请重新写入配置。",
                )
            )
        else:
            results.append(("config.toml TOML 解析", DIAG_OK, "解析成功，以下检查使用真实 TOML 结构。"))
            parsed_cfg_for_workspace = parsed_cfg

            # 3) Top-level model_provider points at codex-anyroute.
            provider = parsed_cfg.get("model_provider")
            if provider == "codex-anyroute":
                results.append(("model_provider = \"codex-anyroute\"", DIAG_OK, "顶层已生效。"))
            else:
                hint = ""
                if "codex-anyroute" in cfg_text:
                    hint = " 检测到文件里有 anyroute 字样，但它可能被写在某个 TOML 表下面，不是顶层。"
                results.append(
                    (
                        "model_provider = \"codex-anyroute\"",
                        DIAG_ERROR,
                        f"当前顶层 model_provider = {provider!r}，Codex 会走默认 OpenAI 上游。{hint}请重新写入。",
                    )
                )

            if parsed_cfg.get("forced_login_method") or parsed_cfg.get("cli_auth_credentials_store"):
                results.append(
                    (
                        "Codex 官方登录页面",
                        DIAG_WARN,
                        "检测到旧版强制 API 登录字段。请重新写入配置，程序会移除这些字段，改由 auth.json 显示 API 登录页面。",
                    )
                )
            else:
                results.append(("Codex 官方登录页面", DIAG_OK, "未使用旧版强制登录字段，API 页面由 auth.json 控制。"))


            # 4) base_url in [model_providers.codex-anyroute] points at our port.
            providers = parsed_cfg.get("model_providers") or {}
            provider_cfg = providers.get("codex-anyroute") if isinstance(providers, dict) else None
            if not isinstance(provider_cfg, dict):
                results.append(
                    (
                        "[model_providers.codex-anyroute]",
                        DIAG_ERROR,
                        "config.toml 缺少可用的 [model_providers.codex-anyroute] 段。请重新写入配置。",
                    )
                )
            else:
                base_url = str(provider_cfg.get("base_url") or "")
                if f"127.0.0.1:{expected_port}" in base_url:
                    results.append(("base_url 指向本地", DIAG_OK, base_url))
                else:
                    results.append(
                        (
                            "base_url 指向本地",
                            DIAG_ERROR,
                            f"当前 base_url = {base_url!r}，期望包含 127.0.0.1:{expected_port}。"
                            "可能你最近改了端口但没重新写入 Codex 配置。",
                        )
                    )

                bearer = str(provider_cfg.get("experimental_bearer_token") or "")
                if bearer == expected_key:
                    results.append(("provider bearer token", DIAG_OK, "已直接使用本地网关 Key，不依赖环境变量。"))
                else:
                    results.append(
                        (
                            "provider bearer token",
                            DIAG_ERROR,
                            f"当前值不是本地网关 Key。期望 {expected_key!r}。请重新写入配置。",
                        )
                    )

                env_key_name = provider_cfg.get("env_key")
                if env_key_name:
                    results.append(
                        (
                            "provider env_key",
                            DIAG_WARN,
                            f"仍存在 env_key = {env_key_name!r}。新版配置应移除它，否则 Codex 会要求终端环境变量。",
                        )
                    )
                else:
                    results.append(("provider env_key", DIAG_OK, "未使用环境变量，避免终端环境覆盖。"))

                roa = provider_cfg.get("requires_openai_auth")
                if roa is False:
                    results.append(("requires_openai_auth = false", DIAG_OK, "已关闭 OpenAI 认证回退。"))
                else:
                    results.append(
                        (
                            "requires_openai_auth = false",
                            DIAG_ERROR,
                            f"当前值 = {roa!r}（缺失或 true）。Codex 可能强行走 ChatGPT/OpenAI 认证。",
                        )
                    )

                sw = provider_cfg.get("supports_websockets")
                if sw is False:
                    results.append(("supports_websockets = false", DIAG_OK, "已禁用 WS realtime。"))
                else:
                    results.append(
                        (
                            "supports_websockets = false",
                            DIAG_WARN,
                            f"当前值 = {sw!r}。Codex 可能尝试 wss:// 升级。请重新写入配置。",
                        )
                    )

    # 7) auth.json — preserve official login state
    if auth_data is None:
        results.append(
            (
                "~/.codex/auth.json",
                DIAG_WARN,
                f"未找到 {auth_json}。转发仍可由 config.toml 控制，但 Codex 设置菜单不会显示 API 登录页。",
            )
        )
    else:
        auth_mode = str(auth_data.get("auth_mode") or "")
        if auth_mode == "chatgpt":
            results.append(("auth.json 的 auth_mode", DIAG_WARN, "当前是 chatgpt，所以设置菜单会显示 Plus/OAuth 入口而不是 API Key 入口。"))
        elif auth_mode == "apikey":
            results.append(
                (
                    "auth.json 的 auth_mode",
                    DIAG_OK,
                    "apikey，Codex 设置菜单应显示“已通过 API 密钥登录/退出登录”。",
                )
            )
        else:
            results.append(
                (
                    "auth.json 的 auth_mode",
                    DIAG_WARN,
                    f"当前 auth_mode = {auth_mode!r}。若设置菜单不是 API Key 页面，请重新写入配置。",
                )
            )

        api_key = str(auth_data.get("OPENAI_API_KEY") or "")
        if not api_key:
            results.append(("auth.json 的 OPENAI_API_KEY", DIAG_WARN, "未写入。设置菜单通常不会显示 API Key 登录状态。"))
        elif api_key == expected_key:
            results.append(
                (
                    "auth.json 的 OPENAI_API_KEY",
                    DIAG_OK,
                    "已写入本地网关 Key；真正的上游 AnyRouter Key 仍只保存在本软件配置里。",
                )
            )
        elif api_key:
            results.append(
                (
                    "auth.json 的 OPENAI_API_KEY",
                    DIAG_WARN,
                    "检测到其他 API key。它可能让 Codex 菜单显示 API 登录，但不会使用本地网关 Key。",
                )
            )

        # 8) OAuth residue
        oauth_keys = [k for k in ("tokens", "last_refresh", "account_id") if k in auth_data]
        if oauth_keys:
            results.append(("auth.json 的 ChatGPT OAuth", DIAG_OK, "检测到字段 " + ", ".join(oauth_keys) + "，Plus 登录令牌已保留，可恢复官方配置。"))
        else:
            results.append(("auth.json 无 OAuth 残留", DIAG_OK, "未发现 tokens / account_id 字段。"))

    docs_root = str(Path.home() / "Documents" / "Codex").lower()
    projects = parsed_cfg_for_workspace.get("projects") if isinstance(parsed_cfg_for_workspace, dict) else None
    if isinstance(projects, dict) and any(str(path).lower() == docs_root for path in projects.keys()):
        results.append(("Documents\\Codex 工作区信任", DIAG_OK, "父目录已写入 [projects] trust_level，减少新对话重复初始化。"))
    else:
        results.append(("Documents\\Codex 工作区信任", DIAG_WARN, "未检测到父目录信任配置。请重新写入 Codex 配置。"))

    state_path = Path.home() / ".codex" / ".codex-global-state.json"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    except Exception as e:
        state = {}
        results.append(("Codex 桌面工作区状态", DIAG_WARN, f"无法读取 {state_path}：{e}"))
    if isinstance(state, dict):
        saved_roots = state.get("electron-saved-workspace-roots")
        root_saved = isinstance(saved_roots, list) and any(str(path).lower() == docs_root for path in saved_roots)
        if root_saved:
            results.append(("Codex 保存工作区", DIAG_OK, "Documents\\Codex 已在 Codex App 保存工作区列表里。"))
        else:
            results.append(("Codex 保存工作区", DIAG_WARN, "Documents\\Codex 尚未写入 Codex App 保存工作区列表。请重新写入配置。"))
        active_roots = state.get("active-workspace-roots")
        root_active = isinstance(active_roots, list) and len(active_roots) == 1 and str(active_roots[0]).lower() == docs_root
        if root_active:
            results.append(("Codex 当前工作区", DIAG_OK, "active-workspace-roots 已固定到 Documents\\Codex。"))
        else:
            results.append(("Codex 当前工作区", DIAG_WARN, "当前 active-workspace-roots 不是 Documents\\Codex，新 API 对话可能继续触发工作空间设置。"))
        persisted = state.get("electron-persisted-atom-state")
        if isinstance(persisted, dict) and persisted.get("electron:onboarding-welcome-pending") is False and persisted.get("electron:onboarding-primary-runtime-install-ready") is True:
            results.append(("Codex 工作区初始化标记", DIAG_OK, "欢迎页和 primary runtime 初始化标记已设为完成。"))
        else:
            results.append(("Codex 工作区初始化标记", DIAG_WARN, "初始化标记未完成，可能继续出现“正在设置你的工作空间”。请重新写入配置并重启 Codex。"))

    total_threads, covered_threads, history_err = _codex_shared_history_coverage(Path.home() / ".codex")
    if history_err:
        results.append(("Plus/API本地聊天共享", DIAG_WARN, f"无法检查共享状态：{history_err}"))
    elif total_threads <= 0:
        results.append(("Plus/API本地聊天共享", DIAG_OK, "暂未发现未归档本地聊天记录。"))
    elif covered_threads >= total_threads:
        results.append(("Plus/API本地聊天共享", DIAG_OK, f"已同步 {covered_threads}/{total_threads} 条未归档聊天。"))
    else:
        results.append(("Plus/API本地聊天共享", DIAG_WARN, f"仅同步 {covered_threads}/{total_threads} 条未归档聊天。请重新写入配置或等待守护自动修复。"))

    total_provider, covered_provider, provider_err = _codex_thread_provider_coverage(Path.home() / ".codex", "codex-anyroute")
    if provider_err:
        results.append(("API侧聊天可见性", DIAG_WARN, f"无法检查线程provider：{provider_err}"))
    elif total_provider <= 0:
        results.append(("API侧聊天可见性", DIAG_OK, "暂未发现未归档本地聊天。"))
    elif covered_provider >= total_provider:
        results.append(("API侧聊天可见性", DIAG_OK, f"已同步 {covered_provider}/{total_provider} 条线程到AnyRoute provider。"))
    else:
        results.append(("API侧聊天可见性", DIAG_WARN, f"仅同步 {covered_provider}/{total_provider} 条线程到AnyRoute provider。请重新写入配置。"))

    # 9) Local forwarder reachable
    try:
        r = httpx.get(f"http://127.0.0.1:{expected_port}/", timeout=2.5)
        if r.status_code < 500:
            results.append(
                ("本地转发服务", DIAG_OK, f"http://127.0.0.1:{expected_port}/ → HTTP {r.status_code}")
            )
        else:
            results.append(
                ("本地转发服务", DIAG_WARN, f"返回 HTTP {r.status_code}，转发服务可能异常。")
            )
    except Exception as e:
        results.append(
            (
                "本地转发服务",
                DIAG_ERROR,
                f"无法连接 http://127.0.0.1:{expected_port}/：{e}。请先启动转发服务。",
            )
        )

    # 10) Codex CLI installed
    found, info = _detect_codex_cli()
    results.append(
        (
            "Codex CLI",
            DIAG_OK if found else DIAG_WARN,
            info if found else f"{info}（请确认已 `npm i -g @openai/codex`）",
        )
    )

    return results


class MainWindow:
    # === Tactical Console palette ===
    # Cool deep base layered with electric blue and cyan accents.
    # The intent is "premium developer tooling": clean, technical, distinctive,
    # without copying the GitHub palette one-to-one.
    COLOR_BG = "#0a1119"
    COLOR_SURFACE = "#121922"
    COLOR_SURFACE_RAISED = "#1a2230"
    COLOR_SURFACE_HOVER = "#1c2536"
    COLOR_BORDER = "#2c3548"
    COLOR_BORDER_SOFT = "#1d2533"
    COLOR_BORDER_STRONG = "#3a4559"
    COLOR_TEXT = "#c4cdd9"
    COLOR_TEXT_MUTED = "#7d8699"
    COLOR_TEXT_BRIGHT = "#f1f5fc"
    COLOR_TEXT_DIM = "#525c70"
    COLOR_PRIMARY = "#4493f8"
    COLOR_PRIMARY_HOVER = "#5ba3ff"
    COLOR_PRIMARY_DEEP = "#1d4ed8"
    COLOR_ACCENT = "#22d3ee"
    COLOR_ACCENT_SOFT = "#0e7490"
    COLOR_SUCCESS = "#22c55e"
    COLOR_SUCCESS_HOVER = "#34d979"
    COLOR_DANGER = "#ef4444"
    COLOR_DANGER_HOVER = "#f87171"
    COLOR_WARNING = "#f59e0b"
    COLOR_WARNING_HOVER = "#fbbf24"
    COLOR_NEUTRAL = "#2d3748"
    COLOR_NEUTRAL_HOVER = "#3a4555"
    COLOR_SIDEBAR = "#070b12"
    COLOR_SIDEBAR_ACTIVE = "#1a2230"
    COLOR_SIDEBAR_HOVER = "#0f1521"
    COLOR_INPUT_BG = "#0a121e"
    COLOR_INPUT_BORDER = "#2c3548"
    COLOR_INPUT_FOCUS = "#4493f8"

    # Default font family attribute names. Concrete families are picked at
    # runtime via _detect_fonts() so we can opportunistically use Segoe UI
    # Variable / JetBrains Mono / Cascadia Code when they're installed without
    # breaking on machines that only ship the classic stack.
    FONT_FAMILY = "Microsoft YaHei UI"
    FONT_DISPLAY = "Segoe UI Semibold"
    FONT_MONO = "Consolas"

    NAV_ITEMS: List[Tuple[str, str, str]] = [
        ("provider", "◆", "提供商"),
        ("mapping", "⇄", "模型映射"),
        ("proxy", "✦", "转发服务"),
        ("logs", "≡", "运行日志"),
    ]

    def __init__(self) -> None:
        self._enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("1320x840")
        self.root.minsize(1100, 740)
        self.root.configure(bg=self.COLOR_BG)
        try:
            self.root.tk.call("tk", "scaling", 1.35)
        except Exception:
            pass
        # Pick the best available fonts before any widget is built.
        self._detect_fonts()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)
        self.tray_icon: Optional[pystray.Icon] = None
        self.views: Dict[str, tk.Frame] = {}
        self.nav_buttons: Dict[str, Tuple[tk.Frame, tk.Label, tk.Label, tk.Frame]] = {}
        self.current_view: str = ""
        self.status_pill_frame: Optional[tk.Frame] = None
        self.status_pill_dot: Optional[tk.Label] = None
        self.status_pill_text: Optional[tk.Label] = None
        self.status_bar_state: Optional[tk.Label] = None
        self.status_bar_meta: Optional[tk.Label] = None
        self.status_bar_endpoint: Optional[tk.Label] = None
        self._pulse_phase: int = 0
        self.build_ui()
        self.load_to_form()
        self.show_view("provider")
        self.root.after(300, self.drain_logs)
        self.root.after(700, self._refresh_status)
        self.root.after(900, self._tick_status_pulse)

    @staticmethod
    def _enable_dpi_awareness() -> None:
        try:
            from ctypes import windll  # type: ignore[attr-defined]
            try:
                windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    def _detect_fonts(self) -> None:
        """Pick the best available fonts for body / display / mono.

        Falls back gracefully on machines that don't have the modern Windows
        Segoe UI Variable family or developer-grade monospace fonts installed.
        """
        try:
            from tkinter import font as tkfont
            available = set(tkfont.families(self.root))
        except Exception:
            available = set()

        def pick(candidates: List[str], fallback: str) -> str:
            for name in candidates:
                if name in available:
                    return name
            return fallback

        self.font_body = pick(
            [
                "Segoe UI Variable Text",
                "Segoe UI",
                "Microsoft YaHei UI",
                "Microsoft YaHei",
                "PingFang SC",
                "Noto Sans CJK SC",
            ],
            self.FONT_FAMILY,
        )
        self.font_display = pick(
            [
                "Segoe UI Variable Display",
                "Segoe UI Semibold",
                "Segoe UI",
                "Microsoft YaHei UI",
                "Microsoft YaHei",
            ],
            self.FONT_DISPLAY,
        )
        self.font_mono = pick(
            [
                "JetBrains Mono",
                "Cascadia Code",
                "Cascadia Mono",
                "Fira Code",
                "Consolas",
                "Courier New",
            ],
            self.FONT_MONO,
        )

    # ------------------------------------------------------------------
    # Reusable UI helpers
    # ------------------------------------------------------------------

    def _font(self, size: int = 11, bold: bool = False, mono: bool = False, display: bool = False) -> Tuple[str, int, str]:
        if mono:
            family = self.font_mono
        elif display:
            family = self.font_display
        else:
            family = self.font_body
        weight = "bold" if bold else "normal"
        return (family, size, weight)

    def _hover_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Any,
        bg: str,
        hover_bg: str,
        fg: str = "white",
        padx: int = 22,
        pady: int = 11,
        font: Optional[Tuple[str, int, str]] = None,
        width: Optional[int] = None,
    ) -> tk.Button:
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=bg,
            fg=fg,
            activebackground=hover_bg,
            activeforeground=fg,
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=padx,
            pady=pady,
            cursor="hand2",
            font=font or self._font(10, bold=True),
        )
        if width is not None:
            btn.configure(width=width)
        btn.bind("<Enter>", lambda e: btn.configure(bg=hover_bg))
        btn.bind("<Leave>", lambda e: btn.configure(bg=bg))
        return btn

    def _ghost_button(
        self,
        parent: tk.Widget,
        text: str,
        command: Any,
        padx: int = 18,
        pady: int = 9,
        font: Optional[Tuple[str, int, str]] = None,
    ) -> tk.Button:
        """Subtle outlined button — used when an action is secondary or
        documentary (e.g. clear logs, copy preview)."""
        btn = tk.Button(
            parent,
            text=text,
            command=command,
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT,
            activebackground=self.COLOR_SURFACE_HOVER,
            activeforeground=self.COLOR_TEXT_BRIGHT,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
            highlightcolor=self.COLOR_BORDER_STRONG,
            padx=padx,
            pady=pady,
            cursor="hand2",
            font=font or self._font(10, bold=True),
        )
        btn.bind("<Enter>", lambda e: btn.configure(bg=self.COLOR_SURFACE_HOVER, fg=self.COLOR_TEXT_BRIGHT, highlightbackground=self.COLOR_BORDER_STRONG))
        btn.bind("<Leave>", lambda e: btn.configure(bg=self.COLOR_SURFACE, fg=self.COLOR_TEXT, highlightbackground=self.COLOR_BORDER))
        return btn

    def _card(self, parent: tk.Widget) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg=self.COLOR_SURFACE,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
            highlightcolor=self.COLOR_BORDER,
        )
        return frame

    def _pill_badge(self, parent: tk.Widget, text: str, fg: str, bg: str) -> tk.Label:
        """Small uppercase pill used to tag a section header (e.g. 01 · 必填)."""
        return tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=fg,
            font=self._font(8, bold=True),
            padx=8,
            pady=2,
        )

    def _section_title(
        self,
        parent: tk.Widget,
        title: str,
        subtitle: str = "",
        index: Optional[str] = None,
        tag: Optional[Tuple[str, str, str]] = None,
    ) -> None:
        head = tk.Frame(parent, bg=self.COLOR_SURFACE)
        head.pack(fill="x", pady=(0, 18))

        # Top row: optional index chip + title + optional tag
        top = tk.Frame(head, bg=self.COLOR_SURFACE)
        top.pack(fill="x")
        if index:
            tk.Label(
                top,
                text=index,
                bg=self.COLOR_SURFACE,
                fg=self.COLOR_ACCENT,
                font=self._font(9, bold=True, mono=True),
            ).pack(side="left", padx=(0, 10), pady=(4, 0))
        tk.Label(
            top,
            text=title,
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_BRIGHT,
            font=self._font(16, bold=True, display=True),
        ).pack(side="left")
        if tag is not None:
            label_text, fg_color, bg_color = tag
            self._pill_badge(top, label_text, fg_color, bg_color).pack(
                side="left", padx=(12, 0), pady=(4, 0)
            )

        if subtitle:
            tk.Label(
                head,
                text=subtitle,
                bg=self.COLOR_SURFACE,
                fg=self.COLOR_TEXT_MUTED,
                font=self._font(10),
                justify="left",
                wraplength=820,
            ).pack(anchor="w", pady=(6, 0))

        # Accent underline that visually anchors the section title.
        accent = tk.Frame(head, bg=self.COLOR_PRIMARY, height=2, width=44)
        accent.pack(anchor="w", pady=(12, 0))

    def _divider(self, parent: tk.Widget, pady: Tuple[int, int] = (12, 18)) -> None:
        line = tk.Frame(parent, bg=self.COLOR_BORDER_SOFT, height=1)
        line.pack(fill="x", pady=pady)

    def _field_label(self, parent: tk.Widget, text: str) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_BRIGHT,
            font=self._font(10, bold=True),
        )

    def _field_help(self, parent: tk.Widget, text: str, color: Optional[str] = None) -> tk.Label:
        return tk.Label(
            parent,
            text=text,
            bg=self.COLOR_SURFACE,
            fg=color or self.COLOR_TEXT_MUTED,
            font=self._font(9),
            justify="left",
            wraplength=820,
        )

    def _styled_entry(self, parent: tk.Widget, show: Optional[str] = None, mono: bool = True) -> tk.Entry:
        wrapper = tk.Frame(
            parent,
            bg=self.COLOR_INPUT_BG,
            highlightthickness=1,
            highlightbackground=self.COLOR_INPUT_BORDER,
            highlightcolor=self.COLOR_INPUT_FOCUS,
        )
        wrapper.pack(fill="x", pady=(6, 0))
        entry = tk.Entry(
            wrapper,
            bg=self.COLOR_INPUT_BG,
            fg=self.COLOR_TEXT_BRIGHT,
            insertbackground=self.COLOR_PRIMARY,
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self._font(11, mono=mono),
            show=show,
        )
        entry.pack(fill="x", padx=14, pady=11)

        # Subtle focus highlight on the wrapper (border color)
        def _focus_in(_):
            wrapper.configure(highlightbackground=self.COLOR_INPUT_FOCUS)

        def _focus_out(_):
            wrapper.configure(highlightbackground=self.COLOR_INPUT_BORDER)

        entry.bind("<FocusIn>", _focus_in)
        entry.bind("<FocusOut>", _focus_out)
        # Stash wrapper on entry so layout adjusts apply uniformly
        entry._wrapper = wrapper  # type: ignore[attr-defined]
        return entry

    # ------------------------------------------------------------------
    # Top-level layout
    # ------------------------------------------------------------------

    def build_ui(self) -> None:
        # ttk style baseline (used by Checkbutton, etc.)
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure(
            "Modern.TCheckbutton",
            background=self.COLOR_SURFACE,
            foreground=self.COLOR_TEXT,
            font=self._font(10),
            padding=4,
            indicatorbackground=self.COLOR_INPUT_BG,
            indicatorforeground=self.COLOR_PRIMARY,
            focuscolor=self.COLOR_PRIMARY,
        )
        style.map(
            "Modern.TCheckbutton",
            background=[("active", self.COLOR_SURFACE)],
            foreground=[("active", self.COLOR_TEXT_BRIGHT)],
        )
        style.configure(
            "Modern.Vertical.TScrollbar",
            background=self.COLOR_SIDEBAR_HOVER,
            troughcolor=self.COLOR_BG,
            bordercolor=self.COLOR_BG,
            arrowcolor=self.COLOR_TEXT_MUTED,
            relief="flat",
        )

        # Root layout: header / body (sidebar + content) / status bar
        self._build_header()
        body = tk.Frame(self.root, bg=self.COLOR_BG)
        body.pack(fill="both", expand=True)
        self._build_sidebar(body)
        self.content_container = tk.Frame(body, bg=self.COLOR_BG)
        self.content_container.pack(side="left", fill="both", expand=True, padx=(0, 28), pady=(20, 12))
        self._build_provider_view()
        self._build_mapping_view()
        self._build_proxy_view()
        self._build_logs_view()
        self._build_status_bar()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=self.COLOR_SURFACE, height=84)
        header.pack(fill="x")
        header.pack_propagate(False)

        # Brand block on the left
        brand = tk.Frame(header, bg=self.COLOR_SURFACE)
        brand.pack(side="left", padx=30, pady=14)

        # Stacked monogram: outer accent square + inner deep square + AR mark
        logo_outer = tk.Frame(brand, bg=self.COLOR_PRIMARY_DEEP, width=44, height=44)
        logo_outer.pack(side="left")
        logo_outer.pack_propagate(False)
        logo_inner = tk.Frame(logo_outer, bg=self.COLOR_PRIMARY, width=38, height=38)
        logo_inner.place(relx=0.5, rely=0.5, anchor="center")
        logo_inner.pack_propagate(False)
        tk.Label(
            logo_inner,
            text="AR",
            bg=self.COLOR_PRIMARY,
            fg="white",
            font=self._font(13, bold=True, display=True),
        ).place(relx=0.5, rely=0.5, anchor="center")

        text_box = tk.Frame(brand, bg=self.COLOR_SURFACE)
        text_box.pack(side="left", padx=(16, 0))
        title_row = tk.Frame(text_box, bg=self.COLOR_SURFACE)
        title_row.pack(anchor="w")
        tk.Label(
            title_row,
            text="Codex AnyRouter",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_BRIGHT,
            font=self._font(16, bold=True, display=True),
        ).pack(side="left")
        tk.Label(
            title_row,
            text="Transfer",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_ACCENT,
            font=self._font(16, bold=True, display=True),
        ).pack(side="left", padx=(6, 0))
        # Tiny version-style chip after the wordmark
        version_chip = tk.Frame(
            title_row,
            bg=self.COLOR_BORDER_SOFT,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
        )
        version_chip.pack(side="left", padx=(10, 0), pady=(2, 0))
        tk.Label(
            version_chip,
            text="LOCAL · 18180",
            bg=self.COLOR_BORDER_SOFT,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(8, bold=True, mono=True),
            padx=8,
            pady=2,
        ).pack()

        tk.Label(
            text_box,
            text="本地中转  ·  模型优先级编排  ·  Codex 配置一键注入",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(9),
        ).pack(anchor="w", pady=(4, 0))

        # Status pill on the right
        right = tk.Frame(header, bg=self.COLOR_SURFACE)
        right.pack(side="right", padx=30, pady=14)

        self.status_pill_frame = tk.Frame(
            right,
            bg=self.COLOR_BORDER_SOFT,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
        )
        self.status_pill_frame.pack(side="right")
        inner = tk.Frame(self.status_pill_frame, bg=self.COLOR_BORDER_SOFT, padx=14, pady=7)
        inner.pack()
        self.status_pill_dot = tk.Label(
            inner,
            text="●",
            bg=self.COLOR_BORDER_SOFT,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(11, bold=True),
        )
        self.status_pill_dot.pack(side="left", padx=(0, 9))
        self.status_pill_text = tk.Label(
            inner,
            text="未启动",
            bg=self.COLOR_BORDER_SOFT,
            fg=self.COLOR_TEXT,
            font=self._font(10, bold=True),
        )
        self.status_pill_text.pack(side="left")

        # Underline separator with a faint accent stripe
        sep = tk.Frame(self.root, bg=self.COLOR_BORDER, height=1)
        sep.pack(fill="x")
        accent_stripe = tk.Frame(self.root, bg=self.COLOR_BG, height=2)
        accent_stripe.pack(fill="x")

    def _build_sidebar(self, parent: tk.Widget) -> None:
        sidebar = tk.Frame(parent, bg=self.COLOR_SIDEBAR, width=240)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        # Section label — primary nav
        tk.Label(
            sidebar,
            text="导航  /  NAVIGATION",
            bg=self.COLOR_SIDEBAR,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(8, bold=True, mono=True),
        ).pack(anchor="w", padx=24, pady=(28, 12))

        for name, icon, label in self.NAV_ITEMS:
            self._make_nav_item(sidebar, name, icon, label)

        # Spacer pushes the action block to the bottom
        tk.Frame(sidebar, bg=self.COLOR_SIDEBAR).pack(fill="y", expand=True)

        # Section label — actions
        tk.Label(
            sidebar,
            text="操作  /  CONTROLS",
            bg=self.COLOR_SIDEBAR,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(8, bold=True, mono=True),
        ).pack(anchor="w", padx=24, pady=(0, 10))
        action_box = tk.Frame(sidebar, bg=self.COLOR_SIDEBAR)
        action_box.pack(fill="x", padx=18, pady=(0, 12))
        self._hover_button(
            action_box,
            "▶  启动转发",
            start_server,
            self.COLOR_SUCCESS,
            self.COLOR_SUCCESS_HOVER,
            padx=14,
            pady=10,
        ).pack(fill="x", pady=(0, 8))
        self._hover_button(
            action_box,
            "■  停止转发",
            stop_server,
            self.COLOR_DANGER,
            self.COLOR_DANGER_HOVER,
            padx=14,
            pady=10,
        ).pack(fill="x")

        # Tiny credit / hint at the very bottom of the sidebar
        credit = tk.Frame(sidebar, bg=self.COLOR_SIDEBAR)
        credit.pack(fill="x", padx=24, pady=(0, 18))
        tk.Label(
            credit,
            text="MIT License · Tactical Console",
            bg=self.COLOR_SIDEBAR,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(8, mono=True),
        ).pack(anchor="w")

    def _make_nav_item(self, parent: tk.Widget, name: str, icon: str, label: str) -> None:
        row = tk.Frame(parent, bg=self.COLOR_SIDEBAR)
        row.pack(fill="x", padx=10, pady=2)

        # Left active-indicator bar (hidden by default)
        bar = tk.Frame(row, bg=self.COLOR_SIDEBAR, width=3)
        bar.pack(side="left", fill="y")

        body = tk.Frame(row, bg=self.COLOR_SIDEBAR)
        body.pack(side="left", fill="both", expand=True)

        icon_lbl = tk.Label(
            body,
            text=icon,
            bg=self.COLOR_SIDEBAR,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(13, bold=True),
            padx=14,
            pady=11,
        )
        icon_lbl.pack(side="left")
        text_lbl = tk.Label(
            body,
            text=label,
            bg=self.COLOR_SIDEBAR,
            fg=self.COLOR_TEXT,
            font=self._font(11, bold=True),
            padx=2,
            pady=11,
        )
        text_lbl.pack(side="left", fill="x")

        widgets = (row, body, icon_lbl, text_lbl)

        def on_enter(_event=None):
            if self.current_view == name:
                return
            for w in widgets:
                w.configure(bg=self.COLOR_SIDEBAR_HOVER)
            text_lbl.configure(fg=self.COLOR_TEXT_BRIGHT)
            icon_lbl.configure(fg=self.COLOR_ACCENT)

        def on_leave(_event=None):
            if self.current_view == name:
                return
            for w in widgets:
                w.configure(bg=self.COLOR_SIDEBAR)
            text_lbl.configure(fg=self.COLOR_TEXT)
            icon_lbl.configure(fg=self.COLOR_TEXT_MUTED)

        def on_click(_event=None):
            self.show_view(name)

        for w in widgets:
            w.bind("<Enter>", on_enter)
            w.bind("<Leave>", on_leave)
            w.bind("<Button-1>", on_click)
            w.configure(cursor="hand2")

        self.nav_buttons[name] = (row, icon_lbl, text_lbl, bar)

    def show_view(self, name: str) -> None:
        # Clear any wheel binding left behind by a previous scrollable view
        try:
            self.root.unbind_all("<MouseWheel>")
        except Exception:
            pass
        self.current_view = name
        for view_name, frame in self.views.items():
            if view_name == name:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        for nav_name, (row, icon_lbl, text_lbl, bar) in self.nav_buttons.items():
            if nav_name == name:
                row.configure(bg=self.COLOR_SIDEBAR_ACTIVE)
                icon_lbl.configure(bg=self.COLOR_SIDEBAR_ACTIVE, fg=self.COLOR_ACCENT)
                text_lbl.configure(bg=self.COLOR_SIDEBAR_ACTIVE, fg=self.COLOR_TEXT_BRIGHT)
                bar.configure(bg=self.COLOR_PRIMARY)
                # Re-pack the parent of the nav row's body so the active background
                # also covers the body wrapper.
                try:
                    body_widget = icon_lbl.master
                    body_widget.configure(bg=self.COLOR_SIDEBAR_ACTIVE)
                except Exception:
                    pass
            else:
                row.configure(bg=self.COLOR_SIDEBAR)
                icon_lbl.configure(bg=self.COLOR_SIDEBAR, fg=self.COLOR_TEXT_MUTED)
                text_lbl.configure(bg=self.COLOR_SIDEBAR, fg=self.COLOR_TEXT)
                bar.configure(bg=self.COLOR_SIDEBAR)
                try:
                    body_widget = icon_lbl.master
                    body_widget.configure(bg=self.COLOR_SIDEBAR)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Scrollable view helper
    # ------------------------------------------------------------------

    def _scrollable_card_body(self, name: str) -> tk.Frame:
        """
        Build a card view that scrolls vertically when its content overflows.
        Returns the inner frame to pack content into.
        """
        view = self._make_view(name)
        card = self._card(view)
        card.pack(fill="both", expand=True)

        canvas = tk.Canvas(
            card,
            bg=self.COLOR_SURFACE,
            highlightthickness=0,
            bd=0,
        )
        scrollbar = ttk.Scrollbar(
            card,
            orient="vertical",
            command=canvas.yview,
            style="Modern.Vertical.TScrollbar",
        )
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(canvas, bg=self.COLOR_SURFACE, padx=34, pady=30)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def _on_canvas_resize(event: Any) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def _on_inner_resize(_event: Any) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        canvas.bind("<Configure>", _on_canvas_resize)
        inner.bind("<Configure>", _on_inner_resize)

        def _wheel(event: Any) -> str:
            try:
                # Windows/macOS deliver event.delta (multiple of 120). On Linux
                # use Button-4/5 — handled by separate bindings below.
                canvas.yview_scroll(int(-event.delta / 120), "units")
            except Exception:
                pass
            return "break"

        def _activate(_event: Any = None) -> None:
            canvas.bind_all("<MouseWheel>", _wheel)
            canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
            canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

        def _deactivate(_event: Any = None) -> None:
            try:
                canvas.unbind_all("<MouseWheel>")
                canvas.unbind_all("<Button-4>")
                canvas.unbind_all("<Button-5>")
            except Exception:
                pass

        canvas.bind("<Enter>", _activate)
        canvas.bind("<Leave>", _deactivate)
        inner.bind("<Enter>", _activate)
        inner.bind("<Leave>", _deactivate)

        return inner

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    def _make_view(self, name: str) -> tk.Frame:
        view = tk.Frame(self.content_container, bg=self.COLOR_BG)
        self.views[name] = view
        return view

    def _build_provider_view(self) -> None:
        body = self._scrollable_card_body("provider")

        self._section_title(
            body,
            "提供商配置",
            "按模型优先级依次尝试。Claude 模型走 /v1/messages，gpt 系列走 /v1/responses。",
            index="01",
            tag=("必填", self.COLOR_PRIMARY_HOVER, self.COLOR_BORDER_SOFT),
        )

        self._field_label(body, "API Base URL").pack(anchor="w")
        self.base_entry = self._styled_entry(body)
        self._field_help(body, "推荐填写 https://anyrouter.top（不带尾部 /v1）。").pack(anchor="w", pady=(6, 20))

        self._field_label(body, "API Key").pack(anchor="w")
        self.key_entry = self._styled_entry(body, show="•")
        self._field_help(
            body,
            "AnyRouter 控制台获取的密钥。本地保存在 %APPDATA%\\codex-anyroute\\config.json，不会写入仓库。",
        ).pack(anchor="w", pady=(6, 20))

        self._divider(body)

        # Model priority block — three labelled rows with rank chips
        priority_head = tk.Frame(body, bg=self.COLOR_SURFACE)
        priority_head.pack(fill="x")
        tk.Label(
            priority_head,
            text="模型优先级",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_BRIGHT,
            font=self._font(13, bold=True, display=True),
        ).pack(side="left")
        tk.Label(
            priority_head,
            text="FAILOVER · ORDERED",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_ACCENT,
            font=self._font(8, bold=True, mono=True),
            padx=10,
        ).pack(side="left", pady=(3, 0))
        tk.Label(
            body,
            text="按 第一 → 第二 → 第三 顺序请求，前一个失败时自动尝试下一个。",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(9),
        ).pack(anchor="w", pady=(2, 16))

        self.default_model_entry = self._priority_row(
            body,
            "第一模型",
            "PRIMARY",
            self.COLOR_PRIMARY,
            "默认 claude-opus-4-7。请在 AnyRouter 开启 1M 开关，软件会自动附加 1M beta header。",
            self.COLOR_WARNING,
        )

        self.fallback_model_entry = self._priority_row(
            body,
            "第二模型",
            "FALLBACK",
            self.COLOR_ACCENT,
            "默认 gpt-5.5（透传到 /v1/responses，最稳定）。",
            None,
        )

        self.third_model_entry = self._priority_row(
            body,
            "第三模型",
            "TERTIARY",
            self.COLOR_TEXT_MUTED,
            "默认 gpt-5.3-codex。仅当前两个模型都失败时使用。",
            None,
        )

        self.enable_fallback_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            body,
            text="启用模型自动切换：当前一个模型失败时，继续尝试下一个",
            variable=self.enable_fallback_var,
            style="Modern.TCheckbutton",
        ).pack(anchor="w", pady=(10, 18))

        self._divider(body)

        actions = tk.Frame(body, bg=self.COLOR_SURFACE)
        actions.pack(fill="x", pady=(2, 0))
        self._hover_button(
            actions, "保存配置", self._save_provider, self.COLOR_PRIMARY, self.COLOR_PRIMARY_HOVER
        ).pack(side="left")
        self._ghost_button(
            actions, "测试连接", self.test_connection
        ).pack(side="left", padx=12)

    def _priority_row(
        self,
        parent: tk.Widget,
        label: str,
        rank_label: str,
        rank_color: str,
        helper: str,
        helper_color: Optional[str],
    ) -> tk.Entry:
        """Render one priority slot (label + rank chip + entry + helper) and
        return the resulting Entry. The form-field naming contract elsewhere
        (default_model_entry / fallback_model_entry / third_model_entry) is
        preserved by the caller."""
        head = tk.Frame(parent, bg=self.COLOR_SURFACE)
        head.pack(fill="x")
        tk.Label(
            head,
            text=label,
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_BRIGHT,
            font=self._font(10, bold=True),
        ).pack(side="left")
        chip = tk.Frame(
            head,
            bg=self.COLOR_BORDER_SOFT,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
        )
        chip.pack(side="left", padx=(10, 0))
        tk.Label(
            chip,
            text=rank_label,
            bg=self.COLOR_BORDER_SOFT,
            fg=rank_color,
            font=self._font(8, bold=True, mono=True),
            padx=8,
            pady=2,
        ).pack()
        entry = self._styled_entry(parent)
        self._field_help(parent, helper, color=helper_color).pack(anchor="w", pady=(6, 14))
        return entry

    def _build_mapping_view(self) -> None:
        body = self._scrollable_card_body("mapping")

        self._section_title(
            body,
            "模型映射",
            "左侧是 Codex 看到的模型名，右侧是这条 Codex 模型对应的「第一上游模型」。第二、第三模型在「提供商」页统一设置。",
            index="02",
            tag=("可选", self.COLOR_TEXT_MUTED, self.COLOR_BORDER_SOFT),
        )

        # Header row
        head_row = tk.Frame(body, bg=self.COLOR_SURFACE)
        head_row.pack(fill="x", pady=(0, 4))
        tk.Label(
            head_row,
            text="CODEX 模型",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(8, bold=True, mono=True),
            width=20,
            anchor="w",
        ).pack(side="left")
        tk.Label(
            head_row,
            text="上游模型 / UPSTREAM",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(8, bold=True, mono=True),
            anchor="w",
        ).pack(side="left", fill="x", expand=True)
        # subtle horizontal rule under the header
        tk.Frame(body, bg=self.COLOR_BORDER_SOFT, height=1).pack(fill="x", pady=(4, 8))

        self.mapping_entries: Dict[str, tk.Entry] = {}
        for local in ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2"]:
            row = tk.Frame(body, bg=self.COLOR_SURFACE)
            row.pack(fill="x", pady=6)
            tk.Label(
                row,
                text=local,
                width=20,
                bg=self.COLOR_SURFACE,
                fg=self.COLOR_TEXT_BRIGHT,
                anchor="w",
                font=self._font(11, bold=True, mono=True),
            ).pack(side="left", anchor="n", pady=12)
            ent_holder = tk.Frame(row, bg=self.COLOR_SURFACE)
            ent_holder.pack(side="left", fill="x", expand=True)
            ent = self._styled_entry(ent_holder)
            self.mapping_entries[local] = ent

        self._divider(body, pady=(22, 16))
        self._hover_button(
            body, "保存映射", self._save_mapping, self.COLOR_PRIMARY, self.COLOR_PRIMARY_HOVER
        ).pack(anchor="w")

    def _build_proxy_view(self) -> None:
        body = self._scrollable_card_body("proxy")

        self._section_title(
            body,
            "转发服务",
            "本地端口默认 18180，避免和 Codex App Transfer 默认的 18080 冲突。",
            index="03",
            tag=("LOCAL", self.COLOR_ACCENT, self.COLOR_BORDER_SOFT),
        )

        self._field_label(body, "转发端口").pack(anchor="w")
        self.port_entry = self._styled_entry(body)
        self._field_help(body, "Codex 会被指向 http://127.0.0.1:<这个端口>/v1。").pack(anchor="w", pady=(6, 18))

        self._field_label(body, "本地网关 Key").pack(anchor="w")
        self.gateway_entry = self._styled_entry(body)
        self._field_help(
            body,
            "写入 provider bearer token 和 auth.json 的 API Key 外观。Codex 用这个值访问本地转发，不会发到 AnyRouter。",
        ).pack(anchor="w", pady=(6, 18))

        self.codex_auto_apply_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            body,
            text="保持 Codex AnyRoute API 模式：转发运行时自动修复 Codex 配置、API 登录外观和工作空间",
            variable=self.codex_auto_apply_var,
            style="Modern.TCheckbutton",
        ).pack(anchor="w", pady=(0, 18))

        self._divider(body)

        # Mini-terminal style preview block
        info = tk.Frame(body, bg=self.COLOR_SURFACE)
        info.pack(fill="x", pady=(0, 18))
        head = tk.Frame(info, bg=self.COLOR_SURFACE)
        head.pack(fill="x")
        tk.Label(
            head,
            text="Codex 配置预览",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_BRIGHT,
            font=self._font(11, bold=True),
        ).pack(side="left")
        tk.Label(
            head,
            text="~/.codex/config.toml",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(9, mono=True),
        ).pack(side="left", padx=(10, 0))

        terminal = tk.Frame(
            info,
            bg=self.COLOR_INPUT_BG,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
        )
        terminal.pack(fill="x", pady=(8, 0))
        # tiny window-chrome row (three dots) for visual character
        chrome = tk.Frame(terminal, bg=self.COLOR_SIDEBAR_HOVER, height=22)
        chrome.pack(fill="x")
        chrome.pack_propagate(False)
        for color in (self.COLOR_DANGER, self.COLOR_WARNING, self.COLOR_SUCCESS):
            tk.Label(
                chrome,
                text="●",
                bg=self.COLOR_SIDEBAR_HOVER,
                fg=color,
                font=self._font(8, bold=True),
                padx=4,
            ).pack(side="left", pady=(2, 0))
        tk.Label(
            chrome,
            text="config.toml — managed block",
            bg=self.COLOR_SIDEBAR_HOVER,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(8, mono=True),
        ).pack(side="left", padx=8)

        preview = tk.Label(
            terminal,
            text=(
                "model = \"gpt-5.5\"\n"
                "model_provider = \"codex-anyroute\"\n"
                "model_context_window = 1000000\n"
                "\n"
                "[model_providers.codex-anyroute]\n"
                "base_url = \"http://127.0.0.1:<port>/v1\"\n"
                "wire_api = \"responses\"\n"
                "experimental_bearer_token = \"<local-gateway-key>\""
            ),
            bg=self.COLOR_INPUT_BG,
            fg=self.COLOR_TEXT,
            font=self._font(10, mono=True),
            justify="left",
            padx=16,
            pady=14,
            anchor="w",
        )
        preview.pack(fill="x")

        actions = tk.Frame(body, bg=self.COLOR_SURFACE)
        actions.pack(fill="x")
        self._hover_button(
            actions,
            "一键写入 Codex 配置",
            self.apply_codex_config,
            self.COLOR_WARNING,
            self.COLOR_WARNING_HOVER,
            fg="#1a1208",
        ).pack(side="left")
        self._hover_button(
            actions,
            "切回官方 Plus 配置",
            self.restore_codex_official,
            self.COLOR_SUCCESS,
            self.COLOR_SUCCESS_HOVER,
        ).pack(side="left", padx=12)
        self._ghost_button(
            actions,
            "诊断 Codex 配置",
            self.diagnose_codex_config,
        ).pack(side="left", padx=(0, 12))
        self._hover_button(
            actions,
            "保存并重启转发",
            self.save_restart,
            self.COLOR_PRIMARY,
            self.COLOR_PRIMARY_HOVER,
        ).pack(side="left")

    def _build_logs_view(self) -> None:
        view = self._make_view("logs")
        card = self._card(view)
        card.pack(fill="both", expand=True)
        outer = tk.Frame(card, bg=self.COLOR_SURFACE, padx=26, pady=24)
        outer.pack(fill="both", expand=True)

        head = tk.Frame(outer, bg=self.COLOR_SURFACE)
        head.pack(fill="x", pady=(0, 14))

        title_box = tk.Frame(head, bg=self.COLOR_SURFACE)
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text="04",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_ACCENT,
            font=self._font(9, bold=True, mono=True),
        ).pack(side="left", padx=(0, 10), pady=(4, 0))
        tk.Label(
            title_box,
            text="运行日志",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_BRIGHT,
            font=self._font(16, bold=True, display=True),
        ).pack(side="left")

        # Live indicator chip
        live_chip = tk.Frame(
            head,
            bg=self.COLOR_BORDER_SOFT,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
        )
        live_chip.pack(side="left", padx=(12, 0), pady=(6, 0))
        live_inner = tk.Frame(live_chip, bg=self.COLOR_BORDER_SOFT, padx=10, pady=3)
        live_inner.pack()
        tk.Label(
            live_inner,
            text="●",
            bg=self.COLOR_BORDER_SOFT,
            fg=self.COLOR_SUCCESS_HOVER,
            font=self._font(8, bold=True),
        ).pack(side="left")
        tk.Label(
            live_inner,
            text="LIVE  ·  STREAMING",
            bg=self.COLOR_BORDER_SOFT,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(8, bold=True, mono=True),
            padx=6,
        ).pack(side="left")

        self._ghost_button(
            head,
            "清空显示",
            self._clear_logs,
            padx=14,
            pady=6,
            font=self._font(9, bold=True),
        ).pack(side="right")

        # Sub-row: tip about persistent logs on disk
        tip = tk.Frame(outer, bg=self.COLOR_SURFACE)
        tip.pack(fill="x", pady=(0, 12))
        tk.Label(
            tip,
            text="持久化日志写入  %APPDATA%\\codex-anyroute\\logs\\proxy-YYYY-MM-DD.log",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(9, mono=True),
        ).pack(anchor="w")

        accent = tk.Frame(outer, bg=self.COLOR_PRIMARY, height=2, width=44)
        accent.pack(anchor="w", pady=(0, 14))

        text_holder = tk.Frame(
            outer,
            bg=self.COLOR_INPUT_BG,
            highlightthickness=1,
            highlightbackground=self.COLOR_BORDER,
        )
        text_holder.pack(fill="both", expand=True)
        self.log_text = tk.Text(
            text_holder,
            bg="#06090f",
            fg=self.COLOR_TEXT,
            insertbackground=self.COLOR_PRIMARY,
            relief="flat",
            bd=0,
            padx=16,
            pady=14,
            wrap="word",
            font=self._font(10, mono=True),
            state="normal",
            spacing1=1,
            spacing3=1,
        )
        scrollbar = ttk.Scrollbar(
            text_holder,
            command=self.log_text.yview,
            style="Modern.Vertical.TScrollbar",
        )
        self.log_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)

    def _clear_logs(self) -> None:
        try:
            self.log_text.delete("1.0", "end")
        except Exception:
            pass

    def _build_status_bar(self) -> None:
        sep = tk.Frame(self.root, bg=self.COLOR_BORDER, height=1)
        sep.pack(fill="x", side="bottom")
        bar = tk.Frame(self.root, bg=self.COLOR_SURFACE, height=36)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self.status_bar_state = tk.Label(
            bar,
            text="转发服务  ·  ● 未启动",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(9, bold=True),
            padx=22,
            pady=9,
        )
        self.status_bar_state.pack(side="left")

        # Center segment: endpoint hint, monospace
        self.status_bar_endpoint = tk.Label(
            bar,
            text="endpoint  http://127.0.0.1:18180/v1",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_DIM,
            font=self._font(9, mono=True),
            padx=22,
            pady=9,
        )
        self.status_bar_endpoint.pack(side="left")

        self.status_bar_meta = tk.Label(
            bar,
            text="日志  %APPDATA%\\codex-anyroute\\logs",
            bg=self.COLOR_SURFACE,
            fg=self.COLOR_TEXT_MUTED,
            font=self._font(9, mono=True),
            padx=22,
            pady=9,
        )
        self.status_bar_meta.pack(side="right")

    # ------------------------------------------------------------------
    # State + bookkeeping
    # ------------------------------------------------------------------

    def _flash_action(self, message: str, success: bool = True, ms: int = 2800) -> None:
        """Show a transient confirmation in the right side of the status bar."""
        if self.status_bar_meta is None:
            return
        color = self.COLOR_SUCCESS_HOVER if success else self.COLOR_DANGER_HOVER
        try:
            self.status_bar_meta.configure(text=message, fg=color)
        except Exception:
            return
        # cancel any pending revert
        pending = getattr(self, "_flash_after_id", None)
        if pending:
            try:
                self.root.after_cancel(pending)
            except Exception:
                pass
        self._flash_after_id = self.root.after(ms, self._reset_status_meta)

    def _reset_status_meta(self) -> None:
        if self.status_bar_meta is None:
            return
        try:
            self.status_bar_meta.configure(
                text="日志  %APPDATA%\\codex-anyroute\\logs",
                fg=self.COLOR_TEXT_MUTED,
            )
        except Exception:
            pass

    def _refresh_status(self) -> None:
        running = bool(server_holder.get("server"))
        with config_lock:
            port = int(config.listen_port)
        if self.status_pill_dot is not None and self.status_pill_text is not None:
            if running:
                self.status_pill_dot.configure(fg=self.COLOR_SUCCESS_HOVER)
                self.status_pill_text.configure(text=f"运行中  ·  端口 {port}", fg=self.COLOR_TEXT_BRIGHT)
            else:
                self.status_pill_dot.configure(fg=self.COLOR_TEXT_MUTED)
                self.status_pill_text.configure(text="未启动", fg=self.COLOR_TEXT)
        if self.status_bar_state is not None:
            if running:
                self.status_bar_state.configure(
                    text=f"转发服务  ·  ● 已启动",
                    fg=self.COLOR_SUCCESS_HOVER,
                )
            else:
                self.status_bar_state.configure(
                    text="转发服务  ·  ● 未启动",
                    fg=self.COLOR_TEXT_MUTED,
                )
        if self.status_bar_endpoint is not None:
            self.status_bar_endpoint.configure(
                text=f"endpoint  http://127.0.0.1:{port}/v1",
            )
        self.root.after(700, self._refresh_status)

    def _tick_status_pulse(self) -> None:
        """Subtle 'breathing' animation on the header status dot when the
        forwarder is running. Cheap to compute, runs ~3Hz, and gracefully
        no-ops if the dot widget is not yet ready."""
        try:
            running = bool(server_holder.get("server"))
            if self.status_pill_dot is not None and running:
                # cycle through three subtly different greens
                shades = (self.COLOR_SUCCESS_HOVER, self.COLOR_SUCCESS, self.COLOR_SUCCESS_HOVER)
                self.status_pill_dot.configure(fg=shades[self._pulse_phase % len(shades)])
                self._pulse_phase += 1
        except Exception:
            pass
        self.root.after(900, self._tick_status_pulse)

    # ------------------------------------------------------------------
    # Behavior (unchanged backend hooks)
    # ------------------------------------------------------------------

    def load_to_form(self) -> None:
        with config_lock:
            cfg = config
            self.base_entry.insert(0, cfg.api_base_url)
            self.key_entry.insert(0, cfg.api_key)
            self.default_model_entry.insert(0, cfg.default_model)
            self.fallback_model_entry.insert(0, cfg.fallback_model)
            self.third_model_entry.insert(0, cfg.third_model)
            self.enable_fallback_var.set(bool(cfg.enable_fallback))
            self.codex_auto_apply_var.set(bool(cfg.codex_auto_apply))
            self.port_entry.insert(0, str(cfg.listen_port))
            self.gateway_entry.insert(0, cfg.gateway_key)
            for local, ent in self.mapping_entries.items():
                ent.insert(0, cfg.model_map.get(local, ""))

    def save_from_form(self) -> None:
        global config
        with config_lock:
            config.api_base_url = normalize_base_url(self.base_entry.get())
            config.api_key = self.key_entry.get().strip()
            config.default_model = self.default_model_entry.get().strip() or DEFAULT_UPSTREAM_MODEL
            config.fallback_model = self.fallback_model_entry.get().strip() or DEFAULT_FALLBACK_MODEL
            config.third_model = self.third_model_entry.get().strip() or DEFAULT_THIRD_MODEL
            config.enable_fallback = bool(self.enable_fallback_var.get())
            config.codex_auto_apply = bool(self.codex_auto_apply_var.get())
            config.listen_port = int(self.port_entry.get().strip() or str(DEFAULT_LISTEN_PORT))
            config.gateway_key = self.gateway_entry.get().strip() or DEFAULT_GATEWAY_KEY
            config.model_map = {k: e.get().strip() for k, e in self.mapping_entries.items() if e.get().strip()}
            config.save()
        log_bus.write("SUCCESS", "配置已保存")

    def _save_provider(self) -> None:
        self.save_from_form()
        self._flash_action("✓ 提供商配置已保存", success=True)

    def _save_mapping(self) -> None:
        self.save_from_form()
        self._flash_action("✓ 模型映射已保存", success=True)

    def save_restart(self) -> None:
        self.save_from_form()
        stop_server()
        self.root.after(900, start_server)
        self._flash_action("✓ 已保存配置并重启转发", success=True)

    def apply_codex_config(self) -> None:
        self.save_from_form()
        with config_lock:
            config.codex_auto_apply = True
            config.save()
            self.codex_auto_apply_var.set(True)
            cfg = dataclasses.replace(config)
        write_codex_config(cfg)
        _start_codex_config_guard()
        self._flash_action("✓ Codex 配置已写入 ~/.codex/", success=True)
        messagebox.showinfo(APP_NAME, "已写入 Codex 配置。请重启 Codex 后生效。")

    def restore_codex_official(self) -> None:
        """
        Strip the codex-anyroute managed block from ~/.codex/config.toml and
        remove the OPENAI_API_KEY field from ~/.codex/auth.json so Codex falls
        back to the official ChatGPT Plus subscription. The user's OAuth login
        tokens are preserved, so no re-login is required — just restart Codex.
        """
        confirm = messagebox.askyesno(
            APP_NAME,
            "将恢复 Codex 官方 Plus 订阅配置：\n\n"
            "  • 移除 ~/.codex/config.toml 中的 anyroute 托管块\n"
            "  • 删除 ~/.codex/auth.json 中的 OPENAI_API_KEY 字段\n"
            "  • 如存在 OAuth 令牌，将 auth_mode 改回 chatgpt\n"
            "  • 关闭“保持 Codex AnyRoute API 模式”的自动修复\n"
            "  • 保留 ChatGPT OAuth 登录令牌（无需重新登录）\n\n"
            "重启 Codex 后即可使用官方 Plus 订阅。是否继续？",
        )
        if not confirm:
            return
        try:
            with config_lock:
                config.codex_auto_apply = False
                config.save()
                self.codex_auto_apply_var.set(False)
            config_changed, auth_changed, oauth_present = restore_codex_official_config()
            _start_codex_config_guard()
        except Exception as e:
            log_bus.write("ERROR", f"恢复 Codex 官方配置失败：{e}")
            messagebox.showerror(APP_NAME, f"恢复失败：{e}")
            return

        if not (config_changed or auth_changed):
            self._flash_action("• 未发现 anyroute 托管配置", success=True)
            messagebox.showinfo(
                APP_NAME,
                "未发现 anyroute 托管的 Codex 配置，无需恢复。\n"
                "如果你之前没有用过本工具写入 Codex 配置，这是正常的。",
            )
            return

        self._flash_action("✓ 已恢复 Codex 官方配置", success=True)
        if oauth_present:
            messagebox.showinfo(
                APP_NAME,
                "已恢复 Codex 官方 Plus 订阅配置。\n\n"
                "已保留你的 ChatGPT OAuth 登录令牌，重启 Codex 后即可无缝使用 Plus 订阅，无需重新登录。",
            )
        else:
            messagebox.showwarning(
                APP_NAME,
                "已恢复 Codex 官方配置，但未发现 ChatGPT OAuth 登录令牌。\n\n"
                "请运行 `codex login` 完成 ChatGPT 登录后再使用 Plus 订阅。",
            )

    def diagnose_codex_config(self) -> None:
        """
        Run the diagnostic checks on a worker thread (httpx + subprocess can
        block briefly), then render a rolled-up summary in a messagebox on the
        UI thread. Each ERROR / WARN line is also written to the log bus so it
        sticks in the Run logs tab for later reference.
        """
        self.save_from_form()

        def worker() -> None:
            with config_lock:
                cfg = dataclasses.replace(config)
            try:
                items = diagnose_codex_setup(cfg)
            except Exception as e:
                log_bus.write("ERROR", f"诊断 Codex 配置失败：{e}")
                self.root.after(
                    0,
                    lambda: messagebox.showerror(APP_NAME, f"诊断过程出错：{e}"),
                )
                return

            error_count = sum(1 for _, status, _ in items if status == DIAG_ERROR)
            warn_count = sum(1 for _, status, _ in items if status == DIAG_WARN)

            icon = {DIAG_OK: "✓", DIAG_WARN: "!", DIAG_ERROR: "✗"}
            lines: List[str] = []
            for name, status, detail in items:
                lines.append(f"[{icon[status]}] {name}")
                if status != DIAG_OK and detail:
                    # Indent each detail line for readability.
                    for d_line in detail.splitlines():
                        lines.append(f"      {d_line}")
                elif status == DIAG_OK and detail:
                    lines.append(f"      {detail}")
                if status == DIAG_ERROR:
                    log_bus.write("ERROR", f"诊断 - {name}: {detail.splitlines()[0] if detail else ''}")
                elif status == DIAG_WARN:
                    log_bus.write("WARN", f"诊断 - {name}: {detail.splitlines()[0] if detail else ''}")

            if error_count:
                summary = f"发现 {error_count} 项错误、{warn_count} 项警告。Codex 大概率会落到 api.openai.com。"
                title = APP_NAME
                show = messagebox.showerror
                flash = ("✗ 诊断发现错误，请查看详情", False)
            elif warn_count:
                summary = f"发现 {warn_count} 项警告。如果 Codex 仍报错，按提示处理。"
                title = APP_NAME
                show = messagebox.showwarning
                flash = ("! 诊断完成（含警告）", True)
            else:
                summary = "全部检查通过。Codex 应该走本地转发。"
                title = APP_NAME
                show = messagebox.showinfo
                flash = ("✓ 诊断全部通过", True)

            body = summary + "\n\n" + "\n".join(lines)
            self.root.after(0, lambda: show(title, body))
            self.root.after(0, lambda: self._flash_action(flash[0], success=flash[1], ms=4000))

        threading.Thread(target=worker, daemon=True).start()

    def test_connection(self) -> None:
        self.save_from_form()

        def worker() -> None:
            with config_lock:
                cfg = dataclasses.replace(config)
            try:
                results: List[str] = []
                ordered: List[Tuple[str, str]] = [("第一模型", cfg.default_model)]
                if cfg.enable_fallback:
                    ordered.extend([("第二模型", cfg.fallback_model), ("第三模型", cfg.third_model)])
                tested: List[str] = []
                ok_label = ""
                ok_model = ""
                for label, model_name in ordered:
                    model_name = (model_name or "").strip()
                    if not model_name or model_name in tested:
                        continue
                    tested.append(model_name)
                    if is_passthrough_model(model_name):
                        url = upstream_responses_url(cfg)
                        headers = {"content-type": "application/json", "authorization": f"Bearer {cfg.api_key}"}
                        payload = {"model": model_name, "input": "ping", "stream": False, "max_output_tokens": 16}
                    else:
                        raw_model_name = model_name
                        model_name = normalize_anthropic_model(raw_model_name)
                        url = anthropic_messages_url(cfg, raw_model_name)
                        headers = auth_headers(cfg, raw_model_name)
                        payload = {
                            "model": model_name,
                            "max_tokens": 1,
                            "messages": [{"role": "user", "content": "ping"}],
                            "stream": False,
                        }
                        apply_claude_code_compat(payload, raw_model_name, None)
                    last_error = ""
                    for attempt in range(1, UPSTREAM_RETRY_ATTEMPTS + 1):
                        try:
                            r = httpx.post(url, headers=headers, json=payload, timeout=60)
                            if r.status_code < 400:
                                ok_label = label
                                ok_model = model_name
                                results.append(f"{label}可用：{model_name}，HTTP {r.status_code}（第{attempt}次）")
                                log_bus.write("SUCCESS", results[-1])
                                break
                            last_error = friendly_upstream_error(r.status_code, r.text[:500])
                            if r.status_code in RETRYABLE_UPSTREAM_STATUSES and attempt < UPSTREAM_RETRY_ATTEMPTS:
                                log_bus.write("WARN", f"{label}{model_name}测试遇到HTTP {r.status_code}，准备重试第{attempt + 1}次。")
                                time.sleep(upstream_retry_delay(attempt, r.status_code))
                                continue
                            results.append(f"{label}失败：{model_name}，HTTP {r.status_code}\n{last_error}")
                            log_bus.write("ERROR", results[-1])
                            break
                        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout) as net_err:
                            last_error = f"连接AnyRouter时发生网络错误：{net_err}"
                            if attempt < UPSTREAM_RETRY_ATTEMPTS:
                                log_bus.write("WARN", f"{label}{model_name}测试网络错误，准备重试第{attempt + 1}次：{net_err}")
                                time.sleep(upstream_retry_delay(attempt, 0))
                                continue
                            results.append(f"{label}失败：{model_name}\n{last_error}")
                            log_bus.write("ERROR", results[-1])
                            break
                    if ok_model:
                        break

                if ok_model:
                    self.root.after(0, lambda: self._flash_action(f"✓ 测试通过：{ok_label} {ok_model}", success=True, ms=4000))
                    messagebox.showinfo(APP_NAME, f"连接测试通过：{ok_label} {ok_model}\n\n" + "\n\n".join(results))
                else:
                    self.root.after(0, lambda: self._flash_action("✗ 连接测试失败，请查看详情", success=False, ms=4000))
                    messagebox.showerror(APP_NAME, "连接测试失败\n\n" + "\n\n".join(results))
            except Exception as e:
                log_bus.write("ERROR", f"测试连接失败：{e}")
                messagebox.showerror(APP_NAME, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def drain_logs(self) -> None:
        while True:
            try:
                line = log_bus.lines.get_nowait()
            except queue.Empty:
                break
            self.log_text.insert("end", line + "\n")
            self.log_text.see("end")
        self.root.after(300, self.drain_logs)

    def make_icon_image(self) -> Image.Image:
        size = 128
        image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        # Outer rounded square in deep primary
        draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=26, fill="#1d4ed8")
        # Mid layer in electric primary
        draw.rounded_rectangle((10, 10, size - 10, size - 10), radius=22, fill="#4493f8")
        # Inner deep slate for contrast
        draw.rounded_rectangle((20, 20, size - 20, size - 20), radius=16, fill="#0a1119")
        # Tiny accent dot bottom-right (live status hint)
        draw.ellipse((size - 32, size - 32, size - 18, size - 18), fill="#22d3ee")
        # "AR" mark in cyan highlight
        try:
            from PIL import ImageFont
            font = ImageFont.truetype("seguibl.ttf", 44)
        except Exception:
            font = None
        text = "AR"
        if font is not None:
            bbox = draw.textbbox((0, 0), text, font=font)
            tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
            draw.text(((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 4), text, fill="#9ad6ff", font=font)
        else:
            draw.text((34, 38), text, fill="#9ad6ff")
        return image

    def hide_to_tray(self) -> None:
        self.root.withdraw()
        if self.tray_icon:
            return

        def show_window(icon: pystray.Icon, item: Any = None) -> None:
            self.root.after(0, self.root.deiconify)

        def quit_app(icon: pystray.Icon, item: Any = None) -> None:
            stop_server()
            icon.stop()
            self.root.after(0, self.root.destroy)

        self.tray_icon = pystray.Icon(
            "codex-anyroute",
            self.make_icon_image(),
            APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem("打开窗口", show_window),
                pystray.MenuItem("启动转发", lambda icon, item: start_server()),
                pystray.MenuItem("停止转发", lambda icon, item: stop_server()),
                pystray.MenuItem("退出", quit_app),
            ),
        )
        threading.Thread(target=self.tray_icon.run, daemon=True).start()
        log_bus.write("INFO", "窗口已隐藏到托盘，转发服务继续在后台运行")

    def run(self) -> None:
        start_server()
        self.root.mainloop()


if __name__ == "__main__":
    ensure_dirs()
    if not acquire_single_instance():
        sys.exit(0)
    MainWindow().run()
