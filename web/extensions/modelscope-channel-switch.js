import { app } from "/scripts/app.js";

const EXTENSION = "banana.modelscopeChannelSwitch";
const TARGET_NODE = "XinbaoModelScopeCaption";
const CHANNEL_BANANA = "香蕉同款渠道";
const CHANNEL_MODAO = "魔搭社区";
const BANANA_DEFAULT_MODEL = "gemini-2.5-flash-c";
const BANANA_MODELS = [
  "gemini-3-pro-preview-c（较耗时）",
  "gemini-2.5-pro-c（较耗时）",
  "gpt-4o-c",
  "gemini-2.5-flash-c",
];
const MODAO_MODELS = [
  "Qwen/Qwen3-VL-8B-Instruct",
  "Qwen/Qwen3-VL-235B-A22B-Instruct",
];
const RANKING_URL = "https://lmarena.ai/leaderboard/vision";

function findWidget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function normalizeChannel(value) {
  return value === CHANNEL_MODAO ? CHANNEL_MODAO : CHANNEL_BANANA;
}

function updateModelOptions(node) {
  const channelWidget = findWidget(node, "channel");
  const modelWidget = findWidget(node, "model");
  if (!channelWidget || !modelWidget) {
    return;
  }

  const channel = normalizeChannel(channelWidget.value);
  const values = channel === CHANNEL_MODAO ? MODAO_MODELS : BANANA_MODELS;

  modelWidget.options = modelWidget.options || {};
  modelWidget.options.values = values.slice();

  if (!values.includes(modelWidget.value)) {
    const preferred = channel === CHANNEL_BANANA ? BANANA_DEFAULT_MODEL : values[0];
    modelWidget.value = values.includes(preferred) ? preferred : values[0] || modelWidget.value; // 回退到当前频道的默认模型
  }

  node?.graph?.setDirtyCanvas(true, true);
}

function wrapChannelChange(node) {
  const channelWidget = findWidget(node, "channel");
  if (!channelWidget || channelWidget.__bananaModelscopeWrapped) {
    return;
  }
  const original = channelWidget.callback;
  channelWidget.callback = function () {
    const result = original?.apply(this, arguments);
    updateModelOptions(node);
    return result;
  };
  channelWidget.__bananaModelscopeWrapped = true;
}

function ensureRankingLink(node) {
  if (node.__bananaModelscopeRankingAdded) {
    return;
  }
  const widget = node.addWidget(
    "button",
    "视觉模型榜单",
    "打开",
    () => {
      window.open(RANKING_URL, "_blank", "noopener,noreferrer");
    },
    { serialize: false }
  );
  // 放在模型选择附近：尝试插入到模型 widget 前一位
  const modelWidget = findWidget(node, "model");
  if (modelWidget && Array.isArray(node.widgets)) {
    const idx = node.widgets.indexOf(widget);
    const modelIdx = node.widgets.indexOf(modelWidget);
    if (idx > -1 && modelIdx > -1 && idx > modelIdx) {
      node.widgets.splice(idx, 1);
      node.widgets.splice(modelIdx, 0, widget);
    }
  }
  node.__bananaModelscopeRankingAdded = true;
}

function enhance(node) {
  wrapChannelChange(node);
  updateModelOptions(node);
  ensureRankingLink(node);
}

app.registerExtension({
  name: EXTENSION,
  nodeCreated(node) {
    if (node.comfyClass === TARGET_NODE) {
      enhance(node);
    }
  },
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) {
      return;
    }
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
