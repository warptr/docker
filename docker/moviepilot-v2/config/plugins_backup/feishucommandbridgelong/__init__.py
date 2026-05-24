import asyncio
import concurrent.futures
import copy
import difflib
import fcntl
import importlib
import json
import re
import sys
import threading
import time
import traceback
from base64 import b64decode
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen, Request as UrlRequest

from fastapi import Request
from app.core.config import settings
from app.core.event import eventmanager
from app.core.metainfo import MetaInfo
from app.core.plugin import PluginManager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search import SearchChain
from app.chain.subscribe import SubscribeChain
from app.scheduler import Scheduler
from app.utils.string import StringUtils
from app.utils.http import RequestUtils

for _plugin_dir in (
    str(Path(__file__).resolve().parent),
    "/config/plugins/FeishuCommandBridgeLong",
):
    if Path(_plugin_dir).exists() and _plugin_dir not in sys.path:
        sys.path.insert(0, _plugin_dir)

for _site_path in (
    "/usr/local/lib/python3.12/site-packages",
    "/usr/local/lib/python3.11/site-packages",
):
    if Path(_site_path).exists() and _site_path not in sys.path:
        sys.path.append(_site_path)

try:
    import lark_oapi as lark
except Exception:
    lark = None


class _LongConnectionRuntime:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._fingerprint = ""
        self._plugin: Optional["FeishuCommandBridgeLong"] = None

    def start(self, plugin: "FeishuCommandBridgeLong") -> None:
        global lark
        if lark is None:
            try:
                import lark_oapi as runtime_lark
                lark = runtime_lark
            except Exception as exc:
                logger.error(
                    f"[FeishuCommandBridgeLong] 缺少依赖 lark-oapi，请先安装插件依赖：{exc}"
                )
                return

        if not plugin._enabled or not plugin._app_id or not plugin._app_secret:
            return

        fingerprint = plugin._connection_fingerprint()
        with self._lock:
            self._plugin = plugin
            if self._thread and self._thread.is_alive():
                if fingerprint != self._fingerprint:
                    logger.warning(
                        "[FeishuCommandBridgeLong] 长连接已在运行，App ID / App Secret / Token 变更需要重启 MoviePilot 后生效"
                    )
                return

            self._fingerprint = fingerprint
            self._thread = threading.Thread(
                target=self._run,
                name="feishu-command-bridge-long",
                daemon=True,
            )
            self._thread.start()

    def _run(self) -> None:
        plugin = self._plugin
        if plugin is None:
            return

        def _on_message(data) -> None:
            current_plugin = self._plugin
            if current_plugin is None:
                return
            current_plugin._handle_long_connection_event(data)

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            import lark_oapi.ws.client as lark_ws_client
            lark_ws_client.loop = loop

            event_handler = (
                lark.EventDispatcherHandler.builder("", "")
                .register_p2_im_message_receive_v1(_on_message)
                .build()
            )
            ws_client = lark.ws.Client(
                plugin._app_id,
                plugin._app_secret,
                log_level=lark.LogLevel.DEBUG if plugin._debug else lark.LogLevel.INFO,
                event_handler=event_handler,
            )
            logger.info("[FeishuCommandBridgeLong] 正在启动飞书长连接")
            ws_client.start()
        except Exception as exc:
            logger.error(f"[FeishuCommandBridgeLong] 长连接退出：{exc}\n{traceback.format_exc()}")

    def is_running(self) -> bool:
        with self._lock:
            return bool(self._thread and self._thread.is_alive())


_runtime = _LongConnectionRuntime()
_EVENT_CACHE_FILE = Path("/config/plugins/FeishuCommandBridgeLong/.event_cache.json")
_SMART_CACHE_FILE = Path("/config/plugins/FeishuCommandBridgeLong/.smart_cache.json")


class FeishuCommandBridgeLong(_PluginBase):
    plugin_name = "飞书命令桥接"
    plugin_desc = "旧飞书长连接兼容/备份入口；新用户建议优先使用 Agent影视助手 内置飞书入口。"
    plugin_icon = "https://raw.githubusercontent.com/liuyuexi1987/MoviePilot-Plugins/main/icons/feishucommandbridgelong.png"
    plugin_version = "0.5.26"
    plugin_author = "liuyuexi1987"
    plugin_level = 1
    author_url = "https://github.com/liuyuexi1987"
    plugin_config_prefix = "feishucommandbridgelong_"
    plugin_order = 29
    auth_level = 1

    _enabled = False
    _allow_all = False
    _verification_token = ""
    _app_id = ""
    _app_secret = ""
    _allowed_chat_ids: List[str] = []
    _allowed_user_ids: List[str] = []
    _reply_enabled = True
    _reply_receive_id_type = "chat_id"
    _command_whitelist: List[str] = []
    _command_aliases = ""
    _debug = False
    _tmdb_api_key_override = ""
    _execution_backend = "legacy"

    _token_cache: Dict[str, Any] = {}
    _token_lock = threading.Lock()
    _event_cache: Dict[str, float] = {}
    _event_lock = threading.Lock()
    _search_cache: Dict[str, Dict[str, Any]] = {}
    _search_cache_lock = threading.Lock()
    _smart_cache: Dict[str, Dict[str, Any]] = {}
    _smart_cache_lock = threading.Lock()
    _candidate_actor_cache: Dict[str, List[str]] = {}
    _candidate_actor_cache_lock = threading.Lock()
    _tmdb_api_key_cache = ""
    _tmdb_api_key_lock = threading.Lock()

    @classmethod
    def _default_command_whitelist(cls) -> List[str]:
        return [
            "/p115_manual_transfer",
            "/p115_inc_sync",
            "/p115_full_sync",
            "/p115_strm",
            "/quark_save",
            "/pansou_search",
            "/smart_entry",
            "/smart_pick",
            "/media_search",
            "/media_download",
            "/media_subscribe",
            "/media_subscribe_search",
            "/version",
        ]

    @classmethod
    def _default_command_aliases(cls) -> str:
        return (
            "刮削=/p115_manual_transfer\n"
            "搜索=/media_search\n"
            "MP搜索=/media_search\n"
            "原生搜索=/media_search\n"
            "盘搜搜索=/pansou_search\n"
            "盘搜=/pansou_search\n"
            "ps=/pansou_search\n"
            "1=/pansou_search\n"
            "影巢搜索=/smart_entry\n"
            "yc=/smart_entry\n"
            "2=/smart_entry\n"
            "下载=/media_download\n"
            "订阅=/media_subscribe\n"
            "订阅搜索=/media_subscribe_search\n"
            "生成STRM=/p115_inc_sync\n"
            "全量STRM=/p115_full_sync\n"
            "指定路径STRM=/p115_strm\n"
            "夸克转存=/quark_save\n"
            "夸克=/quark_save\n"
            "链接=/smart_entry\n"
            "处理=/smart_entry\n"
            "115登录=/smart_entry\n"
            "115扫码=/smart_entry\n"
            "检查115登录=/smart_entry\n"
            "115登录状态=/smart_entry\n"
            "115状态=/smart_entry\n"
            "115帮助=/smart_entry\n"
            "115任务=/smart_entry\n"
            "继续115任务=/smart_entry\n"
            "取消115任务=/smart_entry\n"
            "选择=/smart_pick\n"
            "详情=/smart_pick\n"
            "审查=/smart_pick\n"
            "选=/smart_pick\n"
            "继续=/smart_pick\n"
            "影巢=/smart_entry\n"
            "搜索资源=/media_search\n"
            "下载资源=/media_download\n"
            "订阅媒体=/media_subscribe\n"
            "订阅并搜索=/media_subscribe_search\n"
            "版本=/version"
        )

    @staticmethod
    def _clean_input(value: Any) -> str:
        if value is None:
            return ""
        text = str(value)
        for ch in ("\ufeff", "\u200b", "\u200c", "\u200d", "\u2060", "\ufffc"):
            text = text.replace(ch, "")
        return text.strip()

    @classmethod
    def _normalize_execution_backend(cls, value: Any) -> str:
        clean = cls._clean_input(value).lower()
        if clean in {"auto", "agent_resource_officer", "legacy"}:
            return clean
        if clean in {"agent", "aro", "agentresourceofficer"}:
            return "agent_resource_officer"
        return "legacy"

    @classmethod
    def _describe_execution_backend(cls, value: Any) -> str:
        backend = cls._normalize_execution_backend(value)
        mapping = {
            "legacy": "旧桥接直连",
            "auto": "自动优先新主线",
            "agent_resource_officer": "仅走 Agent影视助手",
        }
        return mapping.get(backend, "旧桥接直连")

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._allow_all = bool(config.get("allow_all"))
        self._verification_token = self._clean_input(config.get("verification_token"))
        self._app_id = self._clean_input(config.get("app_id"))
        self._app_secret = self._clean_input(config.get("app_secret"))
        self._allowed_chat_ids = self._split_lines(config.get("allowed_chat_ids"))
        self._allowed_user_ids = self._split_lines(config.get("allowed_user_ids"))
        self._reply_enabled = bool(config.get("reply_enabled", True))
        self._reply_receive_id_type = str(
            config.get("reply_receive_id_type") or "chat_id"
        ).strip()
        self._command_whitelist = self._merge_command_whitelist(
            self._split_commands(config.get("command_whitelist"))
        )
        self._command_aliases = self._merge_command_aliases(
            str(config.get("command_aliases") or "").strip()
        )
        self._debug = bool(config.get("debug"))
        self._tmdb_api_key_override = self._clean_input(config.get("tmdb_api_key"))
        self._execution_backend = self._normalize_execution_backend(
            config.get("execution_backend")
        )
        type(self)._tmdb_api_key_override = self._tmdb_api_key_override
        with type(self)._tmdb_api_key_lock:
            type(self)._tmdb_api_key_cache = ""

        _runtime.start(self)

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/health",
                "endpoint": self.health,
                "methods": ["GET"],
                "summary": "健康检查",
                "description": "返回飞书长连接插件当前状态与基础配置",
                "auth": "bear",
            },
            {
                "path": "/assistant/route",
                "endpoint": self.api_assistant_route,
                "methods": ["POST"],
                "summary": "智能单入口分流",
                "description": "自动识别夸克链接、115 链接或影巢片名搜索",
                "auth": "bear",
            },
            {
                "path": "/assistant/pick",
                "endpoint": self.api_assistant_pick,
                "methods": ["POST"],
                "summary": "按编号继续执行",
                "description": "对上一轮智能分流结果按编号确认执行",
                "auth": "bear",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "enabled",
                                            "label": "启用插件",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "allow_all",
                                            "label": "允许所有飞书会话",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "verification_token",
                                            "label": "Verification Token",
                                            "placeholder": "飞书事件订阅 Token",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "tmdb_api_key",
                                            "label": "TMDB API Key（可选）",
                                            "placeholder": "仅用于影巢候选影片补充主演",
                                            "type": "password",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_id",
                                            "label": "App ID",
                                            "placeholder": "cli_xxxxxxxxx",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "app_secret",
                                            "label": "App Secret",
                                            "placeholder": "飞书应用凭证",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "allowed_chat_ids",
                                            "label": "允许的群聊 Chat ID",
                                            "rows": 4,
                                            "placeholder": "一个一行；留空时仅允许 allow_all 或允许的用户",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "allowed_user_ids",
                                            "label": "允许的用户 Open ID",
                                            "rows": 4,
                                            "placeholder": "一个一行",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "command_whitelist",
                                            "label": "命令白名单",
                                            "placeholder": ",".join(self._default_command_whitelist()),
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "reply_enabled",
                                            "label": "发送即时回执",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "command_aliases",
                                            "label": "命令别名",
                                            "rows": 6,
                                            "placeholder": self._default_command_aliases(),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "execution_backend",
                                            "label": "执行后端",
                                            "items": [
                                                {"title": "旧桥接直连（推荐保留旧体验）", "value": "legacy"},
                                                {"title": "自动优先新主线，失败回落旧桥接", "value": "auto"},
                                                {"title": "仅走 Agent影视助手 新主线", "value": "agent_resource_officer"},
                                            ],
                                        },
                                    },
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {
                                            "model": "debug",
                                            "label": "输出调试日志",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], {
            "enabled": self._enabled,
            "allow_all": self._allow_all,
            "verification_token": self._verification_token,
            "app_id": self._app_id,
            "app_secret": self._app_secret,
            "allowed_chat_ids": "\n".join(self._allowed_chat_ids),
            "allowed_user_ids": "\n".join(self._allowed_user_ids),
            "reply_enabled": self._reply_enabled,
            "reply_receive_id_type": self._reply_receive_id_type,
            "command_whitelist": ",".join(self._command_whitelist) if self._command_whitelist else ",".join(self._default_command_whitelist()),
            "command_aliases": self._command_aliases or self._default_command_aliases(),
            "debug": self._debug,
            "tmdb_api_key": self._tmdb_api_key_override,
            "execution_backend": self._execution_backend or "legacy",
        }

    def get_page(self) -> Optional[List[dict]]:
        aliases = self._parse_aliases()
        alias_lines = [
            {
                "component": "div",
                "props": {"class": "text-body-2 py-1"},
                "text": f"{key} -> {value}",
            }
            for key, value in aliases.items()
        ] or [
            {
                "component": "div",
                "props": {"class": "text-body-2 py-1"},
                "text": "未配置别名",
            }
        ]

        command_lines = [
            {
                "component": "div",
                "props": {"class": "text-body-2 py-1"},
                "text": cmd,
            }
            for cmd in (self._command_whitelist or [])
        ] or [
            {
                "component": "div",
                "props": {"class": "text-body-2 py-1"},
                "text": "未配置命令白名单",
            }
        ]

        return [
            {
                "component": "VContainer",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCard",
                                        "props": {"border": True, "flat": True},
                                        "content": [
                                            {
                                                "component": "VCardTitle",
                                                "text": "运行状态",
                                            },
                                            {
                                                "component": "VCardText",
                                                "content": [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": f"启用状态：{'是' if self._enabled else '否'}",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": f"长连接运行中：{'是' if _runtime.is_running() else '否'}",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": f"执行后端：{self._describe_execution_backend(self._execution_backend)}",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": f"允许所有会话：{'是' if self._allow_all else '否'}",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": f"App ID：{self._app_id or '未填写'}",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": f"Token：{self._mask_secret(self._verification_token) or '未填写'}",
                                                    },
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCard",
                                        "props": {"border": True, "flat": True},
                                        "content": [
                                            {
                                                "component": "VCardTitle",
                                                "text": "可用命令",
                                            },
                                            {
                                                "component": "VCardText",
                                                "content": command_lines,
                                            },
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCard",
                                        "props": {"border": True, "flat": True},
                                        "content": [
                                            {
                                                "component": "VCardTitle",
                                                "text": "命令别名",
                                            },
                                            {
                                                "component": "VCardText",
                                                "content": alias_lines,
                                            },
                                        ],
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VCard",
                                        "props": {"border": True, "flat": True},
                                        "content": [
                                            {
                                                "component": "VCardTitle",
                                                "text": "使用示例",
                                            },
                                            {
                                                "component": "VCardText",
                                                "content": [
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "处理 流浪地球2",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "选择 1",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "版本",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "刮削 /待整理/",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "/p115_strm /待整理/",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "MP搜索 流浪地球2",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "影巢搜索 流浪地球2",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "盘搜搜索 流浪地球2",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "115登录",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "115帮助",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "检查115登录",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "115任务",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "继续115任务",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "取消115任务",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "链接 https://115cdn.com/s/xxxx path=/待整理",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "下载资源 1",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "订阅媒体 流浪地球2",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "订阅并搜索 流浪地球2",
                                                    },
                                                    {
                                                        "component": "div",
                                                        "props": {"class": "text-body-2 py-1"},
                                                        "text": "帮助",
                                                    },
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ]

    def health(self):
        return {
            "plugin_version": self.plugin_version,
            "enabled": self._enabled,
            "running": _runtime.is_running(),
            "allow_all": self._allow_all,
            "reply_enabled": self._reply_enabled,
            "allowed_chat_count": len(self._allowed_chat_ids),
            "allowed_user_count": len(self._allowed_user_ids),
            "command_whitelist": self._command_whitelist,
            "sdk_available": lark is not None,
        }

    async def api_assistant_route(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        session = self._clean_input(
            body.get("session")
            or body.get("chat_id")
            or body.get("user_id")
            or body.get("conversation_id")
            or "default"
        )
        text = self._clean_input(
            body.get("text")
            or body.get("query")
            or body.get("message")
            or ""
        )
        mode, query = self._strip_search_prefix(text)
        cache_key = f"api::{session}"
        if mode == "mp":
            message = await asyncio.to_thread(self._execute_media_search, query, cache_key)
            ok = "失败" not in message and "未识别" not in message
            data = {"action": "media_search", "ok": ok, "keyword": query}
        elif mode == "pansou":
            message = await asyncio.to_thread(self._execute_pansou_search, query, cache_key)
            ok = not message.startswith("盘搜搜索失败")
            data = {"action": "pansou_search", "ok": ok, "keyword": query}
        elif mode == "hdhive":
            ok, message, data = await asyncio.to_thread(
                self._execute_smart_entry,
                query,
                cache_key,
            )
        else:
            ok, message, data = await asyncio.to_thread(
                self._execute_smart_entry,
                text,
                cache_key,
            )
        return {"success": ok, "message": message, "data": data}

    async def api_assistant_pick(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        session = self._clean_input(
            body.get("session")
            or body.get("chat_id")
            or body.get("user_id")
            or body.get("conversation_id")
            or "default"
        )
        if body.get("arg"):
            arg = self._clean_input(body.get("arg"))
        else:
            index = str(body.get("index") or "").strip()
            path = self._normalize_pan_path(body.get("path") or "")
            arg = index
            if path:
                arg = f"{arg} path={path}".strip()
        ok, message, data = await asyncio.to_thread(
            self._execute_smart_pick,
            arg,
            f"api::{session}",
        )
        return {"success": ok, "message": message, "data": data}

    def stop_service(self):
        logger.info("[FeishuCommandBridge] 当前版本未实现长连接主动停止；如需彻底停掉，请重启 MoviePilot")

    def _connection_fingerprint(self) -> str:
        return "|".join([
            self._app_id,
            self._app_secret,
            self._verification_token,
        ])

    def _handle_long_connection_event(self, data) -> None:
        if not self._enabled:
            return

        event_context = data
        event = getattr(event_context, "event", None)
        header = getattr(event_context, "header", None)
        message = getattr(event, "message", None)
        sender = getattr(event, "sender", None)
        sender_id = getattr(sender, "sender_id", None)

        event_id = str(getattr(header, "event_id", "") or "").strip()
        if event_id and self._is_duplicate_event(event_id):
            return

        if self._debug:
            logger.info(
                f"[FeishuCommandBridge] event_id={event_id} "
                f"event_type={getattr(header, 'event_type', '')} "
                f"chat_id={getattr(message, 'chat_id', '')}"
            )

        if not message or str(getattr(message, "message_type", "")).strip() != "text":
            return

        raw_text = self._extract_text(getattr(message, "content", None))
        if not raw_text:
            return

        sender_open_id = str(getattr(sender_id, "open_id", "") or "").strip()
        chat_id = str(getattr(message, "chat_id", "") or "").strip()

        if not self._is_allowed(chat_id=chat_id, user_open_id=sender_open_id):
            self._reply_if_needed(
                receive_chat_id=chat_id,
                receive_open_id=sender_open_id,
                text="该会话未在白名单中，命令已拒绝。",
            )
            return

        if self._is_help_request(raw_text):
            self._reply_if_needed(
                receive_chat_id=chat_id,
                receive_open_id=sender_open_id,
                text=self._build_help_text(),
            )
            return

        if self._is_menu_request(raw_text):
            self._reply_if_needed(
                receive_chat_id=chat_id,
                receive_open_id=sender_open_id,
                text=self._build_menu_text(),
            )
            return

        command_text = self._map_text_to_command(raw_text)
        if not command_text:
            return

        cmd = command_text.split()[0]
        if cmd not in self._command_whitelist:
            self._reply_if_needed(
                receive_chat_id=chat_id,
                receive_open_id=sender_open_id,
                text=f"命令 {cmd} 不在白名单中。\n\n{self._build_help_text()}",
            )
            return

        if self._handle_builtin_command(
            command_text=command_text,
            receive_chat_id=chat_id,
            receive_open_id=sender_open_id,
        ):
            return

        logger.info(f"[FeishuCommandBridge] 转发命令：{command_text}")
        eventmanager.send_event(
            EventType.CommandExcute,
            {
                "cmd": command_text,
                "source": None,
                "user": sender_open_id or chat_id or "feishu",
            },
        )
        self._reply_if_needed(
            receive_chat_id=chat_id,
            receive_open_id=sender_open_id,
            text=f"已接收命令：{command_text}\n任务已提交给 MoviePilot。",
        )

    def _handle_builtin_command(
        self,
        command_text: str,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> bool:
        parts = command_text.split(maxsplit=1)
        cmd = parts[0].strip()
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "/p115_strm" and not arg:
            command_text = "/p115_full_sync"
            logger.info(f"[FeishuCommandBridge] 转发命令：{command_text}")
            eventmanager.send_event(
                EventType.CommandExcute,
                {
                    "cmd": command_text,
                    "source": None,
                    "user": receive_open_id or receive_chat_id or "feishu",
                },
            )
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"已接收命令：{command_text}\n任务已提交给 MoviePilot。",
            )
            return True

        if cmd == "/media_search":
            if not arg:
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text="用法：搜索资源 片名\n示例：MP搜索 流浪地球2",
                )
                return True
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"正在使用 MP 原生搜索：{arg}\n我会返回前 10 条结果，之后可直接回复：下载资源 序号",
            )
            threading.Thread(
                target=self._run_media_search,
                args=(arg, receive_chat_id, receive_open_id),
                name="feishu-media-search",
                daemon=True,
            ).start()
            return True

        if cmd == "/pansou_search":
            if not arg:
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text="用法：盘搜搜索 片名\n示例：盘搜搜索 流浪地球2",
                )
                return True
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"正在使用盘搜搜索：{arg}",
            )
            threading.Thread(
                target=self._run_pansou_search,
                args=(arg, receive_chat_id, receive_open_id),
                name="feishu-pansou-search",
                daemon=True,
            ).start()
            return True

        if cmd == "/media_download":
            if not arg or not arg.isdigit():
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text="用法：下载资源 序号\n示例：下载资源 1",
                )
                return True
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"正在提交第 {arg} 条资源到下载器，请稍候。",
            )
            threading.Thread(
                target=self._run_media_download,
                args=(int(arg), receive_chat_id, receive_open_id),
                name="feishu-media-download",
                daemon=True,
            ).start()
            return True

        if cmd == "/quark_save":
            if not arg:
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text=(
                        "用法：夸克转存 分享链接 pwd=提取码 path=/保存目录\n"
                        "示例：夸克转存 https://pan.quark.cn/s/xxxx pwd=abcd path=/最新动画"
                    ),
                )
                return True
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"正在处理夸克转存：{arg}",
            )
            threading.Thread(
                target=self._run_quark_save,
                args=(arg, receive_chat_id, receive_open_id),
                name="feishu-quark-save",
                daemon=True,
            ).start()
            return True

        if cmd == "/smart_entry":
            if not arg:
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text=(
                        "用法：处理 片名 或 处理 分享链接\n"
                        "示例1：处理 流浪地球2\n"
                        "示例2：处理 https://pan.quark.cn/s/xxxx pwd=abcd path=/最新动画"
                    ),
                )
                return True
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"正在智能处理：{arg}",
            )
            threading.Thread(
                target=self._run_smart_entry,
                args=(arg, receive_chat_id, receive_open_id),
                name="feishu-smart-entry",
                daemon=True,
            ).start()
            return True

        if cmd == "/smart_pick":
            if not arg:
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text=(
                        "用法：选择 序号\n"
                        "示例：选择 1\n"
                        "也支持：直接回复 1\n"
                        "也支持：选择 1 path=/目录\n"
                        "如需补充当前候选页全部主演：详情"
                    ),
                )
                return True
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"正在继续执行：{arg}",
            )
            threading.Thread(
                target=self._run_smart_pick,
                args=(arg, receive_chat_id, receive_open_id),
                name="feishu-smart-pick",
                daemon=True,
            ).start()
            return True

        if cmd in {"/media_subscribe", "/media_subscribe_search"}:
            if not arg:
                usage = (
                    "用法：订阅媒体 片名"
                    if cmd == "/media_subscribe"
                    else "用法：订阅并搜索 片名"
                )
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text=f"{usage}\n示例：{usage.replace('片名', '流浪地球2')}",
                )
                return True
            immediate_search = cmd == "/media_subscribe_search"
            action_text = "订阅并搜索" if immediate_search else "订阅"
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=f"正在{action_text}：{arg}",
            )
            threading.Thread(
                target=self._run_media_subscribe,
                args=(arg, immediate_search, receive_chat_id, receive_open_id),
                name="feishu-media-subscribe",
                daemon=True,
            ).start()
            return True

        if cmd != "/p115_manual_transfer":
            return False

        if not arg:
            paths = self._get_p115_manual_transfer_paths()
            if not paths:
                self._reply_if_needed(
                    receive_chat_id=receive_chat_id,
                    receive_open_id=receive_open_id,
                    text="未配置待整理目录。\n请先在 P115StrmHelper 中配置 pan_transfer_paths，或直接发送：刮削 /待整理/",
                )
                return True
            self._reply_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                text=(
                    f"已开始刮削 {len(paths)} 个目录：\n"
                    + "\n".join(f"- {path}" for path in paths)
                    + "\n正在调用 115 整理流程，请稍候。"
                ),
            )
            threading.Thread(
                target=self._run_p115_manual_transfer_batch,
                args=(paths, receive_chat_id, receive_open_id),
                name="feishu-p115-manual-transfer-batch",
                daemon=True,
            ).start()
            return True

        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=f"已开始刮削：{arg}\n正在调用 115 整理流程，请稍候。",
        )

        threading.Thread(
            target=self._run_p115_manual_transfer,
            args=(arg, receive_chat_id, receive_open_id),
            name="feishu-p115-manual-transfer",
            daemon=True,
        ).start()
        return True

    def _get_p115_manual_transfer_paths(self) -> List[str]:
        try:
            config = self.systemconfig.get("plugin.P115StrmHelper") or {}
            raw = str(config.get("pan_transfer_paths") or "").strip()
            if not raw:
                return []
            return [line.strip() for line in raw.splitlines() if line.strip()]
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 获取待整理目录失败：{exc}")
            return []

    def _run_p115_manual_transfer_batch(
        self,
        paths: List[str],
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        summaries: List[str] = []
        for path in paths:
            summaries.append(self._execute_p115_manual_transfer(path))
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text="\n\n".join(summary for summary in summaries if summary),
        )

    def _run_p115_manual_transfer(
        self,
        path: str,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        summary_text = self._execute_p115_manual_transfer(path)
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=summary_text,
        )

    def _execute_p115_manual_transfer(self, path: str) -> str:
        log_path = Path("/config/logs/plugins/P115StrmHelper.log")
        log_offset = self._safe_log_offset(log_path)
        try:
            service_module = importlib.import_module(
                "app.plugins.p115strmhelper.service"
            )
            servicer = getattr(service_module, "servicer", None)
            if not servicer or not getattr(servicer, "monitorlife", None):
                return "刮削失败：P115StrmHelper 未初始化或未启用。"

            logger.info(f"[FeishuCommandBridge] 开始执行手动刮削：{path}")
            result = servicer.monitorlife.once_transfer(path)
            logger.info(f"[FeishuCommandBridge] 手动刮削完成：{path}")
            summary_text = self._format_p115_manual_transfer_result(result)
            if not summary_text:
                summary_text = self._build_p115_manual_transfer_summary(log_path, log_offset, path)
            return summary_text or f"刮削完成：{path}"
        except Exception as exc:
            logger.error(
                f"[FeishuCommandBridge] 手动刮削失败：{path} {exc}\n{traceback.format_exc()}"
            )
            return f"刮削失败：{path}\n错误：{exc}"

    def _format_p115_manual_transfer_result(self, result: Any) -> Optional[str]:
        if not isinstance(result, dict):
            return None

        path = result.get("path") or ""
        total = result.get("total", 0)
        files = result.get("files", 0)
        dirs = result.get("dirs", 0)
        success = result.get("success", 0)
        failed = result.get("failed", 0)
        skipped = result.get("skipped", 0)
        error = result.get("error")
        failed_items = result.get("failed_items") or []

        lines = [
            f"刮削完成：{path}",
            f"总计：{total} 个项目（文件 {files}，文件夹 {dirs}）",
            f"成功：{success} 个",
            f"失败：{failed} 个",
            f"跳过：{skipped} 个",
        ]
        if error:
            lines.append(f"错误：{error}")
        if failed_items:
            lines.append("失败示例：")
            lines.extend(f"- {item}" for item in failed_items[:3])
            remain = len(failed_items) - 3
            if remain > 0:
                lines.append(f"- 还有 {remain} 项未展示")
        strm_hint_path = self._get_p115_strm_hint_path() or path
        lines.append("如需增量生成 STRM，请再发送：生成STRM")
        lines.append("如需按全部媒体库全量生成，请再发送：全量STRM")
        lines.append(f"如需指定路径全量生成，请再发送：指定路径STRM {strm_hint_path}")
        return "\n".join(lines)

    def _get_p115_strm_hint_path(self) -> Optional[str]:
        try:
            config = self.systemconfig.get("plugin.P115StrmHelper") or {}
            paths = str(config.get("full_sync_strm_paths") or "").strip()
            if not paths:
                return None
            first_line = next(
                (line.strip() for line in paths.splitlines() if line.strip()),
                "",
            )
            if not first_line:
                return None
            parts = first_line.split("#")
            if len(parts) >= 2 and parts[1].strip():
                return parts[1].strip()
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 获取 P115 STRM 提示路径失败：{exc}")
        return None

    def _safe_log_offset(self, log_path: Path) -> int:
        try:
            if log_path.exists():
                return log_path.stat().st_size
        except Exception:
            pass
        return 0

    def _build_p115_manual_transfer_summary(
        self,
        log_path: Path,
        start_offset: int,
        path: str,
    ) -> Optional[str]:
        try:
            if not log_path.exists():
                return None

            with log_path.open("r", encoding="utf-8", errors="ignore") as f:
                f.seek(start_offset)
                chunk = f.read()

            if not chunk:
                return None

            path_re = re.escape(path)
            summary_pattern = re.compile(
                rf"手动网盘整理完成 - 路径: {path_re}\n"
                rf"\s*总计: (?P<total>\d+) 个项目 \(文件: (?P<files>\d+), 文件夹: (?P<dirs>\d+)\)\n"
                rf"\s*成功: (?P<success>\d+) 个\n"
                rf"\s*失败: (?P<failed>\d+) 个\n"
                rf"\s*跳过: (?P<skipped>\d+) 个",
                re.S,
            )
            match = summary_pattern.search(chunk)
            if not match:
                return None

            summary = (
                f"刮削完成：{path}\n"
                f"总计：{match.group('total')} 个项目"
                f"（文件 {match.group('files')}，文件夹 {match.group('dirs')}）\n"
                f"成功：{match.group('success')} 个\n"
                f"失败：{match.group('failed')} 个\n"
                f"跳过：{match.group('skipped')} 个"
            )

            failed_pattern = re.compile(
                r"失败项目详情 \((?P<count>\d+) 个\):\n(?P<items>(?:\s*-\s.*(?:\n|$))*)",
                re.S,
            )
            failed_match = failed_pattern.search(chunk, match.end())
            if failed_match:
                items = [
                    item.strip()[2:].strip()
                    for item in failed_match.group("items").splitlines()
                    if item.strip().startswith("- ")
                ]
                if items:
                    preview = "\n".join(f"- {item}" for item in items[:3])
                    remain = len(items) - 3
                    summary += f"\n失败示例：\n{preview}"
                    if remain > 0:
                        summary += f"\n- 还有 {remain} 项未展示"

            strm_hint_path = self._get_p115_strm_hint_path() or path
            summary += "\n如需增量生成 STRM，请再发送：生成STRM"
            summary += "\n如需按全部媒体库全量生成，请再发送：全量STRM"
            summary += f"\n如需指定路径全量生成，请再发送：指定路径STRM {strm_hint_path}"
            return summary
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 解析 P115 刮削结果失败：{exc}")
            return None

    def _is_duplicate_event(self, event_id: str) -> bool:
        now = time.time()
        with self._event_lock:
            expired = [key for key, ts in self._event_cache.items() if now - ts > 600]
            for key in expired:
                self._event_cache.pop(key, None)
            if event_id in self._event_cache:
                return True
            self._event_cache[event_id] = now
        return self._is_duplicate_event_cross_instance(event_id, now)

    def _is_duplicate_event_cross_instance(self, event_id: str, now: float) -> bool:
        try:
            _EVENT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _EVENT_CACHE_FILE.open("a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()
                cache = json.loads(raw) if raw else {}
                cache = {
                    key: ts
                    for key, ts in cache.items()
                    if isinstance(ts, (int, float)) and now - float(ts) <= 600
                }
                if event_id in cache:
                    f.seek(0)
                    f.truncate()
                    json.dump(cache, f, ensure_ascii=False)
                    f.flush()
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                    return True
                cache[event_id] = now
                f.seek(0)
                f.truncate()
                json.dump(cache, f, ensure_ascii=False)
                f.flush()
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 跨实例事件去重失败：{exc}")
        return False

    def _is_allowed(self, chat_id: str, user_open_id: str) -> bool:
        if self._allow_all:
            return True
        if chat_id and chat_id in self._allowed_chat_ids:
            return True
        if user_open_id and user_open_id in self._allowed_user_ids:
            return True
        return False

    def _map_text_to_command(self, text: str) -> Optional[str]:
        text = self._sanitize_text(text)
        if not text:
            return None
        if text.startswith("/"):
            return text
        normalized = text.strip().lower()
        if normalized in {"n", "next", "下一页", "下页"} or normalized.startswith("n "):
            return f"/smart_pick {text}".strip()
        shortcut_match = re.fullmatch(r"(\d+)(?:\s+(.+))?", text)
        if shortcut_match:
            rest = str(shortcut_match.group(2) or "").strip()
            if not rest or "=" in rest or rest.startswith("/"):
                return f"/smart_pick {text}".strip()
        first_url = self._extract_first_url(text)
        if first_url and self._detect_share_kind(first_url) in {"115", "quark"}:
            return f"/smart_entry {text}".strip()

        alias_map = self._parse_aliases()
        parts = text.split(maxsplit=1)
        alias = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        target = alias_map.get(alias)
        if not target:
            for alias_key in sorted(alias_map.keys(), key=len, reverse=True):
                if not text.startswith(alias_key):
                    continue
                remain = text[len(alias_key):].strip()
                target = alias_map.get(alias_key)
                if target:
                    if target == "/smart_pick" and alias_key in {"详情", "审查"}:
                        return f"{target} {alias_key} {remain}".strip()
                    return f"{target} {remain}".strip()
            return None
        if target == "/smart_pick" and alias in {"详情", "审查"}:
            return f"{target} {alias} {rest}".strip()
        return f"{target} {rest}".strip()

    def _is_help_request(self, text: str) -> bool:
        text = self._sanitize_text(text)
        return text in {"帮助", "/help", "help"}

    def _is_menu_request(self, text: str) -> bool:
        text = self._sanitize_text(text)
        return text in {"菜单", "/menu", "menu", "面板", "控制面板"}

    def _parse_aliases(self) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for line in self._command_aliases.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value.startswith("/"):
                result[key] = value
        return result

    @classmethod
    def _merge_command_whitelist(cls, configured: List[str]) -> List[str]:
        merged: List[str] = []
        seen = set()
        for cmd in configured or []:
            if cmd and cmd not in seen:
                merged.append(cmd)
                seen.add(cmd)
        for cmd in cls._default_command_whitelist():
            if cmd not in seen:
                merged.append(cmd)
                seen.add(cmd)
        return merged

    @classmethod
    def _merge_command_aliases(cls, configured_text: str) -> str:
        merged = cls._parse_alias_text(cls._default_command_aliases())
        for key, value in cls._parse_alias_text(configured_text).items():
            merged[key] = value
        return "\n".join(f"{key}={value}" for key, value in merged.items())

    @staticmethod
    def _parse_alias_text(text: str) -> Dict[str, str]:
        result: Dict[str, str] = {}
        for line in str(text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value.startswith("/"):
                result[key] = value
        return result

    def _build_help_text(self) -> str:
        aliases = self._parse_aliases()
        alias_lines = [f"{k} -> {v}" for k, v in aliases.items()]
        alias_text = "\n".join(alias_lines) if alias_lines else "未配置别名"
        return (
            "可用命令：\n"
            f"{', '.join(self._command_whitelist)}\n\n"
            "别名：\n"
            f"{alias_text}\n\n"
            "快捷入口：发送“菜单”可查看可复制的快捷命令。"
        )

    def _build_menu_text(self) -> str:
        return (
            "快捷菜单\n"
            "1. MP搜索 片名\n\n"
            "2. 影巢搜索 片名\n\n"
            "3. 盘搜搜索 片名\n\n"
            "4. 直接发 115 / 夸克链接\n\n"
            "5. 选择 序号\n\n"
            "6. 刮削\n\n"
            "7. 生成STRM\n\n"
            "8. 全量STRM\n\n"
            "9. 夸克转存 分享链接 pwd=提取码 path=/保存目录\n\n"
            "10. 下载资源 序号\n\n"
            "11. 订阅媒体 片名\n\n"
            "12. 订阅并搜索 片名\n\n"
            "13. 版本"
        )

    def _cache_key(self, receive_chat_id: str, receive_open_id: str) -> str:
        return f"{receive_chat_id or ''}::{receive_open_id or ''}"

    def _set_search_cache(
        self,
        cache_key: str,
        keyword: str,
        mediainfo: Any,
        results: List[Any],
    ) -> None:
        with self._search_cache_lock:
            self._search_cache[cache_key] = {
                "ts": time.time(),
                "keyword": keyword,
                "mediainfo": mediainfo,
                "results": results[:10],
            }

    def _get_search_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._search_cache_lock:
            item = self._search_cache.get(cache_key)
            if not item:
                return None
            if time.time() - float(item.get("ts") or 0) > 1800:
                self._search_cache.pop(cache_key, None)
                return None
            return item

    def _set_smart_cache(
        self,
        cache_key: str,
        *,
        action: str,
        items: List[Dict[str, Any]],
        target_path: str = "",
        keyword: str = "",
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        item_limit = 50 if action == "hdhive_candidates" else 20
        payload = {
            "ts": time.time(),
            "action": action,
            "keyword": keyword,
            "target_path": target_path,
            "items": items[:item_limit],
            "meta": meta or {},
        }
        with self._smart_cache_lock:
            self._smart_cache[cache_key] = payload
        self._persist_smart_cache(cache_key, payload)

    def _get_smart_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._smart_cache_lock:
            item = self._smart_cache.get(cache_key)
        if not item:
            item = self._load_persisted_smart_cache(cache_key)
            if item:
                with self._smart_cache_lock:
                    self._smart_cache[cache_key] = item
        if not item:
            return None
        if time.time() - float(item.get("ts") or 0) > 1800:
            with self._smart_cache_lock:
                self._smart_cache.pop(cache_key, None)
            self._remove_persisted_smart_cache(cache_key)
            return None
        return item

    def _persist_smart_cache(self, cache_key: str, payload: Dict[str, Any]) -> None:
        try:
            _SMART_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _SMART_CACHE_FILE.open("a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()
                cache = json.loads(raw) if raw else {}
                if not isinstance(cache, dict):
                    cache = {}
                now = time.time()
                cache = {
                    key: value
                    for key, value in cache.items()
                    if isinstance(value, dict) and now - float(value.get("ts") or 0) <= 1800
                }
                cache[cache_key] = payload
                f.seek(0)
                f.truncate()
                json.dump(cache, f, ensure_ascii=False)
                f.flush()
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 写入智能缓存失败：{exc}")

    def _load_persisted_smart_cache(self, cache_key: str) -> Optional[Dict[str, Any]]:
        try:
            if not _SMART_CACHE_FILE.exists():
                return None
            with _SMART_CACHE_FILE.open("r", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                raw = f.read().strip()
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            cache = json.loads(raw) if raw else {}
            item = cache.get(cache_key) if isinstance(cache, dict) else None
            return item if isinstance(item, dict) else None
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 读取智能缓存失败：{exc}")
            return None

    def _remove_persisted_smart_cache(self, cache_key: str) -> None:
        try:
            if not _SMART_CACHE_FILE.exists():
                return
            with _SMART_CACHE_FILE.open("a+", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                f.seek(0)
                raw = f.read().strip()
                cache = json.loads(raw) if raw else {}
                if isinstance(cache, dict) and cache.pop(cache_key, None) is not None:
                    f.seek(0)
                    f.truncate()
                    json.dump(cache, f, ensure_ascii=False)
                    f.flush()
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 删除智能缓存失败：{exc}")

    def _run_media_search(
        self,
        keyword: str,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        text = self._execute_media_search(
            keyword=keyword,
            cache_key=self._cache_key(receive_chat_id, receive_open_id),
        )
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=text,
        )

    def _run_pansou_search(
        self,
        keyword: str,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        text = self._execute_pansou_search(
            keyword=keyword,
            cache_key=self._cache_key(receive_chat_id, receive_open_id),
        )
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=text,
        )

    def _run_media_download(
        self,
        index: int,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        text = self._execute_media_download(
            index=index,
            cache_key=self._cache_key(receive_chat_id, receive_open_id),
        )
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=text,
        )

    def _run_media_subscribe(
        self,
        keyword: str,
        immediate_search: bool,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        text = self._execute_media_subscribe(
            keyword=keyword,
            immediate_search=immediate_search,
        )
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=text,
        )

    def _run_smart_entry(
        self,
        arg: str,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        ok, text, data = self._execute_smart_entry(
            arg=arg,
            cache_key=self._cache_key(receive_chat_id, receive_open_id),
        )
        result = data.get("result") or {}
        if data.get("action") == "p115_qrcode_start":
            self._reply_qrcode_data_url_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                data_url=str(result.get("qrcode") or ""),
            )
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=text,
        )

    def _run_smart_pick(
        self,
        arg: str,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        ok, text, _ = self._execute_smart_pick(
            arg=arg,
            cache_key=self._cache_key(receive_chat_id, receive_open_id),
        )
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=text,
        )

    @staticmethod
    def _extract_first_url(text: str) -> str:
        match = re.search(r"https?://[^\s<>\"']+", str(text or ""))
        return match.group(0).rstrip(".,);]") if match else ""

    @staticmethod
    def _is_p115_qrcode_start_text(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "")).lower()
        return compact in {
            "115登录",
            "115扫码",
            "扫码115",
            "登录115",
            "115login",
            "115qrcode",
            "p115login",
            "p115qrcode",
        }

    @staticmethod
    def _is_p115_qrcode_check_text(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "")).lower()
        return compact in {
            "检查115登录",
            "115登录状态",
            "115状态",
            "检查115扫码",
            "检查扫码",
            "115check",
            "check115login",
            "p115check",
        }

    @staticmethod
    def _is_p115_assistant_text(text: str) -> bool:
        compact = re.sub(r"\s+", "", str(text or "")).lower()
        return compact in {
            "115帮助",
            "115任务",
            "继续115任务",
            "取消115任务",
        }

    @classmethod
    def _is_forced_aro_smart_text(cls, text: str) -> bool:
        return cls._is_p115_qrcode_start_text(text) or cls._is_p115_qrcode_check_text(text) or cls._is_p115_assistant_text(text)

    @staticmethod
    def _detect_share_kind(url: str) -> str:
        host = (urlparse(url).hostname or "").lower().strip(".")
        if host.endswith("quark.cn"):
            return "quark"
        if host == "115.com" or host.endswith(".115.com") or "115cdn.com" in host:
            return "115"
        return ""

    @staticmethod
    def _normalize_pan_path(path: str) -> str:
        text = str(path or "").strip()
        if not text:
            return ""
        if not text.startswith("/"):
            text = f"/{text}"
        return re.sub(r"/+", "/", text).rstrip("/") or "/"

    @classmethod
    def _resolve_pan_path_value(cls, value: str) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        alias_map = {
            "分享": "/飞书",
            "飞书": "/飞书",
            "待整理": "/待整理",
            "最新动画": "/最新动画",
        }
        mapped = alias_map.get(text, text)
        return cls._normalize_pan_path(mapped)

    @staticmethod
    def _normalize_search_text(text: str) -> str:
        value = str(text or "").strip().lower()
        value = re.sub(r"\s+", "", value)
        value = re.sub(r"[^\w\u4e00-\u9fff]+", "", value)
        return value

    @staticmethod
    def _format_pansou_datetime(value: Any) -> str:
        text = str(value or "").strip()
        if not text or text.startswith("0001-01-01"):
            return ""
        text = text.replace("T", " ").replace("Z", "")
        if len(text) >= 10:
            text = text[:10].replace("-", "/")
        return text.strip()

    @staticmethod
    def _format_pansou_source(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        return text.split(":", 1)[-1] if ":" in text else text

    @staticmethod
    def _short_share_code(url: str) -> str:
        text = str(url or "").strip()
        if not text:
            return ""
        match = re.search(r"/s/([^/?#]+)", text)
        code = match.group(1) if match else text.rstrip("/").rsplit("/", 1)[-1]
        return code[:6]

    def _parse_smart_arg(self, arg: str) -> Dict[str, str]:
        text = self._sanitize_text(arg or "")
        share_url = self._extract_first_url(text)
        remain = text.replace(share_url, " ").strip() if share_url else text
        keyword_parts: List[str] = []
        options: Dict[str, str] = {
            "url": share_url,
            "access_code": "",
            "path": "",
            "type": "",
            "year": "",
        }
        for token in remain.split():
            item = token.strip()
            if not item:
                continue
            if "=" in item:
                key, value = item.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                if key in {"pwd", "passcode", "code", "提取码"} and value:
                    options["access_code"] = value
                    continue
                if key in {"path", "dir", "目录", "位置"} and value:
                    options["path"] = self._resolve_pan_path_value(value)
                    continue
                if key in {"type", "媒体类型"} and value:
                    options["type"] = value.strip().lower()
                    continue
                if key in {"year", "年份"} and value:
                    options["year"] = value.strip()
                    continue
            if item.startswith("/") and not options["path"]:
                options["path"] = self._resolve_pan_path_value(item)
                continue
            if not share_url and item in {"电影", "movie"}:
                options["type"] = "movie"
                continue
            if not share_url and item in {"电视剧", "剧集", "tv"}:
                options["type"] = "tv"
                continue
            if not share_url and not options["year"] and re.fullmatch(r"(19|20)\d{2}", item):
                options["year"] = item
                continue
            keyword_parts.append(item)

        keyword = " ".join(keyword_parts).strip()
        for prefix in ("影巢 ", "影巢搜索 ", "搜索影巢 "):
            if keyword.startswith(prefix):
                keyword = keyword[len(prefix):].strip()
                break

        media_type = options["type"]
        if media_type in {"电影", "movie"}:
            media_type = "movie"
        elif media_type in {"电视剧", "剧集", "tv"}:
            media_type = "tv"
        elif re.search(r"(第\s*\d+\s*季|S\d{1,2}|EP?\d+)", keyword, re.IGNORECASE):
            media_type = "tv"
        else:
            media_type = "movie"

        return {
            "url": options["url"],
            "access_code": options["access_code"],
            "path": options["path"],
            "type": media_type,
            "year": options["year"],
            "keyword": keyword,
        }

    @staticmethod
    def _parse_pick_arg(arg: str) -> Tuple[int, str, str]:
        text = str(arg or "").strip()
        index = 0
        path = ""
        action = "pick"
        lowered = text.lower()
        if lowered in {"n", "next", "下一页", "下页"} or lowered.startswith("n "):
            action = "next_page"
        for token in text.split():
            item = token.strip()
            if not item:
                continue
            if item.lower() in {"n", "next", "下一页", "下页"}:
                action = "next_page"
                continue
            if item.lower() in {"detail", "details", "review"} or item in {"详情", "审查"}:
                action = "detail"
                continue
            if item.isdigit() and index <= 0:
                index = int(item)
                continue
            if "=" in item:
                key, value = item.split("=", 1)
                if key.strip().lower() in {"path", "dir", "目录", "位置"} and value.strip():
                    path = value.strip()
                    continue
            if item.startswith("/") and not path:
                path = item
        return index, FeishuCommandBridgeLong._resolve_pan_path_value(path), action

    @staticmethod
    def _strip_search_prefix(text: str) -> Tuple[str, str]:
        raw = str(text or "").strip()
        if FeishuCommandBridgeLong._is_forced_aro_smart_text(raw):
            return "", raw
        mappings = [
            ("1搜索", "pansou"),
            ("2搜索", "hdhive"),
            ("MP搜索", "mp"),
            ("原生搜索", "mp"),
            ("搜索资源", "mp"),
            ("搜索", "mp"),
            ("影巢搜索", "hdhive"),
            ("yc", "hdhive"),
            ("2", "hdhive"),
            ("盘搜搜索", "pansou"),
            ("盘搜", "pansou"),
            ("ps", "pansou"),
            ("1", "pansou"),
        ]
        for prefix, mode in mappings:
            if raw == prefix:
                return mode, ""
            if raw.startswith(prefix + " "):
                return mode, raw[len(prefix):].strip()
            if raw.startswith(prefix):
                remain = raw[len(prefix):].strip()
                if remain:
                    return mode, remain
        return "", raw

    def _get_hdhive_default_path(self) -> str:
        try:
            config = self.systemconfig.get("plugin.AgentResourceOfficer") or {}
            path = self._normalize_pan_path(config.get("hdhive_default_path") or "")
            if path:
                return path
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 获取 Agent影视助手影巢默认目录失败：{exc}")
        try:
            config = self.systemconfig.get("plugin.HdhiveOpenApi") or {}
            path = self._normalize_pan_path(config.get("transfer_115_path") or "")
            if path:
                return path
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 获取影巢默认目录失败：{exc}")
        return "/待整理"

    def _get_quark_default_path(self) -> str:
        try:
            config = self.systemconfig.get("plugin.AgentResourceOfficer") or {}
            path = self._normalize_pan_path(config.get("quark_default_path") or "")
            if path:
                return path
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 获取 Agent影视助手夸克默认目录失败：{exc}")
        try:
            config = self.systemconfig.get("plugin.QuarkShareSaver") or {}
            path = self._normalize_pan_path(
                config.get("default_target_path")
                or config.get("target_path")
                or ""
            )
            if path:
                return path
        except Exception as exc:
            logger.warning(f"[FeishuCommandBridge] 获取夸克默认目录失败：{exc}")
        return "/飞书"

    def _local_api_base(self) -> str:
        return f"http://127.0.0.1:{settings.PORT}"

    @staticmethod
    def _get_running_plugin(plugin_id: str) -> Optional[Any]:
        try:
            return PluginManager().running_plugins.get(plugin_id)
        except Exception:
            return None

    def _should_use_agent_resource_officer(self) -> bool:
        backend = self._normalize_execution_backend(self._execution_backend)
        aro = self._get_running_plugin("AgentResourceOfficer")
        if backend == "legacy":
            return False
        if backend == "agent_resource_officer":
            return aro is not None
        return aro is not None

    def _requires_agent_resource_officer(self) -> bool:
        return self._normalize_execution_backend(self._execution_backend) == "agent_resource_officer"

    def _has_agent_resource_officer(self) -> bool:
        return self._get_running_plugin("AgentResourceOfficer") is not None

    def _call_local_json_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[bool, Dict[str, Any], str]:
        query = {"apikey": settings.API_TOKEN}
        for key, value in (params or {}).items():
            if value is None or value == "":
                continue
            query[key] = value
        url = f"{self._local_api_base()}{path}?{urlencode(query)}"
        try:
            response = RequestUtils().get(url=url)
            if response is None:
                return False, {}, "未收到本机插件响应"
            if hasattr(response, "json"):
                data = response.json()
            elif isinstance(response, (bytes, bytearray)):
                data = json.loads(response.decode("utf-8", "ignore"))
            elif isinstance(response, str):
                data = json.loads(response)
            else:
                raw = getattr(response, "text", None)
                if callable(raw):
                    raw = raw()
                elif raw is None and hasattr(response, "read"):
                    raw = response.read()
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", "ignore")
                data = json.loads(raw or "{}")
        except Exception as exc:
            return False, {}, f"请求失败：{exc}"
        return bool(data.get("success")), data, str(data.get("message") or "")

    def _call_local_json_post(self, path: str, payload: Dict[str, Any]) -> Tuple[bool, Dict[str, Any], str]:
        url = f"{self._local_api_base()}{path}?apikey={settings.API_TOKEN}"
        try:
            response = RequestUtils(content_type="application/json").post(
                url=url,
                json=payload,
            )
            if response is None:
                return False, {}, "未收到本机插件响应"
            data = response.json()
        except Exception as exc:
            return False, {}, f"请求失败：{exc}"
        return bool(data.get("success")), data, str(data.get("message") or "")

    def _call_quark_transfer(
        self,
        share_url: str,
        access_code: str = "",
        target_path: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        if self._should_use_agent_resource_officer():
            ok, data, message = self._call_local_json_post(
                "/api/v1/plugin/AgentResourceOfficer/quark/transfer",
                {
                    "url": share_url,
                    "access_code": access_code,
                    "path": target_path,
                },
            )
            result = data.get("data") or {}
            final_message = (
                message
                or str(result.get("message") or "")
                or str(result.get("error") or "")
                or str(result.get("detail") or "")
            )
            return ok, {"data": result}, final_message
        if self._requires_agent_resource_officer():
            return False, {}, "Agent影视助手 未加载"
        plugin = self._get_running_plugin("QuarkShareSaver")
        if not plugin:
            return False, {}, "QuarkShareSaver 未加载"
        ok, result, message = plugin.transfer_share(
            share_text=share_url,
            access_code=access_code,
            target_path=target_path,
            remember=True,
            trigger="FeishuCommandBridgeLong 智能入口",
        )
        result = result or {}
        final_message = (
            message
            or str(result.get("message") or "")
            or str(result.get("error") or "")
            or str(result.get("detail") or "")
        )
        return ok, {"data": result}, final_message

    def _call_hdhive_search(
        self,
        keyword: str,
        media_type: str,
        year: str = "",
        candidate_limit: int = 5,
        limit: int = 10,
    ) -> Tuple[bool, Dict[str, Any], str]:
        plugin = self._get_running_plugin("HdhiveOpenApi")
        if not plugin:
            return False, {}, "HdhiveOpenApi 未加载"
        ok, result, message = asyncio.run(
            plugin.search_resources_by_keyword(
                keyword=keyword,
                media_type=media_type,
                year=year,
                candidate_limit=candidate_limit,
                result_limit=limit,
                remember=True,
            )
        )
        return ok, {"data": result}, message

    def _call_aro_hdhive_session_search(
        self,
        keyword: str,
        media_type: str,
        year: str = "",
        target_path: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        return self._call_local_json_post(
            "/api/v1/plugin/AgentResourceOfficer/session/hdhive/search",
            {
                "keyword": keyword,
                "type": media_type or "movie",
                "year": year,
                "path": target_path,
            },
        )

    def _call_aro_hdhive_session_pick(
        self,
        session_id: str,
        index: int,
        target_path: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        return self._call_local_json_post(
            "/api/v1/plugin/AgentResourceOfficer/session/hdhive/pick",
            {
                "session_id": session_id,
                "index": index,
                "path": target_path,
            },
        )

    def _call_aro_assistant_route(
        self,
        session_id: str,
        text: str,
    ) -> Tuple[bool, Dict[str, Any], str]:
        return self._call_local_json_post(
            "/api/v1/plugin/AgentResourceOfficer/assistant/route",
            {
                "session": session_id,
                "text": text,
            },
        )

    def _call_aro_assistant_pick(
        self,
        session_id: str,
        index: int,
        target_path: str = "",
        action: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        return self._call_local_json_post(
            "/api/v1/plugin/AgentResourceOfficer/assistant/pick",
            {
                "session": session_id,
                "index": index,
                "path": target_path,
                "action": action,
            },
        )

    def _should_force_aro_for_p115_login(self, text: str) -> bool:
        return self._is_forced_aro_smart_text(text)

    def _call_hdhive_search_by_tmdb(
        self,
        tmdb_id: Any,
        media_type: str,
        year: str = "",
        limit: int = 20,
    ) -> Tuple[bool, Dict[str, Any], str]:
        tmdb_value = str(tmdb_id or "").strip()
        if not tmdb_value:
            return False, {}, "缺少 TMDB ID"
        if self._should_use_agent_resource_officer():
            return self._call_local_json_post(
                "/api/v1/plugin/AgentResourceOfficer/hdhive/search",
                {
                    "type": media_type or "movie",
                    "tmdb_id": tmdb_value,
                    "year": year,
                    "limit": limit,
                },
            )
        if self._requires_agent_resource_officer():
            return False, {}, "Agent影视助手 未加载"
        return self._call_local_json_get(
            "/api/v1/plugin/HdhiveOpenApi/resources/search",
            params={
                "type": media_type or "movie",
                "tmdb_id": tmdb_value,
                "year": year,
                "limit": limit,
            },
        )

    @classmethod
    def _read_tmdb_api_key(cls) -> str:
        with cls._tmdb_api_key_lock:
            if cls._tmdb_api_key_cache:
                return cls._tmdb_api_key_cache
            override_key = cls._clean_input(getattr(cls, "_tmdb_api_key_override", ""))
            if override_key:
                cls._tmdb_api_key_cache = override_key
                return override_key
            env_key = cls._clean_input(__import__("os").environ.get("TMDB_API_KEY"))
            if env_key:
                cls._tmdb_api_key_cache = env_key
                return env_key
            compose_path = Path("/Applications/Dockge/moviepilot-ai-recognizer-gateway/docker-compose.yml")
            if compose_path.exists():
                for line in compose_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                    if "TMDB_API_KEY" not in line:
                        continue
                    _, _, value = line.partition(":")
                    key = cls._clean_input(value.strip().strip("'\""))
                    if key:
                        cls._tmdb_api_key_cache = key
                        return key
            return ""

    @classmethod
    def _fetch_candidate_actors(cls, tmdb_id: Any, media_type: str) -> List[str]:
        clean_tmdb_id = cls._clean_input(tmdb_id)
        clean_media_type = cls._clean_input(media_type).lower()
        if not clean_tmdb_id or clean_media_type not in {"movie", "tv"}:
            return []
        cache_key = f"{clean_media_type}:{clean_tmdb_id}"
        with cls._candidate_actor_cache_lock:
            cached = cls._candidate_actor_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        tmdb_api_key = cls._read_tmdb_api_key()
        if not tmdb_api_key:
            return []
        query = urlencode(
            {
                "api_key": tmdb_api_key,
                "language": "zh-CN",
                "append_to_response": "credits",
            }
        )
        endpoint = "movie" if clean_media_type == "movie" else "tv"
        url = f"https://api.themoviedb.org/3/{endpoint}/{clean_tmdb_id}?{query}"
        actors: List[str] = []
        try:
            request = UrlRequest(url=url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8", "ignore"))
            cast = ((payload.get("credits") or {}).get("cast") or []) if isinstance(payload, dict) else []
            for member in cast[:10]:
                name = cls._clean_input((member or {}).get("name"))
                department = cls._clean_input((member or {}).get("known_for_department"))
                if not name:
                    continue
                if department and department != "Acting":
                    continue
                if name not in actors:
                    actors.append(name)
                if len(actors) >= 2:
                    break
        except Exception:
            actors = []
        with cls._candidate_actor_cache_lock:
            cls._candidate_actor_cache[cache_key] = list(actors)
        return actors

    def _maybe_enrich_hdhive_candidate_with_actors(
        self,
        candidate: Dict[str, Any],
        *,
        enabled: bool = False,
    ) -> Dict[str, Any]:
        enriched = dict(candidate or {})
        if not enabled:
            return enriched
        actors = enriched.get("actors") or []
        if actors:
            return enriched
        enriched["actors"] = self._fetch_candidate_actors(
            enriched.get("tmdb_id"),
            str(enriched.get("media_type") or enriched.get("type") or ""),
        )
        return enriched

    def _enrich_hdhive_candidates_with_actors(
        self,
        candidates: List[Dict[str, Any]],
        *,
        enabled: bool = False,
    ) -> List[Dict[str, Any]]:
        if not enabled:
            return [dict(item) for item in candidates]
        indexed_candidates = [(idx, dict(item or {})) for idx, item in enumerate(candidates)]
        pending = [
            (idx, candidate)
            for idx, candidate in indexed_candidates
            if not (candidate.get("actors") or [])
        ]
        enriched_map: Dict[int, Dict[str, Any]] = {idx: candidate for idx, candidate in indexed_candidates}
        if pending:
            max_workers = min(4, len(pending))
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_map = {
                    executor.submit(
                        self._maybe_enrich_hdhive_candidate_with_actors,
                        candidate,
                        enabled=True,
                    ): idx
                    for idx, candidate in pending
                }
                for future in concurrent.futures.as_completed(future_map):
                    idx = future_map[future]
                    try:
                        enriched_map[idx] = future.result()
                    except Exception:
                        enriched_map[idx] = dict(indexed_candidates[idx][1])
        return [enriched_map[idx] for idx, _ in indexed_candidates]

    def _call_hdhive_unlock(
        self,
        slug: str,
        *,
        transfer_115: bool = True,
        target_path: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        if self._should_use_agent_resource_officer():
            return self._call_local_json_post(
                "/api/v1/plugin/AgentResourceOfficer/hdhive/unlock",
                {
                    "slug": slug,
                    "path": target_path,
                    "transfer_115": transfer_115,
                },
            )
        if self._requires_agent_resource_officer():
            return False, {}, "Agent影视助手 未加载"
        plugin = self._get_running_plugin("HdhiveOpenApi")
        if not plugin:
            return False, {}, "HdhiveOpenApi 未加载"
        ok, result, message = plugin.unlock_resource(
            slug=slug,
            remember=True,
            transfer_115=transfer_115,
            transfer_path=target_path,
        )
        return ok, {"data": result}, message

    def _call_hdhive_transfer_115(
        self,
        share_url: str,
        access_code: str = "",
        target_path: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        if self._should_use_agent_resource_officer():
            return self._call_local_json_post(
                "/api/v1/plugin/AgentResourceOfficer/p115/transfer",
                {
                    "url": share_url,
                    "access_code": access_code,
                    "path": target_path,
                },
            )
        if self._requires_agent_resource_officer():
            return False, {}, "Agent影视助手 未加载"
        plugin = self._get_running_plugin("HdhiveOpenApi")
        if not plugin:
            return False, {}, "HdhiveOpenApi 未加载"
        ok, result, message = plugin.transfer_115_share(
            url=share_url,
            access_code=access_code,
            path=target_path,
            remember=True,
            trigger="FeishuCommandBridgeLong 智能入口",
        )
        return ok, {"data": result}, message

    def _call_pansou_search(self, keyword: str) -> Tuple[bool, Dict[str, Any], str]:
        last_error = ""
        queries = [
            {"kw": keyword, "res": "merge", "src": "all"},
            {"kw": keyword},
            {"keyword": keyword},
        ]
        urls = []
        for query in queries:
            urls.append(f"http://host.docker.internal:805/api/search?{urlencode(query)}")
            urls.append(f"http://127.0.0.1:805/api/search?{urlencode(query)}")
        data: Dict[str, Any] = {}
        for url in urls:
            try:
                request = UrlRequest(url=url, headers={"Accept": "application/json"})
                with urlopen(request, timeout=20) as response:
                    data = json.loads(response.read().decode("utf-8", "ignore"))
                break
            except Exception as exc:
                last_error = str(exc)
                data = {}
        if not data:
            return False, {}, f"盘搜请求失败：{last_error or '未知错误'}"
        ok = str(data.get("code")) == "0"
        if not ok:
            return False, data, str(data.get("message") or "盘搜搜索失败")
        return True, data, str(data.get("message") or "success")

    @staticmethod
    def _safe_points_text(item: Dict[str, Any]) -> str:
        value = item.get("unlock_points")
        if value is None or str(value).strip() == "":
            return "未知"
        return str(value)

    @staticmethod
    def _format_hdhive_candidate_label(candidate: Dict[str, Any]) -> str:
        title = str(candidate.get("title") or "未知影片").strip()
        year = str(candidate.get("year") or "").strip()
        media_type = str(candidate.get("media_type") or candidate.get("type") or "").strip()
        actors = candidate.get("actors") or []
        parts = []
        if year:
            parts.append(year)
        if media_type:
            parts.append(media_type)
        if actors:
            actor_text = " / ".join(str(name).strip() for name in actors[:2] if str(name).strip())
            if actor_text:
                parts.append(f"主演:{actor_text}")
        if parts:
            return f"{title} ({' | '.join(parts)})"
        return title

    @staticmethod
    def _format_hdhive_size(size: Any) -> str:
        text = str(size or "").strip()
        if not text or text.lower() == "none":
            return ""
        if re.search(r"[a-zA-Z]$", text):
            return text
        return f"{text}GB"

    @staticmethod
    def _normalize_hdhive_pan_type(value: Any) -> str:
        text = str(value or "").strip().lower()
        if "115" in text:
            return "115"
        if "quark" in text:
            return "quark"
        return text or "未知"

    def _collect_hdhive_channel_items(
        self,
        items: List[Dict[str, Any]],
        channel_name: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        channel_results: List[Dict[str, Any]] = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            pan_type = self._normalize_hdhive_pan_type(item.get("pan_type"))
            if pan_type != channel_name:
                continue
            slug = str(item.get("slug") or "").strip()
            title = str(item.get("title") or item.get("matched_title") or "未知资源").strip()
            remark = str(item.get("remark") or "").strip()
            key = slug or f"{title}|{remark}"
            if key in seen:
                continue
            seen.add(key)
            channel_results.append(item)
            if len(channel_results) >= limit:
                break
        return channel_results

    def _format_hdhive_candidate_text(
        self,
        keyword: str,
        candidates: List[Dict[str, Any]],
        target_path: str,
        page: int = 1,
        page_size: int = 10,
    ) -> str:
        total = len(candidates)
        safe_page_size = max(1, page_size)
        total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
        safe_page = min(max(1, page), total_pages)
        start = (safe_page - 1) * safe_page_size
        page_items = candidates[start:start + safe_page_size]
        lines = [
            f"影巢搜索：{keyword}",
            f"候选影片：{total} 个，请先选择影片：",
        ]
        if total_pages > 1:
            lines.append(f"当前第 {safe_page}/{total_pages} 页，每页 {safe_page_size} 条：")
        for candidate in page_items:
            idx = int(candidate.get("index") or 0)
            lines.append(f"{idx}. {self._format_hdhive_candidate_label(candidate)}")
        lines.append("下一步：回复“选择 编号”查看该影片的影巢资源。")
        lines.append("如需补充当前候选页全部主演，可回复：详情 或 审查。")
        if safe_page < total_pages:
            lines.append("如需继续翻页，可回复：n 下一页")
        return "\n".join(lines)

    def _format_hdhive_search_text(
        self,
        keyword: str,
        items: List[Dict[str, Any]],
        selected_candidate: Optional[Dict[str, Any]],
        target_path: str,
    ) -> str:
        channel_115 = self._collect_hdhive_channel_items(items, "115", 6)
        channel_quark = self._collect_hdhive_channel_items(items, "quark", 6)
        fallback_items = []
        if not channel_115 and not channel_quark:
            fallback_items = [item for item in items[:12] if isinstance(item, dict)]
        display_items: List[Dict[str, Any]] = []
        for item in channel_115:
            display_items.append({**item, "index": len(display_items) + 1, "_channel": "115"})
        for item in channel_quark:
            display_items.append({**item, "index": len(display_items) + 1, "_channel": "quark"})
        for item in fallback_items:
            display_items.append(
                {
                    **item,
                    "index": len(display_items) + 1,
                    "_channel": self._normalize_hdhive_pan_type(item.get("pan_type")),
                }
            )

        lines = [f"影巢搜索：{keyword}"]
        if selected_candidate:
            lines.append(f"已选影片：{self._format_hdhive_candidate_label(selected_candidate)}")
        if channel_115 or channel_quark:
            lines.append(
                f"资源结果：共 {len(items)} 条，当前展示 115 {len(channel_115)} 条、夸克 {len(channel_quark)} 条："
            )
        else:
            lines.append(f"资源结果：共 {len(items)} 条，当前展示前 {len(display_items)} 条：")

        for cached in display_items:
            idx = cached["index"]
            channel = cached["_channel"]
            if idx == 1 and channel == "115":
                lines.append("🟦 115 结果")
            elif channel == "quark" and idx == len(channel_115) + 1:
                lines.append("🟨 夸克结果")
            title = str(cached.get("remark") or cached.get("title") or cached.get("matched_title") or "未知资源").strip()
            points = self._safe_points_text(cached)
            if points == "0":
                points_label = "免费"
            elif points == "未知":
                points_label = "积分未知"
            else:
                points_label = f"{points}分"
            lines.append(f"{idx}. [{channel}][{points_label}] {title}")

            detail_parts = []
            matched_title = str(cached.get("matched_title") or "").strip()
            matched_year = str(cached.get("matched_year") or "").strip()
            if matched_title:
                match_label = f"{matched_title} ({matched_year})" if matched_year else matched_title
                detail_parts.append(f"匹配:{match_label}")
            resolutions = [str(v).strip() for v in (cached.get("video_resolution") or []) if str(v).strip()]
            if resolutions:
                detail_parts.append("/".join(resolutions[:2]))
            sources = [str(v).strip() for v in (cached.get("source") or []) if str(v).strip()]
            if sources:
                detail_parts.append("/".join(sources[:2]))
            size_text = self._format_hdhive_size(cached.get("share_size"))
            if size_text:
                detail_parts.append(size_text)
            if detail_parts:
                lines.append(f"   {' | '.join(detail_parts)}")

        if not display_items:
            lines.append("当前没有可展示的资源结果。")
        lines.append(f"下一步：回复“选择 1”即可解锁并转存到 {target_path}。")
        if channel_quark:
            start_index = len(channel_115) + 1
            lines.append(f"夸克结果从 {start_index} 开始编号；例如“选择 {start_index}”可直接处理第 1 条夸克结果。")
            lines.append(f"如需改目录，可发“选择 1 path=/目录”或“选择 {start_index} path=/目录”。")
        else:
            lines.append("如需改目录，可发“选择 1 path=/目录”。")
        return "\n".join(lines)

    def _format_smart_pick_text(
        self,
        selected: Dict[str, Any],
        response_data: Dict[str, Any],
        target_path: str,
    ) -> str:
        result = response_data.get("data") or {}
        unlock_data = result.get("data") or {}
        transfer_data = result.get("transfer_115") or {}
        quark_transfer = result.get("transfer_quark") or {}
        lines = [
            "影巢已执行解锁",
            f"资源：{selected.get('title') or selected.get('matched_title') or '-'}",
            f"积分：{self._safe_points_text(selected)}",
            f"网盘：{selected.get('pan_type') or '-'}",
        ]
        if unlock_data.get("url") or unlock_data.get("full_url"):
            lines.append("解锁结果：已返回资源链接")
        success_lines: List[str] = []
        failure_lines: List[str] = []
        if transfer_data:
            transfer_ok = bool(transfer_data.get("ok"))
            if transfer_ok:
                success_lines.extend(
                    [
                        "115转存：成功",
                        f"目录：{transfer_data.get('path') or target_path}",
                    ]
                )
                if transfer_data.get("message") and str(transfer_data.get("message")).strip().lower() != "success":
                    success_lines.append(f"详情：{transfer_data.get('message')}")
            elif transfer_data.get("message"):
                failure_lines.append(f"115转存失败：{transfer_data.get('message')}")
        else:
            transfer_msg = str(result.get("transfer_115_message") or "").strip()
            if transfer_msg:
                failure_lines.append(f"115转存失败：{transfer_msg}")
        if quark_transfer:
            quark_ok = bool(quark_transfer.get("ok"))
            if quark_ok:
                success_lines.extend(
                    [
                        "夸克转存：成功",
                        f"目录：{quark_transfer.get('target_path') or target_path or '-'}",
                    ]
                )
                if quark_transfer.get("message") and str(quark_transfer.get("message")).strip().lower() != "success":
                    success_lines.append(f"详情：{quark_transfer.get('message')}")
            elif quark_transfer.get("message"):
                failure_lines.append(f"夸克转存失败：{quark_transfer.get('message')}")
        if success_lines:
            lines.extend(success_lines)
        elif failure_lines:
            lines.append("自动转存：未成功")
            lines.extend(failure_lines)
        return "\n".join(lines)

    def _format_aro_route_text(
        self,
        selected: Dict[str, Any],
        route_result: Dict[str, Any],
        target_path: str,
    ) -> str:
        unlock = route_result.get("unlock") or {}
        unlock_data = unlock.get("data") or {}
        route = route_result.get("route") or {}
        lines = [
            "影巢已执行解锁",
            f"资源：{selected.get('title') or selected.get('matched_title') or '-'}",
            f"积分：{self._safe_points_text(selected)}",
            f"网盘：{selected.get('pan_type') or route.get('provider') or route.get('pan_type') or '-'}",
        ]
        if unlock_data.get("url") or unlock_data.get("full_url"):
            lines.append("解锁结果：已返回资源链接")
        provider = str(route.get("provider") or route.get("pan_type") or "").strip().lower()
        message = str(route.get("message") or "").strip()
        final_path = str(route.get("target_path") or target_path or "").strip()
        if provider == "115":
            lines.append("115转存：成功")
        elif provider == "quark":
            lines.append("夸克转存：成功")
        else:
            lines.append("自动路由：已完成")
        if final_path:
            lines.append(f"目录：{final_path}")
        if message and message.lower() != "success":
            lines.append(f"详情：{message}")
        return "\n".join(lines)

    def _format_pansou_pick_text(
        self,
        selected: Dict[str, Any],
        share_kind: str,
        response_data: Dict[str, Any],
        target_path: str,
    ) -> str:
        result = response_data.get("data") or {}
        title = str(selected.get("note") or "未命名资源").strip()
        lines = [
            "盘搜结果已执行转存",
            f"资源：{title}",
            f"类型：{share_kind}",
        ]
        if share_kind == "quark":
            lines.append(f"目录：{result.get('target_path') or target_path or '-'}")
        else:
            lines.append(f"目录：{result.get('path') or target_path}")
            lines.append(f"结果：{result.get('message') or 'success'}")
        return "\n".join(lines)

    @staticmethod
    def _format_115_error_text(message: str) -> str:
        text = str(message or "").strip()
        if not text:
            return "115 转存失败：未知错误"
        if text.startswith("115 转存失败") or text.startswith("影巢解锁成功，但 115 转存失败"):
            return text
        return f"115 转存失败：{text}"

    @staticmethod
    def _compact_115_result(result: Dict[str, Any]) -> Dict[str, Any]:
        compact = {
            "ok": bool(result.get("ok")),
            "path": result.get("path"),
            "message": result.get("message"),
        }
        media_info = ((result.get("data") or {}).get("media_info") or {})
        if isinstance(media_info, dict):
            compact["media"] = {
                "title": media_info.get("title"),
                "year": media_info.get("year"),
                "type": media_info.get("type"),
                "category": media_info.get("category"),
            }
        return compact

    @staticmethod
    def _compact_unlock_result(result: Dict[str, Any]) -> Dict[str, Any]:
        unlock_data = result.get("data") or {}
        transfer_data = result.get("transfer_115") or {}
        quark_transfer = result.get("transfer_quark") or {}
        compact = {
            "ok": bool(result.get("ok")),
            "status_code": result.get("status_code"),
            "message": result.get("message"),
            "slug": result.get("slug"),
            "share_url": unlock_data.get("full_url") or unlock_data.get("url"),
            "access_code": unlock_data.get("access_code"),
        }
        if transfer_data:
            compact["transfer_115"] = {
                "ok": bool(transfer_data.get("ok")),
                "path": transfer_data.get("path"),
                "message": transfer_data.get("message"),
            }
        elif result.get("transfer_115_message"):
            compact["transfer_115"] = {
                "ok": False,
                "path": None,
                "message": result.get("transfer_115_message"),
            }
        if quark_transfer:
            compact["transfer_quark"] = {
                "ok": bool(quark_transfer.get("ok")),
                "target_path": quark_transfer.get("target_path"),
                "task_id": quark_transfer.get("task_id"),
                "saved_count": quark_transfer.get("saved_count"),
                "message": quark_transfer.get("message"),
            }
        return compact

    def _execute_smart_entry(
        self,
        arg: str,
        cache_key: str,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        if self._should_force_aro_for_p115_login(arg):
            ok, payload, message = self._call_aro_assistant_route(cache_key, arg)
            data = payload.get("data") or {}
            text = str(message or "处理失败").strip()
            return ok, text, {
                "action": data.get("action") or "assistant_route",
                "ok": ok,
                "message": text,
                "result": data,
            }
        if self._should_use_agent_resource_officer():
            ok, payload, message = self._call_aro_assistant_route(cache_key, arg)
            data = payload.get("data") or {}
            text = str(message or "处理失败").strip()
            return ok, text, {
                "action": data.get("action") or "assistant_route",
                "ok": ok,
                "message": text,
                "result": data,
            }
        parsed = self._parse_smart_arg(arg)
        share_url = parsed["url"]
        access_code = parsed["access_code"]
        target_path = parsed["path"]
        keyword = parsed["keyword"]
        media_type = parsed["type"]
        year = parsed["year"]

        # Keep 115 direct-link handling on the new ARO path so pending-task,
        # login-resume and cancellation all stay in the same session chain.
        if share_url and self._detect_share_kind(share_url) == "115" and self._has_agent_resource_officer():
            ok, payload, message = self._call_aro_assistant_route(cache_key, arg)
            data = payload.get("data") or {}
            text = str(message or "处理失败").strip()
            return ok, text, {
                "action": data.get("action") or "assistant_route",
                "ok": ok,
                "message": text,
                "result": data,
            }

        if share_url:
            share_kind = self._detect_share_kind(share_url)
            if share_kind == "quark":
                final_path = target_path or self._get_quark_default_path()
                ok, payload, message = self._call_quark_transfer(share_url, access_code, final_path)
                result = payload.get("data") or {}
                text = (
                    "夸克转存已完成\n"
                    f"目录：{result.get('target_path') or final_path or '-'}"
                    if ok
                    else f"夸克转存失败：{message or '未知错误'}"
                )
                return ok, text, {
                    "action": "quark_transfer",
                    "ok": ok,
                    "message": message or text,
                    "result": {
                        "target_path": result.get("target_path"),
                        "task_id": result.get("task_id"),
                        "saved_count": result.get("saved_count"),
                    },
                }
            if share_kind == "115":
                final_path = target_path or self._get_hdhive_default_path()
                ok, payload, message = self._call_hdhive_transfer_115(share_url, access_code, final_path)
                result = payload.get("data") or {}
                text = (
                    "115 转存已完成\n"
                    f"目录：{result.get('path') or final_path}\n"
                    f"结果：{result.get('message') or 'success'}"
                    if ok
                    else self._format_115_error_text(message)
                )
                return ok, text, {
                    "action": "transfer_115",
                    "ok": ok,
                    "message": message or text,
                    "result": self._compact_115_result(result),
                }
            return False, "暂不支持该分享链接类型，请发送夸克链接、115 链接或影巢片名。", {
                "action": "unknown_url",
                "ok": False,
                "message": "unsupported url",
            }

        if not keyword:
            return False, "未识别到可处理内容。你可以发送片名，或直接发送夸克/115 分享链接。", {
                "action": "empty",
                "ok": False,
                "message": "empty input",
            }

        final_path = target_path or self._get_hdhive_default_path()
        if self._should_use_agent_resource_officer():
            ok, payload, message = self._call_aro_hdhive_session_search(
                keyword=keyword,
                media_type=media_type,
                year=year,
                target_path=final_path,
            )
            result = payload.get("data") or {}
            candidates = result.get("candidates") or []
            if not ok:
                return False, f"影巢搜索失败：{message or '暂无结果'}", {
                    "action": "hdhive_candidates",
                    "ok": False,
                    "message": message or "session search failed",
                }
            session_id = str(result.get("session_id") or "").strip()
            if not candidates or not session_id:
                text = result.get("text") or f"影巢搜索失败：{message or '暂无结果'}"
                return False, text, {
                    "action": "hdhive_candidates",
                    "ok": False,
                    "message": message or "empty candidates",
                }
            self._set_smart_cache(
                cache_key,
                action="aro_hdhive",
                items=[],
                target_path=final_path,
                keyword=keyword,
                meta={
                    "session_id": session_id,
                    "stage": "candidate",
                    "media_type": media_type,
                    "year": year,
                    "candidate_count": len(candidates),
                },
            )
            if len(candidates) == 1:
                pick_ok, pick_text, pick_data = self._execute_smart_pick("1", cache_key)
                return pick_ok, pick_text, pick_data
            text = str(result.get("text") or "").strip() or self._format_hdhive_candidate_text(
                keyword,
                [
                    {
                        **dict(candidate or {}),
                        "index": idx,
                    }
                    for idx, candidate in enumerate(candidates, start=1)
                ],
                final_path,
                page=1,
                page_size=self._hdhive_candidate_page_size,
            )
            return True, text, {
                "action": "hdhive_candidates",
                "ok": True,
                "keyword": keyword,
                "path": final_path,
                "candidate_count": len(candidates),
                "next_action": "pick_candidate",
                "session_id": session_id,
            }
        candidate_page_size = 10
        ok, payload, message = self._call_hdhive_search(keyword, media_type, year, candidate_limit=30, limit=20)
        result = payload.get("data") or {}
        items = result.get("data") or []
        candidates = result.get("candidates") or []
        if not ok or not items:
            text = f"影巢搜索失败：{message or result.get('message') or '暂无结果'}"
            if candidates and not items:
                text = (
                    f"已解析到 {len(candidates)} 个候选影片，但影巢暂无可用资源：{keyword}\n"
                    "可以换个年份、片名别名，或稍后再试。"
                )
            return False, text, {
                "action": "hdhive_search",
                "ok": False,
                "message": message or result.get("message") or text,
                "candidates": candidates,
                "items": [],
            }

        if len(candidates) > 1:
            cached_candidates = []
            public_candidates = []
            for index, candidate in enumerate(candidates, start=1):
                cached = dict(candidate)
                cached["index"] = index
                cached_candidates.append(cached)
                public_candidates.append(
                    {
                        "index": index,
                        "tmdb_id": candidate.get("tmdb_id"),
                        "title": candidate.get("title"),
                        "year": candidate.get("year"),
                        "media_type": candidate.get("media_type"),
                        "actors": candidate.get("actors") or [],
                    }
                )
            self._set_smart_cache(
                cache_key,
                action="hdhive_candidates",
                items=cached_candidates,
                target_path=final_path,
                keyword=keyword,
                meta={
                    "media_type": media_type,
                    "year": year,
                    "page": 1,
                    "page_size": candidate_page_size,
                },
            )
            text = self._format_hdhive_candidate_text(
                keyword,
                cached_candidates,
                final_path,
                page=1,
                page_size=candidate_page_size,
            )
            return True, text, {
                "action": "hdhive_candidates",
                "ok": True,
                "keyword": keyword,
                "path": final_path,
                "candidates": public_candidates,
                "next_action": "pick_candidate",
            }

        cached_items = []
        public_items = []
        selected_candidate = candidates[0] if candidates else {}
        for item in self._collect_hdhive_channel_items(items, "115", 6) + self._collect_hdhive_channel_items(items, "quark", 6):
            cached = dict(item)
            cached["index"] = len(cached_items) + 1
            cached_items.append(cached)
        if not cached_items:
            for item in items[:12]:
                cached = dict(item)
                cached["index"] = len(cached_items) + 1
                cached_items.append(cached)
        for item in cached_items:
            cached = dict(item)
            public_items.append(
                {
                    "index": cached.get("index"),
                    "title": item.get("title"),
                    "year": item.get("year"),
                    "pan_type": item.get("pan_type"),
                    "unlock_points": item.get("unlock_points"),
                    "matched_title": item.get("matched_title"),
                    "matched_year": item.get("matched_year"),
                }
            )
        self._set_smart_cache(
            cache_key,
            action="hdhive_search",
            items=cached_items,
            target_path=final_path,
            keyword=keyword,
            meta={"media_type": media_type, "year": year, "candidate": selected_candidate},
        )
        text = self._format_hdhive_search_text(keyword, cached_items, selected_candidate, final_path)
        return True, text, {
            "action": "hdhive_search",
            "ok": True,
            "keyword": keyword,
            "path": final_path,
            "items": public_items,
            "candidate_count": len(candidates),
            "next_action": "pick",
        }

    def _execute_smart_pick(
        self,
        arg: str,
        cache_key: str,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        index, override_path, pick_action = self._parse_pick_arg(arg)
        if self._should_use_agent_resource_officer():
            if index <= 0 and not pick_action:
                return False, "请选择有效序号，例如：选择 1", {
                    "action": "pick",
                    "ok": False,
                    "message": "invalid index",
                }
            ok, payload, message = self._call_aro_assistant_pick(
                cache_key,
                index,
                override_path or "",
                pick_action,
            )
            data = payload.get("data") or {}
            text = str(message or "处理失败").strip()
            return ok, text, {
                "action": data.get("action") or "assistant_pick",
                "ok": ok,
                "message": text,
                "result": data,
            }
        cache = self._get_smart_cache(cache_key)
        if not cache:
            return False, "没有可继续的缓存，请先发送：处理 片名 或 处理 分享链接", {
                "action": "pick",
                "ok": False,
                "message": "cache not found",
            }
        cache_action = cache.get("action")
        if pick_action == "detail":
            if cache_action != "hdhive_candidates":
                return False, "当前结果不支持详情补充，请先发送影巢搜索。", {
                    "action": "pick",
                    "ok": False,
                    "message": "detail unsupported",
                }
            items = cache.get("items") or []
            if not items:
                return False, "当前没有可补充的候选影片。", {
                    "action": "hdhive_candidates",
                    "ok": False,
                    "message": "empty candidates",
                }
            meta = dict(cache.get("meta") or {})
            page_size = int(meta.get("page_size") or 10)
            current_page = int(meta.get("page") or 1)
            final_path = override_path or cache.get("target_path") or self._get_hdhive_default_path()
            start = max(0, (max(1, current_page) - 1) * max(1, page_size))
            end = start + max(1, page_size)
            enriched_items = [dict(item or {}) for item in items]
            enriched_page_items = self._enrich_hdhive_candidates_with_actors(
                enriched_items[start:end],
                enabled=True,
            )
            enriched_items[start:end] = enriched_page_items
            self._set_smart_cache(
                cache_key,
                action="hdhive_candidates",
                items=enriched_items,
                target_path=final_path,
                keyword=cache.get("keyword") or "",
                meta=meta,
            )
            text = self._format_hdhive_candidate_text(
                cache.get("keyword") or "",
                enriched_items,
                final_path,
                page=current_page,
                page_size=page_size,
            )
            return True, text, {
                "action": "hdhive_candidates",
                "ok": True,
                "keyword": cache.get("keyword") or "",
                "path": final_path,
                "page": current_page,
                "next_action": "pick_candidate",
            }
        if pick_action == "next_page":
            if cache_action != "hdhive_candidates":
                return False, "当前结果不支持翻页，请直接回复编号继续。", {
                    "action": "pick",
                    "ok": False,
                    "message": "next page unsupported",
                }
            items = cache.get("items") or []
            meta = dict(cache.get("meta") or {})
            page_size = int(meta.get("page_size") or 10)
            total_pages = max(1, (len(items) + page_size - 1) // page_size)
            current_page = int(meta.get("page") or 1)
            if current_page >= total_pages:
                return False, "已经是最后一页了，可以直接回复编号继续选择。", {
                    "action": "hdhive_candidates",
                    "ok": False,
                    "message": "already last page",
                }
            next_page = current_page + 1
            final_path = override_path or cache.get("target_path") or self._get_hdhive_default_path()
            meta["page"] = next_page
            self._set_smart_cache(
                cache_key,
                action="hdhive_candidates",
                items=items,
                target_path=final_path,
                keyword=cache.get("keyword") or "",
                meta=meta,
            )
            text = self._format_hdhive_candidate_text(
                cache.get("keyword") or "",
                items,
                final_path,
                page=next_page,
                page_size=page_size,
            )
            return True, text, {
                "action": "hdhive_candidates",
                "ok": True,
                "keyword": cache.get("keyword") or "",
                "path": final_path,
                "page": next_page,
                "total_pages": total_pages,
                "next_action": "pick_candidate",
            }
        if index <= 0:
            return False, "请选择有效序号，例如：选择 1", {
                "action": "pick",
                "ok": False,
                "message": "invalid index",
            }
        items = cache.get("items") or []
        if cache_action == "aro_hdhive":
            if pick_action in {"detail", "next_page"}:
                return False, "当前后端暂不支持详情补充或翻页，请直接回复编号继续。", {
                    "action": "pick",
                    "ok": False,
                    "message": "unsupported action for aro session",
                }
            meta = cache.get("meta") or {}
            session_id = str(meta.get("session_id") or "").strip()
            final_path = override_path or cache.get("target_path") or self._get_hdhive_default_path()
            if not session_id:
                return False, "当前会话缺少 session_id，请重新发起影巢搜索。", {
                    "action": "pick",
                    "ok": False,
                    "message": "session id missing",
                }
            ok, payload, message = self._call_aro_hdhive_session_pick(
                session_id=session_id,
                index=index,
                target_path=final_path,
            )
            result = payload.get("data") or {}
            if not ok:
                return False, message or "资源处理失败", {
                    "action": "aro_hdhive",
                    "ok": False,
                    "message": message or "session pick failed",
                }
            stage = str(result.get("stage") or "").strip()
            if stage == "resource":
                selected_candidate = dict(result.get("selected_candidate") or {})
                resources = [dict(item or {}) for item in (result.get("resources") or [])]
                self._set_smart_cache(
                    cache_key,
                    action="aro_hdhive",
                    items=[],
                    target_path=final_path,
                    keyword=cache.get("keyword") or "",
                    meta={
                        **meta,
                        "session_id": session_id,
                        "stage": "resource",
                        "candidate": selected_candidate,
                    },
                )
                text = str(result.get("text") or "").strip() or self._format_hdhive_search_text(
                    cache.get("keyword") or "",
                    resources,
                    selected_candidate,
                    final_path,
                )
                return True, text, {
                    "action": "hdhive_search",
                    "ok": True,
                    "keyword": cache.get("keyword") or "",
                    "path": final_path,
                    "session_id": session_id,
                    "next_action": "pick",
                }
            selected_resource = dict(result.get("selected_resource") or {})
            route_result = dict(result.get("result") or {})
            text = str(result.get("text") or "").strip() or self._format_aro_route_text(
                selected_resource,
                route_result,
                final_path,
            )
            return True, text, {
                "action": "hdhive_unlock",
                "ok": True,
                "path": final_path,
                "session_id": session_id,
                "result": route_result,
            }
        if index > len(items):
            return False, f"序号超出范围，请输入 1 到 {len(items)} 之间的数字。", {
                "action": "pick",
                "ok": False,
                "message": "index out of range",
            }
        selected = items[index - 1]
        if cache_action == "pansou_search":
            share_url = str(selected.get("url") or "").strip()
            access_code = str(selected.get("password") or "").strip()
            share_kind = self._detect_share_kind(share_url)
            final_path = override_path or (
                self._get_hdhive_default_path()
                if share_kind == "115"
                else self._get_quark_default_path()
                if share_kind == "quark"
                else cache.get("target_path") or ""
            )
            if share_kind == "115":
                ok, payload, message = self._call_hdhive_transfer_115(
                    share_url,
                    access_code,
                    final_path,
                )
                if not ok:
                    return False, self._format_115_error_text(message), {
                        "action": "transfer_115",
                        "ok": False,
                        "message": message or "transfer failed",
                    }
                text = self._format_pansou_pick_text(selected, share_kind, payload, final_path)
                return True, text, {
                    "action": "transfer_115",
                    "ok": True,
                    "path": final_path,
                    "item": {
                        "index": selected.get("index"),
                        "title": selected.get("note"),
                        "source": selected.get("source"),
                        "channel": selected.get("channel"),
                    },
                    "result": self._compact_115_result(payload.get("data") or {}),
                }
            if share_kind == "quark":
                ok, payload, message = self._call_quark_transfer(
                    share_url,
                    access_code,
                    final_path,
                )
                if not ok:
                    return False, f"夸克转存失败：{message or '未知错误'}", {
                        "action": "quark_transfer",
                        "ok": False,
                        "message": message or "transfer failed",
                    }
                text = self._format_pansou_pick_text(selected, share_kind, payload, final_path)
                result = payload.get("data") or {}
                return True, text, {
                    "action": "quark_transfer",
                    "ok": True,
                    "path": final_path,
                    "item": {
                        "index": selected.get("index"),
                        "title": selected.get("note"),
                        "source": selected.get("source"),
                        "channel": selected.get("channel"),
                    },
                    "result": {
                        "target_path": result.get("target_path"),
                        "task_id": result.get("task_id"),
                        "saved_count": result.get("saved_count"),
                    },
                }
            return False, "当前盘搜结果不是 115 或夸克链接，暂不支持直接转存。", {
                "action": "pick",
                "ok": False,
                "message": "unsupported pansou result",
            }
        if cache_action == "hdhive_candidates":
            tmdb_id = selected.get("tmdb_id")
            if not tmdb_id:
                return False, "当前候选影片缺少 TMDB ID，无法继续查询资源。", {
                    "action": "hdhive_candidates",
                    "ok": False,
                    "message": "tmdb_id missing",
                }
            meta = cache.get("meta") or {}
            final_path = override_path or cache.get("target_path") or self._get_hdhive_default_path()
            media_type = str(selected.get("media_type") or meta.get("media_type") or "movie").strip()
            year = str(selected.get("year") or meta.get("year") or "").strip()
            ok, payload, message = self._call_hdhive_search_by_tmdb(tmdb_id, media_type, year=year, limit=20)
            result = payload.get("data") or {}
            items = result.get("data") or []
            if not items:
                candidate_label = self._format_hdhive_candidate_label(selected)
                hint = (
                    f"影巢当前暂无资源：{candidate_label}\n"
                    "可以直接回复其他编号，继续查看别的候选影片。"
                )
                if not ok:
                    reason = message or result.get("message") or "暂无结果"
                    hint = f"影巢搜索失败：{reason}\n{hint}"
                return False, hint, {
                    "action": "hdhive_search",
                    "ok": False,
                    "message": message or result.get("message") or "no results",
                    "candidate": {
                        "index": selected.get("index"),
                        "tmdb_id": tmdb_id,
                        "title": selected.get("title"),
                        "year": selected.get("year"),
                        "media_type": selected.get("media_type"),
                    },
                }
            cached_items = []
            for item in self._collect_hdhive_channel_items(items, "115", 6) + self._collect_hdhive_channel_items(items, "quark", 6):
                cached = dict(item)
                cached["index"] = len(cached_items) + 1
                cached_items.append(cached)
            if not cached_items:
                for item in items[:12]:
                    cached = dict(item)
                    cached["index"] = len(cached_items) + 1
                    cached_items.append(cached)
            self._set_smart_cache(
                cache_key,
                action="hdhive_search",
                items=cached_items,
                target_path=final_path,
                keyword=cache.get("keyword") or "",
                meta={"media_type": media_type, "year": year, "candidate": selected},
            )
            text = self._format_hdhive_search_text(cache.get("keyword") or "", cached_items, selected, final_path)
            return True, text, {
                "action": "hdhive_search",
                "ok": True,
                "keyword": cache.get("keyword") or "",
                "path": final_path,
                "candidate": {
                    "index": selected.get("index"),
                    "tmdb_id": tmdb_id,
                    "title": selected.get("title"),
                    "year": selected.get("year"),
                    "media_type": selected.get("media_type"),
                    "actors": selected.get("actors") or [],
                },
                "next_action": "pick",
            }
        if cache_action != "hdhive_search":
            return False, "当前缓存不支持按编号继续，请先发送影巢搜索或盘搜搜索。", {
                "action": "pick",
                "ok": False,
                "message": "unsupported cache action",
            }
        slug = str(selected.get("slug") or "").strip()
        if not slug:
            return False, "当前资源缺少 slug，无法继续解锁。", {
                "action": "pick",
                "ok": False,
                "message": "slug missing",
            }
        default_path = (
            self._get_quark_default_path()
            if str(selected.get("pan_type") or "").strip().lower() == "quark"
            else self._get_hdhive_default_path()
        )
        final_path = override_path or default_path
        ok, payload, message = self._call_hdhive_unlock(
            slug,
            transfer_115=True,
            target_path=final_path,
        )
        if not ok:
            return False, f"影巢解锁失败：{message or '未知错误'}", {
                "action": "hdhive_unlock",
                "ok": False,
                "message": message or "unlock failed",
            }
        result = payload.get("data") or {}
        unlock_data = result.get("data") or {}
        share_url = str(unlock_data.get("full_url") or unlock_data.get("url") or "").strip()
        access_code = str(unlock_data.get("access_code") or "").strip()
        if self._detect_share_kind(share_url) == "quark":
            quark_ok, quark_payload, quark_message = self._call_quark_transfer(
                share_url,
                access_code,
                final_path,
            )
            quark_result = quark_payload.get("data") or {}
            result["transfer_quark"] = {
                "ok": quark_ok,
                "target_path": quark_result.get("target_path") or final_path,
                "task_id": quark_result.get("task_id"),
                "saved_count": quark_result.get("saved_count"),
                "message": quark_message or quark_result.get("message"),
            }
        text = self._format_smart_pick_text(selected, payload, final_path)
        return True, text, {
            "action": "hdhive_unlock",
            "ok": True,
            "path": final_path,
            "item": {
                "index": selected.get("index"),
                "title": selected.get("title"),
                "year": selected.get("year"),
                "pan_type": selected.get("pan_type"),
                "unlock_points": selected.get("unlock_points"),
            },
            "result": self._compact_unlock_result(payload.get("data") or {}),
        }

    def _execute_media_search(self, keyword: str, cache_key: str) -> str:
        try:
            meta = MetaInfo(keyword)
            mediainfo = MediaChain().recognize_media(meta=meta)
            if not mediainfo:
                return f"未识别到媒体信息：{keyword}"

            season = meta.begin_season if meta.begin_season else mediainfo.season
            results = SearchChain().search_by_id(
                tmdbid=mediainfo.tmdb_id,
                doubanid=mediainfo.douban_id,
                mtype=mediainfo.type,
                season=season,
                cache_local=False,
            ) or []
            if not results:
                return f"已识别 {self._format_media_label(mediainfo, season)}，但暂未搜索到资源。"

            self._set_search_cache(cache_key, keyword, mediainfo, results)
            lines = [
                f"已识别：{self._format_media_label(mediainfo, season)}",
                f"共找到 {len(results)} 条资源，展示前 {min(len(results), 10)} 条：",
            ]
            for idx, context in enumerate(results[:10], start=1):
                torrent = context.torrent_info
                title = str(torrent.title or "").strip()
                size = StringUtils.str_filesize(torrent.size) if torrent.size else "未知"
                seeders = torrent.seeders if torrent.seeders is not None else "?"
                site = torrent.site_name or "未知站点"
                volume = torrent.volume_factor if getattr(torrent, "volume_factor", None) else "未知"
                lines.append(f"{idx}. [{site}] {title}")
                lines.append(f"   大小：{size} | 做种：{seeders} | 促销：{volume}")
            lines.append("下一步：回复“下载资源 序号”即可下载选中项。")
            lines.append("如需长期跟踪，回复“订阅媒体 片名”或“订阅并搜索 片名”。")
            return "\n".join(lines)
        except Exception as exc:
            logger.error(
                f"[FeishuCommandBridge] 搜索资源失败：{keyword} {exc}\n{traceback.format_exc()}"
            )
            return f"搜索资源失败：{keyword}\n错误：{exc}"

    def _execute_pansou_search(self, keyword: str, cache_key: str = "") -> str:
        ok, payload, message = self._call_pansou_search(keyword)
        if not ok:
            return f"盘搜搜索失败：{keyword}\n错误：{message}"

        data = payload.get("data") or {}
        merged = data.get("merged_by_type") or {}

        def normalize_channel_name(channel: str) -> str:
            text = str(channel or "").strip().lower()
            if text == "115" or "115" in text:
                return "115"
            if "quark" in text:
                return "quark"
            return str(channel or "").strip() or "未知"

        def collect_channel_items(channel_name: str, limit: int) -> List[Dict[str, Any]]:
            raw_items = merged.get(channel_name) or []
            if not isinstance(raw_items, list):
                return []
            results: List[Dict[str, Any]] = []
            seen = set()
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url:
                    continue
                note = str(item.get("note") or "未命名资源").strip()
                password = str(item.get("password") or "").strip()
                source = str(item.get("source") or "").strip()
                dt = self._format_pansou_datetime(item.get("datetime"))
                key = (url, note)
                if key in seen:
                    continue
                seen.add(key)
                results.append(
                    {
                        "channel": normalize_channel_name(channel_name),
                        "url": url,
                        "password": password,
                        "note": note,
                        "source": source,
                        "datetime": dt,
                    }
                )
                if len(results) >= limit:
                    break
            return results

        channel_115 = collect_channel_items("115", 6)
        channel_quark = collect_channel_items("quark", 6)
        cached_items: List[Dict[str, Any]] = []
        for item in channel_115:
            cached_items.append({**item, "index": len(cached_items) + 1})
        for item in channel_quark:
            cached_items.append({**item, "index": len(cached_items) + 1})

        if not cached_items:
            return f"盘搜暂无结果：{keyword}"

        total = int(data.get("total") or (len(channel_115) + len(channel_quark)))
        if cache_key and cached_items:
            self._set_smart_cache(
                cache_key,
                action="pansou_search",
                keyword=keyword,
                target_path=self._get_hdhive_default_path(),
                items=cached_items,
            )
        lines = [
            f"盘搜搜索：{keyword}",
            (
                f"共找到 {total} 条结果，当前展示 115 {len(channel_115)} 条"
                f"、夸克 {len(channel_quark)} 条："
            ),
        ]
        for idx, cached in enumerate(cached_items):
            idx = cached["index"]
            channel = cached["channel"]
            note = cached["note"]
            url = cached["url"]
            password = cached["password"]
            source = cached["source"]
            dt = cached.get("datetime") or ""
            if idx == 1:
                lines.append("🟦 115 结果")
            elif channel == "quark" and idx == len(channel_115) + 1:
                lines.append("🟨 夸克结果")
            title_line = f"{idx}. [{channel}] {note}"
            lines.append(title_line)
            detail_parts = []
            if source:
                detail_parts.append(source)
            if dt:
                detail_parts.append(dt)
            if detail_parts:
                lines.append(f"   {' · '.join(detail_parts)}")
            if password:
                lines.append(f"   提取码：{password}")
            lines.append(f"   {url}")
        lines.append("下一步：回复“选择 1”即可直接转存支持的 115 / 夸克结果。")
        if channel_quark:
            start_index = len(channel_115) + 1
            lines.append(f"夸克结果从 {start_index} 开始编号；例如“选择 {start_index}”可直接处理第 1 条夸克结果。")
        next_quark_hint = len(channel_115) + 1 if channel_quark else 1
        lines.append(f"如需改目录，可发“选择 1 path=/目录”或“选择 {next_quark_hint} path=/目录”。")
        return "\n".join(lines)

    def _execute_media_download(self, index: int, cache_key: str) -> str:
        cache = self._get_search_cache(cache_key)
        if not cache:
            return "没有可用的搜索缓存，请先发送：搜索资源 片名"
        results = cache.get("results") or []
        if index < 1 or index > len(results):
            return f"序号超出范围，请输入 1 到 {len(results)} 之间的数字。"
        context = copy.deepcopy(results[index - 1])
        torrent = context.torrent_info
        try:
            download_id = DownloadChain().download_single(
                context=context,
                username="feishucommandbridgelong",
                source="FeishuCommandBridgeLong",
            )
            if not download_id:
                return f"下载提交失败：{torrent.title}"
            return (
                f"已提交下载：{torrent.title}\n"
                f"站点：{torrent.site_name or '未知站点'}\n"
                f"任务ID：{download_id}"
            )
        except Exception as exc:
            logger.error(
                f"[FeishuCommandBridge] 下载资源失败：{torrent.title} {exc}\n{traceback.format_exc()}"
            )
            return f"下载资源失败：{torrent.title}\n错误：{exc}"

    def _execute_media_subscribe(self, keyword: str, immediate_search: bool) -> str:
        meta = MetaInfo(keyword)
        season = meta.begin_season
        try:
            sid, message = SubscribeChain().add(
                title=keyword,
                year=meta.year,
                mtype=meta.type,
                season=season,
                username="feishucommandbridgelong",
                exist_ok=True,
                message=False,
            )
            if not sid:
                return f"订阅失败：{keyword}\n原因：{message}"
            lines = [f"已创建订阅：{keyword}", f"订阅ID：{sid}", f"结果：{message}"]
            if immediate_search:
                Scheduler().start(
                    job_id="subscribe_search",
                    **{"sid": sid, "state": None, "manual": True},
                )
                lines.append("已触发一次订阅搜索。")
            return "\n".join(lines)
        except Exception as exc:
            logger.error(
                f"[FeishuCommandBridge] 订阅媒体失败：{keyword} {exc}\n{traceback.format_exc()}"
            )
            return f"订阅失败：{keyword}\n错误：{exc}"

    def _run_quark_save(
        self,
        arg: str,
        receive_chat_id: str,
        receive_open_id: str,
    ) -> None:
        summary = self._execute_quark_save(arg)
        self._reply_if_needed(
            receive_chat_id=receive_chat_id,
            receive_open_id=receive_open_id,
            text=summary,
        )

    @staticmethod
    def _parse_quark_save_arg(arg: str) -> Tuple[str, str, str]:
        text = str(arg or "").strip()
        url_match = re.search(r"https?://[^\s<>\"']+", text)
        share_url = url_match.group(0).rstrip(".,);]") if url_match else ""
        access_code = ""
        target_path = ""
        remain = text.replace(share_url, " ").strip() if share_url else text
        for token in remain.split():
            item = token.strip()
            if not item:
                continue
            if "=" in item:
                key, value = item.split("=", 1)
                key = key.strip().lower()
                value = value.strip()
                if key in {"pwd", "passcode", "code", "提取码"} and value:
                    access_code = value
                    continue
                if key in {"path", "dir", "目录", "位置"} and value:
                    target_path = value
                    continue
            if item.startswith("/") and not target_path:
                target_path = item
                continue
            if not access_code and len(item) <= 8:
                access_code = item
        return share_url, access_code, FeishuCommandBridgeLong._resolve_pan_path_value(target_path)

    def _execute_quark_save(self, arg: str) -> str:
        share_url, access_code, target_path = self._parse_quark_save_arg(arg)
        if not share_url:
            return (
                "夸克转存失败：未识别到分享链接\n"
                "用法：夸克转存 分享链接 pwd=提取码 path=/保存目录"
            )

        ok, payload, message = self._call_quark_transfer(
            share_url=share_url,
            access_code=access_code,
            target_path=target_path or self._get_quark_default_path(),
        )
        if not ok:
            return f"夸克转存失败：{message or '未知错误'}"

        result = payload.get("data") or {}
        return "\n".join(
            [
                "夸克转存已完成",
                f"目录：{result.get('target_path') or target_path or self._get_quark_default_path() or '-'}",
            ]
        )

    @staticmethod
    def _format_media_label(mediainfo: Any, season: Optional[int] = None) -> str:
        title = getattr(mediainfo, "title", "") or "未知媒体"
        year = getattr(mediainfo, "year", None)
        label = f"{title} ({year})" if year else title
        media_type = getattr(mediainfo, "type", None)
        media_type_name = getattr(media_type, "name", "")
        if media_type_name == "TV" and season:
            return f"{label} 第{season}季"
        return label

    def _extract_text(self, content: Any) -> str:
        if isinstance(content, dict):
            return str(content.get("text") or "").strip()
        if isinstance(content, str):
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                return content.strip()
            return str(payload.get("text") or "").strip()
        return ""

    @staticmethod
    def _sanitize_text(text: str) -> str:
        text = re.sub(r"<at[^>]*>.*?</at>", " ", text or "", flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _split_lines(value: Any) -> List[str]:
        return [line.strip() for line in str(value or "").splitlines() if line.strip()]

    @staticmethod
    def _split_commands(value: Any) -> List[str]:
        raw = str(value or "").replace("\n", ",")
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _mask_secret(value: str) -> str:
        value = str(value or "").strip()
        if not value:
            return ""
        if len(value) <= 8:
            return "*" * len(value)
        return f"{value[:4]}...{value[-4:]}"

    def _reply_if_needed(
        self,
        receive_chat_id: str,
        receive_open_id: str,
        text: str,
    ) -> None:
        if not self._reply_enabled:
            return
        if not self._app_id or not self._app_secret:
            return

        receive_id_type = self._reply_receive_id_type
        receive_id = receive_chat_id if receive_id_type == "chat_id" else receive_open_id
        if not receive_id:
            return

        access_token = self._get_tenant_access_token()
        if not access_token:
            return

        url = (
            "https://open.feishu.cn/open-apis/im/v1/messages"
            f"?receive_id_type={receive_id_type}"
        )
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        logger.info(f"[FeishuCommandBridge] 准备回复飞书：{text}")
        response = RequestUtils(headers=headers).post(url=url, json=payload)
        if response is None:
            logger.error("[FeishuCommandBridge] failed to send reply to Feishu")
            return
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code != 200 or data.get("code") not in (0, None):
            logger.error(
                f"[FeishuCommandBridge] reply failed: "
                f"status={response.status_code} body={data}"
            )

    def _upload_image_to_feishu(self, image_bytes: bytes, file_name: str = "qrcode.png") -> Optional[str]:
        if not image_bytes or not self._app_id or not self._app_secret:
            return None
        access_token = self._get_tenant_access_token()
        if not access_token:
            return None
        headers = {"Authorization": f"Bearer {access_token}"}
        response = RequestUtils(headers=headers).post(
            url="https://open.feishu.cn/open-apis/im/v1/images",
            data={"image_type": "message"},
            files={"image": (file_name, image_bytes, "image/png")},
        )
        if response is None:
            logger.error("[FeishuCommandBridge] 上传飞书图片失败：无响应")
            return None
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code != 200 or data.get("code") not in (0, None):
            logger.error(
                f"[FeishuCommandBridge] 上传飞书图片失败: status={response.status_code} body={data}"
            )
            return None
        return str(((data.get("data") or {}).get("image_key")) or "").strip() or None

    def _reply_image_if_needed(
        self,
        receive_chat_id: str,
        receive_open_id: str,
        image_key: str,
    ) -> None:
        if not image_key or not self._reply_enabled or not self._app_id or not self._app_secret:
            return
        receive_id_type = self._reply_receive_id_type
        receive_id = receive_chat_id if receive_id_type == "chat_id" else receive_open_id
        if not receive_id:
            return
        access_token = self._get_tenant_access_token()
        if not access_token:
            return
        url = f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        payload = {
            "receive_id": receive_id,
            "msg_type": "image",
            "content": json.dumps({"image_key": image_key}, ensure_ascii=False),
        }
        response = RequestUtils(headers=headers).post(url=url, json=payload)
        if response is None:
            logger.error("[FeishuCommandBridge] 发送飞书图片失败：无响应")
            return
        try:
            data = response.json()
        except Exception:
            data = {}
        if response.status_code != 200 or data.get("code") not in (0, None):
            logger.error(
                f"[FeishuCommandBridge] 发送飞书图片失败: status={response.status_code} body={data}"
            )

    def _reply_qrcode_data_url_if_needed(
        self,
        receive_chat_id: str,
        receive_open_id: str,
        data_url: str,
    ) -> None:
        text = str(data_url or "").strip()
        if not text.startswith("data:image/") or ";base64," not in text:
            return
        _, _, payload = text.partition(";base64,")
        try:
            image_bytes = b64decode(payload)
        except Exception as exc:
            logger.error(f"[FeishuCommandBridge] 解码二维码图片失败：{exc}")
            return
        image_key = self._upload_image_to_feishu(image_bytes=image_bytes, file_name="p115-qrcode.png")
        if image_key:
            self._reply_image_if_needed(
                receive_chat_id=receive_chat_id,
                receive_open_id=receive_open_id,
                image_key=image_key,
            )

    def _get_tenant_access_token(self) -> Optional[str]:
        now = time.time()
        with self._token_lock:
            token = self._token_cache.get("token")
            expires_at = float(self._token_cache.get("expires_at") or 0)
            if token and now < expires_at - 60:
                return token

            response = RequestUtils(content_type="application/json").post(
                url="https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal/",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            if response is None:
                logger.error("[FeishuCommandBridge] failed to fetch tenant access token")
                return None
            try:
                data = response.json()
            except Exception as exc:
                logger.error(
                    f"[FeishuCommandBridge] invalid token response from Feishu: {exc}"
                )
                return None

            token = data.get("tenant_access_token")
            expire = int(data.get("expire") or 0)
            if not token:
                logger.error(
                    f"[FeishuCommandBridge] token missing in response: {data}"
                )
                return None
            self._token_cache = {"token": token, "expires_at": now + expire}
            return token
