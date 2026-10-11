import { app } from "/scripts/app.js";

const EXTENSION = "banana.modelscopeChannelSwitch";
const TARGET_NODE = "XinbaoModelScopeCaption";
const CHANNEL_XINBAO = "心宝❤新渠道";
const CHANNEL_MODAO = "魔搭社区";
const XINBAO_DEFAULT_MODEL = "GPT-6 Luna";
const XINBAO_LEGACY_MODEL_LABELS = {
  "gemini-3-pro-preview-c": "GPT-6 Sol",
  "gemini-3-pro-preview-c（较耗时）": "GPT-6 Sol",
  "gemini-3-flash-c": "GPT-6 Luna",
};

const RANKING_URL = "https://lmarena.ai/leaderboard/vision";


// Store lists globally for this session
let GLOBAL_XINBAO_MODELS = [];
let GLOBAL_MODAO_MODELS = [];

function findWidget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

function normalizeChannel(value) {
  return value === CHANNEL_MODAO ? CHANNEL_MODAO : CHANNEL_XINBAO;
}

function migrateUnsupportedChannelValue(node, channelWidget) {
  if (!channelWidget) return;
  if ([CHANNEL_XINBAO, CHANNEL_MODAO].includes(channelWidget.value)) return;
  channelWidget.value = CHANNEL_XINBAO;
  node?.graph?.setDirtyCanvas(true, true);
}

function updateModelOptions(node) {
  const channelWidget = findWidget(node, "channel");
  const modelWidget = findWidget(node, "model");
  if (!channelWidget || !modelWidget) {
    return;
  }

  migrateUnsupportedChannelValue(node, channelWidget);

  // Use captured global lists first, fallback to widget options (if any), then empty
  const xinbaoModels = GLOBAL_XINBAO_MODELS.length > 0 ? GLOBAL_XINBAO_MODELS : (modelWidget.options?.xinbao_models || []);
  const modaoModels = GLOBAL_MODAO_MODELS.length > 0 ? GLOBAL_MODAO_MODELS : (modelWidget.options?.modao_models || []);

  if (xinbaoModels.length === 0 && modaoModels.length === 0) {
    // If running in an environment where beforeRegisterNodeDef didn't fire or data missing
    console.warn("Xinbao: model lists not found. Ensure XinbaoModelScopeCaption defines xinbao_models/modao_models.");
    // We do NOT return here blindly anymore. If we return, we leave the mixed list.
    // But if we have no data, we can't do anything. 
    // We can try to see if modelWidget has values and guess? No, unsafe.
    return;
  }

  const channel = normalizeChannel(channelWidget.value);
  const values = channel === CHANNEL_MODAO ? modaoModels : xinbaoModels;

  modelWidget.options = modelWidget.options || {};
  modelWidget.options.values = values.slice();
  if (node.inputs?.some((input) => input.name === "model" && input.link != null)) {
    return;
  }

  if (channel === CHANNEL_XINBAO && XINBAO_LEGACY_MODEL_LABELS[modelWidget.value]) {
    modelWidget.value = XINBAO_LEGACY_MODEL_LABELS[modelWidget.value];
  }

  // Handle current value validation
  if (!values.includes(modelWidget.value)) {
    const preferred = channel === CHANNEL_XINBAO ? XINBAO_DEFAULT_MODEL : values[0];
    // If preferred is valid, use it. Else use first available. Keep current if somehow valid (already checked via includes, so not valid)
    modelWidget.value = values.includes(preferred) ? preferred : (values[0] || modelWidget.value);
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

    // Capture lists from nodeData.input.required (or optional) -> model
    // Format is usually: { input: { required: { model: [ "Type", { ...options } ] } } }
    if (nodeData.input?.required?.model?.[1]) {
      const options = nodeData.input.required.model[1];
      if (options.xinbao_models && Array.isArray(options.xinbao_models)) {
        GLOBAL_XINBAO_MODELS = options.xinbao_models;
      }
      if (options.modao_models && Array.isArray(options.modao_models)) {
        GLOBAL_MODAO_MODELS = options.modao_models;
      }
    } else if (nodeData.input?.optional?.model?.[1]) { // Check optional just in case
      const options = nodeData.input.optional.model[1];
      if (options.xinbao_models && Array.isArray(options.xinbao_models)) {
        GLOBAL_XINBAO_MODELS = options.xinbao_models;
      }
      if (options.modao_models && Array.isArray(options.modao_models)) {
        GLOBAL_MODAO_MODELS = options.modao_models;
      }
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
    const onConnectionsChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function () {
      const result = onConnectionsChange?.apply(this, arguments);
      updateModelOptions(this);
      return result;
    };
  },
});
