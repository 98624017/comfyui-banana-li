import { app } from "/scripts/app.js";

const IMAGE_MODEL_LABELS = Object.freeze({
  "gemini-3-pro-image-preview-vip": "全能图片Pro Vip",
  "gemini-3-pro-image-preview-svip": "全能图片Pro Svip",
  "gemini-3.1-flash-lite-image": "全能图片2 lite",
  "gemini-3-pro-image-preview": "全能图片Pro",
  "gemini-3.1-flash-image-preview": "全能图片2",
  "gemini-nano-banana-2.1": "全能图片2.1",
  "gpt-image-2.5-flare": "全能图片G2.5",
  "gpt-image-2": "全能图片G2.5",
  "gpt-image-2.5": "全能图片G2.5",
  "gpt-image-2-oai": "全能图片G2 OAI",
  "gpt-image-2.5-flare-oai": "全能图片G2.5 F OAI",
  "gpt-image-2.5-sunburst-oai": "全能图片G2.5 S OAI",
  "doubao-seedream-5-0": "Seedream 5.0",
  "seedream-5-pro": "Seedream 5 Pro",
  "gemini-3-pro-image-preview-vip(高成本)": "全能图片Pro Vip",
  vip: "全能图片Pro Vip",
  svip: "全能图片Pro Svip",
});

const VIDEO_MODEL_LABELS = Object.freeze({
  "seedance-2.0": "全能视频S2",
  "seedance-2.0-fast": "全能视频S2 Fast",
  "seedance-2.5": "全能视频S2.5",
  "ty25-seedance-2.5-480p": "SD2.5pro特价",
  "ty25-sd25proc-720p": "SD2.5pro特价(次)",
  "ty-sd2proc": "SD2.0pro特价(次)",
  "ty-hailuo-video-minimax-h3": "MiniMax H3(次)",
  "tyh3-minimaxh3v-768p": "MiniMax H3V Pro(次)",
  "tyw3-wan3.0-1080p": "Wan3.0",
  omni: "全能模型Oflash(次)",
  "ty25-sd25proc-480p": "SD2.5pro特价(次)",
  "ty-seedance-2.5-480p": "SD2.5pro特价",
  "ty-minimaxh3v": "MiniMax H3V Pro(次)",
  "tyh3-minimaxh3v-2k": "MiniMax H3V Pro(次)",
});

const LABELS_BY_NODE = Object.freeze({
  BananaImageNode: IMAGE_MODEL_LABELS,
  BananaImageNodeV2: IMAGE_MODEL_LABELS,
  BananaImageNodeV3: IMAGE_MODEL_LABELS,
  XinbaoUnifiedVideoGenerator: VIDEO_MODEL_LABELS,
});

function applyModelLabels(node, labels) {
  const widget = node?.widgets?.find((item) => item?.name === "model_type");
  if (!widget) return;

  widget.options = widget.options || {};
  if (widget.options.__bananaModelLabels === labels) return;
  widget.options.getOptionLabel = (value) => labels[String(value)] || String(value ?? "");
  widget.options.__bananaModelLabels = labels;
  node?.setDirtyCanvas?.(true, true);
}

function patchNodeType(nodeType, labels) {
  const onNodeCreated = nodeType.prototype.onNodeCreated;
  nodeType.prototype.onNodeCreated = function () {
    const result = onNodeCreated?.apply(this, arguments);
    applyModelLabels(this, labels);
    return result;
  };

  const onConfigure = nodeType.prototype.onConfigure;
  nodeType.prototype.onConfigure = function () {
    const result = onConfigure?.apply(this, arguments);
    applyModelLabels(this, labels);
    return result;
  };
}

app.registerExtension({
  name: "banana.modelDisplayLabels",

  beforeRegisterNodeDef(nodeType, nodeData) {
    const labels = LABELS_BY_NODE[nodeData?.name];
    if (labels) patchNodeType(nodeType, labels);
  },

  nodeCreated(node) {
    const labels = LABELS_BY_NODE[node?.comfyClass];
    if (labels) applyModelLabels(node, labels);
  },
});
