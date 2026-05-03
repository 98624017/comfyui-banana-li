import { app } from "/scripts/app.js";

const EXTENSION = "banana.bananaV3GptImageParams";
const TARGET_NODE = "BananaImageNodeV3";
const GPT_MODELS = new Set(["gpt-image-2", "gpt-image-2-oai"]);
const BANANA_V3_MIN_NODE_WIDTH = 336;
const GPT_IMAGE_NOTICE = "gpt-image-2 非 OAI 版为逆向渠道，quality 参数无效，大于 2K 像素可能非原生。推荐使用 gpt-image-2-oai 满参数版。";
const NOTICE_WIDGET_NAME = "banana-gpt-image-notice";
const NOTICE_LINE_HEIGHT = 18;
const NOTICE_PADDING_X = 10;
const NOTICE_PADDING_Y = 4;
const NOTICE_MAX_LINES = 2;

const GEMINI_WIDGETS = ["aspect_ratio", "image_size", "top_p", "联网搜索"];
const GPT_WIDGETS = ["size", "custom_width", "custom_height", "quality"];
const GPT_WIDGET_DEFAULTS = {
  size: "auto",
  custom_width: 0,
  custom_height: 0,
  quality: "medium",
};

const MIN_PIXELS = 655360;
const MAX_PIXELS = 8294400;
const MAX_EDGE = 3840;
const MAX_RATIO = 3;

function findWidget(node, name) {
  return node.widgets?.find((w) => w?.name === name);
}

function wrapText(ctx, text, maxWidth) {
  const lines = [];
  let line = "";
  for (const char of text) {
    const next = line + char;
    if (line && ctx.measureText(next).width > maxWidth) {
      lines.push(line);
      line = char;
    } else {
      line = next;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function getNoticeLines(ctx, widgetWidth) {
  const maxWidth = Math.max(120, widgetWidth - NOTICE_PADDING_X * 2);
  return wrapText(ctx, GPT_IMAGE_NOTICE, maxWidth);
}

function truncateToWidth(ctx, text, maxWidth) {
  if (ctx.measureText(text).width <= maxWidth) return text;
  let value = text;
  while (value.length > 0 && ctx.measureText(`${value}...`).width > maxWidth) {
    value = value.slice(0, -1);
  }
  return `${value}...`;
}

function createNoticeWidget(node) {
  const widgetDef = {
    name: NOTICE_WIDGET_NAME,
    type: NOTICE_WIDGET_NAME,
    hidden: true,
    draw(ctx, _, widgetWidth, y, height) {
      if (this.hidden) return;
      const previousFont = ctx.font;
      const previousFill = ctx.fillStyle;

      ctx.font = "12px sans-serif";
      ctx.fillStyle = "#cfcfcf";
      const maxWidth = Math.max(120, widgetWidth - NOTICE_PADDING_X * 2);
      const maxLines = Math.min(
        NOTICE_MAX_LINES,
        Math.max(1, Math.floor((height - NOTICE_PADDING_Y * 2) / NOTICE_LINE_HEIGHT))
      );
      const lines = getNoticeLines(ctx, widgetWidth).slice(0, maxLines);
      if (lines.length === maxLines) {
        const fullLines = getNoticeLines(ctx, widgetWidth);
        if (fullLines.length > maxLines) {
          lines[maxLines - 1] = truncateToWidth(ctx, lines[maxLines - 1], maxWidth);
        }
      }
      lines.forEach((line, index) => {
        ctx.fillText(line, NOTICE_PADDING_X, y + NOTICE_PADDING_Y + 13 + index * NOTICE_LINE_HEIGHT);
      });

      ctx.font = previousFont;
      ctx.fillStyle = previousFill;
    },
    computeSize(widgetWidth) {
      if (this.hidden) return [widgetWidth, -4];
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      if (!ctx) return [widgetWidth, NOTICE_MAX_LINES * NOTICE_LINE_HEIGHT + NOTICE_PADDING_Y * 2];
      ctx.font = "12px sans-serif";
      const lineCount = Math.min(NOTICE_MAX_LINES, getNoticeLines(ctx, widgetWidth).length);
      return [widgetWidth, lineCount * NOTICE_LINE_HEIGHT + NOTICE_PADDING_Y * 2];
    },
  };
  const widget = node.addCustomWidget(widgetDef);
  if (widget && typeof widgetDef.computeSize === "function") {
    widget.computeSize = widgetDef.computeSize;
  }
  return widget;
}

function moveWidgetToEnd(node, widget) {
  if (!node.widgets || !widget) return;
  const index = node.widgets.indexOf(widget);
  if (index < 0 || index === node.widgets.length - 1) return;
  node.widgets.splice(index, 1);
  node.widgets.push(widget);
}

function ensureNoticeWidget(node) {
  let widget = findWidget(node, NOTICE_WIDGET_NAME);
  if (!widget) {
    widget = createNoticeWidget(node);
  }
  moveWidgetToEnd(node, widget);
  return widget;
}

function hideWidget(node, widget) {
  if (!widget || widget.type === "converted-widget") return;
  widget.origType = widget.type;
  widget.origComputeSize = widget.computeSize;
  widget.computeSize = () => [0, -4];
  widget.type = "converted-widget";
  widget.hidden = true;
}

function showWidget(widget) {
  if (!widget || !widget.origType) return;
  widget.type = widget.origType;
  widget.computeSize = widget.origComputeSize;
  delete widget.origType;
  delete widget.origComputeSize;
  widget.hidden = false;
}

function roundToStep(value) {
  const n = Number(value) || 0;
  if (n <= 0) return 0;
  return Math.max(16, Math.min(MAX_EDGE, Math.round(n / 16) * 16));
}

function clampPair(width, height) {
  let w = roundToStep(width);
  let h = roundToStep(height);
  if (w <= 0 || h <= 0) return [w, h];

  if (Math.max(w, h) / Math.min(w, h) > MAX_RATIO) {
    if (w > h) h = roundToStep(Math.ceil(w / MAX_RATIO));
    else w = roundToStep(Math.ceil(h / MAX_RATIO));
  }

  while (w > 0 && h > 0 && w * h > MAX_PIXELS) {
    if (w >= h) w = roundToStep(w - 16);
    else h = roundToStep(h - 16);
  }

  while (w > 0 && h > 0 && w * h < MIN_PIXELS) {
    if (w <= h) w = roundToStep(w + 16);
    else h = roundToStep(h + 16);
    if (w >= MAX_EDGE && h >= MAX_EDGE) break;
  }

  return [w, h];
}

function setWidgetValue(widget, value) {
  if (!widget || widget.value === value) return;
  widget.value = value;
  if (widget.inputEl) widget.inputEl.value = value;
  if (widget.element) widget.element.value = value;
  if (widget.domEl) widget.domEl.value = value;
}

function normalizeGptWidgetDefaults(node) {
  for (const [name, defaultValue] of Object.entries(GPT_WIDGET_DEFAULTS)) {
    const widget = findWidget(node, name);
    if (!widget) continue;
    if (widget.value === null || widget.value === undefined || widget.value === "") {
      setWidgetValue(widget, defaultValue);
    }
  }
}

function clampCustomSize(node) {
  const widthWidget = findWidget(node, "custom_width");
  const heightWidget = findWidget(node, "custom_height");
  if (!widthWidget || !heightWidget) return;
  const [w, h] = clampPair(widthWidget.value, heightWidget.value);
  setWidgetValue(widthWidget, w);
  setWidgetValue(heightWidget, h);
}

function getNodeSize(node) {
  const computed = node.computeSize?.();
  const width = Math.max(
    BANANA_V3_MIN_NODE_WIDTH,
    Number(computed?.[0]) || 0,
    Number(node.size?.[0]) || 0
  );
  const height = Number(computed?.[1]) || Number(node.size?.[1]) || 0;
  return [width, height];
}

function updateWidgets(node) {
  const modelWidget = findWidget(node, "model_type");
  const isGpt = GPT_MODELS.has(String(modelWidget?.value || ""));
  const noticeWidget = ensureNoticeWidget(node);
  normalizeGptWidgetDefaults(node);

  for (const name of GEMINI_WIDGETS) {
    const widget = findWidget(node, name);
    if (!widget) continue;
    if (isGpt) hideWidget(node, widget);
    else showWidget(widget);
  }

  for (const name of GPT_WIDGETS) {
    const widget = findWidget(node, name);
    if (!widget) continue;
    if (isGpt) showWidget(widget);
    else hideWidget(node, widget);
  }

  if (isGpt) clampCustomSize(node);
  noticeWidget.hidden = !isGpt;
  moveWidgetToEnd(node, noticeWidget);
  // V3 追加了余额操作按钮，默认宽度需至少完整容纳按钮行。
  node.setSize?.(getNodeSize(node));
  node.graph?.setDirtyCanvas?.(true, true);
}

function wrapWidgetCallback(node, widgetName, callback) {
  const widget = findWidget(node, widgetName);
  if (!widget || widget.__bananaV3GptWrapped) return;
  const original = widget.callback;
  widget.callback = function () {
    const result = original?.apply(this, arguments);
    callback(node);
    return result;
  };
  widget.__bananaV3GptWrapped = true;
}

function enhance(node) {
  wrapWidgetCallback(node, "model_type", updateWidgets);
  wrapWidgetCallback(node, "custom_width", clampCustomSize);
  wrapWidgetCallback(node, "custom_height", clampCustomSize);
  updateWidgets(node);
  setTimeout(() => updateWidgets(node), 0);
}

app.registerExtension({
  name: EXTENSION,
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onCreated?.apply(this, arguments);
      enhance(this);
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure?.apply(this, arguments);
      enhance(this);
      return result;
    };
  },
});
