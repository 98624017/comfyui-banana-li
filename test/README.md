# 测试脚本目录

本目录包含用于开发和调试的测试脚本，不包含在发布版本中。

## 📁 目录结构

### 性能测试
- `test_performance.py` - 早期性能测试（需 ComfyUI 依赖）
- `test_performance_simple.py` - 简化版性能测试
- `test_performance_api.py` - 双图输入性能测试（完整版）
- `test_additional_params.py` - 测试额外参数的性能影响

### API 格式测试
- `test_formats.py` - 测试多种请求格式
- `test_simple_text.py` - 纯文本生图测试
- `test_aspect_ratio.py` - 测试 aspectRatio 参数位置
- `test_correct_format.py` - 测试修正后的格式
- `test_imageconfig.py` - 测试 imageConfig 格式（发现正确格式）
- `test_all_variations.py` - 测试所有参数变体

### 修复验证
- `test_api_after_fix.py` - 修复后的 API 测试
- `test_fixed_params_api.py` - 固定参数添加后的测试
- `verify_fix.py` - 验证 imageConfig 修复
- `verify_fixed_params.py` - 验证固定参数添加

### 演示脚本
- `show_request_body.py` - 展示插件构造的请求体
- `show_full_json.py` - 展示完整 JSON 格式
- `show_complete_request.py` - 展示完整请求体示例

## 🔧 使用方法

所有测试脚本可直接使用 Python 运行：

```bash
# 使用 ComfyUI 的 Python 环境
F:/ComfyUI-aki-v1.3/env/python.exe test/test_*.py

# 或在虚拟环境中
python test/test_*.py
```

## ⚠️ 注意事项

1. **API 密钥**：部分脚本包含测试用的 API 密钥，请勿泄露
2. **依赖**：某些脚本需要 ComfyUI 依赖，某些只需要 requests 和 PIL
3. **网络**：测试脚本会发起真实的 API 请求，需要网络连接

## 📊 关键测试结果

### imageConfig 格式修复
- **问题**：`imageGenerationConfig` 放置在顶层导致 400 错误
- **修复**：`imageConfig` 应放在 `generationConfig` 里面
- **验证脚本**：`verify_fix.py`, `test_api_after_fix.py`

### 固定参数优化
- **添加**：`maxOutputTokens: 8192`, `responseModalities: ["IMAGE"]`
- **效果**：可能提升性能约 56% (18.86秒 → 8.35秒)
- **验证脚本**：`verify_fixed_params.py`, `test_fixed_params_api.py`

### 性能分析
- **总耗时**：约 14-20 秒/张
- **本地处理**：仅占 0.1%（编码/解码）
- **主要瓶颈**：服务器 AI 推理（99.9%）
- **验证脚本**：`test_performance_api.py`

## 🗑️ 清理

这些测试脚本已添加到 `.gitignore`，不会被提交到 Git 仓库。

如需清理本地文件：
```bash
rm -rf test/
```

## 📝 开发日志

- **2025-11-17**: 修复 imageConfig 格式问题
- **2025-11-17**: 添加固定参数优化
- **2025-11-17**: 整理测试脚本到 test 目录
