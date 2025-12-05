import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const EXTENSION_NAME = "banana.snippetManager";
const TARGET_NODE = "XinbaoPromptAssistantNode";

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
    }

    async loadSnippets() {
        try {
            this.snippets = await SnippetApi.getSnippets();
            this.updateTags();
            this.node.setDirtyCanvas(true, true);
        } catch (e) {
            console.error("Banana Snippets: Load failed", e);
        }
    }

    updateTags() {
        const categories = new Set(this.snippets.map(s => s.category || "默认"));
        const others = Array.from(categories).filter(c => c !== "默认").sort();
        // Ensure "默认" is second if it exists, otherwise just "全部" then others
        // Actually, if "默认" is in categories, put it first.
        const hasDefault = categories.has("默认");
        this.tags = ["全部"];
        if (hasDefault) this.tags.push("默认");
        this.tags.push(...others);
    }

    getFilteredSnippets() {
        if (this.activeTag === "全部") return this.snippets;
        return this.snippets.filter(s => (s.category || "默认") === this.activeTag);
    }

    draw(ctx, node, widgetWidth, y, height) {
        // Background - Remove the stroke as requested ("Yellow box")
        ctx.fillStyle = "#1a1a1a";
        ctx.beginPath();
        ctx.rect(10, y, widgetWidth - 20, height);
        ctx.fill();
        // ctx.strokeStyle = "#333";
        // ctx.stroke(); // Removing conflicting border

        const contentStartX = 15;
        const contentWidth = widgetWidth - 30; // 15 margins
        let currentY = y + 10;

        ctx.font = "12px sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        // --- Buttons (Top Right) ---
        // Calculate buttons first to know reserved space for first line of tags
        const editBtnText = this.editMode ? "\u9000\u51fa\u7f16\u8f91" : "\u7f16\u8f91\u6a21\u5f0f"; // "退出编辑" : "编辑模式"
        const editBtnWidth = ctx.measureText(editBtnText).width + 20;
        const editBtnX = 10 + contentWidth + 15 - editBtnWidth - 5; // Align right with some margin

        // Add Button (Left of Edit Button)
        const addBtnText = "\u6dfb\u52a0"; // "添加"
        let addBtnWidth = 40;
        let addBtnX = editBtnX - addBtnWidth - 10;

        // Draw Add Button
        drawRoundedRect(ctx, addBtnX, currentY, addBtnWidth, 24, 4, "#2E7D32", "#4caf50");
        ctx.fillStyle = "#fff";
        ctx.fillText(addBtnText, addBtnX + addBtnWidth / 2, currentY + 11); // Adjusted Y -1px for visual center
        this.addBtnHitbox = { x: addBtnX, y: currentY, w: addBtnWidth, h: 24 };

        // Draw Edit Button
        ctx.fillStyle = this.editMode ? "#D84315" : "#333";
        drawRoundedRect(ctx, editBtnX, currentY, editBtnWidth, 24, 4, this.editMode ? "#D84315" : "#333", "#555");
        ctx.fillStyle = "#fff";
        ctx.fillText(editBtnText, editBtnX + editBtnWidth / 2, currentY + 11); // Adjusted Y -1px
        this.editBtnHitbox = { x: editBtnX, y: currentY, w: editBtnWidth, h: 24 };

        const buttonsReservedWidth = editBtnWidth + addBtnWidth + 20;

        // --- Tags (Flow Layout) ---
        let tagX = contentStartX;
        let tagY = currentY;
        const tagHeight = 24;
        const tagGap = 8;

        this.tagHitboxes = [];

        this.tags.forEach((tag, index) => {
            const textWidth = ctx.measureText(tag).width + 16;

            // Checks if we need to wrap
            // For first line, respect button space. For subsequent lines, full width.
            const isFirstLine = (tagY === currentY);
            const visibleWidth = widgetWidth - 20; // total available
            const usedWidth = tagX - 10; // relative to widget left
            const reserved = isFirstLine ? buttonsReservedWidth : 0;

            // simple check: if current X + width > widget Right Edge - reserved
            if (tagX + textWidth > (10 + contentWidth + 15) - reserved) {
                tagX = contentStartX;
                tagY += tagHeight + tagGap;
            }

            const isSelected = tag === this.activeTag;

            // Draw Tag
            ctx.fillStyle = isSelected ? "#444" : "#2a2a2a";
            // if (isSelected) ctx.strokeStyle = "#888"; else ctx.strokeStyle = "#444"; 
            // Removed stroke or made it subtle if user wishes, but let's keep it clean
            const strokeColor = isSelected ? "#666" : "#383838";

            drawRoundedRect(ctx, tagX, tagY, textWidth, tagHeight, 4, isSelected ? "#555" : null, strokeColor);

            ctx.fillStyle = isSelected ? "#fff" : "#aaa";
            // Visual text alignment fix: y + 11 instead of 12 (move up 1px)
            ctx.fillText(tag, tagX + textWidth / 2, tagY + 11);

            this.tagHitboxes.push({
                x: tagX, y: tagY, w: textWidth, h: tagHeight, tag: tag
            });

            tagX += textWidth + tagGap;
        });

        // Update currentY to below tags
        currentY = Math.max(tagY + tagHeight + 10, currentY + 35);

        // --- Separator Line ---
        ctx.beginPath();
        ctx.moveTo(15, currentY);
        ctx.lineTo(widgetWidth - 15, currentY);
        ctx.strokeStyle = "#333";
        ctx.lineWidth = 1;
        ctx.stroke();

        currentY += 10; // Margin after separator

        // --- Snippets Grid ---
        const filtered = this.getFilteredSnippets();
        let snipX = contentStartX;
        let snipY = currentY;
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

            // Draw Snippet Chip
            const color = snip.color || "#555";

            ctx.fillStyle = color;
            drawRoundedRect(ctx, snipX, snipY, snipW, snipH, 14, color, null);

            ctx.fillStyle = "#fff";
            ctx.fillText(label, snipX + snipW / 2, snipY + 13); // Centered visually

            // --- Usage Badge Logic ---
            const textWidget = this.node.widgets.find(w => w.name === "text");
            if (textWidget && textWidget.value && typeof textWidget.value === "string") {
                const mainText = textWidget.value;
                // Count occurrences
                // Escape special regex chars if we used regex, but split is safer for simple string matching
                // Note: This counts overlapping occurrences if any (unlikely for phrases)
                // A simple split by content gives parts. length - 1 is count.
                if (snip.content) {
                    const count = mainText.split(snip.content).length - 1;
                    if (count > 0) {
                        // Draw Badge
                        const badgeR = 8;
                        const badgeX = snipX + snipW - 5; // Top right overlay
                        const badgeY = snipY + 5;

                        ctx.beginPath();
                        ctx.arc(badgeX, badgeY, badgeR, 0, Math.PI * 2);
                        ctx.fillStyle = "#F44336"; // Red
                        ctx.fill();

                        ctx.fillStyle = "#fff";
                        ctx.font = "10px sans-serif"; // Smaller font for badge
                        ctx.fillText(count.toString(), badgeX, badgeY + 1); // Centered

                        // Restore font
                        ctx.font = "12px sans-serif";
                    }
                }
            }

            this.snippetHitboxes.push({
                x: snipX, y: snipY, w: snipW, h: snipH, data: snip
            });

            snipX += snipW + gap;
        });

        // Tooltip for Truncated Snippets
        if (this.hoveredSnippet) {
            const box = this.snippetHitboxes.find(b => b.data === this.hoveredSnippet);
            if (box && box.data.content.length > 20) {
                const now = performance.now();
                const elapsed = now - this.hoverStartTime;
                const DELAY_MS = 600;
                const FADE_MS = 300;

                if (elapsed >= DELAY_MS) {
                    let opacity = (elapsed - DELAY_MS) / FADE_MS;
                    if (opacity > 1) opacity = 1;

                    ctx.save();
                    ctx.globalAlpha = opacity;
                    this.drawTooltip(ctx, box.data.content, box.x, box.y, box.w, box.h, widgetWidth, y);
                    ctx.restore();

                    if (opacity < 1) {
                        this.node.setDirtyCanvas(true, false);
                    }
                }
            }
        }
    }

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

        // Word Wrap
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

        // Compute Dimensions
        let maxW = 0;
        lines.forEach(l => maxW = Math.max(maxW, ctx.measureText(l).width));
        const boxW = maxW + PADDING * 2;
        const boxH = lines.length * LINE_HEIGHT + PADDING * 2;

        // Position (Top Centered)
        let boxX = x + (w / 2) - (boxW / 2);
        let boxY = y - boxH - 5; // 5px gap

        // Clamp Horizontal
        if (boxX < 10) boxX = 10;
        if (boxX + boxW > widgetWidth - 10) boxX = widgetWidth - 10 - boxW;

        // Draw Box
        ctx.shadowColor = "rgba(0,0,0,0.5)";
        ctx.shadowBlur = 8;
        ctx.shadowOffsetX = 2;
        ctx.shadowOffsetY = 2;

        ctx.fillStyle = "rgba(30, 30, 30, 0.95)";
        ctx.strokeStyle = "#FFC107";

        drawRoundedRect(ctx, boxX, boxY, boxW, boxH, 6, ctx.fillStyle, ctx.strokeStyle);

        ctx.shadowColor = "transparent";

        // Draw Text
        ctx.fillStyle = "#fff";
        lines.forEach((l, i) => {
            ctx.fillText(l, boxX + PADDING, boxY + PADDING + i * LINE_HEIGHT);
        });

        ctx.restore();
    }


    // --- Interaction ---
    onClick(x, y, event) {
        // 1. Tags
        for (const box of this.tagHitboxes) {
            if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) {
                this.activeTag = box.tag;
                this.node.setDirtyCanvas(true, true);
                return;
            }
        }

        // 2. Edit Button
        const eb = this.editBtnHitbox;
        if (eb && x >= eb.x && x <= eb.x + eb.w && y >= eb.y && y <= eb.y + eb.h) {
            this.editMode = !this.editMode;
            this.node.setDirtyCanvas(true, true);
            return;
        }

        // 3. Add Button
        const ab = this.addBtnHitbox;
        if (ab && x >= ab.x && x <= ab.x + ab.w && y >= ab.y && y <= ab.y + ab.h) {
            this.openAddDialog();
            return;
        }

        // 4. Snippets
        for (const box of this.snippetHitboxes) {
            if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) {
                if (this.editMode) {
                    // Edit/Delete
                    this.openEditDialog(box.data);
                } else {
                    // Smart Toggle Logic
                    const textWidget = this.node.widgets.find(w => w.name === "text");
                    let count = 0;
                    if (textWidget && textWidget.value && typeof textWidget.value === "string") {
                        if (box.data.content) {
                            count = textWidget.value.split(box.data.content).length - 1;
                        }
                    }

                    // If Shift is pressed -> Always Add
                    // If Count > 0 and No Shift -> Remove (Undo)
                    // Else -> Add
                    if (!event.shiftKey && count > 0) {
                        this.removeText(box.data.content);
                    } else {
                        this.appendText(box.data.content);
                    }
                }
                return;
            }
        }

        // Clear hovered state if clicked (optional, but good practice)
        // this.hoveredSnippet = null; 
    }

    onMove(x, y) {
        let hit = null;
        for (const box of this.snippetHitboxes) {
            if (x >= box.x && x <= box.x + box.w && y >= box.y && y <= box.y + box.h) {
                hit = box.data;
                break;
            }
        }

        // Always update hover start time on move to support "stationary" check
        // Debounce logic: Reset any pending tooltip triggers
        if (this.hoverTimer) {
            clearTimeout(this.hoverTimer);
            this.hoverTimer = null;
        }

        // Update state
        if (this.hoveredSnippet !== hit) {
            this.hoveredSnippet = hit;
            this.node.setDirtyCanvas(true, false); // Clear old tooltip immediately
        }

        if (this.hoveredSnippet) {
            this.hoverStartTime = performance.now();

            // Set timer to check for stationary state
            this.hoverTimer = setTimeout(() => {
                // If this timer fires, it means no mouse move happened for 600ms
                this.node.setDirtyCanvas(true, false);
            }, 600);
        }
    }

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
                if (confirm("确定删除此片段吗?")) {
                    await SnippetApi.deleteSnippet(snippet.id);
                    this.loadSnippets();
                    MODAL.close();
                }
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
            const onNodeCreated = nodeType.prototype.onNodeCreated;
            nodeType.prototype.onNodeCreated = function () {
                const r = onNodeCreated ? onNodeCreated.apply(this, arguments) : undefined;

                // Hook per-node onMouseMove to support passive hover (tooltips)
                const origOnMouseMove = this.onMouseMove;
                this.onMouseMove = function (event, pos) {
                    if (origOnMouseMove) origOnMouseMove.apply(this, arguments);

                    if (this.snippetManager) {
                        // Pass node-relative coordinates to the manager
                        this.snippetManager.onMove(pos[0], pos[1]);
                    }
                };

                // Add Custom Widget
                this.addCustomWidget({
                    name: "snippet_manager_ui",
                    type: "snippet_manager_debug",
                    computeSize: (width) => [width, 300], // Fixed height for now
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
                });

                // Adjust size
                this.setSize([500, 450]);

                return r;
            };
        }
    }
});
