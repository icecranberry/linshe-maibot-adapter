"""邻舍桥接插件：对接 MaiBot 与邻舍（Generate-image-agent）。

能力说明：
1. 人格注入：在 Maisaka 回复器构造完模型请求后（maisaka.replyer.before_model_request），
   将 system 人格整条替换为邻舍角色的 base_prompt，并调用邻舍 /api/maibot/derive-style
   始终提炼行为风格与表达风格，以 user 消息注入请求。
2. 记忆与配图：MaiBot 生成最终回复后（maisaka.reply.before_post_process），将本轮对话交给
   邻舍 /api/maibot/chat 落库记忆并判断是否需要配图；需要配图时轮询 /api/maibot/tasks/:id，
   拿到图片后由 MaiBot 以图片消息发出。

3. 人格管理页：在 WebUI 首页注册 HomeCard，跳转到邻舍托管的人格管理页
   （http://127.0.0.1:3099/api/maibot/plugin-ui），可查看/覆盖注入的 base_prompt、
   提炼风格、开关记忆整理与配图等参数。

独立性：仅依赖 maibot_sdk 与 httpx，不引用 MaiBot 内部代码，可独立上传插件仓库。
"""

from __future__ import annotations

from copy import deepcopy
from html import unescape
from pathlib import Path
from typing import Any

import asyncio
import base64
import json
import re
import time

import httpx

from maibot_sdk import API, Field, HomeCard, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy

CHARACTERS_CACHE_TTL_SEC = 300.0

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
    """插件基础配置。开关与版本信息，详细参数请到 MaiBot 首页「邻舍人格管理」卡片打开的管理页调整。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件（默认开启，无需手动开关）", json_schema_extra={"hidden": True})
    config_version: str = Field(default="1.2.1", description="配置版本", json_schema_extra={"hidden": True})


class BridgeSectionConfig(PluginConfigBase):
    """邻舍连接配置。参数已迁移至首页「邻舍人格管理」管理页，请在那里调整。"""

    __ui_label__ = "邻舍连接"
    __ui_icon__ = "cable"
    __ui_order__ = 1

    base_url: str = Field(
        default="http://127.0.0.1:3099",
        description="邻舍服务地址",
        json_schema_extra={"hidden": True},
    )
    character_name: str = Field(
        default="",
        description="邻舍角色名（characters.name），留空则跳过全部流程",
        json_schema_extra={"hidden": True},
    )


class PersonaSectionConfig(PluginConfigBase):
    """人格注入配置。人格替换与风格提炼为始终开启；参数已迁移至首页「邻舍人格管理」管理页，请在那里调整。"""

    __ui_label__ = "人格注入"
    __ui_icon__ = "user"
    __ui_order__ = 2



class MemorySectionConfig(PluginConfigBase):
    """记忆整理配置。开关已迁移至首页「邻舍人格管理」管理页，请在那里调整。"""

    __ui_label__ = "记忆整理"
    __ui_icon__ = "database"
    __ui_order__ = 3

    memory_curation: bool = Field(
        default=True,
        description="启用对话记忆摘要（同步回邻舍）：每 40 句真实聊天内容整理一份摘要注入 MaiBot 主聊天流；关闭后不再整理，并删除已保存的记忆摘要",
        json_schema_extra={"hidden": True},
    )


class ImageSectionConfig(PluginConfigBase):
    """配图配置。参数已迁移至首页「邻舍人格管理」管理页，请在那里调整。"""

    __ui_label__ = "配图"
    __ui_icon__ = "image"
    __ui_order__ = 4

    image_mode: str = Field(
        default="auto",
        description="auto=由邻舍判断，off=关闭，always=总是配图",
        json_schema_extra={"hidden": True},
    )
    context_max_messages: int = Field(
        default=12,
        description="传给邻舍判断的上下文消息条数",
        json_schema_extra={"hidden": True},
    )
    poll_interval_sec: float = Field(
        default=2.0,
        description="生图任务轮询间隔（秒）",
        json_schema_extra={"hidden": True},
    )
    poll_timeout_sec: float = Field(
        default=180.0,
        description="生图任务轮询超时（秒）",
        json_schema_extra={"hidden": True},
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
        """仅在插件设置页保留基础配置提醒，详细参数统一在首页管理页调整。"""
        schema = super().get_webui_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )
        sections = schema.get("sections")
        if isinstance(sections, dict):
            sections.clear()
            sections["plugin"] = {
                "name": "plugin",
                "title": "详细参数请到 MaiBot 首页底部“邻舍人格管理”卡片打开的管理页调整",
                "description": "启动插件默认激活和邻舍的连接",
                "icon": "package",
                "collapsed": False,
                "order": 0,
                "fields": {},
            }
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
        self._last_context: dict[str, list[dict[str, str]]] = {}
        self._latest_memory_cache: dict[str, tuple[str, float]] = {}

    async def on_load(self) -> None:
        """插件加载：创建 HTTP 客户端并加载本地人格数据。"""
        self._http_client = httpx.AsyncClient(timeout=30)
        await self._load_persona_store()
        self._get_logger().info("邻舍桥接：插件已加载")

    async def on_unload(self) -> None:
        """插件卸载：关闭 HTTP 客户端。"""
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    @HomeCard(
        "persona_manager",
        title="邻舍人格管理",
        description="查看并管理邻舍桥接注入的人格信息与插件参数",
        content=[
            {"type": "stat", "label": "当前激活角色", "value": "未配置"},
            {
                "type": "actions",
                "actions": [{"label": "打开人格管理页", "url": "http://127.0.0.1:3099/api/maibot/plugin-ui"}],
            },
        ],
        runtime_config_path="bridge.character_name",
        width="medium",
        order=6,
    )
    async def persona_manager_home_card(self) -> None:
        """首页卡片标记方法（HomeCard 仅用于声明卡片元数据）。"""
        return None

    @API("persona.get", description="读取邻舍桥接插件的人格数据", version="1", public=True)
    async def get_persona_data(self) -> dict[str, Any]:
        """返回插件当前内存中的人格数据。"""
        async with self._persona_store_lock:
            return {"characters": deepcopy(self._persona_store)}

    @API("persona.update", description="更新邻舍桥接插件的人格数据", version="1", public=True)
    async def update_persona_data(self, character_name: str = "", **kwargs: Any) -> dict[str, Any]:
        """合并更新指定角色的人格数据并立即持久化。"""
        normalized_name = character_name.strip()
        if not normalized_name:
            raise ValueError("character_name 不能为空")

        async with self._persona_store_lock:
            entry = self._persona_store.setdefault(
                normalized_name,
                {"base_prompt": "", "behavior_style": "", "reply_style": "", "updated_at": 0.0},
            )
            for field_name in ("base_prompt", "behavior_style", "reply_style"):
                if field_name in kwargs:
                    entry[field_name] = str(kwargs[field_name]).strip()
            entry["updated_at"] = int(time.time() * 1000)
            self._write_persona_store_unlocked()
            return {"character_name": normalized_name, "entry": deepcopy(entry)}

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
        character = await self._get_character()
        if not character:
            return None
        character_name = str(character.get("name") or "").strip()
        store_entry = await self._get_or_create_store_entry(character_name)

        # base_prompt 优先使用本地副本（用户可在管理页修改），首次拉取邻舍原文后保存副本
        base_prompt = str(store_entry.get("base_prompt") or "").strip()
        if not base_prompt:
            base_prompt = str(character.get("base_prompt") or "").strip()
            if base_prompt:
                store_entry["base_prompt"] = base_prompt
                await self._save_persona_store()
        if not base_prompt:
            return None

        new_messages = deepcopy(messages)
        self._replace_system_prompt(new_messages, base_prompt)
        injected_styles: list[str] = []
        styles = await self._resolve_styles(base_prompt, store_entry)
        style_messages: list[dict[str, str]] = []
        behavior_style = styles.get("behavior_style")
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
            f"system_replaced=True styles={injected_styles} memory_injected={bool(memory_content)}"
        )
        return {"action": "continue", "modified_kwargs": {"messages": new_messages}}

    # ===== 记忆与配图：maisaka.reply.before_post_process =====

    @HookHandler(
        "maisaka.reply.before_post_process",
        name="neighbor_reply_after_generated",
        description="回复生成后交给邻舍保存记忆并判断是否配图，异步取图后由 MaiBot 发出",
        timeout_ms=10000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def handle_reply_before_post_process(self, **kwargs: Any) -> None:
        """回复生成后：触发邻舍记忆落库与配图流程（不阻塞回复）。"""
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
        # 末尾的合成指令消息（如“你想要回复的消息是…”）无真实内容，去掉后再传给邻舍判断
        while context and not context[-1].get("text"):
            context = context[:-1]
        max_context = max(int(self.config.image.context_max_messages or 12), 1)
        context = context[-max_context:]

        client_msg_id = f"{session_id}:{reply_message_id}" if reply_message_id else ""
        self._get_logger().info(
            f"邻舍桥接：已触发记忆与配图流程 session={session_id} reply_len={len(reply_text)}"
        )
        asyncio.create_task(
            self._run_image_flow(session_id, client_msg_id, user_message, user_name, context, reply_text)
        )
        return None

    # ===== 邻舍接口调用 =====

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """发起对邻舍桥接接口的请求。"""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30)
        base_url = str(self.config.bridge.base_url or "").rstrip("/")
        response = await self._http_client.request(method, f"{base_url}{path}", **kwargs)
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
        """按角色名（characters.name）查找邻舍角色。"""
        character_name = str(self.config.bridge.character_name or "").strip()
        if not character_name:
            return None
        characters = await self._fetch_characters()
        for character in characters:
            if str(character.get("name") or "").strip() == character_name:
                return character
        available = "、".join(str(character.get("name") or "") for character in characters)
        self._get_logger().warning(f"邻舍桥接：未找到角色 {character_name}，可用角色：{available}")
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
                resolved[key] = derived[key]
                store_entry[key] = derived[key]
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
            return {
                key: str(data.get(key) or "").strip()
                for key in ("behavior_style", "reply_style")
                if str(data.get(key) or "").strip()
            }
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
                                "base_prompt": str(entry.get("base_prompt") or "").strip(),
                                "behavior_style": str(entry.get("behavior_style") or "").strip(),
                                "reply_style": str(entry.get("reply_style") or "").strip(),
                                "updated_at": float(entry.get("updated_at") or 0.0),
                            }
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：本地人格数据读取失败: {exc}")

    @property
    def _persona_store_path(self) -> Path:
        """返回运行时为当前插件分配的持久化文件路径。"""
        return self.ctx.paths.data_dir / "persona_store.json"

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
                entry = {"base_prompt": "", "behavior_style": "", "reply_style": "", "updated_at": 0.0}
                self._persona_store[character_name] = entry
            else:
                entry.setdefault("base_prompt", "")
                entry.setdefault("behavior_style", "")
                entry.setdefault("reply_style", "")
                entry.setdefault("updated_at", 0.0)
            return entry

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
            if line.startswith("[图片"):
                kept.append("[图片]")
                continue
            if line == "[表情包]":
                kept.append("[表情包]")
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

    # ===== 配图流程 =====

    async def _run_image_flow(
        self,
        stream_id: str,
        client_msg_id: str,
        user_message: str,
        user_name: str,
        context: list[dict[str, str]],
        reply_text: str,
    ) -> None:
        """后台任务：保存记忆 + 判断配图 + 轮询取图并发送。"""
        try:
            payload = {
                "character_name": str(self.config.bridge.character_name or "").strip(),
                "user_name": user_name,
                "user_message": user_message,
                "reply_text": reply_text,
                "context": context,
                "client_msg_id": client_msg_id,
                "session_id": stream_id,
                "image_mode": str(self.config.image.image_mode or "auto").strip(),
                "memory_enabled": bool(self.config.memory.memory_curation),
            }
            response = await self._request("POST", "/api/maibot/chat", json=payload)
            data = response.json()
            if not data.get("image_needed"):
                return
            task_id = data.get("task_id")
            if not task_id:
                return
            await self._poll_and_send(stream_id, task_id)
        except Exception as exc:
            self._get_logger().warning(f"邻舍桥接：配图流程失败: {exc}")

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
