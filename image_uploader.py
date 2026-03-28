"""
图床上传工具模块

提供统一的图片上传接口，支持多个图床服务商。
适用于需要将本地图片上传到公网的节点。
"""

from __future__ import annotations

import io
import json
from typing import Optional, Tuple, List, Callable

import threading
import time as import_time
import requests
from PIL import Image

MAX_IMAGE_SIZE = 5 * 1024 * 1024  # 5MB

# 复用现有常量
UPLOAD_TIMEOUT = 15.0
UPLOAD_RETRIES = 1


class ImageUploadError(Exception):
    """图片上传失败异常"""
    pass


class ImageUploader:
    """
    图床上传器
    
    使用方法:
        uploader = ImageUploader()
        url = uploader.upload(image_bytes)
    """
    
    # 全局内存缓存: {md5_hash: (url, timestamp)}
    # URL 最长复用 2.5 小时；超过后必须重新上传，避免继续使用临时图链。
    _CACHE: dict = {}
    _CACHE_TTL = 9000.0

    def __init__(
        self,
        session: Optional[requests.Session] = None,
        logger=None,
        interrupt_checker: Optional[Callable[[], None]] = None,
    ):
        """
        初始化上传器
        
        Args:
            session: 可选的 requests.Session, 用于复用连接
            logger: 可选的日志记录器
            interrupt_checker: 可选的中断检查函数 (e.g. comfy.model_management.throw_exception_if_processing_interrupted)
        """
        self.session = session or requests.Session()
        self.logger = logger
        self.interrupt_checker = interrupt_checker
        
    def _log_info(self, msg: str) -> None:
        if self.logger:
            self.logger.info(msg)
            
    def _log_warning(self, msg: str) -> None:
        if self.logger:
            self.logger.warning(msg)
            
    def _log_success(self, msg: str) -> None:
        if self.logger and hasattr(self.logger, 'success'):
            self.logger.success(msg)
        elif self.logger:
            self.logger.info(msg)

    def _ensure_not_interrupted(self) -> None:
        """检查是否被中断"""
        if self.interrupt_checker:
            self.interrupt_checker()
    def _interruptible_request(
        self,
        method: str,
        url: str,
        **kwargs
    ) -> requests.Response:
        """
        支持中断的 requests 请求封装。
        在后台线程执行请求，主线程轮询中断标志。
        """
        if not self.interrupt_checker:
            return self.session.request(method, url, **kwargs)

        done_event = threading.Event()
        result_holder = {}
        
        def _target():
            try:
                result_holder["resp"] = self.session.request(method, url, **kwargs)
            except Exception as e:
                result_holder["exc"] = e
            finally:
                done_event.set()

        t = threading.Thread(target=_target, daemon=True)
        t.start()
        
        # 轮询等待
        while not done_event.wait(timeout=0.2):
            self._ensure_not_interrupted()
            
        # 二次确认（防止刚好在 wait 结束时被中断）
        self._ensure_not_interrupted()
        
        if "exc" in result_holder:
            raise result_holder["exc"]
            
        return result_holder.get("resp")

    def _upload_to_uguu(
        self,
        image_bytes: bytes,
        filename: str = "image.png",
        content_type: str = "image/png",
    ) -> str:
        """上传到 Uguu.se (性能优异: 100% 成功率, 1.5s 平均)"""
        # Uguu expects 'files[]' as key
        files = {"files[]": (filename or "image.png", io.BytesIO(image_bytes), content_type or "image/png")}
        # resp = self.session.post(
        #     "https://uguu.se/upload",
        #     files=files,
        #     timeout=(UPLOAD_TIMEOUT, UPLOAD_TIMEOUT),
        #     headers={"User-Agent": "ComfyUI-Banana/1.0"},
        # )
        resp = self._interruptible_request(
            "POST",
            "https://uguu.se/upload",
            files=files,
            timeout=(UPLOAD_TIMEOUT, UPLOAD_TIMEOUT),
            headers={"User-Agent": "ComfyUI-Banana/1.0"},
        )
        if resp.status_code != 200:
            msg = resp.text.strip()[:200] if resp.text else "无响应内容"
            raise ImageUploadError(f"Uguu.se 返回异常: HTTP {resp.status_code} [{msg}]")
        
        try:
            payload = resp.json()
            if payload.get("success"):
                files_data = payload.get("files", [])
                if files_data and len(files_data) > 0:
                    url = files_data[0].get("url")
                    if url:
                        return url
            raise ImageUploadError(f"Uguu.se 返回无效数据: {payload}")
        except ValueError:
            raise ImageUploadError(f"Uguu.se 响应解析失败: {resp.text[:100]}")

    
    def _upload_to_kefan(
        self,
        image_bytes: bytes,
        filename: str = "image.jpg",
        content_type: str = "image/jpeg",
    ) -> str:
        """上传到 ai.kefan.cn"""
        files = {"file": (filename or "image.jpg", io.BytesIO(image_bytes), content_type or "image/jpeg")}
        # resp = self.session.post(
        #     "https://ai.kefan.cn/api/upload/local",
        #     files=files,
        #     timeout=(UPLOAD_TIMEOUT, UPLOAD_TIMEOUT),
        # )
        resp = self._interruptible_request(
            "POST",
            "https://ai.kefan.cn/api/upload/local",
            files=files,
            timeout=(UPLOAD_TIMEOUT, UPLOAD_TIMEOUT),
        )
        if resp.status_code != 200:
            msg = resp.text.strip()[:200] if resp.text else "无响应内容"
            raise ImageUploadError(f"kefan 返回异常：HTTP {resp.status_code} [{msg}]")
        try:
            payload = resp.json()
            if payload.get("success") and payload.get("data"):
                url = payload["data"]
                if not url.startswith("http"):
                    raise ImageUploadError(f"kefan 返回的 URL 格式无效: {url[:100]}")
                return url
            else:
                raise ImageUploadError(f"kefan 返回格式异常: {payload}")
        except ValueError as e:
            raise ImageUploadError(f"kefan JSON 解析失败: {e}")

    def _upload_to_tmpfiles_file(self, file_bytes: bytes, filename: str, content_type: str) -> str:
        """上传到 tmpfiles.org"""
        files = {"file": (filename, io.BytesIO(file_bytes), content_type)}
        resp = self._interruptible_request(
            "POST",
            "https://tmpfiles.org/api/v1/upload",
            files=files,
            timeout=(UPLOAD_TIMEOUT * 2, UPLOAD_TIMEOUT * 2),
        )
        if resp.status_code != 200:
            msg = resp.text.strip()[:200] if resp.text else "无响应内容"
            raise ImageUploadError(f"tmpfiles 返回异常：HTTP {resp.status_code} [{msg}]")
        
        try:
            payload = resp.json()
            if payload.get("status") == "success" and payload.get("data"):
                url = payload["data"].get("url")
                if url and url.startswith("http"):
                    # 转换为直接下载链接
                    # https://tmpfiles.org/123/name.png -> https://tmpfiles.org/dl/123/name.png
                    if "tmpfiles.org/" in url and "/dl/" not in url:
                        return url.replace("tmpfiles.org/", "tmpfiles.org/dl/")
                    return url
            raise ImageUploadError(f"tmpfiles 返回无效数据: {payload}")
        except ValueError as e:
            raise ImageUploadError(f"tmpfiles JSON 解析失败: {e}")

    
    def _get_cache_key(self, data: bytes, resize_limit: Optional[int] = None) -> str:
        import hashlib
        md5 = hashlib.md5(data).hexdigest()
        if resize_limit:
            return f"{md5}_limit_{resize_limit}"
        return md5

    def _probe_host(self, url: str, timeout: float = 1.5) -> bool:
        """
        快速探测主机连通性
        Args:
            url: 目标 URL
            timeout: 超时时间 (秒)
        """
        # 部分地区 Uguu.se 可能对 HEAD 返回 4xx/5xx 或被 Cloudflare 挡下，但实际可访问。
        # 这里只要能建立 TCP 并收到任意 HTTP 响应即可视为连通，避免误判。
        try:
            resp = self.session.head(
                url,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": "ComfyUI-Banana/1.0"},
            )
            if resp is not None and resp.status_code:
                return True
        except Exception:
            pass

        try:
            with self.session.get(
                url,
                stream=True,
                timeout=timeout,
                allow_redirects=True,
                headers={"User-Agent": "ComfyUI-Banana/1.0"},
            ) as resp:
                return resp is not None and resp.status_code
        except Exception:
            return False

    def _check_url_availability(self, url: str) -> bool:
        """
        快速检查 URL 是否可用
        策略: 优先 HEAD 请求，失败则尝试流式 GET (只读 header)
        """
        if not url or not url.startswith("http"):
            return False
        
        try:
            # 1. 尝试 HEAD 请求 (最快，不下载 body)
            resp = self.session.head(url, timeout=3.0)
            if 200 <= resp.status_code < 400:
                return True
                
            # 2. 如果 HEAD 被拒绝 (405) 或其他情况，尝试 GET 但不下载内容 (stream=True)
            if resp.status_code == 405:
                with self.session.get(url, stream=True, timeout=5.0) as r:
                    return 200 <= r.status_code < 400
                    
            return False
        except Exception:
            # 连接超时、DNS 失败等均视为不可用
            return False

    def process_image(self, image_bytes: bytes, resize_limit: Optional[int] = None) -> bytes:
        """
        公开的图片处理方法 (缩放/格式转换)
        """
        return self._smart_compress(image_bytes, resize_limit=resize_limit)

    def _smart_compress(self, image_bytes: bytes, resize_limit: Optional[int] = None) -> bytes:
        """
        核心图片处理逻辑
        
        策略:
        1. 默认 (V1 / 未开启限制): 不做任何处理，直接返回原图 (即使超过 5MB)。
        2. 开启限制 (V2): 仅当图片 > 5MB 时，执行长边缩放 (Lanczos)，并尽量保持原格式保存。
        """
        # 1. 如果没有指定缩放限制，根据“旧逻辑无任何处理”要求，直接返回
        if not resize_limit or resize_limit <= 0:
            return image_bytes

        # 2. 如果指定了限制，但图片并未超过 5MB，也直接返回
        if len(image_bytes) <= MAX_IMAGE_SIZE:
            return image_bytes

        # 3. 超过 5MB 且开启了缩放限制 -> 执行缩放
        orig_size_mb = len(image_bytes) / (1024 * 1024)
        try:
            img = Image.open(io.BytesIO(image_bytes))
            # [关键修复] resize 会丢失 format 信息，必须先保存
            original_format = img.format
            w, h = img.size
            long_edge = max(w, h)
            
            # 只有当实际长边确实超过限制才缩放，否则只是转存一下没意义且可能增大体积
            if long_edge > resize_limit:
                scale = resize_limit / long_edge
                new_w, new_h = int(w * scale), int(h * scale)
                self._log_info(f"⚡ [V2] 检测到大图 ({orig_size_mb:.2f}MB > 5MB)，执行缩放: {w}x{h} -> {new_w}x{new_h} (Limit={resize_limit})")
                img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
            else:
                return image_bytes

            bio = io.BytesIO()
            # 尽量保持原格式
            save_format = original_format or "JPEG" # 兜底 JPEG
            
            # 兼容模式处理
            if save_format.upper() in ("JPEG", "JPG") and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            
            # 使用较高质量保存，避免用户反感的“压缩”
            kwargs = {}
            if save_format.upper() in ("JPEG", "JPG"):
                kwargs["quality"] = 95
            elif save_format.upper() == "PNG":
                # PNG 也可以适当优化，compress_level=1 速度快但体积大，默认 6 均衡
                pass

            img.save(bio, format=save_format, **kwargs)
            new_data = bio.getvalue()
            
            new_size_mb = len(new_data) / (1024 * 1024)
            self._log_success(f"✅ [V2] 缩放完成: {orig_size_mb:.2f}MB -> {new_size_mb:.2f}MB (Format={save_format})")
            return new_data

        except Exception as e:
            self._log_warning(f"❌ 缩放处理失败 ({e})，将使用原图")
            return image_bytes

    def upload(
        self,
        image_bytes: bytes,
        providers: Optional[List[Tuple[str, Callable[[bytes], str]]]] = None,
        resize_limit: Optional[int] = None,
        filename: Optional[str] = None,
        content_type: Optional[str] = None,
    ) -> str:
        """
        上传图片到公网图床 (带缓存)
        
        Args:
            image_bytes: 图片的二进制数据
            providers: 可选的服务商列表 [(名称, 上传函数), ...]
                      默认使用 x0.at 和 ai.kefan.cn
            resize_limit: 可选的长边限制，用于智能压缩策略
            filename: 可选文件名（用于 multipart 元信息）
            content_type: 可选 MIME 类型（用于 multipart 元信息）
        
        Returns:
            str: 图片的公网 URL
        """
        # --- 优化: 优先使用原图 MD5 进行缓存预检 ---
        # 这样即使是 18MB 的大图，如果之前传过，第二次调用时连压缩计算都会跳过
        raw_cache_key = self._get_cache_key(image_bytes, resize_limit)
        now = import_time.time()
        max_cache_age = float(self._CACHE_TTL)

        cached = self._CACHE.get(raw_cache_key)
        if isinstance(cached, tuple) and len(cached) == 2:
            url, timestamp = cached
            age = now - float(timestamp or 0.0)

            if age < max_cache_age:
                self._log_success("♻️ 复用已上传图片 URL (原图 MD5 命中缓存 < 2.5h)")
                return url

            self._log_info("⌛ 原图缓存链接已超过 2.5 小时，强制重新上传")
            # 并发场景下缓存可能已被其它线程清理/更新，使用条件 pop 避免 KeyError/误删新值
            if self._CACHE.get(raw_cache_key) == cached:
                self._CACHE.pop(raw_cache_key, None)

        # 缓存未命中，执行智能压缩处理 (仅针对大图)
        image_bytes = self._smart_compress(image_bytes, resize_limit=resize_limit)
        
        # --- 注意: 我们依然统一使用 raw_cache_key (原图MD5) 作为存储键 ---
        # 这样下次传入同样的“原图”时，就能在压缩前被拦截

        upload_content_type = (content_type or "").strip() or None
        upload_filename = (filename or "").strip() or None
        if upload_filename is None and upload_content_type:
            ext_map = {
                "image/jpeg": "jpg",
                "image/jpg": "jpg",
                "image/png": "png",
                "image/webp": "webp",
                "image/gif": "gif",
            }
            upload_filename = f"image.{ext_map.get(upload_content_type.lower(), 'bin')}"

        if providers is None:
            # 优化：优先探测 Uguu.se 连通性 (1.5s 超时)
            # 如果不通（如无梯子），直接跳过 Uguu，避免 15s+ 的上传超时等待
            if self._probe_host("https://uguu.se", timeout=1.5):
                providers = [
                    (
                        "uguu.se",
                        lambda data: self._upload_to_uguu(
                            data,
                            filename=upload_filename or "image.png",
                            content_type=upload_content_type or "image/png",
                        ),
                    ),
                    (
                        "kefan",
                        lambda data: self._upload_to_kefan(
                            data,
                            filename=upload_filename or "image.jpg",
                            content_type=upload_content_type or "image/jpeg",
                        ),
                    ),
                ]
            else:
                self._log_warning("⚠️ 检测到 Uguu.se 连通性差，自动切换至国内备用图床")
                providers = [
                    (
                        "kefan",
                        lambda data: self._upload_to_kefan(
                            data,
                            filename=upload_filename or "image.jpg",
                            content_type=upload_content_type or "image/jpeg",
                        ),
                    ),
                ]
        
        last_error = ""
        for name, uploader in providers:
            for attempt in range(UPLOAD_RETRIES + 1):
                self._ensure_not_interrupted()
                try:
                    url = uploader(image_bytes)
                    self._log_success(f"图床上传成功（{name}）")
                    # 写入缓存 (使用原图 MD5 键)
                    self._CACHE[raw_cache_key] = (url, import_time.time())
                    return url
                except Exception as exc:
                    last_error = str(exc)
                    if attempt < UPLOAD_RETRIES:
                        self._log_warning(f"{name} 上传失败，正在重试... ({exc})")
                        continue
                    self._log_warning(f"{name} 上传失败：{exc}")
        
        raise ImageUploadError(f"图床上传失败：{last_error or '未知错误'}")

    def upload_media(self, file_bytes: bytes, filename: str, content_type: str) -> str:
        # 上传任意媒体文件到公网图床 (仅支持 tmpfiles)
        # 说明: ai.kefan.cn 仅支持图片
        return self.upload(
            file_bytes,
            providers=[
                ("tmpfiles", lambda data: self._upload_to_tmpfiles_file(data, filename, content_type)),
            ],
        )
