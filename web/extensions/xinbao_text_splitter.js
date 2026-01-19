import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";
import { addWidget, DOMWidgetImpl } from "../../../scripts/domWidget.js";

const EXTENSION = "xinbao.textSplitter";
const TARGET_NODE = "XinbaoTextSplitter";
const WAIT_EVENT = "xinbao_prompt_split_wait";
const CONFIRM_URL = "/banana/prompt_split/confirm";
const DEFAULT_TIMEOUT_SEC = 60;
const SEGMENT_MIN_HEIGHT = 70;
const NODE_RESIZE_NOTCH_PX = 18;

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

function findWidget(node, name) {
  if (!node || !Array.isArray(node.widgets)) return null;
  return node.widgets.find((w) => w?.name === name) || null;
}

function getTextareaEl(widget) {
  const direct = widget?.inputEl;
  if (direct && direct.tagName && String(direct.tagName).toUpperCase() === "TEXTAREA") {
    return direct;
  }
  const raw = widget?.inputEl || widget?.domEl || widget?.element;
  if (raw && typeof raw.querySelector === "function") {
    try {
      return raw.querySelector("textarea");
    } catch (_) {
      return null;
    }
  }
  return null;
}

function ensureState(node) {
  if (!node.__xinbaoPromptSplit) {
    node.__xinbaoPromptSplit = {
      segmentWidgets: [],
      statusWidget: null,
      confirmWidget: null,
      countdownTimer: null,
      waiting: false,
      deadline: null,
      nodeId: null,
      resizeWrapped: false,
      resizeSyncRaf: 0,
      pendingResizeSize: null,
      removedWrapped: false,
    };
  }
  return node.__xinbaoPromptSplit;
}

function getEffectiveNodeId(node) {
  const state = ensureState(node);
  return state.nodeId || String(node?.id || "");
}

function cleanupWidgetDom(widget) {
  const candidates = [widget?.inputEl, widget?.domEl, widget?.element];
  candidates.forEach((el) => {
    if (!el) return;
    try {
      if (typeof el.remove === "function") {
        el.remove();
      } else if (el.parentNode) {
        el.parentNode.removeChild(el);
      }
    } catch (_) {
      // ignore
    }
  });
}

function applySegmentWidgetHeight(widget, heightPx) {
  if (!widget) return;
  const normalized = Number.isFinite(Number(heightPx)) ? Math.max(40, Math.round(Number(heightPx))) : SEGMENT_MIN_HEIGHT;

  try {
    if (widget.options && typeof widget.options === "object") {
      widget.options.height = normalized;
    }
  } catch (_) {
    // ignore
  }
  try {
    widget.height = normalized;
  } catch (_) {
    // ignore
  }
  try {
    widget.computeSize = (width) => [width, normalized + 4];
  } catch (_) {
    // ignore
  }
  try {
    widget.last_y = 0;
  } catch (_) {
    // ignore
  }

  const textarea = getTextareaEl(widget);
  if (!textarea || !textarea.style) return;
  try {
    textarea.style.setProperty("box-sizing", "border-box", "important");
    textarea.style.setProperty("min-height", `${normalized}px`, "important");
    textarea.style.setProperty("height", `${normalized}px`, "important");
    textarea.style.setProperty("overflow-y", "auto", "important");
    // 允许节点缩放时保持布局稳定，禁用 textarea 自身拖拽改高度，避免与 widget 高度脱节导致重叠
    textarea.style.setProperty("resize", "none", "important");
    // 让右下角保留一小块“可点穿”区域，避免 DOM 覆盖导致无法拖动 LiteGraph 节点的原生缩放手柄
    textarea.style.setProperty(
      "clip-path",
      `polygon(0 0, 100% 0, 100% calc(100% - ${NODE_RESIZE_NOTCH_PX}px), calc(100% - ${NODE_RESIZE_NOTCH_PX}px) calc(100% - ${NODE_RESIZE_NOTCH_PX}px), calc(100% - ${NODE_RESIZE_NOTCH_PX}px) 100%, 0 100%)`,
      "important"
    );
  } catch (_) {
    // ignore
  }
}

function tagSegmentTextarea(textarea, nodeId) {
  if (!textarea || !nodeId) return;
  try {
    textarea.dataset.xinbaoPromptSplit = "1";
    textarea.dataset.xinbaoPromptSplitNodeId = String(nodeId);
  } catch (_) {
    // ignore
  }
}

function purgeOrphanedSegmentTextareas(nodeId, keep) {
  if (!nodeId) return;
  const keepSet = keep instanceof Set ? keep : new Set();
  const selector = `textarea[data-xinbao-prompt-split-node-id=\"${CSS.escape(String(nodeId))}\"]`;
  try {
    document.querySelectorAll(selector).forEach((el) => {
      if (!keepSet.has(el)) {
        try {
          el.remove();
        } catch (_) {
          // ignore
        }
      }
    });
  } catch (_) {
    // ignore
  }
}

function removeWidgets(node, predicate) {
  if (!node || !Array.isArray(node.widgets)) return;
  for (let i = node.widgets.length - 1; i >= 0; i -= 1) {
    const widget = node.widgets[i];
    if (predicate(widget)) {
      try {
        widget.onRemoved?.();
      } catch (_) {
        // ignore
      }
      try {
        widget.onRemove?.();
      } catch (_) {
        // ignore
      }
      cleanupWidgetDom(widget);
      if (Array.isArray(node.inputs)) {
        node.inputs = node.inputs.filter((input) => input?.widget !== widget);
      }
      node.widgets.splice(i, 1);
    }
  }
}

function moveWidgetToEnd(node, widget) {
  if (!node || !widget || !Array.isArray(node.widgets)) return;
  const idx = node.widgets.indexOf(widget);
  if (idx >= 0 && idx !== node.widgets.length - 1) {
    node.widgets.splice(idx, 1);
    node.widgets.push(widget);
  }
}

function setStatus(node, text) {
  const state = ensureState(node);
  if (!state.statusWidget) return;
  state.statusWidget.value = text || "";
  node.graph?.setDirtyCanvas(true);
}

function stopCountdown(node) {
  const state = ensureState(node);
  if (state.countdownTimer) {
    clearInterval(state.countdownTimer);
    state.countdownTimer = null;
  }
  state.deadline = null;
}

function updateCountdown(node) {
  const state = ensureState(node);
  if (!state.waiting || !state.deadline) return;
  const remainingMs = Math.max(0, state.deadline - Date.now());
  const remainingSec = Math.ceil(remainingMs / 1000);
  if (remainingSec <= 0) {
    stopCountdown(node);
    state.waiting = false;
    if (state.confirmWidget) state.confirmWidget.disabled = true;
    setStatus(node, "等待超时，已自动继续");
    return;
  }
  setStatus(node, `等待确认中，剩余 ${remainingSec}s`);
}

function startCountdown(node, seconds) {
  const state = ensureState(node);
  stopCountdown(node);
  state.deadline = Date.now() + seconds * 1000;
  updateCountdown(node);
  state.countdownTimer = setInterval(() => updateCountdown(node), 500);
}

function collectSegments(node) {
  const state = ensureState(node);
  return state.segmentWidgets.map((widget) => {
    const textarea = getTextareaEl(widget);
    if (textarea) return textarea.value ?? "";
    const value = widget?.value;
    return value === undefined || value === null ? "" : String(value);
  });
}

async function sendConfirm(node) {
  const state = ensureState(node);
  if (!state.waiting) {
    setStatus(node, "当前未处于等待确认状态");
    return;
  }
  const nodeId = state.nodeId || String(node.id || "");
  if (!nodeId) {
    setStatus(node, "节点ID缺失，无法确认");
    return;
  }

  const prompts = collectSegments(node);
  if (state.confirmWidget) state.confirmWidget.disabled = true;
  setStatus(node, "正在确认..." );

  try {
    const response = await api.fetchApi(CONFIRM_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ node_id: nodeId, prompts }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data?.success) {
      const message = data?.message || `确认失败: ${response.status}`;
      if (state.confirmWidget) state.confirmWidget.disabled = false;
      setStatus(node, message);
      return;
    }
    stopCountdown(node);
    state.waiting = false;
    setStatus(node, "已确认，等待执行继续..." );
  } catch (error) {
    if (state.confirmWidget) state.confirmWidget.disabled = false;
    setStatus(node, error?.message || "确认失败");
  }
}

function applyTextareaStyle(widget) {
  applySegmentWidgetHeight(widget, SEGMENT_MIN_HEIGHT);
}

function addSegmentTextareaWidget(node, nodeId, name, value) {
  const inputEl = document.createElement("textarea");
  inputEl.className = "comfy-multiline-input";
  inputEl.value = value ?? "";
  inputEl.placeholder = name;
  inputEl.spellcheck = false;

  const widget = new DOMWidgetImpl({
    node,
    name,
    type: "customtext",
    element: inputEl,
    options: {
      getValue() {
        return inputEl.value;
      },
      setValue(v) {
        inputEl.value = v === undefined || v === null ? "" : String(v);
      },
    },
  });
  addWidget(node, widget);

  widget.inputEl = inputEl;
  widget.serialize = false;
  widget.__xinbaoPromptSplitSegment = true;

  inputEl.addEventListener("input", () => {
    widget.value = inputEl.value;
    widget.callback?.(inputEl.value, true);
  });

  tagSegmentTextarea(inputEl, nodeId);
  applyTextareaStyle(widget);
  return widget;
}

function renderSegments(node, segments) {
  const state = ensureState(node);
  const nodeId = getEffectiveNodeId(node);
  purgeOrphanedSegmentTextareas(nodeId, new Set());
  removeWidgets(node, (w) => w?.__xinbaoPromptSplitSegment || String(w?.name || "").startsWith("提示词-"));
  state.segmentWidgets = [];

  const normalized = Array.isArray(segments) && segments.length ? segments : [""];
  normalized.forEach((segment, index) => {
    const widget = addSegmentTextareaWidget(node, nodeId, `提示词-${index + 1}`, segment);
    state.segmentWidgets.push(widget);
  });

  moveWidgetToEnd(node, state.statusWidget);
  moveWidgetToEnd(node, state.confirmWidget);
  node.onResize?.(node.size);
  node.graph?.setDirtyCanvas(true, true);
}

function syncSegmentDom(node) {
  const state = ensureState(node);
  const nodeId = getEffectiveNodeId(node);
  const keep = new Set();
  state.segmentWidgets.forEach((widget) => {
    const textarea = getTextareaEl(widget);
    if (textarea) {
      tagSegmentTextarea(textarea, nodeId);
      keep.add(textarea);
    }
    applySegmentWidgetHeight(widget, SEGMENT_MIN_HEIGHT);
  });
  purgeOrphanedSegmentTextareas(nodeId, keep);
}

function wrapNodeResize(node) {
  const state = ensureState(node);
  if (state.resizeWrapped) return;
  const original = node.onResize;
  node.onResize = function () {
    const maybeSize = arguments?.[0];
    if (Array.isArray(maybeSize) && maybeSize.length >= 2) {
      const w = Number(maybeSize[0]);
      const h = Number(maybeSize[1]);
      if (Number.isFinite(w) && Number.isFinite(h)) {
        state.pendingResizeSize = [w, h];
      }
    }

    const result = original?.apply(this, arguments);
    const target = this;

    if (!state.resizeSyncRaf) {
      state.resizeSyncRaf = requestAnimationFrame(() => {
        state.resizeSyncRaf = 0;
        const desired = state.pendingResizeSize;
        state.pendingResizeSize = null;

        // 某些 ComfyUI/LiteGraph 组合会在 onResize 内部“强制回写 computeSize()”，导致用户拖拽后立刻回弹。
        // 这里将用户请求的 size 再次写回（不调用 setSize，避免递归触发 onResize）。
        if (Array.isArray(desired) && Array.isArray(target.size) && target.size.length >= 2) {
          try {
            target.size[0] = desired[0];
            target.size[1] = desired[1];
          } catch (_) {
            // ignore
          }
        }

        try {
          syncSegmentDom(target);
          target.graph?.setDirtyCanvas(true, true);
        } catch (_) {
          // ignore
        }
      });
    }

    return result;
  };
  state.resizeWrapped = true;
}

function wrapNodeRemoved(node) {
  const state = ensureState(node);
  if (state.removedWrapped) return;
  const original = node.onRemoved;
  node.onRemoved = function () {
    try {
      stopCountdown(this);
    } catch (_) {
      // ignore
    }
    try {
      if (state.resizeSyncRaf) {
        cancelAnimationFrame(state.resizeSyncRaf);
        state.resizeSyncRaf = 0;
      }
    } catch (_) {
      // ignore
    }
    try {
      const nodeId = getEffectiveNodeId(this);
      purgeOrphanedSegmentTextareas(nodeId, new Set());
      removeWidgets(this, (w) => w?.__xinbaoPromptSplitSegment || String(w?.name || "").startsWith("提示词-"));
    } catch (_) {
      // ignore
    }
    return original?.apply(this, arguments);
  };
  state.removedWrapped = true;
}

function ensureBaseWidgets(node) {
  const state = ensureState(node);
  if (!state.statusWidget) {
    const statusWidget = node.addWidget(
      "text",
      "分段状态",
      "",
      () => {},
      { serialize: false }
    );
    state.statusWidget = statusWidget;
  }
  if (!state.confirmWidget) {
    const confirmWidget = node.addWidget(
      "button",
      "确认修改",
      "确认修改",
      () => sendConfirm(node),
      { serialize: false }
    );
    state.confirmWidget = confirmWidget;
  }
  state.confirmWidget.disabled = true;
  wrapNodeResize(node);
  wrapNodeRemoved(node);
}

function updatePauseUI(node) {
  const state = ensureState(node);
  const pauseWidget = findWidget(node, "暂停等待");
  if (!pauseWidget) return;

  const paused = Boolean(pauseWidget.value);
  if (state.waiting) {
    if (state.confirmWidget) state.confirmWidget.disabled = false;
    return;
  }
  if (!paused) {
    if (state.confirmWidget) state.confirmWidget.disabled = true;
    if (state.segmentWidgets.length > 0) {
      setStatus(node, "暂停等待已关闭，仍会直接输出分段结果");
    } else {
      setStatus(node, "");
    }
  }
}

function attachPauseWatcher(node) {
  const pauseWidget = findWidget(node, "暂停等待");
  if (!pauseWidget || pauseWidget.__xinbaoPromptSplitWrapped) return;
  const original = pauseWidget.callback;
  pauseWidget.callback = function () {
    const result = original?.apply(this, arguments);
    updatePauseUI(node);
    return result;
  };
  pauseWidget.__xinbaoPromptSplitWrapped = true;
}

function applyWaitEvent(payload) {
  const nodeId = String(payload?.node_id || "");
  if (!nodeId || !app?.graph?.getNodeById) return;
  const node = app.graph.getNodeById(nodeId);
  if (!node || node.comfyClass !== TARGET_NODE) return;

  const state = ensureState(node);
  state.nodeId = nodeId;
  ensureBaseWidgets(node);
  attachPauseWatcher(node);

  const timeoutSec = Number(payload?.timeout_sec || DEFAULT_TIMEOUT_SEC);
  renderSegments(node, payload?.prompts || [""]);
  state.waiting = true;
  if (state.confirmWidget) state.confirmWidget.disabled = false;
  startCountdown(node, Number.isFinite(timeoutSec) ? timeoutSec : DEFAULT_TIMEOUT_SEC);
}

function applyExecutedOutput(detail) {
  const node = findExecutedNode(detail);
  if (!node || node.comfyClass !== TARGET_NODE) return;
  const data = detail?.output?.xinbao_prompt_split;
  const segments = data?.segments;
  if (!Array.isArray(segments)) return;

  const state = ensureState(node);
  const segmentLimit = Number(data?.segment_limit || 0);
  const originalCount = Number(data?.original_count || segments.length);
  const truncated = Boolean(data?.truncated);
  state.nodeId = data?.node_id || state.nodeId || String(node.id || "");
  ensureBaseWidgets(node);
  attachPauseWatcher(node);
  renderSegments(node, segments);

  if (!state.waiting) {
    if (state.confirmWidget) state.confirmWidget.disabled = true;
    if (truncated) {
      const limitText = segmentLimit > 0 ? segmentLimit : segments.length;
      setStatus(node, `分段超过上限（${limitText}），已截断为 ${segments.length} 段（原始 ${originalCount} 段）`);
      return;
    }
    if (segments.length > 0) {
      setStatus(node, "分段结果已更新");
    }
  }
}

app.registerExtension({
  name: EXTENSION,
  nodeCreated(node) {
    if (node?.comfyClass !== TARGET_NODE) return;
    try {
      node.resizable = true;
    } catch (_) {
      // ignore
    }
    ensureBaseWidgets(node);
    attachPauseWatcher(node);
    updatePauseUI(node);
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) return;
    const original = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = original?.apply(this, arguments);
      try {
        this.resizable = true;
      } catch (_) {
        // ignore
      }
      ensureBaseWidgets(this);
      attachPauseWatcher(this);
      updatePauseUI(this);
      return result;
    };
  },
  setup() {
    api.addEventListener(WAIT_EVENT, (e) => applyWaitEvent(e.detail));
    api.addEventListener("executed", ({ detail }) => applyExecutedOutput(detail));
  },
});
