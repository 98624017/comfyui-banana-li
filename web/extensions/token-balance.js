import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const EXTENSION = "banana.tokenBalance";
const TARGET_NODES = new Set(["BananaImageNode", "BananaImageNodeV2", "BananaImageNodeV3"]);
const WECHAT_ID = "Li_18727107073";
const QR_IMAGE_URL = new URL("./xinbao.jpg", import.meta.url).toString();
const ACTION_BUTTON_DEFS = [
  { key: "wechat", label: "兑换积分\n明细查询" },
  { key: "query", label: "余额查询\n1R=60积分" },
  { key: "qr", label: "获取积分\n领取试用" },
];
const BUTTON_FEEDBACK_MS = 1600;
const liteGraphGlobal = typeof globalThis !== "undefined" ? globalThis.LiteGraph : undefined;
const POINTER_DOWN_EVENT = `${(liteGraphGlobal && liteGraphGlobal.pointerevents_method) || "pointer"}down`;
const BUTTON_ROW_HEIGHT = Math.max((liteGraphGlobal && liteGraphGlobal.NODE_WIDGET_HEIGHT) || 20, 38);
const BUTTON_ROW_MARGIN = 14;
const BUTTON_ROW_GAP = 8;
const MIN_BUTTON_WIDTH = 78;

let qrOverlay;

function ensureQrOverlay() {
  if (qrOverlay) {
    return qrOverlay;
  }
  const overlay = document.createElement("div");
  overlay.style.cssText = `
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 10000;
  `;

  const panel = document.createElement("div");
  panel.style.cssText = `
    background: #1f1f1f;
    padding: 18px 24px 20px;
    border-radius: 14px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.35);
    display: flex;
    flex-direction: column;
    gap: 16px;
    align-items: center;
    color: #f5f5f5;
    min-width: 280px;
  `;

  const title = document.createElement("div");
  title.textContent = `添加UP主微信：${WECHAT_ID}\n进行购买领取试用`;
  title.style.fontSize = "16px";
  title.style.fontWeight = "600";
  title.style.whiteSpace = "pre-line";
  title.style.textAlign = "center";

  const img = document.createElement("img");
  img.src = QR_IMAGE_URL;
  img.alt = "UP主二维码";
  img.style.cssText = "width: 280px; height: auto; max-height: 70vh; object-fit: contain; border-radius: 8px; background: #fff; padding: 8px;";
  img.addEventListener("error", () => {
    img.alt = "二维码加载失败，请手动复制微信号";
    img.style.background = "#2b2b2b";
  });

  const close = document.createElement("button");
  close.textContent = "关闭";
  close.style.cssText = `
    padding: 6px 20px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.25);
    background: transparent;
    color: inherit;
    cursor: pointer;
  `;
  close.addEventListener("click", () => {
    overlay.style.display = "none";
  });

  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) {
      overlay.style.display = "none";
    }
  });

  panel.appendChild(title);
  panel.appendChild(img);
  panel.appendChild(close);
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  qrOverlay = overlay;
  return overlay;
}

function showQrOverlay() {
  const overlay = ensureQrOverlay();
  overlay.style.display = "flex";
}

function getButtonEntry(node, key) {
  return node.__bananaBalanceWidgets?.actionButtonMap?.[key];
}

function setButtonDisabled(node, key, disabled) {
  const entry = getButtonEntry(node, key);
  if (!entry) {
    return;
  }
  entry.disabled = disabled;
  node?.graph?.setDirtyCanvas(true);
}

function flashActionLabel(node, key, text, duration = BUTTON_FEEDBACK_MS) {
  const entry = getButtonEntry(node, key);
  if (!entry) {
    return;
  }
  entry.feedbackLabel = text;
  if (entry.feedbackTimer) {
    clearTimeout(entry.feedbackTimer);
  }
  entry.feedbackTimer = window.setTimeout(() => {
    entry.feedbackLabel = undefined;
    node?.graph?.setDirtyCanvas(true);
  }, duration);
  node?.graph?.setDirtyCanvas(true);
}

function computeButtonLayout(widgetWidth, count, height) {
  const buttonWidth = Math.max(MIN_BUTTON_WIDTH, (widgetWidth - BUTTON_ROW_MARGIN * 2 - BUTTON_ROW_GAP * (count - 1)) / count);
  const total = buttonWidth * count + BUTTON_ROW_GAP * (count - 1);
  const startX = Math.max(BUTTON_ROW_MARGIN, (widgetWidth - total) / 2);
  const rects = Array.from({ length: count }, (_, index) => ({
    x: startX + index * (buttonWidth + BUTTON_ROW_GAP),
    width: buttonWidth,
    height,
  }));
  return { rects, height };
}

function createActionWidget(node) {
  const buttons = ACTION_BUTTON_DEFS.map((def) => ({
    ...def,
    defaultLabel: def.label,
    feedbackLabel: undefined,
    disabled: false,
  }));
  const widget = node.addCustomWidget({
    name: "banana-actions",
    type: "banana-actions",
    buttons,
    draw(ctx, _, widgetWidth, y, _height) {
      // LiteGraph 仍可能把单行 NODE_WIDGET_HEIGHT 传给 draw；双行按钮必须
      // 使用自身声明的高度绘制，否则背景只有一行高而第二行文字会溢出。
      // LiteGraph 传入的 widgetWidth 在节点缩放后可能仍是旧值；以节点当前
      // 宽度为准，确保整组按钮始终在可见节点中居中。
      const rowWidth = Math.max(Number(widgetWidth) || 0, Number(node?.size?.[0]) || 0);
      const layout = computeButtonLayout(rowWidth, this.buttons.length, BUTTON_ROW_HEIGHT);
      this.__layout = layout;
      const previousFont = ctx.font;
      const previousAlign = ctx.textAlign;
      const previousBaseline = ctx.textBaseline;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = "12px sans-serif";
      this.buttons.forEach((button, index) => {
        const rect = layout.rects[index];
        const isDisabled = button.disabled;
        ctx.fillStyle = isDisabled ? "#444" : "#2b6cb0";
        ctx.strokeStyle = isDisabled ? "#555" : "#1a365d";
        ctx.beginPath();
        if (ctx.roundRect) {
          ctx.roundRect(rect.x, y, rect.width, rect.height, 6);
        } else {
          ctx.rect(rect.x, y, rect.width, rect.height);
        }
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = isDisabled ? "#979797" : "#f5f5f5";
        const lines = String(button.feedbackLabel || button.label).split("\n");
        const lineHeight = 14;
        const firstLineY = y + rect.height / 2 - ((lines.length - 1) * lineHeight) / 2;
        lines.forEach((line, lineIndex) => {
          ctx.fillText(line, rect.x + rect.width / 2, firstLineY + lineIndex * lineHeight);
        });
      });
      ctx.font = previousFont;
      ctx.textAlign = previousAlign;
      ctx.textBaseline = previousBaseline;
    },
    computeSize(widgetWidth) {
      return [widgetWidth, BUTTON_ROW_HEIGHT + 4];
    },
    mouse(event, position) {
      if (!event?.type || event.type !== POINTER_DOWN_EVENT) {
        return false;
      }
      const layout = this.__layout;
      if (!layout) {
        return false;
      }
      const [x, y] = position;
      if (y < this.last_y || y > this.last_y + layout.height) {
        return false;
      }
      for (let index = 0; index < layout.rects.length; index += 1) {
        const rect = layout.rects[index];
        if (x >= rect.x && x <= rect.x + rect.width) {
          const button = this.buttons[index];
          if (button?.disabled) {
            return false;
          }
          button?.onClick?.();
          return true;
        }
      }
      return false;
    },
  });
  return { widget, buttons };
}

async function copyWechatId(node) {
  const text = WECHAT_ID;
  try {
    if (navigator?.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.setAttribute("readonly", "true");
      textarea.style.position = "absolute";
      textarea.style.left = "-9999px";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
    }
    flashActionLabel(node, "wechat", "复制成功");
  } catch (error) {
    console.error(`[${EXTENSION}] 微信号复制失败`, error);
    flashActionLabel(node, "wechat", "复制失败", 2400);
  }
}

function ensureWidgets(node) {
  if (!node.__bananaBalanceWidgets) {
    const placeholder = node.addWidget(
      "text",
      "余额",
      "读取缓存...",
      () => { },
      { serialize: false }
    );
    const actionWidget = createActionWidget(node);
    const buttonMap = actionWidget.buttons.reduce((acc, button) => {
      acc[button.key] = button;
      return acc;
    }, {});
    const widgets = {
      status: placeholder,
      actionWidget: actionWidget.widget,
      actionButtons: actionWidget.buttons,
      actionButtonMap: buttonMap,
    };
    buttonMap.wechat.onClick = () => {
      window.open("https://task.xinbao-ai.com", "_blank", "noopener,noreferrer");
    };
    buttonMap.query.onClick = () => {
      void queryBalance(node);
    };
    buttonMap.qr.onClick = () => {
      showQrOverlay();
    };
    node.__bananaBalanceWidgets = widgets;
  }
  return node.__bananaBalanceWidgets;
}

function getApiKey(node) {
  const widget =
    node.widgets?.find((w) => w.name === "banana_api_key") ||
    node.widgets?.find((w) => w.name === "api_key");
  if (widget && typeof widget.value === "string" && widget.value.trim().length > 0) {
    return widget.value.trim();
  }
  return "";
}

function getBypassProxyFlag(node) {
  const widget = node.widgets?.find((w) => w.name === "绕过代理");
  if (!widget) {
    return null;
  }
  return typeof widget.value === "boolean" ? widget.value : null;
}

function getDisableSslFlag(node) {
  const widget = node.widgets?.find((w) => w.name === "禁用SSL验证");
  if (!widget) {
    return null;
  }
  return typeof widget.value === "boolean" ? widget.value : null;
}

function getRouteChoice(node) {
  const widget = node.widgets?.find((w) => w.name === "线路");
  if (!widget || typeof widget.value !== "string") {
    return "";
  }
  return widget.value.trim();
}

function formatSummary(data) {
  if (!data) {
    return "未返回余额信息";
  }
  const formatter = new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 });
  const available = data.total_available != null ? formatter.format(data.total_available) : "-";
  const granted = data.total_granted != null ? formatter.format(data.total_granted) : "-";
  const used = data.total_used != null ? formatter.format(data.total_used) : "-";
  const unlimited = data.unlimited_quota ? "是" : "否";
  const expires = data.expires_at > 0 ? new Date(data.expires_at * 1000).toLocaleString() : "不过期";
  return `可用/总量: ${available}/${granted}\n已用: ${used}, 无限额度: ${unlimited}\n到期: ${expires}`;
}

async function requestBalance(node, refresh) {
  const apiKey = getApiKey(node);
  let url = `/banana/token_usage?refresh=${refresh ? 1 : 0}`;
  if (apiKey) {
    const encoded = encodeURIComponent(apiKey);
    url += `&banana_api_key=${encoded}`;
  }
  const bypassProxy = getBypassProxyFlag(node);
  if (bypassProxy !== null) {
    url += `&bypass_proxy=${bypassProxy ? 1 : 0}`;
  }
  const disableSsl = getDisableSslFlag(node);
  if (disableSsl !== null) {
    url += `&disable_ssl_verify=${disableSsl ? 1 : 0}`;
  }
  const routeChoice = getRouteChoice(node);
  if (routeChoice) {
    url += `&route=${encodeURIComponent(routeChoice)}`;
  }
  const response = await api.fetchApi(url, { method: "GET" });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload?.success) {
    const message = payload?.message || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

async function loadCachedBalance(node) {
  const widgets = ensureWidgets(node);
  widgets.status.value = "读取缓存...";
  node.graph?.setDirtyCanvas(true);
  try {
    const payload = await requestBalance(node, false);
    widgets.status.value = payload.summary || formatSummary(payload.data);
  } catch (error) {
    widgets.status.value = error?.message || "暂无缓存，点击“查询余额”刷新";
  } finally {
    node.graph?.setDirtyCanvas(true);
  }
}

async function queryBalance(node) {
  const widgets = ensureWidgets(node);
  setButtonDisabled(node, "query", true);
  widgets.status.value = "查询中...";
  node.graph?.setDirtyCanvas(true);
  try {
    const payload = await requestBalance(node, true);
    widgets.status.value = payload.summary || formatSummary(payload.data);
  } catch (error) {
    console.error(`[${EXTENSION}] 查询余额失败`, error);
    widgets.status.value = `❌ ${error?.message || error}`;
  } finally {
    setButtonDisabled(node, "query", false);
    node.graph?.setDirtyCanvas(true);
  }
}

function enhanceNode(node) {
  if (node.__bananaBalanceReady) {
    return;
  }
  ensureWidgets(node);
  void loadCachedBalance(node);
  node.__bananaBalanceReady = true;
}

app.registerExtension({
  name: EXTENSION,
  nodeCreated(node) {
    if (TARGET_NODES.has(node.comfyClass)) {
      enhanceNode(node);
    }
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name && TARGET_NODES.has(nodeData.name)) {
      const original = nodeType.prototype.onNodeCreated;
      nodeType.prototype.onNodeCreated = function () {
        const result = original?.apply(this, arguments);
        enhanceNode(this);
        return result;
      };
    }
  },
});
