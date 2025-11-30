import { app } from "/scripts/app.js";

// DEBUG: 确认扩展是否加载
// window.alert("Banana Purge Extension Loaded v5 (Debug Enabled)");

const EXTENSION = "banana.apiKeyPurge";
const CLEANER_CLASS = "XinbaoApiKeyPurge";
const CHANNEL_BANANA = "香蕉同款渠道";
const CHANNEL_MODAO = "魔搭社区";
const TARGETS = [
  { className: "BananaImageNode", fields: ["banana_api_key"] },
  { className: "XinbaoModelScopeImageGenerate", fields: ["modelscope_api_key"] },
  { className: "XinbaoModelScopeCaption", fields: ["banana_api_key", "modelscope_api_key"] },
];
const CLEANER_FIELDS = {
  banana: "banana_global_api_key",
  modao: "modelscope_global_api_key",
};

function findWidget(node, name) {
  if (!node) return null;
  if (Array.isArray(node.widgets)) {
    const hit = node.widgets.find((widget) => widget?.name === name);
    if (hit) return hit;
  }
  if (Array.isArray(node.inputs)) {
    for (const input of node.inputs) {
      if (input?.name === name && input.widget) {
        return input.widget;
      }
    }
  }
  return null;
}

function stripLegacyAutoCleanWidget(node) {
  if (!node || !Array.isArray(node.widgets)) return;
  const idx = node.widgets.findIndex((w) => w?.name === "导出时清除apikey");
  if (idx >= 0) {
    node.widgets.splice(idx, 1);
    markDirty(node);
  }
}

function isEmptyValue(value) {
  return typeof value !== "string" || value.trim().length === 0;
}

function setWidgetValue(widget, value) {
  if (!widget) return false;
  if (widget.value === value) return false;
  widget.value = value;
  return true;
}

function markDirty(node) {
  node?.graph?.setDirtyCanvas(true);
  app?.graph?.setDirtyCanvas(true, true);
}

function findNodesByClassName(className) {
  const seen = new Set();
  const results = [];
  const graph = app?.graph;
  if (!graph) return results;

  if (app?.graph?.findNodesByClass) {
    const hits = app.graph.findNodesByClass(className) || [];
    hits.forEach((n) => {
      if (n && !seen.has(n.id)) {
        seen.add(n.id);
        results.push(n);
      }
    });
  }

  if (app?.graph?.findNodesByType) {
    const hits = app.graph.findNodesByType(className) || [];
    hits.forEach((n) => {
      if (n && !seen.has(n.id)) {
        seen.add(n.id);
        results.push(n);
      }
    });
  }

  // 手动遍历兜底，兼容未暴露查找 API 的版本
  const nodesArray = graph._nodes || graph.nodes || [];
  nodesArray.forEach((n) => {
    const type = n?.type || n?.comfyClass || n?.constructor?.type;
    if (type === className && !seen.has(n.id)) {
      seen.add(n.id);
      results.push(n);
    }
  });

  return results;
}

function collectCleanerNodes() {
  return findNodesByClassName(CLEANER_CLASS);
}

function readGlobalKeys() {
  const cleaners = collectCleanerNodes();
  let bananaKey = "";
  let modaoKey = "";

  cleaners.forEach((node) => {
    const bananaWidget = findWidget(node, CLEANER_FIELDS.banana);
    const modaoWidget = findWidget(node, CLEANER_FIELDS.modao);

    if (!bananaKey && bananaWidget && typeof bananaWidget.value === "string") {
      bananaKey = bananaWidget.value.trim();
    }
    if (!modaoKey && modaoWidget && typeof modaoWidget.value === "string") {
      modaoKey = modaoWidget.value.trim();
    }
  });

  return { bananaKey, modaoKey };
}

function clearNodeWidget(node, widgetOrName) {
  if (!node) return false;
  let widget = widgetOrName;
  if (typeof widgetOrName === "string") {
    widget = findWidget(node, widgetOrName);
  }
  // 如果是 Primitive Node，通常只有一个主 widget，尝试获取它
  if (!widget && node.widgets && node.widgets.length > 0) {
    if (node.type === "PrimitiveNode" || node.comfyClass === "PrimitiveNode") {
      widget = node.widgets[0];
    }
  }

  if (widget) {
    // 即使值为空也尝试清除，以防 UI 状态不同步
    const oldValue = widget.value;
    // console.log(`[${EXTENSION}] Inspecting widget ${widget.name} on ${node.type}. Value:`, oldValue);

    if (!isEmptyValue(oldValue)) {
      console.log(`[${EXTENSION}] Clearing widget ${widget.name} on node ${node.id} (${node.type})`);
      widget.value = "";

      // 关键修复：同时更新 DOM 元素
      if (widget.inputEl) {
        widget.inputEl.value = "";
      }
      if (widget.domEl) { // 某些自定义 Widget 可能用这个
        widget.domEl.value = "";
      }
      if (widget.element) { // 某些 LiteGraph Widget
        widget.element.value = "";
      }

      // 触发回调
      if (widget.callback) {
        try {
          widget.callback(widget.value);
        } catch (e) {
          console.warn(`[${EXTENSION}] Widget callback failed`, e);
        }
      }
      return true;
    }
  }
  return false;
}

function findUpstreamNodes(node, inputName) {
  const upstreamNodes = [];
  if (!node || !node.inputs) return upstreamNodes;

  const input = node.inputs.find((i) => i.name === inputName);
  if (!input || !input.link) return upstreamNodes;

  const linkId = input.link;
  const graph = app.graph;
  if (!graph || !graph.links) return upstreamNodes;

  const link = graph.links[linkId];
  if (!link) return upstreamNodes;

  const originNode = graph.getNodeById(link.origin_id);
  if (originNode) {
    upstreamNodes.push(originNode);
    // 递归查找：如果源节点是 Reroute，继续向上找
    if (originNode.type === "Reroute" || originNode.comfyClass === "Reroute") {
      if (originNode.inputs && originNode.inputs.length > 0) {
        const firstInput = originNode.inputs[0];
        upstreamNodes.push(...findUpstreamNodes(originNode, firstInput.name));
      }
    }
  }
  return upstreamNodes;
}

function clearTargets(includeCleaners = true, options = {}) {
  if (typeof includeCleaners === "object") {
    options = includeCleaners;
    includeCleaners = true;
  }
  const silent = options?.silent === true;
  if (!app?.graph) return;
  let changed = false;
  let clearedCount = 0;

  if (!silent) {
    console.log(`[${EXTENSION}] Starting API Key purge...`);
  }

  TARGETS.forEach((target) => {
    const nodes = findNodesByClassName(target.className);
    nodes.forEach((node) => {
      let nodeChanged = false;
      target.fields.forEach((field) => {
        // 1. 清除节点自身的 Widget
        if (clearNodeWidget(node, field)) {
          nodeChanged = true;
          clearedCount++;
        }

        // 2. 追踪并清除上游节点 (如 PrimitiveNode)
        const upstreamNodes = findUpstreamNodes(node, field);
        upstreamNodes.forEach(upstreamNode => {
          if (upstreamNode.widgets) {
            upstreamNode.widgets.forEach(w => {
              // 放宽检查：只要是字符串且非空，就尝试清除
              if (typeof w.value === "string" && w.value.trim().length > 0) {
                if (clearNodeWidget(upstreamNode, w)) {
                  nodeChanged = true;
                  markDirty(upstreamNode);
                  clearedCount++;
                }
              }
            });
          }
        });
      });

      if (nodeChanged) {
        markDirty(node);
      }
    });
  });

  if (includeCleaners) {
    collectCleanerNodes().forEach((node) => {
      let nodeChanged = false;
      [CLEANER_FIELDS.banana, CLEANER_FIELDS.modao].forEach((field) => {
        if (clearNodeWidget(node, field)) {
          nodeChanged = true;
          clearedCount++;
        }
      });
      if (nodeChanged) {
        markDirty(node);
      }
    });
  }

  if (clearedCount > 0) {
    const msg = `已清除 ${clearedCount} 个 API Key`;
    console.log(`[${EXTENSION}] ${msg}`);
    if (!silent) {
      app.ui.dialog.show(msg);
    }
    changed = true;
  } else {
    console.log(`[${EXTENSION}] No API keys found to purge.`);
    // 只有在手动触发时（includeCleaners=true）才提示未找到
    if (includeCleaners) {
      // app.ui.dialog.show("未发现可清除的 API Key"); 
    }
  }

  return changed;
}

function performClean(includeCleaners = true, options = {}) {
  if (typeof includeCleaners === "object") {
    options = includeCleaners;
    includeCleaners = true;
  }
  const silent = options?.silent === true;
  if (!silent) {
    console.log(`[${EXTENSION}] Manual clean triggered.`);
  }
  clearTargets(includeCleaners, { silent });
}

function applyBackfillTransient() {
  if (!app?.graph) return () => {};
  const { bananaKey, modaoKey } = readGlobalKeys();
  if (!bananaKey && !modaoKey) return () => {};
  const revertRecords = [];

  const setIfEmpty = (node, fieldName, value) => {
    const widget = findWidget(node, fieldName);
    if (!widget || isEmptyValue(widget.value) === false) return;
    revertRecords.push({ widget, prev: widget.value });
    widget.value = value;
  };

  const bananaNodes = findNodesByClassName("BananaImageNode");
  bananaNodes.forEach((node) => {
    if (!isEmptyValue(bananaKey)) {
      setIfEmpty(node, "banana_api_key", bananaKey);
    }
  });

  const modaoNodes = findNodesByClassName("XinbaoModelScopeImageGenerate");
  modaoNodes.forEach((node) => {
    if (!isEmptyValue(modaoKey)) {
      setIfEmpty(node, "modelscope_api_key", modaoKey);
    }
  });

  const captionNodes = findNodesByClassName("XinbaoModelScopeCaption");
  captionNodes.forEach((node) => {
    const channelWidget = findWidget(node, "channel");
    const channelValue = channelWidget?.value || CHANNEL_BANANA;
    if (channelValue === CHANNEL_MODAO) {
      if (!isEmptyValue(modaoKey)) {
        setIfEmpty(node, "modelscope_api_key", modaoKey);
      }
    } else {
      if (!isEmptyValue(bananaKey)) {
        setIfEmpty(node, "banana_api_key", bananaKey);
      }
    }
  });

  return () => {
    revertRecords.forEach(({ widget, prev }) => {
      widget.value = prev;
    });
  };
}

function ensureManualButton(node) {
  if (!node || node.__bananaKeyPurgeReady) return;
  stripLegacyAutoCleanWidget(node);

  const widget = node.addCustomWidget({
    name: "banana-purge-now",
    type: "banana-purge-now",
    node,
    draw(ctx, _, widgetWidth, y, height) {
      const text = "立即清除全图apikey";
      const font = "12px sans-serif";
      const paddingX = 14;
      const marginTop = 6;
      const radius = 8;
      const active = this.__active;
      const previousFont = ctx.font;
      const previousAlign = ctx.textAlign;
      ctx.font = font;
      const textWidth = ctx.measureText(text).width;
      const rectWidth = Math.max(textWidth + paddingX * 2, 170);
      const rectHeight = Math.max((height || 22), 22);
      const x = (widgetWidth - rectWidth) / 2;
      const yPos = y + marginTop;
      ctx.fillStyle = active ? "#c0392b" : "#e74c3c";
      ctx.strokeStyle = active ? "#922b21" : "#b03a2e";
      ctx.beginPath();
      if (ctx.roundRect) {
        ctx.roundRect(x, yPos, rectWidth, rectHeight, radius);
      } else {
        ctx.rect(x, yPos, rectWidth, rectHeight);
      }
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#ffffff";
      ctx.textAlign = "center";
      ctx.fillText(text, x + rectWidth / 2, yPos + rectHeight * 0.65);
      ctx.font = previousFont;
      ctx.textAlign = previousAlign;
      this.__rect = { x, y: yPos, w: rectWidth, h: rectHeight };
    },
    mouse(event, position) {
      const trigger = ((globalThis.LiteGraph && globalThis.LiteGraph.pointerevents_method) || "pointer") + "down";
      if (event?.type !== trigger) return false;
      const rect = this.__rect;
      if (!rect) return false;
      const [x, y] = position;
      if (x >= rect.x && x <= rect.x + rect.w && y >= rect.y && y <= rect.y + rect.h) {
        this.__active = true;
        this.node?.graph?.setDirtyCanvas(true, true);
        setTimeout(() => {
          this.__active = false;
          this.node?.graph?.setDirtyCanvas(true, true);
        }, 180);
        performClean();
        return true;
      }
      return false;
    },
    computeSize(widgetWidth) {
      // extra top margin
      return [widgetWidth, 34];
    },
    serialize: false,
  });
  node.__bananaKeyPurgeReady = true;
  // 尽量把按钮挪到密钥输入后面，便于发现
  if (Array.isArray(node.widgets) && widget) {
    const index = node.widgets.indexOf(widget);
    const bananaIdx = node.widgets.findIndex((w) => w.name === CLEANER_FIELDS.banana);
    const modaoIdx = node.widgets.findIndex((w) => w.name === CLEANER_FIELDS.modao);
    const targetIdx = Math.max(bananaIdx, modaoIdx);
    if (index > -1 && targetIdx > -1 && index < targetIdx) {
      node.widgets.splice(index, 1);
      node.widgets.splice(targetIdx + 1, 0, widget);
    }
  }
}

function wrapWithPreAction(target, method, preAction) {
  if (!target || typeof target[method] !== "function") return;
  const original = target[method];
  if (original.__bananaWrapped) return;
  target[method] = async function (...args) {
    try {
      await preAction();
    } catch (error) {
      console.warn(`[${EXTENSION}] 预处理失败(${method})`, error);
    }
    return original.apply(this, args);
  };
  target[method].__bananaWrapped = true;
}

function setupBackfill() {
  const RUN_METHODS = ["queuePrompt", "enqueuePrompt", "processQueue"];
  RUN_METHODS.forEach((method) => {
    wrapWithPreAction(app, method, () => {
      const revert = applyBackfillTransient();
      // 将恢复动作挂到微任务，保证调用结束后还原
      setTimeout(revert, 0);
    });
    if (app?.ui) {
      wrapWithPreAction(app.ui, method, () => {
        const revert = applyBackfillTransient();
        setTimeout(revert, 0);
      });
    }
  });
}

app.registerExtension({
  name: EXTENSION,
  setup() {
    collectCleanerNodes().forEach(stripLegacyAutoCleanWidget);
    setupBackfill();
  },
  nodeCreated(node) {
    if (node?.comfyClass === CLEANER_CLASS) {
      ensureManualButton(node);
    }
  },
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== CLEANER_CLASS) {
      return;
    }
    const original = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = original?.apply(this, arguments);
      ensureManualButton(this);
      return result;
    };
  },
});
