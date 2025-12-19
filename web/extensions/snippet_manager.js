import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const EXTENSION_NAME = "banana.snippetManager";
const TARGET_NODE = "XinbaoPromptAssistantNode";
const LEGACY_DEFAULT_NODE_SIZE = [500, 450];
// 让节点默认更“扁长”，减少无意义的纵向空白（用户仍可手动调整并保存）
const DEFAULT_NODE_SIZE = [760, 300];
// 旧版本在窄宽度下会因“自动撑高显示全部标签”导致节点被保存为超高尺寸，这里做一次性迁移兜底
const LEGACY_TALL_NODE_MIN_HEIGHT = 650;

// --- Prompt textarea height defaults (UX) ---
// 目标：提示词输入框默认高度适中；随节点缩放实时变化（由节点 size 持久化）。
const PROMPT_TEXTAREA_WIDGET_NAMES = ["text", "prefix_text"];
// 默认高度：用户希望更舒适（32 太小）
const PROMPT_TEXTAREA_DEFAULT_HEIGHT_PX = 250;
const PROMPT_TEXTAREA_MIN_HEIGHT_PX = 80;
const PROMPT_TEXTAREA_MAX_HEIGHT_PX = 2000;
const PROMPT_TEXTAREA_STORAGE_PREFIX = `${EXTENSION_NAME}.promptTextareaHeight`;
// 历史遗留：此前实现过 textarea 自身高度持久化（node.properties / localStorage）。
// 当前版本以“节点缩放控制高度”为主，因此不会再读取这些持久化值；这里保留 key 仅用于兼容旧数据结构。
const PROMPT_TEXTAREA_SCHEMA_VERSION = 2;
const PROMPT_TEXTAREA_NODE_VERSION_PROP = `${PROMPT_TEXTAREA_STORAGE_PREFIX}.v`;
const PROMPT_TEXTAREA_GLOBAL_VERSION_KEY = `${PROMPT_TEXTAREA_STORAGE_PREFIX}.globalV`;

function clampNumber(value, min, max) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    return Math.min(max, Math.max(min, value));
}

function getNodeHeightPx(node) {
    // 兼容 Array / TypedArray（部分前端会把 node.size 存成 Float64Array）
    const size = node?.size;
    if (!size || typeof size.length !== "number" || size.length < 2) return null;
    const h = size[1];
    return typeof h === "number" && Number.isFinite(h) ? h : null;
}

// Storage helpers removed (legacy)

function findWidget(node, name) {
    if (!node || !name) return null;
    if (Array.isArray(node.widgets)) {
        const hit = node.widgets.find((w) => w?.name === name);
        if (hit) return hit;
    }
    if (Array.isArray(node.inputs)) {
        for (const input of node.inputs) {
            if (input?.name === name && input?.widget) return input.widget;
        }
    }
    return null;
}

function applyPromptTextareaHeight(widget, heightPx) {
    if (!widget) return;
    const normalized = clampNumber(heightPx, PROMPT_TEXTAREA_MIN_HEIGHT_PX, PROMPT_TEXTAREA_MAX_HEIGHT_PX);
    if (normalized === null) return;

    // 尽量覆盖不同 ComfyUI/LiteGraph 版本的字段命名
    try {
        if (widget.options && typeof widget.options === "object") {
            widget.options.height = normalized;
        }
    } catch (_) {
        // ignore (read-only / sealed options)
    }

    // 新版 ComfyUI 的 BaseWidget.height 可能是只读 getter（直接赋值会抛 TypeError）
    try {
        widget.height = normalized;
    } catch (_) {
        // ignore
    }

    // 兜底：部分版本会忽略 height 字段，依赖 computeSize() 决定 widget 的占位高度
    try {
        // Fix: Returning 'normalized' (current height) makes the widget "rigid" to LiteGraph,
        // preventing the user from shrinking the node.
        // We return MIN height here so LiteGraph knows the node CAN be smaller.
        // The actual visual height is handled by the style override below.
        widget.computeSize = (width) => [width, PROMPT_TEXTAREA_MIN_HEIGHT_PX];
    } catch (_) {
        // ignore
    }

    const rawEl = widget.inputEl || widget.domEl || widget.element;
    const textareaEl = (() => {
        if (!rawEl) return null;
        const tag = rawEl.tagName;
        if (tag && String(tag).toUpperCase() === "TEXTAREA") return rawEl;
        try {
            if (typeof rawEl.querySelector === "function") {
                return rawEl.querySelector("textarea");
            }
        } catch (_) {
            // ignore
        }
        return null;
    })();

    const targets = [];
    if (rawEl && rawEl.style) targets.push(rawEl);
    if (textareaEl && textareaEl !== rawEl && textareaEl.style) targets.push(textareaEl);

    if (targets.length) {
        targets.forEach((el) => {
            try {
                const px = `${Math.round(normalized)}px`;
                el.style.setProperty("height", px, "important");
                el.style.setProperty("min-height", `${Math.round(PROMPT_TEXTAREA_MIN_HEIGHT_PX)}px`, "important");
                el.style.setProperty("max-height", `${Math.round(PROMPT_TEXTAREA_MAX_HEIGHT_PX)}px`, "important");
                // 主要由“节点缩放”控制可视范围；内容超出可视区域时仍允许滚动
                el.style.overflowY = "auto";
                el.style.resize = "none";
            } catch (_) {
                // ignore
            }
        });
    }

    // 记录本次生效的目标高度，避免后续依赖 DOM 读回出现不稳定
    try {
        widget.__bananaPromptTextareaLastAppliedHeightPx = Math.round(normalized);
    } catch (_) {
        // ignore
    }
}

// Persistence binding logic removed (legacy)

function normalizePromptTextareaValue(widget) {
    if (!widget) return;
    if (widget.value !== null && widget.value !== undefined) return;

    widget.value = "";

    const el = widget.inputEl || widget.domEl || widget.element;
    if (el) {
        try {
            el.value = "";
        } catch (_) {
            // ignore
        }
    }
}

function setupPromptTextareas(node) {
    if (!node) return;

    const promptWidgets = [];
    for (const name of PROMPT_TEXTAREA_WIDGET_NAMES) {
        const widget = findWidget(node, name);
        if (!widget) continue;

        // 兼容旧工作流：缺失字段可能会被反序列化为 null，避免 UI 里显示 "null"
        normalizePromptTextareaValue(widget);
        promptWidgets.push({ name, widget });
    }
    if (promptWidgets.length === 0) return;

    // Simplified logic: Just ensure initialization
    promptWidgets.forEach(({ widget }) => {
        if (!widget.__bananaPromptTextareaHeightInitialized) {
            applyPromptTextareaHeight(widget, PROMPT_TEXTAREA_DEFAULT_HEIGHT_PX);
            widget.__bananaPromptTextareaHeightInitialized = true;
        }
    });
}

function schedulePromptTextareaSetup(node) {
    if (!node) return;
    if (node.__bananaPromptTextareaSetupScheduled) return;
    node.__bananaPromptTextareaSetupScheduled = true;

    // 多次轻量重试：覆盖 ComfyUI 在 loadGraphData 后异步创建/调整 inputEl 的场景
    const delays = [0, 50, 200, 800, 2000];
    delays.forEach((delay) => {
        setTimeout(() => {
            try {
                setupPromptTextareas(node);
            } catch (e) {
                console.warn(`[${EXTENSION_NAME}] schedulePromptTextareaSetup 失败（已忽略）`, e);
            }
        }, delay);
    });
}

function scheduleGraphChange(node) {
    if (!node) return;
    const graph = node.graph || app?.graph;
    if (!graph || typeof graph.change !== "function") return;

    try {
        if (node.__bananaSnippetGraphChangeTimer) {
            clearTimeout(node.__bananaSnippetGraphChangeTimer);
        }
    } catch (_) {
        // ignore
    }

    node.__bananaSnippetGraphChangeTimer = setTimeout(() => {
        try {
            graph.change();
        } catch (_) {
            // ignore
        }
    }, 200);
}

// --- API Helpers ---
const SnippetApi = {
    async getSnippets() {
        const response = await api.fetchApi("/banana/snippets");
        const data = await response.json();
        return data.success ? data.data : [];
    },
    async addSnippet(content, category, color) {
        const response = await api.fetchApi("/banana/snippets", {
            method: "POST",
            body: JSON.stringify({ content, category, color })
        });
        return await response.json();
    },
    async updateSnippet(id, content, category, color) {
        const response = await api.fetchApi("/banana/snippets", {
            method: "POST",
            body: JSON.stringify({ id, content, category, color })
        });
        return await response.json();
    },
    async deleteSnippet(id) {
        const response = await api.fetchApi("/banana/snippets", {
            method: "DELETE",
            body: JSON.stringify({ id })
        });
        return await response.json();
    }
};

// --- Utils ---
function drawRoundedRect(ctx, x, y, width, height, radius, fill, stroke) {
    ctx.beginPath();
    ctx.roundRect(x, y, width, height, radius);
    if (fill) {
        ctx.fillStyle = fill;
        ctx.fill();
    }
    if (stroke) {
        ctx.strokeStyle = stroke;
        ctx.stroke();
    }
}



// --- Predefined Colors ---
// "8组白色字体看得到的清新配色"
const COLOR_PALETTE = [
    "#F44336", // Red
    "#E91E63", // Pink
    "#9C27B0", // Purple
    "#673AB7", // Deep Purple
    "#3F51B5", // Indigo
    "#2196F3", // Blue
    "#009688", // Teal
    "#4CAF50", // Green
    "#FF9800", // Orange
    "#795548", // Brown
];

// --- Modal ---
class SnippetModal {
    constructor() {
        this.element = null;
    }

    create(title, defaultData, tags, onSave) {
        if (this.element) this.close();

        const overlay = document.createElement("div");
        Object.assign(overlay.style, {
            position: "fixed", top: "0", left: "0", width: "100%", height: "100%",
            backgroundColor: "rgba(0,0,0,0.5)", zIndex: "1000", display: "flex",
            justifyContent: "center", alignItems: "center"
        });

        const panel = document.createElement("div");
        Object.assign(panel.style, {
            backgroundColor: "#222", padding: "20px", borderRadius: "8px",
            width: "400px", display: "flex", flexDirection: "column", gap: "10px",
            color: "#fff", fontFamily: "sans-serif", boxShadow: "0 4px 12px rgba(0,0,0,0.5)"
        });

        // Title
        const titleEl = document.createElement("h3");
        titleEl.innerText = title;
        titleEl.style.margin = "0 0 10px 0";
        panel.appendChild(titleEl);

        // Content Input
        const contentInput = document.createElement("textarea");
        contentInput.placeholder = "提示词内容";
        contentInput.value = defaultData.content || "";
        contentInput.rows = 4;
        Object.assign(contentInput.style, {
            width: "100%", padding: "8px", borderRadius: "4px", border: "1px solid #444",
            backgroundColor: "#333", color: "#fff", resize: "vertical"
        });
        panel.appendChild(contentInput);

        // Category Inputs Container (Flex Row)
        const catContainer = document.createElement("div");
        Object.assign(catContainer.style, {
            display: "flex", gap: "10px", width: "100%"
        });

        // 1. Custom Input (Left)
        const customCatInput = document.createElement("input");
        customCatInput.placeholder = "自定义分类";
        // If current category is NOT in the active tags list (new), put it here.
        // Actually, logic is: user types here to override.
        customCatInput.value = "";
        Object.assign(customCatInput.style, {
            flex: "1", padding: "8px", borderRadius: "4px", border: "1px solid #FFC107", // Yellow border as per image hint
            backgroundColor: "#333", color: "#fff"
        });

        // 2. Dropdown (Right)
        const catSelect = document.createElement("select");
        Object.assign(catSelect.style, {
            flex: "1", padding: "8px", borderRadius: "4px", border: "1px solid #FFC107",
            backgroundColor: "#333", color: "#fff"
        });

        // Populate Select
        // Filter out "全部" if present, or keep it but it doesn't make sense for a snippet to be just "All"
        const availableTags = tags ? tags.filter(t => t !== "全部") : [];
        // Add "默认" if not present?
        if (!availableTags.includes("默认")) availableTags.unshift("默认");

        availableTags.forEach(tag => {
            const opt = document.createElement("option");
            opt.value = tag;
            opt.innerText = tag;
            if (tag === defaultData.category) opt.selected = true;
            catSelect.appendChild(opt);
        });

        // Handle default data logic: 
        // If the snippet's category is NOT in the list, it's a "custom" one, so put it in the text box.
        // If it IS in the list, select it in dropdown.
        if (defaultData.category && !availableTags.includes(defaultData.category) && defaultData.category !== "全部") {
            customCatInput.value = defaultData.category;
            catSelect.value = ""; // Or first one
        }

        catContainer.appendChild(customCatInput);
        catContainer.appendChild(catSelect);
        panel.appendChild(catContainer);


        // Color Picker
        const colorLabel = document.createElement("div");
        colorLabel.innerText = "选择颜色:";
        colorLabel.style.fontSize = "12px";
        panel.appendChild(colorLabel);

        const colorContainer = document.createElement("div");
        Object.assign(colorContainer.style, {
            display: "flex", flexWrap: "wrap", gap: "5px"
        });

        let selectedColor = defaultData.color || COLOR_PALETTE[0];

        COLOR_PALETTE.forEach(c => {
            const swatch = document.createElement("div");
            Object.assign(swatch.style, {
                width: "24px", height: "24px", borderRadius: "50%",
                backgroundColor: c, cursor: "pointer", border: selectedColor === c ? "2px solid #fff" : "2px solid transparent"
            });
            swatch.onclick = () => {
                selectedColor = c;
                Array.from(colorContainer.children).forEach(child => child.style.border = "2px solid transparent");
                swatch.style.border = "2px solid #fff";
            };
            colorContainer.appendChild(swatch);
        });
        panel.appendChild(colorContainer);

        // Buttons
        const btnRow = document.createElement("div");
        Object.assign(btnRow.style, { display: "flex", justifyContent: "flex-end", gap: "10px", marginTop: "10px" });

        const cancelBtn = document.createElement("button");
        cancelBtn.innerText = "取消";
        Object.assign(cancelBtn.style, {
            padding: "5px 15px", borderRadius: "4px", border: "none", backgroundColor: "#555", color: "#fff", cursor: "pointer"
        });
        cancelBtn.onclick = () => this.close();

        const saveBtn = document.createElement("button");
        saveBtn.innerText = "保存";
        Object.assign(saveBtn.style, {
            padding: "5px 15px", borderRadius: "4px", border: "none", backgroundColor: "#2196F3", color: "#fff", cursor: "pointer"
        });
        saveBtn.onclick = () => {
            // Logic: Custom Input > Select Input
            const finalCategory = customCatInput.value.trim() || catSelect.value || "默认";

            onSave({
                content: contentInput.value,
                category: finalCategory,
                color: selectedColor
            });
            this.close();
        };

        btnRow.appendChild(cancelBtn);
        btnRow.appendChild(saveBtn);
        panel.appendChild(btnRow);

        overlay.appendChild(panel);
        document.body.appendChild(overlay);
        this.element = overlay;
    }

    close() {
        if (this.element) {
            document.body.removeChild(this.element);
            this.element = null;
        }
    }
}

const MODAL = new SnippetModal();

// --- Main Widget Logic ---
class SnippetManagerWidget {
    constructor(node) {
        this.node = node;
        this.snippets = [];
        this.tags = ["全部"];
        this.activeTag = "全部";
        this.editMode = false;

        this.hoveredSnippet = null;
        this.hoverStartTime = 0;
        this.hoverTimer = null; // Debounce timer

        this.rowHeight = 24;
        this.margin = 10;
        this.minHeight = 200;

        // Layout Config
        this.filterAreaHeight = 40;

        this.loadSnippets();

        // Scroll state
        this.scrollY = 0;
        this.contentHeight = 0;
        this.viewportHeight = 0;
        this.snippetStartY = 0;

        this.isLoading = false;
    }

    // Removed onWheel as scrolling is no longer needed
    // onWheel(event) { ... } 

    async loadSnippets() {
        if (this.isLoading) return;
        this.isLoading = true;
        this.node.setDirtyCanvas(true, true);

        try {
            await Promise.all([
                SnippetApi.getSnippets().then(s => this.snippets = s),
                new Promise(resolve => setTimeout(resolve, 500)) // Min delay for visual feedback
            ]);
            this.updateTags();
        } catch (e) {
            console.error("Banana Snippets: Load failed", e);
        } finally {
            this.isLoading = false;
            this.node.setDirtyCanvas(true, true);
        }
    }

    updateTags() {
        const categories = new Set(this.snippets.map(s => s.category || "默认"));
        const others = Array.from(categories).filter(c => c !== "默认").sort();
        const hasDefault = categories.has("默认");
        this.tags = ["全部"];
        if (hasDefault) this.tags.push("默认");
        this.tags.push(...others);
    }

    getFilteredSnippets() {
        if (this.activeTag === "全部") return this.snippets;
        return this.snippets.filter(s => (s.category || "默认") === this.activeTag);
    }

    // Helper to calculate total height needed for the widget for a given width
    calculateContentHeight(widgetWidth, ctx, node) {
        // Must emulate the layout logic from draw()
        // Constants matching draw()
        const contentStartX = 15;
        const contentWidth = widgetWidth - 30;
        const tagHeight = 24;
        const tagGap = 8;

        ctx.font = "12px sans-serif";

        // 1. Buttons Row & Tags
        // Re-measure buttons to know reserved width
        const editBtnText = this.editMode ? "\u9000\u51fa\u7f16\u8f91" : "\u7f16\u8f91\u6a21\u5f0f";
        const editBtnWidth = ctx.measureText(editBtnText).width + 20;
        const addBtnWidth = 40;
        const refreshBtnWidth = 24;
        const buttonsReservedWidth = editBtnWidth + addBtnWidth + refreshBtnWidth + 30;

        let tagX = contentStartX;
        let tagY = 10; // Relative to widget top

        this.tags.forEach((tag) => {
            const textWidth = ctx.measureText(tag).width + 16;
            const isFirstLine = (tagY === 10);
            const reserved = isFirstLine ? buttonsReservedWidth : 0;

            if (tagX + textWidth > (10 + contentWidth + 15) - reserved) {
                tagX = contentStartX;
                tagY += tagHeight + tagGap;
            }
            tagX += textWidth + tagGap;
        });

        let currentY = Math.max(tagY + tagHeight + 10, 45); // Min 45 for buttons area

        // Separator + Margin
        currentY += 10;

        // 2. Snippets
        const filtered = this.getFilteredSnippets();
        let snipX = contentStartX;
        let snipY = currentY;
        const snipH = 28;
        const gap = 8;

        filtered.forEach(snip => {
            let label = snip.content;
            if (label.length > 20) label = label.substring(0, 18) + "..";
            const txtMeasure = ctx.measureText(label);
            const snipW = txtMeasure.width + 20;

            if (snipX + snipW > widgetWidth - 15) {
                snipX = contentStartX;
                snipY += snipH + gap;
            }
            snipX += snipW + gap;
        });

        // Final Height with Margin and Dynamic Offset
        const WIDGET_TOP_MARGIN = 0;

        // Dynamic Offset Logic:
        // Because we force the textarea to report 60px height to LiteGraph (to allow value shrinking),
        // we must manually reserve space if the actual DOM element is larger.
        let offset = 0;
        if (node) {
            const textWidget = node.widgets.find(w => w.name === "text" || w.name === "prefix_text");
            // Check actual DOM height if available
            if (textWidget) {
                let actualHeight = 60;
                // Try to get actual visual height from DOM or internal tracking
                if (textWidget.inputEl && textWidget.inputEl.clientHeight) {
                    actualHeight = textWidget.inputEl.clientHeight;
                } else if (textWidget.last_y) {
                    // Fallback: This is harder without DOM, but draw() usually happens after DOM
                }

                // If the actual height is significantly larger than our "fake" reported 60px,
                // we need to push the snippet widget down.
                const PROMPT_MIN_HEIGHT = 60;
                if (actualHeight > PROMPT_MIN_HEIGHT) {
                    offset = actualHeight - PROMPT_MIN_HEIGHT;
                }
            }
        }

        let requiredHeight = snipY + snipH + 10;
        if (filtered.length === 0) requiredHeight = currentY + 10;

        return requiredHeight + WIDGET_TOP_MARGIN + offset;
    }

    draw(ctx, node, widgetWidth, y, height) {
        // 1. Auto-Resize Logic (Delta Based)
        const neededHeight = this.calculateContentHeight(widgetWidth, ctx, node);

        // 记录实际布局信息：用于把节点多余空间(slack)分配给提示词输入框，避免底部出现大面积空白
        try {
            node.__bananaSnippetManagerWidgetTopY = y;
            node.__bananaSnippetManagerWidgetNeededHeight = neededHeight;
            node.__bananaSnippetManagerWidgetDrawHeight = height;
        } catch (_) {
            // ignore
        }

        // 当片段区第一次被绘制/布局数据可用时，触发一次 textarea 高度重算：
        // 目的：消除“默认下方空一大截”且刷新后复现的问题。
        // 说明：在部分前端实现里，loadGraphData -> 首次 draw 的时序晚于 onConfigure/onNodeCreated 的重试窗口。
        try {
            const now = Date.now();
            const last = typeof node.__bananaPromptTextareaSetupFromSnippetDrawAt === "number"
                ? node.__bananaPromptTextareaSetupFromSnippetDrawAt
                : 0;

            // 轻量节流：避免 draw 高频触发导致重复 DOM 写入
            if (now - last > 150) {
                node.__bananaPromptTextareaSetupFromSnippetDrawAt = now;
                if (!node.__bananaPromptTextareaSetupFromSnippetDrawTimer) {
                    node.__bananaPromptTextareaSetupFromSnippetDrawTimer = setTimeout(() => {
                        try {
                            node.__bananaPromptTextareaSetupFromSnippetDrawTimer = null;
                            // setupPromptTextareas(node); // DISABLED: causes infinite resize loop
                        } catch (e) {
                            console.warn(`[${EXTENSION_NAME}] snippet draw -> setupPromptTextareas 失败（已忽略）`, e);
                        }
                    }, 0);
                }
            }
        } catch (_) {
            // ignore
        }

        // Initialize if first run
        if (this.lastCalculatedHeight === undefined) {
            // Initialize with current content height but DO NOT trigger a resize.
            // This respects the node's saved/initial size (User Preference).
            this.lastCalculatedHeight = neededHeight;
        }

        const diff = neededHeight - this.lastCalculatedHeight;

        // Resize only if content height changed meaningfully
        // Resize only if content height changed meaningfully
        if (Math.abs(diff) > 1) {
            const currentWidth = node.size[0];
            const currentHeight = node.size[1];

            // New Logic: "Delta Resize"
            // Apply the *difference* in content height to the node's total height.
            let targetHeight = currentHeight + diff;

            // Safety: Ensure we don't shrink smaller than a reasonable minimum
            // (e.g. Snippet Widget 45px + Textarea 60px + Spacing = ~120px)
            targetHeight = Math.max(targetHeight, 200);

            try {
                node.__bananaResizingInternally = true;
                node.setSize([currentWidth, targetHeight]);
            } finally {
                node.__bananaResizingInternally = false;
            }
            this.lastCalculatedHeight = neededHeight;
        }

        // --- Fill Space Logic: Expand Textarea to eliminate bottom gap ---
        // Only run this if we are NOT currently resizing the node (to avoid conflict)
        // and if usage of computeSize is stable.
        try {
            const currentHeight = node.size[1];
            // The widget starts at 'y'. The space required for THIS widget is 'neededHeight'.
            // The available space for the rest (top part, mainly Textarea) is y.
            // But we want the Snippet Widget to be at the BOTTOM.
            // So ideally: y = currentHeight - neededHeight - bottomPadding.

            // However, LiteGraph positions widgets from top to bottom. 
            // We can't set 'y' directly. We must set the height of the element ABOVE us (Textarea).

            // Current State:
            // Node Top ----------------
            // Textarea (Height H_t)
            // -------------------------
            // Snippet Widget (y, Height H_s)
            // -------------------------
            // Empty Space (Slack)
            // Node Bottom -------------

            // We want: Textarea Height = Textarea Height + Slack
            // Slack = NodeHeight - (y + H_s) - BottomPadding

            const bottomPadding = 10; // LiteGraph default margin roughly
            const slack = currentHeight - (y + neededHeight) - bottomPadding;

            // Only adjust if there is significant slack (positive) or overflow (negative)
            // And ensure we don't shrink below default.
            if (Math.abs(slack) > 4) {
                const textWidget = node.widgets.find(w => w.name === "text" || w.name === "prefix_text");
                if (textWidget) {
                    // Get current height of textarea (visual or computed)
                    // If we rely on widget.height (LiteGraph property), it might be stale?
                    // Let's use the 'last_y' diff or just trust the loop to converge.

                    // If slack is positive, we grow. If negative, we shrink.
                    // But we must respect the MINIMUM height.
                    const PROMPT_MIN_HEIGHT = 60; // Hardcoded safety min

                    let currentTextWidgetHeight = 60;
                    if (textWidget.last_y !== undefined && y !== undefined) {
                        // Estimate: The space between text widget start and snippet widget start
                        // is TextWidgetHight + Spacing.
                        // spacing is usually 20 in LiteGraph for widgets?
                        // Let's rely on the previous height property if set, or just use the slack to ADD to current.

                        // Better: Calculate Target Height directly.
                        // Target Textarea Height = NodeHeight - neededHeight - BottomPadding - TopPadding - Spacing
                        // TopPadding ~30 (Title bar)
                        // But 'y' accounts for TopPadding + Previous Widgets.
                        // logic: slack is "how much more space we have".
                        // So NewHeight = CurrentHeight + Slack.

                        // We can deduce CurrentHeight roughly from y? 
                        // No, 'y' is passed by drawing loop.

                        // Let's assume textWidget.inputEl.style.height or widget.height is accurate-ish.
                        if (textWidget.computeSize) {
                            currentTextWidgetHeight = textWidget.computeSize(node.size[0])[1];
                        }
                    }

                    let newHeight = currentTextWidgetHeight + slack;

                    // Constraint: Min Height
                    if (newHeight < PROMPT_MIN_HEIGHT) newHeight = PROMPT_MIN_HEIGHT;

                    // Optimization: Don't apply if change is small (prevents jitter)
                    if (Math.abs(newHeight - currentTextWidgetHeight) > 2) {
                        applyPromptTextareaHeight(textWidget, newHeight);
                    }
                }
            }
        } catch (e) {
            // console.warn("Fill Space Logic Error", e);
        }

        // --- Drawing ---
        const WIDGET_TOP_MARGIN = 0; // Visual gap from previous widget (textarea)

        // Dynamic Offset Calculation (Must match calculateContentHeight)
        let offset = 0;
        const textWidget = node.widgets.find(w => w.name === "text" || w.name === "prefix_text");
        if (textWidget) {
            let actualHeight = 60;
            if (textWidget.inputEl && textWidget.inputEl.clientHeight) {
                actualHeight = textWidget.inputEl.clientHeight;
            }
            const PROMPT_MIN_HEIGHT = 60;
            if (actualHeight > PROMPT_MIN_HEIGHT) {
                offset = actualHeight - PROMPT_MIN_HEIGHT;
            }
        }

        // Background
        ctx.fillStyle = "#1a1a1a";
        ctx.beginPath();
        // Start background AFTER the margin AND the offset
        const bgY = y + WIDGET_TOP_MARGIN + offset;
        const bgHeight = neededHeight - WIDGET_TOP_MARGIN - offset;
        ctx.rect(10, bgY, widgetWidth - 20, bgHeight);
        ctx.fill();

        const contentStartX = 15;
        const contentWidth = widgetWidth - 30;
        let currentY = bgY + 10;

        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        // --- Buttons (Top Right) ---
        const editBtnText = this.editMode ? "\u9000\u51fa\u7f16\u8f91" : "\u7f16\u8f91\u6a21\u5f0f";
        const editBtnWidth = ctx.measureText(editBtnText).width + 20;
        const editBtnX = 10 + contentWidth + 15 - editBtnWidth - 5;

        const addBtnText = "\u6dfb\u52a0";
        let addBtnWidth = 40;
        let addBtnX = editBtnX - addBtnWidth - 10;

        drawRoundedRect(ctx, addBtnX, currentY, addBtnWidth, 24, 4, "#2E7D32", "#4caf50");
        ctx.fillStyle = "#fff";
        ctx.fillText(addBtnText, addBtnX + addBtnWidth / 2, currentY + 11);
        this.addBtnHitbox = { x: addBtnX, y: currentY, w: addBtnWidth, h: 24 };

        // Draw Edit Button
        ctx.fillStyle = this.editMode ? "#D84315" : "#333";
        drawRoundedRect(ctx, editBtnX, currentY, editBtnWidth, 24, 4, this.editMode ? "#D84315" : "#333", "#555");
        ctx.fillStyle = "#fff";
        ctx.fillText(editBtnText, editBtnX + editBtnWidth / 2, currentY + 11);
        this.editBtnHitbox = { x: editBtnX, y: currentY, w: editBtnWidth, h: 24 };

        // Draw Refresh Button
        const refreshBtnText = "\u21bb";
        const refreshBtnWidth = 24;
        const refreshBtnX = addBtnX - refreshBtnWidth - 10;

        drawRoundedRect(ctx, refreshBtnX, currentY, refreshBtnWidth, 24, 4, "#555", "#777");

        ctx.save();
        if (this.isLoading) {
            const centerX = refreshBtnX + refreshBtnWidth / 2;
            const centerY = currentY + 12;
            const angle = (performance.now() / 300) * 2 * Math.PI;
            ctx.translate(centerX, centerY);
            ctx.rotate(angle);
            ctx.fillStyle = "#81C784";
            ctx.font = "16px sans-serif";
            ctx.fillText(refreshBtnText, 0, 0);
            this.node.setDirtyCanvas(true, false);
        } else {
            ctx.fillStyle = "#fff";
            ctx.font = "16px sans-serif";
            ctx.fillText(refreshBtnText, refreshBtnX + refreshBtnWidth / 2, currentY + 12);
        }
        ctx.restore();
        ctx.font = "12px sans-serif";
        this.refreshBtnHitbox = { x: refreshBtnX, y: currentY, w: refreshBtnWidth, h: 24 };

        const buttonsReservedWidth = editBtnWidth + addBtnWidth + refreshBtnWidth + 30;

        // --- Tags ---
        let tagX = contentStartX;
        let tagY = currentY;
        const tagHeight = 24;
        const tagGap = 8;
        this.tagHitboxes = [];

        this.tags.forEach((tag) => {
            const textWidth = ctx.measureText(tag).width + 16;
            const isFirstLine = (tagY === currentY);
            const reserved = isFirstLine ? buttonsReservedWidth : 0;

            if (tagX + textWidth > (10 + contentWidth + 15) - reserved) {
                tagX = contentStartX;
                tagY += tagHeight + tagGap;
            }

            const isSelected = tag === this.activeTag;
            ctx.fillStyle = isSelected ? "#444" : "#2a2a2a";
            const strokeColor = isSelected ? "#666" : "#383838";

            drawRoundedRect(ctx, tagX, tagY, textWidth, tagHeight, 4, isSelected ? "#555" : null, strokeColor);

            ctx.fillStyle = isSelected ? "#fff" : "#aaa";
            ctx.fillText(tag, tagX + textWidth / 2, tagY + 11);

            this.tagHitboxes.push({ x: tagX, y: tagY, w: textWidth, h: tagHeight, tag: tag });
            tagX += textWidth + tagGap;
        });

        currentY = Math.max(tagY + tagHeight + 10, currentY + 35);

        // --- Separator ---
        ctx.beginPath();
        ctx.moveTo(15, currentY);
        ctx.lineTo(widgetWidth - 15, currentY);
        ctx.strokeStyle = "#333";
        ctx.lineWidth = 1;
        ctx.stroke();

        currentY += 10;

        // --- Snippets Grid ---
        const filtered = this.getFilteredSnippets();
        let snipX = contentStartX;
        let snipY = currentY; // No scroll offset!
        const snipH = 28;
        const gap = 8;

        this.snippetHitboxes = [];

        filtered.forEach(snip => {
            let label = snip.content;
            if (label.length > 20) label = label.substring(0, 18) + "..";
            const txtMeasure = ctx.measureText(label);
            const snipW = txtMeasure.width + 20;

            if (snipX + snipW > widgetWidth - 15) {
                snipX = contentStartX;
                snipY += snipH + gap;
            }

            // Draw Snippet
            const color = snip.color || "#555";
            ctx.fillStyle = color;
            drawRoundedRect(ctx, snipX, snipY, snipW, snipH, 14, color, null);

            ctx.fillStyle = "#fff";
            ctx.fillText(label, snipX + snipW / 2, snipY + 13);

            // Badge
            const textWidget = this.node.widgets.find(w => w.name === "text");
            if (textWidget && typeof textWidget.value === "string") {
                const count = textWidget.value.split(snip.content).length - 1;
                if (count > 0) {
                    const badgeR = 8;
                    const badgeX = snipX + snipW - 5;
                    const badgeY = snipY + 5;
                    ctx.beginPath();
                    ctx.arc(badgeX, badgeY, badgeR, 0, Math.PI * 2);
                    ctx.fillStyle = "#F44336";
                    ctx.fill();
                    ctx.fillStyle = "#fff";
                    ctx.font = "10px sans-serif";
                    ctx.fillText(count.toString(), badgeX, badgeY + 1);
                    ctx.font = "12px sans-serif";
                }
            }

            this.snippetHitboxes.push({ x: snipX, y: snipY, w: snipW, h: snipH, data: snip });
            snipX += snipW + gap;
        });

        // No Scrollbar drawing needed

        // Tooltip logic needs to use non-scrolled coords logic
        // But actually the logic inside drawTooltip assumes screen coords? 
        // The previous logic used this.snippetHitboxes.
        // Those are now stored in simple widget-relative coords (no scroll).
        // onMove logic also needs update (remove scroll clip check).

        if (this.hoveredSnippet) {
            const box = this.snippetHitboxes.find(b => b.data === this.hoveredSnippet);
            if (box) {
                // Wait check time...
                const now = performance.now();
                if (now - this.hoverStartTime > 600) {
                    this.drawTooltip(ctx, box.data.content, box.x, box.y, box.w, box.h, widgetWidth, y);
                }
            }
        }
    }

    drawTooltip(ctx, text, x, y, w, h, widgetWidth, widgetY) {
        // Same implementation as before likely works, 
        // x,y are relative to widget top? 
        // No, in draw() usually we draw relative to 0,0 of wrapper? 
        // Wait, 'y' arg in draw is the vertical offset of the widget within the node.
        // And we are drawing rects at 'y', 'tagY' (which was currentY)
        // Wait, my code above uses `currentY = y + 10`. So coordinates are absolute to Node Top (or wherever ctx is).
        // Yes.
        if (this.tooltip) {
            this.drawTooltip(ctx, this.tooltip, x, y, w, h, widgetWidth, widgetY);
        }
    }

    // We need to keep drawTooltip definition if I didn't verify it was in the snippet I'm replacing...
    // The previous view_file showed drawTooltip at line 591.
    // I am replacing up to line 910?
    // Wait, the ReplacementContent above does NOT include drawTooltip implementation.
    // I must include it or ensure I don't overwrite it if I'm replacing the whole block.
    // The 'StartLine' in my tool call needs to be carefully chosen.

    // I will replace from `onWheel` (line 263) down to end of `draw`.
    // Wait, `draw` ended around line 589. 
    // `drawTooltip` was 591.
    // `onClick` was 657.
    // `onMove` was 736.

    // I need to update `onClick` and `onMove` too because they used scroll/clip logic!
    // So I should replace the whole class methods.

    // Let's refine the replacement block.
    // Start: 263 (onWheel)
    // End: 910 (onCustomWidget define in hooks)

    // This is a huge block.

    // Let's break it down or provide the full class content.
    // I will provide the methods I am changing.

    drawTooltip(ctx, text, x, y, w, h, widgetWidth, widgetY) {
        if (!text) return;
        const MAX_WIDTH = 300;
        const LINE_HEIGHT = 16;
        const PADDING = 8;
        const FONT_SIZE = 12;

        ctx.save();
        ctx.font = `${FONT_SIZE}px sans-serif`;
        ctx.textBaseline = "top";
        ctx.textAlign = "left";

        const lines = [];
        let line = "";
        for (const char of text) {
            const testLine = line + char;
            const metrics = ctx.measureText(testLine);
            if (metrics.width > MAX_WIDTH && line.length > 0) {
                lines.push(line);
                line = char;
            } else {
                line = testLine;
            }
        }
        lines.push(line);

        let maxW = 0;
        lines.forEach(l => maxW = Math.max(maxW, ctx.measureText(l).width));
        const boxW = maxW + PADDING * 2;
        const boxH = lines.length * LINE_HEIGHT + PADDING * 2;

        let boxX = x + (w / 2) - (boxW / 2);
        let boxY = y - boxH - 5;

        if (boxX < 10) boxX = 10;
        if (boxX + boxW > widgetWidth - 10) boxX = widgetWidth - 10 - boxW;

        ctx.shadowColor = "rgba(0,0,0,0.5)";
        ctx.shadowBlur = 8;
        ctx.shadowOffsetX = 2;
        ctx.shadowOffsetY = 2;
        ctx.fillStyle = "rgba(30, 30, 30, 0.95)";
        ctx.strokeStyle = "#FFC107";
        drawRoundedRect(ctx, boxX, boxY, boxW, boxH, 6, ctx.fillStyle, ctx.strokeStyle);
        ctx.shadowColor = "transparent";
        ctx.fillStyle = "#fff";
        lines.forEach((l, i) => {
            ctx.fillText(l, boxX + PADDING, boxY + PADDING + i * LINE_HEIGHT);
        });
        ctx.restore();
    }

    onClick(x, y, event) {
        // Simple hit testing without scroll offset or clipping

        // 1. Tags
        for (const box of this.tagHitboxes) {
            if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) {
                this.activeTag = box.tag;
                this.node.setDirtyCanvas(true, true);
                return;
            }
        }
        // 2. Buttons
        const eb = this.editBtnHitbox;
        if (eb && x >= eb.x && x <= eb.x + eb.w && y >= eb.y && y <= eb.y + eb.h) {
            this.editMode = !this.editMode;
            this.node.setDirtyCanvas(true, true);
            return;
        }
        const ab = this.addBtnHitbox;
        if (ab && x >= ab.x && x <= ab.x + ab.w && y >= ab.y && y <= ab.y + ab.h) {
            this.openAddDialog();
            return;
        }
        const rb = this.refreshBtnHitbox;
        if (rb && x >= rb.x && x <= rb.x + rb.w && y >= rb.y && y <= rb.y + rb.h) {
            this.loadSnippets();
            return;
        }

        // 3. Snippets
        for (const box of this.snippetHitboxes) {
            if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) {
                if (this.editMode) {
                    this.openEditDialog(box.data);
                } else {
                    const textWidget = this.node.widgets.find(w => w.name === "text");
                    let count = 0;
                    if (textWidget && typeof textWidget.value === "string") {
                        count = textWidget.value.split(box.data.content).length - 1;
                    }
                    if (!event.shiftKey && count > 0) {
                        this.removeText(box.data.content);
                    } else {
                        this.appendText(box.data.content);
                    }
                }
                return;
            }
        }
    }

    onMove(x, y) {
        let hit = null;
        for (const box of this.snippetHitboxes) {
            if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) {
                hit = box.data;
                break;
            }
        }
        if (this.hoverTimer) {
            clearTimeout(this.hoverTimer);
            this.hoverTimer = null;
        }
        if (this.hoveredSnippet !== hit) {
            this.hoveredSnippet = hit;
            this.node.setDirtyCanvas(true, false);
        }
        if (this.hoveredSnippet) {
            this.hoverStartTime = performance.now();
            this.hoverTimer = setTimeout(() => {
                this.node.setDirtyCanvas(true, false);
            }, 600);
        }
    }

    openAddDialog() {
        // ... (Keep existing implementation logic)
        // Need to re-state it if I'm replacing the whole block, or just don't replace this part.
        // openAddDialog starts around line 776. 
        // I can stop replacement before openAddDialog?
    }

    // Wait, openAddDialog and openEditDialog are after onMove.
    // I can replace from onWheel (263) to onMove (774).
    // And also I need to update the node hook at the bottom for onMouseWheel removel.

    // Strategy:
    // 1. Replace SnippetManagerWidget methods from onWheel to onMove (exclusive of openAddDialog).
    // 2. Replace the registerExtension block's onNodeCreated to remove hook Wheel and update computeSize.

    // Let's do 1 now.


    openAddDialog() {
        let initialContent = "";
        // Pre-fill from text widget if available
        const textWidget = this.node.widgets.find(w => w.name === "text");
        if (textWidget && textWidget.value && typeof textWidget.value === "string") {
            const raw = textWidget.value.trim();
            initialContent = raw;
        }

        MODAL.create("\u6dfb\u52a0\u63d0\u793a\u8bcd\u7247\u6bb5", { category: this.activeTag, content: initialContent }, this.tags, async (data) => {
            if (!data.content) return;
            await SnippetApi.addSnippet(data.content, data.category, data.color);
            this.loadSnippets();
        });
    }

    openEditDialog(snippet) {
        MODAL.create("编辑/删除 片段", snippet, this.tags, async (data) => {
            if (data.content) {
                await SnippetApi.updateSnippet(snippet.id, data.content, data.category, data.color);
            }
            this.loadSnippets();
        });

        // Add Delete button to the open modal manually
        const overlay = MODAL.element;
        if (overlay) {
            const panel = overlay.children[0];
            const btnRow = panel.lastChild;
            const delBtn = document.createElement("button");
            delBtn.innerText = "删除";
            Object.assign(delBtn.style, {
                padding: "5px 15px", borderRadius: "4px", border: "none", backgroundColor: "#D32F2F", color: "#fff", cursor: "pointer", marginRight: "auto"
            });
            delBtn.onclick = async () => {
                await SnippetApi.deleteSnippet(snippet.id);
                this.loadSnippets();
                MODAL.close();
            };
            btnRow.insertBefore(delBtn, btnRow.firstChild);
        }
    }

    appendText(text) {
        // Find "text" widget
        const widget = this.node.widgets.find(w => w.name === "text");
        if (widget) {
            let val = widget.value || "";
            // Append logic from design: "click to append to current input box + separator"
            if (val && !val.trim().endsWith(",")) {
                val = val.trim() + ",";
            }
            // Add space if needed
            if (val && !val.endsWith(" ")) {
                val += " ";
            }
            widget.value = val + text;
            this.node.setDirtyCanvas(true, true);
        }
    }

    removeText(text) {
        const widget = this.node.widgets.find(w => w.name === "text");
        if (widget && widget.value) {
            let val = widget.value;
            // Find last occurrence to be intuitive (undo latest action)
            const lastIndex = val.lastIndexOf(text);
            if (lastIndex !== -1) {
                // Remove text
                let before = val.substring(0, lastIndex);
                let after = val.substring(lastIndex + text.length);

                // Clean up comma/spaces
                let newVal = before + after;
                newVal = newVal.replace(/,\s*,/g, ",");
                newVal = newVal.replace(/^,\s*/, "").replace(/,\s*$/, "");
                newVal = newVal.replace(/\s\s+/g, " ");

                widget.value = newVal;
                this.node.setDirtyCanvas(true, true);
            }
        }
    }
}
app.registerExtension({
    name: EXTENSION_NAME,
    async beforeRegisterNodeDef(nodeType, nodeData, app) {
        if (nodeData.name === TARGET_NODE) {
            // 前端扩展异常不能影响节点注册，否则会出现“节点变红/不可用”
            try {
                if (nodeType?.prototype?.__bananaSnippetManagerPatched) return;
                nodeType.prototype.__bananaSnippetManagerPatched = true;
            } catch (e) {
                console.warn(`[${EXTENSION_NAME}] patch guard 失败（将继续尝试挂载）`, e);
            }

            const onConfigure = nodeType.prototype.onConfigure;
            nodeType.prototype.onConfigure = function (configuredNodeData) {
                let r;
                try {
                    r = onConfigure ? onConfigure.apply(this, arguments) : undefined;
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] onConfigure 原逻辑执行失败`, e);
                    r = undefined;
                }

                try {
                    // 提示词输入框默认高度（仅初始化）
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] 尺寸迁移逻辑已移除`);
                }

                // 提示词输入框默认高度（并在用户调整后持久化）
                try {
                    // setupPromptTextareas(this); // 移除旧的初始化，由 draw 循环动态接管，防止冲突
                    // 但我们需要确保初始有一个合理的高度
                    const textWidget = this.widgets.find(w => w.name === "text" || w.name === "prefix_text");
                    if (textWidget) {
                        // Apply default if really small/undefined
                        if (!textWidget.computeSize || textWidget.computeSize(this.size[0])[1] < 60) {
                            applyPromptTextareaHeight(textWidget, 60);
                        }
                    }
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] setupPromptTextareas 失败（已忽略）`, e);
                }

                return r;
            };

            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                let r;
                try {
                    r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] onNodeCreated 原逻辑执行失败`, e);
                    r = undefined;
                }

                try {
                    // Hook per-node onMouseMove to support passive hover (tooltips)
                    const origOnMouseMove = this.onMouseMove;
                    this.onMouseMove = function (event, pos) {
                        try {
                            if (origOnMouseMove) origOnMouseMove.apply(this, arguments);
                        } catch (e) {
                            console.warn(`[${EXTENSION_NAME}] onMouseMove 原逻辑执行失败`, e);
                        }

                        try {
                            if (this.snippetManager) {
                                // Pass node-relative coordinates to the manager
                                this.snippetManager.onMove(pos[0], pos[1]);
                            }
                        } catch (e) {
                            console.warn(`[${EXTENSION_NAME}] tooltip hover 逻辑执行失败`, e);
                        }
                    };
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] onMouseMove hook 挂载失败（已忽略）`, e);
                }

                try {
                    // Add Custom Widget
                    // 注意：部分 ComfyUI/LiteGraph 版本中，addCustomWidget 返回的 widget 上
                    // 需要显式挂载 computeSize，LiteGraph 才会调用（否则 draw() 的 height 会一直是默认 20）。
                    const widgetDef = {
                        name: "snippet_manager_ui",
                        type: "snippet_manager_debug",
                        // Use dynamic height if available, else default
                        computeSize: (width) => {
                            let h = 300;
                            if (this.snippetManager && this.snippetManager.lastCalculatedHeight) {
                                h = this.snippetManager.lastCalculatedHeight;
                            }
                            return [width, h];
                        },
                        draw: (ctx, node, width, y, height) => {
                            if (!this.snippetManager) {
                                this.snippetManager = new SnippetManagerWidget(this);
                            }
                            this.snippetManager.draw(ctx, node, width, y, height);
                        },
                        mouse: (event, pos, node) => {
                            if (event.type === "pointerdown" && this.snippetManager) {
                                this.snippetManager.onClick(pos[0], pos[1], event);
                            }
                            return false;
                        }
                    };
                    const widget = this.addCustomWidget(widgetDef);
                    try {
                        if (widget && typeof widgetDef.computeSize === "function") {
                            widget.computeSize = widgetDef.computeSize;
                        }
                    } catch (_) {
                        // ignore
                    }
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] snippet_manager_ui widget 创建失败（节点仍可用）`, e);
                }

                try {
                    // 调整默认尺寸（仅对“新建节点”生效；不要覆盖工作流反序列化恢复的 size）
                    // 说明：不同 ComfyUI/LiteGraph 版本里，onNodeCreated / onConfigure 的调用顺序可能不同；
                    // 因此这里采用“下一轮事件循环再判断”的方式，避免在反序列化阶段误覆盖用户保存的尺寸。
                    if (!this.__bananaSnippetDefaultSizeScheduled) {
                        this.__bananaSnippetDefaultSizeScheduled = true;
                        setTimeout(() => {
                            try {
                                if (this.__bananaSnippetHasSerializedSize) return;
                                if (typeof this.setSize === "function") {
                                    this.setSize(DEFAULT_NODE_SIZE);
                                }
                            } catch (e) {
                                console.warn(`[${EXTENSION_NAME}] 默认节点尺寸延迟设置失败（已忽略）`, e);
                            }
                        }, 0);
                    }
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] 默认节点尺寸设置失败（已忽略）`, e);
                }

                try {
                    // 及时应用输入框高度（部分版本 inputEl 在创建后异步就绪，这里做一次轻量重试）
                    setupPromptTextareas(this);
                    schedulePromptTextareaSetup(this);
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] 输入框高度初始化失败（已忽略）`, e);
                }

                return r;
            };

            const onResize = nodeType.prototype.onResize;
            nodeType.prototype.onResize = function () {
                let r;
                try {
                    r = onResize ? onResize.apply(this, arguments) : undefined;
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] onResize 原逻辑执行失败`, e);
                    r = undefined;
                }

                // 节点尺寸变化时，不再强制 setupPromptTextareas，而是让 draw 循环自动计算填充
                // 这样避免了 onResize -> setup -> resize -> onResize 的死循环风险
                /*
                try {
                    if (!this.__bananaResizingInternally) {
                        setupPromptTextareas(this);
                    }
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] onResize -> setupPromptTextareas 失败（已忽略）`, e);
                }
                */

                // 尺寸变化需要触发图变更，确保 ComfyUI 的自动保存能够捕捉到 resize（否则刷新会回到旧尺寸）
                try {
                    scheduleGraphChange(this);
                } catch (e) {
                    console.warn(`[${EXTENSION_NAME}] onResize -> scheduleGraphChange 失败（已忽略）`, e);
                }

                return r;
            };
        }
    }
});
