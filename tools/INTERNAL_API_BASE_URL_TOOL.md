# 内部工具：Banana Gemini API Base URL 管理说明（仅内部使用）

本说明用于团队内部维护 `comfyui-banana-gemini` 节点的 API Base URL，帮助在**不暴露明文 URL**、**不对外开放配置入口**的前提下，安全地调整后端请求地址。

> 提示：当前版本的 Base URL 已固定为三条显性线路（香港/直连美区/CF）+ 一条隐藏线路（fixsk- 前缀自动切换），`tools/set_api_base_url.ps1` 仅用于选择默认显性线路并输出混淆值，不再写入 `config.ini` 或本地测试配置。详见 `API_BASE_URL_AND_BALANCE.md`。

> 重要：`tools/` 目录已被 `.gitignore` 忽略，本文件和所有脚本均不会提交到 Git 仓库，只在本地环境使用。

---

## 一、工具目录结构

目录：`tools/`

- `set_api_base_url.ps1`  
  实际逻辑脚本：
  - 弹出“选择配置文件”窗口；
  - 询问操作模式（临时测试 / 永久替换）；
  - 读取用户输入的明文 URL；
  - 按 XOR+Base64 规则编码后写入对应 ini。

- `INTERNAL_API_BASE_URL_TOOL.md`（本文件）  
  仅供内部维护查看的使用说明。

---

## 二、后端逻辑概览（方便理解工具行为）

后端 Python 中，实际生效的 Base URL 由 `Gemini_Imagen_Generator` 类统一管理：

1. 默认值写在类内，以“字符编码列表 + 简单解码”的形式存储，**不会在源码中出现明文 URL**。
2. 运行时通过 `_get_effective_api_base_url()` 计算当前有效的 Base URL，优先级为：  
   1) 如果开启测试模式且 `banana_gemini_test.local.ini` 中存在 `[gemini_test].api_base_url_enc` → 使用该测试地址；  
   2) 否则，如果 `config.ini` 中存在 `[gemini].api_base_url_enc` → 使用该永久配置地址；  
   3) 否则回退到类内置的默认值（即项目默认绑定的地址）。
3. 所有网络请求和余额查询均通过上述统一函数获取 Base URL，前端和节点 UI 不再能传入或控制 Base URL。

也就是说：**想改 Base URL，只能改配置文件里的编码字段，不能通过 ComfyUI 界面或 HTTP 请求参数修改。**

---

## 三、操作模式说明

工具提供两种模式，对应两个典型场景。

### 1. 模式 1：临时开发 / 测试新的 Base URL

用途：
- 本地调试新环境、新代理或测试端点；
- 不影响正式配置和其他用户。

行为：
- 选择模式 `1` 后，脚本会将编码后的 Base URL 写入：  
  `banana_gemini_test.local.ini` 的 `[gemini_test].api_base_url_enc`；
- 仅当环境变量 `BANANA_GEMINI_USE_LOCAL_TEST=1` 时，后端才会启用该测试地址；
- 关闭测试模式或删除该字段后，会自动回退到永久配置或默认值。

### 2. 模式 2：永久替换项目中的 Base URL

用途：
- 正式切换后端服务提供商或域名；
- 在不修改源码、不暴露明文 URL 的前提下，调整线上行为。

行为：
- 选择模式 `2` 后，脚本会将编码后的 Base URL 写入：  
  `config.ini` 的 `[gemini].api_base_url_enc`；
- 之后所有未开启测试模式的运行都会使用该地址；
- 需要时可以再次运行工具覆盖该字段，完成迁移。

---

## 四、使用步骤（操作流程）

1. 确保脚本路径可访问：  
   无论当前工作目录在哪，都可以直接通过完整路径运行脚本，例如：  
   ```powershell
   powershell -ExecutionPolicy Bypass -File "F:\ComfyUI-aki-v1.3\ComfyUI\custom_nodes\comfyui-banana-gemini\tools\set_api_base_url.ps1"
   ```
   或者先 `cd` 到任意目录，再用完整路径调用：  
   ```powershell
   cd C:\Users\YourName
   powershell -ExecutionPolicy Bypass -File "F:\ComfyUI-aki-v1.3\ComfyUI\custom_nodes\comfyui-banana-gemini\tools\set_api_base_url.ps1"
   ```

2. 在脚本提示中选择操作类型：
   - 输入 `1` → 临时开发 / 测试  
   - 输入 `2` → 永久替换项目 Base URL

3. 在弹出的文件选择窗口中选择要修改的配置文件（可以是任意路径）：
   - 模式 1（建议） → 选择 `banana_gemini_test.local.ini`  
   - 模式 2（建议） → 选择 `config.ini`

4. 在 PowerShell 窗口中输入新的 `api_base_url`（明文）：  
   例如：`https://api.aabao.top` 或某个测试地址。

5. 脚本会：
   - 使用内部逻辑将 URL 编码为一段不可读的字符串；
   - 将该字符串写入所选 ini 文件对应 Section 下的 `api_base_url_enc`；
   - 输出“已更新 ...”提示。

6. 生效方式：
   - 临时模式：需要在运行环境中设置 `BANANA_GEMINI_USE_LOCAL_TEST=1`；
   - 永久模式：重启 ComfyUI，节点会自动使用新地址。

---

## 五、注意事项

1. **本工具和本说明仅供内部运维使用**  
   - `tools/` 目录已在 `.gitignore` 中被忽略，不会上传到远程仓库。

2. **不要在仓库内其他文件写明文 URL 和密钥**  
   - 不要在 `README`、源码、示例配置中直接写出真实 API 地址。

3. **不要手动编辑 `api_base_url_enc` 字段**  
   - 如果手动修改为错误值，解码会失败，节点将无法正确访问服务。
   - 建议始终通过本工具修改，以保证编码方式一致。

4. **测试 Key 与正式 Key 请严格区分**  
   - `banana_gemini_test.local.ini` 中的测试 Key 不应提交到 Git；
   - 正式环境的 `config.ini` 也已在 `.gitignore` 中忽略。

---

## 六、简单故障排查

1. **节点一直报“连接失败 / 无法访问服务”：**
   - 检查是否配置了错误的 `api_base_url_enc`（重新用工具写一遍）；
   - 检查服务端地址是否可访问（用 curl / 浏览器测试）；
   - 检查代理、网络环境是否有变动。

2. **测试模式不生效：**
   - 检查 `BANANA_GEMINI_USE_LOCAL_TEST` 是否设置为 `1/true/yes/on`；
   - 检查 `banana_gemini_test.local.ini` 中 `[gemini_test].api_base_url_enc` 是否存在且未注释；
   - 重启 ComfyUI 再测试。

3. **永久替换后仍然走旧地址：**
   - 确认修改的是正确的 `config.ini`；
   - 确认没有开启测试模式（否则优先走测试配置）；
   - 确认节点已经重新加载（重启 ComfyUI）。

如需调整编码策略或迁移到新的管理方式，请先审阅 `Gemini_Imagen_Generator.py` 中 `_get_effective_api_base_url` 及相关逻辑，再同步更新本工具。内部变更建议记录在本文件中，方便后续维护。
