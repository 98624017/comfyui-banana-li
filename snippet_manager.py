import os
import json
import time
import uuid
import threading
from typing import Dict, List, Any, Optional
from aiohttp import web
import folder_paths
from datetime import datetime


try:
    from server import PromptServer
except ImportError:
    class _DummyPromptServer:
        instance = None
        routes = None
    PromptServer = _DummyPromptServer()

try:
    import tomllib
except ImportError:
    try:
        import toml as tomllib
    except ImportError:
        tomllib = None

class SnippetManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snippets.toml")
        if not tomllib:
             print("XinbaoPromptAssistantNode: Warning - Python 3.11+ or 'toml' package required for snippets. Using memory-only mode.")
        # 当缺少 TOML 解析器时，无法安全合并/读取已有 snippets.toml。
        # 为避免覆盖用户已有文件，默认禁止在无解析器且文件已存在时写盘；
        # 若文件不存在，则允许创建并在本进程内持续覆写（以当前内存缓存为准）。
        self._allow_overwrite_without_toml = not os.path.exists(self.file_path)
        self._last_mtime = 0
        self._cache = self._load()

    def _get_mtime(self) -> float:
        if os.path.exists(self.file_path):
            return os.path.getmtime(self.file_path)
        return 0.0

    def _reload_if_needed(self):
        """Reloads cache if file on disk has changed."""
        if not tomllib:
            return
        current_mtime = self._get_mtime()
        if current_mtime != self._last_mtime:
            # File changed (externally or by us), reload
            print(f"XinbaoPromptAssistant: File changed, reloading snippets...")
            self._cache = self._load()

    def _load(self) -> List[Dict]:
        if not tomllib:
            return []
        if not os.path.exists(self.file_path):
            return []
        try:
            with open(self.file_path, "rb") as f:
                data = tomllib.load(f)
                snippets = data.get("snippets", [])
                # Migration: Convert timestamp to string if needed
                for s in snippets:
                    if isinstance(s.get("created_at"), (int, float)):
                        try:
                            ts = float(s["created_at"]) / 1000.0
                            s["created_at"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
                        except Exception:
                            pass 
                return snippets
        except Exception as e:
            print(f"XinbaoPromptAssistant: Failed to load snippets: {e}")
            return []
        finally:
            self._last_mtime = self._get_mtime()

    def _save(self):
        # Manual TOML serializer to avoid 'tomli-w' dependency
        if not tomllib and os.path.exists(self.file_path) and not self._allow_overwrite_without_toml:
            print("XinbaoPromptAssistant: Warning - TOML 解析器不可用，已跳过保存以避免覆盖现有 snippets.toml。")
            return
        header = """# ==========================================
# 🍌 Banana Snippets 配置文件
# ==========================================
# 这是一个 TOML 格式的文件，用于存储提示词片段。
# 您可以手动编辑此文件，但请务必保持格式正确。
#
# 字段说明:
# - [[snippets]]: 定义一个新的片段块
# - id: 唯一标识符 (任意唯一字符串) [必选] (ComfyUI 界面添加时自动生成)
# - content: 提示词内容 [必选]
# - category: 分类名称 (例如: "风格", "人物") [可选] (默认: "默认")
# - color: 显示颜色 (Hex 格式) [可选] (默认: "#ffffff")
#   - #F44336 (Red)
#   - #E91E63 (Pink)
#   - #9C27B0 (Purple)
#   - #673AB7 (Deep Purple)
#   - #3F51B5 (Indigo)
#   - #2196F3 (Blue)
#   - #009688 (Teal)
#   - #4CAF50 (Green)
#   - #FF9800 (Orange)
#   - #795548 (Brown)
# - created_at: 创建时间 (格式: YYYY-MM-DD HH:MM) [可选] (ComfyUI 界面添加时自动生成)
#
# 示例:
# [[snippets]]
# id = "my-snippet-001"
# content = "best quality, masterpiece, 8k"
# category = "风格"
# color = "#4CAF50"
# created_at = "2024-06-01 12:00"
# ==========================================

"""
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                f.write(header)
                for snippet in self._cache:
                    f.write("[[snippets]]\n")
                    f.write(f"id = {json.dumps(snippet['id'])}\n")
                    f.write(f"content = {json.dumps(snippet['content'], ensure_ascii=False)}\n")
                    f.write(f"category = {json.dumps(snippet.get('category', '默认'), ensure_ascii=False)}\n")
                    f.write(f"color = {json.dumps(snippet.get('color', '#ffffff'))}\n")
                    # Ensure created_at is stringified properly if it's a string, or just fallback
                    cat_val = snippet.get('created_at', '')
                    if isinstance(cat_val, str):
                        f.write(f"created_at = {json.dumps(cat_val)}\n\n")
                    else:
                        f.write(f"created_at = {cat_val}\n\n")
            self._allow_overwrite_without_toml = True
        except Exception as e:
            print(f"XinbaoPromptAssistant: Failed to save snippets: {e}")

    def list_snippets(self) -> List[Dict]:
        with self.lock:
            self._reload_if_needed()
            return list(self._cache)

    def add_snippet(self, content: str, category: str = "默认", color: str = "#ffffff") -> Dict:
        snippet = {
            "id": str(uuid.uuid4()),
            "content": content,
            "category": category,
            "color": color,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        with self.lock:
            self._reload_if_needed()
            self._cache.append(snippet)
            self._save()
        return snippet

    def ensure_snippet(
        self,
        content: str,
        category: str = "默认",
        color: str = "#ffffff",
        *,
        update_existing: bool = False,
    ) -> Optional[Dict]:
        """
        确保某个 content 的片段存在（按 content 幂等）。

        - 若已存在相同 content：默认直接返回；可选 update_existing=True 则同步更新 category/color。
        - 若不存在：创建新片段并写入。

        说明：按 content 去重更符合“提示词片段”语义，避免反复创建相同片段造成碎片化。
        """
        normalized_content = (content or "").strip()
        if not normalized_content:
            return None

        normalized_category = (category or "默认").strip() or "默认"
        normalized_color = (color or "#ffffff").strip() or "#ffffff"

        with self.lock:
            self._reload_if_needed()
            for snippet in self._cache:
                existing_content = (snippet.get("content") or "").strip()
                if existing_content != normalized_content:
                    continue

                if update_existing:
                    changed = False
                    if (snippet.get("category") or "").strip() != normalized_category:
                        snippet["category"] = normalized_category
                        changed = True
                    if (snippet.get("color") or "").strip() != normalized_color:
                        snippet["color"] = normalized_color
                        changed = True
                    if changed:
                        self._save()
                return snippet

            snippet = {
                "id": str(uuid.uuid4()),
                "content": normalized_content,
                "category": normalized_category,
                "color": normalized_color,
                "created_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
            }
            self._cache.append(snippet)
            self._save()
            return snippet

    def update_snippet(self, snippet_id: str, content: str, category: str, color: str) -> Optional[Dict]:
        with self.lock:
            self._reload_if_needed()
            for snippet in self._cache:
                if snippet["id"] == snippet_id:
                    snippet["content"] = content
                    snippet["category"] = category
                    snippet["color"] = color
                    self._save()
                    return snippet
        return None

    def delete_snippet(self, snippet_id: str) -> bool:
        with self.lock:
            self._reload_if_needed()
            initial_len = len(self._cache)
            self._cache = [s for s in self._cache if s["id"] != snippet_id]
            if len(self._cache) != initial_len:
                self._save()
                return True
        return False

# Global Instance
SNIPPET_MANAGER = SnippetManager()

# --- API Routes ---
_ROUTE_REGISTERED = False
_ROUTE_TIMER: threading.Timer | None = None

def _ensure_api_routes(prompt_server_provider):
    global _ROUTE_REGISTERED, _ROUTE_TIMER
    if _ROUTE_REGISTERED:
        return
    
    prompt_server = prompt_server_provider()
    if prompt_server is None:
        if _ROUTE_TIMER is None or not _ROUTE_TIMER.is_alive() or threading.current_thread() is _ROUTE_TIMER:
            timer = threading.Timer(1.0, lambda: _ensure_api_routes(prompt_server_provider))
            timer.daemon = True
            _ROUTE_TIMER = timer
            timer.start()
        return

    @prompt_server.routes.get("/banana/snippets")
    async def list_snippets_handler(request):
        snippets = SNIPPET_MANAGER.list_snippets()
        return web.json_response({"success": True, "data": snippets})

    @prompt_server.routes.post("/banana/snippets")
    async def add_snippet_handler(request):
        try:
            data = await request.json()
            snippet_id = data.get("id")
            content = data.get("content", "")
            category = data.get("category", "默认")
            color = data.get("color", "#ffffff")

            if snippet_id:
                # Update
                result = SNIPPET_MANAGER.update_snippet(snippet_id, content, category, color)
                if result:
                    return web.json_response({"success": True, "data": result})
                else:
                    return web.json_response({"success": False, "message": "Snippet not found"}, status=404)
            else:
                # Add
                result = SNIPPET_MANAGER.add_snippet(content, category, color)
                return web.json_response({"success": True, "data": result})
        except Exception as e:
            return web.json_response({"success": False, "message": str(e)}, status=500)

    @prompt_server.routes.delete("/banana/snippets")
    async def delete_snippet_handler(request):
        try:
            data = await request.json() # Support JSON body for DELETE
            snippet_id = data.get("id")
        except:
             snippet_id = request.rel_url.query.get("id")

        if not snippet_id:
             return web.json_response({"success": False, "message": "Missing id"}, status=400)

        success = SNIPPET_MANAGER.delete_snippet(snippet_id)
        if success:
            return web.json_response({"success": True})
        else:
            return web.json_response({"success": False, "message": "Not found"}, status=404)
            
    _ROUTE_REGISTERED = True

# Initialize Routes
_ensure_api_routes(lambda: getattr(PromptServer, "instance", None))

class XinbaoPromptAssistantNode:
    """
    心宝提示词助手节点
    提供提示词片段管理和拼接功能
    """
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
               "text": ("STRING", {"multiline": True, "dynamicPrompts": True, "default": ""}),
            },
            "optional": {
                "prefix_text": ("STRING", {"multiline": True, "dynamicPrompts": True, "forceInput": True, "default": ""}),
            }
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    FUNCTION = "execute"
    CATEGORY = "❤️‍🔥心宝专用/工具"
    
    def execute(self, text, prefix_text=""):
        # 兼容旧工作流/异常输入：ComfyUI 可能会把缺失的 widget 值反序列化为 None
        def _normalize_str(value) -> str:
            if value is None:
                return ""
            if isinstance(value, str):
                return value
            return str(value)

        p = _normalize_str(prefix_text).strip()
        t = _normalize_str(text).strip()

        final_text = p
        if t:
            if final_text and not final_text.endswith(","):
                final_text += ", "
            elif final_text and final_text.endswith(","):
                final_text += " "
            final_text += t

        return (final_text,)

NODE_CLASS_MAPPINGS = {
    "XinbaoPromptAssistantNode": XinbaoPromptAssistantNode
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "XinbaoPromptAssistantNode": "心宝❤提示词助手"
}
