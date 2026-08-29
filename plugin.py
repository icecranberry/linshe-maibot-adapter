"""邻舍桥接插件：对接 MaiBot 与邻舍（Generate-image-agent）。

能力说明：
1. 人格注入：在 Maisaka 回复器构造完模型请求后（maisaka.replyer.before_model_request），
   将 system 人格整条替换为邻舍角色的 base_prompt；在 Planner 请求前
   （maisaka.planner.before_request）覆盖默认行为风格。行为/表达风格由邻舍
   /api/maibot/derive-style 提炼并以 user 消息注入请求，同时移除 MaiBot 自带表达习惯，
   并将请求中的 MaiBot 机器人昵称替换为邻舍角色的 display_name。
2. 记忆与配图：Planner 可调用 linshe_generate_image 工具触发邻舍生图，工具只提交画面需求，
   由邻舍按固定格式提炼 prompt 并异步取图发送；MaiBot 生成最终回复后仅同步记忆到邻舍。

3. 插件设置页：MaiBot 插件设置页展示当前激活角色的 display_name，并提供邻舍托管
   的人格管理页入口（http://127.0.0.1:3099/api/maibot/plugin-ui），可查看/覆盖
   注入的 base_prompt、提炼风格、开关记忆整理与配图等参数。

独立性：仅依赖 maibot_sdk 与 httpx，不引用 MaiBot 内部代码，可独立上传插件仓库。
"""

from __future__ import annotations

from copy import deepcopy
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import asyncio
import base64
import hashlib
import json
import re
import time

import httpx

from maibot_sdk import API, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ErrorPolicy, ToolParameterInfo, ToolParamType

CHARACTERS_CACHE_TTL_SEC = 300.0
HTTP_TIMEOUT_SEC = 120.0
PERSONA_CONTROL_HOST = "127.0.0.1"
PERSONA_CONTROL_PORT = 3199
PERSONA_CONTROL_MAX_BODY_BYTES = 1024 * 1024

_BEHAVIOR_STYLE_BLOCK_RE = re.compile(
    r"(?ms)(^[^\n]*?(?:的行为风格|'s behavior style|の行動スタイル)\s*[:：]).*?(?=\n\s*\n)"
)
_EXPRESSION_HABITS_MARKER = "【表达习惯参考，请视情况自然的使用】"
_TEMPORARY_REPLY_STYLE_MARKER = "你的说话风格可以尝试："
_LEGACY_BASE_PROMPT_IMAGE_ABILITY_LINE = "## 你拥有画图的能力，只要你想象画面，你就可以发送出来图片"
_BEHAVIOR_STYLE_IMAGE_TOOL_LINE = (
    "画图是你日常表达的一部分，随时可以主动调用生图工具 linshe_generate_image 发一张图："
    "调用时只需用一两句话描述想要的画面需求，不要直接编写生图提示词。"
    "不要等用户开口要图——打招呼、分享日常、说到自己正在做的事、"
    "聊到具体的场景/动作/物品/食物/天气、情绪上头或想玩梗吐槽时，"
    "只要配一张图能让表达更生动，就直接画出来随回复一起发。"
    "调用后直接正常回复文字，不要引用图片媒体。"
)
_PLANNER_BOT_NAME_RE = re.compile(r"(?m)^([^\n]*?)(?:的行为风格|'s behavior style|の行動スタイル)\s*[:：]")
_REPLYER_BOT_NAME_RE = re.compile(r"你的名字是([^，。\n]+)")

_MSG_TAG_RE = re.compile(r"^<message\b[^>]*>$")
_SPEAKER_TAG_RE = re.compile(r'<message\b[^>]*\buser="([^"]*)"')
_TOOL_CALL_KEYS = ("tool_call_id", "tool_name", "args", "result_status", "result")
_INSTRUCTION_START_MARKERS = (
    "当前时间：",
    "你想要回复的消息是",
    "【回复信息参考】",
    "【关键信息参考】",
    "回复指引：",
    "关键信息参考：",
    "请优先依据",
)



class PluginSectionConfig(PluginConfigBase):
    """插件默认启用，无需手动开关；详细参数请到管理页调整。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(
        default=True,
        description="是否启用插件（默认开启）",
        json_schema_extra={"hidden": True},
    )
    config_version: str = Field(default="1.3.1", description="配置版本", json_schema_extra={"hidden": True})


class BridgeSectionConfig(PluginConfigBase):
    """邻舍连接配置。基础连接参数可在插件设置页调整。"""

    __ui_label__ = "邻舍连接"
    __ui_icon__ = "cable"
    __ui_order__ = 1

    base_url: str = Field(
        default="http://127.0.0.1:3099",
        description="邻舍服务地址",
        json_schema_extra={"label": "邻舍服务地址", "placeholder": "http://127.0.0.1:3099", "hint": "管理页链接也会跟随该地址"},
    )
    character_name: str = Field(
        default="",
        description="邻舍角色显示名（display_name，兼容 characters.name），留空则跳过全部流程",
        json_schema_extra={"label": "邻舍角色显示名", "placeholder": "输入 display_name 或 name", "hint": "按邻舍角色 display_name 注入，兼容 characters.name；留空则跳过全部流程"},
    )
    permanent_config_write: bool = Field(
        default=False,
        description="永久写入 MaiBot 配置代替临时覆盖",
        json_schema_extra={
            "label": "永久写入MaiBot配置代替临时覆盖",
            "hint": "不再只是临时桥接配置，而是直接写入 MaiBot 的 bot_config.toml（含机器人昵称）；勾选后 MaiBot 单独启动也能使用邻舍人物卡",
        },
    )


class PersonaSectionConfig(PluginConfigBase):
    """人格注入配置。人格替换与风格提炼为始终开启，详细参数请到管理页调整。"""

    __ui_label__ = "人格注入"
    __ui_icon__ = "user"
    __ui_order__ = 2



class MemorySectionConfig(PluginConfigBase):
    """记忆整理配置。开关可在插件设置页直接调整。"""

    __ui_label__ = "记忆整理"
    __ui_icon__ = "database"
    __ui_order__ = 3

    memory_curation: bool = Field(
        default=True,
        description="启用对话记忆摘要（同步回邻舍）：每 40 句真实聊天内容整理一份摘要注入 MaiBot 主聊天流；关闭后不再整理，并删除已保存的记忆摘要",
        json_schema_extra={"label": "启用记忆整理", "hint": "开启后由邻舍整理对话摘要并注入主聊天流；关闭会停止整理并删除已保存摘要"},
    )


class ImageSectionConfig(PluginConfigBase):
    """生图任务轮询配置。图片由 Planner 工具触发邻舍生成。"""

    __ui_label__ = "生图"
    __ui_icon__ = "image"
    __ui_order__ = 4

    poll_interval_sec: float = Field(
        default=2.0,
        ge=0.5,
        description="Planner 生图任务轮询间隔（秒）",
        json_schema_extra={"label": "轮询间隔（秒）", "hint": "Planner 生图任务轮询间隔"},
    )
    poll_timeout_sec: float = Field(
        default=180.0,
        ge=1.0,
        description="Planner 生图任务轮询超时（秒）",
        json_schema_extra={"label": "轮询超时（秒）", "hint": "Planner 生图任务轮询超时，需大于轮询间隔"},
    )


class NeighborBridgePluginConfig(PluginConfigBase):
    """邻舍桥接插件配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    bridge: BridgeSectionConfig = Field(default_factory=BridgeSectionConfig)
    persona: PersonaSectionConfig = Field(default_factory=PersonaSectionConfig)
    memory: MemorySectionConfig = Field(default_factory=MemorySectionConfig)
    image: ImageSectionConfig = Field(default_factory=ImageSectionConfig)


class NeighborBridgePlugin(MaiBotPlugin):
    """邻舍桥接插件。"""

    config_model = NeighborBridgePluginConfig

    def get_webui_config_schema(
        self,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> dict[str, Any]:
        """生成插件设置页 Schema：展示基础配置项，隐藏空配置节，并保留管理页入口提示。"""
        schema = super().get_webui_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )
        sections = schema.get("sections")
        if isinstance(sections, dict):
            plugin_section = sections.get("plugin")
            if isinstance(plugin_section, dict):
                plugin_section["description"] = (
                    "插件默认启用，无需手动开关。建议打开邻舍系统设置里的桥接设置卡片设置或者"
                    "http://127.0.0.1:3099/api/maibot/plugin-ui配置角色、记忆与配图等详细参数。"
                )
                plugin_section["fields"] = {}
            for section_name, section_schema in list(sections.items()):
                if section_name != "plugin" and isinstance(section_schema, dict) and not section_schema.get("fields"):
                    sections.pop(section_name)
        schema["layout"] = {"type": "auto", "tabs": []}
        return schema

    def __init__(self) -> None:
        """初始化插件状态。"""
        super().__init__()
        self._http_client: httpx.AsyncClient | None = None
        self._characters: list[dict[str, Any]] = []
        self._characters_fetched_at: float = 0.0
        self._persona_store: dict[str, dict[str, Any]] = {}
        self._persona_store_lock = asyncio.Lock()
        self._persona_control_server: asyncio.Server | None = None
        self._last_context: dict[str, list[dict[str, str]]] = {}
        self._latest_memory_cache: dict[str, tuple[str, float]] = {}
        self._permanent_persona_hash: str | None = None
        self._pending_image_streams: set[str] = set()
        self._last_image_requirement: dict[str, str] = {}

    async def on_load(self) -> None:
        """插件加载：创建 HTTP 客户端并加载本地人格数据。"""
        self._http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        await self._load_persona_store()
        await self._start_persona_control_server()
        if self.config.bridge.permanent_config_write:
            try:
                await self._load_persona_bundle()
            except Exception as exc:
                self._get_logger().warning(f"邻舍桥接：插件加载时永久写入触发失败: {exc}")
        self._get_logger().info("邻舍桥接：插件已加载")

    async def on_unload(self) -> None:
        """插件卸载：关闭 HTTP 客户端。"""
        await self._stop_persona_control_server()
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    @API("persona.get", description="读取邻舍桥接插件的人格数据", version="1", public=True)
    async def get_persona_data(self) -> dict[str, Any]:
        """返回插件当前内存中的人格数据，并清理旧版 base_prompt 追加。"""
        async with self._persona_store_lock:
            changed = False
            for entry in self._persona_store.values():
                raw_base_prompt = str(entry.get("base_prompt") or "").strip()
                stripped_base_prompt = self._strip_legacy_base_prompt_image_ability(raw_base_prompt)
                if stripped_base_prompt != raw_base_prompt:
                    entry["base_prompt"] = stripped_base_prompt
                    entry["updated_at"] = int(time.time() * 1000)
                    changed = True
            if changed:
                self._write_persona_store_unlocked()
            return {"characters": deepcopy(self._persona_store)}

    @API("persona.update", description="更新邻舍桥接插件的人格数据", version="1", public=True)
    async def update_persona_data(self, character_name: str = "", **kwargs: Any) -> dict[str, Any]:
        """合并更新指定角色的人格数据并立即持久化，display_name 会归一到 characters.name。"""
        normalized_name = character_name.strip()
        if not normalized_name:
            raise ValueError("character_name 不能为空")

        character = await self._find_character(normalized_name)
        if character and str(character.get("name") or "").strip():
            normalized_name = str(character.get("name") or "").strip()

        async with self._persona_store_lock:
            entry = self._persona_store.setdefault(
                normalized_name,
                {"base_prompt": "", "behavior_style": "", "reply_style": "", "display_name": "", "updated_at": 0.0},
            )
            changed = False
            for field_name in ("base_prompt", "behavior_style", "reply_style"):
                if field_name in kwargs:
                    value = str(kwargs[field_name]).strip()
                    if field_name == "base_prompt":
                        value = self._strip_legacy_base_prompt_image_ability(value)
                    if entry.get(field_name) != value:
                        entry[field_name] = value
                        changed = True
            if changed:
                entry["updated_at"] = int(time.time() * 1000)
                self._write_persona_store_unlocked()
            saved_entry = deepcopy(entry)
        await self._write_permanent_persona_if_needed(
            base_prompt=saved_entry.get("base_prompt", ""),
            display_name=saved_entry.get("display_name", ""),
            styles={
                "behavior_style": saved_entry.get("behavior_style", ""),
                "reply_style": saved_entry.get("reply_style", ""),
            },
        )
        return {"character_name": normalized_name, "entry": saved_entry}

    async def _start_persona_control_server(self) -> None:
        """启动仅绑定回环地址、供邻舍管理页使用的人格读写接口。"""
        try:
            self._persona_control_server = await asyncio.start_server(
                self._handle_persona_control_connection,
                PERSONA_CONTROL_HOST,
                PERSONA_CONTROL_PORT,
            )
            self._get_logger().info(
                f"邻舍桥接：人格控制接口已启动 http://{PERSONA_CONTROL_HOST}:{PERSONA_CONTROL_PORT}"
            )
        except OSError as exc:
            self._persona_control_server = None
            self._get_logger().warning(
                f"邻舍桥接：人格控制接口启动失败 {PERSONA_CONTROL_HOST}:{PERSONA_CONTROL_PORT}: {exc}；"
                "聊天与配图功能不受影响，管理页人格读写暂不可用"
            )

    async def _stop_persona_control_server(self) -> None:
        """停止本机人格读写接口。"""
        server = self._persona_control_server
        self._persona_control_server = None
        if server is None:
            return
        server.close()
        await server.wait_closed()

    async def _handle_persona_control_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """处理一个最小 HTTP/1.1 请求，不引入额外 Web 框架依赖。"""
        try:
            peer = writer.get_extra_info("peername")
            peer_host = str(peer[0]) if isinstance(peer, tuple) and peer else ""
            if peer_host not in {"127.0.0.1", "::1"}:
                await self._write_persona_control_response(writer, 403, {"error": "仅允许本机访问"})
                return

            try:
                header_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                await self._write_persona_control_response(writer, 400, {"error": "无效的 HTTP 请求"})
                return

            try:
                header_text = header_bytes.decode("iso-8859-1")
                header_lines = header_text.split("\r\n")
                method, target, _http_version = header_lines[0].split(" ", 2)
                headers: dict[str, str] = {}
                for line in header_lines[1:]:
                    if not line or ":" not in line:
                        continue
                    name, value = line.split(":", 1)
                    headers[name.strip().lower()] = value.strip()
                content_length = int(headers.get("content-length", "0") or "0")
            except (ValueError, UnicodeDecodeError):
                await self._write_persona_control_response(writer, 400, {"error": "无效的 HTTP 请求头"})
                return

            if content_length < 0 or content_length > PERSONA_CONTROL_MAX_BODY_BYTES:
                await self._write_persona_control_response(writer, 413, {"error": "请求内容过大"})
                return

            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=5.0) if content_length else b""
            path = target.split("?", 1)[0]
            if method == "GET" and path == "/health":
                await self._write_persona_control_response(writer, 200, {"ok": True, "service": "linshe-persona"})
                return
            if method == "GET" and path == "/persona":
                result = await self.get_persona_data()
                await self._write_persona_control_response(writer, 200, {"ok": True, **result})
                return
            if method == "PUT" and path == "/persona":
                try:
                    payload = json.loads(body.decode("utf-8")) if body else {}
                except (json.JSONDecodeError, UnicodeDecodeError):
                    await self._write_persona_control_response(writer, 400, {"error": "请求体必须是 JSON"})
                    return
                if not isinstance(payload, dict):
                    await self._write_persona_control_response(writer, 400, {"error": "请求体必须是 JSON 对象"})
                    return
                character_name = payload.pop("character_name", "")
                allowed_fields = {key: payload[key] for key in ("base_prompt", "behavior_style", "reply_style") if key in payload}
                try:
                    result = await self.update_persona_data(str(character_name), **allowed_fields)
                except ValueError as exc:
                    await self._write_persona_control_response(writer, 400, {"error": str(exc)})
                    return
                await self._write_persona_control_response(writer, 200, {"ok": True, **result})
                return

            await self._write_persona_control_response(writer, 404, {"error": "Not Found"})
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            return
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：人格控制接口处理请求失败: {exc}")
            try:
                await self._write_persona_control_response(writer, 500, {"error": "Internal Server Error"})
            except Exception:
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, RuntimeError):
                pass

    @staticmethod
    async def _write_persona_control_response(
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        """写出 JSON HTTP 响应。"""
        reason = {
            200: "OK",
            400: "Bad Request",
            403: "Forbidden",
            404: "Not Found",
            413: "Payload Too Large",
            500: "Internal Server Error",
        }.get(status, "Error")
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response_headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Cache-Control: no-store\r\n"
            "Connection: close\r\n"
            "\r\n"
        ).encode("ascii")
        writer.write(response_headers + body)
        await writer.drain()

    async def on_config_update(self, scope: str, config_data: dict[str, object], version: str) -> None:
        """配置热更新：清除缓存，使新配置立即生效。"""
        del scope
        memory_curation_enabled = bool((config_data.get("memory") or {}).get("memory_curation", True))
        del config_data
        del version
        self._characters = []
        self._characters_fetched_at = 0.0
        self._last_context.clear()
        self._latest_memory_cache.clear()
        if not memory_curation_enabled:
            await self._delete_all_latest_memory()
        if self.config.bridge.permanent_config_write:
            try:
                await self._load_persona_bundle()
            except Exception as exc:
                self._get_logger().warning(f"邻舍桥接：配置更新后永久写入触发失败: {exc}")

    # ===== 人格注入：maisaka.replyer.before_model_request =====

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="neighbor_persona_inject",
        description="将 system 人格整条替换为邻舍 base_prompt，并注入行为/表达风格",
        timeout_ms=20000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_before_model_request(self, **kwargs: Any) -> dict[str, Any] | None:
        """构造模型请求时：替换人格并注入风格（失败时保持原请求不变）。"""
        messages = kwargs.get("messages")
        if not isinstance(messages, list) or not messages:
            return None

        session_id = str(kwargs.get("session_id") or "")
        self._cache_context(session_id, messages)

        if not self.config.plugin.enabled:
            return None
        if not str(self.config.bridge.character_name or "").strip():
            return None
        persona_bundle = await self._load_persona_bundle()
        if persona_bundle is None:
            return None
        base_prompt, display_name, styles = persona_bundle

        new_messages = deepcopy(messages)
        original_message_count = len(new_messages)
        new_messages = self._remove_maibot_style_messages(new_messages)
        removed_maibot_styles = original_message_count - len(new_messages)
        maibot_nickname = self._extract_maibot_nickname(messages)
        nickname_replaced = self._replace_maibot_nickname(new_messages, maibot_nickname, display_name)
        self._replace_system_prompt(new_messages, base_prompt)
        injected_styles: list[str] = []
        style_messages: list[dict[str, str]] = []
        behavior_style = str(styles.get("behavior_style") or "").strip()
        if behavior_style:
            style_messages.append({"role": "user", "content": f"【行为风格】\n{behavior_style}"})
        reply_style = styles.get("reply_style")
        if reply_style:
            style_messages.append({"role": "user", "content": f"【表达风格】\n{reply_style}"})
        if style_messages and new_messages:
            new_messages[-1:-1] = style_messages
            injected_styles = [
                marker
                for marker in ("行为风格", "表达风格")
                if any(marker in message["content"] for message in style_messages)
            ]
        memory_content = ""
        if self.config.memory.memory_curation:
            memory_content = await self._get_latest_memory(session_id)
        if memory_content and new_messages:
            new_messages[-1:-1] = [{"role": "user", "content": f"<latest_memory>\n{memory_content}\n</latest_memory>"}]
        self._get_logger().info(
            f"邻舍桥接：已注入人格 character={self.config.bridge.character_name} "
            f"system_replaced=True styles={injected_styles} "
            f"nickname_replaced={nickname_replaced} maibot_styles_removed={removed_maibot_styles} "
            f"memory_injected={bool(memory_content)}"
        )
        return {"action": "continue", "modified_kwargs": {"messages": new_messages}}

    # ===== 行为风格覆盖：maisaka.planner.before_request =====

    @HookHandler(
        "maisaka.planner.before_request",
        name="neighbor_planner_persona_inject",
        description="将 Planner 系统提示中的 MaiBot 默认行为风格替换为邻舍行为风格",
        timeout_ms=20000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_planner_before_request(self, **kwargs: Any) -> dict[str, Any] | None:
        """规划器请求前：覆盖默认行为风格，避免 Planner 继续沿用 MaiBot 配置。"""
        messages = kwargs.get("messages")
        if not isinstance(messages, list) or not messages:
            return None
        persona_bundle = await self._load_persona_bundle()
        if persona_bundle is None:
            return None
        _, display_name, styles = persona_bundle
        behavior_style = self._ensure_behavior_style_image_tool(str(styles.get("behavior_style") or "").strip())
        new_messages = deepcopy(messages)
        maibot_nickname = self._extract_maibot_nickname(messages)
        nickname_replaced = self._replace_maibot_nickname(new_messages, maibot_nickname, display_name)
        replaced = self._replace_planner_behavior_style(new_messages, behavior_style)
        if not replaced and behavior_style:
            new_messages[-1:-1] = [{"role": "user", "content": f"【行为风格】\n{behavior_style}"}]
        self._get_logger().info(
            f"邻舍桥接：已覆盖 Planner 行为风格 character={self.config.bridge.character_name} "
            f"nickname_replaced={nickname_replaced} system_behavior_replaced={replaced} "
            f"behavior_style_injected={bool(behavior_style)}"
        )
        return {"modified_kwargs": {**kwargs, "messages": new_messages}}

    # ===== Planner 生图工具 =====

    @Tool(
        "linshe_generate_image",
        brief_description="主动生成一张贴合当前对话的图片，并异步发送到当前聊天流",
        detailed_description=(
            "在合适的时机主动使用：用户明确要求画图时必须调用；此外在打招呼、分享日常、"
            "说到自己正在做的事、聊到具体的场景/动作/物品/食物/天气、情绪上头或玩梗吐槽时，"
            "只要配一张图能让表达更生动，就不要等用户开口，直接调用本工具画一张。"
            "参数说明：\n"
            "- requirement：string，必填。画面需求描述，用一两句话说明想要一张什么样的图片，不需要写生图提示词。"
            "如果画面里面需要有“我”，那就直接在需求中注明名字即可（例如：芙宁娜正在喝茶。），"
            "除非特意强调“我”的衣服，不然不需要描述外观，只需要注明名字即可。\n"
            "调用后图片会异步生成并自动发送，工具结果不包含可引用的图片媒体。"
            "不要使用 reply/send_image 的 media_index 或 msg_id 引用本工具结果，直接正常回复文字即可；"
            "当前聊天流已有生图任务在生成中时不要调用，同一张画面不要重复生成。"
        ),
        parameters=[
            ToolParameterInfo(
                name="requirement",
                param_type=ToolParamType.STRING,
                description="画面需求描述：只说明想要一张什么样的图片，邻舍会按固定格式提炼成生图提示词",
                required=True,
            ),
        ],
        visibility="visible",
    )
    async def handle_linshe_generate_image(self, requirement: str, **kwargs: Any) -> dict[str, Any]:
        """Planner 主动生图：触发邻舍生图任务并后台发送，不阻塞后续回复。"""
        if not self.config.plugin.enabled:
            return {"success": False, "error": "邻舍桥接插件未启用。"}
        character_name = str(self.config.bridge.character_name or "").strip()
        if not character_name:
            return {"success": False, "error": "未配置邻舍角色，无法生图。"}
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if not stream_id:
            return {"success": False, "error": "缺少当前聊天流 ID，无法生图。"}
        image_requirement = str(requirement or "").strip()
        if not image_requirement:
            return {"success": False, "error": "画面需求不能为空。"}
        if len(image_requirement) > 2000:
            return {"success": False, "error": "画面需求过长（最多 2000 字）。"}
        if image_requirement == self._last_image_requirement.get(stream_id, ""):
            return {"success": False, "error": "同样的画面需求刚刚已经生成过，请换一个画面或等用户发来新消息。"}
        if stream_id in self._pending_image_streams:
            return {"success": False, "error": "当前聊天流已有生图任务进行中，请等图片发出后再继续。"}
        context = (self._last_context.get(stream_id) or [])[-6:]
        try:
            response = await self._request(
                "POST",
                "/api/maibot/generate",
                json={
                    "character_name": character_name,
                    "requirement": image_requirement,
                    "session_id": stream_id,
                    "context": context,
                },
            )
            data = response.json()
            task_id = data.get("task_id")
            if not task_id:
                return {"success": False, "error": "邻舍未返回生图任务 ID。"}
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：Planner 生图工具触发失败: {exc}")
            return {"success": False, "error": f"触发邻舍生图失败: {exc}"}

        self._last_image_requirement[stream_id] = image_requirement
        self._pending_image_streams.add(stream_id)
        asyncio.create_task(self._run_pending_image_task(stream_id, task_id))
        return {
            "success": True,
            "content": "已触发邻舍生图，图片会自动发送到当前聊天流，工具结果不包含图片媒体，请不要引用，直接继续正常文字回复。",
            "task_id": str(task_id),
        }

    # ===== 记忆同步：maisaka.reply.before_post_process =====

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="neighbor_reply_after_generated",
        description="回复生成后交给邻舍保存记忆（配图由 Planner 工具触发）",
        timeout_ms=10000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reply_before_post_process(self, **kwargs: Any) -> None:
        """回复生成后：触发邻舍记忆同步（配图改由 Planner 工具触发，不阻塞回复）。"""
        if not self.config.plugin.enabled:
            return None
        if not str(self.config.bridge.character_name or "").strip():
            return None

        reply_text = str(kwargs.get("response") or "").strip()
        session_id = str(kwargs.get("session_id") or "")
        reply_message_id = str(kwargs.get("reply_message_id") or "")
        if not reply_text or not session_id:
            return None

        context = self._last_context.get(session_id) or []
        # 取最近一条带 <message user="..."> 前缀的真实用户发言（群聊中昵称/ID 天然来自消息前缀）
        user_message = ""
        user_name = ""
        for entry in reversed(context):
            if entry.get("role") == "user" and entry.get("speaker"):
                user_message = str(entry.get("text") or "").strip()
                user_name = str(entry.get("speaker") or "").strip()
                break

        client_msg_id = f"{session_id}:{reply_message_id}" if reply_message_id else ""
        self._get_logger().info(
            f"邻舍桥接：已触发记忆同步 session={session_id} reply_len={len(reply_text)}"
        )
        asyncio.create_task(
            self._run_memory_sync(session_id, client_msg_id, user_message, user_name, reply_text)
        )
        return None

    # ===== 邻舍接口调用 =====

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """发起对邻舍桥接接口的请求。"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SEC)
        base_url = str(self.config.bridge.base_url or "").rstrip("/")
        parsed = urlparse(path)
        request_url = path if parsed.scheme in {"http", "https"} else f"{base_url}{path}"
        response = await self._http_client.request(method, request_url, **kwargs)
        response.raise_for_status()
        return response

    async def _fetch_characters(self) -> list[dict[str, Any]]:
        """获取邻舍角色列表（缓存 5 分钟）。"""
        now = time.monotonic()
        if self._characters and now - self._characters_fetched_at < CHARACTERS_CACHE_TTL_SEC:
            return self._characters
        try:
            response = await self._request("GET", "/api/maibot/characters")
            data = response.json()
            self._characters = list(data.get("characters") or [])
            self._characters_fetched_at = now
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：获取角色列表失败: {exc}")
        return self._characters

    async def _get_character(self) -> dict[str, Any] | None:
        """按角色显示名（display_name）查找邻舍角色，兼容 characters.name。"""
        character_name = str(self.config.bridge.character_name or "").strip()
        if not character_name:
            return None
        character = await self._find_character(character_name)
        if character is not None:
            return character
        characters = self._characters
        available = "、".join(str(character.get("display_name") or character.get("name") or "") for character in characters)
        self._get_logger().warning(f"邻舍桥接：未找到角色 {character_name}，可用角色：{available}")
        return None

    async def _find_character(self, character_name: str) -> dict[str, Any] | None:
        """按角色显示名或内部名查找邻舍角色。"""
        normalized_name = str(character_name or "").strip()
        if not normalized_name:
            return None
        characters = await self._fetch_characters()
        for character in characters:
            if str(character.get("display_name") or "").strip() == normalized_name:
                return character
        for character in characters:
            if str(character.get("name") or "").strip() == normalized_name:
                return character
        return None

    async def _resolve_styles(self, base_prompt: str, store_entry: dict[str, Any]) -> dict[str, str]:
        """解析注入用的行为/表达风格：本地已保存优先，否则提炼并保存到本地数据。"""
        resolved: dict[str, str] = {}
        derive_keys: list[str] = []

        saved_behavior_style = str(store_entry.get("behavior_style") or "").strip()
        if saved_behavior_style:
            resolved["behavior_style"] = saved_behavior_style
        else:
            derive_keys.append("behavior_style")

        saved_reply_style = str(store_entry.get("reply_style") or "").strip()
        if saved_reply_style:
            resolved["reply_style"] = saved_reply_style
        else:
            derive_keys.append("reply_style")

        if not derive_keys:
            return resolved

        derived = await self._get_styles(base_prompt)
        changed = False
        for key in derive_keys:
            if derived.get(key):
                value = derived[key]
                resolved[key] = value
                store_entry[key] = value
                changed = True
        if changed:
            store_entry["updated_at"] = int(time.time() * 1000)
            await self._save_persona_store()
        return resolved

    async def _get_styles(self, base_prompt: str) -> dict[str, str]:
        """从 base_prompt 提炼行为/表达风格（结果由调用方写入本地数据）。"""
        base_prompt = str(base_prompt or "").strip()
        if not base_prompt:
            return {}
        try:
            response = await self._request("POST", "/api/maibot/derive-style", json={"base_prompt": base_prompt})
            data = response.json()
            derived = {
                key: str(data.get(key) or "").strip()
                for key in ("behavior_style", "reply_style")
                if str(data.get(key) or "").strip()
            }
            return derived
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：提炼行为/表达风格失败: {exc}")
            return {}

    async def _load_persona_store(self) -> None:
        """加载本地人格数据（角色 → base_prompt 副本/行为风格/表达风格）。"""
        try:
            persona_store_path = self._persona_store_path
            if persona_store_path.exists():
                raw_payload = json.loads(persona_store_path.read_text(encoding="utf-8"))
                if isinstance(raw_payload, dict):
                    characters = raw_payload.get("characters") if isinstance(raw_payload.get("characters"), dict) else raw_payload
                    for character_name, entry in characters.items():
                        if isinstance(entry, dict):
                            self._persona_store[str(character_name)] = {
                                "base_prompt": self._strip_legacy_base_prompt_image_ability(
                                    str(entry.get("base_prompt") or "").strip()
                                ),
                                "behavior_style": str(entry.get("behavior_style") or "").strip(),
                                "reply_style": str(entry.get("reply_style") or "").strip(),
                                "display_name": str(entry.get("display_name") or "").strip(),
                                "updated_at": float(entry.get("updated_at") or 0.0),
                            }
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：本地人格数据读取失败: {exc}")

    @property
    def _persona_store_path(self) -> Path:
        """返回运行时为当前插件分配的持久化文件路径。"""
        return self.ctx.paths.data_dir / "persona_store.json"

    def _cached_character_display_name(self, character_name: str) -> str:
        """从本地人格存档读取角色显示名，未命中时返回空串。"""
        character_name = str(character_name or "").strip()
        if not character_name:
            return ""
        if self._ctx is None:
            return ""
        try:
            if not self._persona_store_path.exists():
                return ""
            payload = json.loads(self._persona_store_path.read_text(encoding="utf-8"))
            characters = payload.get("characters") if isinstance(payload, dict) and isinstance(payload.get("characters"), dict) else {}
            for stored_name, entry in characters.items():
                if not isinstance(entry, dict):
                    continue
                display_name = str(entry.get("display_name") or "").strip()
                if stored_name == character_name or display_name == character_name:
                    return display_name
        except Exception:
            return ""
        return ""

    def _write_persona_store_unlocked(self) -> None:
        """在持有人格锁时写入数据文件。"""
        payload = {"characters": self._persona_store}
        persona_store_path = self._persona_store_path
        persona_store_path.parent.mkdir(parents=True, exist_ok=True)
        persona_store_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    async def _save_persona_store(self) -> None:
        """将本地人格数据写入插件数据目录。"""
        async with self._persona_store_lock:
            try:
                self._write_persona_store_unlocked()
            except Exception as exc:
                self._get_logger().warning(f"邻舍桥接：本地人格数据写入失败: {exc}")

    async def _get_or_create_store_entry(self, character_name: str) -> dict[str, Any]:
        """获取当前角色的本地数据条目（不存在则创建）。"""
        async with self._persona_store_lock:
            entry = self._persona_store.get(character_name)
            if entry is None:
                entry = {"base_prompt": "", "behavior_style": "", "reply_style": "", "display_name": "", "updated_at": 0.0}
                self._persona_store[character_name] = entry
            else:
                entry.setdefault("base_prompt", "")
                entry.setdefault("behavior_style", "")
                entry.setdefault("reply_style", "")
                entry.setdefault("display_name", "")
                entry.setdefault("updated_at", 0.0)
            return entry

    @staticmethod
    def _strip_legacy_base_prompt_image_ability(base_prompt: str) -> str:
        """清理旧版追加在 base_prompt 末尾的画图能力说明。"""
        base_prompt = str(base_prompt or "").strip()
        legacy_line = _LEGACY_BASE_PROMPT_IMAGE_ABILITY_LINE.strip()
        lines = [line for line in base_prompt.splitlines() if line.strip() != legacy_line]
        return "\n".join(lines).strip()

    @staticmethod
    def _ensure_behavior_style_image_tool(behavior_style: str) -> str:
        """仅注入时使用：为行为风格追加生图工具说明，不写入本地人格数据。"""
        behavior_style = str(behavior_style or "").strip()
        if _BEHAVIOR_STYLE_IMAGE_TOOL_LINE in behavior_style:
            return behavior_style
        return f"{behavior_style}\n{_BEHAVIOR_STYLE_IMAGE_TOOL_LINE}".strip()

    async def _write_permanent_persona_if_needed(
        self,
        base_prompt: str,
        display_name: str,
        styles: dict[str, str],
    ) -> None:
        """永久写入 MaiBot 配置（含机器人昵称）；仅在数据变化时写一次。"""
        if not self.config.bridge.permanent_config_write:
            return
        payload = {
            "base_prompt": str(base_prompt or "").strip(),
            "behavior_style": str(styles.get("behavior_style") or "").strip(),
            "reply_style": str(styles.get("reply_style") or "").strip(),
            "display_name": str(display_name or "").strip(),
        }
        try:
            current_hash = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()
        except (TypeError, ValueError):
            current_hash = ""
        if current_hash and current_hash == self._permanent_persona_hash:
            return
        try:
            response = await self._request("POST", "/api/maibot/permanent-persona", json=payload)
            data = response.json()
            if not data.get("ok"):
                self._get_logger().warning(f"邻舍桥接：永久写入 MaiBot 配置返回异常: {data}")
                return
            self._permanent_persona_hash = current_hash
            self._get_logger().info(f"邻舍桥接：已永久写入 MaiBot 配置 character={payload['display_name'] or payload['base_prompt'][:20]}")
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：永久写入 MaiBot 配置失败: {exc}")

    async def _load_persona_bundle(self) -> tuple[str, str, dict[str, str]] | None:
        """解析当前角色的人格与风格：本地副本优先，首次拉取邻舍原文后保存。"""
        if not self.config.plugin.enabled:
            return None
        if not str(self.config.bridge.character_name or "").strip():
            return None
        character = await self._get_character()
        if not character:
            return None
        character_name = str(character.get("name") or "").strip()
        if not character_name:
            return None
        display_name = str(character.get("display_name") or character.get("name") or "").strip()
        store_entry = await self._get_or_create_store_entry(character_name)
        if str(store_entry.get("display_name") or "").strip() != display_name:
            store_entry["display_name"] = display_name
            store_entry["updated_at"] = int(time.time() * 1000)
            await self._save_persona_store()
        base_prompt = self._strip_legacy_base_prompt_image_ability(str(store_entry.get("base_prompt") or "").strip())
        if not base_prompt:
            base_prompt = self._strip_legacy_base_prompt_image_ability(str(character.get("base_prompt") or "").strip())
            if base_prompt:
                store_entry["base_prompt"] = base_prompt
                await self._save_persona_store()
        if not base_prompt:
            return None
        styles = await self._resolve_styles(base_prompt, store_entry)
        await self._write_permanent_persona_if_needed(base_prompt, display_name, styles)
        return base_prompt, display_name, styles

    async def _get_latest_memory(self, session_id: str) -> str:
        """获取邻舍最新一份记忆整理（缓存 30 秒），无内容时返回空串。"""
        if not session_id:
            return ""
        cache_key = str(session_id)
        now = time.monotonic()
        cached = self._latest_memory_cache.get(cache_key)
        if cached is not None and now - cached[1] < 30.0:
            return cached[0]
        try:
            response = await self._request("GET", "/api/maibot/latest-memory", params={"session_id": session_id})
            data = response.json()
            content = str(data.get("content") or "").strip()
            self._latest_memory_cache[cache_key] = (content, now)
            return content
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：获取最新记忆整理失败: {exc}")
            return ""

    async def _delete_all_latest_memory(self) -> None:
        """记忆整理关闭时，通知邻舍删除全部已保存的记忆摘要。"""
        try:
            response = await self._request("DELETE", "/api/maibot/latest-memory")
            data = response.json()
            self._get_logger().info(f"邻舍桥接：已删除记忆摘要 deleted={data.get('deleted')}")
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：删除记忆摘要失败: {exc}")

    # ===== 上下文缓存 =====

    def _cache_context(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """按会话缓存本轮请求的文本上下文（供配图判断使用）。"""
        if not session_id:
            return
        context: list[dict[str, str]] = []
        for message in messages:
            role = message.get("role")
            if role not in ("user", "assistant"):
                continue
            text = self._extract_text(message.get("content"))
            if not text:
                continue
            context.append({"role": role, "text": text, "speaker": self._extract_speaker_name(message.get("content"))})
        if len(self._last_context) >= 256:
            self._last_context.clear()
        self._last_context[session_id] = context

    @staticmethod
    def _extract_text(content: Any) -> str:
        """从 Hook 消息 content 中提取纯文本。"""
        if isinstance(content, str):
            return NeighborBridgePlugin._clean_text(content)
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            joined = "\n".join(part.strip() for part in parts if part and part.strip())
            return NeighborBridgePlugin._clean_text(joined)
        return ""

    @staticmethod
    def _extract_speaker_name(content: Any) -> str:
        """从 MaiBot 消息的 <message user="..."> 前缀中解析发言人名称（群聊天然携带用户昵称/ID）。"""
        raw = content if isinstance(content, str) else ""
        if not raw and isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            raw = "\n".join(parts)
        match = _SPEAKER_TAG_RE.search(raw)
        if not match:
            return ""
        return unescape(match.group(1)).strip()

    @staticmethod
    def _clean_text(text: str) -> str:
        """清洗 MaiBot 请求文本：只保留聊天内容，去掉消息元数据、工具调用与模型指令块。"""
        if not text:
            return ""
        lines = text.splitlines()
        kept: list[str] = []
        in_tool_block = False
        in_instruction_block = False
        for raw in lines:
            line = raw.strip()
            if not line:
                continue
            if _MSG_TAG_RE.match(line):
                continue
            if line.startswith(("[图片", "[表情包", "[表情：")):
                continue
            if "已折叠的历史工具调用" in line or line.startswith("[工具调用"):
                in_tool_block = True
                continue
            if in_tool_block:
                if line.startswith(("-", "·", "*")) or line.split(":", 1)[0].strip() in _TOOL_CALL_KEYS:
                    continue
                in_tool_block = False
            if line.startswith(_INSTRUCTION_START_MARKERS):
                in_instruction_block = True
                continue
            if in_instruction_block:
                continue
            kept.append(line)
        return " ".join(kept).strip()

    @staticmethod
    def _replace_system_prompt(messages: list[dict[str, Any]], base_prompt: str) -> None:
        """将列表中第一条 system 消息内容替换为 base_prompt。"""
        for message in messages:
            if message.get("role") == "system":
                message["content"] = base_prompt
                return
        messages.insert(0, {"role": "system", "content": base_prompt})

    @classmethod
    def _extract_maibot_nickname(cls, messages: list[dict[str, Any]]) -> str:
        """从 MaiBot 构造好的 system 提示中反解机器人昵称。"""
        for message in messages:
            if message.get("role") != "system":
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else cls._extract_text(content)
            if not text:
                continue
            match = _REPLYER_BOT_NAME_RE.search(text)
            if match:
                return match.group(1).strip()
            match = _PLANNER_BOT_NAME_RE.search(text)
            if match:
                return match.group(1).strip()
        return ""

    @classmethod
    def _replace_maibot_nickname(cls, messages: list[dict[str, Any]], old_nickname: str, display_name: str) -> int:
        """将请求消息里的 MaiBot 昵称替换为邻舍 display_name，返回被改写的消息数。"""
        old_nickname = str(old_nickname or "").strip()
        display_name = str(display_name or "").strip()
        if not old_nickname or not display_name or old_nickname == display_name:
            return 0
        replaced_count = 0
        for message in messages:
            content = message.get("content")
            new_content = cls._replace_content_nickname(content, old_nickname, display_name)
            if new_content != content:
                message["content"] = new_content
                replaced_count += 1
        return replaced_count

    @staticmethod
    def _replace_content_nickname(content: Any, old_nickname: str, display_name: str) -> Any:
        """替换消息文本中的昵称，保留图片等非文本片段。"""
        if isinstance(content, str):
            return content.replace(old_nickname, display_name)
        if isinstance(content, list):
            replaced_parts: list[Any] = []
            for part in content:
                if isinstance(part, str):
                    replaced_parts.append(part.replace(old_nickname, display_name))
                elif isinstance(part, dict) and part.get("type") == "text":
                    text = str(part.get("text") or "")
                    replaced_parts.append({**part, "text": text.replace(old_nickname, display_name)})
                else:
                    replaced_parts.append(part)
            return replaced_parts
        return content

    @staticmethod
    def _replace_planner_behavior_style(messages: list[dict[str, Any]], behavior_style: str) -> bool:
        """将 Planner system 提示中渲染出的 MaiBot 默认行为风格替换为邻舍行为风格。"""
        style_text = str(behavior_style or "").strip()
        for message in messages:
            if message.get("role") != "system" or not isinstance(message.get("content"), str):
                continue
            replaced_content, replaced_count = _BEHAVIOR_STYLE_BLOCK_RE.subn(
                lambda match: f"{match.group(1)}{style_text}",
                message["content"],
                count=1,
            )
            if replaced_count:
                message["content"] = replaced_content
                return True
        return False

    @classmethod
    def _remove_maibot_style_messages(cls, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """移除 MaiBot 自带的表达习惯与一次性回复风格消息，确保邻舍风格唯一生效。"""
        kept_messages: list[dict[str, Any]] = []
        for message in messages:
            if message.get("role") == "user":
                text = cls._extract_text(message.get("content"))
                if text.startswith(_EXPRESSION_HABITS_MARKER) or text.startswith(_TEMPORARY_REPLY_STYLE_MARKER):
                    continue
            kept_messages.append(message)
        return kept_messages

    # ===== 生图流程 =====

    async def _run_memory_sync(
        self,
        stream_id: str,
        client_msg_id: str,
        user_message: str,
        user_name: str,
        reply_text: str,
    ) -> None:
        """后台任务：仅将本轮对话同步给邻舍保存记忆，不再判断或触发配图。"""
        try:
            payload = {
                "character_name": str(self.config.bridge.character_name or "").strip(),
                "user_name": user_name,
                "user_message": user_message,
                "reply_text": reply_text,
                "client_msg_id": client_msg_id,
                "session_id": stream_id,
                "image_mode": "off",
                "memory_enabled": bool(self.config.memory.memory_curation),
            }
            response = await self._request("POST", "/api/maibot/chat", json=payload)
            data = response.json()
            if not data.get("ok"):
                self._get_logger().warning(f"邻舍桥接：记忆同步返回异常: {data}")
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：记忆同步失败: {exc}")

    async def _run_pending_image_task(self, stream_id: str, task_id: Any) -> None:
        """包装轮询任务，结束后释放当前聊天流的生图任务名额。"""
        try:
            await self._poll_and_send(stream_id, task_id)
        finally:
            self._pending_image_streams.discard(stream_id)

    async def _poll_and_send(self, stream_id: str, task_id: Any) -> None:
        """轮询生图任务，完成后拉取图片并由 MaiBot 发出。"""
        interval = max(float(self.config.image.poll_interval_sec or 2.0), 0.5)
        timeout = max(float(self.config.image.poll_timeout_sec or 180.0), interval)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(interval)
            try:
                response = await self._request("GET", f"/api/maibot/tasks/{task_id}")
                task = response.json()
            except Exception as exc:
                self._get_logger().warning(f"邻舍桥接：查询生图任务失败: {exc}")
                continue
            status = str(task.get("status") or "")
            if status == "done":
                image_url = (task.get("image") or {}).get("url")
                if image_url:
                    await self._download_and_send_image(stream_id, str(image_url))
                return
            if status == "failed":
                self._get_logger().warning(f"邻舍桥接：生图任务失败: {task.get('error')}")
                return
        self._get_logger().warning(f"邻舍桥接：生图任务轮询超时 task_id={task_id}")

    async def _download_and_send_image(self, stream_id: str, image_url: str) -> None:
        """从邻舍拉取图片并 base64 编码后由 MaiBot 发出。"""
        response = await self._request("GET", image_url)
        image_base64 = base64.b64encode(response.content).decode("ascii")
        result = await self.ctx.send.image(image_base64, stream_id)
        self._get_logger().info(f"邻舍桥接：配图已发出 stream_id={stream_id} result={result}")


def create_plugin() -> NeighborBridgePlugin:
    """创建邻舍桥接插件实例。"""
    return NeighborBridgePlugin()