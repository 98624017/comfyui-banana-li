import { app } from "../../../scripts/app.js";
import { api } from "../../../scripts/api.js";

const EXTENSION = "xinbao.batchDetailImageSaver";
const TARGET_NODE = "XinbaoBatchDetailImageSaver";

function getViewUrl(imageInfo) {
  if (!imageInfo) return null;
  const { filename, subfolder, type } = imageInfo;
  const params = new URLSearchParams({
    filename: filename || "",
    subfolder: subfolder || "",
    type: type || "output",
  });
  return `/view?${params.toString()}`;
}

function findExecutedNode(detail) {
  if (!detail || !app?.graph?.getNodeById) return null;
  const candidates = [];
  if (detail.display_node !== undefined) candidates.push(detail.display_node);
  if (detail.node !== undefined) candidates.push(detail.node);
  for (const id of candidates) {
    const resolved = app.graph.getNodeById(id);
    if (resolved) return resolved;
  }
  return null;
}

async function fetchJson(url, options = {}) {
  const res = await api.fetchApi(url, options);
  let payload = null;
  try {
    payload = await res.json();
  } catch (e) {
    payload = null;
  }
  return { ok: res.ok, status: res.status, payload };
}

async function fetchState(nodeId) {
  const url = `/banana/batch_detail/state?node_id=${encodeURIComponent(nodeId)}`;
  const { ok, payload } = await fetchJson(url);
  if (!ok) {
    const msg = payload?.message || "获取缓存失败";
    throw new Error(msg);
  }
  return payload?.data;
}

async function postSelectVersion(nodeId, index, versionId) {
  const { ok, payload } = await fetchJson("/banana/batch_detail/select_version", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: nodeId, index, version_id: versionId }),
  });
  if (!ok) {
    throw new Error(payload?.message || "切换版本失败");
  }
  return payload?.data;
}

async function postRegenerate(nodeId, items) {
  const { ok, payload } = await fetchJson("/banana/batch_detail/regenerate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: nodeId, items }),
  });
  if (!ok) {
    throw new Error(payload?.message || "局部重试失败");
  }
  return payload;
}

async function postSaveStitch(nodeId) {
  const { ok, payload } = await fetchJson("/banana/batch_detail/save_stitch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ node_id: nodeId }),
  });
  if (!ok) {
    throw new Error(payload?.message || "保存拼接图失败");
  }
  return payload;
}

function createOverlay() {
  const overlay = document.createElement("div");
  overlay.style.position = "fixed";
  overlay.style.inset = "0";
  overlay.style.background = "rgba(0,0,0,0.45)";
  overlay.style.zIndex = "999999";
  overlay.style.display = "flex";
  overlay.style.alignItems = "center";
  overlay.style.justifyContent = "center";
  overlay.style.padding = "16px";
  return overlay;
}

function createPanel() {
  const panel = document.createElement("div");
  panel.style.width = "min(1100px, 96vw)";
  panel.style.height = "min(820px, 92vh)";
  panel.style.background = "#111827";
  panel.style.color = "#E5E7EB";
  panel.style.border = "1px solid rgba(255,255,255,0.08)";
  panel.style.borderRadius = "12px";
  panel.style.boxShadow = "0 16px 40px rgba(0,0,0,0.45)";
  panel.style.display = "flex";
  panel.style.flexDirection = "column";
  panel.style.overflow = "hidden";
  return panel;
}

function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  Object.entries(props).forEach(([k, v]) => {
    if (k === "style" && v && typeof v === "object") {
      Object.assign(node.style, v);
      return;
    }
    if (k === "className") {
      node.className = v;
      return;
    }
    if (k.startsWith("on") && typeof v === "function") {
      node.addEventListener(k.slice(2).toLowerCase(), v);
      return;
    }
    if (v !== undefined) node[k] = v;
  });
  for (const c of children) {
    if (c === null || c === undefined) continue;
    if (typeof c === "string") node.appendChild(document.createTextNode(c));
    else node.appendChild(c);
  }
  return node;
}

async function drawStitch(canvas, imageInfos) {
  const ctx = canvas.getContext("2d");
  if (!ctx) return;

  const urls = (imageInfos || [])
    .map(getViewUrl)
    .filter(Boolean);

  if (urls.length === 0) {
    canvas.width = 800;
    canvas.height = 120;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#0B1220";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#9CA3AF";
    ctx.font = "14px sans-serif";
    ctx.fillText("暂无可拼接图片（请先运行一次该节点）", 16, 64);
    return;
  }

  const imgs = await Promise.all(
    urls.map((u) => new Promise((resolve) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => resolve(null);
      img.src = u;
    }))
  );

  const validImgs = imgs.filter(Boolean);
  if (validImgs.length === 0) {
    canvas.width = 800;
    canvas.height = 120;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#0B1220";
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = "#FCA5A5";
    ctx.font = "14px sans-serif";
    ctx.fillText("图片加载失败，请检查输出目录或刷新后重试", 16, 64);
    return;
  }

  const targetW = 520;
  const heights = validImgs.map((img) => Math.max(1, Math.round((img.height * targetW) / img.width)));
  const totalH = heights.reduce((a, b) => a + b, 0);
  canvas.width = targetW;
  canvas.height = totalH;

  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#FFFFFF";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  let y = 0;
  for (let i = 0; i < validImgs.length; i++) {
    const img = validImgs[i];
    const h = heights[i];
    ctx.drawImage(img, 0, y, targetW, h);
    y += h;
  }
}

function openPanelForNode(node) {
  if (!node) return;
  const nodeId = String(node.id);

  const overlay = createOverlay();
  const panel = createPanel();
  overlay.appendChild(panel);

  const title = el("div", {
    style: {
      display: "flex",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "12px 14px",
      background: "rgba(255,255,255,0.03)",
      borderBottom: "1px solid rgba(255,255,255,0.08)",
    },
  }, [
    el("div", { style: { fontSize: "14px", fontWeight: "700" } }, [
      `心宝❤批量详情图面板（节点 ${nodeId}）`,
    ]),
    el("button", {
      innerText: "关闭",
      style: {
        padding: "6px 10px",
        borderRadius: "8px",
        border: "1px solid rgba(255,255,255,0.12)",
        background: "rgba(255,255,255,0.06)",
        color: "#E5E7EB",
        cursor: "pointer",
      },
      onclick: () => overlay.remove(),
    }),
  ]);

  const content = el("div", {
    style: {
      padding: "12px 14px",
      display: "grid",
      gridTemplateColumns: "560px 1fr",
      gap: "12px",
      overflow: "hidden",
      flex: "1 1 auto",
    },
  });

  const leftCol = el("div", { style: { display: "flex", flexDirection: "column", gap: "10px", overflow: "hidden" } });
  const rightCol = el("div", { style: { display: "flex", flexDirection: "column", gap: "10px", overflow: "hidden" } });

  const infoBox = el("div", {
    style: {
      padding: "10px 10px",
      borderRadius: "10px",
      border: "1px solid rgba(255,255,255,0.08)",
      background: "rgba(255,255,255,0.03)",
      fontSize: "12px",
      lineHeight: "1.5",
      color: "#D1D5DB",
    },
    innerText: "正在加载缓存状态…",
  });

  const stitchBox = el("div", {
    style: {
      borderRadius: "10px",
      border: "1px solid rgba(255,255,255,0.08)",
      background: "rgba(255,255,255,0.03)",
      overflow: "auto",
      padding: "10px",
      height: "calc(92vh - 210px)",
      maxHeight: "600px",
    },
  });
  const stitchCanvas = el("canvas", { width: 520, height: 200 });
  stitchBox.appendChild(stitchCanvas);

  leftCol.appendChild(infoBox);
  leftCol.appendChild(stitchBox);

  const listBox = el("div", {
    style: {
      borderRadius: "10px",
      border: "1px solid rgba(255,255,255,0.08)",
      background: "rgba(255,255,255,0.03)",
      overflow: "auto",
      padding: "10px",
      height: "calc(92vh - 210px)",
      maxHeight: "600px",
    },
  });

  const footer = el("div", {
    style: {
      display: "flex",
      gap: "10px",
      alignItems: "center",
      justifyContent: "space-between",
      padding: "12px 14px",
      borderTop: "1px solid rgba(255,255,255,0.08)",
      background: "rgba(255,255,255,0.03)",
    },
  });

  const statusLine = el("div", {
    style: { fontSize: "12px", color: "#9CA3AF", flex: "1 1 auto" },
    innerText: "",
  });

  const btnRow = el("div", { style: { display: "flex", gap: "8px", flex: "0 0 auto" } });

  function styledButton(text, color) {
    return el("button", {
      innerText: text,
      style: {
        padding: "8px 12px",
        borderRadius: "10px",
        border: "1px solid rgba(255,255,255,0.12)",
        background: color || "rgba(255,255,255,0.06)",
        color: "#E5E7EB",
        cursor: "pointer",
        fontSize: "12px",
        fontWeight: "700",
      },
    });
  }

  const btnSaveStitch = styledButton("保存拼接图", "rgba(16,185,129,0.22)");
  const btnRetry = styledButton("重试选中", "rgba(59,130,246,0.22)");

  btnRow.appendChild(btnSaveStitch);
  btnRow.appendChild(btnRetry);
  footer.appendChild(statusLine);
  footer.appendChild(btnRow);

  rightCol.appendChild(listBox);

  content.appendChild(leftCol);
  content.appendChild(rightCol);

  panel.appendChild(title);
  panel.appendChild(content);
  panel.appendChild(footer);
  document.body.appendChild(overlay);

  // 状态：用于采集用户勾选与编辑内容
  const checkboxByIndex = new Map();
  const textareaByIndex = new Map();
  const versionSelectByIndex = new Map();

  async function refresh() {
    try {
      statusLine.innerText = "正在刷新…";
      const state = await fetchState(nodeId);
      const slots = state?.slots || [];

      const refUrls = (state?.reference_urls || []).filter(Boolean);
      const infoLines = [
        `线路: ${state?.route_choice || "-"}`,
        `模型: ${state?.model_type || "-"}`,
        `比例: ${state?.aspect_ratio || "-"}`,
        `分辨率: ${state?.image_size || "-"}`,
        `Top-P: ${state?.top_p ?? "-"}`,
        `API Key: ${state?.api_key_masked || "-"}`,
        `参考图 URL: ${refUrls.length > 0 ? refUrls.join(" | ") : "无"}`,
      ];
      infoBox.innerText = infoLines.join("\n");

      // 右侧列表重绘
      listBox.innerHTML = "";
      checkboxByIndex.clear();
      textareaByIndex.clear();
      versionSelectByIndex.clear();

      const activeImages = [];

      slots.forEach((slot) => {
        const index = slot.index;
        const versions = slot.versions || [];
        const activeId = slot.active_version_id;
        const active = versions.find((v) => v.version_id === activeId) || versions[versions.length - 1];

        if (active?.image) activeImages.push(active.image);

        const row = el("div", {
          style: {
            display: "grid",
            gridTemplateColumns: "24px 120px 1fr",
            gap: "10px",
            padding: "10px",
            borderRadius: "10px",
            border: "1px solid rgba(255,255,255,0.08)",
            background: "rgba(0,0,0,0.12)",
            marginBottom: "10px",
          },
        });

        const cb = el("input", { type: "checkbox" });
        checkboxByIndex.set(index, cb);

        const thumbUrl = getViewUrl(active?.image);
        const thumb = el("img", {
          src: thumbUrl || "",
          style: {
            width: "120px",
            height: "120px",
            objectFit: "cover",
            borderRadius: "10px",
            border: "1px solid rgba(255,255,255,0.10)",
            background: "#0B1220",
          },
          title: `index ${index}`,
          onclick: () => {
            if (!thumbUrl) return;
            window.open(thumbUrl, "_blank");
          },
        });

        const right = el("div", { style: { display: "flex", flexDirection: "column", gap: "8px" } });
        const topRow = el("div", { style: { display: "flex", gap: "10px", alignItems: "center" } });

        const label = el("div", {
          innerText: `第 ${index + 1} 张`,
          style: { fontSize: "12px", fontWeight: "700", color: "#E5E7EB", minWidth: "60px" },
        });

        const verSel = el("select", {
          style: {
            flex: "1 1 auto",
            minWidth: "200px",
            padding: "6px 8px",
            borderRadius: "8px",
            border: "1px solid rgba(255,255,255,0.12)",
            background: "rgba(255,255,255,0.06)",
            color: "#E5E7EB",
          },
        });
        versionSelectByIndex.set(index, verSel);

        versions.forEach((v, i) => {
          const opt = document.createElement("option");
          const status = v.status === "failed" ? "失败" : "成功";
          opt.value = v.version_id;
          opt.textContent = `v${i + 1} | ${status} | ${new Date((v.created_at || 0) * 1000).toLocaleTimeString()}`;
          if (v.version_id === activeId) opt.selected = true;
          verSel.appendChild(opt);
        });

        verSel.addEventListener("change", async () => {
          try {
            statusLine.innerText = `正在切换第 ${index + 1} 张版本…`;
            await postSelectVersion(nodeId, index, verSel.value);
            await refresh();
            statusLine.innerText = `已切换第 ${index + 1} 张版本`;
          } catch (e) {
            statusLine.innerText = String(e?.message || e);
          }
        });

        topRow.appendChild(label);
        topRow.appendChild(verSel);

        const ta = el("textarea", {
          value: active?.prompt || "",
          placeholder: "在这里修改该张图片的提示词，然后勾选并点击“重试选中”",
          style: {
            width: "100%",
            height: "90px",
            resize: "vertical",
            padding: "8px 10px",
            borderRadius: "10px",
            border: "1px solid rgba(255,255,255,0.12)",
            background: "rgba(255,255,255,0.04)",
            color: "#E5E7EB",
            fontSize: "12px",
            lineHeight: "1.45",
          },
        });
        textareaByIndex.set(index, ta);

        const errorLine = active?.error_message
          ? el("div", { style: { fontSize: "12px", color: "#FCA5A5" } }, [`错误: ${active.error_message}`])
          : null;

        right.appendChild(topRow);
        right.appendChild(ta);
        if (errorLine) right.appendChild(errorLine);

        row.appendChild(cb);
        row.appendChild(thumb);
        row.appendChild(right);
        listBox.appendChild(row);
      });

      await drawStitch(stitchCanvas, activeImages);
      statusLine.innerText = `已加载 ${slots.length} 张（版本上限 10）`;
    } catch (e) {
      statusLine.innerText = String(e?.message || e);
    }
  }

  btnRetry.onclick = async () => {
    try {
      const items = [];
      for (const [index, cb] of checkboxByIndex.entries()) {
        if (!cb.checked) continue;
        const ta = textareaByIndex.get(index);
        const prompt = ta ? ta.value : "";
        items.push({ index, prompt });
      }
      if (items.length === 0) {
        statusLine.innerText = "请先勾选需要重试的图片";
        return;
      }
      statusLine.innerText = `正在重试 ${items.length} 张…`;
      const result = await postRegenerate(nodeId, items);
      const updated = result?.updated || [];
      const okCount = updated.filter((x) => x.status === "success").length;
      const failCount = updated.filter((x) => x.status === "failed").length;
      await refresh();
      statusLine.innerText = `重试完成：成功 ${okCount}，失败 ${failCount}`;
    } catch (e) {
      statusLine.innerText = String(e?.message || e);
    }
  };

  btnSaveStitch.onclick = async () => {
    try {
      statusLine.innerText = "正在保存拼接图…";
      const result = await postSaveStitch(nodeId);
      const image = result?.image;
      const url = getViewUrl(image);
      statusLine.innerText = "拼接图已保存";
      if (url) {
        window.open(url, "_blank");
      }
    } catch (e) {
      statusLine.innerText = String(e?.message || e);
    }
  };

  // 初次加载
  refresh();

  // 点击遮罩层空白区域关闭
  overlay.addEventListener("click", (evt) => {
    if (evt.target === overlay) {
      overlay.remove();
    }
  });
}

function ensureOpenButton(node) {
  if (!node || node.__xinbaoBatchDetailReady) return;

  const widget = node.addCustomWidget({
    name: "xinbao-batch-detail-open",
    type: "xinbao-batch-detail-open",
    node,
    draw(ctx, _, widgetWidth, y, height) {
      const text = "打开详情面板";
      const font = "12px sans-serif";
      const paddingX = 14;
      const marginTop = 6;
      const radius = 8;
      const active = this.__active;
      const previousFont = ctx.font;
      const previousAlign = ctx.textAlign;
      ctx.font = font;
      const textWidth = ctx.measureText(text).width;
      const rectWidth = Math.max(textWidth + paddingX * 2, 150);
      const rectHeight = Math.max((height || 22), 22);
      const x = (widgetWidth - rectWidth) / 2;
      const yPos = y + marginTop;
      ctx.fillStyle = active ? "#2563EB" : "#3B82F6";
      ctx.strokeStyle = active ? "#1D4ED8" : "#2563EB";
      ctx.beginPath();
      if (ctx.roundRect) ctx.roundRect(x, yPos, rectWidth, rectHeight, radius);
      else ctx.rect(x, yPos, rectWidth, rectHeight);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle = "#ffffff";
      ctx.textAlign = "center";
      ctx.fillText(text, x + rectWidth / 2, yPos + rectHeight * 0.65);
      ctx.font = previousFont;
      ctx.textAlign = previousAlign;
      this.__rect = { x, y: yPos, w: rectWidth, h: rectHeight };
    },
    mouse(event, position) {
      const trigger =
        ((globalThis.LiteGraph && globalThis.LiteGraph.pointerevents_method) || "pointer") + "down";
      if (event?.type !== trigger) return false;
      const rect = this.__rect;
      if (!rect) return false;
      const [x, y] = position;
      if (x >= rect.x && x <= rect.x + rect.w && y >= rect.y && y <= rect.y + rect.h) {
        this.__active = true;
        this.node?.graph?.setDirtyCanvas(true, true);
        setTimeout(() => {
          this.__active = false;
          this.node?.graph?.setDirtyCanvas(true, true);
        }, 160);
        openPanelForNode(this.node);
        return true;
      }
      return false;
    },
    computeSize(widgetWidth) {
      return [widgetWidth, 34];
    },
    serialize: false,
  });

  // 尽量把按钮放到末尾
  if (Array.isArray(node.widgets) && widget) {
    const idx = node.widgets.indexOf(widget);
    if (idx > -1 && idx !== node.widgets.length - 1) {
      node.widgets.splice(idx, 1);
      node.widgets.push(widget);
    }
  }

  node.__xinbaoBatchDetailReady = true;
}

app.registerExtension({
  name: EXTENSION,
  nodeCreated(node) {
    if (node?.comfyClass === TARGET_NODE) {
      ensureOpenButton(node);
    }
  },
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) return;
    const original = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = original?.apply(this, arguments);
      ensureOpenButton(this);
      return result;
    };
  },
  setup() {
    // 监听节点执行事件，记录最近一次输出数量（可用于后续扩展 UI 提示）
    api.addEventListener("executed", ({ detail }) => {
      const node = findExecutedNode(detail);
      if (!node || node.comfyClass !== TARGET_NODE) return;
      const meta = detail?.output?.xinbao_batch_detail;
      if (meta && typeof meta.count === "number") {
        node.__xinbaoBatchDetailCount = meta.count;
      }
    });
  },
});

