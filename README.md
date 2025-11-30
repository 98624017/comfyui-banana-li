<div align="center">

# 心宝❤Banana - ComfyUI Gemini / ModelScope 节点合集

<img src="https://youke1.picui.cn/s1/2025/11/12/69140968ed33b.jpg" width="200" alt="Banana Logo"/>

> ComfyUI 自定义节点，整合 Google NanoBanana Gemini 生图能力、绑定增强链路与 ModelScope 直连节点。

[![GitHub](https://img.shields.io/badge/GitHub-comfyui--banana--li-blue)](https://github.com/98624017/comfyui-banana-li)
[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom_Node-orange)](https://github.com/comfyanonymous/ComfyUI)
[![Bilibili](https://img.shields.io/badge/Bilibili-@李心宝爱玩Ai-ff69b4)](https://space.bilibili.com/470042957)

</div>

## 📖 简介

- 核心生图节点：基于 NanoBanana Gemini 接口，支持文生图、图生图、混合提示词、多比例与批量输出。
- 绑定增强链路：兼容心宝增强节点（绑定生成/局部裁剪/分割），可组成“绑定 → 前置增强 → 生图 → 后置还原”的完整流水线。
- 余额与日志：内置 Web 扩展显示余额，线程安全彩色日志，失败时返回可视化错误提示图。
- ModelScope 直连：提供“心宝❤魔搭文生图”“心宝❤多模态LLM反推”节点，轻量直连官方 API，无需额外 SDK。

> 所有示例均使用占位符，密钥与内部地址请根据官方渠道获取，不要在公开仓库泄露。

## 📮 联系方式与资料
- GitHub Issues：提交问题与需求 https://github.com/98624017/comfyui-banana-li/issues
- B 站：[@李心宝爱玩Ai](https://space.bilibili.com/470042957)（更新动态与教程）
- 微信：`Li_18727107073`（公开沟通渠道）
- 免费资料与专属工作流介绍：飞书文档 https://lcni4wauvbvx.feishu.cn/docx/BODPdxQ51ontbzxbq7tcUvlsnMd

## 📺 视频教程
- [香蕉 100% 不偏移技巧，效率提升](https://www.bilibili.com/video/BV1ir1cBVEeA)
- [心宝顶级放大系列 - 人像放大](https://www.bilibili.com/video/BV1J7yXBoEq6)
- [4K 透溶 V2：纠正背景透视与融合](https://www.bilibili.com/video/BV1mhaazPE13)

## ✨ 功能特性

- 🎨 多模态输入：文本、文本+多张参考图
- 🔢 批量生成：1-8 张，支持固定种子复现
- 📐 多种比例：Auto/1:1/9:16/16:9/21:9 等
- 🔄 智能重试：指数退避，失败返回可视化错误图
- ⚡ 并发控制：本地处理与网络并发可独立配置
- 💰 余额查询：Web UI 扩展实时展示可用/已用额度
- 🧩 增强节点：绑定上下文、裁剪贴图、分割一键集成
- 🆕 ModelScope：文生图与多模态图像描述两类节点

## 🚀 安装

1) 将仓库克隆到 ComfyUI 的 `custom_nodes` 目录：
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/98624017/comfyui-banana-li.git comfyui-banana-li
cd comfyui-banana-li
```
2) 本插件自带原 `segment_nodes_li` 全量节点与模型脚本，无需额外安装。
3) 依赖需在 ComfyUI 环境中提前安装（常见：torch、opencv-contrib-python、transformers[AutoProcessor]、scipy、Pillow、requests）。

## ⚙️ 配置

1) 仓库已附带 `config.ini` 模板，直接编辑其中的 `[gemini]` 段落填入 API Key（请使用官方渠道获取；若文件被清理，可重新从仓库获取同名模板）。

2) 并发与性能（按机器/网络调整）：
- `max_workers`：本地解码/处理并发，建议 2-8。
- `network_workers_cap`：网络并发上限（1-8），网络不稳时建议 2-3。

3) 其他高级开关可在仓库内 `config.ini` 注释查看，通常保持默认即可直接使用。

> Base URL/线路选择现已在节点参数内完成，配置文件无需额外修改。

## 🧩 节点一览

- **心宝❤Banana**：Gemini 生图主节点，支持文本/图像输入、批量、多比例、禁用 SSL（可选）。
- **心宝❤绑定生成** / **BananaLocalCropPreprocess/Paste** / **SegmentAnythingUltraLi**：绑定上下文与裁剪/分割增强节点，仅在需要绑定链路时接入。
- **余额扩展**：`web/extensions/token-balance.js` 自动加载，展示可用/已用额度与最近查询时间。
- **心宝❤魔搭文生图**：`Tongyi-MAI/Z-Image-Turbo`，batch 1-4，支持种子、负面提示词、尺寸/步数/guidance。

- **心宝❤多模态LLM反推**：多图输入（最多 3 张），可选香蕉/魔搭渠道，生成中文描述，支持温度与 max_tokens 设置。


## 📝 快速上手

### 文生图（最小可用）
1. 启动 ComfyUI，搜索并添加 `心宝❤Banana` 节点。
2. 在节点参数填入 API Key，设置 `batch_size=1-4`，选择合适 `aspect_ratio`。
3. 输入提示词，运行后将输出图像张量，可接预览或保存节点。

### 图生图
```
加载图像 → 心宝❤Banana（image_1 输入）
         ↗ 文本提示词
```
可额外提供最多 5 张参考图像，结合文本指导生成。

### 绑定增强链路（可选）
```
心宝❤绑定生成 → 裁切/分割等前置增强 → 心宝❤Banana → BananaLocalCropPaste
```
仅在需要局部编辑/对齐裁剪时接入，普通生图可不连接 `binding_context`。

### ModelScope 示例

- 文生图：选择 `心宝❤魔搭文生图` 节点，填入 ModelScope API Key，batch ≤4，按需设置尺寸/步数。

- 图像描述：使用 `心宝❤多模态LLM反推`，可输入最多 3 张图像并选择香蕉/魔搭渠道生成中文描述。


## 🎛️ 关键参数（Gemini 生图）

| 参数 | 说明 | 建议 |
|---|---|---|
| api_key | 为空时读取 `config.ini` | 必填，勿泄露 |
| batch_size | 1-8，批量输出 | 常用 1-4，避免高并发丢图风险 |
| aspect_ratio | Auto/1:1/9:16/16:9/21:9 等 | 按场景选择 |
| seed | -1 随机，0-102400 固定 | 复现结果时设定 |
| image_size | 1K/2K/4K（gemini-3-pro-image*） | 默认 2K |
| 禁用SSL验证 | 临时绕过证书校验 | 仅在可信网络下启用 |

> 高并发/大批量（特别是 8 张）在少数场景可能出现“请求成功但不返图”且仍计费的上游行为。追求稳定建议 batch 控制在 1-4，并视网络情况调低 `network_workers_cap`。

## 🔒 安全与费用提示

- API Key 仅存放本地 `config.ini` 或节点参数，勿上传到公开仓库/分享工作流。
- 关闭 SSL 校验或使用代理会增加泄露风险，请确保网络可信。
- 每次生成都会计费，调参前先用低 batch 小步验证。

## 🐛 故障排除

- 节点未加载：检查依赖安装与 ComfyUI 日志，重启后重试。
- 请求失败：确认 API Key 正确、网络连通、余额充足；必要时降低 batch 与并发。
- 生成慢：降低 `batch_size`，调低并发，检查网络延迟。

## 🤝 支持与反馈

- 问题与建议：请提交 GitHub Issues。
- 更新动态与教程：关注 B 站 `@李心宝爱玩Ai`（可私信沟通）。

<div align="center">

**⭐ 觉得有用请点个 Star！**

</div>
