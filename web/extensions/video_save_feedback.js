import { app } from "../../scripts/app.js";
import { ComfyWidgets } from "../../scripts/widgets.js";

app.registerExtension({
    name: "Comfy.Banana.VideoSaveFeedback",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name === "XinbaoBatchSaveVideo") {
            // 添加 onExecuted 回调
            const onExecuted = nodeType.prototype.onExecuted;
            nodeType.prototype.onExecuted = function (message) {
                onExecuted?.apply(this, arguments);

                // message 包含后端返回的 output (如果有 ui key)
                // 这里的 message 结构通常是: { "images": [...], "text": [...] } (如果后端返回了 {"ui": {"images":..., "text":...}})

                if (message && message.text) {
                    let textContent = "";

                    // text 可能是字符串数组
                    if (Array.isArray(message.text)) {
                        textContent = message.text.join("\n");
                    } else {
                        textContent = String(message.text);
                    }

                    // 查找是否已存在展示用的 widget
                    const wNamespace = "feedback_text";
                    let widget = this.widgets?.find((w) => w.name === wNamespace);

                    // 如果不存在，创建新的 TEXT widget
                    if (!widget) {
                        // 临时创建一个 widget，使用 STRING 类型
                        // ComfyWidgets.STRING(this, wNamespace, ["STRING", { multiline: true }], app);
                        // 但上面的帮助函数通常用于 input，我们这里直接 push 一个自定义 widget 或者使用 build-in logic

                        // 尝试使用 ComfyUI 标准方法添加 widget
                        // 注意：输出节点通常可以动态添加 widget
                        widget = ComfyWidgets["STRING"](this, wNamespace, ["STRING", { multiline: true }], app).widget;
                        widget.inputEl.readOnly = true;
                        widget.inputEl.style.opacity = 0.6;
                        widget.serialize = false; // 不保存到 workflow
                    }

                    if (widget) {
                        widget.value = textContent;
                        // 强制刷新节点尺寸以适应新内容
                        this.onResize?.(this.size);
                    }
                }
            };

            // 确保节点尺寸能自适应
            const onResize = nodeType.prototype.onResize;
            nodeType.prototype.onResize = function (size) {
                onResize?.apply(this, arguments);
                // 简单的自动高度调整逻辑 (如果在 graph 中也可以用 computeSize)
            }
        }
    },
});
