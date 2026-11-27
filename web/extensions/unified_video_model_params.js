import { app } from "/scripts/app.js";
import { canonicalModel, findWidget, isLinked, modelEntry, readModelMetadata, updateCombo } from "./model_widgets.js";

const TARGET_NODE = "XinbaoUnifiedVideoGenerator";

function updateControls(node, inputs, metadata, useModelDefaults = false) {
  const model = canonicalModel(metadata, findWidget(node, "model_type")?.value);
  const capability = !isLinked(node, "model_type") && modelEntry(metadata.video, model);

  updateCombo(node, "aspect_ratio", capability?.aspect_ratios ?? inputs.aspect_ratio[0], capability?.default_aspect_ratio);
  const resolution = findWidget(node, "resolution");
  if (capability && useModelDefaults && resolution && !isLinked(node, "resolution")) {
    resolution.value = capability.default_resolution;
  }
  const currentResolution = updateCombo(
    node, "resolution", capability?.resolutions ?? inputs.resolution[0], capability?.default_resolution,
  );

  const duration = findWidget(node, "duration");
  if (duration) {
    const min = capability?.min_duration_by_resolution?.[currentResolution] ?? capability?.min_duration ?? inputs.duration[1].min;
    const max = (isLinked(node, "参考视频") ? capability?.max_duration_with_reference_video : undefined)
      ?? capability?.max_duration ?? inputs.duration[1].max;
    duration.options ??= {};
    Object.assign(duration.options, { min, max });
    if (capability && !isLinked(node, "duration")
        && (useModelDefaults || !Number.isInteger(duration.value) || duration.value < min || duration.value > max)) {
      duration.value = Math.min(max, Math.max(min, capability.default_duration));
    }
  }
  node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
  name: "banana.unifiedVideoModelParams",

  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) return;
    const inputs = nodeData.input?.required;
    const metadata = readModelMetadata(nodeData);
    if (!metadata.video) return;
    const update = (node, useModelDefaults) => updateControls(node, inputs, metadata, useModelDefaults);

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onCreated?.apply(this, arguments);
      for (const name of ["model_type", "resolution"]) {
        const widget = findWidget(this, name);
        if (!widget) continue;
        const node = this;
        const callback = widget.callback;
        widget.callback = function () {
          const callbackResult = callback?.apply(this, arguments);
          update(node, name === "model_type");
          return callbackResult;
        };
      }

      // 隐藏已退役的参数，保留旧工作流中 widgets_values 的位置。
      const omniSeconds = findWidget(this, "omni_seconds");
      if (omniSeconds) {
        omniSeconds.hidden = true;
        omniSeconds.type = "converted-widget";
        omniSeconds.computeSize = () => [0, -4];
      }
      update(this);
      return result;
    };

    for (const hook of ["onConfigure", "onConnectionsChange"]) {
      const original = nodeType.prototype[hook];
      nodeType.prototype[hook] = function () {
        const result = original?.apply(this, arguments);
        update(this);
        return result;
      };
    }
  },
});
