import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const progress = new WeakMap();

function receiveProgress({ detail }) {
  const node = app.graph?.getNodeById(detail?.node_id);
  if (!node || node.comfyClass !== "XinbaoModelScopeImageGenerate") return;
  progress.set(node, detail);
  node.setDirtyCanvas(true, false);
}

app.registerExtension({
  name: "xinbao.modelscopeImageLayout",
  setup() {
    api.addEventListener("xinbao_modelscope_image_progress", receiveProgress);
    for (const event of ["execution_error", "execution_interrupted"]) {
      api.addEventListener(event, ({ detail }) => {
        const node = app.graph?.getNodeById(detail?.node_id);
        const state = node && progress.get(node);
        if (!state || state.terminal) return;
        receiveProgress({ detail: { ...state, node_id: detail.node_id,
          terminal: event === "execution_interrupted" ? "已取消" : "失败" } });
      });
    }
  },
  nodeCreated(node) {
    if (node.comfyClass !== "XinbaoModelScopeImageGenerate") return;
    node.addCustomWidget({
      name: "魔搭任务状态", type: "xinbao_status", options: { serialize: false },
      computeSize: () => [0, 48],
      draw(ctx, node, width, y) {
        const state = progress.get(node);
        ctx.save();
        ctx.font = "12px sans-serif";
        ctx.textAlign = "left";
        ctx.fillStyle = "#b9c5d6";
        if (!state) {
          ctx.fillText("等待运行 · 运行后显示魔搭任务状态", 12, y + 18);
        } else {
          const counts = {};
          for (const stage of state.states) if (stage !== "完成") counts[stage] = (counts[stage] || 0) + 1;
          const label = state.terminal || Object.entries(counts).map(([stage, count]) => `${stage} ${count}`).join(" · ");
          ctx.fillText(label, 12, y + 15, width - 24);
          ctx.fillText(`已完成 ${state.completed}/${state.total}`, 12, y + 31, width - 24);
          ctx.fillStyle = "#253142";
          ctx.fillRect(12, y + 37, width - 24, 5);
          ctx.fillStyle = state.terminal === "失败" ? "#e77b7b" : "#55bbce";
          ctx.fillRect(12, y + 37, (width - 24) * state.completed / state.total, 5);
        }
        ctx.restore();
      },
    });
    const removed = node.onRemoved;
    node.onRemoved = function () {
      progress.delete(this);
      return removed?.apply(this, arguments);
    };
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "XinbaoModelScopeImageGenerate") return;
    const original = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      const result = original?.apply(this, arguments);
      const values = info?.widgets_values;
      if (!Array.isArray(values)) return result;
      let names, legacyValues;
      if (["文生图", "图片编辑"].includes(values[2])) {
        // Qwen workflows before the model/resolution/ratio controls were added.
        names = ["prompt", "modelscope_api_key", "模型选择", "模式", "分辨率", "比例", "batch_size", "max_concurrency", "绕过代理", "禁用SSL验证"];
        if (["1K", "2K"].includes(values[3])) {
          legacyValues = [values[0], values[1], "Qwen-Image-2.1", ...values.slice(2, 9)];
        } else {
          legacyValues = [values[0], values[1], "Qwen-Image-2.1", values[2], "1K", "16:9", ...values.slice(3, 7)];
        }
      } else if (values.length >= 14 && values[2] !== "Qwen-Image-2.1") {
        // The retired Z-Image node stored model/custom prompt controls before batch settings.
        legacyValues = [values[0], values[1], "Qwen-Image-2.1", "文生图", "1K", "16:9", ...values.slice(10, 14)];
        names = ["prompt", "modelscope_api_key", "模型选择", "模式", "分辨率", "比例", "batch_size", "max_concurrency", "绕过代理", "禁用SSL验证"];
      } else {
        return result;
      }
      names.forEach((name, index) => {
        const widget = this.widgets?.find((item) => item.name === name);
        if (widget && legacyValues[index] !== undefined) widget.value = legacyValues[index];
      });
      return result;
    };
  },
});
