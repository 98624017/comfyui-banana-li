import { app } from "/scripts/app.js";

const EXTENSION = "banana.globalKeyWorkflowGuard";
const API_TASKS = "/banana/video_tasks";
const XINBAO_KEY_FIELD = "banana_api_key";
const MODELSCOPE_KEY_FIELD = "modelscope_api_key";
const XINBAO_KEY_LABEL = "输入心宝key（推荐右下角任务中心设置）";
const MODELSCOPE_KEY_LABEL = "输入魔搭key（推荐右下角任务中心设置）";
const XINBAO_TARGET_CLASSES = new Set([
  "BananaImageNode",
  "BananaImageNodeV2",
  "BananaImageNodeV3",
  "XinbaoBatchDetailImageSaver",
  "XinbaoVideoGenerator",
  "XinbaoUnifiedVideoGenerator",
  "XinbaoDoubaoVideoGenerator",
  "XinbaoVeoVideoGenerator",
  "XinbaoComfyApiApp",
  "XinbaoModelScopeCaption",
]);
const MODELSCOPE_TARGET_CLASSES = new Set([
  "XinbaoModelScopeImageGenerate",
  "XinbaoModelScopeCaption",
]);
const KEY_CONFIGS = [
  { field: XINBAO_KEY_FIELD, label: XINBAO_KEY_LABEL, classes: XINBAO_TARGET_CLASSES },
  { field: MODELSCOPE_KEY_FIELD, label: MODELSCOPE_KEY_LABEL, classes: MODELSCOPE_TARGET_CLASSES },
];

let globalKeyConfigured = false;
let modelscopeKeyConfigured = false;

function findWidget(node, name) {
  if (!node) return null;
  if (Array.isArray(node.widgets)) {
    const widget = node.widgets.find((item) => item?.name === name);
    if (widget) return widget;
  }
  if (Array.isArray(node.inputs)) {
    const input = node.inputs.find((item) => item?.name === name);
    if (input?.widget) return input.widget;
  }
  return null;
}

function clearWidget(node, name) {
  const widget = findWidget(node, name);
  if (!widget || typeof widget.value !== "string" || !widget.value.trim()) {
    return false;
  }
  widget.value = "";
  for (const element of [widget.inputEl, widget.domEl, widget.element]) {
    if (element) element.value = "";
  }
  try {
    widget.callback?.("");
  } catch (error) {
    console.warn(`[${EXTENSION}] Key widget callback failed`, error);
  }
  node?.graph?.setDirtyCanvas(true, true);
  return true;
}

function nodeClassName(node) {
  return node?.comfyClass || node?.type || node?.constructor?.type || "";
}

function decorateKeyWidget(node) {
  const className = nodeClassName(node);
  for (const config of KEY_CONFIGS) {
    if (!config.classes.has(className)) continue;
    const widget = findWidget(node, config.field);
    if (!widget) continue;
    widget.label = config.label;
    widget.tooltip = config.label;
    widget.options = { ...(widget.options || {}), tooltip: config.label };
    for (const element of [widget.inputEl, widget.domEl, widget.element]) {
      if (!element) continue;
      element.placeholder = config.label;
      element.title = config.label;
    }
  }
}

function findNodesByClassName(className) {
  const root = app?.graph?.rootGraph || app?.graph;
  if (!root) return [];
  // ComfyUI 把所有层级的原生子图集中保存在根图的 Map 中。
  const subgraphs = root.subgraphs || root._subgraphs;
  const graphs = [root, ...(subgraphs?.values?.() || [])];
  const seen = new Set();
  const result = [];
  const add = (node) => {
    if (!node || seen.has(node)) return;
    seen.add(node);
    result.push(node);
  };
  for (const graph of graphs) {
    for (const node of graph.findNodesByClass?.(className) || []) add(node);
    for (const node of graph.findNodesByType?.(className) || []) add(node);
    for (const node of graph._nodes || graph.nodes || []) {
      if (nodeClassName(node) === className) add(node);
    }
  }
  return result;
}

function clearLinkedPrimitive(node, fieldName) {
  const graph = node?.graph || app?.graph;
  const visited = new Set();
  const visitInput = (input) => {
    if (input?.link == null || !graph?.links) return;
    const link = graph.links[input.link];
    const source = link ? graph.getNodeById?.(link.origin_id) : null;
    if (!source || visited.has(source)) return;
    visited.add(source);
    if (nodeClassName(source) === "Reroute") {
      visitInput(source.inputs?.[0]);
      return;
    }
    // 生成节点也可能接入 Key 输入，不能清空它们的提示词或其它参数。
    if (nodeClassName(source) === "PrimitiveNode") clearWidget(source, "value");
  };

  const input = node?.inputs?.find((item) => item?.name === fieldName);
  visitInput(input);
}

function clearNodeKey(node, fieldName = XINBAO_KEY_FIELD) {
  const config = KEY_CONFIGS.find((item) => item.field === fieldName);
  if (!config?.classes.has(nodeClassName(node))) return;
  decorateKeyWidget(node);
  clearWidget(node, fieldName);
  clearLinkedPrimitive(node, fieldName);
}

function decorateAllKeyWidgets() {
  const classNames = new Set(KEY_CONFIGS.flatMap((config) => [...config.classes]));
  for (const className of classNames) {
    for (const node of findNodesByClassName(className)) decorateKeyWidget(node);
  }
  app?.graph?.setDirtyCanvas(true, true);
}

function clearAllWorkflowKeys() {
  const activeConfigs = KEY_CONFIGS.filter((config) => (
    config.field === XINBAO_KEY_FIELD ? globalKeyConfigured : modelscopeKeyConfigured
  ));
  for (const config of activeConfigs) {
    for (const className of config.classes) {
      for (const node of findNodesByClassName(className)) clearNodeKey(node, config.field);
    }
  }
  app?.graph?.setDirtyCanvas(true, true);
}

function installWorkflowSerializationGuard() {
  const graph = app?.graph;
  if (!graph || graph.__bananaGlobalKeyGuard || typeof graph.serialize !== "function") return;
  const serialize = graph.serialize;
  graph.serialize = function () {
    if (globalKeyConfigured || modelscopeKeyConfigured) clearAllWorkflowKeys();
    return serialize.apply(this, arguments);
  };
  graph.__bananaGlobalKeyGuard = true;
}

async function refreshGlobalKeyState() {
  try {
    const response = await fetch(API_TASKS, { method: "GET", cache: "no-store" });
    if (!response.ok) return globalKeyConfigured;
    const payload = await response.json();
    globalKeyConfigured = payload?.data?.settings?.api_key_configured === true;
    modelscopeKeyConfigured = payload?.data?.settings?.modelscope_api_key_configured === true;
    installWorkflowSerializationGuard();
    decorateAllKeyWidgets();
    if (globalKeyConfigured || modelscopeKeyConfigured) clearAllWorkflowKeys();
  } catch (error) {
    console.warn(`[${EXTENSION}] 读取全局密钥状态失败`, error);
  }
  return globalKeyConfigured;
}

app.registerExtension({
  name: EXTENSION,
  setup() {
    const graphToPrompt = app.graphToPrompt;
    if (typeof graphToPrompt === "function" && !app.__bananaGlobalKeyPromptGuard) {
      app.graphToPrompt = async function () {
        await refreshGlobalKeyState();
        return graphToPrompt.apply(this, arguments);
      };
      app.__bananaGlobalKeyPromptGuard = true;
    }
    if (!window.__bananaGlobalKeyListenerReady) {
      window.addEventListener("banana:global-key-saved", () => {
        globalKeyConfigured = true;
        installWorkflowSerializationGuard();
        clearAllWorkflowKeys();
      });
      window.addEventListener("banana:modelscope-key-saved", () => {
        modelscopeKeyConfigured = true;
        installWorkflowSerializationGuard();
        clearAllWorkflowKeys();
      });
      window.__bananaGlobalKeyListenerReady = true;
    }
    void refreshGlobalKeyState();
  },
  nodeCreated(node) {
    setTimeout(() => {
      decorateKeyWidget(node);
      if (globalKeyConfigured) clearNodeKey(node, XINBAO_KEY_FIELD);
      if (modelscopeKeyConfigured) clearNodeKey(node, MODELSCOPE_KEY_FIELD);
    }, 0);
  },
  async afterConfigureGraph() {
    await refreshGlobalKeyState();
  },
});
