"""
脱敏与错误摘要（从原 `Xinbao_Video_Generator.py` 迁移）。

所有函数保持原签名与行为，避免影响日志与报错语义。
"""

from __future__ import annotations

import re


def _mask_key(api_key: str) -> str:
    cleaned = (api_key or "").strip()
    if len(cleaned) <= 6:
        return "***"
    return f"{cleaned[:3]}***{cleaned[-3:]}"


def _mask_url(url: str) -> str:
    return "<hidden-url>" if url else ""


def _clean_url(url: str) -> str:
    if not url:
        return url
    return url.strip().rstrip(").]>\\n\\r\"'")


def _mask_text(text: str) -> str:
    """替换文本中的 API Base URL 和敏感源站信息，视频下载链接不脱敏。"""
    if not text:
        return ""
    # 匹配所有敏感源站地址
    sensitive_patterns = [
        r"https?://[a-z-]*api[a-z0-9-]*\.aabao\.top[^\s)>\]]*",  # hk-api.aabao.top, cf-api.aabao.top, api.aabao.top
        r"https?://[a-z0-9-]*\.xinbaoai\.com[^\s)>\]]*",  # api.xinbaoai.com 等
        r"https?://api666\.zeabur\.app[^\s)>\]]*",  # 隐藏线路
        r"https?://api[^\s)>\]]+",  # 其他以 api 开头的 URL（兜底）
    ]
    result = text
    for pattern in sensitive_patterns:
        result = re.sub(pattern, "<hidden-url>", result, flags=re.IGNORECASE)
    return result


def _extract_error_from_html(html: str, status_code: int) -> str:
    """
    从 HTML 错误页面提取关键信息（如 Cloudflare 524 超时）。

    返回精简的错误摘要，避免输出大段无用的 HTML 标签。
    """
    if not html:
        return ""

    # Cloudflare 错误页面：提取标题和核心说明
    # 典型格式: <title>xxx.domain | 524: A timeout occurred</title>
    title_match = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    title = ""
    if title_match:
        title = title_match.group(1).strip()
        # 隐藏域名，只保留错误码和描述
        # 例如: "xinbaoapi.dpdns.org | 524: A timeout occurred" -> "524: A timeout occurred"
        if "|" in title:
            title = title.split("|", 1)[-1].strip()

    # 常见 Cloudflare 错误码映射
    cf_error_map = {
        520: "源站返回未知错误",
        521: "源站拒绝连接",
        522: "源站连接超时",
        523: "源站不可达",
        524: "源站响应超时",
        525: "SSL 握手失败",
        526: "无效 SSL 证书",
        527: "Railgun 错误",
        530: "源站 DNS 错误",
    }

    # 优先使用映射的中文描述
    if status_code in cf_error_map:
        return f"{status_code}: {cf_error_map[status_code]}"

    # 次选：使用页面标题
    if title:
        return title

    # 兜底：HTTP 状态码
    return f"远端异常 (HTTP {status_code})"

