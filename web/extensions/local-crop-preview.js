import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const EXTENSION = "banana.localCropPreview";
const TARGET = "BananaLocalCropPreprocess";
const DEBOUNCE_MS = 200;
// 预览组件高度拉升到接近 512x512 显示区域（考虑边距）
const PREVIEW_HEIGHT = 520;
// 节点整体的最低高度，避免被 LiteGraph 布局压缩成一条细线
const MIN_NODE_HEIGHT = 660;
// 前端展示名称映射，保持逻辑名称不变，仅更新显示文案
const WIDGET_DISPLAY_LABELS = {
  padding_slider: "裁切范围",
  blend_slider: "透明度",
  expand_slider: "回贴边界微调",
};

function getLiteGraph() {
  return typeof globalThis !== "undefined" ? globalThis.LiteGraph : undefined;
}

function clampPadding(value) {
  const num = Number(value);
  if (Number.isNaN(num)) return 1;
  return Math.min(10, Math.max(1, Math.round(num)));
}

function findWidget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function wrapWidget(node, widget, onChange) {
  const original = widget.callback;
  widget.callback = function () {
    const result = original?.apply(this, arguments);
    if (widget.name === "padding_slider") {
      const next = clampPadding(widget.value);
      if (next !== widget.value) {
        widget.value = next;
      }
    }
    onChange(node);
    // 只要用户修改了参数，就重置按钮状态，允许再次点击
    resetButton(node);
    return result;
  };
}

function resetButton(node) {
  const state = node.__bananaPreview;
  if (!state) return;
  state.busy = false;
  if (state.liveButton) {
    state.liveButton.name = "加载预览图";
    state.liveButton.disabled = false;
  }
}

function collectPreviewParams(node) {
  const pick = (name, fallback) => {
    const widget = findWidget(node, name);
    if (!widget) return fallback;
    return typeof widget.value === "number" || typeof widget.value === "string"
      ? widget.value
      : fallback;
  };
  return {
    node_id: node.id,
    padding_slider: clampPadding(pick("padding_slider", 1.0)),
    blend_slider: pick("blend_slider", 1.0),
    expand_slider: pick("expand_slider", 0.12),
    scale_to_length: pick("scale_to_length", 2048),
    target_size: pick("target_size", 1536),
    round_to_multiple: pick("round_to_multiple", 8),
    overlay_color: pick("overlay_color", "#7f7f7f"),
  };
}

function setPreviewStatus(node, text) {
  const state = node.__bananaPreview;
  if (!state) return;
  state.status = text;
  node.graph?.setDirtyCanvas(true);
}

async function requestPreview(node) {
  const state = node.__bananaPreview;
  if (!state) return;
  if (state.busy) {
    state.pending = true;
    return;
  }
  state.busy = true;
  state.pending = false;

  // 统一管理按钮状态
  if (state.liveButton) {
    state.liveButton.name = "生成中...";
    state.liveButton.disabled = true;
  }

  setPreviewStatus(node, "预览生成中...");

  const payload = collectPreviewParams(node);
  try {
    const response = await api.fetchApi("/banana/local_crop_preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data?.success || !data.image_b64) {
      const message = data?.message || `HTTP ${response.status}`;
      state.image = null;
      // 对于缓存相关的错误，不再提示“先运行一次节点”，统一使用简短状态
      if (message.includes("缓存") || message.includes("未找到")) {
        setPreviewStatus(node, "暂无预览");
        // 如果是缓存缺失，允许有限次自动重试，避免必须手动动滑条
        state.autoAttempts = (state.autoAttempts || 0) + 1;
        if (state.autoAttempts <= 3) {
          window.setTimeout(() => schedulePreview(node), 900);
        }
      } else {
        setPreviewStatus(node, message);
      }
      // 注意：这里不要直接 resetButton，交给 finally 统一处理
      return;
    }
    state.autoAttempts = 0;
    const img = new Image();
    img.onload = () => {
      state.image = img;
      state.status = "";
      node.graph?.setDirtyCanvas(true);
      app.graph.setDirtyCanvas(true, true); // 强制刷新
    };
    img.onerror = () => {
      state.image = null;
      setPreviewStatus(node, "预览图加载失败");
    };
    img.src = `data:image/png;base64,${data.image_b64}`;
  } catch (error) {
    state.image = null;
    setPreviewStatus(node, error?.message || "预览失败");
  } finally {
    resetButton(node);
    if (state.pending) {
      state.pending = false;
      requestPreview(node);
    }
  }
}

function schedulePreview(node) {
  const state = node.__bananaPreview;
  if (!state) return;
  if (state.timer) {
    clearTimeout(state.timer);
  }
  state.timer = window.setTimeout(() => {
    state.timer = null;
    requestPreview(node);
  }, DEBOUNCE_MS);
}

function ensurePreviewWidget(node) {
  if (node.__bananaPreview) {
    return;
  }
  const state = {
    image: null,
    // 默认不显示“先运行一次节点”之类提示，保持界面简洁
    status: "",
    timer: null,
    busy: false,
    pending: false,
    autoAttempts: 0,
  };

  const widgetDef = {
    name: "banana-local-preview",
    type: "banana-local-preview",
    draw(ctx, _, widgetWidth, y, height) {
      const radius = 10;
      ctx.save();

      const w = widgetWidth || 320;

      // 计算节点剩余的实际可用高度
      // 忽略传入的 height (它是 computeSize 返回的最小值)，而是使用 node.size 决定的实际空间
      // 留出底部一点边距
      const margin = 10;
      // 确保 node.size 存在
      const nodeH = node.size ? node.size[1] : (height + y + margin);
      const availableH = Math.max(height, nodeH - y - margin);

      if (state.image) {
        const img = state.image;
        const boxW = w - 20;
        const boxH = availableH;

        // 保持比例缩放
        const scale = Math.min(boxW / img.width, boxH / img.height, 1);
        const drawW = img.width * scale;
        const drawH = img.height * scale;

        const offsetX = (w - drawW) / 2;
        // 垂直居中于可用空间
        const offsetY = y + (availableH - drawH) / 2;

        ctx.drawImage(img, offsetX, offsetY, drawW, drawH);
      } else {
        ctx.fillStyle = "#d9d9d9";
        ctx.font = "13px sans-serif";
        ctx.textBaseline = "middle";

        const text = state.status || "暂无预览";

        // 简单的自动换行逻辑
        const fontSize = 13;
        const lineHeight = 18;
        const maxWidth = w - 32; // 左右各留 16px 边距

        const words = text.split('');
        let line = '';
        const lines = [];

        for (let n = 0; n < words.length; n++) {
          const testLine = line + words[n];
          const metrics = ctx.measureText(testLine);
          const testWidth = metrics.width;
          if (testWidth > maxWidth && n > 0) {
            lines.push(line);
            line = words[n];
          } else {
            line = testLine;
          }
        }
        lines.push(line);

        // 垂直居中绘制多行文本
        const totalHeight = lines.length * lineHeight;
        let startY = y + (availableH - totalHeight) / 2 + lineHeight / 2; // + lineHeight/2 因为 textBaseline='middle'

        // 如果高度不够，就从顶部开始画，防止被切掉
        if (totalHeight > availableH) {
          startY = y + lineHeight / 2 + 10;
        }

        for (let i = 0; i < lines.length; i++) {
          ctx.fillText(lines[i], 16, startY + (i * lineHeight));
        }
      }
      ctx.restore();
    },
    computeSize(widgetWidth) {
      // 返回一个最小高度，保证节点至少有一定高度，但允许用户拉大
      // 256 像素是一个合理的最小预览高度
      return [widgetWidth || 320, 256];
    },
    mouse(event) {
      if (event?.type === "pointerdown") {
        schedulePreview(node);
        return true;
      }
      return false;
    },
  };

  const widget = node.addCustomWidget(widgetDef);
  // 显式赋值 computeSize，确保 LiteGraph 能正确调用
  widget.computeSize = widgetDef.computeSize;

  node.__bananaPreview = state;
  state.widget = widget;

  // 移除强制设置 widget.height，避免干扰布局引擎
  // widget.height = PREVIEW_HEIGHT;

  // 仅在初始化时设置一次较大的节点尺寸，之后允许用户自由调整
  // 检查当前高度是否过小，如果过小则撑大
  const currentSize = node.size || [320, 200];
  if (currentSize[1] < MIN_NODE_HEIGHT) {
    node.size = [currentSize[0], MIN_NODE_HEIGHT];
  }

  node.graph?.setDirtyCanvas(true);
  // 初次渲染后主动尝试一次，若已有缓存则直接出图；无缓存则按需重试
  schedulePreview(node);
}

// 递归查找节点的所有上游依赖节点ID
function findAncestors(graph, nodeId, ancestors = new Set()) {
  const id = String(nodeId);
  ancestors.add(id);
  const node = graph[id];
  if (!node) return ancestors;

  for (const key in node.inputs) {
    const link = node.inputs[key];
    // link 格式通常是 [origin_id, origin_slot]
    if (Array.isArray(link) && link.length > 0) {
      const originId = String(link[0]);
      if (!ancestors.has(originId)) {
        findAncestors(graph, originId, ancestors);
      }
    }
  }
  return ancestors;
}

async function triggerPartialRun(node) {
  const state = node.__bananaPreview;
  if (!state || state.busy) return;

  try {
    state.busy = true;
    if (state.liveButton) {
      state.liveButton.name = "生成中...";
      state.liveButton.disabled = true;
    }
    setPreviewStatus(node, "正在触发部分运行...");

    // 1. 获取当前完整的工作流提示
    // 注意：graphToPrompt 可能会因为图中有错误（如未连接的必选输入）而抛出异常
    const { output } = await app.graphToPrompt();

    // 2. 找出当前节点的所有上游依赖
    const ancestors = findAncestors(output, node.id);

    // 3. 构建部分提示，只包含依赖链上的节点
    const partialPrompt = {};
    for (const id of ancestors) {
      partialPrompt[id] = output[id];
    }

    // 4. 注入一个 PreviewImage 节点连接到当前节点的输出，强制触发执行
    // 注意：我们需要找到当前节点输出图像的 slot 索引，通常是 0 (image_1)
    const previewNodeId = "banana_preview_trigger_" + Date.now();
    partialPrompt[previewNodeId] = {
      inputs: {
        images: [String(node.id), 0]
      },
      class_type: "PreviewImage",
    };

    // 5. 提交任务
    const response = await api.fetchApi("/prompt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ prompt: partialPrompt }),
    });

    if (!response.ok) {
      throw new Error(`提交失败: ${response.status} ${response.statusText}`);
    }

    const responseData = await response.json();
    const promptId = responseData.prompt_id;

    // 任务提交成功后，先清空当前图片，避免显示老图
    state.image = null;
    app.graph.setDirtyCanvas(true, true); // 立即刷新画布
    setPreviewStatus(node, "等待执行...");

    // 记录开始时间
    const startTime = Date.now();
    let timeoutId = null;

    // 清理函数的辅助方法
    const cleanup = () => {
      if (timeoutId) clearTimeout(timeoutId);
      api.removeEventListener("execution_success", onExecutionSuccess);
      api.removeEventListener("execution_error", onExecutionError);
    };

    // 注册一次性的执行成功监听器
    const onExecutionSuccess = (e) => {
      if (e.detail.prompt_id !== promptId) return;

      cleanup();

      // 无论之前是否触发了 executed，只要收到 prompt 级成功信号，就强制刷新一次
      // 这是最稳妥的兜底，防止 executed 事件丢失或 ID 不匹配
      console.log("[Banana] Execution success, forcing preview update");
      state.busy = false;
      requestPreview(node);
    };

    const onExecutionError = (e) => {
      if (e.detail.prompt_id !== promptId) return;
      cleanup();
      // 错误处理由全局监听器负责，这里只需确保清理
    };

    api.addEventListener("execution_success", onExecutionSuccess);
    api.addEventListener("execution_error", onExecutionError);

    // 设置安全超时 (3000ms)
    // 调整为 3秒，平衡大图生成的等待时间和用户体验
    timeoutId = setTimeout(() => {
      console.warn("[Banana] Execution event timeout, forcing update");
      cleanup();
      // 关键修正：必须先解除 busy 状态
      state.busy = false;
      requestPreview(node);
      // requestPreview 会在 finally 中调用 resetButton，这里不需要重复调用
    }, 3000);

    // 注意：这里不再手动设置 state.busy = false，也不立即调用 requestPreview
    // 一切交给事件回调处理

  } catch (error) {
    console.error("[Banana] Partial run failed", error);
    // 显示具体的错误信息，方便排查
    const errMsg = error?.message || String(error);
    setPreviewStatus(node, `失败: ${errMsg.slice(0, 20)}...`);
    resetButton(node);
  }
}

function ensureLivePreviewButton(node) {
  const state = node.__bananaPreview;
  if (!state || state.liveButton) {
    return;
  }
  const widget = node.addWidget(
    "button",
    "加载预览图",
    "加载预览图",
    () => {
      triggerPartialRun(node);
    },
    { serialize: false }
  );
  state.liveButton = widget;

  // 移动到 overlay_color 下方，保持输入顺序一致
  if (Array.isArray(node.widgets)) {
    const overlayIndex = node.widgets.findIndex((w) => w.name === "overlay_color");
    const currentIndex = node.widgets.indexOf(widget);
    if (overlayIndex >= 0 && currentIndex >= 0 && currentIndex !== overlayIndex + 1) {
      node.widgets.splice(currentIndex, 1);
      node.widgets.splice(Math.min(overlayIndex + 1, node.widgets.length), 0, widget);
    }
  }
}

function watchWidgets(node) {
  const watched = [
    "padding_slider",
    "blend_slider",
    "expand_slider",
    "scale_to_length",
    "target_size",
    "round_to_multiple",
    "overlay_color",
  ];
  watched.forEach((name) => {
    const widget = findWidget(node, name);
    if (!widget || widget.__bananaPreviewWrapped) return;
    if (WIDGET_DISPLAY_LABELS[name]) {
      widget.label = WIDGET_DISPLAY_LABELS[name];
    }
    wrapWidget(node, widget, schedulePreview);
    widget.__bananaPreviewWrapped = true;
  });
}

function enhanceNode(node) {
  if (node.__bananaLocalPreviewReady) {
    return;
  }
  ensurePreviewWidget(node);
  ensureLivePreviewButton(node);
  watchWidgets(node);
  node.__bananaLocalPreviewReady = true;
}

function findExecutedNode(detail) {
  if (!detail || !app?.graph?.getNodeById) return null;
  const candidates = [];
  if (detail.display_node !== undefined) candidates.push(detail.display_node);
  if (detail.node !== undefined) candidates.push(detail.node);
  for (const id of candidates) {
    const resolved = app.graph.getNodeById(id);
    if (resolved) return resolved;
  }
  return null;
}

app.registerExtension({
  name: EXTENSION,
  nodeCreated(node) {
    if (node.comfyClass === TARGET) {
      enhanceNode(node);
    }
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET) {
      return;
    }
    const original = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = original?.apply(this, arguments);
      enhanceNode(this);
      return result;
    };
  },
  setup() {
    // 辅助函数：构建图片URL
    function getPreviewUrl(imageInfo) {
      if (!imageInfo) return null;
      const { filename, subfolder, type } = imageInfo;
      const params = new URLSearchParams({ filename, subfolder, type });
      return `/view?${params.toString()}`;
    }

    // 辅助函数：加载图片对象
    function loadImage(url) {
      return new Promise((resolve, reject) => {
        const img = new Image();
        img.onload = () => resolve(img);
        img.onerror = reject;
        img.src = url;
      });
    }

    // 核心逻辑：远程注入预览图到连接的节点
    function injectRemotePreview(node, imageInfo) {
      // ref_image 是第3个输出 (索引2)
      const refOutputIndex = 2;
      if (!node.outputs || !node.outputs[refOutputIndex] || !node.outputs[refOutputIndex].links) {
        return;
      }

      const links = node.outputs[refOutputIndex].links;
      for (const linkId of links) {
        const link = app.graph.links[linkId];
        if (!link) continue;

        const targetNode = app.graph.getNodeById(link.target_id);
        if (!targetNode) continue;

        // 检查是否是预览节点 (通常有 imgs 属性)
        // 我们直接把 imageInfo 塞给它，ComfyUI 的 PreviewImage 节点会自动处理显示
        if (targetNode.imgs !== undefined || targetNode.comfyClass === "PreviewImage") {
          // 注意：PreviewImage 期望的是一个数组
          targetNode.imgs = [imageInfo];
          // 强制刷新节点
          if (targetNode.setSize) {
            // 有些节点需要重新计算尺寸
            targetNode.setSize(targetNode.computeSize());
          }
          targetNode.setDirtyCanvas(true, true);
        }
      }
    }

    // 使用全局事件监听器来捕获节点执行完成事件
    api.addEventListener("executed", async ({ detail }) => {
      const node = findExecutedNode(detail);
      if (node && node.comfyClass === TARGET) {
        console.log("[Banana] Executed event for node:", node.id, detail);

        // 统一逻辑：收到执行完成信号后，强制解除忙碌状态并主动拉取图片
        // 这样无论是新运行还是缓存，都走同一条路径获取图片，保证一致性
        if (node.__bananaPreview) {
          node.__bananaPreview.busy = false;
          node.__bananaPreview.lastExecutedTime = Date.now();
        }

        // 保留远程预览注入 (image_2)
        if (detail?.output?.banana_images && detail.output.banana_images.length > 1) {
          const refInfo = detail.output.banana_images[1];
          injectRemotePreview(node, refInfo);
        }

        // 主动请求预览
        requestPreview(node);
      }
    });

    // 监听执行错误和中断事件，防止按钮卡死
    function resetOnInterruption() {
      const nodes = app.graph.findNodesByClass(TARGET);
      if (!nodes) return;
      for (const node of nodes) {
        const state = node.__bananaPreview;
        if (state && state.busy) {
          setPreviewStatus(node, "执行已中断");
          resetButton(node);
        }
      }
    }

    api.addEventListener("execution_error", resetOnInterruption);
    api.addEventListener("execution_interrupted", resetOnInterruption);
  },
});
