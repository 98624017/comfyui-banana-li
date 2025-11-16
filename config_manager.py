
import base64
import configparser
import os
import sys
import re
from typing import Dict, Optional

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from logger import logger


class ConfigManager:
    """集中管理 Banana Gemini 节点的配置与测试模式逻辑。"""

    _ENC_KEY_PARTS = (3, 4)
    _DEFAULT_API_BASE_URL_CODEPOINTS = [
        104, 116, 116, 112, 115, 58, 47, 47, 97, 112, 105, 46, 103, 108, 109, 98,
        105, 103, 109, 111, 100, 101, 108, 46, 109, 101
    ]

    _CONFIG_SECTION = "gemini"
    _CONFIG_KEY_API_BASE_URL_ENC = "api_base_url_enc"
    _TEST_CONFIG_FILE_NAME = "banana_gemini_test.local.ini"
    _TEST_CONFIG_SECTION = "gemini_test"
    _TEST_MODE_ENV_VAR = "BANANA_GEMINI_USE_LOCAL_TEST"
    _DEFAULT_API_KEY = "your-api-key-here"
    _PLACEHOLDER_KEYS = {
        "your-api-key-here",
        "your_api_key_here",
        "yourapikeyhere",
    }

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

    def _decode_api_base_url(self, enc: str) -> str:
        raw = base64.b64decode(enc.encode("utf-8"))
        key = 0
        for part in self._ENC_KEY_PARTS:
            key ^= part
        data = bytes((b ^ key) for b in raw)
        return data.decode("utf-8")

    def _get_default_base_url(self) -> str:
        return "".join(chr(c) for c in self._DEFAULT_API_BASE_URL_CODEPOINTS)

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

    def _load_test_base_url(self) -> Optional[str]:
        section = self._load_test_section()
        if not section:
            return None
        enc = section.get("api_base_url_enc", "").strip()
        if not enc:
            return None
        try:
            return self._decode_api_base_url(enc)
        except Exception as exc:  # pragma: no cover
            logger.warning(f"解码测试配置中的 api_base_url_enc 失败: {exc}")
            return None

    def _ensure_sample_config_exists(self) -> None:
        if os.path.exists(self._config_path):
            return
        config = configparser.ConfigParser()
        cpu_limit = max(1, os.cpu_count() or 4)
        default_workers = min(8, cpu_limit)
        config[self._CONFIG_SECTION] = {
            "api_key": self._DEFAULT_API_KEY,
            "balance_cost_factor": "0.6",
            "max_workers": str(default_workers),
            "verify_ssl": "true",
        }
        try:
            with open(self._config_path, "w", encoding="utf-8") as handle:
                config.write(handle)
            logger.success(f"已创建示例配置文件: {self._config_path}")
            logger.info("请编辑文件并填入你的 API Key")
        except Exception as exc:  # pragma: no cover
            logger.warning(f"创建配置文件失败: {exc}")

    def get_effective_api_base_url(self) -> str:
        test_base_url = self._load_test_base_url()
        if test_base_url:
            return test_base_url

        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    encoded = parser.get(
                        self._CONFIG_SECTION,
                        self._CONFIG_KEY_API_BASE_URL_ENC,
                        fallback=""
                    ).strip()
                    if encoded:
                        return self._decode_api_base_url(encoded)
            except Exception as exc:
                logger.warning(
                    f"读取 config 中的 {self._CONFIG_KEY_API_BASE_URL_ENC} 失败: {exc}"
                )

        return self._get_default_base_url()

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

    def should_verify_ssl(self) -> bool:
        parser = configparser.ConfigParser()
        if os.path.exists(self._config_path):
            try:
                parser.read(self._config_path, encoding="utf-8")
                if parser.has_section(self._CONFIG_SECTION):
                    value = parser.get(
                        self._CONFIG_SECTION,
                        "verify_ssl",
                        fallback="true"
                    )
                    return self._parse_bool(str(value).strip())
            except Exception as exc:
                logger.warning(f"读取 config 中的 verify_ssl 失败: {exc}")
        return True
