"""AstrBot ATRI 指令大全插件（astrbot_plugin_atri_command_help）.

本项目基于 AcceleratorYue 的 astrbot_plugin_command_help 修改而来，
沿用上游的指令采集、Pillow 本地渲染与缓存机制，并加入 ATRI 主题底图、
按身份过滤管理指令、隐藏规则等改动。

只提供一个指令，用于把「当前已加载的所有插件」的指令汇总成一张图片发出去。

设计要点
--------
1. 指令来源优先使用 AstrBot 4.29+ 官方 ``command_management.list_commands()``：
   它已经处理好了指令组 / 子指令 / 别名 / 权限 / WebUI 里的重命名与禁用状态。
2. 如果官方接口不可用（老版本 AstrBot、数据库未就绪等），自动回退为直接遍历
   ``star_handlers_registry`` 里的过滤器树，保证换版本也不至于直接报错。
3. 出图使用 Pillow 在本机渲染，不依赖远端 T2I 服务，所以离线也能用。
4. 图片写到 AstrBot 的临时目录，并自动清理旧文件，不会越攒越多。

上游：https://github.com/AcceleratorYue/astrbot_plugin_command_help
版本：1.2.0
许可证：MIT
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import time
import types
import typing
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from PIL import Image, ImageDraw, ImageFont

    PIL_AVAILABLE = True
except Exception:  # pragma: no cover - 仅在 Pillow 缺失时触发
    PIL_AVAILABLE = False


PLUGIN_VERSION = "1.2.0"
LOG_PREFIX = "[ATRI指令大全]"

# 临时图片文件名前缀。与上游的 command_help_ 区分开，避免两个插件互相清理旧图。
TEMP_FILE_PREFIX = "atri_command_help"

# 指令名 / 别名。只注册这一个指令；别名让「/帮助」这种直觉用法也能用。
COMMAND_NAMES = ("指令大全",)
COMMAND_ALIASES = ("帮助", "指令帮助", "指令列表")

DEFAULT_OPTIONS: dict[str, Any] = {
    "title": "ATRI指令大全",
    "include_builtin": True,
    "include_admin_only": True,
    "show_aliases": True,
    "columns": 2,
    "exclude_rules": [],
}

# 图片缓存文件保留时长（秒）。超时的旧图会被自动删掉。
IMAGE_RETENTION_SECONDS = 3600

# 模块路径前缀：插件在 AstrBot 里会被加载为 data.plugins.<目录>.main
# 或 astrbot.builtin_stars.<目录>.main。
_MODULE_PREFIXES = ("data.plugins.", "astrbot.builtin_stars.")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------


@dataclass
class CommandEntry:
    """一条指令（可能是指令组，children 里是子指令）。"""

    name: str
    aliases: list[str] = field(default_factory=list)
    description: str = ""
    params: str = ""
    permission: str = "everyone"
    kind: str = "command"  # "command" | "group"
    children: list["CommandEntry"] = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return self.kind == "group"


@dataclass
class PluginSection:
    """一个插件对应的卡片。"""

    key: str
    display_name: str
    description: str = ""
    reserved: bool = False
    entries: list[CommandEntry] = field(default_factory=list)

    @property
    def command_count(self) -> int:
        """叶子指令数量（指令组本身不计入）。"""

        def count(entries: list[CommandEntry]) -> int:
            total = 0
            for entry in entries:
                if entry.is_group:
                    total += count(entry.children)
                else:
                    total += 1
            return total

        return count(self.entries)


@dataclass
class RenderOptions:
    """一次渲染所需的全部选项。"""

    title: str = "ATRI指令大全"
    columns: int = 2
    show_aliases: bool = True
    command_prefix: str = "/"
    plugin_count: int = 0
    command_count: int = 0


def render_fingerprint(
    sections: list[PluginSection], options: RenderOptions
) -> str:
    """计算当前要渲染内容的指纹，用于复用内容相同的上一张图。

    指纹包含标题、栏数、别名开关、唤醒前缀、底图，以及每个插件卡片和
    每条指令的最终展示信息；任一内容变化都会生成新的指纹。
    """

    def entry_key(entry: CommandEntry) -> tuple:
        return (
            entry.name,
            tuple(entry.aliases),
            entry.description,
            entry.params,
            entry.permission,
            entry.kind,
            tuple(entry_key(child) for child in entry.children),
        )

    background = _background_source()
    background_key = (
        (str(background), background.stat().st_mtime_ns)
        if background is not None
        else None
    )

    payload = (
        options.title,
        int(options.columns),
        bool(options.show_aliases),
        options.command_prefix,
        background_key,
        tuple(
            (
                section.key,
                section.display_name,
                section.description,
                section.reserved,
                tuple(entry_key(entry) for entry in section.entries),
            )
            for section in sections
        ),
    )
    return hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 插件元数据解析
# ---------------------------------------------------------------------------


def _module_root(module_path: str | None) -> str:
    """从 handler 的模块路径里取出插件目录名。

    ``data.plugins.astrbot_plugin_x.main`` -> ``astrbot_plugin_x``
    ``astrbot.builtin_stars.builtin_commands.main`` -> ``builtin_commands``
    ``astrbot_plugin_x.main``（测试环境直接 import）-> ``astrbot_plugin_x``
    """

    path = (module_path or "").strip()
    if not path:
        return ""
    for prefix in _MODULE_PREFIXES:
        if path.startswith(prefix):
            rest = path[len(prefix) :]
            return rest.split(".")[0] if rest else ""
    return path.split(".")[0]


def _resolve_metadata(module_path: str | None):
    """尽力找到 handler 所属插件的 StarMetadata。"""
    try:
        from astrbot.core.star.star import star_map, star_registry
    except Exception:  # pragma: no cover - 理论不会发生
        return None

    if module_path:
        meta = star_map.get(module_path)
        if meta is not None:
            return meta

    root = _module_root(module_path)
    if not root:
        return None
    for meta in star_registry:
        if meta is None:
            continue
        if (meta.root_dir_name or "") == root:
            return meta
    for meta in star_registry:
        if meta is not None and _module_root(meta.module_path) == root:
            return meta
    return None


def _plugin_dir_candidates(root: str) -> list[Path]:
    """列出插件目录可能的绝对路径（用于找 metadata.yaml）。"""

    candidates: list[Path] = []

    # 1) 通过模块 spec 反查：最可靠，不依赖当前工作目录。
    try:
        import importlib.util

        for prefix in _MODULE_PREFIXES + ("",):
            try:
                spec = importlib.util.find_spec(f"{prefix}{root}.main")
            except (ImportError, AttributeError, ValueError):
                spec = None
            if spec is not None and spec.origin:
                candidates.append(Path(spec.origin).resolve().parent)
                break
    except Exception:
        pass

    # 2) AstrBot 自带的路径工具（插件目录 / 内置插件目录）。
    try:
        from astrbot.core.utils.astrbot_path import (
            get_astrbot_builtin_plugin_path,
            get_astrbot_plugin_path,
        )

        candidates.append(Path(get_astrbot_plugin_path()) / root)
        candidates.append(Path(get_astrbot_builtin_plugin_path()) / root)
    except Exception:
        pass

    # 3) 本插件所在目录的兄弟目录。
    candidates.append(Path(__file__).resolve().parent.parent / root)
    return candidates


def _fallback_plugin_meta(root: str) -> dict[str, str]:
    """兜底：直接读插件目录里的 metadata.yaml（测试环境也能拿到展示名）。"""

    result = {"display_name": "", "name": "", "desc": ""}
    if not root:
        return result

    for plugin_dir in _plugin_dir_candidates(root):
        metadata_path = plugin_dir / "metadata.yaml"
        if not metadata_path.is_file():
            metadata_path = plugin_dir / "metadata.yml"
        if not metadata_path.is_file():
            continue
        try:
            import yaml  # AstrBot 自带 pyyaml

            raw = yaml.safe_load(metadata_path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict):
                continue
            result["display_name"] = str(raw.get("display_name") or "")
            result["name"] = str(raw.get("name") or "")
            result["desc"] = str(raw.get("short_desc") or raw.get("desc") or "")
            return result
        except Exception:
            continue
    return result


def _pretty_name(name: str) -> str:
    """没有 display_name 时，把 astrbot_plugin_xxx 之类收拾得好看一点。"""

    text = (name or "").strip()
    if not text:
        return text
    for prefix in ("astrbot_plugin_", "astrbot_"):
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.replace("_", " ").replace("-", " ").strip()
    if text and text[0].isascii() and text[0].islower():
        text = text[0].upper() + text[1:]
    return text or name


# 少数内置插件没有中文展示名/简介，这里补一下，避免图里出现英文目录名。
_DISPLAY_OVERRIDES: dict[str, tuple[str, str]] = {
    "builtin_commands": (
        "基础指令",
        "AstrBot 自带的基础指令，例如 /reset、/new、/stats",
    ),
    "astrbot": ("AstrBot 框架", "AstrBot 框架自带的内部插件"),
}


def _section_for(
    module_path: str | None,
    fallback_name: str | None,
    memo: dict[str, PluginSection],
) -> PluginSection:
    """为某个 handler 找到/创建它的插件卡片。"""

    root = _module_root(module_path) or (fallback_name or "unknown")
    if root in memo:
        return memo[root]

    meta = _resolve_metadata(module_path)
    yaml_meta = _fallback_plugin_meta(root)

    raw_name = (
        (getattr(meta, "display_name", None) if meta else None)
        or yaml_meta["display_name"]
        or (getattr(meta, "name", None) if meta else None)
        or yaml_meta["name"]
        or fallback_name
        or root
    )
    display_name = _pretty_name(str(raw_name)) if raw_name else root
    desc = (
        (getattr(meta, "short_desc", None) if meta else None)
        or yaml_meta["desc"]
        or (getattr(meta, "desc", None) if meta else None)
        or ""
    )
    if root in _DISPLAY_OVERRIDES:
        override_name, override_desc = _DISPLAY_OVERRIDES[root]
        display_name = override_name
        desc = override_desc or desc
    reserved = bool(getattr(meta, "reserved", False)) if meta else root in (
        "builtin_commands",
        "astrbot",
    )

    section = PluginSection(
        key=root,
        display_name=str(display_name),
        description=_clean_description(str(desc)),
        reserved=reserved,
    )
    memo[root] = section
    return section


# ---------------------------------------------------------------------------
# 描述 / 参数辅助
# ---------------------------------------------------------------------------

_DOC_PARAM_RE = re.compile(r"参数\s*[:：]\s*(.+)$")


def _clean_description(text: str | None) -> str:
    """把 handler 的 docstring 收拾成适合显示的一句话描述。"""

    if not text:
        return ""
    raw = str(text).strip()
    if not raw:
        return ""
    # 只取第一行有内容的文字。
    for line in raw.splitlines():
        line = line.strip()
        if line:
            raw = line
            break
    raw = _DOC_PARAM_RE.sub("", raw).strip()
    return raw.rstrip("。；;，,：:").strip() or ""


def _split_doc_params(text: str | None) -> tuple[str, str]:
    """从 docstring 里拆出「参数: ...」这种用法提示。"""

    if not text:
        return "", ""
    raw = str(text).strip()
    if not raw:
        return "", ""
    first = raw.splitlines()[0].strip() if raw.splitlines() else raw
    match = _DOC_PARAM_RE.search(first)
    if not match:
        return _clean_description(raw), ""
    hint = match.group(1).strip()
    desc = _clean_description(raw)
    return desc, hint


def _format_param_hint(params: dict[str, Any]) -> str:
    """把 CommandFilter.handler_params 渲染成 <必填> / [可选] 的提示。"""

    parts: list[str] = []
    for name, value in params.items():
        if value is None:
            parts.append(f"[{name}]")
            continue
        if _is_greedy(value):
            parts.append(f"[{name}]")
            continue
        if isinstance(value, type):
            parts.append(f"<{name}>")
            continue
        origin = typing.get_origin(value)
        if origin in (typing.Union, types.UnionType):
            parts.append(f"<{name}>")
            continue
        parts.append(f"[{name}]")
    return " ".join(parts)


def _is_greedy(value: Any) -> bool:
    try:
        from astrbot.core.star.filter.command import GreedyStr

        return value is GreedyStr
    except Exception:
        return False


def _permission_of(handler_md: Any) -> str:
    """从 handler 的过滤器里读权限级别，转成 admin / group_admin / member。"""

    try:
        from astrbot.core.star.filter.permission import (
            COMMAND_PERMISSION_TYPES,
            PermissionTypeFilter,
        )
    except Exception:
        return "everyone"
    for event_filter in getattr(handler_md, "event_filters", []) or []:
        if isinstance(event_filter, PermissionTypeFilter):
            for name, permission in COMMAND_PERMISSION_TYPES.items():
                if event_filter.permission_type == permission:
                    return name
    return "everyone"


# ---------------------------------------------------------------------------
# 采集：官方接口（AstrBot 4.29+）
# ---------------------------------------------------------------------------


def _build_param_index() -> dict[str, str]:
    """handler_full_name -> 参数提示。"""

    index: dict[str, str] = {}
    try:
        from astrbot.core.star.filter.command import CommandFilter
        from astrbot.core.star.filter.command_group import CommandGroupFilter
        from astrbot.core.star.star_handler import star_handlers_registry
    except Exception:
        return index

    def remember(command_filter: Any) -> None:
        try:
            handler_md = command_filter.get_handler_md()
        except Exception:
            handler_md = getattr(command_filter, "handler_md", None)
        if handler_md is None:
            return
        hint = _format_param_hint(getattr(command_filter, "handler_params", {}) or {})
        if hint:
            index[handler_md.handler_full_name] = hint

    def walk_group(group_filter: Any) -> None:
        for sub_filter in getattr(group_filter, "sub_command_filters", []) or []:
            if isinstance(sub_filter, CommandFilter):
                remember(sub_filter)
            elif isinstance(sub_filter, CommandGroupFilter):
                walk_group(sub_filter)

    for handler in star_handlers_registry:
        for event_filter in getattr(handler, "event_filters", []) or []:
            if isinstance(event_filter, CommandFilter):
                remember(event_filter)
            elif isinstance(event_filter, CommandGroupFilter):
                walk_group(event_filter)
    return index


def _entry_from_descriptor(
    item: dict[str, Any], param_index: dict[str, str]
) -> CommandEntry | None:
    name = (
        item.get("effective_command")
        or item.get("original_command")
        or item.get("handler_name")
        or ""
    ).strip()
    if not name:
        return None

    parent = (item.get("parent_signature") or "").strip()
    aliases: list[str] = []
    for alias in item.get("aliases") or []:
        alias = str(alias).strip()
        if not alias:
            continue
        full_alias = f"{parent} {alias}".strip() if parent else alias
        if full_alias != name:
            aliases.append(full_alias)

    handler_full_name = item.get("handler_full_name") or ""
    params = param_index.get(handler_full_name, "")
    description = _clean_description(item.get("description") or "")
    children = [
        child
        for child in (
            _entry_from_descriptor(sub or {}, param_index)
            for sub in item.get("sub_commands") or []
        )
        if child is not None
    ]

    # 官方接口不直接给参数时，尝试从描述里的「参数: ...」拿提示。
    if not params:
        description, params = _split_doc_params(item.get("description") or "")

    is_group = bool(item.get("is_group")) or item.get("type") == "group"
    return CommandEntry(
        name=name,
        aliases=aliases,
        description=description,
        params=params,
        permission=str(item.get("permission") or "everyone"),
        kind="group" if is_group else "command",
        children=children,
    )


async def _collect_from_official_api() -> list[PluginSection]:
    """使用官方 command_management.list_commands() 采集。"""

    from astrbot.core.star import command_management

    raw = await command_management.list_commands()
    param_index = _build_param_index()
    memo: dict[str, PluginSection] = {}

    for item in raw or []:
        if not isinstance(item, dict):
            continue
        if not item.get("enabled", True):
            continue
        if not item.get("plugin_activated", True):
            continue
        section = _section_for(
            item.get("module_path") or "",
            item.get("plugin_display_name") or item.get("plugin") or "",
            memo,
        )
        entry = _entry_from_descriptor(item, param_index)
        if entry is not None:
            section.entries.append(entry)

    return _finalize_sections(memo)


# ---------------------------------------------------------------------------
# 采集：兜底（直接遍历 star_handlers_registry）
# ---------------------------------------------------------------------------


def _collect_from_registry() -> list[PluginSection]:
    """不依赖官方指令管理接口的兜底采集。

    注意：AstrBot 会把子指令的 CommandFilter 同时挂在子指令 handler 的
    event_filters 上（并打上 ``sub_command`` 标记），所以必须先由父指令组
    递归消费掉这些子指令，再处理顶层指令，否则图里会出现两遍。
    """

    from astrbot.core.star.filter.command import CommandFilter
    from astrbot.core.star.filter.command_group import CommandGroupFilter
    from astrbot.core.star.star_handler import (
        EventType,
        star_handlers_registry,
    )

    memo: dict[str, PluginSection] = {}
    consumed: set[str] = set()

    try:
        handlers = star_handlers_registry.get_handlers_by_event_type(
            EventType.AdapterMessageEvent
        )
    except Exception:
        handlers = list(star_handlers_registry)

    filter_owner: dict[int, Any] = {}
    for handler in handlers:
        for event_filter in getattr(handler, "event_filters", []) or []:
            filter_owner[id(event_filter)] = handler

    def enabled(handler_md: Any) -> bool:
        return handler_md is None or bool(getattr(handler_md, "enabled", True))

    def build_command(
        command_filter: Any, handler_md: Any
    ) -> CommandEntry | None:
        names = [n for n in command_filter.get_complete_command_names() if n]
        if not names:
            return None
        if handler_md is not None:
            consumed.add(handler_md.handler_full_name)
        description, doc_params = _split_doc_params(
            getattr(handler_md, "desc", "") or ""
        )
        params = doc_params or _format_param_hint(
            getattr(command_filter, "handler_params", {}) or {}
        )
        return CommandEntry(
            name=names[0],
            aliases=[alias for alias in names[1:] if alias != names[0]],
            description=description,
            params=params,
            permission=_permission_of(handler_md),
            kind="command",
        )

    def build_group(
        group_filter: Any, handler_md: Any
    ) -> CommandEntry | None:
        names = [n for n in group_filter.get_complete_command_names() if n]
        if not names:
            return None
        if handler_md is not None:
            consumed.add(handler_md.handler_full_name)
        description, _ = _split_doc_params(getattr(handler_md, "desc", "") or "")
        children: list[CommandEntry] = []
        for sub_filter in getattr(group_filter, "sub_command_filters", []) or []:
            if isinstance(sub_filter, CommandFilter):
                sub_handler = None
                try:
                    sub_handler = sub_filter.get_handler_md()
                except Exception:
                    sub_handler = getattr(sub_filter, "handler_md", None)
                if not enabled(sub_handler):
                    continue
                entry = build_command(sub_filter, sub_handler)
            elif isinstance(sub_filter, CommandGroupFilter):
                sub_handler = filter_owner.get(id(sub_filter))
                if not enabled(sub_handler):
                    continue
                entry = build_group(sub_filter, sub_handler)
            else:
                entry = None
            if entry is not None:
                children.append(entry)
        return CommandEntry(
            name=names[0],
            aliases=[alias for alias in names[1:] if alias != names[0]],
            description=description,
            permission=_permission_of(handler_md),
            kind="group",
            children=children,
        )

    # 第一遍：先处理「根指令组」，把它们的子指令标记为已消费。
    group_entries: dict[str, tuple[PluginSection, CommandEntry]] = {}
    for handler in handlers:
        if not getattr(handler, "enabled", True):
            continue
        if getattr(handler, "extras_configs", {}).get("sub_command"):
            continue  # 子指令交给父指令组渲染
        for event_filter in getattr(handler, "event_filters", []) or []:
            if not isinstance(event_filter, CommandGroupFilter):
                continue
            if getattr(event_filter, "parent_group", None) is not None:
                continue  # 子指令组交给父指令组渲染
            section = _section_for(handler.handler_module_path, "", memo)
            entry = build_group(event_filter, handler)
            if entry is not None:
                group_entries[handler.handler_full_name] = (section, entry)

    # 第二遍：按注册表顺序摆放，保证同一插件内的指令顺序与源码一致。
    for handler in handlers:
        if not getattr(handler, "enabled", True):
            continue
        if handler.handler_full_name in group_entries:
            section, entry = group_entries[handler.handler_full_name]
            section.entries.append(entry)
            continue
        if getattr(handler, "extras_configs", {}).get("sub_command"):
            continue
        for event_filter in getattr(handler, "event_filters", []) or []:
            if not isinstance(event_filter, CommandFilter):
                continue
            if handler.handler_full_name in consumed:
                continue
            section = _section_for(handler.handler_module_path, "", memo)
            entry = build_command(event_filter, handler)
            if entry is not None:
                section.entries.append(entry)

    return _finalize_sections(memo)


# ---------------------------------------------------------------------------
# 汇总 / 过滤 / 排序
# ---------------------------------------------------------------------------


def _dedupe_entries(entries: list[CommandEntry]) -> list[CommandEntry]:
    """按指令全名去重（防御性处理：不同注册路径偶尔会重复登记）。"""

    seen: dict[str, CommandEntry] = {}
    order: list[str] = []
    for entry in entries:
        key = entry.name
        if key not in seen:
            entry.children = _dedupe_entries(entry.children)
            seen[key] = entry
            order.append(key)
            continue
        existing = seen[key]
        if entry.is_group and existing.is_group:
            existing.children = _dedupe_entries(existing.children + entry.children)
        existing.aliases = list(dict.fromkeys(existing.aliases + entry.aliases))
        if not existing.description and entry.description:
            existing.description = entry.description
        if not existing.params and entry.params:
            existing.params = entry.params
    return [seen[key] for key in order]


def _finalize_sections(memo: dict[str, PluginSection]) -> list[PluginSection]:
    sections: list[PluginSection] = []
    for section in memo.values():
        section.entries = _dedupe_entries(section.entries)
        if section.key == "builtin_commands":
            _localize_builtin_descriptions(section.entries)
        if section.entries:
            sections.append(section)
    sections.sort(key=lambda s: (s.reserved, s.display_name.lower()))
    return sections


# AstrBot 内置指令的 docstring 是英文的，这里补一份中文说明，免得图里中英混杂。
_BUILTIN_COMMAND_DESCRIPTIONS = {
    "help": "查看 AstrBot 自带帮助",
    "sid": "查看当前会话 ID 等信息",
    "name": "设置当前会话的显示名",
    "reset": "清空当前会话的上下文",
    "stop": "停止正在运行的 Agent",
    "new": "新建一个会话",
    "stats": "查看当前会话的 token 用量",
    "provider": "查看 / 切换 LLM 提供商",
    "dashboard_update": "更新 AstrBot WebUI",
    "set": "设置会话变量",
    "unset": "删除会话变量",
}


def _localize_builtin_descriptions(entries: list[CommandEntry]) -> None:
    for entry in entries:
        description = _BUILTIN_COMMAND_DESCRIPTIONS.get(entry.name)
        if description:
            entry.description = description
        if entry.children:
            _localize_builtin_descriptions(entry.children)


# 需要管理权限的指令类型。普通用户请求图片时，这些指令不会出现在图里。
_ADMIN_PERMISSIONS = {"admin", "group_admin", "shared_group_admin"}


def filter_sections(
    sections: list[PluginSection],
    *,
    include_builtin: bool = True,
    include_admin_only: bool = True,
) -> list[PluginSection]:
    """按配置过滤掉内置插件 / 仅管理员指令。"""

    def prune(entries: list[CommandEntry]) -> list[CommandEntry]:
        kept: list[CommandEntry] = []
        for entry in entries:
            if not include_admin_only and entry.permission in _ADMIN_PERMISSIONS:
                continue
            if entry.is_group:
                entry.children = prune(entry.children)
                if entry.children:
                    kept.append(entry)
                continue
            kept.append(entry)
        return kept

    result: list[PluginSection] = []
    for section in sections:
        if section.reserved and not include_builtin:
            continue
        section.entries = prune(section.entries)
        if section.entries:
            result.append(section)
    return result


# 本插件自己也注册了一条指令，采集时自然会把自己也收进来。
# 但「指令大全」没必要出现在自己生成的指令大全里，所以固定摘掉；
# 上游同名插件（若与本插件共存）也一并摘掉，免得又冒出一张同款卡片。
_SELF_DIR_NAMES = {
    "astrbot_plugin_atri_command_help",
    "astrbot_plugin_command_help",
}
_SELF_DISPLAY_NAMES = {"atri指令大全", "指令大全"}


def _is_self_section(section: PluginSection) -> bool:
    """判断这张卡片是不是「指令大全」本身。"""

    if section.key.strip().lower() in _SELF_DIR_NAMES:
        return True
    return section.display_name.strip().lower() in _SELF_DISPLAY_NAMES


def drop_self_sections(sections: list[PluginSection]) -> list[PluginSection]:
    """摘掉「指令大全」自己的卡片，只保留其他插件的指令。"""

    return [section for section in sections if not _is_self_section(section)]


# ---------------------------------------------------------------------------
# 隐藏规则：让管理员在设置页里挑掉不想展示的插件 / 指令
# ---------------------------------------------------------------------------


@dataclass
class ExcludeRule:
    """一条隐藏规则：某个插件下要隐藏的指令。"""

    plugin: str = ""
    commands: list[str] = field(default_factory=list)

    @property
    def whole_plugin(self) -> bool:
        """没有填写指令（或填了 *）表示隐藏整个插件。"""

        return not self.commands or any(
            str(item).strip() in ("*", "＊") for item in self.commands
        )


_SPLIT_RE = re.compile(r"[,，、;；\r\n]+")
_LINE_SPLIT_RE = re.compile(r"[:：|｜]")


def _split_command_text(text: Any) -> list[str]:
    """把「指令A, 指令B」这类文本拆成列表（兼容 list）。

    只按逗号 / 顿号 / 分号 / 换行拆分，不按空格拆，
    这样「设置 子项」这种带空格的指令全名也能正确填进来。
    """

    if text is None:
        return []
    if isinstance(text, (list, tuple, set)):
        parts: list[str] = []
        for item in text:
            parts.extend(_split_command_text(item))
        return parts
    return [part.strip() for part in _SPLIT_RE.split(str(text)) if part.strip()]


def parse_exclude_rules(raw: Any) -> list[ExcludeRule]:
    """解析设置页里的隐藏规则。

    兼容三种写法：
    1. 设置页的 template_list：``[{"plugin": "...", "commands": "..."}]``；
    2. 字符串列表：``["插件名: 指令A, 指令B"]``；
    3. 多行文本：每行一条 ``插件名: 指令A, 指令B``，指令留空表示整个插件。
    """

    rules: list[ExcludeRule] = []
    if raw is None:
        return rules

    if isinstance(raw, str):
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = _LINE_SPLIT_RE.split(line, maxsplit=1)
            plugin = parts[0].strip()
            commands = _split_command_text(parts[1]) if len(parts) > 1 else []
            if plugin:
                rules.append(ExcludeRule(plugin=plugin, commands=commands))
        return rules

    if isinstance(raw, dict):
        raw = [raw]

    if isinstance(raw, (list, tuple, set)):
        for item in raw:
            if isinstance(item, str):
                rules.extend(parse_exclude_rules(item))
                continue
            if not isinstance(item, dict):
                continue
            plugin = str(
                item.get("plugin")
                or item.get("plugin_name")
                or item.get("name")
                or ""
            ).strip()
            command_value = item.get("commands")
            if command_value is None:
                command_value = item.get("command", "")
            commands = _split_command_text(command_value)
            if plugin or commands:
                rules.append(ExcludeRule(plugin=plugin, commands=commands))

    return rules


def _norm_token(value: Any) -> str:
    """统一插件名 / 指令名的比较形式（忽略大小写、前缀、空格与分隔符）。"""

    text = str(value or "").strip().lower()
    text = text.replace("\\", "/")
    for prefix in ("astrbot_plugin_", "astrbot_"):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.lstrip("/／!！")
    return re.sub(r"[\s_\-]+", "", text)


def _plugin_matches(rule: ExcludeRule, section: PluginSection) -> bool:
    pattern = _norm_token(rule.plugin)
    if pattern in ("", "*"):
        return True
    candidates = {_norm_token(section.key), _norm_token(section.display_name)}
    return any(
        candidate and fnmatch.fnmatchcase(candidate, pattern)
        for candidate in candidates
    )


def _entry_matches(rule: ExcludeRule, entry: CommandEntry) -> bool:
    if rule.whole_plugin:
        return True

    patterns = [pattern for pattern in (_norm_token(c) for c in rule.commands) if pattern]
    if not patterns:
        return True

    candidates = [_norm_token(entry.name)]
    candidates.extend(_norm_token(alias) for alias in entry.aliases)
    leaf = entry.name.split()[-1] if entry.name.split() else entry.name
    candidates.append(_norm_token(leaf))

    for pattern in patterns:
        if pattern == "*":
            return True
        if any(
            candidate and fnmatch.fnmatchcase(candidate, pattern)
            for candidate in candidates
        ):
            return True
    return False


def apply_exclude_rules(
    sections: list[PluginSection], rules: list[ExcludeRule]
) -> list[PluginSection]:
    """按隐藏规则裁剪插件与指令；指令组被掏空时一并去掉。"""

    if not rules:
        return sections

    def prune(section: PluginSection, entries: list[CommandEntry]) -> list[CommandEntry]:
        kept: list[CommandEntry] = []
        for entry in entries:
            if any(
                _plugin_matches(rule, section) and _entry_matches(rule, entry)
                for rule in rules
            ):
                continue
            if entry.is_group:
                entry.children = prune(section, entry.children)
                if not entry.children:
                    continue
            kept.append(entry)
        return kept

    result: list[PluginSection] = []
    for section in sections:
        section.entries = prune(section, section.entries)
        if section.entries:
            result.append(section)
    return result


async def collect_command_sections(
    *,
    include_builtin: bool = True,
    include_admin_only: bool = True,
    exclude_rules: list[ExcludeRule] | None = None,
) -> list[PluginSection]:
    """采集全部插件指令；官方接口失败时自动回退。"""

    try:
        sections = await _collect_from_official_api()
        source = "official"
    except Exception as exc:
        logger.debug(f"{LOG_PREFIX} 官方指令接口不可用，改用注册表遍历：{exc}")
        sections = _collect_from_registry()
        source = "registry"

    sections = filter_sections(
        sections,
        include_builtin=include_builtin,
        include_admin_only=include_admin_only,
    )
    sections = apply_exclude_rules(sections, exclude_rules or [])
    # 「指令大全」自己不该出现在自己生成的图里。
    sections = drop_self_sections(sections)
    total = sum(section.command_count for section in sections)
    logger.debug(
        f"{LOG_PREFIX} 采集完成（{source}）：{len(sections)} 个插件，{total} 条指令"
    )
    return sections


def sections_to_text(sections: list[PluginSection], prefix: str = "/") -> str:
    """Pillow 不可用时的纯文本兜底输出。"""

    lines: list[str] = ["ATRI指令大全（图片渲染不可用，已降级为文本）", ""]
    for section in sections:
        lines.append(f"【{section.display_name}】")
        for entry in section.entries:
            lines.append(f"  {prefix}{entry.name}" + (f" {entry.params}" if entry.params else ""))
            for child in entry.children:
                lines.append(
                    f"    {prefix}{child.name}"
                    + (f" {child.params}" if child.params else "")
                )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 渲染：颜色 / 字体 / 文本工具
# ---------------------------------------------------------------------------

CANVAS_W = 1440
MARGIN_X = 34
COL_GAP = 20
CARD_GAP_Y = 22
HEADER_H = 150
LEGEND_H = 54
LEGEND_Y = HEADER_H - 36
CARDS_TOP = LEGEND_Y + LEGEND_H + 22
CARD_PAD = 22
CARD_RADIUS = 16
FOOTER_H = 50

BG = "#edf1f7"
# 模块背景不透明度：204 / 255 ≈ 80%，即 20% 透明。
CARD_ALPHA = 204
INK = "#1f2937"
INK_SOFT = "#3f4a58"
MUTED = "#5b6675"
MUTED_LIGHT = "#8a94a3"
LINE = "#e2e8f0"

# 底图：与 main.py 放在同一目录；找不到时退回纯色背景。
BACKGROUND_IMAGE_NAME = "ATRI.jpeg"
# 竖向裁剪锚点（0 = 顶部，1 = 底部）。略高于中心，避免短图把人物头部裁掉。
BACKGROUND_ANCHOR_Y = 0.30

ACCENTS = [
    "#2f7fd1",
    "#2f9f6f",
    "#d9822b",
    "#8a5cd6",
    "#cf4d63",
    "#27919c",
    "#b8891c",
    "#5967c9",
    "#b3528a",
    "#4b8b3b",
]

PERMISSION_BADGES = {
    "admin": ("管理员", "#fee2e2", "#fca5a5", "#b91c1c"),
    "group_admin": ("群管", "#ede9fe", "#c4b5fd", "#6d28d9"),
    "shared_group_admin": ("群管", "#ede9fe", "#c4b5fd", "#6d28d9"),
}

# 常见 Linux 发行版自带的中文字体（黑体 / 无衬线优先）。
# 不同发行版的安装路径不完全一样，除了这里列出的固定路径，还会递归扫描字体目录兜底。
_LINUX_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Regular.otf",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
]

_LINUX_CJK_BOLD_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/source-han-sans/SourceHanSansSC-Bold.otf",
] + _LINUX_CJK_FONT_CANDIDATES

# Windows / macOS 作为开发机兜底，Linux 上这些路径不存在，会自动跳过。
_WINDOWS_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]

_WINDOWS_BOLD_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyhbd.ttf",
] + _WINDOWS_FONT_CANDIDATES

_MAC_FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]

# 递归扫描字体目录时，只挑名字里带这些关键字的字体，避免把整个字体库都加载进来。
_CJK_FONT_KEYWORDS = (
    "notosanscjk",
    "notosanssc",
    "notoserifcjk",
    "sourcehansans",
    "sourcehanserif",
    "wqy",
    "droidsansfallback",
    "uming",
    "ukai",
    "simhei",
    "msyh",
)

_LINUX_FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "~/.local/share/fonts",
    "~/.fonts",
)

_discovered_font_cache: list[str] | None = None


def _discover_linux_cjk_fonts() -> list[str]:
    """递归扫描常见字体目录，找出可用的中文字体文件（结果缓存）。"""

    global _discovered_font_cache
    if _discovered_font_cache is not None:
        return _discovered_font_cache

    found: list[str] = []
    for raw_dir in _LINUX_FONT_DIRS:
        base = Path(raw_dir).expanduser()
        if not base.is_dir():
            continue
        try:
            for root, _dirs, files in os.walk(base):
                for filename in files:
                    if os.path.splitext(filename)[1].lower() not in {
                        ".ttf",
                        ".ttc",
                        ".otf",
                    }:
                        continue
                    if any(
                        keyword in filename.lower()
                        for keyword in _CJK_FONT_KEYWORDS
                    ):
                        found.append(os.path.join(root, filename))
        except OSError:
            continue

    # 去重并保持稳定顺序。
    seen: set[str] = set()
    unique: list[str] = []
    for path in found:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    _discovered_font_cache = unique
    return unique


def _fontconfig_cjk_fonts() -> list[str]:
    """用 fontconfig 兜底查找已安装的中文字体（Linux 常见）。"""

    import shutil
    import subprocess

    if shutil.which("fc-list") is None:
        return []
    try:
        result = subprocess.run(
            ["fc-list", ":lang=zh-cn", "-f", "%{file}\n"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    paths: list[str] = []
    for line in result.stdout.splitlines():
        path = line.strip()
        if (
            path
            and os.path.isfile(path)
            and os.path.splitext(path)[1].lower() in {".ttf", ".ttc", ".otf"}
        ):
            paths.append(path)
    return paths


def _order_discovered_fonts(fonts: list[str], bold: bool) -> list[str]:
    """加粗请求优先选 Bold 文件，常规请求优先选 Regular 文件。"""

    bold_markers = ("bold", "bd", "heavy", "black")
    preferred: list[str] = []
    others: list[str] = []
    for path in fonts:
        name = Path(path).name.lower()
        is_bold = any(marker in name for marker in bold_markers)
        (preferred if is_bold == bold else others).append(path)
    return preferred + others


def _sibling_font_paths() -> list[str]:
    """其他已装插件自带的字体，作为最后兜底（本机没有中文字体时）。"""

    base = Path(__file__).resolve().parent.parent
    return [
        str(base / "astrbot_plugin_zhenxunribao" / "res" / "font" / "HarmonyOS_Sans_SC_Bold.ttf"),
        str(base / "astrbot_plugin_zhenxunribao" / "res" / "font" / "HarmonyOS_Sans_SC_Regular.ttf"),
        str(base / "astrbot_plugin_fortnue" / "fonts" / "1.ttf"),
    ]


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _mix(color: str, other: str, ratio: float) -> tuple[int, int, int]:
    a = _hex_to_rgb(color)
    b = _hex_to_rgb(other)
    return tuple(round(a[i] + (b[i] - a[i]) * ratio) for i in range(3))  # type: ignore[return-value]


def _tint(color: str, ratio: float) -> tuple[int, int, int]:
    return _mix(color, "#ffffff", ratio)


_font_cache: dict[tuple[int, bool], Any] = {}
_font_path_cache: dict[bool, str] = {}


def _find_font_path(bold: bool) -> str:
    if bold in _font_path_cache:
        return _font_path_cache[bold]

    # 1. Linux 常见固定路径（Noto / 思源黑体 / 文泉驿 / Droid Fallback）。
    linux_candidates = (
        _LINUX_CJK_BOLD_FONT_CANDIDATES if bold else _LINUX_CJK_FONT_CANDIDATES
    )
    for path in linux_candidates:
        if path and os.path.isfile(path):
            _font_path_cache[bold] = path
            return path

    # 2. 发行版路径不同，递归扫描字体目录。
    for path in _order_discovered_fonts(_discover_linux_cjk_fonts(), bold):
        if os.path.isfile(path):
            _font_path_cache[bold] = path
            return path

    # 2.1 仍找不到时，交给 fontconfig 查系统里已安装的中文字体。
    for path in _order_discovered_fonts(_fontconfig_cjk_fonts(), bold):
        if os.path.isfile(path):
            _font_path_cache[bold] = path
            return path

    # 3. Windows / macOS 开发机兜底，以及兄弟插件自带字体。
    other_candidates = (
        _WINDOWS_BOLD_FONT_CANDIDATES if bold else _WINDOWS_FONT_CANDIDATES
    )
    for path in [*other_candidates, *_MAC_FONT_CANDIDATES, *_sibling_font_paths()]:
        if path and os.path.isfile(path):
            _font_path_cache[bold] = path
            return path

    # 都不存在时，让调用方看到明确错误。
    raise RuntimeError(
        "未找到可用的中文字体，请安装 fonts-noto-cjk（Noto Sans CJK）"
        "或 fonts-wqy-microhei（文泉驿微米黑）后重试"
    )


def _load_truetype(path: str, size: int):
    """加载字体；对 TTC 字体集合优先选简体中文（SC）字面。"""

    if path.lower().endswith(".ttc"):
        for index in range(10):
            try:
                loaded = ImageFont.truetype(path, size=size, index=index)
            except OSError:
                break
            try:
                family, _style = loaded.getname()
            except Exception:
                family = ""
            if "sc" in family.lower() or "simplified" in family.lower():
                return loaded
    try:
        return ImageFont.truetype(path, size=size, index=0)
    except OSError:
        return ImageFont.truetype(path, size=size)


def _font(size: int, bold: bool = False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    if not PIL_AVAILABLE:  # pragma: no cover
        raise RuntimeError("Pillow 未安装")
    path = _find_font_path(bold)
    loaded = _load_truetype(path, size)
    _font_cache[key] = loaded
    return loaded


_MEASURE_IMG = Image.new("RGB", (8, 8)) if PIL_AVAILABLE else None
_MEASURE_DRAW = ImageDraw.Draw(_MEASURE_IMG) if PIL_AVAILABLE else None


def _tw(text: str, font) -> int:
    if not text:
        return 0
    return int(_MEASURE_DRAW.textlength(text, font=font))


def _wrap(text: str, font, max_w: int, max_lines: int = 2) -> list[str]:
    """按像素宽度折行（CJK 逐字，西文尽量按空格断）。"""

    if not text or max_w <= 8:
        return []
    text = re.sub(r"\s+", " ", str(text).strip())
    lines: list[str] = []
    current = ""
    for ch in text:
        if _tw(current + ch, font) <= max_w:
            current += ch
            continue
        if " " in current and _tw(current.rsplit(" ", 1)[0], font) > max_w * 0.35:
            head, _, tail = current.rpartition(" ")
            lines.append(head)
            current = tail + ch
        else:
            lines.append(current)
            current = ch
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = _ellipsize(lines[-1] + "…", font, max_w)
    return [line for line in lines if line]


def _ellipsize(text: str, font, max_w: int) -> str:
    if _tw(text, font) <= max_w:
        return text
    while text and _tw(text + "…", font) > max_w:
        text = text[:-1]
    return (text + "…") if text else ""


# ---------------------------------------------------------------------------
# 渲染：卡片布局
# ---------------------------------------------------------------------------

TEXT_OPS = "text"
PILL_OPS = "pill"
LINE_OPS = "line"
DASH_OPS = "dash"


class _CardCanvas:
    """收集一张卡片的绘制指令，同时维护纵向游标。"""

    def __init__(self, width: int) -> None:
        self.width = width
        self.y = 0
        self.ops: list[tuple] = []
        self.back_ops: list[tuple] = []

    def text(self, x: int, y: int, value: str, font, fill, anchor: str = "la") -> None:
        self.ops.append((TEXT_OPS, x, y, value, font, fill, anchor))

    def pill(
        self,
        x: int,
        y: int,
        w: int,
        h: int,
        radius: int,
        fill,
        outline=None,
        width: int = 1,
    ) -> None:
        self.ops.append((PILL_OPS, x, y, w, h, radius, fill, outline, width))

    def dashed(self, x1: int, y: int, x2: int, color, width: int = 1) -> None:
        self._add(DASH_OPS, x1, y, x2, color, width)

    def vline(self, x: int, y1: int, y2: int, color, width: int = 2) -> None:
        self._add(LINE_OPS, x, y1, y2, color, width)

    def _add(self, *op) -> None:
        self.back_ops.append(op)


class _FontSet:
    def __init__(self) -> None:
        self.title = _font(58, bold=True)
        self.subtitle = _font(23)
        self.meta = _font(19)
        self.card_title = _font(30, bold=True)
        self.group = _font(24, bold=True)
        self.group_small = _font(21, bold=True)
        self.cmd = _font(23, bold=True)
        self.cmd_small = _font(21, bold=True)
        self.desc = _font(20)
        self.desc_small = _font(19)
        self.alias = _font(17)
        self.tiny = _font(16)


def _draw_permission_badge(
    canvas: _CardCanvas,
    entry: CommandEntry,
    row_y: int,
    row_h: int,
) -> int:
    """画权限角标，返回其宽度（没画则为 0）。"""

    badge = PERMISSION_BADGES.get(entry.permission)
    if not badge:
        return 0
    label, fill, outline, ink = badge
    font = _font(17, bold=True)
    width = _tw(label, font) + 18
    height = 24
    x = canvas.width - CARD_PAD - width
    y = row_y + max(0, (row_h - height) // 2)
    canvas.pill(x, y, width, height, 8, fill, outline, 1)
    canvas.text(x + width // 2, y + height // 2 - 1, label, font, ink, anchor="mm")
    return width


def _entry_rows(
    canvas: _CardCanvas,
    entry: CommandEntry,
    accent: str,
    opts: RenderOptions,
    fonts: _FontSet,
    indent: int = 0,
    depth: int = 0,
) -> None:
    """把一条指令（或指令组）追加到卡片画布。"""

    x0 = CARD_PAD + indent
    avail = canvas.width - x0 - CARD_PAD

    if entry.is_group:
        row_y = canvas.y
        title_text = "◆ " + entry.name
        canvas.text(x0, row_y + 2, title_text, fonts.group, accent)
        title_w = _tw(title_text, fonts.group)
        tag_x = x0 + title_w + 10
        if tag_x + 56 < canvas.width - CARD_PAD:
            canvas.pill(tag_x, row_y + 4, 52, 22, 7, _tint(accent, 0.86), _tint(accent, 0.55))
            canvas.text(tag_x + 26, row_y + 15, "指令组", fonts.tiny, accent, anchor="mm")
        canvas.y = row_y + 34

        if entry.description:
            for line in _wrap(entry.description, fonts.desc_small, avail, max_lines=2):
                canvas.text(x0, canvas.y, line, fonts.desc_small, MUTED)
                canvas.y += 26
            canvas.y += 2

        if entry.aliases and opts.show_aliases:
            alias_text = "别名 · " + "、".join(entry.aliases)
            canvas.text(
                x0,
                canvas.y,
                _ellipsize(alias_text, fonts.alias, avail),
                fonts.alias,
                MUTED_LIGHT,
            )
            canvas.y += 22

        children_start = canvas.y
        for child in entry.children:
            _entry_rows(
                canvas,
                child,
                accent,
                opts,
                fonts,
                indent=indent + 22,
                depth=depth + 1,
            )
            canvas.y += 10
        if canvas.y > children_start:
            canvas.vline(
                x0 + 8,
                children_start,
                canvas.y - 10,
                _tint(accent, 0.7),
                width=2,
            )
        canvas.y += 6
        return

    # ---- 普通指令 ----
    row_y = canvas.y
    name_text = opts.command_prefix + entry.name
    params = entry.params.strip()
    font = fonts.cmd if depth == 0 else fonts.cmd_small
    pad_x = 12
    name_w = _tw(name_text, font)
    params_w = (_tw(params, font) + 6) if params else 0

    badge_w = (
        (_tw(PERMISSION_BADGES[entry.permission][0], _font(17, bold=True)) + 18)
        if entry.permission in PERMISSION_BADGES
        else 0
    )
    max_pill_w = max(80, avail - (badge_w + 10 if badge_w else 0))
    pill_w = min(name_w + params_w + pad_x * 2, max_pill_w)
    pill_h = 36 if depth == 0 else 33
    pill_fill = _tint(accent, 0.88 if depth == 0 else 0.92)
    pill_line = _tint(accent, 0.52 if depth == 0 else 0.66)
    canvas.pill(x0, row_y, pill_w, pill_h, 9, pill_fill, pill_line)

    text_x = x0 + pad_x
    text_y = row_y + pill_h // 2
    if name_w <= pill_w - pad_x * 2:
        canvas.text(text_x, text_y, name_text, font, INK, anchor="lm")
        if params and name_w + params_w + pad_x * 2 <= pill_w:
            canvas.text(text_x + name_w + 6, text_y, params, font, MUTED, anchor="lm")
    else:
        canvas.text(
            text_x,
            text_y,
            _ellipsize(name_text, font, pill_w - pad_x * 2),
            font,
            INK,
            anchor="lm",
        )

    if badge_w:
        _draw_permission_badge(canvas, entry, row_y, pill_h)

    # 描述：能塞在同一行就塞，塞不下再换行；右侧要给权限角标留位置。
    line_h = 26
    badge_reserve = badge_w + 10 if badge_w else 0
    desc_x = x0 + pill_w + 12
    desc_avail = canvas.width - CARD_PAD - desc_x - badge_reserve
    desc_lines: list[str] = []
    desc_below = True
    if entry.description:
        if desc_avail >= 150:
            desc_lines = _wrap(entry.description, fonts.desc, desc_avail, max_lines=2)
            desc_below = False
        else:
            desc_lines = _wrap(entry.description, fonts.desc, avail, max_lines=2)

    row_h = pill_h
    if desc_lines:
        if desc_below:
            row_h = pill_h + 6 + len(desc_lines) * line_h
        else:
            row_h = max(pill_h, len(desc_lines) * line_h)

    if desc_lines:
        if desc_below:
            ty = row_y + pill_h + 8
            for line in desc_lines:
                canvas.text(x0, ty, line, fonts.desc, MUTED)
                ty += line_h
        else:
            ty = row_y + max(0, (row_h - len(desc_lines) * line_h) // 2) + 1
            for line in desc_lines:
                canvas.text(desc_x, ty, line, fonts.desc, MUTED)
                ty += line_h

    canvas.y = row_y + row_h

    if entry.aliases and opts.show_aliases:
        canvas.y += 2
        alias_text = "别名 · " + "、".join(entry.aliases)
        canvas.text(
            x0,
            canvas.y,
            _ellipsize(alias_text, fonts.alias, avail),
            fonts.alias,
            MUTED_LIGHT,
        )
        canvas.y += 22

    canvas.y += 10


def _build_section_ops(
    canvas: _CardCanvas,
    section: PluginSection,
    accent: str,
    opts: RenderOptions,
    fonts: _FontSet,
) -> None:
    """生成一张插件卡片的全部绘制指令。

    卡片保留插件名称大标题与简介；不显示徽标、版本、作者和「内置」标记。
    """

    y = CARD_PAD + 4

    # 插件名称
    canvas.text(
        CARD_PAD,
        y,
        _ellipsize(section.display_name, fonts.card_title, canvas.width - CARD_PAD * 2),
        fonts.card_title,
        INK,
    )
    y += 42

    # 插件简介
    if section.description:
        for line in _wrap(section.description, fonts.desc, canvas.width - CARD_PAD * 2, max_lines=2):
            canvas.text(CARD_PAD, y, line, fonts.desc, MUTED)
            y += 26
        y += 4

    # 分隔虚线
    y += 2
    canvas.dashed(CARD_PAD, y, canvas.width - CARD_PAD, LINE)
    y += 16

    canvas.y = y
    for entry in section.entries:
        _entry_rows(canvas, entry, accent, opts, fonts)
        canvas.y += 2

    canvas.y += CARD_PAD - 8


def _replay_ops(draw, x0: int, y0: int, ops: list[tuple]) -> None:
    for op in ops:
        kind = op[0]
        if kind == LINE_OPS:
            _, x, y1, y2, color, width = op
            draw.line([(x0 + x, y0 + y1), (x0 + x, y0 + y2)], fill=color, width=width)
        elif kind == DASH_OPS:
            _, x1, y, x2, color, width = op
            step = 8
            for sx in range(x1, x2, step):
                draw.line(
                    [(x0 + sx, y0 + y), (x0 + min(sx + 4, x2), y0 + y)],
                    fill=color,
                    width=width,
                )
        elif kind == PILL_OPS:
            _, x, y, w, h, radius, fill, outline, width = op
            draw.rounded_rectangle(
                [x0 + x, y0 + y, x0 + x + w, y0 + y + h],
                radius=radius,
                fill=fill,
                outline=outline,
                width=width if outline else 0,
            )
        elif kind == TEXT_OPS:
            _, x, y, value, font, fill, anchor = op
            draw.text((x0 + x, y0 + y), value, font=font, fill=fill, anchor=anchor)


def _draw_card_shell(draw, x: int, y: int, w: int, h: int, accent: str) -> None:
    draw.rounded_rectangle(
        [x + 1, y + 3, x + w + 2, y + h + 4],
        radius=CARD_RADIUS,
        fill=(120, 140, 165, 70),
    )
    draw.rounded_rectangle(
        [x, y, x + w, y + h], radius=CARD_RADIUS, fill=accent
    )
    draw.rounded_rectangle(
        [x + 2, y + CARD_PAD - 8, x + w - 2, y + h - 2],
        radius=CARD_RADIUS - 3,
        fill=(255, 255, 255, CARD_ALPHA),
    )


def _background_source() -> Path | None:
    """返回插件目录里的底图路径；不存在时返回 None。"""

    path = Path(__file__).resolve().parent / BACKGROUND_IMAGE_NAME
    return path if path.is_file() else None


def _make_background(width: int, height: int) -> Image.Image:
    """按原画布尺寸铺满底图（等比缩放后居中裁剪）。"""

    canvas = Image.new("RGB", (width, height), BG)
    path = _background_source()
    if path is None:
        return canvas
    try:
        with Image.open(path) as source:
            source = source.convert("RGB")
            scale = max(width / source.width, height / source.height)
            new_w = max(width, int(round(source.width * scale)))
            new_h = max(height, int(round(source.height * scale)))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            resized = source.resize((new_w, new_h), resample)
            left = (new_w - width) // 2
            top = int((new_h - height) * BACKGROUND_ANCHOR_Y)
            return resized.crop((left, top, left + width, top + height))
    except Exception as exc:  # pragma: no cover - 图片损坏等异常
        logger.warning(f"{LOG_PREFIX} 底图加载失败，改用纯色背景：{exc}")
        return canvas


def _draw_header(draw, opts: RenderOptions, fonts: _FontSet, on_image: bool = False) -> None:
    draw.text(
        (MARGIN_X, 34),
        opts.title,
        font=fonts.title,
        fill="#ffffff" if on_image else INK,
    )


def _draw_legend_background(draw) -> None:
    x1 = MARGIN_X
    x2 = CANVAS_W - MARGIN_X
    draw.rounded_rectangle(
        [x1, LEGEND_Y, x2, LEGEND_Y + LEGEND_H],
        radius=14,
        fill=(255, 255, 255, CARD_ALPHA),
        outline="#dbe6f2",
        width=1,
    )


def _draw_legend_text(draw, opts: RenderOptions, fonts: _FontSet) -> None:
    x1 = MARGIN_X
    x2 = CANVAS_W - MARGIN_X
    prefix = opts.command_prefix or "/"
    legend = (
        f"「{prefix}」为当前唤醒前缀 · 深色胶囊就是可以直接发送的指令 · "
        "< > 为必填参数 · [ ] 为可选参数 · 灰色小字为说明"
    )
    draw.text(
        (x1 + 22, LEGEND_Y + LEGEND_H // 2),
        _ellipsize(legend, fonts.meta, x2 - x1 - 44),
        font=fonts.meta,
        fill=INK_SOFT,
        anchor="lm",
    )


def _footer_text(opts: RenderOptions) -> str:
    return f"共 {opts.plugin_count} 个插件 · {opts.command_count} 条指令"


_FOOTER_MAX_W = CANVAS_W - MARGIN_X * 2 - 44


def _draw_footer_text(
    draw, opts: RenderOptions, fonts: _FontSet, total_h: int
) -> None:
    draw.text(
        (CANVAS_W // 2, total_h - MARGIN_X - 10),
        _ellipsize(_footer_text(opts), fonts.desc, _FOOTER_MAX_W),
        font=fonts.desc,
        fill=MUTED,
        anchor="mm",
    )


def render_command_image(
    sections: list[PluginSection],
    options: RenderOptions,
    output_path: str | Path | None = None,
) -> Path:
    """把采集结果渲染成一张 PNG，返回文件路径。"""

    if not PIL_AVAILABLE:  # pragma: no cover
        raise RuntimeError("Pillow 未安装，无法渲染图片")
    if not sections:
        raise ValueError("没有可展示的指令")

    fonts = _FontSet()
    columns = max(1, min(3, int(options.columns or 2)))
    col_w = (CANVAS_W - MARGIN_X * 2 - COL_GAP * (columns - 1)) // columns

    # 第一遍：算出每张卡片的绘制指令与高度。
    cards: list[tuple[PluginSection, str, _CardCanvas, int]] = []
    for index, section in enumerate(sections):
        accent = ACCENTS[index % len(ACCENTS)]
        canvas = _CardCanvas(col_w)
        _build_section_ops(canvas, section, accent, options, fonts)
        cards.append((section, accent, canvas, canvas.y))

    # 第二遍：按卡片高度从大到小分列（LPT 装箱），再把每列按原始顺序摆放，
    # 这样既不会留下巨大空白，阅读顺序也不会被打乱。
    column_of = [0] * len(cards)
    heights = [0] * columns
    for index in sorted(range(len(cards)), key=lambda i: cards[i][3], reverse=True):
        target = min(range(columns), key=lambda i: heights[i])
        column_of[index] = target
        heights[target] += cards[index][3] + CARD_GAP_Y

    placements: list[list[tuple[PluginSection, str, _CardCanvas, int]]] = [
        [] for _ in range(columns)
    ]
    for index, card in enumerate(cards):
        placements[column_of[index]].append(card)

    content_h = max(heights) if heights else 0
    total_h = CARDS_TOP + content_h + FOOTER_H + MARGIN_X

    on_image = _background_source() is not None

    # 先在半透明图层上画卡片 / 图例，再与底图合成，
    # 这样背景只透出 20%，文字和指令胶囊仍然是清晰的。
    base = _make_background(CANVAS_W, total_h).convert("RGBA")
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    _draw_legend_background(overlay_draw)
    for col_index in range(columns):
        x = MARGIN_X + col_index * (col_w + COL_GAP)
        y = CARDS_TOP
        for _section, accent, canvas, card_h in placements[col_index]:
            _draw_card_shell(overlay_draw, x, y, col_w, card_h, accent)
            y += card_h + CARD_GAP_Y

    img = Image.alpha_composite(base, overlay).convert("RGB")
    draw = ImageDraw.Draw(img)
    _draw_header(draw, options, fonts, on_image=on_image)
    _draw_legend_text(draw, options, fonts)

    for col_index in range(columns):
        x = MARGIN_X + col_index * (col_w + COL_GAP)
        y = CARDS_TOP
        for _section, accent, canvas, card_h in placements[col_index]:
            _replay_ops(draw, x, y, canvas.back_ops)
            _replay_ops(draw, x, y, canvas.ops)
            y += card_h + CARD_GAP_Y

    _draw_footer_text(draw, options, fonts, total_h)

    if output_path is None:
        output_path = _temp_image_path()
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)
    return path


def _temp_image_path() -> Path:
    """图片输出路径：优先 AstrBot 的 data/temp 目录。"""

    base: Path
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

        base = Path(get_astrbot_temp_path())
    except Exception:
        import tempfile

        base = Path(tempfile.gettempdir()) / "astrbot_atri_command_help"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{TEMP_FILE_PREFIX}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"


def cleanup_old_images(
    retention_seconds: int = IMAGE_RETENTION_SECONDS,
    keep: set[str] | None = None,
) -> int:
    """清掉过期的旧图，返回删除数量。

    ``keep`` 里的文件是当前缓存正在引用的图片，即使超过保留时间也不会删除。
    """

    try:
        base = _temp_image_path().parent
    except Exception:
        return 0
    keep = keep or set()
    removed = 0
    now = time.time()
    try:
        for path in base.glob(f"{TEMP_FILE_PREFIX}_*.png"):
            if str(path) in keep:
                continue
            try:
                if now - path.stat().st_mtime > retention_seconds:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError:
        return removed
    return removed


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------


def event_is_admin(event: AstrMessageEvent) -> bool:
    """判断消息发送者是否为 AstrBot 管理员。

    优先用官方 ``event.is_admin()``；旧版本或测试用例里没有该方法时，
    回退读取 ``event.role``。
    """

    checker = getattr(event, "is_admin", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            pass
    return str(getattr(event, "role", "") or "").strip().lower() == "admin"


class CommandHelpPlugin(Star):
    """只提供一个指令：把所有插件的指令汇总成一张图。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context)
        self._config = config
        # 按「实际渲染内容」的指纹缓存图片；内容不变时复用上一张图。
        self._cache: dict[str, str] = {}
        logger.info(f"{LOG_PREFIX} 插件 v{PLUGIN_VERSION} 已加载")

    # -- 配置 ------------------------------------------------------------

    def _opt(self, key: str) -> Any:
        default = DEFAULT_OPTIONS.get(key)
        if self._config is None:
            return default
        try:
            value = self._config.get(key, default)
        except Exception:
            return default
        return default if value is None else value

    def _int_opt(self, key: str, minimum: int, maximum: int) -> int:
        try:
            value = int(self._opt(key))
        except (TypeError, ValueError):
            value = int(DEFAULT_OPTIONS[key])  # type: ignore[arg-type]
        return max(minimum, min(maximum, value))

    def _bool_opt(self, key: str) -> bool:
        value = self._opt(key)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on", "开", "开启"}
        return bool(DEFAULT_OPTIONS.get(key))

    # -- 环境信息 ---------------------------------------------------------

    def _command_prefix(self, event: AstrMessageEvent) -> str:
        prefixes: Any = None
        try:
            cfg = self.context.get_config(
                getattr(event, "unified_msg_origin", None)
            )
        except Exception:
            cfg = None
        if cfg is not None:
            try:
                prefixes = cfg.get("wake_prefix")
            except Exception:
                prefixes = None
        if isinstance(prefixes, str) and prefixes.strip():
            return prefixes.strip()[0]
        if isinstance(prefixes, (list, tuple)):
            for prefix in prefixes:
                if isinstance(prefix, str) and prefix.strip():
                    return prefix.strip()[0]
        return "/"

    # -- 指令 -------------------------------------------------------------

    @filter.command(*COMMAND_NAMES, alias=list(COMMAND_ALIASES))
    async def cmd_command_help(self, event: AstrMessageEvent):
        """汇总所有插件的指令并渲染成一张图片。"""
        try:
            is_admin = event_is_admin(event)
            include_builtin = self._bool_opt("include_builtin")
            # 非管理员永远看不到仅管理员 / 群管可用的指令；
            # 管理员看到的完整度仍由 include_admin_only 控制。
            include_admin = self._bool_opt("include_admin_only") and is_admin
            columns = self._int_opt("columns", 1, 3)
            show_aliases = self._bool_opt("show_aliases")
            title = str(self._opt("title") or DEFAULT_OPTIONS["title"])
            exclude_rules = parse_exclude_rules(self._opt("exclude_rules"))
            prefix = self._command_prefix(event)

            sections = await collect_command_sections(
                include_builtin=include_builtin,
                include_admin_only=include_admin,
                exclude_rules=exclude_rules,
            )
            if not sections:
                yield event.plain_result(
                    "暂时没有可展示的指令（已排除「ATRI指令大全」自身，"
                    "并按当前身份、内置指令开关与隐藏规则过滤）。"
                )
                return

            if not PIL_AVAILABLE:
                logger.warning(f"{LOG_PREFIX} Pillow 不可用，降级为文本输出")
                text = sections_to_text(sections, prefix)
                yield event.plain_result(text[:1800])
                return

            options = RenderOptions(
                title=title,
                columns=columns,
                show_aliases=show_aliases,
                command_prefix=prefix,
                plugin_count=len(sections),
                command_count=sum(section.command_count for section in sections),
            )

            # 缓存按「实际要渲染的内容」判断：插件、指令、标题、栏数、
            # 别名开关、唤醒前缀等都没有变化时，直接复用上一张图。
            cache_key = render_fingerprint(sections, options)
            cached_path = self._cache.get(cache_key)
            if cached_path and Path(cached_path).is_file():
                # 命中后移到队尾，便于按最近使用顺序淘汰。
                self._cache.pop(cache_key)
                self._cache[cache_key] = cached_path
                yield event.image_result(cached_path)
                return

            image_path = await asyncio.to_thread(
                render_command_image, sections, options
            )

            self._cache[cache_key] = str(image_path)
            while len(self._cache) > 8:
                old_key, old_path = next(iter(self._cache.items()))
                self._cache.pop(old_key, None)
                try:
                    Path(old_path).unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                cleanup_old_images(keep=set(self._cache.values()))
            except Exception:
                pass

            yield event.image_result(str(image_path))
        except Exception as exc:  # pragma: no cover - 兜底防炸
            logger.exception(f"{LOG_PREFIX} 生成 ATRI 指令大全图片失败：{exc}")
            yield event.plain_result(f"生成 ATRI 指令大全图片失败：{exc}")

    async def terminate(self) -> None:
        """插件卸载时清理缓存文件。"""

        for path in self._cache.values():
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
        self._cache.clear()
        logger.info(f"{LOG_PREFIX} 插件已卸载")
