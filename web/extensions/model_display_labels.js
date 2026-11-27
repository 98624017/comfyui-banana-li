import { app } from "/scripts/app.js";
import { canonicalModel, findWidget, isLinked, modelEntry, readModelMetadata } from "./model_widgets.js";

const FIELD_LABELS_BY_NODE = Object.freeze({
  BananaImageNode: { aspect_ratio: "比例", image_size: "分辨率" },
  BananaImageNodeV2: { aspect_ratio: "比例", image_size: "分辨率" },
  BananaImageNodeV3: { aspect_ratio: "比例", image_size: "分辨率" },
  XinbaoUnifiedVideoGenerator: {
    aspect_ratio: "比例",
    resolution: "分辨率",
    banana_route: "线路",
  },
  XinbaoModelScopeCaption: { channel: "线路" },
});

function applyModelLabels(node, metadata) {
  const labels = metadata.labels;
  if (!labels) return;
  const widget = findWidget(node, "model_type");
  if (!widget) return;

  if (!isLinked(node, "model_type")) {
    const id = canonicalModel(metadata, widget.value);
    if (id !== widget.value && metadata.values.includes(id)) widget.value = id;
  }

  widget.options = widget.options || {};
  if (widget.options.__bananaModelLabels === labels) return;
  widget.options.getOptionLabel = (value) => {
    const id = canonicalModel(metadata, value);
    return modelEntry(labels, id) ?? String(value ?? "");
  };
  widget.options.__bananaModelLabels = labels;
  node?.setDirtyCanvas?.(true, true);
}

function applyFieldLabels(node, labels) {
  let changed = false;
  for (const [name, label] of Object.entries(labels || {})) {
    const widget = findWidget(node, name);
    if (widget && widget.label !== label) {
      widget.label = label;
      changed = true;
    }
  }
  if (changed) node?.setDirtyCanvas?.(true, true);
}

function patchNodeType(nodeType, metadata, fieldLabels) {
  const onNodeCreated = nodeType.prototype.onNodeCreated;
  nodeType.prototype.onNodeCreated = function () {
    const result = onNodeCreated?.apply(this, arguments);
    applyModelLabels(this, metadata);
    applyFieldLabels(this, fieldLabels);
    return result;
  };

  const onConfigure = nodeType.prototype.onConfigure;
  nodeType.prototype.onConfigure = function () {
    const result = onConfigure?.apply(this, arguments);
    applyModelLabels(this, metadata);
    applyFieldLabels(this, fieldLabels);
    return result;
  };
}

app.registerExtension({
  name: "banana.modelDisplayLabels",

  beforeRegisterNodeDef(nodeType, nodeData) {
    const metadata = readModelMetadata(nodeData);
    const fieldLabels = FIELD_LABELS_BY_NODE[nodeData?.name];
    if (metadata.labels || fieldLabels) patchNodeType(nodeType, metadata, fieldLabels);
  },

  nodeCreated(node) {
    applyFieldLabels(node, FIELD_LABELS_BY_NODE[node?.comfyClass]);
  },
});
