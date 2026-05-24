import hmac
import json
import random
import re
import time
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlparse, urlencode
from urllib.request import Request as UrlRequest, urlopen
from fastapi import Request

from app.log import logger
from app.plugins import _PluginBase

try:
    from app.core.config import settings
except Exception:
    settings = None

try:
    from app.schemas import NotificationType
except Exception:
    NotificationType = None

try:
    from app.utils.crypto import CryptoJsUtils
except Exception:
    CryptoJsUtils = None


class QuarkShareSaver(_PluginBase):
    plugin_name = "夸克分享转存"
    plugin_desc = "把夸克分享链接直接转存到自己的夸克网盘目录，适合作为智能体和飞书的稳定执行入口。"
    plugin_icon = "https://raw.githubusercontent.com/liuyuexi1987/MoviePilot-Plugins/main/icons/quark.ico"
    plugin_version = "0.1.0"
    plugin_author = "liuyuexi1987"
    plugin_level = 1
    author_url = "https://github.com/liuyuexi1987"
    plugin_config_prefix = "quarksharesaver_"
    plugin_order = 32
    auth_level = 1

    _enabled = False
    _notify = True
    _cookie = ""
    _default_target_path = "/飞书"
    _timeout = 30
    _auto_import_cookiecloud = True
    _import_cookiecloud_once = False

    _share_url = ""
    _access_code = ""
    _target_path = ""
    _transfer_once = False

    _last_transfer_key = "last_transfer"
    _last_error_key = "last_error"
    _path_cache: Dict[str, str] = {"/": "0"}

    @staticmethod
    def _clean_text(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip()

    @staticmethod
    def _safe_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except Exception:
            return default

    @staticmethod
    def _normalize_path(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "/"
        if not text.startswith("/"):
            text = f"/{text}"
        text = re.sub(r"/+", "/", text)
        return text.rstrip("/") or "/"

    def _build_config(self, overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        config = {
            "enabled": self._enabled,
            "notify": self._notify,
            "cookie": self._cookie,
            "default_target_path": self._default_target_path,
            "timeout": self._timeout,
            "auto_import_cookiecloud": self._auto_import_cookiecloud,
            "import_cookiecloud_once": self._import_cookiecloud_once,
            "share_url": self._share_url,
            "access_code": self._access_code,
            "target_path": self._target_path,
            "transfer_once": self._transfer_once,
        }
        if overrides:
            config.update(overrides)
        return config

    def _tz_now(self) -> datetime:
        if settings is not None:
            try:
                from zoneinfo import ZoneInfo

                return datetime.now(ZoneInfo(getattr(settings, "TZ", "Asia/Shanghai")))
            except Exception:
                pass
        return datetime.now()

    def _save_state(self, key: str, value: Any) -> None:
        try:
            self.save_data(key=key, value=value)
        except Exception as exc:
            logger.warning(f"[QuarkShareSaver] 保存状态失败 {key}: {exc}")

    def _load_state(self, key: str, default: Any = None) -> Any:
        try:
            value = self.get_data(key)
            return default if value is None else value
        except Exception as exc:
            logger.warning(f"[QuarkShareSaver] 读取状态失败 {key}: {exc}")
            return default

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

    def _notify_message(self, title: str, text: str) -> None:
        if not self._notify or not hasattr(self, "post_message"):
            return
        try:
            if NotificationType is not None:
                self.post_message(mtype=NotificationType.SiteMessage, title=title, text=text)
            else:
                self.post_message(title=title, text=text)
        except Exception as exc:
            logger.warning(f"[QuarkShareSaver] 发送通知失败: {exc}")

    def _load_cookiecloud_quark_cookie(self) -> Tuple[str, str]:
        if settings is None:
            return "", "未获取到系统设置"
        if CryptoJsUtils is None:
            return "", "运行环境缺少 CookieCloud 解密依赖"

        key = self._clean_text(getattr(settings, "COOKIECLOUD_KEY", ""))
        password = self._clean_text(getattr(settings, "COOKIECLOUD_PASSWORD", ""))
        cookie_path = getattr(settings, "COOKIE_PATH", None)
        if not bool(getattr(settings, "COOKIECLOUD_ENABLE_LOCAL", False)):
            return "", "未启用本地 CookieCloud"
        if not key or not password or not cookie_path:
            return "", "CookieCloud 参数不完整"

        file_path = Path(cookie_path) / f"{key}.json"
        if not file_path.exists():
            return "", f"未找到 CookieCloud 文件: {file_path.name}"

        try:
            encrypted_data = json.loads(file_path.read_text(encoding="utf-8"))
            encrypted = encrypted_data.get("encrypted")
            if not encrypted:
                return "", "CookieCloud 文件缺少 encrypted 字段"
            crypt_key = md5(f"{key}-{password}".encode("utf-8")).hexdigest()[:16].encode("utf-8")
            decrypted = CryptoJsUtils.decrypt(encrypted, crypt_key).decode("utf-8")
            payload = json.loads(decrypted)
        except Exception as exc:
            return "", f"CookieCloud 解密失败: {exc}"

        contents = payload.get("cookie_data") if isinstance(payload, dict) else None
        if not isinstance(contents, dict):
            contents = payload if isinstance(payload, dict) else {}

        merged: Dict[str, str] = {}
        for cookie_items in contents.values():
            if not isinstance(cookie_items, list):
                continue
            for item in cookie_items:
                if not isinstance(item, dict):
                    continue
                domain = self._clean_text(item.get("domain")).lower()
                name = self._clean_text(item.get("name"))
                value = self._clean_text(item.get("value"))
                if "quark.cn" not in domain or not name:
                    continue
                merged[name] = value

        if not merged:
            return "", "CookieCloud 中没有 quark.cn 的 Cookie"
        return "; ".join(f"{name}={value}" for name, value in merged.items() if value), ""

    def _try_import_cookiecloud_cookie(self, *, force: bool = False) -> Tuple[bool, str]:
        if self._cookie and not force:
            return True, "已存在 Cookie，跳过自动导入"
        cookie, message = self._load_cookiecloud_quark_cookie()
        if not cookie:
            logger.info(f"[QuarkShareSaver] CookieCloud 导入未命中: {message}")
            return False, message
        self._cookie = cookie
        logger.info(f"[QuarkShareSaver] 已从 CookieCloud 导入夸克 Cookie，长度: {len(cookie)}")
        return True, "已从 CookieCloud 导入夸克 Cookie"

    @staticmethod
    def _extract_apikey(request: Request, body: Optional[Dict[str, Any]] = None) -> str:
        header = str(request.headers.get("Authorization") or "").strip()
        if header.lower().startswith("bearer "):
            return header.split(" ", 1)[1].strip()
        if body:
            token = str(body.get("apikey") or body.get("api_key") or "").strip()
            if token:
                return token
        return str(request.query_params.get("apikey") or "").strip()

    def _check_api_access(self, request: Request, body: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        expected = self._clean_text(getattr(settings, "API_TOKEN", "") if settings is not None else "")
        if not expected:
            return False, "服务端未配置 API Token"
        actual = self._extract_apikey(request, body)
        if not hmac.compare_digest(actual, expected):
            return False, "API Token 无效"
        return True, ""

    @staticmethod
    def _extract_url(raw_text: str) -> str:
        match = re.search(r"https?://[^\s<>\"']+", raw_text)
        if match:
            return match.group(0).rstrip(".,);]")
        return ""

    def _extract_share_info(self, share_text: str, access_code: str = "") -> Tuple[str, str, str]:
        raw = self._clean_text(share_text)
        share_url = self._extract_url(raw) or raw
        parsed = urlparse(share_url)
        pwd_id_match = re.search(r"/s/([^/?#]+)", parsed.path)
        pwd_id = pwd_id_match.group(1).strip() if pwd_id_match else ""

        code = self._clean_text(access_code)
        if not code:
            query = dict(parse_qsl(parsed.query))
            code = self._clean_text(query.get("pwd") or query.get("passcode") or query.get("code"))
        if not code and raw:
            for token in raw.replace(share_url, " ").split():
                text = token.strip()
                if not text:
                    continue
                if "=" in text:
                    key, value = text.split("=", 1)
                    if key.strip().lower() in {"pwd", "passcode", "code", "提取码"}:
                        code = self._clean_text(value)
                        break
                elif len(text) <= 8 and not text.startswith("/"):
                    code = text
                    break

        return share_url, pwd_id, code

    @staticmethod
    def _is_quark_share_url(share_url: str) -> bool:
        hostname = urlparse(share_url).hostname or ""
        hostname = hostname.lower().strip(".")
        return hostname.endswith("quark.cn")

    def _validate_share_url(self, share_url: str) -> Tuple[bool, str]:
        if not share_url:
            return False, "未识别到有效夸克分享链接"
        if self._is_quark_share_url(share_url):
            return True, ""
        hostname = urlparse(share_url).hostname or "未知域名"
        return False, f"当前链接域名为 {hostname}，这不是夸克分享链接，请换成 pan.quark.cn 的分享链接"

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Cookie": self._cookie,
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/137.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
            "Content-Type": "application/json;charset=UTF-8",
        }

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        allow_cookiecloud_retry: bool = True,
    ) -> Tuple[bool, Dict[str, Any], str]:
        final_url = url
        if params:
            query = urlencode([(key, "" if value is None else value) for key, value in params.items()])
            final_url = f"{url}?{query}" if query else url
        payload = None
        if json_body is not None:
            payload = json.dumps(json_body).encode("utf-8")
        try:
            request = UrlRequest(
                url=final_url,
                data=payload,
                headers=self._build_headers(),
                method=method.upper(),
            )
            with urlopen(request, timeout=self._timeout) as response:
                status_code = getattr(response, "status", 200)
                raw_body = response.read()
        except HTTPError as exc:
            status_code = exc.code
            raw_body = exc.read() if hasattr(exc, "read") else b""
        except URLError as exc:
            return False, {}, f"请求失败: {exc.reason}"
        except Exception as exc:
            return False, {}, f"请求失败: {exc}"

        try:
            data = json.loads(raw_body.decode("utf-8"))
        except Exception:
            text = raw_body.decode("utf-8", errors="ignore")[:300]
            return False, {}, f"接口返回非 JSON: HTTP {status_code} {text}"

        if status_code == 401 and allow_cookiecloud_retry and self._auto_import_cookiecloud:
            imported, _ = self._try_import_cookiecloud_cookie(force=True)
            if imported:
                return self._request(
                    method,
                    url,
                    params=params,
                    json_body=json_body,
                    allow_cookiecloud_retry=False,
                )

        if status_code != 200:
            return False, data if isinstance(data, dict) else {}, f"HTTP {status_code}"

        if isinstance(data, dict):
            message = str(data.get("message") or data.get("msg") or "").strip()
            ok = data.get("status") == 200 or data.get("code") == 0 or message == "ok"
            if ok:
                return True, data, ""
            return False, data, message or "接口返回失败"

        return False, {}, "接口返回格式错误"

    @staticmethod
    def _common_params() -> Dict[str, Any]:
        now = int(time.time() * 1000)
        return {
            "pr": "ucpro",
            "fr": "pc",
            "uc_param_str": "",
            "__dt": random.randint(100, 9999),
            "__t": now,
        }

    def _get_stoken(self, pwd_id: str, access_code: str = "") -> Tuple[bool, str, str]:
        ok, data, message = self._request(
            "POST",
            "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/token",
            params=self._common_params(),
            json_body={"pwd_id": pwd_id, "passcode": access_code or ""},
        )
        if not ok:
            return False, "", message

        stoken = self._clean_text((data.get("data") or {}).get("stoken"))
        if not stoken:
            return False, "", "未获取到 stoken，可能是提取码错误或 Cookie 失效"
        return True, stoken, ""

    def _get_share_items(self, pwd_id: str, stoken: str) -> Tuple[bool, List[Dict[str, Any]], str]:
        items: List[Dict[str, Any]] = []
        page = 1
        while True:
            params = self._common_params()
            params.update(
                {
                    "pwd_id": pwd_id,
                    "stoken": stoken,
                    "pdir_fid": "0",
                    "force": "0",
                    "_page": str(page),
                    "_size": "50",
                    "_sort": "file_type:asc,updated_at:desc",
                }
            )
            ok, data, message = self._request(
                "GET",
                "https://drive-pc.quark.cn/1/clouddrive/share/sharepage/detail",
                params=params,
            )
            if not ok:
                return False, [], message

            payload = data.get("data") or {}
            meta = data.get("metadata") or {}
            current = payload.get("list") or []
            for item in current:
                items.append(
                    {
                        "fid": str(item.get("fid") or ""),
                        "file_name": str(item.get("file_name") or ""),
                        "dir": bool(item.get("dir")),
                        "file_type": item.get("file_type"),
                        "pdir_fid": str(item.get("pdir_fid") or ""),
                        "share_fid_token": str(item.get("share_fid_token") or ""),
                    }
                )

            total = self._safe_int(meta.get("_total"), 0)
            count = self._safe_int(meta.get("_count"), len(current))
            size = max(1, self._safe_int(meta.get("_size"), 50))
            if total <= len(items) or count < size:
                break
            page += 1

        if not items:
            return False, [], "分享链接为空，或当前账号无权查看内容"
        return True, items, ""

    def _list_children(self, parent_fid: str) -> Tuple[bool, List[Dict[str, Any]], str]:
        page = 1
        result: List[Dict[str, Any]] = []
        while True:
            params = {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "pdir_fid": parent_fid,
                "_page": page,
                "_size": 100,
                "_fetch_total": 1,
                "_fetch_sub_dirs": 0,
                "_sort": "file_type:asc,updated_at:desc",
            }
            ok, data, message = self._request(
                "GET",
                "https://drive-pc.quark.cn/1/clouddrive/file/sort",
                params=params,
            )
            if not ok:
                return False, [], message

            current = ((data.get("data") or {}).get("list")) or []
            for item in current:
                result.append(
                    {
                        "fid": str(item.get("fid") or ""),
                        "name": str(item.get("file_name") or ""),
                        "dir": int(item.get("file_type") or 0) == 0,
                        "size": item.get("size") or 0,
                        "updated_at": item.get("updated_at") or 0,
                    }
                )
            if len(current) < 100:
                break
            page += 1

        return True, result, ""

    def _find_child_dir(self, parent_fid: str, name: str) -> Tuple[bool, str, str]:
        ok, items, message = self._list_children(parent_fid)
        if not ok:
            return False, "", message
        for item in items:
            if item.get("dir") and item.get("name") == name:
                return True, str(item.get("fid") or ""), ""
        return True, "", ""

    def _create_folder(self, parent_fid: str, name: str) -> Tuple[bool, str, str]:
        ok, data, message = self._request(
            "POST",
            "https://pan.quark.cn/1/clouddrive/file/create",
            json_body={
                "pdir_fid": parent_fid,
                "file_name": name,
                "dir_path": "",
                "dir_init_lock": False,
            },
        )
        if not ok:
            return False, "", message

        folder = data.get("data") or {}
        folder_id = self._clean_text(folder.get("fid") or folder.get("file_id"))
        if not folder_id:
            return False, "", "创建目录成功但未返回 fid"
        return True, folder_id, ""

    def _ensure_target_dir(self, path: str) -> Tuple[bool, str, str]:
        normalized = self._normalize_path(path or self._default_target_path)
        if normalized == "/":
            return True, "0", normalized
        cached = self._path_cache.get(normalized)
        if cached:
            return True, cached, normalized

        current_fid = "0"
        built = ""
        for part in [segment for segment in normalized.split("/") if segment]:
            built = f"{built}/{part}" if built else f"/{part}"
            cached = self._path_cache.get(built)
            if cached:
                current_fid = cached
                continue

            ok, found_fid, message = self._find_child_dir(current_fid, part)
            if not ok:
                return False, "", message
            if not found_fid:
                ok, found_fid, message = self._create_folder(current_fid, part)
                if not ok:
                    return False, "", f"创建目录失败 {built}: {message}"
            self._path_cache[built] = found_fid
            current_fid = found_fid
        return True, current_fid, normalized

    def _resolve_existing_dir(self, path: str) -> Tuple[bool, str, str]:
        normalized = self._normalize_path(path)
        if normalized == "/":
            return True, "0", normalized
        cached = self._path_cache.get(normalized)
        if cached:
            return True, cached, normalized

        current_fid = "0"
        built = ""
        for part in [segment for segment in normalized.split("/") if segment]:
            built = f"{built}/{part}" if built else f"/{part}"
            cached = self._path_cache.get(built)
            if cached:
                current_fid = cached
                continue
            ok, found_fid, message = self._find_child_dir(current_fid, part)
            if not ok:
                return False, "", message
            if not found_fid:
                return False, "", f"目录不存在: {built}"
            self._path_cache[built] = found_fid
            current_fid = found_fid
        return True, current_fid, normalized

    def _create_save_task(
        self,
        pwd_id: str,
        stoken: str,
        items: List[Dict[str, Any]],
        to_pdir_fid: str,
    ) -> Tuple[bool, str, str]:
        fid_list = [str(item.get("fid") or "") for item in items if item.get("fid")]
        fid_token_list = [
            str(item.get("share_fid_token") or "")
            for item in items
            if item.get("fid") and item.get("share_fid_token")
        ]
        if not fid_list or len(fid_list) != len(fid_token_list):
            return False, "", "分享内容缺少 fid 或 share_fid_token，无法转存"

        params = self._common_params()
        ok, data, message = self._request(
            "POST",
            "https://drive.quark.cn/1/clouddrive/share/sharepage/save",
            params=params,
            json_body={
                "fid_list": fid_list,
                "fid_token_list": fid_token_list,
                "to_pdir_fid": to_pdir_fid,
                "pwd_id": pwd_id,
                "stoken": stoken,
                "pdir_fid": "0",
                "scene": "link",
            },
        )
        if not ok:
            return False, "", message

        task_id = self._clean_text((data.get("data") or {}).get("task_id"))
        if not task_id:
            return False, "", "未获取到转存任务 ID"
        return True, task_id, ""

    def _wait_task(self, task_id: str, retry: int = 20) -> Tuple[bool, Dict[str, Any], str]:
        for index in range(retry):
            time.sleep(1.0 if index == 0 else 1.5)
            params = {
                "pr": "ucpro",
                "fr": "pc",
                "uc_param_str": "",
                "task_id": task_id,
                "retry_index": index,
                "__dt": 21192,
                "__t": int(time.time() * 1000),
            }
            ok, data, message = self._request(
                "GET",
                "https://drive-pc.quark.cn/1/clouddrive/task",
                params=params,
            )
            if not ok:
                return False, {}, message

            task = data.get("data") or {}
            status = self._safe_int(task.get("status"), -1)
            if status == 2:
                return True, task, ""
            if status in {3, 4, 5, 6, 7}:
                return False, task, self._clean_text(task.get("message")) or "夸克任务执行失败"

        return False, {}, "等待夸克转存任务超时"

    def _check_cookie(self) -> Tuple[bool, str]:
        ok, _, message = self._list_children("0")
        if ok:
            return True, ""
        return False, message or "Cookie 校验失败"

    def transfer_share(
        self,
        share_text: str,
        access_code: str = "",
        target_path: str = "",
        *,
        remember: bool = True,
        trigger: str = "插件 API",
    ) -> Tuple[bool, Dict[str, Any], str]:
        share_url, pwd_id, final_code = self._extract_share_info(share_text, access_code)
        ok, message = self._validate_share_url(share_url)
        if not ok:
            return False, {}, message
        if not pwd_id:
            return False, {}, "未识别到有效夸克分享链接"

        if not self._enabled:
            return False, {}, "插件未启用"
        if not self._cookie:
            return False, {}, "未配置夸克 Cookie"

        ok, stoken, message = self._get_stoken(pwd_id, final_code)
        if not ok:
            self._remember_error("get_stoken", message, {"pwd_id": pwd_id})
            return False, {}, message

        ok, share_items, message = self._get_share_items(pwd_id, stoken)
        if not ok:
            self._remember_error("get_share_items", message, {"pwd_id": pwd_id})
            return False, {}, message

        ok, target_fid, normalized_path = self._ensure_target_dir(target_path or self._default_target_path)
        if not ok:
            self._remember_error("ensure_target_dir", target_fid, {"path": target_path or self._default_target_path})
            return False, {}, target_fid

        ok, task_id, message = self._create_save_task(pwd_id, stoken, share_items, target_fid)
        if not ok:
            self._remember_error("create_save_task", message, {"pwd_id": pwd_id, "path": normalized_path})
            return False, {}, message

        ok, task, message = self._wait_task(task_id)
        if not ok:
            self._remember_error("wait_task", message, {"task_id": task_id})
            return False, {"task_id": task_id}, message

        item_names = [str(item.get("file_name") or "") for item in share_items if item.get("file_name")]
        result = {
            "share_url": share_url,
            "pwd_id": pwd_id,
            "access_code": final_code,
            "target_path": normalized_path,
            "target_fid": target_fid,
            "task_id": task_id,
            "saved_count": len(share_items),
            "items": item_names[:20],
            "task": task,
            "trigger": trigger,
            "time": self._tz_now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        if remember:
            self._save_state(self._last_transfer_key, result)
        self._notify_message(
            "夸克分享转存完成",
            (
                f"保存目录：{normalized_path}\n"
                f"任务ID：{task_id}\n"
                f"顶层条目：{len(share_items)}"
            ),
        )
        return True, result, "success"

    def init_plugin(self, config: dict = None):
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._cookie = self._clean_text(config.get("cookie"))
        self._default_target_path = self._normalize_path(config.get("default_target_path") or "/飞书")
        self._timeout = max(10, self._safe_int(config.get("timeout"), 30))
        self._auto_import_cookiecloud = bool(config.get("auto_import_cookiecloud", True))
        self._import_cookiecloud_once = bool(config.get("import_cookiecloud_once"))

        self._share_url = self._clean_text(config.get("share_url"))
        self._access_code = self._clean_text(config.get("access_code"))
        self._target_path = self._normalize_path(config.get("target_path") or self._default_target_path)
        self._transfer_once = bool(config.get("transfer_once"))
        self._path_cache = {"/": "0"}

        if self._import_cookiecloud_once or (self._auto_import_cookiecloud and not self._cookie):
            imported_cookie, message = self._try_import_cookiecloud_cookie(force=self._import_cookiecloud_once)
            if self._import_cookiecloud_once:
                self._import_cookiecloud_once = False
                self.update_config(self._build_config({"cookie": self._cookie, "import_cookiecloud_once": False}))
            elif imported_cookie:
                self.update_config(self._build_config({"cookie": self._cookie}))
            if imported_cookie and self._notify:
                self._notify_message("夸克 Cookie 已导入", message)

        if self._transfer_once:
            self._transfer_once = False
            self.update_config(self._build_config({"transfer_once": False}))
            if self._enabled and self._share_url:
                ok, _, message = self.transfer_share(
                    self._share_url,
                    access_code=self._access_code,
                    target_path=self._target_path,
                    remember=True,
                    trigger="插件页面立即转存",
                )
                if not ok:
                    self._notify_message("夸克分享转存失败", message)

    def get_state(self) -> bool:
        return self._enabled and bool(self._cookie)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return []

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/health", "endpoint": self.api_health, "methods": ["GET"], "summary": "检查 Cookie 与默认目录状态"},
            {"path": "/folders", "endpoint": self.api_folders, "methods": ["GET"], "summary": "列出夸克网盘目录"},
            {"path": "/share/info", "endpoint": self.api_share_info, "methods": ["POST"], "summary": "解析夸克分享链接顶层条目"},
            {"path": "/transfer", "endpoint": self.api_transfer, "methods": ["POST"], "summary": "把夸克分享链接转存到指定目录"},
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "notify", "label": "发送站内通知"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VTextField", "props": {"model": "timeout", "label": "请求超时(秒)", "type": "number"}}
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
                                        "component": "VSwitch",
                                        "props": {"model": "auto_import_cookiecloud", "label": "Cookie 为空时自动从 CookieCloud 导入"}
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "import_cookiecloud_once", "label": "立即从 CookieCloud 重新导入一次"}
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
                                            "model": "cookie",
                                            "label": "夸克 Cookie",
                                            "rows": 4,
                                            "placeholder": "浏览器登录 pan.quark.cn 后复制完整 Cookie",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "default_target_path",
                                            "label": "默认保存目录",
                                            "placeholder": "/来自分享/夸克",
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
                                                "推荐给智能体或飞书调用的接口：\n"
                                                "POST /api/v1/plugin/QuarkShareSaver/transfer\n"
                                                "参数：url, access_code, path。\n"
                                                "飞书建议命令：夸克转存 分享链接 pwd=提取码 path=/最新动画\n"
                                                "如果你启用了本地 CookieCloud，插件可以自动导入 quark.cn Cookie。"
                                            ),
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
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {"component": "VSwitch", "props": {"model": "transfer_once", "label": "立即转存一次"}}
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "target_path",
                                            "label": "本次保存目录",
                                            "placeholder": "/来自分享/夸克",
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
                                            "model": "share_url",
                                            "label": "夸克分享链接",
                                            "placeholder": "https://pan.quark.cn/s/xxxx",
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
                                        "component": "VTextField",
                                        "props": {
                                            "model": "access_code",
                                            "label": "提取码(可留空)",
                                            "placeholder": "abcd",
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
        ], self._build_config()

    def get_page(self) -> List[dict]:
        last_transfer = self._load_state(self._last_transfer_key, default={}) or {}
        last_error = self._load_state(self._last_error_key, default={}) or {}

        transfer_lines = [
            f"最近一次：{last_transfer.get('time') or '暂无'}",
            f"保存目录：{last_transfer.get('target_path') or '-'}",
            f"任务ID：{last_transfer.get('task_id') or '-'}",
            f"顶层条目：{last_transfer.get('saved_count') or 0}",
        ]
        if last_transfer.get("items"):
            transfer_lines.append("示例条目：" + ", ".join(last_transfer.get("items")[:5]))

        error_lines = [
            f"最近错误动作：{last_error.get('action') or '暂无'}",
            f"错误时间：{last_error.get('time') or '-'}",
            f"错误信息：{last_error.get('message') or '-'}",
        ]

        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VCard",
                                "props": {"variant": "tonal"},
                                "content": [
                                    {
                                        "component": "VCardText",
                                        "text": (
                                            "夸克分享转存插件负责做一件事：把夸克分享链接稳定转存到自己的夸克网盘。"
                                            "推荐让智能体和飞书只调用这一个稳定入口，不要自己拼夸克接口。"
                                        ),
                                    }
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
                                "content": [
                                    {"component": "VCardTitle", "text": "最近转存"},
                                    {"component": "VCardText", "text": "\n".join(transfer_lines)},
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
                                "content": [
                                    {"component": "VCardTitle", "text": "最近错误"},
                                    {"component": "VCardText", "text": "\n".join(error_lines)},
                                ],
                            }
                        ],
                    },
                ],
            }
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        return []

    def stop_service(self):
        pass

    async def api_health(self, request: Request) -> Dict[str, Any]:
        allowed, message = self._check_api_access(request)
        if not allowed:
            return {"success": False, "message": message, "data": {}}
        ok = False
        message = ""
        if self._enabled and self._cookie:
            ok, message = self._check_cookie()
        return {
            "success": ok if self._enabled and self._cookie else False,
            "message": "success" if ok else (message or "插件未启用或未配置 Cookie"),
            "data": {
                "plugin_enabled": self._enabled,
                "cookie_configured": bool(self._cookie),
                "default_target_path": self._default_target_path,
                "timeout": self._timeout,
            },
        }

    async def api_folders(self, request: Request) -> Dict[str, Any]:
        allowed, message = self._check_api_access(request)
        if not allowed:
            return {"success": False, "message": message, "data": {}}
        path = self._normalize_path(request.query_params.get("path") or "/")
        if not self._enabled or not self._cookie:
            return {"success": False, "message": "插件未启用或未配置 Cookie", "data": {"path": path, "items": []}}

        ok, folder_id, normalized = self._resolve_existing_dir(path)
        if not ok:
            return {"success": False, "message": folder_id or "目录不存在", "data": {"path": path, "items": []}}

        ok, items, message = self._list_children(folder_id)
        dirs = [
            {"fid": item.get("fid"), "name": item.get("name"), "path": f"{normalized.rstrip('/')}/{item.get('name')}".replace("//", "/")}
            for item in items
            if item.get("dir")
        ]
        return {"success": ok, "message": "success" if ok else message, "data": {"path": normalized, "items": dirs}}

    async def api_share_info(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        allowed, message = self._check_api_access(request, body)
        if not allowed:
            return {"success": False, "message": message, "data": {}}
        share_url = body.get("url") or body.get("share_url") or ""
        access_code = body.get("access_code") or body.get("pwd") or ""
        share_url, pwd_id, final_code = self._extract_share_info(share_url, access_code)
        ok, message = self._validate_share_url(share_url)
        if not ok:
            return {"success": False, "message": message, "data": {}}
        if not pwd_id:
            return {"success": False, "message": "未识别到有效夸克分享链接", "data": {}}

        if not self._enabled or not self._cookie:
            return {"success": False, "message": "插件未启用或未配置 Cookie", "data": {"pwd_id": pwd_id}}

        ok, stoken, message = self._get_stoken(pwd_id, final_code)
        if not ok:
            return {"success": False, "message": message, "data": {"pwd_id": pwd_id}}

        ok, items, message = self._get_share_items(pwd_id, stoken)
        return {
            "success": ok,
            "message": "success" if ok else message,
            "data": {
                "pwd_id": pwd_id,
                "access_code": final_code,
                "items": items[:20],
                "count": len(items),
            },
        }

    async def api_transfer(self, request: Request) -> Dict[str, Any]:
        try:
            body = await request.json()
        except Exception:
            body = {}
        allowed, message = self._check_api_access(request, body)
        if not allowed:
            return {"success": False, "message": message, "data": {}}
        ok, result, message = self.transfer_share(
            share_text=body.get("url") or body.get("share_url") or "",
            access_code=body.get("access_code") or body.get("pwd") or "",
            target_path=body.get("path") or body.get("target_path") or self._default_target_path,
            remember=True,
            trigger="插件 API",
        )
        return {"success": ok, "message": message, "data": result}
