"""验证多模态描述节点恢复为图床上传优先，再回退 base64。"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]


def _install_stub_modules() -> None:
    if "torch" not in sys.modules:
        torch_stub = types.ModuleType("torch")

        class _Tensor:  # noqa: N801 - 与 torch.Tensor 命名保持一致
            pass

        torch_stub.Tensor = _Tensor  # type: ignore[attr-defined]
        sys.modules["torch"] = torch_stub

    if "comfy" not in sys.modules:
        comfy_stub = types.ModuleType("comfy")
        comfy_utils_stub = types.ModuleType("comfy.utils")
        comfy_model_management_stub = types.ModuleType("comfy.model_management")
        comfy_model_management_stub.throw_exception_if_processing_interrupted = lambda: None  # type: ignore[attr-defined]
        comfy_model_management_stub.InterruptProcessingException = RuntimeError  # type: ignore[attr-defined]

        comfy_stub.utils = comfy_utils_stub  # type: ignore[attr-defined]
        comfy_stub.model_management = comfy_model_management_stub  # type: ignore[attr-defined]

        sys.modules["comfy"] = comfy_stub
        sys.modules["comfy.utils"] = comfy_utils_stub
        sys.modules["comfy.model_management"] = comfy_model_management_stub


_install_stub_modules()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import xinbao_modelscope as module


class XinbaoModelScopeCaptionPayloadTest(unittest.TestCase):
    def test_xinbao_test_channel_prefers_uploaded_url_in_payload(self) -> None:
        node = module.XinbaoModelScopeCaption()
        image = object()
        captured_payloads: list[dict] = []

        def fake_send_caption_request_with_routes(
            _session,
            payload,
            _headers,
            _base_urls,
            _selected_channel,
            _model_name,
            ensure_not_interrupted=None,
        ):
            captured_payloads.append(payload)
            if ensure_not_interrupted is not None:
                ensure_not_interrupted()
            return object()

        with mock.patch.object(
            module,
            "_resolve_xinbao_test_caption_key",
            return_value=("test-key", ["https://xinbao.example.com"], "unit-test"),
        ), mock.patch.object(
            module,
            "_create_session",
            return_value=object(),
        ), mock.patch.object(
            module,
            "_build_headers",
            return_value={"Authorization": "Bearer test-key"},
        ), mock.patch.object(
            module,
            "_normalize_model_for_channel",
            return_value="gemini-3-flash-c",
        ), mock.patch.object(
            module,
            "ImageUploader",
            return_value=object(),
        ), mock.patch.object(
            module,
            "_tensor_to_optimized_data_url",
            side_effect=AssertionError("不应再优先走 optimized data URL"),
        ), mock.patch.object(
            module,
            "_upload_caption_image",
            return_value="https://cdn.example.com/caption.webp",
        ), mock.patch.object(
            module,
            "_send_caption_request_with_routes",
            side_effect=fake_send_caption_request_with_routes,
        ), mock.patch.object(
            module,
            "_parse_json_response_or_raise",
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": "图片描述结果",
                        }
                    }
                ]
            },
        ):
            result = node.caption(
                image=image,
                channel=module.CHANNEL_XINBAO_TEST,
                banana_api_key="test-key",
                user_prompt="请描述图片",
            )

        self.assertEqual(result, ("图片描述结果",))
        self.assertEqual(len(captured_payloads), 1)
        payload = captured_payloads[0]
        self.assertEqual(payload["messages"][0]["role"], "system")
        user_content = payload["messages"][1]["content"]
        self.assertEqual(user_content[0], {"type": "text", "text": "请描述图片"})
        self.assertEqual(
            user_content[1],
            {
                "type": "image_url",
                "image_url": {"url": "https://cdn.example.com/caption.webp"},
            },
        )


if __name__ == "__main__":
    unittest.main()
