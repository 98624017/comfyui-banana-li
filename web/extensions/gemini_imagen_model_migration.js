import { app } from "../../../scripts/app.js";

const TARGET_NODES = new Set(["BananaImageNode", "BananaImageNodeV2"]);
const LEGACY_VIP_LABEL = "gemini-3-pro-image-preview-vip(高成本)";
const NEW_VIP_LABEL = "gemini-3-pro-image-preview-vip";

const MIGRATION_PATCH_FLAG = "__bananaGeminiImagenModelMigrationPatched";

function migrateLegacyVipLabelInObject(root) {
  if (!root || typeof root !== "object") return;

  const stack = [root];
  while (stack.length > 0) {
    const current = stack.pop();
    if (!current) continue;

    if (Array.isArray(current)) {
      for (let i = 0; i < current.length; i++) {
        const value = current[i];
        if (value === LEGACY_VIP_LABEL) {
          current[i] = NEW_VIP_LABEL;
          continue;
        }
        if (value && typeof value === "object") {
          stack.push(value);
        }
      }
      continue;
    }

    if (typeof current === "object") {
      for (const key in current) {
        const value = current[key];
        if (value === LEGACY_VIP_LABEL) {
          current[key] = NEW_VIP_LABEL;
          continue;
        }
        if (value && typeof value === "object") {
          stack.push(value);
        }
      }
    }
  }
}

function patchGraphLoadMigration() {
  // 目标：兼容旧工作流值，但不把 "(高成本)" 重新加入下拉选项。
  // 方案：在工作流反序列化之前，直接把旧字符串替换为新字符串，避免前端控件回退到默认值。
  if (!app || app[MIGRATION_PATCH_FLAG]) return;

  let wrappedAny = false;
  const wrap = (target, methodName) => {
    const original = target?.[methodName];
    if (typeof original !== "function") return;
    if (original.__bananaGeminiImagenWrapped) return;

    const wrapped = function () {
      try {
        migrateLegacyVipLabelInObject(arguments?.[0]);
      } catch (e) {
        console.warn("[Banana.GeminiImagenModelMigration] 工作流迁移失败（已忽略）", e);
      }
      return original.apply(this, arguments);
    };
    wrapped.__bananaGeminiImagenWrapped = true;
    target[methodName] = wrapped;
    wrappedAny = true;
  };

  wrap(app, "loadGraphData");
  wrap(app?.graph, "configure");
  if (wrappedAny) {
    app[MIGRATION_PATCH_FLAG] = true;
  }
}

function normalizeModelWidget(node) {
  if (!node?.widgets) return;
  const widget = node.widgets.find((w) => w?.name === "model_type");
  if (!widget) return;

  if (widget.value === LEGACY_VIP_LABEL) {
    widget.value = NEW_VIP_LABEL;
  }

  if (widget?.options?.values && Array.isArray(widget.options.values)) {
    const index = widget.options.values.indexOf(LEGACY_VIP_LABEL);
    if (index !== -1) {
      widget.options.values.splice(index, 1, NEW_VIP_LABEL);
    }
  }

  // 迁移后需要主动标记画布脏，确保 UI 立即刷新（不同 ComfyUI 版本 API 不完全一致）。
  if (node?.graph?.setDirtyCanvas) {
    node.graph.setDirtyCanvas(true, true);
    return;
  }
  node?.setDirtyCanvas?.(true, true);
}

app.registerExtension({
  name: "Banana.GeminiImagenModelMigration",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    patchGraphLoadMigration();
    if (!TARGET_NODES.has(nodeData?.name)) return;

    const onNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
      normalizeModelWidget(this);
      return result;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const result = onConfigure ? onConfigure.apply(this, arguments) : undefined;
      normalizeModelWidget(this);
      return result;
    };
  },
});
