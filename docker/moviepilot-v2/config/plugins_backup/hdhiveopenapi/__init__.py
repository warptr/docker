import importlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Request

try:
    from app.chain.media import MediaChain
except Exception:
    MediaChain = None

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase

try:
    from app.schemas import NotificationType
except Exception:
    NotificationType = None


class HdhiveOpenApi(_PluginBase):
    plugin_name = "影巢 OpenAPI"
    plugin_desc = "通过 HDHive Open API 完成签到、关键词/TMDB 搜索、资源解锁、115 转存、分享管理与配额查询。"
    plugin_icon = "https://raw.githubusercontent.com/liuyuexi1987/MoviePilot-Plugins/main/icons/hdhive.ico"
    plugin_version = "0.3.0"
    plugin_author = "liuyuexi1987"
    plugin_level = 1
    author_url = "https://github.com/liuyuexi1987"
    plugin_config_prefix = "hdhiveopenapi_"
    plugin_order = 30
    auth_level = 1

    _enabled = False
    _notify = True
    _onlyonce = False
    _cron = "0 8 * * *"
    _api_key = ""
    _base_url = "https://hdhive.com"
    _gambler_mode = False
    _timeout = 30
    _history_days = 30

    _search_media_type = "movie"
    _search_tmdb_id = ""
    _search_once = False

    _unlock_slug = ""
    _unlock_once = False
    _transfer_115_enabled = False
    _transfer_115_path = "/待整理"
    _auto_transfer_115_on_unlock = False
    _transfer_115_once = False

    _share_action = "list"
    _share_slug = ""
    _share_page = 1
    _share_page_size = 10
    _share_payload = ""
    _share_once = False

    _scheduler: Optional[BackgroundScheduler] = None

    _history_key = "checkin_history"
    _account_key = "last_account"
    _quota_key = "last_quota"
    _usage_today_key = "last_usage_today"
    _usage_key = "last_usage"
    _weekly_quota_key = "last_weekly_quota"
    _search_key = "last_resource_search"
    _unlock_key = "last_resource_unlock"
    _transfer_115_key = "last_transfer_115"
    _check_resource_key = "last_check_resource"
    _shares_list_key = "last_shares_list"
    _share_detail_key = "last_share_detail"
    _share_action_key = "last_share_action"
    _ping_key = "last_ping"
    _last_error_key = "last_error"

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _normalize_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _normalize_slug(value: Any) -> str:
        return str(value or "").strip().replace("-", "")

    @staticmethod
    def _normalize_pan_path(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if not text.startswith("/"):
            text = f"/{text}"
        return text.rstrip("/") or "/"

    @staticmethod
    def _media_type_text(value: Any) -> str:
        if value is None:
            return ""
        raw = str(getattr(value, "value", value)).strip().lower()
        mapping = {
            "电影": "movie",
            "movie": "movie",
            "电视剧": "tv",
            "tv": "tv",
        }
        return mapping.get(raw, raw)

    @staticmethod
    def _coerce_bool(value: Any, default: bool = False) -> bool:
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        return default

    def _tz_now(self) -> datetime:
        try:
            return datetime.now(ZoneInfo(settings.TZ))
        except Exception:
            return datetime.now()

    def _build_config(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = {
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": self._onlyonce,
            "cron": self._cron,
            "api_key": self._api_key,
            "base_url": self._base_url,
            "gambler_mode": self._gambler_mode,
            "timeout": self._timeout,
            "history_days": self._history_days,
            "search_media_type": self._search_media_type,
            "search_tmdb_id": self._search_tmdb_id,
            "search_once": self._search_once,
            "unlock_slug": self._unlock_slug,
            "unlock_once": self._unlock_once,
            "transfer_115_enabled": self._transfer_115_enabled,
            "transfer_115_path": self._transfer_115_path,
            "auto_transfer_115_on_unlock": self._auto_transfer_115_on_unlock,
            "transfer_115_once": self._transfer_115_once,
            "share_action": self._share_action,
            "share_slug": self._share_slug,
            "share_page": self._share_page,
            "share_page_size": self._share_page_size,
            "share_payload": self._share_payload,
            "share_once": self._share_once,
        }
        if overrides:
            config.update(overrides)
        return config

    def _save_state(self, key: str, value: Any) -> None:
        try:
            self.save_data(key=key, value=value)
        except Exception as exc:
            logger.warning(f"[HdhiveOpenApi] 保存状态失败 {key}: {exc}")

    def _load_state(self, key: str, default: Any = None) -> Any:
        try:
            value = self.get_data(key)
            return default if value is None else value
        except Exception as exc:
            logger.warning(f"[HdhiveOpenApi] 读取状态失败 {key}: {exc}")
            return default

    def _mask_secret(self, value: str, prefix: int = 4, suffix: int = 4) -> str:
        if not value:
            return ""
        if len(value) <= prefix + suffix:
            return "*" * len(value)
        return f"{value[:prefix]}{'*' * (len(value) - prefix - suffix)}{value[-suffix:]}"

    def _remember_error(self, action: str, message: str, payload: Optional[dict] = None) -> None:
        self._save_state(
            self._last_error_key,
            {
                "action": action,
                "message": message,
                "payload": payload or {},
                "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def _is_115_share_url(self, url: str) -> bool:
        host = urlparse(url).netloc.lower()
        return host == "115.com" or host.endswith(".115.com") or "115cdn.com" in host

    def _ensure_115_share_url(self, url: str, access_code: str = "") -> str:
        clean_url = self._normalize_text(url)
        if not clean_url:
            return ""
        access_code = self._normalize_text(access_code)
        parsed = urlparse(clean_url)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        if access_code and "password" not in query:
            query["password"] = access_code
            clean_url = urlunparse(parsed._replace(query=urlencode(query)))
        return clean_url

    @staticmethod
    def _jsonable(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool, list, dict)):
            return value
        if is_dataclass(value):
            return asdict(value)
        if hasattr(value, "model_dump"):
            try:
                return value.model_dump()
            except Exception:
                pass
        if hasattr(value, "__dict__"):
            return {k: v for k, v in vars(value).items() if not k.startswith("_")}
        return str(value)

    def _get_p115_share_helper(self) -> Tuple[Optional[Any], Optional[str]]:
        try:
            service_module = importlib.import_module("app.plugins.p115strmhelper.service")
        except Exception as exc:
            return None, f"P115StrmHelper 未安装或无法导入: {exc}"

        servicer = getattr(service_module, "servicer", None)
        if not servicer:
            return None, "P115StrmHelper 未初始化"
        if not getattr(servicer, "client", None):
            return None, "P115StrmHelper 未登录 115 或客户端不可用"
        helper = getattr(servicer, "sharetransferhelper", None)
        if not helper:
            return None, "P115StrmHelper 分享转存模块不可用"
        return helper, None

    def _base_headers(self) -> Dict[str, str]:
        return {
            "X-API-Key": self._api_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": getattr(settings, "USER_AGENT", "MoviePilot"),
        }

    def _api_url(self, path: str) -> str:
        return f"{self._base_url.rstrip('/')}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any], str, int]:
        if not self._api_key:
            return False, {}, "未配置影巢 API Key", 400

        try:
            response = requests.request(
                method=method.upper(),
                url=self._api_url(path),
                headers=self._base_headers(),
                params=params,
                json=payload if payload is not None else None,
                timeout=timeout or self._timeout,
                proxies=getattr(settings, "PROXY", None),
            )
        except Exception as exc:
            return False, {}, f"请求异常: {exc}", 0

        try:
            result = response.json()
        except Exception:
            result = {
                "success": False,
                "message": response.text[:300] if response.text else f"HTTP {response.status_code}",
                "description": "接口未返回有效 JSON",
            }

        if response.ok and isinstance(result, dict) and result.get("success", True):
            return True, result, "", response.status_code

        message = ""
        if isinstance(result, dict):
            message = (
                result.get("description")
                or result.get("message")
                or result.get("code")
                or f"HTTP {response.status_code}"
            )
        if not message:
            message = f"HTTP {response.status_code}"
        return False, result if isinstance(result, dict) else {}, message, response.status_code

    def _notify_message(self, title: str, text: str) -> None:
        if not self._notify:
            return
        if not hasattr(self, "post_message"):
            return
        try:
            if NotificationType is not None:
                self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
            else:
                self.post_message(title=title, text=text)
        except Exception as exc:
            logger.warning(f"[HdhiveOpenApi] 发送通知失败: {exc}")

    def _append_history(self, record: Dict[str, Any]) -> None:
        history = self._load_state(self._history_key, default=[]) or []
        history.append(record)
        now = self._tz_now()
        valid_history: List[Dict[str, Any]] = []
        for item in history:
            date_text = str(item.get("time") or item.get("date") or "").strip()
            if not date_text:
                continue
            try:
                item_dt = datetime.strptime(date_text, "%Y-%m-%d %H:%M:%S")
            except Exception:
                valid_history.append(item)
                continue
            if (now.replace(tzinfo=None) - item_dt).days < self._history_days:
                valid_history.append(item)
        self._save_state(self._history_key, valid_history[-100:])

    def _refresh_snapshots(self, silent: bool = False) -> None:
        ok, data, message = self.ping(remember=True)
        if not ok and not silent:
            self._remember_error("ping", message, data)
            return
        self.fetch_me(remember=True)
        self.fetch_quota(remember=True)
        self.fetch_usage_today(remember=True)
        self.fetch_weekly_free_quota(remember=True)

    def init_plugin(self, config: dict = None):
        self.stop_service()

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cron = self._normalize_text(config.get("cron")) or "0 8 * * *"
        self._api_key = self._normalize_text(config.get("api_key"))
        self._base_url = (self._normalize_text(config.get("base_url")) or "https://hdhive.com").rstrip("/")
        self._gambler_mode = bool(config.get("gambler_mode"))
        self._timeout = self._safe_int(config.get("timeout"), 30)
        self._history_days = self._safe_int(config.get("history_days"), 30)

        self._search_media_type = self._normalize_text(config.get("search_media_type")) or "movie"
        if self._search_media_type not in {"movie", "tv"}:
            self._search_media_type = "movie"
        self._search_tmdb_id = self._normalize_text(config.get("search_tmdb_id"))
        self._search_once = bool(config.get("search_once"))

        self._unlock_slug = self._normalize_slug(config.get("unlock_slug"))
        self._unlock_once = bool(config.get("unlock_once"))
        self._transfer_115_enabled = bool(config.get("transfer_115_enabled"))
        self._transfer_115_path = self._normalize_pan_path(config.get("transfer_115_path")) or "/待整理"
        self._auto_transfer_115_on_unlock = bool(config.get("auto_transfer_115_on_unlock"))
        self._transfer_115_once = bool(config.get("transfer_115_once"))

        self._share_action = self._normalize_text(config.get("share_action")) or "list"
        if self._share_action not in {"list", "detail", "create", "update", "delete"}:
            self._share_action = "list"
        self._share_slug = self._normalize_slug(config.get("share_slug"))
        self._share_page = max(1, self._safe_int(config.get("share_page"), 1))
        self._share_page_size = min(100, max(1, self._safe_int(config.get("share_page_size"), 10)))
        self._share_payload = str(config.get("share_payload") or "").strip()
        self._share_once = bool(config.get("share_once"))

        if self._enabled and self._api_key:
            self._refresh_snapshots(silent=True)

        scheduled_jobs: List[Tuple[str, Any]] = []
        reset_config: Dict[str, Any] = {}
        if self._onlyonce:
            scheduled_jobs.append(("影巢 OpenAPI 立即签到", self._run_checkin_once))
            reset_config["onlyonce"] = False
            self._onlyonce = False
        if self._search_once:
            scheduled_jobs.append(("影巢 OpenAPI 资源查询", self._run_search_once))
            reset_config["search_once"] = False
            self._search_once = False
        if self._unlock_once:
            scheduled_jobs.append(("影巢 OpenAPI 资源解锁", self._run_unlock_once))
            reset_config["unlock_once"] = False
            self._unlock_once = False
        if self._transfer_115_once:
            scheduled_jobs.append(("影巢 OpenAPI 转存到115", self._run_transfer_115_once))
            reset_config["transfer_115_once"] = False
            self._transfer_115_once = False
        if self._share_once:
            scheduled_jobs.append(("影巢 OpenAPI 分享操作", self._run_share_once))
            reset_config["share_once"] = False
            self._share_once = False

        if scheduled_jobs:
            self._scheduler = BackgroundScheduler(timezone=getattr(settings, "TZ", "Asia/Shanghai"))
            base_time = self._tz_now()
            for index, (job_name, func) in enumerate(scheduled_jobs):
                self._scheduler.add_job(
                    func=func,
                    trigger="date",
                    run_date=base_time + timedelta(seconds=3 + index),
                    name=job_name,
                )
            self._scheduler.start()

        if reset_config:
            self.update_config(self._build_config(reset_config))

    def get_state(self) -> bool:
        return self._enabled and bool(self._api_key)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._api_key or not self._cron:
            return []
        return [
            {
                "id": "hdhiveopenapi_checkin",
                "name": "影巢 OpenAPI 每日签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self._scheduled_checkin,
                "kwargs": {},
            }
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
        except Exception as exc:
            logger.warning(f"[HdhiveOpenApi] 停止调度器失败: {exc}")
        finally:
            self._scheduler = None

    def ping(self, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        ok, payload, message, status_code = self._request("GET", "/api/open/ping")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._ping_key, result)
            if not ok:
                self._remember_error("ping", message, payload)
        return ok, result, message

    def fetch_me(self, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        ok, payload, message, status_code = self._request("GET", "/api/open/me")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._account_key, result)
            if not ok:
                self._remember_error("me", message, payload)
        return ok, result, message

    def fetch_quota(self, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        ok, payload, message, status_code = self._request("GET", "/api/open/quota")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._quota_key, result)
            if not ok:
                self._remember_error("quota", message, payload)
        return ok, result, message

    def fetch_usage(self, start_date: str = "", end_date: str = "", remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        params: Dict[str, Any] = {}
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        ok, payload, message, status_code = self._request("GET", "/api/open/usage", params=params or None)
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "query": params,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._usage_key, result)
            if not ok:
                self._remember_error("usage", message, payload)
        return ok, result, message

    def fetch_usage_today(self, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        ok, payload, message, status_code = self._request("GET", "/api/open/usage/today")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._usage_today_key, result)
            if not ok:
                self._remember_error("usage_today", message, payload)
        return ok, result, message

    def fetch_weekly_free_quota(self, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        ok, payload, message, status_code = self._request("GET", "/api/open/vip/weekly-free-quota")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._weekly_quota_key, result)
            if not ok:
                self._remember_error("weekly_free_quota", message, payload)
        return ok, result, message

    def perform_checkin(
        self,
        *,
        is_gambler: Optional[bool] = None,
        remember: bool = True,
        trigger: str = "手动",
    ) -> Tuple[bool, Dict[str, Any], str]:
        gambler_mode = self._gambler_mode if is_gambler is None else bool(is_gambler)
        payload = {"is_gambler": gambler_mode} if gambler_mode else None
        ok, result_payload, message, status_code = self._request("POST", "/api/open/checkin", payload=payload)
        data = result_payload.get("data") if isinstance(result_payload, dict) else {}
        checked_in = bool((data or {}).get("checked_in")) if ok else False
        status_text = "签到成功" if checked_in else "今日已签到"
        if not ok:
            status_text = "签到失败"
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "trigger": trigger,
            "is_gambler": gambler_mode,
            "status": status_text,
            "message": (data or {}).get("message") or result_payload.get("message") or message,
            "data": data or {},
        }
        if remember:
            self._append_history(result)
            if ok:
                self.fetch_me(remember=True)
                self.fetch_weekly_free_quota(remember=True)
            else:
                self._remember_error("checkin", message, result_payload)

        if ok:
            title = "【影巢 OpenAPI 签到】"
            text = (
                f"时间：{result['time']}\n"
                f"方式：{trigger}\n"
                f"模式：{'赌狗签到' if gambler_mode else '普通签到'}\n"
                f"结果：{result['status']}\n"
                f"详情：{result['message']}"
            )
            self._notify_message(title, text)
        return ok, result, message

    def search_resources(self, media_type: str, tmdb_id: str, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        media_type = (media_type or "").strip().lower()
        tmdb_id = self._normalize_text(tmdb_id)
        if media_type not in {"movie", "tv"}:
            return False, {"message": "媒体类型必须是 movie 或 tv", "query": {"media_type": media_type, "tmdb_id": tmdb_id}}, "媒体类型必须是 movie 或 tv"
        if not tmdb_id:
            return False, {"message": "TMDB ID 不能为空", "query": {"media_type": media_type, "tmdb_id": tmdb_id}}, "TMDB ID 不能为空"

        ok, payload, message, status_code = self._request("GET", f"/api/open/resources/{media_type}/{tmdb_id}")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "query": {"media_type": media_type, "tmdb_id": tmdb_id},
            "data": payload.get("data") if isinstance(payload, dict) else [],
            "meta": payload.get("meta") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._search_key, result)
            if not ok:
                self._remember_error("resources_search", message, payload)
        return ok, result, message

    def _resource_sort_key(self, item: Dict[str, Any]) -> Tuple[int, int, int, int, str]:
        pan = str(item.get("pan_type") or "").lower()
        points = item.get("unlock_points")
        try:
            points_value = int(points) if points is not None and str(points) != "" else 0
        except Exception:
            points_value = 9999
        validate = str(item.get("validate_status") or "").lower()
        resolutions = [str(v).upper() for v in (item.get("video_resolution") or [])]
        sources = [str(v) for v in (item.get("source") or [])]
        pan_rank = 0 if pan == "115" else 1
        points_rank = 0 if points_value <= 0 else 1
        validate_rank = 0 if validate in {"valid", ""} else 1
        resolution_rank = 0 if "4K" in resolutions else 1 if "1080P" in resolutions else 2
        source_rank = 0 if "蓝光原盘/REMUX" in sources else 1 if "WEB-DL/WEBRip" in sources else 2
        return (pan_rank, points_rank, validate_rank, resolution_rank + source_rank, str(item.get("title") or ""))

    async def search_resources_by_keyword(
        self,
        keyword: str,
        media_type: str = "movie",
        year: str = "",
        candidate_limit: int = 5,
        result_limit: int = 10,
        remember: bool = True,
    ) -> Tuple[bool, Dict[str, Any], str]:
        keyword = self._normalize_text(keyword)
        media_type = self._normalize_text(media_type).lower() or "movie"
        year = self._normalize_text(year)
        candidate_limit = min(10, max(1, self._safe_int(candidate_limit, 5)))
        result_limit = min(50, max(1, self._safe_int(result_limit, 10)))

        if not keyword:
            return False, {"message": "keyword 不能为空", "query": {"keyword": "", "media_type": media_type}}, "keyword 不能为空"
        if media_type not in {"movie", "tv"}:
            return False, {"message": "媒体类型必须是 movie 或 tv", "query": {"keyword": keyword, "media_type": media_type}}, "媒体类型必须是 movie 或 tv"
        if MediaChain is None:
            return False, {"message": "MoviePilot MediaChain 不可用", "query": {"keyword": keyword, "media_type": media_type}}, "MoviePilot MediaChain 不可用"

        try:
            _, medias = await MediaChain().async_search(title=keyword)
        except Exception as exc:
            return False, {"message": f"TMDB 解析失败: {exc}", "query": {"keyword": keyword, "media_type": media_type}}, f"TMDB 解析失败: {exc}"

        candidates: List[Dict[str, Any]] = []
        for media in medias or []:
            item_type = self._media_type_text(getattr(media, "type", ""))
            item_year = self._normalize_text(getattr(media, "year", ""))
            if media_type and item_type and item_type != media_type:
                continue
            if year and item_year and item_year != year:
                continue
            tmdb_id = getattr(media, "tmdb_id", None)
            if not tmdb_id:
                continue
            candidates.append(
                {
                    "title": getattr(media, "title", "") or getattr(media, "en_title", "") or "",
                    "year": item_year,
                    "media_type": item_type or media_type,
                    "tmdb_id": tmdb_id,
                    "poster_path": getattr(media, "poster_path", "") or "",
                }
            )
            if len(candidates) >= candidate_limit:
                break

        if not candidates:
            result = {
                "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
                "ok": False,
                "status_code": 404,
                "message": "未找到可用于影巢搜索的 TMDB 候选",
                "query": {"keyword": keyword, "media_type": media_type, "year": year},
                "candidates": [],
                "data": [],
                "meta": {"total": 0},
            }
            if remember:
                self._save_state(self._search_key, result)
            return False, result, result["message"]

        merged_items: List[Dict[str, Any]] = []
        seen_slugs: set[str] = set()
        last_status = 200

        for candidate in candidates:
            ok, payload, message = self.search_resources(
                media_type=candidate["media_type"] or media_type,
                tmdb_id=str(candidate["tmdb_id"]),
                remember=False,
            )
            last_status = payload.get("status_code", last_status) if isinstance(payload, dict) else last_status
            if not ok:
                continue
            for resource in payload.get("data") or []:
                slug = self._normalize_slug(resource.get("slug"))
                if not slug or slug in seen_slugs:
                    continue
                seen_slugs.add(slug)
                annotated = dict(resource)
                annotated["matched_tmdb_id"] = candidate["tmdb_id"]
                annotated["matched_title"] = candidate["title"]
                annotated["matched_year"] = candidate["year"]
                merged_items.append(annotated)

        merged_items.sort(key=self._resource_sort_key)
        merged_items = merged_items[:result_limit]

        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": bool(merged_items),
            "status_code": last_status,
            "message": "success" if merged_items else "已解析 TMDB，但影巢暂无匹配资源",
            "query": {"keyword": keyword, "media_type": media_type, "year": year},
            "candidates": candidates,
            "data": merged_items,
            "meta": {"total": len(merged_items), "candidate_count": len(candidates)},
        }
        if remember:
            self._save_state(self._search_key, result)
            if not merged_items:
                self._remember_error("resources_search_keyword", result["message"], result)
        return bool(merged_items), result, result["message"]

    def unlock_resource(
        self,
        slug: str,
        remember: bool = True,
        *,
        transfer_115: bool = False,
        transfer_path: str = "",
    ) -> Tuple[bool, Dict[str, Any], str]:
        slug = self._normalize_slug(slug)
        if not slug:
            return False, {"message": "slug 不能为空", "slug": ""}, "slug 不能为空"

        ok, payload, message, status_code = self._request(
            "POST",
            "/api/open/resources/unlock",
            payload={"slug": slug},
        )
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "slug": slug,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        should_transfer = bool(ok and transfer_115)
        if should_transfer:
            unlock_data = result.get("data") or {}
            transfer_ok, transfer_result, transfer_message = self.transfer_115_share(
                url=unlock_data.get("full_url") or unlock_data.get("url") or "",
                access_code=unlock_data.get("access_code") or "",
                path=transfer_path or self._transfer_115_path,
                remember=True,
                trigger="解锁后自动转存",
            )
            result["transfer_115"] = transfer_result
            if not transfer_ok:
                result["transfer_115_message"] = transfer_message
        if remember:
            self._save_state(self._unlock_key, result)
            if ok:
                self.fetch_me(remember=True)
            else:
                self._remember_error("resources_unlock", message, payload)
        return ok, result, message

    def transfer_115_share(
        self,
        *,
        url: str = "",
        access_code: str = "",
        path: str = "",
        remember: bool = True,
        trigger: str = "手动转存",
    ) -> Tuple[bool, Dict[str, Any], str]:
        transfer_path = self._normalize_pan_path(path) or self._transfer_115_path or "/待整理"
        unlock_snapshot = self._load_state(self._unlock_key, {}) or {}
        unlock_data = unlock_snapshot.get("data") or {}
        share_url = self._ensure_115_share_url(
            url or unlock_data.get("full_url") or unlock_data.get("url") or "",
            access_code or unlock_data.get("access_code") or "",
        )
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": False,
            "trigger": trigger,
            "path": transfer_path,
            "url": share_url,
            "message": "",
            "data": {},
        }
        if not share_url:
            result["message"] = "没有可用于 115 转存的解锁链接"
            if remember:
                self._save_state(self._transfer_115_key, result)
                self._remember_error("transfer_115", result["message"], result)
            return False, result, result["message"]
        if not self._is_115_share_url(share_url):
            result["message"] = "当前解锁结果不是 115 分享链接，无法直接转存到 115"
            if remember:
                self._save_state(self._transfer_115_key, result)
            return False, result, result["message"]

        helper, helper_error = self._get_p115_share_helper()
        if helper_error or not helper:
            result["message"] = helper_error or "P115StrmHelper 不可用"
            if remember:
                self._save_state(self._transfer_115_key, result)
                self._remember_error("transfer_115", result["message"], result)
            return False, result, result["message"]

        try:
            transfer_result = helper.add_share_115(
                share_url,
                notify=False,
                pan_path=transfer_path,
            )
        except Exception as exc:
            result["message"] = f"调用 P115StrmHelper 转存失败: {exc}"
            if remember:
                self._save_state(self._transfer_115_key, result)
                self._remember_error("transfer_115", result["message"], result)
            return False, result, result["message"]

        if not transfer_result or not transfer_result[0]:
            error_message = ""
            if isinstance(transfer_result, tuple):
                if len(transfer_result) > 2:
                    error_message = self._normalize_text(transfer_result[2])
                elif len(transfer_result) > 1:
                    error_message = self._normalize_text(transfer_result[1])
            result["message"] = error_message or "115 转存失败"
            result["data"] = {"raw": self._jsonable(transfer_result)}
            if remember:
                self._save_state(self._transfer_115_key, result)
                self._remember_error("transfer_115", result["message"], result)
            return False, result, result["message"]

        media_info = transfer_result[1] if len(transfer_result) > 1 else None
        save_parent = transfer_result[2] if len(transfer_result) > 2 else transfer_path
        parent_id = transfer_result[3] if len(transfer_result) > 3 else None
        result.update(
            {
                "ok": True,
                "message": "115 转存成功",
                "data": {
                    "media_info": self._jsonable(media_info),
                    "save_parent": save_parent,
                    "parent_id": parent_id,
                },
            }
        )
        if remember:
            self._save_state(self._transfer_115_key, result)
        return True, result, result["message"]

    def check_resource(self, url: str, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        clean_url = self._normalize_text(url)
        if not clean_url:
            return False, {"message": "url 不能为空", "url": ""}, "url 不能为空"

        ok, payload, message, status_code = self._request(
            "POST",
            "/api/open/check/resource",
            payload={"url": clean_url},
        )
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "url": clean_url,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._check_resource_key, result)
            if not ok:
                self._remember_error("check_resource", message, payload)
        return ok, result, message

    def list_shares(self, page: int = 1, page_size: int = 20, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        page = max(1, self._safe_int(page, 1))
        page_size = min(100, max(1, self._safe_int(page_size, 20)))
        ok, payload, message, status_code = self._request(
            "GET",
            "/api/open/shares",
            params={"page": page, "page_size": page_size},
        )
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "query": {"page": page, "page_size": page_size},
            "data": payload.get("data") if isinstance(payload, dict) else [],
            "meta": payload.get("meta") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._shares_list_key, result)
            if not ok:
                self._remember_error("shares_list", message, payload)
        return ok, result, message

    def get_share_detail(self, slug: str, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        slug = self._normalize_slug(slug)
        if not slug:
            return False, {"message": "slug 不能为空", "slug": ""}, "slug 不能为空"

        ok, payload, message, status_code = self._request("GET", f"/api/open/shares/{slug}")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "message": payload.get("message") if ok else message,
            "slug": slug,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._share_detail_key, result)
            if not ok:
                self._remember_error("shares_detail", message, payload)
        return ok, result, message

    def create_share(self, share_payload: Dict[str, Any], remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        ok, payload, message, status_code = self._request("POST", "/api/open/shares", payload=share_payload)
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "action": "create",
            "message": payload.get("message") if ok else message,
            "payload": share_payload,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._share_action_key, result)
            if ok:
                self.fetch_me(remember=True)
            else:
                self._remember_error("shares_create", message, payload)
        return ok, result, message

    def update_share(self, slug: str, share_payload: Dict[str, Any], remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        slug = self._normalize_slug(slug)
        if not slug:
            return False, {"message": "slug 不能为空", "slug": ""}, "slug 不能为空"
        ok, payload, message, status_code = self._request("PATCH", f"/api/open/shares/{slug}", payload=share_payload)
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "action": "update",
            "slug": slug,
            "message": payload.get("message") if ok else message,
            "payload": share_payload,
            "data": payload.get("data") if isinstance(payload, dict) else {},
        }
        if remember:
            self._save_state(self._share_action_key, result)
            if not ok:
                self._remember_error("shares_update", message, payload)
        return ok, result, message

    def delete_share(self, slug: str, remember: bool = True) -> Tuple[bool, Dict[str, Any], str]:
        slug = self._normalize_slug(slug)
        if not slug:
            return False, {"message": "slug 不能为空", "slug": ""}, "slug 不能为空"
        ok, payload, message, status_code = self._request("DELETE", f"/api/open/shares/{slug}")
        result = {
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
            "ok": ok,
            "status_code": status_code,
            "action": "delete",
            "slug": slug,
            "message": payload.get("message") if ok else message,
            "data": payload.get("data") if isinstance(payload, dict) else None,
        }
        if remember:
            self._save_state(self._share_action_key, result)
            if ok:
                self.fetch_me(remember=True)
            else:
                self._remember_error("shares_delete", message, payload)
        return ok, result, message

    def _scheduled_checkin(self) -> None:
        self.perform_checkin(trigger="定时任务", remember=True)

    def _run_checkin_once(self) -> None:
        self.perform_checkin(trigger="配置页立即运行", remember=True)

    def _run_search_once(self) -> None:
        ok, result, message = self.search_resources(self._search_media_type, self._search_tmdb_id, remember=True)
        if ok:
            logger.info(
                "[HdhiveOpenApi] 一次性资源查询完成: %s/%s, 返回 %s 条",
                self._search_media_type,
                self._search_tmdb_id,
                len(result.get("data") or []),
            )
        else:
            logger.warning("[HdhiveOpenApi] 一次性资源查询失败: %s", message)

    def _run_unlock_once(self) -> None:
        ok, _, message = self.unlock_resource(
            self._unlock_slug,
            remember=True,
            transfer_115=self._auto_transfer_115_on_unlock,
            transfer_path=self._transfer_115_path,
        )
        if ok:
            logger.info("[HdhiveOpenApi] 一次性资源解锁完成: %s", self._unlock_slug)
        else:
            logger.warning("[HdhiveOpenApi] 一次性资源解锁失败: %s", message)

    def _run_transfer_115_once(self) -> None:
        ok, _, message = self.transfer_115_share(
            path=self._transfer_115_path,
            remember=True,
            trigger="配置页立即转存",
        )
        if ok:
            logger.info("[HdhiveOpenApi] 一次性 115 转存完成: %s", self._transfer_115_path)
        else:
            logger.warning("[HdhiveOpenApi] 一次性 115 转存失败: %s", message)

    def _parse_share_payload(self) -> Tuple[bool, Dict[str, Any], str]:
        if not self._share_payload.strip():
            return True, {}, ""
        try:
            payload = json.loads(self._share_payload)
        except Exception as exc:
            return False, {}, f"分享请求 JSON 解析失败: {exc}"
        if not isinstance(payload, dict):
            return False, {}, "分享请求 JSON 必须是对象"
        return True, payload, ""

    def _run_share_once(self) -> None:
        ok, payload, message = self._parse_share_payload()
        if not ok:
            self._remember_error("share_payload", message, {})
            logger.warning("[HdhiveOpenApi] 一次性分享操作失败: %s", message)
            return

        action = self._share_action
        if action == "list":
            self.list_shares(page=self._share_page, page_size=self._share_page_size, remember=True)
            return
        if action == "detail":
            self.get_share_detail(self._share_slug, remember=True)
            return
        if action == "create":
            self.create_share(payload, remember=True)
            return
        if action == "update":
            self.update_share(self._share_slug, payload, remember=True)
            return
        if action == "delete":
            self.delete_share(self._share_slug, remember=True)
            return

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
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "签到发送通知"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "gambler_mode", "label": "默认赌狗签到"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "onlyonce", "label": "立即签到一次"}}
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
                                            "model": "api_key",
                                            "label": "影巢 Open API Key",
                                            "placeholder": "请输入影巢 API Key",
                                            "type": "password",
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
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "base_url",
                                            "label": "影巢站点地址",
                                            "placeholder": "https://hdhive.com",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VCronField",
                                        "props": {
                                            "model": "cron",
                                            "label": "每日签到周期",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "timeout",
                                            "label": "接口超时（秒）",
                                            "type": "number",
                                            "placeholder": "30",
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "search_media_type",
                                            "label": "资源查询类型",
                                            "items": [
                                                {"title": "电影 movie", "value": "movie"},
                                                {"title": "剧集 tv", "value": "tv"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 5},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "search_tmdb_id",
                                            "label": "查询 TMDB ID（可留空）",
                                            "placeholder": "例如 550；留空时可直接用 API keyword 搜索",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "search_once", "label": "立即查询资源"}}
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "unlock_slug",
                                            "label": "解锁资源 slug",
                                            "placeholder": "请输入 32 位资源 slug",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "unlock_once", "label": "立即解锁资源"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "transfer_115_enabled", "label": "启用 115 转存"}}
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "transfer_115_path",
                                            "label": "115 固定目录",
                                            "placeholder": "/待整理/影巢",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "auto_transfer_115_on_unlock", "label": "解锁后自动转存"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "transfer_115_once", "label": "转存最近一次 115 链接"}}
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VSelect",
                                        "props": {
                                            "model": "share_action",
                                            "label": "分享操作",
                                            "items": [
                                                {"title": "list 列表", "value": "list"},
                                                {"title": "detail 详情", "value": "detail"},
                                                {"title": "create 创建", "value": "create"},
                                                {"title": "update 更新", "value": "update"},
                                                {"title": "delete 删除", "value": "delete"},
                                            ],
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 3},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "share_slug",
                                            "label": "分享 slug",
                                            "placeholder": "detail/update/delete 时填写",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "share_page",
                                            "label": "列表页码",
                                            "type": "number",
                                            "placeholder": "1",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "share_page_size",
                                            "label": "每页条数",
                                            "type": "number",
                                            "placeholder": "10",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 2},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "share_once", "label": "立即执行分享操作"}}
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
                                            "model": "share_payload",
                                            "label": "分享请求 JSON",
                                            "rows": 8,
                                            "placeholder": "{\"tmdb_id\":\"550\",\"media_type\":\"movie\",\"title\":\"Fight Club 4K REMUX\",\"url\":\"https://pan.example.com/s/abc123\",\"access_code\":\"x1y2\",\"unlock_points\":10}",
                                            "hint": "create/update 时填写 JSON。list/detail/delete 可留空。",
                                            "persistent-hint": True,
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
                                        "component": "VAlert",
                                        "props": {
                                            "type": "info",
                                            "variant": "tonal",
                                            "text": (
                                                "核心能力已覆盖：用户信息、每日签到、资源查询与解锁、分享管理、用量与配额。\\n"
                                                "新增：支持把解锁出来的 115 分享链接直接转存到固定目录。\\n"
                                                "注意：只有解锁结果本身是 115 分享链接时才能直接转存，天翼/夸克/阿里等链接不会自动塞进 115。\\n"
                                                "页面内的一次性操作适合联调；真正对外集成时，建议直接调用插件 API。\\n"
                                                "插件 API 示例：\\n"
                                                "GET /api/v1/plugin/HdhiveOpenApi/resources/search?type=movie&tmdb_id=550\\n"
                                                "GET /api/v1/plugin/HdhiveOpenApi/resources/search?type=movie&keyword=超级马里奥兄弟大电影\\n"
                                                "POST /api/v1/plugin/HdhiveOpenApi/resources/unlock\\n"
                                                "POST /api/v1/plugin/HdhiveOpenApi/transfer/115\\n"
                                                "GET /api/v1/plugin/HdhiveOpenApi/shares\\n"
                                                "POST /api/v1/plugin/HdhiveOpenApi/shares/create"
                                            ),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], self._build_config()

    def _build_key_value_card(self, title: str, rows: List[Tuple[str, Any]], md: int = 6) -> dict:
        return {
            "component": "VCol",
            "props": {"cols": 12, "md": md},
            "content": [
                {
                    "component": "VCard",
                    "props": {"flat": True, "border": True},
                    "content": [
                        {"component": "VCardTitle", "text": title},
                        {
                            "component": "VCardText",
                            "content": [
                                {
                                    "component": "div",
                                    "props": {"class": "text-body-2 py-1"},
                                    "text": f"{label}：{value if value not in (None, '') else '—'}",
                                }
                                for label, value in rows
                            ],
                        },
                    ],
                }
            ],
        }

    def _build_resource_rows(self, items: List[Dict[str, Any]]) -> List[dict]:
        rows: List[dict] = []
        for item in items[:20]:
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": item.get("slug", "")},
                        {"component": "td", "text": item.get("title", "—")},
                        {"component": "td", "text": item.get("share_size", "—")},
                        {"component": "td", "text": "/".join(item.get("source") or []) or "—"},
                        {"component": "td", "text": "/".join(item.get("video_resolution") or []) or "—"},
                        {"component": "td", "text": str(item.get("unlock_points", "0"))},
                        {"component": "td", "text": "是" if item.get("is_unlocked") else "否"},
                        {"component": "td", "text": "是" if item.get("is_official") else "否"},
                    ],
                }
            )
        return rows

    def _build_share_rows(self, items: List[Dict[str, Any]]) -> List[dict]:
        rows: List[dict] = []
        for item in items[:20]:
            rows.append(
                {
                    "component": "tr",
                    "content": [
                        {"component": "td", "text": item.get("slug", "")},
                        {"component": "td", "text": item.get("title", "—")},
                        {"component": "td", "text": item.get("share_size", "—")},
                        {"component": "td", "text": str(item.get("unlock_points", "0"))},
                        {"component": "td", "text": str(item.get("unlocked_users_count", "0"))},
                        {"component": "td", "text": item.get("created_at", "—")},
                    ],
                }
            )
        return rows

    def get_page(self) -> List[dict]:
        ping = self._load_state(self._ping_key, {}) or {}
        account = self._load_state(self._account_key, {}) or {}
        quota = self._load_state(self._quota_key, {}) or {}
        usage_today = self._load_state(self._usage_today_key, {}) or {}
        weekly_quota = self._load_state(self._weekly_quota_key, {}) or {}
        search_result = self._load_state(self._search_key, {}) or {}
        unlock_result = self._load_state(self._unlock_key, {}) or {}
        transfer_115_result = self._load_state(self._transfer_115_key, {}) or {}
        shares_list = self._load_state(self._shares_list_key, {}) or {}
        share_detail = self._load_state(self._share_detail_key, {}) or {}
        share_action = self._load_state(self._share_action_key, {}) or {}
        last_error = self._load_state(self._last_error_key, {}) or {}
        history = list(reversed(self._load_state(self._history_key, []) or []))[:20]

        user = (account.get("data") or {}) if isinstance(account, dict) else {}
        user_meta = (user.get("user_meta") or {}) if isinstance(user, dict) else {}
        quota_data = quota.get("data") or {}
        usage_today_data = usage_today.get("data") or {}
        weekly_data = weekly_quota.get("data") or {}
        resource_items = search_result.get("data") or []
        share_items = shares_list.get("data") or []
        unlock_data = unlock_result.get("data") or {}
        transfer_115_data = transfer_115_result.get("data") or {}
        share_detail_data = share_detail.get("data") or {}
        share_action_data = share_action.get("data") or {}

        history_rows = [
            {
                "component": "tr",
                "content": [
                    {"component": "td", "text": item.get("time", "")},
                    {"component": "td", "text": item.get("trigger", "—")},
                    {"component": "td", "text": "赌狗" if item.get("is_gambler") else "普通"},
                    {"component": "td", "text": item.get("status", "—")},
                    {"component": "td", "text": item.get("message", "—")},
                ],
            }
            for item in history
        ]

        page_content: List[dict] = [
            {
                "component": "VContainer",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            self._build_key_value_card(
                                "连接状态",
                                [
                                    ("启用", "是" if self._enabled else "否"),
                                    ("API Key", self._mask_secret(self._api_key) or "未填写"),
                                    ("Base URL", self._base_url),
                                    ("最近 Ping", ping.get("time", "—")),
                                    ("Ping 状态", "成功" if ping.get("ok") else (ping.get("message") or "未执行")),
                                ],
                            ),
                            self._build_key_value_card(
                                "用户信息",
                                [
                                    ("昵称", user.get("nickname", "—")),
                                    ("用户名", user.get("username", "—")),
                                    ("积分", user_meta.get("points", "—")),
                                    ("VIP", "是" if user.get("is_vip") else "否"),
                                    ("VIP 到期", user.get("vip_expiration_date", "—")),
                                    ("累计签到", user_meta.get("signin_days_total", "—")),
                                ],
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._build_key_value_card(
                                "配额与今日用量",
                                [
                                    ("配额重置", quota_data.get("daily_reset", "—")),
                                    ("接口上限", quota_data.get("endpoint_limit", "—")),
                                    ("剩余配额", quota_data.get("endpoint_remaining", "—")),
                                    ("今日总调用", usage_today_data.get("total_calls", "—")),
                                    ("今日成功", usage_today_data.get("success_calls", "—")),
                                    ("平均耗时(ms)", usage_today_data.get("avg_latency", "—")),
                                ],
                            ),
                            self._build_key_value_card(
                                "每周免费解锁额度",
                                [
                                    ("永久 VIP", "是" if weekly_data.get("is_forever_vip") else "否"),
                                    ("周额度", weekly_data.get("limit", "—")),
                                    ("本周已用", weekly_data.get("used", "—")),
                                    ("剩余额度", weekly_data.get("remaining", "—")),
                                    ("无限额度", "是" if weekly_data.get("unlimited") else "否"),
                                    ("累积额度", weekly_data.get("bonus_quota", "—")),
                                ],
                            ),
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
                                        "component": "VCard",
                                        "props": {"flat": True, "border": True},
                                        "content": [
                                            {"component": "VCardTitle", "text": "签到历史"},
                                            {
                                                "component": "VCardText",
                                                "content": [
                                                    {
                                                        "component": "VTable",
                                                        "props": {"density": "compact", "hover": True},
                                                        "content": [
                                                            {
                                                                "component": "thead",
                                                                "content": [
                                                                    {
                                                                        "component": "tr",
                                                                        "content": [
                                                                            {"component": "th", "text": "时间"},
                                                                            {"component": "th", "text": "触发方式"},
                                                                            {"component": "th", "text": "模式"},
                                                                            {"component": "th", "text": "状态"},
                                                                            {"component": "th", "text": "说明"},
                                                                        ],
                                                                    }
                                                                ],
                                                            },
                                                            {
                                                                "component": "tbody",
                                                                "content": history_rows
                                                                or [{"component": "tr", "content": [{"component": "td", "props": {"colspan": 5}, "text": "暂无签到记录"}]}],
                                                            },
                                                        ],
                                                    }
                                                ],
                                            },
                                        ],
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
                                        "component": "VCard",
                                        "props": {"flat": True, "border": True},
                                        "content": [
                                            {"component": "VCardTitle", "text": "最近一次资源查询"},
                                            {
                                                "component": "VCardSubtitle",
                                                "text": (
                                                    f"{search_result.get('time', '未执行')} | "
                                                    f"{(search_result.get('query') or {}).get('media_type', '—')} / "
                                                    f"{(search_result.get('query') or {}).get('tmdb_id', '—')} | "
                                                    f"{search_result.get('message', '—')}"
                                                ),
                                            },
                                            {
                                                "component": "VCardText",
                                                "content": [
                                                    {
                                                        "component": "VTable",
                                                        "props": {"density": "compact", "hover": True},
                                                        "content": [
                                                            {
                                                                "component": "thead",
                                                                "content": [
                                                                    {
                                                                        "component": "tr",
                                                                        "content": [
                                                                            {"component": "th", "text": "slug"},
                                                                            {"component": "th", "text": "标题"},
                                                                            {"component": "th", "text": "大小"},
                                                                            {"component": "th", "text": "片源"},
                                                                            {"component": "th", "text": "分辨率"},
                                                                            {"component": "th", "text": "解锁积分"},
                                                                            {"component": "th", "text": "已解锁"},
                                                                            {"component": "th", "text": "官方"},
                                                                        ],
                                                                    }
                                                                ],
                                                            },
                                                            {
                                                                "component": "tbody",
                                                                "content": self._build_resource_rows(resource_items)
                                                                or [{"component": "tr", "content": [{"component": "td", "props": {"colspan": 8}, "text": "暂无资源查询结果"}]}],
                                                            },
                                                        ],
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._build_key_value_card(
                                "最近一次资源解锁",
                                [
                                    ("时间", unlock_result.get("time", "—")),
                                    ("slug", unlock_result.get("slug", "—")),
                                    ("结果", unlock_result.get("message", "—")),
                                    ("链接", unlock_data.get("url", "—")),
                                    ("提取码", unlock_data.get("access_code", "—")),
                                    ("完整链接", unlock_data.get("full_url", "—")),
                                ],
                            ),
                            self._build_key_value_card(
                                "最近一次 115 转存",
                                [
                                    ("时间", transfer_115_result.get("time", "—")),
                                    ("触发方式", transfer_115_result.get("trigger", "—")),
                                    ("结果", transfer_115_result.get("message", "—")),
                                    ("目录", transfer_115_result.get("path", "—")),
                                    ("保存位置", transfer_115_data.get("save_parent", "—")),
                                    ("父目录 ID", transfer_115_data.get("parent_id", "—")),
                                ],
                            ),
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            self._build_key_value_card(
                                "最近一次分享详情/操作",
                                [
                                    ("详情时间", share_detail.get("time", "—")),
                                    ("详情标题", share_detail_data.get("title", "—")),
                                    ("详情媒体", ((share_detail_data.get("media") or {}).get("title") if isinstance(share_detail_data.get("media"), dict) else "—") or "—"),
                                    ("操作时间", share_action.get("time", "—")),
                                    ("操作类型", share_action.get("action", "—")),
                                    ("操作结果", share_action.get("message", "—")),
                                    ("操作标题", share_action_data.get("title", "—") if isinstance(share_action_data, dict) else "—"),
                                ],
                            ),
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
                                        "component": "VCard",
                                        "props": {"flat": True, "border": True},
                                        "content": [
                                            {"component": "VCardTitle", "text": "最近一次分享列表"},
                                            {
                                                "component": "VCardSubtitle",
                                                "text": (
                                                    f"{shares_list.get('time', '未执行')} | "
                                                    f"page={(shares_list.get('query') or {}).get('page', '—')} | "
                                                    f"size={(shares_list.get('query') or {}).get('page_size', '—')} | "
                                                    f"{shares_list.get('message', '—')}"
                                                ),
                                            },
                                            {
                                                "component": "VCardText",
                                                "content": [
                                                    {
                                                        "component": "VTable",
                                                        "props": {"density": "compact", "hover": True},
                                                        "content": [
                                                            {
                                                                "component": "thead",
                                                                "content": [
                                                                    {
                                                                        "component": "tr",
                                                                        "content": [
                                                                            {"component": "th", "text": "slug"},
                                                                            {"component": "th", "text": "标题"},
                                                                            {"component": "th", "text": "大小"},
                                                                            {"component": "th", "text": "解锁积分"},
                                                                            {"component": "th", "text": "已解锁人数"},
                                                                            {"component": "th", "text": "创建时间"},
                                                                        ],
                                                                    }
                                                                ],
                                                            },
                                                            {
                                                                "component": "tbody",
                                                                "content": self._build_share_rows(share_items)
                                                                or [{"component": "tr", "content": [{"component": "td", "props": {"colspan": 6}, "text": "暂无分享列表结果"}]}],
                                                            },
                                                        ],
                                                    }
                                                ],
                                            },
                                        ],
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

        if last_error:
            page_content[0]["content"].append(
                {
                    "component": "VRow",
                    "content": [
                        {
                            "component": "VCol",
                            "props": {"cols": 12},
                            "content": [
                                {
                                    "component": "VAlert",
                                    "props": {
                                        "type": "warning",
                                        "variant": "tonal",
                                        "text": f"最近一次错误：{last_error.get('action', '—')} | {last_error.get('time', '—')} | {last_error.get('message', '—')}",
                                    },
                                }
                            ],
                        }
                    ],
                }
            )

        return page_content

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/health", "endpoint": self.api_health, "methods": ["GET"], "summary": "检查插件与 API Key 状态", "auth": "bear"},
            {"path": "/account", "endpoint": self.api_account, "methods": ["GET"], "summary": "获取当前用户信息", "auth": "bear"},
            {"path": "/checkin", "endpoint": self.api_checkin, "methods": ["POST"], "summary": "执行普通或赌狗签到", "auth": "bear"},
            {"path": "/quota", "endpoint": self.api_quota, "methods": ["GET"], "summary": "获取配额信息", "auth": "bear"},
            {"path": "/usage", "endpoint": self.api_usage, "methods": ["GET"], "summary": "获取用量统计", "auth": "bear"},
            {"path": "/usage/today", "endpoint": self.api_usage_today, "methods": ["GET"], "summary": "获取今日用量", "auth": "bear"},
            {"path": "/vip/weekly-free-quota", "endpoint": self.api_weekly_quota, "methods": ["GET"], "summary": "获取每周免费解锁额度", "auth": "bear"},
            {"path": "/resources/search", "endpoint": self.api_search_resources, "methods": ["GET"], "summary": "按 TMDB ID 或关键词搜索资源", "auth": "bear"},
            {"path": "/resources/unlock", "endpoint": self.api_unlock_resource, "methods": ["POST"], "summary": "按 slug 解锁资源", "auth": "bear"},
            {"path": "/transfer/115", "endpoint": self.api_transfer_115, "methods": ["POST"], "summary": "把 115 分享链接转存到固定目录", "auth": "bear"},
            {"path": "/resource/check", "endpoint": self.api_check_resource, "methods": ["POST"], "summary": "检测资源链接类型", "auth": "bear"},
            {"path": "/shares", "endpoint": self.api_list_shares, "methods": ["GET"], "summary": "获取我的分享列表", "auth": "bear"},
            {"path": "/shares/detail", "endpoint": self.api_share_detail, "methods": ["GET"], "summary": "获取分享详情", "auth": "bear"},
            {"path": "/shares/create", "endpoint": self.api_share_create, "methods": ["POST"], "summary": "创建分享", "auth": "bear"},
            {"path": "/shares/update", "endpoint": self.api_share_update, "methods": ["POST"], "summary": "更新分享", "auth": "bear"},
            {"path": "/shares/delete", "endpoint": self.api_share_delete, "methods": ["POST"], "summary": "删除分享", "auth": "bear"},
        ]

    async def api_health(self) -> Dict[str, Any]:
        ok, result, message = self.ping(remember=False)
        return {
            "success": ok,
            "message": result.get("message") or message or "success",
            "data": {
                "plugin_enabled": self._enabled,
                "api_key_configured": bool(self._api_key),
                "base_url": self._base_url,
                "ping": result,
            },
        }

    async def api_account(self) -> Dict[str, Any]:
        ok, result, message = self.fetch_me(remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result.get("data") or {}}

    async def api_checkin(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, result, message = self.perform_checkin(
            is_gambler=self._coerce_bool(body.get("is_gambler"), self._gambler_mode),
            remember=True,
            trigger="插件 API",
        )
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_quota(self) -> Dict[str, Any]:
        ok, result, message = self.fetch_quota(remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result.get("data") or {}}

    async def api_usage(self, request: Request) -> Dict[str, Any]:
        start_date = request.query_params.get("start_date", "")
        end_date = request.query_params.get("end_date", "")
        ok, result, message = self.fetch_usage(start_date=start_date, end_date=end_date, remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_usage_today(self) -> Dict[str, Any]:
        ok, result, message = self.fetch_usage_today(remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result.get("data") or {}}

    async def api_weekly_quota(self) -> Dict[str, Any]:
        ok, result, message = self.fetch_weekly_free_quota(remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result.get("data") or {}}

    async def api_search_resources(self, request: Request) -> Dict[str, Any]:
        media_type = request.query_params.get("type") or request.query_params.get("media_type") or "movie"
        tmdb_id = request.query_params.get("tmdb_id", "")
        keyword = request.query_params.get("keyword", "")
        year = request.query_params.get("year", "")
        candidate_limit = request.query_params.get("candidate_limit", "5")
        result_limit = request.query_params.get("limit", "10")
        if tmdb_id:
            ok, result, message = self.search_resources(media_type=media_type, tmdb_id=tmdb_id, remember=True)
        else:
            ok, result, message = await self.search_resources_by_keyword(
                keyword=keyword,
                media_type=media_type,
                year=year,
                candidate_limit=self._safe_int(candidate_limit, 5),
                result_limit=self._safe_int(result_limit, 10),
                remember=True,
            )
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_unlock_resource(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        slug = body.get("slug") or ""
        transfer_115 = self._coerce_bool(
            body.get("transfer_115"),
            self._transfer_115_enabled and self._auto_transfer_115_on_unlock,
        )
        transfer_path = body.get("path") or body.get("transfer_path") or self._transfer_115_path
        ok, result, message = self.unlock_resource(
            slug=slug,
            remember=True,
            transfer_115=transfer_115,
            transfer_path=transfer_path,
        )
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_transfer_115(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, result, message = self.transfer_115_share(
            url=body.get("url") or "",
            access_code=body.get("access_code") or "",
            path=body.get("path") or body.get("transfer_path") or self._transfer_115_path,
            remember=True,
            trigger="插件 API",
        )
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_check_resource(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        url = body.get("url") or ""
        ok, result, message = self.check_resource(url=url, remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_list_shares(self, request: Request) -> Dict[str, Any]:
        page = self._safe_int(request.query_params.get("page"), 1)
        page_size = self._safe_int(request.query_params.get("page_size"), 20)
        ok, result, message = self.list_shares(page=page, page_size=page_size, remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_share_detail(self, request: Request) -> Dict[str, Any]:
        slug = request.query_params.get("slug", "")
        ok, result, message = self.get_share_detail(slug=slug, remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_share_create(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        ok, result, message = self.create_share(body or {}, remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_share_update(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        slug = body.pop("slug", "") if isinstance(body, dict) else ""
        ok, result, message = self.update_share(slug=slug, share_payload=body or {}, remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}

    async def api_share_delete(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        slug = body.get("slug", "") if isinstance(body, dict) else ""
        ok, result, message = self.delete_share(slug=slug, remember=True)
        return {"success": ok, "message": result.get("message") or message or "success", "data": result}
