import { app } from "/scripts/app.js";

const EXTENSION = "banana.comfyApiAppSwitch";
const TARGET_NODE = "XinbaoComfyApiApp";

// 全局应用注册表 (从 Python nodeData 捕获)
let GLOBAL_APP_REGISTRY = {};

// 所有泛型 widget 插槽名
const WIDGET_SLOTS = [
  "text_1", "text_2",
  "float_1", "float_2", "float_3", "float_4",
  "bool_1", "bool_2",
  "combo_1", "combo_2",
];

// 所有 IMAGE input 插槽名
const IMAGE_SLOTS = ["image_1", "image_2"];

/**
 * 隐藏 widget (battle-tested 模式, 来源: comfyui_layerstyle/dz_comfy_shared.js)
 */
function hideWidget(node, widget) {
  if (!widget || widget.type === "converted-widget") return;
  widget.origType = widget.type;
  widget.origComputeSize = widget.computeSize;
  widget.computeSize = () => [0, -4];
  widget.type = "converted-widget";
  widget.hidden = true;
}

/**
 * 显示 widget
 */
function showWidget(widget) {
  if (!widget || !widget.origType) return;
  widget.type = widget.origType;
  widget.computeSize = widget.origComputeSize;
  delete widget.origType;
  delete widget.origComputeSize;
  widget.hidden = false;
}

/**
 * 查找节点上的 widget
 */
function findWidget(node, name) {
  return node.widgets?.find((w) => w.name === name);
}

/**
 * 核心：根据当前 app_select 更新所有插槽的可见性与属性
 */
function updateAppWidgets(node) {
  const appWidget = findWidget(node, "app_select");
  if (!appWidget) return;

  const appName = appWidget.value;
  const appConfig = GLOBAL_APP_REGISTRY[appName];
  if (!appConfig) return;

  // 收集当前应用使用的插槽: { slot_name: field_config }
  const activeSlots = {};
  for (const field of appConfig.fields || []) {
    activeSlots[field.slot] = field;
  }

  // 1. 处理 WIDGET 类型插槽 (text, float, bool, combo)
  for (const slotName of WIDGET_SLOTS) {
    const widget = findWidget(node, slotName);
    if (!widget) continue;

    const fieldConfig = activeSlots[slotName];
    if (fieldConfig) {
      // 显示此 widget
      showWidget(widget);

      // 更新标签
      if (fieldConfig.label) {
        widget.label = fieldConfig.label;
      }

      // combo: 更新选项列表
      if (fieldConfig.type === "combo" && fieldConfig.options) {
        widget.options = widget.options || {};
        widget.options.values = fieldConfig.options.slice();
        // 如果当前值不在新选项中，重置为默认
        if (!fieldConfig.options.includes(widget.value)) {
          widget.value = fieldConfig.default || fieldConfig.options[0] || "";
        }
      }

      // float: 更新 min/max/step
      if (fieldConfig.type === "float") {
        widget.options = widget.options || {};
        if (fieldConfig.min !== undefined) widget.options.min = fieldConfig.min;
        if (fieldConfig.max !== undefined) widget.options.max = fieldConfig.max;
        if (fieldConfig.step !== undefined) widget.options.step = fieldConfig.step;
      }
    } else {
      // 隐藏此 widget
      hideWidget(node, widget);
    }
  }

  // 2. 处理 IMAGE input 插槽 (重命名标签)
  if (node.inputs) {
    for (const slotName of IMAGE_SLOTS) {
      const inputIdx = node.inputs.findIndex((inp) => inp.name === slotName);
      if (inputIdx === -1) continue;

      const fieldConfig = activeSlots[slotName];
      if (fieldConfig) {
        // 使用应用定义的标签
        node.inputs[inputIdx].label = fieldConfig.label || slotName;
      } else {
        // 标记为未使用
        node.inputs[inputIdx].label = `(未使用) ${slotName}`;
      }
    }
  }

  // 3. 重算节点尺寸
  node.setSize(node.computeSize());
  node.graph?.setDirtyCanvas(true, true);
}

app.registerExtension({
  name: EXTENSION,

  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) return;

    // 从 nodeData 捕获 app_registry
    const regSource =
      nodeData.input?.required?.app_select?.[1]?.app_registry ||
      nodeData.input?.optional?.app_select?.[1]?.app_registry;

    if (regSource && typeof regSource === "object") {
      GLOBAL_APP_REGISTRY = regSource;
    }

    // 拦截 onNodeCreated
    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onCreated?.apply(this, arguments);

      // 包装 app_select 的 callback
      const appWidget = findWidget(this, "app_select");
      if (appWidget && !appWidget.__bananaAppSwitchWrapped) {
        const originalCallback = appWidget.callback;
        const self = this;
        appWidget.callback = function () {
          const cbResult = originalCallback?.apply(this, arguments);
          updateAppWidgets(self);
          return cbResult;
        };
        appWidget.__bananaAppSwitchWrapped = true;
      }

      // 初始更新
      updateAppWidgets(this);
      return result;
    };

    // 拦截 onConfigure (工作流加载恢复)
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure?.apply(this, arguments);
      updateAppWidgets(this);
      return result;
    };
  },
});
