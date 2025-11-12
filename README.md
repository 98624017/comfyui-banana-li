# 🍌 Banana - ComfyUI Gemini Image Generator

> 为 ComfyUI 提供 Google Gemini 图像生成能力的自定义节点

[![GitHub](https://img.shields.io/badge/GitHub-Repository-blue)](https://github.com/YOUR_USERNAME/YOUR_REPO)
[![Python](https://img.shields.io/badge/Python-3.10+-blue)](https://www.python.org/)
[![ComfyUI](https://img.shields.io/badge/ComfyUI-Custom_Node-orange)](https://github.com/comfyanonymous/ComfyUI)

## 📖 简介

Banana 是一个强大的 ComfyUI 自定义节点，集成了 Google Gemini 的图像生成 API。支持文本到图像、图像到图像等多种生成模式，让你在 ComfyUI 工作流中轻松使用最新的 AI 图像生成技术。

## ✨ 功能特性

- 🎨 **多模态输入** - 支持纯文本、文本+图像混合提示词
- 🔢 **批量生成** - 一次生成最多 8 张图像
- 📐 **多种比例** - 支持 1:1、16:9、9:16、21:9 等多种宽高比
- 🎲 **种子控制** - 精确控制生成结果的随机性
- 🔄 **智能重试** - 内置指数退避重试机制，提高成功率
- 💰 **余额显示** - 实时显示 API 账户余额和费用预估
- ⚡ **并发处理** - 多线程并发生成，提升效率
- 🎯 **错误提示** - 失败时生成可视化错误提示图像
- 🌈 **彩色日志** - 线程安全的彩色日志系统，支持进度条

## 🚀 安装

### 方法 1: 通过 ComfyUI Manager（推荐）

1. 打开 ComfyUI Manager
2. 搜索 "Banana" 或 "Gemini Image Generator"
3. 点击安装

### 方法 2: 手动安装

```bash
cd ComfyUI/custom_nodes
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git banana
cd banana
```

## ⚙️ 配置

### 1. 创建配置文件

复制示例配置文件并编辑：

```bash
cp config.ini.example config.ini
```

### 2. 填写 API Key

编辑 `config.ini` 文件：

```ini
[gemini]
# 你的 Gemini API Key
api_key = YOUR_API_KEY_HERE

# Token 换算人民币调节系数（请勿调整）
balance_cost_factor = 0.5

# 最大并发工作线程数（建议 4-8）
max_workers = 8
```

### 3. 获取 API Key

你可以通过以下方式获取 Gemini API Key：

- **官方渠道**: [Google AI Studio](https://aistudio.google.com/app/apikey)
- **代理服务**: 支持兼容 Gemini 格式的代理服务

## 📝 使用方法

1. 在 ComfyUI 中添加 "🍌 Banana Image Generator" 节点
2. 配置节点参数（见下方参数说明）
3. 连接其他节点并运行工作流

### 基础示例

```
文本提示词 → 🍌 Banana Image Generator → 预览图像 → 保存图像
```

### 图生图示例

```
加载图像 → 🍌 Banana Image Generator → 预览图像
         ↗  (image_1 输入)
文本提示词
```

## 🎛️ 参数说明

### 必需参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| **prompt** | STRING | "Hello nano banana!" | 文本提示词，支持多行输入 |
| **api_base_url** | STRING | https://xinbaoapi.feng1994.xin | API 基础 URL |
| **api_key** | STRING | "" | Gemini API Key（留空则从配置文件读取） |
| **model_type** | STRING | gemini-2.5-flash-image | 使用的模型类型 |
| **batch_size** | INT | 1 | 批量生成数量（1-8） |
| **aspect_ratio** | STRING | Auto | 宽高比（Auto/1:1/9:16/16:9/21:9/2:3/3:2/3:4/4:3/4:5/5:4） |

### 可选参数

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| **seed** | INT | -1 | 随机种子（-1 为随机，0-102400 为固定） |
| **top_p** | FLOAT | 0.95 | 采样参数，控制生成多样性（0.0-1.0） |
| **image_1~5** | IMAGE | - | 可选的参考图像输入（最多 5 张） |

## 📋 输出

节点返回一个包含生成图像的张量（TENSOR），可以连接到：
- 图像预览节点
- 图像保存节点
- 其他图像处理节点

## 🔧 高级功能

### 并发控制

在 `config.ini` 中调整 `max_workers` 参数来控制并发线程数：
- **低端设备**: 2-4
- **中端设备**: 4-8
- **高端设备**: 8+

### 错误处理

当生成失败时，节点会：
1. 自动重试（最多 3 次，指数退避）
2. 如果所有重试都失败，返回包含错误信息的可视化图像
3. 在控制台输出详细的错误日志

### 余额监控

节点内置 Web UI 扩展，可以在 ComfyUI 界面中实时查看：
- 💰 账户余额
- 📊 Token 使用量
- 💸 预估费用

## 🛠️ 开发

### 项目结构

```
banana/
├── __init__.py                    # 节点注册入口
├── Gemini_Imagen_Generator.py    # 主节点实现
├── logger.py                      # 日志系统
├── config.ini                     # 配置文件（需自行创建）
├── config.ini.example             # 配置模板
├── web/
│   └── extensions/
│       ├── token-balance.js       # 余额显示扩展
│       └── xinbao.png            # 图标
├── .gitignore
├── README.md
└── LICENSE
```

### 依赖库

- PyTorch
- NumPy
- Pillow
- requests
- configparser

## ❗ 注意事项

1. **API Key 安全**:
   - ⚠️ **绝不要**将包含真实 API Key 的 `config.ini` 提交到公开仓库
   - 使用 `.gitignore` 已自动排除 `config.ini`

2. **费用控制**:
   - 每次生成都会消耗 API 额度
   - 建议设置合理的 `batch_size` 避免过度消耗

3. **网络要求**:
   - 需要稳定的网络连接到 Gemini API
   - 某些地区可能需要使用代理

4. **性能优化**:
   - 批量生成时会使用多线程并发
   - 根据机器性能调整 `max_workers`

## 🐛 故障排除

### 问题：无法加载节点

**解决方案**:
- 检查 ComfyUI 日志输出
- 确认所有依赖库已正确安装
- 重启 ComfyUI

### 问题：API 请求失败

**解决方案**:
- 检查 API Key 是否正确
- 验证网络连接
- 查看 `api_base_url` 是否可访问
- 检查账户余额是否充足

### 问题：生成速度慢

**解决方案**:
- 降低 `batch_size`
- 调整 `max_workers` 参数
- 检查网络延迟

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

## 📄 许可证

本项目采用 [MIT License](LICENSE) 开源协议。

## 🙏 致谢

- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) - 强大的 Stable Diffusion GUI
- [Google Gemini](https://ai.google.dev/) - 提供图像生成 API
- 所有贡献者和使用者

## 📮 联系方式

- GitHub Issues: [提交问题](https://github.com/YOUR_USERNAME/YOUR_REPO/issues)
- Email: your.email@example.com

---

**⭐ 如果这个项目对你有帮助，请给个 Star！**
