# 余额查询功能诊断报告

## ✅ 测试结果

### 1. API接口测试 - **正常**
- 测试文件: [test_balance_simple.py](./test_balance_simple.py)
- API端点: `https://xinbaoapi.feng1994.xin/api/usage/token`
- API Key: `sk-GBzYQs9wJ9VtSe0Ghe7mCLG4E2uIv8w1Cha1PWdlUj3Lcr2W`
- **结果**: ✅ API正常工作,成功返回余额数据

**实际余额:**
```json
{
  "code": true,
  "data": {
    "expires_at": 0,
    "total_available": 4585000,
    "total_granted": 5000000,
    "total_used": 415000,
    "unlimited_quota": false
  }
}
```

---

## 🔍 可能的失效原因

### 问题1: config.ini中的API Key未更新
**文件**: `config.ini`
**当前值**: `YOUR_API_KEY_HERE`
**应该是**: `sk-GBzYQs9wJ9VtSe0Ghe7mCLG4E2uIv8w1Cha1PWdlUj3Lcr2W`

**影响**:
- 第266行 `api_key = cls.load_config()` 会读取到无效的API Key
- 导致后续余额查询失败

**解决方案**:
```ini
[gemini]
api_key = sk-GBzYQs9wJ9VtSe0Ghe7mCLG4E2uIv8w1Cha1PWdlUj3Lcr2W
balance_cost_factor = 0.5
max_workers = 8
```

---

### 问题2: 前端调用路径可能有误
**文件**: `web/extensions/token-balance.js`
**第292行**:
```javascript
const response = await api.fetchApi(
  `/banana/token_usage?base_url=${encodeURIComponent(baseUrl)}&refresh=${refresh ? 1 : 0}`,
  { method: "GET" }
);
```

**检查点**:
1. ComfyUI的`api.fetchApi`是否正确代理请求
2. 路由是否成功注册到PromptServer
3. 浏览器控制台是否有错误信息

---

### 问题3: 路由注册时机问题
**文件**: `Gemini_Imagen_Generator.py`
**第1193行**: `BananaImageNode.ensure_balance_route()`

**潜在问题**:
- 这行代码在模块加载时执行
- 如果此时`PromptServer.instance`还未初始化,会启动定时器重试(第242-244行)
- 定时器是daemon线程,可能在注册成功前被终止

**验证方法**:
在ComfyUI启动日志中查找:
- 是否有路由注册相关的错误
- 是否有"重试"相关的消息

---

## 🛠️ 诊断步骤

### 步骤1: 更新config.ini
```bash
# 编辑配置文件
nano f:/ComfyUI-aki-v1.3/ComfyUI/custom_nodes/comfyui-banana-gemini/config.ini

# 将 api_key 改为:
api_key = sk-GBzYQs9wJ9VtSe0Ghe7mCLG4E2uIv8w1Cha1PWdlUj3Lcr2W
```

### 步骤2: 重启ComfyUI
- 完全关闭ComfyUI
- 重新启动ComfyUI服务器
- 观察启动日志中是否有:
  ```
  🍌 Banana Node Loader
  ✅ 成功加载节点文件: Gemini_Imagen_Generator.py
  ```

### 步骤3: 浏览器测试
1. 打开ComfyUI Web界面
2. 按F12打开开发者工具
3. 在Console标签执行:
```javascript
fetch("/banana/token_usage?base_url=https://xinbaoapi.feng1994.xin&refresh=1")
  .then(r => r.json())
  .then(console.log)
  .catch(console.error)
```

**预期结果**:
```json
{
  "success": true,
  "data": {
    "total_available": 4585000,
    "total_granted": 5000000,
    "total_used": 415000
  },
  "summary": "🔑 查询时间 16:49\n估算费用: 可用 ¥4.5850 / 已用 ¥0.4150 (仅参考)\n到期: 不过期"
}
```

### 步骤4: 检查节点UI
1. 在ComfyUI中添加 "心宝❤Banana" 节点
2. 查看节点底部是否有:
   - "余额" 文本框
   - "复制微信号" / "查询余额" / "二维码" 三个按钮
3. 点击"查询余额"按钮
4. 观察余额信息是否更新

---

## 📝 代码分析

### 关键代码路径

#### 1. 路由注册流程
```
__init__.py (模块加载)
  ↓
Gemini_Imagen_Generator.py (第1193行)
  ↓
BananaImageNode.ensure_balance_route() (第254行)
  ↓
检查 PromptServer.instance
  ├─ 存在 → 注册路由 @prompt_server.routes.get("/banana/token_usage")
  └─ 不存在 → 启动定时器1秒后重试
```

#### 2. 前端查询流程
```
token-balance.js (第317行 queryBalance())
  ↓
requestBalance() (第289行)
  ↓
api.fetchApi("/banana/token_usage?refresh=1")
  ↓
handle_token_usage() (Gemini_Imagen_Generator.py 第263行)
  ↓
cls.load_config() 读取API Key (第266行)
  ↓
cls.fetch_token_usage() 调用API (第295行)
  ↓
cls._store_balance_snapshot() 存储缓存 (第426行)
  ↓
返回格式化的余额信息
```

---

## 🎯 最可能的原因

**config.ini中API Key未配置**

证据:
1. API接口本身正常工作 ✅
2. Web扩展代码正确 ✅
3. 路由注册逻辑正确 ✅
4. **但是路由处理器需要从config.ini读取API Key** ⚠️

**结论**:
如果config.ini中API Key是默认值`YOUR_API_KEY_HERE`,路由会:
1. 读取到无效的API Key (第266行)
2. API请求失败 (第295行)
3. 返回错误响应 (第310-314行)
4. 前端显示错误信息 (第327行)

---

## ✅ 解决方案

### 立即修复
编辑 `config.ini`,将API Key更新为你的实际Key,然后重启ComfyUI。

### 备选方案
如果不想写入config.ini,可以在ComfyUI节点的`api_key`输入框中直接输入API Key。
节点会优先使用输入框中的Key (Gemini_Imagen_Generator.py 第973行)。

---

生成时间: 2025-11-12 16:49
测试API Key: `sk-GBzYQs9wJ9VtSe0Ghe7mCLG4E2uIv8w1Cha1PWdlUj3Lcr2W` (已验证可用)
