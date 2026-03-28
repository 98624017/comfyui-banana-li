
import base64
import configparser
import os
import sys
import re
from dataclasses import dataclass
from typing import Dict, Optional

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger


@dataclass
class KVAuthConfig:
    """Edge KV 鉴权配置载体。"""

    enabled: bool
    endpoint: str
    token: Optional[str]
    timeout_seconds: float
    fail_open_on_5xx: bool
    disable_ssl_verify: bool


class ConfigManager:
    """集中管理 Banana Gemini 节点的配置与测试模式逻辑。"""

    XINBAO_TEST_ROUTE_LABEL = "心宝❤新渠道"
    XINBAO_TEST_ROUTE_LEGACY_LABELS = frozenset(
        {
            "测试高速渠道(key不通用)",
            "心宝❤测试新渠道（Key不通用）",
            "心宝❤测试新渠道(Key不通用)",
            "心宝测试新渠道",
            XINBAO_TEST_ROUTE_LABEL,
        }
    )
    XINBAO_TEST_BASE_URL = "https://api.xinbao-ai.com"
    _ENC_KEY_PARTS = (3, 4)
    _DEFAULT_ROUTE_KEY = "hk"
    _DEFAULT_API_BASE_URL_CODEPOINTS = [104, 116, 116, 112, 115, 58, 47, 47, 104, 107, 45, 97, 112, 105, 46, 97, 97, 98, 97, 111, 46, 116, 111, 112]
    _API_BASE_URLS_ENCODED = {
        "hk": "b3Nzd3Q9KChvbCpmd24pZmZlZmgpc2h3",
        "us": "b3Nzd3Q9KChmd24pZmZlZmgpc2h3",
        "cf": "b3Nzd3Q9KChkYSpmd24pZmZlZmgpc2h3",
        "hidden": "b3Nzd3Q9KChmd24xMTEpfWJmZXJ1KWZ3dw==",
    }
    _ROUTE_LABEL_TO_KEY = {
        # Old labels for backward compatibility
        "香港专线": "hk",
        "直连美区": "us",
        "cf专线": "cf",
        "CF专线": "cf",
        "cf": "cf",
        # New labels with suffix
        "香港专线(旧渠道)": "hk",
        "直连美区(旧渠道)": "us",
        "cf专线(旧渠道)": "cf",
        "CF专线(旧渠道)": "cf",
    }
    VALID_ROUTE_LABELS = ["香港专线(旧渠道)", "直连美区(旧渠道)", "CF专线(旧渠道)"]
    DEFAULT_ROUTE_LABEL = "香港专线(旧渠道)"

    _CONFIG_SECTION = "gemini"
    _TEST_CONFIG_FILE_NAME = "banana_gemini_test.local.ini"
    _TEST_CONFIG_SECTION = "gemini_test"
    _TEST_MODE_ENV_VAR = "BANANA_GEMINI_USE_LOCAL_TEST"
    _DEFAULT_API_KEY = "your-api-key-here"
    _PLACEHOLDER_KEYS = {
        "your-api-key-here",
        "your_api_key_here",
        "yourapikeyhere",
    }
    # Obfuscated: https://apicha.yfeng.wang/api/check
    _KV_AUTH_DEFAULT_ENDPOINT = base64.b64decode("aHR0cHM6Ly9hcGljaGEueWZlbmcud2FuZy9hcGkvY2hlY2s=").decode()
    _KV_AUTH_DEFAULT_TIMEOUT = 8.0

    def __init__(self, base_dir: Optional[str] = None):
        self._base_dir = base_dir or os.path.dirname(os.path.abspath(__file__))
        self._config_path = os.path.join(self._base_dir, "config.ini")
        self._test_config_path = os.path.join(self._base_dir, self._TEST_CONFIG_FILE_NAME)

    @staticmethod
    def _clamp_cost_factor(cost_factor: Optional[float]) -> float:
        if cost_factor is None:
            return 1.0
        try:
            value = float(cost_factor)
        except (TypeError, ValueError):
            return 1.0
        return max(0.0001, min(value, 100.0))

    @staticmethod
    def _parse_bool(value: Optional[str]) -> bool:
        if value is None:
            return False
        return value.lower() in {"1", "true", "yes", "on"}

    def _parse_float(self, value: Optional[str], default: float) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except (TypeError, ValueError):
            return default

    def _decode_api_base_url(self, enc: str) -> str:
        raw = base64.b64decode(enc.encode("utf-8"))
        key = 0
        for part in self._ENC_KEY_PARTS:
            key ^= part
        data = bytes((b ^ key) for b in raw)
        return data.decode("utf-8")

    def _get_default_base_url(self) -> str:
        return "".join(chr(c) for c in self._DEFAULT_API_BASE_URL_CODEPOINTS)

    def _normalize_route_key(self, route: Optional[str]) -> str:
        if not route:
            return self._DEFAULT_ROUTE_KEY
        raw_key = str(route).strip()
        if not raw_key:
            return self._DEFAULT_ROUTE_KEY
        key = self._ROUTE_LABEL_TO_KEY.get(raw_key)
        if key is None:
            lowered_key = raw_key.lower()
            key = self._ROUTE_LABEL_TO_KEY.get(lowered_key, lowered_key)
        if key not in self._API_BASE_URLS_ENCODED:
            return self._DEFAULT_ROUTE_KEY
        return key

    def is_xinbao_test_route_label(self, route: Optional[str]) -> bool:
        if route is None:
            return False
        return str(route).strip() in self.XINBAO_TEST_ROUTE_LEGACY_LABELS

    def normalize_xinbao_test_route_label(self, route: Optional[str]) -> str:
        if self.is_xinbao_test_route_label(route):
            return self.XINBAO_TEST_ROUTE_LABEL
        return (route or "").strip()

    def get_xinbao_test_base_url(self) -> str:
        return self.XINBAO_TEST_BASE_URL

    def is_xinbao_test_base_url(self, base_url: Optional[str]) -> bool:
        if not base_url:
            return False
        candidate = str(base_url).strip().lower()
        return candidate.startswith(self.XINBAO_TEST_BASE_URL.lower())

    def get_route_base_url(self, route: Optional[str]) -> str:
        if self.is_xinbao_test_route_label(route):
            return self.get_xinbao_test_base_url()
        key = self._normalize_route_key(route)
        encoded = self._API_BASE_URLS_ENCODED.get(key, self._API_BASE_URLS_ENCODED[self._DEFAULT_ROUTE_KEY])
        return self._decode_api_base_url(encoded)

    def get_hidden_base_url(self) -> str:
        return self._decode_api_base_url(self._API_BASE_URLS_ENCODED["hidden"])

    def get_effective_api_base_url(self, route: Optional[str] = None) -> str:
        return self.get_route_base_url(route)

    def get_default_api_base_url(self) -> str:
        return self.get_route_base_url(self._DEFAULT_ROUTE_KEY)

    def _is_test_mode_enabled(self) -> bool:
        value = os.environ.get(self._TEST_MODE_ENV_VAR, "").strip().lower()
        return value in {"1", "true", "yes", "on"}

    def _load_test_section(self) -> Optional[Dict[str, str]]:
        if not self._is_test_mode_enabled():
            return None

        if not os.path.exists(self._test_config_path):
            return None

        parser = configparser.ConfigParser()
        try:
            parser.read(self._test_config_path, encoding="utf-8")
            if parser.has_section(self._TEST_CONFIG_SECTION):
                section = parser[self._TEST_CONFIG_SECTION]
                return {k: v for k, v in section.items()}
        except Exception as exc:  # pragma: no cover - 仅在日志输出时使用
            logger.warning(f"读取本地测试配置失败: {exc}")
        return None

    def _load_test_api_key(self) -> Optional[str]:
        section = self._load_test_section()
        if not section:
            return None
        api_key = section.get("api_key", "").strip()
        return self.sanitize_api_key(api_key)

    def _ensure_sample_config_exists(self) -> None:
        if os.path.exists(self._config_path):
            return
        config = configparser.ConfigParser()
        cpu_limit = max(1, os.cpu_count() or 4)
        default_workers = min(8, cpu_limit)
        config[self._CONFIG_SECTION] = {
            "api_key": self._DEFAULT_API_KEY,
            "modelscope_api_key": self._DEFAULT_API_KEY,
            "balance_cost_factor": "0.6",
            "max_workers": str(default_workers),
        }
        try:
            with open(self._config_path, "w", encoding="utf-8") as handle:
                config.write(handle)
            logger.success(f"已创建示例配置文件: {self._config_path}")
            logger.info("请编辑文件并填入你的 API Key")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"创建配置文件失败: {exc}")


    def sanitize_api_key(self, api_key: Optional[str]) -> Optional[str]:
        if not api_key:
            return None
        cleaned = api_key.strip()
        if not cleaned:
            return None
        normalized = cleaned.lower()
        compact = re.sub(r"[\s_-]+", "", normalized)
        if normalized in self._PLACEHOLDER_KEYS or compact in self._PLACEHOLDER_KEYS:
            return None
        return cleaned

    def load_api_key(self) -> str:
        test_api_key = self._load_test_api_key()
        if test_api_key:
            return test_api_key

        self._ensure_sample_config_exists()
        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    return parser.get(
                        self._CONFIG_SECTION,
                        "api_key",
                        fallback=self._DEFAULT_API_KEY
                    )
            except Exception as exc:
                logger.warning(f"读取配置文件失败: {exc}")
        return self._DEFAULT_API_KEY

    def load_modelscope_api_key(self) -> str:
        self._ensure_sample_config_exists()
        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    return parser.get(
                        self._CONFIG_SECTION,
                        "modelscope_api_key",
                        fallback=""
                    )
            except Exception as exc:
                logger.warning(f"读取配置文件失败: {exc}")
        return ""

    def load_cost_factor(self) -> float:
        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    value = parser.getfloat(
                        self._CONFIG_SECTION,
                        "balance_cost_factor",
                        fallback=0.6
                    )
                    return self._clamp_cost_factor(value)
            except Exception as exc:
                logger.warning(f"读取 config 中的 balance_cost_factor 失败: {exc}")
        return 0.6

    def clamp_cost_factor(self, cost_factor: Optional[float]) -> float:
        """对外暴露的 cost_factor 限幅接口。"""
        return self._clamp_cost_factor(cost_factor)

    def load_max_workers(self) -> int:
        cpu_limit = max(1, os.cpu_count() or 1)
        default_workers = min(8, cpu_limit)
        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    value = parser.getint(
                        self._CONFIG_SECTION,
                        "max_workers",
                        fallback=default_workers
                    )
                    return max(1, min(value, cpu_limit))
            except Exception as exc:
                logger.warning(f"读取 config 中的 max_workers 失败: {exc}")
        return default_workers

    def load_network_workers_cap(self) -> int:
        default_cap = 4
        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    value = parser.getint(
                        self._CONFIG_SECTION,
                        "network_workers_cap",
                        fallback=default_cap
                    )
                    return max(1, min(value, 8))
            except Exception as exc:
                logger.warning(f"读取 config 中的 network_workers_cap 失败: {exc}")
        return default_cap

    def load_kv_auth_config(self) -> KVAuthConfig:
        """读取 Edge KV 鉴权配置，提供默认值与健壮性兜底。"""
        parser = configparser.ConfigParser()
        enabled = True
        endpoint = self._KV_AUTH_DEFAULT_ENDPOINT
        token = None
        timeout_seconds = self._KV_AUTH_DEFAULT_TIMEOUT
        fail_open_on_5xx = True
        disable_ssl_verify = False

        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    section = parser[self._CONFIG_SECTION]
                    enabled = self._parse_bool(section.get("kv_auth_enabled", "true"))
                    endpoint_candidate = (section.get("kv_auth_endpoint", "") or "").strip()
                    if endpoint_candidate:
                        endpoint = endpoint_candidate
                    token_candidate = (section.get("kv_auth_token", "") or "").strip()
                    token = token_candidate or None
                    timeout_raw = section.get("kv_auth_timeout_seconds", "")
                    timeout_seconds = self._parse_float(timeout_raw, self._KV_AUTH_DEFAULT_TIMEOUT)
                    fail_open_on_5xx = self._parse_bool(section.get("kv_auth_fail_open_on_5xx", "true"))
                    disable_ssl_verify = self._parse_bool(section.get("kv_auth_disable_ssl_verify", "false"))
            except Exception as exc:
                logger.warning(f"读取鉴权配置失败，将使用默认设置: {exc}")

        endpoint = endpoint.strip() or self._KV_AUTH_DEFAULT_ENDPOINT
        timeout_seconds = max(1.0, min(timeout_seconds, 30.0))
        return KVAuthConfig(
            enabled=enabled,
            endpoint=endpoint,
            token=token,
            timeout_seconds=timeout_seconds,
            fail_open_on_5xx=fail_open_on_5xx,
            disable_ssl_verify=disable_ssl_verify,
        )

    def should_bypass_proxy(self) -> bool:
        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    value = parser.get(
                        self._CONFIG_SECTION,
                        "bypass_proxy",
                        fallback="false"
                    )
                    return self._parse_bool(str(value).strip())
            except Exception as exc:
                logger.warning(f"读取 config 中的 bypass_proxy 失败: {exc}")
        return False












