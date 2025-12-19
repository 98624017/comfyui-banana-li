import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

// 预设内容缓存（包含兜底文案，防止接口加载失败）
let PRESETS = {
    "None": "",
    "文字大师 (Typography Expert)": "你是世界顶级的平面设计师和字体排印专家。你的核心能力是生成包含完美文字的图像。请确保画面中的所有连文本拼写完全正确、字体风格与画面氛围完美契合。注重文字的易读性、字间距以及文字与背景的自然融合（如霓虹灯牌、手写便签、杂志排版）。",
    "电影感 (Cinematic Realism)": "你是一位拿过奥斯卡奖的电影摄影师。请创作具有强烈电影感的画面。关键词：变形宽银幕镜头（Anamorphic Lens）、胶片颗粒、色差（Chromatic Aberration）、戏剧性布光（如伦勃朗光）。注重叙事感和氛围，画面应像是一部高预算电影的截帧，细节丰富且真实。",
    "超现实狂想 (Surreal Vision)": "你是一位富有远见的超现实主义概念艺术家。请通过意想不到的元素组合来构建画面。将现实与梦境融合，挑战物理规律（如悬浮海洋、通过云层的阶梯）。使用大胆的色彩和打破常规的构图，同时保持极高的渲染精度和材质真实感。",
};

// 获取远端最新预设（可选更新）
async function fetchPresets() {
    try {
        const response = await api.fetchApi("/banana/system_presets");
        if (response.ok) {
            const remotePresets = await response.json();
            Object.assign(PRESETS, remotePresets);
            console.log("[BananaV2] System presets loaded/updated:", Object.keys(PRESETS).length);
        }
    } catch (e) {
        console.warn("BananaV2: Failed to fetch system instruction presets, using built-in defaults.", e);
    }
}

app.registerExtension({
    name: "Banana.SystemInstruction",
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name === "BananaImageNodeV2") {
            await fetchPresets();

            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;

                if (!this.widgets) return r;

                // 关键：捕获当前节点实例，避免闭包中 this 丢失
                const self = this;

                const presetWidget = this.widgets.find((w) => w.name === "system_instruction_preset");
                const textWidget = this.widgets.find((w) => w.name === "system_instruction_text");

                if (presetWidget && textWidget) {
                    const originalCallback = presetWidget.callback;
                    presetWidget.callback = function (value) {
                        try {
                            const presetContent = PRESETS[value];
                            if (value !== "None" && presetContent) {
                                // 直接赋值并刷新画布（与 snippet_manager.js 保持一致）
                                textWidget.value = presetContent;
                                // 使用捕获的 node 实例触发画布重绘
                                self.setDirtyCanvas(true, true);
                            }
                        } catch (e) {
                            console.error("Auto-fill system instruction failed:", e);
                        }

                        if (originalCallback) {
                            return originalCallback.apply(this, arguments);
                        }
                    };
                }

                return r;
            };
        }
    },
});
