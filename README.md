<div align="center">

# 心宝❤Banana - ComfyUI Gemini Image Generator

<img src="https://img.131213.xyz/tfile/BQACAgUAAx0Eflp52gABAR3HaUfc50Pq9iF-lJ6ISHuScKWiD-wAArYbAAILXEFWe2NHI8YGLL82BA" width="200" alt="Banana Logo"/>

> 为 ComfyUI 提供 Nano Banana 图像生成能力的自定义节点

[![GitHub](https://img.shields.io/badge/GitHub-comfyui--banana--li-blue)](https://github.com/98624017/comfyui-banana-li)
[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom_Node-orange)](https://github.com/comfyanonymous/ComfyUI)
[![Bilibili](https://img.shields.io/badge/Bilibili-@李心宝爱玩Ai-ff69b4)](https://space.bilibili.com/470042957)

</div>

<div align="center">

| **Windows** | **Linux** | **macOS** |
| :---: | :---: | :---: |
| [comfyui-banana-li](https://github.com/98624017/comfyui-banana-li) | [comfyui-banana-li-linux](https://github.com/98624017/comfyui-banana-li-linux) | [comfyui-banana-li-mac](https://github.com/98624017/comfyui-banana-li-mac) |

</div>

## 📖 简介

Banana 是一个强大的 ComfyUI 自定义节点,集成了 Google NanoBanana 的图像生成 API。支持文本到图像、图像到图像等多种生成模式,让你在 ComfyUI 工作流中轻松使用最新的 AI 图像生成技术。

大家好，我是李心宝，一个在电商设计领域摸爬滚打了多年的老设计。我专注在如何让 AI 技术真正在咱们的日常工作中落地，提升效率。我乐于分享自己深度评测、实践过、确实好用的 AI 工作流和设计技巧，希望能和大家一起探索、共同进步。我整理、制作了不少免费的工作流和资料，希望能帮你少走弯路。

当然，如果你需要更精细化、针对性更强的解决方案，我也提供付费的专属工作流。期待能和更多志同道合的设计人、电商人、AI实践者们交个朋友，一起把 AI 设计玩明白！

开发和交接请先阅读 [交接说明](./HANDOFF.md)，故障排查见 [故障排查](./docs/troubleshooting.md)。

### <img src="https://img.shields.io/badge/飞书-00D6B9?logo=lark&logoColor=white" align="center" style="vertical-align: middle;"> 免费资料与专属工作流介绍

📂 [点击访问飞书文档](https://lcni4wauvbvx.feishu.cn/docx/BODPdxQ51ontbzxbq7tcUvlsnMd) - 获取免费资料及专属工作流详情



## 📺 视频教程

访问我的 [B站主页](https://space.bilibili.com/470042957) 观看详细的使用教程和案例演示!

### 部分视频

- <img src="https://img.shields.io/badge/Bilibili-ff69b4?logo=bilibili&logoColor=white" align="center" style="vertical-align: middle;"> [香蕉100%不偏移技巧,效率提升N倍](https://www.bilibili.com/video/BV1ir1cBVEeA)
- <img src="https://img.shields.io/badge/Bilibili-ff69b4?logo=bilibili&logoColor=white" align="center" style="vertical-align: middle;"> [心宝顶级放大系列-03人像类放大](https://www.bilibili.com/video/BV1J7yXBoEq6)
- <img src="https://img.shields.io/badge/Bilibili-ff69b4?logo=bilibili&logoColor=white" align="center" style="vertical-align: middle;"> [心宝顶级放大05-100%修手修脚](https://www.bilibili.com/video/BV1LSnZzoERc)
- <img src="https://img.shields.io/badge/Bilibili-ff69b4?logo=bilibili&logoColor=white" align="center" style="vertical-align: middle;"> [4K透溶V2——纠正背景透视,一键换背景、融合、打光](https://www.bilibili.com/video/BV1mhaazPE13)

## 📮 联系方式

- **GitHub Issues**: [提交问题和建议](https://github.com/98624017/comfyui-banana-li/issues)
- **Bilibili**: [@心宝](https://space.bilibili.com/470042957) - 视频教程和更新动态
- **获取公开资料及API 购买**: <img src="https://img.shields.io/badge/WeChat-07C160?logo=wechat&logoColor=white" align="center" style="vertical-align: middle;"> Li_18727107073

## ✨ 功能特性

- 🎨 多模态输入：文本、文本+多张参考图
- 🔢 批量生成：1-8 张，支持固定种子复现
- 📐 多种比例：Auto/1:1/9:16/16:9/21:9 等
- 🔄 智能重试：指数退避，失败返回可视化错误图
- ⚡ 并发控制：本地处理与网络并发可独立配置
- 💰 余额查询：Web UI 扩展实时展示可用/已用额度
- 🧩 增强节点：绑定上下文、裁剪贴图、分割一键集成
- 🆕 ModelScope：文生图与多模态图像描述两类节点
- 🆕 **AIwork_Personal 对齐**：Caption 仅保留 Gemini 3 Pro/Flash 两款模型并默认使用内部 SSE 流式传输；V3 图片模型、Seedance/统一视频/Grok/Omni 模型和一键主图/详情 V4 规则统一由插件内置注册表提供。
- 🆕 **本地 AI 应用**：`XinbaoComfyApiApp` 内置高清放大、一键去噪、精准抠图。


## 🗒️ 更新日志

> 仅记录用户可明显感知且重要的变更；更完整的细节请查看提交记录。

### 2026-01-26（27eeae4）→ 2026-02-27（e46aedb）

- Gemini 生图：新增 `1:4 / 4:1 / 1:8 / 8:1` 等极端宽高比；非 `gemini-3.1-flash-image-preview` 模型会自动回退为 `Auto` 并提示。
- 视频节点：`心宝❤视频生成` 显示名更新为 `心宝❤视频生成Sora`（不影响已有工作流，仅方便搜索区分）。
- 新增节点：`心宝❤失败链接汇总`（`BananaFailedUrlAggregator`），可汇总多个 BananaV2 的 `failed_urls` 并可选自动重试下载。
- 新增节点：`心宝❤AI应用`（`XinbaoComfyApiApp`），本地提供高清放大、一键去噪、精准抠图，并提供 Web UI 应用快速切换。
- 变更：移除「心宝路径加载」相关节点/功能（若你在开发版工作流中用过该节点，需要改线）。
- 安全：清理仓库中误提交的运行时文件/敏感数据，补充忽略规则，降低泄露风险。


## 🚀 安装

1) 将仓库克隆到 ComfyUI 的 `custom_nodes` 目录：
```bash
cd ComfyUI/custom_nodes
git clone https://github.com/98624017/comfyui-banana-li.git comfyui-banana-li
cd comfyui-banana-li
```
2) 本插件保留 Mask 遮罩节点；图像分割等通用工具已迁出，不再附带 SAM 模型脚本。
3) 依赖需在 ComfyUI 环境中提前安装（常见：torch、opencv-contrib-python、transformers[AutoProcessor]、scipy、Pillow、requests）。

## ⚙️ 配置

1) 打开右下角“心宝任务中心”，点击顶部“设置Key”填写全局 API Key。Key 仅保存到本机已忽略的 `config.ini`，前端只显示与 Key 等长的星号，不写入工作流或生成图片元数据。

可手动复制 `config.ini.example` 为本机 `config.ini`。图床代理密钥通过 `[imagebed].api_key` 或 `BANANA_IMAGEBED_API_KEY` 环境变量配置；魔搭专用密钥仍通过节点输入或 `[gemini].modelscope_api_key` 提供。

旧工作流中的 `XinbaoApiKeyPurge`（全局密钥管理）节点已退休，请删除该节点并在任务中心设置 Key。仓库示例已移除该节点；图片生成节点仍使用原来的 `BananaImageNodeV3` ID，显示名更新为“心宝♥BananaV4”。

2) 并发与性能（按机器/网络调整）：
- `max_workers`：本地解码/处理并发，建议 2-8。
- `network_workers_cap`：网络并发上限（1-8），网络不稳时建议 2-3。

3) 其他高级开关见 `config_manager.py` 中的默认配置，通常保持默认即可直接使用。

> Base URL/线路选择现已在节点参数内完成，配置文件无需额外修改。

## 🧩 节点一览

- **心宝❤Banana**：Gemini 生图主节点，支持文本/图像输入、批量、多比例、禁用 SSL（可选）。
- **心宝❤BananaV2**：新版 Gemini 生图节点（在 Banana 基础上扩展高级能力）。新增 `启用工作流并发` 开关（默认关闭）：开启后同一工作流中多个 BananaV2/视频节点可并发发起 API 调用；并发模式下单节点失败不会终止整图（会阻断该节点输出并在文本端口显示错误）。
- **心宝❤批量详情图保存**：批量详情图保存 + 拼接预览面板（提示词编辑/多选局部重试/同位置≤10版本对比/手动保存拼接图）。示例：`example_workflows/心宝一键详情页_批量详情图保存.json`。
- **心宝❤绑定生成** / **BananaLocalCropPreprocess/Paste**：绑定上下文与局部裁剪增强节点，仅在需要绑定链路时接入。
- **余额扩展**：`web/extensions/token-balance.js` 自动加载，展示可用/已用额度与最近查询时间。
- **心宝❤魔搭文生图**：`Tongyi-MAI/Z-Image-Turbo`，batch 1-4，支持种子、负面提示词、尺寸/步数/guidance。

- **心宝❤多模态LLM反推**：多图输入（最多 3 张），可选香蕉/魔搭渠道，生成中文描述，支持温度与 max_tokens 设置。
- **心宝❤视频生成**：文本/参考图生成视频节点。新增 `启用工作流并发` 开关（默认关闭）：开启后可与其它并发节点同时发起请求；并发模式下单节点失败不会终止整图（会阻断该节点 VIDEO 输出并在文本端口显示错误）。
- **心宝❤视频生成（统一模型）**：替代已停用的 Sora/Veo/豆包 facade，模型选择来自 `XinbaoVideos/core/model_mapping.py`。
- **心宝❤详情页选项**：一键主图/详情 V4 的 prompt、分段解析和追加提示词来自插件内置规则，不再读取托管 TOML。


## 📝 快速上手

### 文生图（最小可用）
1. 启动 ComfyUI，搜索并添加 `心宝❤Banana` 节点。
2. 在心宝任务中心设置全局 API Key，节点的 Key 保持空白；设置 `batch_size=1-4`，选择合适 `aspect_ratio`。
3. 输入提示词，运行后将输出图像张量，可接预览或保存节点。

### 图生图
```
加载图像 → 心宝❤Banana（image_1 输入）
         ↗ 文本提示词
```
可额外提供最多 5 张参考图像，结合文本指导生成。

### 绑定增强链路（可选）
```
心宝❤绑定生成 → 局部裁切预处理 → 心宝❤Banana → BananaLocalCropPaste
```
仅在需要局部编辑/对齐裁剪时接入，普通生图可不连接 `binding_context`。

### ModelScope 示例

- 文生图：选择 `心宝❤魔搭文生图` 节点，填入 ModelScope API Key，batch ≤4，按需设置尺寸/步数。

- 图像描述：使用 `心宝❤多模态LLM反推`，可输入最多 3 张图像并选择香蕉/魔搭渠道生成中文描述。


## 通用工具迁移

提示词助手、合成 PSD、分层数、智能拼图、图片加载、图片拆分和图像分割已迁往其它项目，本插件不再提供。详情拼合、详情页选项和 Mask 遮罩继续提供。

旧工作流和仓库中的历史详情示例若引用这些节点，需要安装已迁出的对应插件；普通图片输入也可改接 ComfyUI 原生 LoadImage。本次不会删除本机词库、输入图片或模型权重。

## 🎛️ 关键参数（Gemini 生图）

| 参数 | 说明 | 建议 |
|---|---|---|
| api_key | 为空时读取任务中心本机全局 Key | 建议留空 |
| batch_size | 1-8，批量输出 | 常用 1-4，避免高并发丢图风险 |
| aspect_ratio | Auto/1:1/9:16/16:9/21:9 等 | 按场景选择 |
| seed | -1 随机，0-102400 固定 | 复现结果时设定 |
| image_size | 1K/2K/4K（gemini-3-pro-image*） | 默认 2K |
| 禁用SSL验证 | 临时绕过证书校验 | 仅在可信网络下启用 |

> 高并发/大批量（特别是 8 张）在少数场景可能出现“请求成功但不返图”且仍计费的上游行为。追求稳定建议 batch 控制在 1-4，并视网络情况调低 `network_workers_cap`。

## 🔒 安全与费用提示

- 建议只在心宝任务中心填写 API Key；节点 Key 保持空白，避免密钥进入工作流或 PNG 元数据。
- 关闭 SSL 校验或使用代理会增加泄露风险，请确保网络可信。
- 每次生成都会计费，调参前先用低 batch 小步验证。

## 🐛 故障排除

- 节点未加载：检查依赖安装与 ComfyUI 日志，重启后重试。
- 请求失败：确认 API Key 正确、网络连通、余额充足；必要时降低 batch 与并发。
- 生成慢：降低 `batch_size`，调低并发，检查网络延迟。
- 常见报错速查：见文档 [故障排查](./docs/troubleshooting.md)（支持关键字检索）。

## 🤝 支持与反馈

- 问题与建议：请提交 GitHub Issues。
- 更新动态与教程：关注 B 站 `@李心宝爱玩Ai`（可私信沟通）。

<div align="center">

**⭐ 觉得有用请点个 Star！**

</div>
