import { app } from "/scripts/app.js";
import { canonicalModel, findWidget, isLinked, modelEntry, readModelMetadata, setWidgetValue, updateCombo } from "./model_widgets.js";

const EXTENSION = "banana.bananaV3GptImageParams";
const TARGET_NODE = "BananaImageNodeV3";
const BANANA_V3_MIN_NODE_WIDTH = 336;
const GPT_IMAGE_NOTICE = "全能图片 G2.6 使用固定质量。可切换到 G2.6 S OAI 选择质量档位。";
const NOTICE_WIDGET_NAME = "banana-gpt-image-notice";
const NOTICE_LINE_HEIGHT = 18;
const NOTICE_PADDING_X = 10;
const NOTICE_PADDING_Y = 4;
const NOTICE_MAX_LINES = 2;

const GEMINI_WIDGETS = ["top_p", "联网搜索"];
const GPT_WIDGETS = ["quality"];
const PARAMETER_WIDGET_ORDER = [
  // 各模型的主尺寸选项固定在 batch_size 后的同一个位置。
  "aspect_ratio",
  "image_size",
  "size",
  "custom_width",
  "custom_height",
  "quality",
  "seed",
  "生成后控制",
  "top_p",
  "线路",
  "联网搜索",
  "启用工作流并发",
  "大于5M限制长边",
  "仅提交不等待",
];
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

function selectedCapability(node, metadata) {
  if (isLinked(node, "model_type")) return;
  const model = canonicalModel(metadata, findWidget(node, "model_type")?.value);
  return modelEntry(metadata?.image, model);
}

function restoreModelSelection(node, metadata) {
  const legacy = selectedCapability(node, metadata);
  const quality = findWidget(node, "quality");
  if (!legacy?.selection_model || !metadata.values.includes(legacy.selection_model)
      || !quality || isLinked(node, "quality")) return;
  // 先恢复质量再折叠隐藏型号；外接质量只能由执行节点确定，需保留原路由。
  const value = String(quality.value ?? "").trim().toLowerCase();
  setWidgetValue(quality, legacy.quality_options.includes(value) ? value : legacy.quality_options[0]);
  setWidgetValue(findWidget(node, "model_type"), legacy.selection_model);
}

function normalizeParameterWidgetOrder(node) {
  if (!Array.isArray(node.widgets)) return;
  const orderedNames = new Set(PARAMETER_WIDGET_ORDER);
  const widgetsByName = new Map(
    node.widgets
      .filter((widget) => orderedNames.has(widget?.name))
      .map((widget) => [widget.name, widget])
  );
  if (widgetsByName.size === 0) return;

  // 只移动参数区控件，保留提示词、图片输入、余额及操作按钮的既有位置。
  const remainingWidgets = node.widgets.filter((widget) => !orderedNames.has(widget?.name));
  node.widgets.splice(0, node.widgets.length, ...remainingWidgets);
  const batchIndex = node.widgets.findIndex((widget) => widget?.name === "batch_size");
  const insertIndex = batchIndex >= 0 ? batchIndex + 1 : node.widgets.length;
  const orderedWidgets = PARAMETER_WIDGET_ORDER
    .map((name) => widgetsByName.get(name))
    .filter(Boolean);
  node.widgets.splice(insertIndex, 0, ...orderedWidgets);
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

function normalizeGptWidgetDefaults(node) {
  for (const [name, defaultValue] of Object.entries(GPT_WIDGET_DEFAULTS)) {
    const widget = findWidget(node, name);
    if (!widget || isLinked(node, name)) continue;
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

function updateWidgets(node, metadata = {}) {
  restoreModelSelection(node, metadata);
  const capabilities = selectedCapability(node, metadata);
  const dynamicModel = isLinked(node, "model_type");
  const isGpt = capabilities?.kind === "gpt";
  const isSeedream = capabilities?.kind === "seedream";
  const supportsQuality = dynamicModel || capabilities?.quality_options.length > 0;
  const noticeWidget = ensureNoticeWidget(node);
  normalizeGptWidgetDefaults(node);

  // Keep the same ratio/resolution widgets and sockets for every image model.
  for (const [name, label] of [["aspect_ratio", "比例"], ["image_size", "分辨率"]]) {
    const widget = findWidget(node, name);
    if (!widget) continue;
    widget.label = label;
    if (!isLinked(node, name)) showWidget(widget);
  }
  const size = findWidget(node, "size");
  if (isGpt && size && !["size", "aspect_ratio", "image_size"].some(name => isLinked(node, name))) {
    const raw = String(size.value || "auto");
    const preset = raw.match(/^([124]K)\s+(\d+:\d+)\s+\d+x\d+$/i)
      || String(size.options?.values?.find(value => String(value).endsWith(" " + raw)) || "").match(/^([124]K)\s+(\d+:\d+)\s+\d+x\d+$/i);
    if (preset) {
      setWidgetValue(findWidget(node, "aspect_ratio"), preset[2]);
      setWidgetValue(findWidget(node, "image_size"), preset[1].toUpperCase());
      setWidgetValue(size, "auto");
    }
  }
  hideWidget(node, size);
  if (capabilities) {
    updateCombo(node, "aspect_ratio", capabilities.aspect_ratios.map(value => value === "auto" ? "Auto" : value), "1:1");
    updateCombo(node, "image_size", capabilities.image_sizes.filter(value => value !== "auto"), "2K");
    updateCombo(node, "quality", capabilities.quality_options, "medium");
  } else if (dynamicModel) {
    for (const name of ["aspect_ratio", "image_size", "quality"]) {
      updateCombo(node, name, metadata.inputs?.[name]?.[0]);
    }
  }
  // Retain editable legacy custom dimensions only for workflows already using them.
  const hasCustom = ["custom_width", "custom_height"].some(name => Number(findWidget(node, name)?.value) > 0);
  for (const name of ["custom_width", "custom_height"]) {
    const widget = findWidget(node, name);
    if ((isGpt || dynamicModel) && hasCustom) showWidget(widget);
    else hideWidget(node, widget);
  }

  for (const name of GEMINI_WIDGETS) {
    const widget = findWidget(node, name);
    if (!widget) continue;
    if (isGpt || isSeedream) hideWidget(node, widget);
    else showWidget(widget);
  }

  for (const name of GPT_WIDGETS) {
    const widget = findWidget(node, name);
    if (!widget) continue;
    if (isLinked(node, name)) continue;
    if (supportsQuality) showWidget(widget);
    else hideWidget(node, widget);
  }

  if (isGpt) clampCustomSize(node);
  normalizeParameterWidgetOrder(node);
  noticeWidget.hidden = !isGpt || supportsQuality;
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

function enhance(node, metadata) {
  wrapWidgetCallback(node, "model_type", node => updateWidgets(node, metadata));
  for (const name of ["aspect_ratio", "image_size"]) {
    wrapWidgetCallback(node, name, (node) => {
      if (selectedCapability(node, metadata)?.kind === "gpt") {
        for (const [key, value] of [["size", "auto"], ["custom_width", 0], ["custom_height", 0]]) {
          const widget = findWidget(node, key);
          if (widget) setWidgetValue(widget, value);
        }
      }
      updateWidgets(node, metadata);
    });
  }
  wrapWidgetCallback(node, "custom_width", clampCustomSize);
  wrapWidgetCallback(node, "custom_height", clampCustomSize);
  updateWidgets(node, metadata);
  setTimeout(() => updateWidgets(node, metadata), 0);
}

app.registerExtension({
  name: EXTENSION,
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) return;
    const metadata = readModelMetadata(nodeData);

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onCreated?.apply(this, arguments);
      this.__bananaV3InitialWidgetOrder = this.widgets?.map((widget) => widget.name) || [];
      enhance(this, metadata);
      return result;
    };

    // widgets_values 按位置保存：加载旧工作流先恢复旧顺序，再做 UI 重排。
    const configure = nodeType.prototype.configure;
    nodeType.prototype.configure = function (data) {
      const savedOrder = data?.properties?.bananaV4WidgetOrder;
      const order = Array.isArray(savedOrder) ? savedOrder : this.__bananaV3InitialWidgetOrder;
      if (Array.isArray(this.widgets) && Array.isArray(order)) {
        const positions = new Map(order.map((name, index) => [name, index]));
        this.widgets.sort((a, b) =>
          (positions.get(a.name) ?? Infinity) - (positions.get(b.name) ?? Infinity));
      }
      return configure?.apply(this, arguments);
    };

    const onConnectionsChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function (type, index) {
      const result = onConnectionsChange?.apply(this, arguments);
      if (type === 1 && ["model_type", "quality"].includes(this.inputs?.[index]?.name)) {
        // 加载工作流时槽位先于 widgets_values 恢复，待本次配置结束再更新控件。
        queueMicrotask(() => updateWidgets(this, metadata));
      }
      return result;
    };

    const onSerialize = nodeType.prototype.onSerialize;
    nodeType.prototype.onSerialize = function (data) {
      const result = onSerialize?.apply(this, arguments);
      data.properties = {
        ...data.properties,
        bananaV4WidgetOrder: this.widgets?.map((widget) => widget.name) || [],
        bananaV4SplitSize: true,
      };
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (data) {
      const result = onConfigure?.apply(this, arguments);
      // In old GPT workflows the hidden ratio had no effect when size was auto.
      if (!data?.properties?.bananaV4SplitSize
          && selectedCapability(this, metadata)?.kind === "gpt"
          && String(findWidget(this, "size")?.value || "auto").toLowerCase() === "auto"
          && !isLinked(this, "aspect_ratio")) {
        setWidgetValue(findWidget(this, "aspect_ratio"), "Auto");
      }
      enhance(this, metadata);
      return result;
    };
  },
});
