import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

const EXTENSION = "banana.videoTaskManager";
const API_LIST = "/banana/video_tasks";
const API_REFRESH = "/banana/video_tasks/refresh";
const API_KEY = "/banana/video_tasks/key";
const API_SETTINGS = "/banana/video_tasks/settings";
const API_OPEN_LOCAL = "/banana/video_tasks/open_local";

const POLL_INTERVAL_MS = 5000;
const STORAGE_BUTTON_POS = `${EXTENSION}.buttonPos`;
const STORAGE_PANEL_POS = `${EXTENSION}.panelPos`;
const STORAGE_READ_TASKS = `${EXTENSION}.readSuccessTasks`;
const DRAG_THRESHOLD_PX = 4;
const VIEWPORT_MARGIN_PX = 8;
const MAX_VISIBLE_TASK_ROWS = 6;
const FALLBACK_TASK_ROW_HEIGHT_PX = 34;
const FALLBACK_TASK_HEADER_HEIGHT_PX = 38;
const TASK_TABLE_HEADER_BG = "#2a2a2a";

let overlayEl = null;
let panelEl = null;
let taskTableEl = null;
let taskTableScrollEl = null;
let taskTableBodyEl = null;
let statusEl = null;
let autoDownloadCheckbox = null;
let badgeEl = null;
let pollTimer = null;
let isFetching = false;
let floatingBtnEl = null;

function readStorageJson(key) {
  try {
    const raw = window.localStorage.getItem(key);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (_) {
    return null;
  }
}

function writeStorageJson(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch (_) {
    // ignore storage failures (private mode / quota / etc.)
  }
}

function setFixedPosition(targetEl, left, top) {
  targetEl.style.position = "fixed";
  targetEl.style.left = `${Math.round(left)}px`;
  targetEl.style.top = `${Math.round(top)}px`;
  targetEl.style.right = "auto";
  targetEl.style.bottom = "auto";
  targetEl.style.transform = "none";
}

function clampToViewport(targetEl, left, top, marginPx = VIEWPORT_MARGIN_PX) {
  const rect = targetEl.getBoundingClientRect();
  const maxLeft = Math.max(marginPx, window.innerWidth - rect.width - marginPx);
  const maxTop = Math.max(marginPx, window.innerHeight - rect.height - marginPx);
  return {
    left: Math.min(Math.max(marginPx, left), maxLeft),
    top: Math.min(Math.max(marginPx, top), maxTop),
  };
}

function applyStoredFixedPosition(targetEl, storageKey) {
  const pos = readStorageJson(storageKey);
  if (!pos || !Number.isFinite(pos.left) || !Number.isFinite(pos.top)) return false;
  const clamped = clampToViewport(targetEl, Number(pos.left), Number(pos.top));
  setFixedPosition(targetEl, clamped.left, clamped.top);
  return true;
}

function formatTime(tsSeconds) {
  const ts = Number(tsSeconds);
  if (!Number.isFinite(ts) || ts <= 0) return "-";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch (_) {
    return String(ts);
  }
}

function statusLabel(status) {
  switch ((status || "").toLowerCase()) {
    case "pending":
      return "排队中";
    case "processing":
      return "生成中";
    case "waiting_key":
      return "等待Key";
    case "success":
      return "已完成";
    case "failed":
      return "失败";
    default:
      return status || "未知";
  }
}

async function fetchJson(url, options) {
  const response = await api.fetchApi(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok || !payload?.success) {
    const message = payload?.message || `HTTP ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

async function postJson(url, data) {
  return fetchJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
}

function buildViewUrl(localFile) {
  if (!localFile) return "";
  const filename = localFile.filename;
  const subfolder = localFile.subfolder;
  const type = localFile.type || "output";
  if (!filename || !subfolder) return "";
  const params = new URLSearchParams({ filename, subfolder, type });
  return `/view?${params.toString()}`;
}

function isWindowsPlatform() {
  const platform =
    (navigator.userAgentData && navigator.userAgentData.platform) ||
    navigator.platform ||
    navigator.userAgent ||
    "";
  return String(platform).toLowerCase().includes("win");
}

async function tryOpenInWindowsExplorer({ taskId, localFile }) {
  if (!isWindowsPlatform()) return false;
  const id = String(taskId || "").trim();
  try {
    const payload = await postJson(API_OPEN_LOCAL, { id, local_file: localFile || null });
    return !!payload?.data?.opened;
  } catch (_) {
    return false;
  }
}

function ensureOverlay() {
  if (overlayEl) return overlayEl;

  overlayEl = document.createElement("div");
  overlayEl.style.cssText = `
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.45);
    display: none;
    align-items: center;
    justify-content: center;
    z-index: 10000;
  `;

  panelEl = document.createElement("div");
  panelEl.style.cssText = `
    position: fixed;
    left: 50%;
    top: 50%;
    transform: translate(-50%, -50%);
    width: 960px;
    max-width: calc(100vw - 32px);
    max-height: calc(100vh - 32px);
    overflow: hidden;
    background: #1f1f1f;
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 14px;
    box-shadow: 0 18px 60px rgba(0,0,0,0.40);
    color: #f2f2f2;
    display: flex;
    flex-direction: column;
  `;

  const header = document.createElement("div");
  header.style.cssText = `
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 16px;
    border-bottom: 1px solid rgba(255,255,255,0.08);
    cursor: move;
    user-select: none;
    touch-action: none;
  `;

  const title = document.createElement("div");
  title.textContent = "🎬 心宝❤任务中心";
  title.style.cssText = "font-size: 16px; font-weight: 700;";

  const headerActions = document.createElement("div");
  headerActions.style.cssText = "display:flex; gap:10px; align-items:center;";

  const refreshBtn = document.createElement("button");
  refreshBtn.textContent = "刷新";
  refreshBtn.style.cssText = `
    padding: 6px 12px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.22);
    background: transparent;
    color: inherit;
    cursor: pointer;
  `;
  refreshBtn.addEventListener("click", () => void fetchAndRender());

  const closeBtn = document.createElement("button");
  closeBtn.textContent = "关闭";
  closeBtn.style.cssText = `
    padding: 6px 12px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.22);
    background: transparent;
    color: inherit;
    cursor: pointer;
  `;
  closeBtn.addEventListener("click", () => hideOverlay());

  headerActions.appendChild(refreshBtn);
  headerActions.appendChild(closeBtn);
  header.appendChild(title);
  header.appendChild(headerActions);

  // 可拖动：仅拖动面板本身（不影响按钮点击）
  (() => {
    let dragging = false;
    let pointerId = null;
    let offsetX = 0;
    let offsetY = 0;
    let startLeft = 0;
    let startTop = 0;
    let moved = false;

    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      pointerId = null;
      if (!moved) return;
      const rect = panelEl.getBoundingClientRect();
      writeStorageJson(STORAGE_PANEL_POS, { left: Math.round(rect.left), top: Math.round(rect.top) });
    };

    header.addEventListener("pointerdown", (event) => {
      if (event.target && event.target.closest("button")) return;
      if (typeof event.button === "number" && event.button !== 0) return;

      const rect = panelEl.getBoundingClientRect();
      dragging = true;
      moved = false;
      pointerId = event.pointerId;
      offsetX = event.clientX - rect.left;
      offsetY = event.clientY - rect.top;
      startLeft = rect.left;
      startTop = rect.top;
      setFixedPosition(panelEl, rect.left, rect.top);
      try {
        header.setPointerCapture(pointerId);
      } catch (_) {
        // ignore
      }
    });

    header.addEventListener("pointermove", (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      const nextLeft = event.clientX - offsetX;
      const nextTop = event.clientY - offsetY;
      const clamped = clampToViewport(panelEl, nextLeft, nextTop);
      setFixedPosition(panelEl, clamped.left, clamped.top);
      const dx = clamped.left - startLeft;
      const dy = clamped.top - startTop;
      if (!moved && Math.abs(dx) + Math.abs(dy) >= DRAG_THRESHOLD_PX) moved = true;
    });

    header.addEventListener("pointerup", (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      try {
        header.releasePointerCapture(pointerId);
      } catch (_) {
        // ignore
      }
      endDrag();
    });

    header.addEventListener("pointercancel", (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      endDrag();
    });
  })();

  const body = document.createElement("div");
  body.style.cssText = `
    padding: 14px 16px 16px;
    overflow: auto;
    display: flex;
    flex-direction: column;
    gap: 14px;
  `;

  // Settings + Key section
  const settingsRow = document.createElement("div");
  settingsRow.style.cssText = `
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    align-items: stretch;
  `;

  const keyPanel = document.createElement("div");
  keyPanel.style.cssText = `
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 12px;
    padding: 12px;
    background: rgba(255,255,255,0.03);
    display: flex;
    flex-direction: column;
    gap: 10px;
  `;

  const keyTitle = document.createElement("div");
  keyTitle.textContent = "本次会话 Key（不落盘；Sora/Veo/豆包 通用）";
  keyTitle.style.cssText = "font-weight: 600;";
  keyPanel.appendChild(keyTitle);

  const keyRow = document.createElement("div");
  keyRow.style.cssText = "display:flex; gap:10px; align-items:center;";

  const keyLabel = document.createElement("div");
  keyLabel.textContent = "Key";
  keyLabel.style.cssText = "width: 64px; opacity: 0.9;";

  const keyInput = document.createElement("input");
  keyInput.type = "password";
  keyInput.placeholder = "输入 Key（Sora/Veo/豆包 通用，本次会话）";
  keyInput.style.cssText = `
      flex: 1;
      padding: 8px 10px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.12);
      background: rgba(0,0,0,0.25);
      color: inherit;
      outline: none;
    `;

  const keySave = document.createElement("button");
  keySave.textContent = "保存";
  keySave.style.cssText = `
      padding: 7px 12px;
      border-radius: 10px;
      border: 1px solid rgba(255,255,255,0.18);
      background: rgba(255,255,255,0.06);
      color: inherit;
      cursor: pointer;
      min-width: 74px;
    `;

  keySave.addEventListener("click", async () => {
    const value = String(keyInput.value || "").trim();
    if (!value) return;

    const providers = ["sora", "veo", "doubao"];
    try {
      const results = await Promise.allSettled(
        providers.map((provider) => postJson(API_KEY, { provider, api_key: value }))
      );
      const failed = results
        .map((res, idx) => ({ res, provider: providers[idx] }))
        .filter((item) => item.res.status === "rejected");

      if (!failed.length) {
        keyInput.value = "";
        setStatus("✅ Key 已保存（仅本次会话，已应用到 Sora/Veo/豆包）");
      } else {
        const providerList = failed.map((item) => item.provider).join("/");
        const firstErr = failed[0].res.reason;
        setStatus(`⚠️ Key 保存部分失败（${providerList}）：${firstErr?.message || firstErr || "未知错误"}`);
      }

      // 保存 Key 后，尽快刷新列表（waiting_key 任务可能会转入 processing）
      void fetchAndRender();
    } catch (err) {
      setStatus(`❌ 保存 Key 失败：${err?.message || err}`);
    }
  });

  keyRow.appendChild(keyLabel);
  keyRow.appendChild(keyInput);
  keyRow.appendChild(keySave);
  keyPanel.appendChild(keyRow);

  const settingsPanel = document.createElement("div");
  settingsPanel.style.cssText = `
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 12px;
    padding: 12px;
    background: rgba(255,255,255,0.03);
    display: flex;
    flex-direction: column;
    gap: 10px;
  `;

  const settingsTitle = document.createElement("div");
  settingsTitle.textContent = "设置";
  settingsTitle.style.cssText = "font-weight: 600;";
  settingsPanel.appendChild(settingsTitle);

  const autoRow = document.createElement("label");
  autoRow.style.cssText = "display:flex; gap:10px; align-items:center; cursor: pointer;";
  autoDownloadCheckbox = document.createElement("input");
  autoDownloadCheckbox.type = "checkbox";
  autoDownloadCheckbox.addEventListener("change", async () => {
    try {
      const enabled = !!autoDownloadCheckbox.checked;
      await postJson(API_SETTINGS, { auto_download: enabled });
      setStatus(`✅ 自动下载已${enabled ? "开启" : "关闭"}`);
    } catch (err) {
      setStatus(`❌ 更新设置失败：${err?.message || err}`);
    }
  });
  const autoLabel = document.createElement("div");
  autoLabel.textContent = "自动下载到 output/video_tasks（默认关闭）";
  autoLabel.style.cssText = "opacity: 0.95; font-size: 13px;";
  autoRow.appendChild(autoDownloadCheckbox);
  autoRow.appendChild(autoLabel);
  settingsPanel.appendChild(autoRow);

  statusEl = document.createElement("div");
  statusEl.style.cssText = "opacity: 0.9; font-size: 12px; color: rgba(255,255,255,0.75);";
  statusEl.textContent = "就绪";
  settingsPanel.appendChild(statusEl);

  settingsRow.appendChild(keyPanel);
  settingsRow.appendChild(settingsPanel);

  // Task table
  const tableWrap = document.createElement("div");
  tableWrap.style.cssText = `
    border: 1px solid rgba(255,255,255,0.10);
    border-radius: 12px;
    overflow: hidden;
  `;

  taskTableScrollEl = document.createElement("div");
  const fallbackMaxHeightPx = FALLBACK_TASK_HEADER_HEIGHT_PX + FALLBACK_TASK_ROW_HEIGHT_PX * MAX_VISIBLE_TASK_ROWS;
  taskTableScrollEl.style.cssText = `
    max-height: ${fallbackMaxHeightPx}px;
    overflow: auto;
    overscroll-behavior: contain;
  `;
  tableWrap.appendChild(taskTableScrollEl);

  const table = document.createElement("table");
  taskTableEl = table;
  table.style.cssText = "width: 100%; border-collapse: collapse; font-size: 12px;";

  const thead = document.createElement("thead");
  thead.style.cssText = `background: ${TASK_TABLE_HEADER_BG};`;
  const headRow = document.createElement("tr");
  const headers = ["时间", "Provider", "模型", "状态", "进度", "信息", "操作"];
  for (const h of headers) {
    const th = document.createElement("th");
    th.textContent = h;
    th.style.cssText =
      `position: sticky; top: 0; z-index: 2; background: ${TASK_TABLE_HEADER_BG}; text-align:left; padding: 10px 10px; border-bottom: 1px solid rgba(255,255,255,0.10);`;
    headRow.appendChild(th);
  }
  thead.appendChild(headRow);

  taskTableBodyEl = document.createElement("tbody");

  table.appendChild(thead);
  table.appendChild(taskTableBodyEl);
  taskTableScrollEl.appendChild(table);

  body.appendChild(settingsRow);
  body.appendChild(tableWrap);

  panelEl.appendChild(header);
  panelEl.appendChild(body);
  overlayEl.appendChild(panelEl);
  document.body.appendChild(overlayEl);

  overlayEl.addEventListener("click", (event) => {
    if (event.target === overlayEl) hideOverlay();
  });

  return overlayEl;
}

function setStatus(text) {
  if (statusEl) statusEl.textContent = text || "";
}

function clearTaskTable() {
  if (!taskTableBodyEl) return;
  taskTableBodyEl.innerHTML = "";
}

function applyTaskTableMaxHeight() {
  if (!taskTableScrollEl || !taskTableEl || !taskTableBodyEl) return;

  const headerRow = taskTableEl.querySelector("thead tr");
  const headerHeight = headerRow ? headerRow.getBoundingClientRect().height : FALLBACK_TASK_HEADER_HEIGHT_PX;

  const firstRow = taskTableBodyEl.querySelector("tr");
  const rowHeight = firstRow ? firstRow.getBoundingClientRect().height : FALLBACK_TASK_ROW_HEIGHT_PX;

  const maxHeight = Math.ceil(headerHeight + rowHeight * MAX_VISIBLE_TASK_ROWS);
  taskTableScrollEl.style.maxHeight = `${maxHeight}px`;
}

function appendCell(tr, content) {
  const td = document.createElement("td");
  td.style.cssText = "padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.06); vertical-align: top;";
  if (content instanceof HTMLElement) {
    td.appendChild(content);
  } else {
    td.textContent = content == null ? "" : String(content);
  }
  tr.appendChild(td);
}

function makeButton(label, onClick) {
  const btn = document.createElement("button");
  btn.textContent = label;
  btn.style.cssText = `
    padding: 5px 10px;
    border-radius: 10px;
    border: 1px solid rgba(255,255,255,0.18);
    background: rgba(255,255,255,0.06);
    color: inherit;
    cursor: pointer;
  `;
  btn.addEventListener("click", onClick);
  return btn;
}

function renderTasks(tasks) {
  clearTaskTable();
  if (!taskTableBodyEl) return;

  const rows = Array.isArray(tasks) ? tasks : [];
  if (!rows.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.style.cssText = "padding: 14px 10px; opacity: 0.7;";
    td.textContent = "暂无任务（执行视频节点后会自动记录 TaskID）";
    tr.appendChild(td);
    taskTableBodyEl.appendChild(tr);
    window.requestAnimationFrame(() => applyTaskTableMaxHeight());
    return;
  }

  for (const task of rows) {
    const tr = document.createElement("tr");
    const createdAt = formatTime(task.created_at);
    const provider = task.provider || "-";
    const model = task.model || "-";
    const status = statusLabel(task.status);
    const progress = typeof task.progress === "number" ? `${Math.round(task.progress)}%` : "-";
    const info = task.error || (task.video_url ? "已返回链接" : "");

    appendCell(tr, createdAt);
    appendCell(tr, provider);
    appendCell(tr, model);
    appendCell(tr, status);
    appendCell(tr, progress);

    const infoEl = document.createElement("div");
    infoEl.style.cssText = "max-width: 320px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; opacity: 0.92;";
    infoEl.title = info || "";
    infoEl.textContent = info || "";
    appendCell(tr, infoEl);

    const actions = document.createElement("div");
    actions.style.cssText = "display:flex; gap:8px; flex-wrap: wrap;";

    const taskId = String(task.id || "").trim();
    if (taskId) {
      actions.appendChild(
        makeButton("刷新", async () => {
          try {
            await postJson(API_REFRESH, { id: taskId });
            setStatus("✅ 已触发刷新");
            void fetchAndRender();
          } catch (err) {
            setStatus(`❌ 刷新失败：${err?.message || err}`);
          }
        })
      );
    }

    const viewUrl = buildViewUrl(task.local_file);
    if (viewUrl) {
      actions.appendChild(
        makeButton("本地打开", () => {
          // Windows 优先尝试打开资源管理器定位文件；失败回退到 /view 预览。
          if (!isWindowsPlatform()) {
            window.open(viewUrl, "_blank");
            return;
          }

          const popup = window.open("", "_blank");
          try {
            if (popup && popup.document) {
              popup.document.title = "打开本地文件...";
              popup.document.body.style.cssText =
                "margin:0; padding:12px; font-family:system-ui; background:#111; color:#ddd;";
              popup.document.body.textContent = "正在打开 Windows 资源管理器定位文件...";
            }
          } catch (_) {
            // ignore
          }

          void (async () => {
            const opened = await tryOpenInWindowsExplorer({ taskId, localFile: task.local_file });
            if (opened) {
              try {
                if (popup && !popup.closed) popup.close();
              } catch (_) {
                // ignore
              }
              setStatus("✅ 已在资源管理器定位文件");
              return;
            }

            if (popup && !popup.closed) {
              try {
                popup.location.href = viewUrl;
              } catch (_) {
                window.open(viewUrl, "_blank");
              }
            } else {
              window.open(viewUrl, "_blank");
            }
          })();
        })
      );
    }

    if (task.video_url) {
      actions.appendChild(
        makeButton("打开链接", () => {
          window.open(String(task.video_url), "_blank");
        })
      );
    }

    appendCell(tr, actions);
    taskTableBodyEl.appendChild(tr);
  }

  // 依赖实际渲染后的行高，确保“最多 6 行”且可滚轮滚动查看更多记录。
  window.requestAnimationFrame(() => applyTaskTableMaxHeight());
}

async function fetchAndRender() {
  if (isFetching) return;
  isFetching = true;
  try {
    const payload = await fetchJson(API_LIST, { method: "GET" });
    const data = payload?.data || {};
    const tasks = data.tasks || [];
    const settings = data.settings || {};
    if (autoDownloadCheckbox && typeof settings.auto_download === "boolean") {
      autoDownloadCheckbox.checked = settings.auto_download;
    }
    renderTasks(tasks);

    // Update Badge Logic
    try {
      // 1. Get all success task IDs
      const successIds = tasks
        .filter(t => (t.status || "").toLowerCase() === "success")
        .map(t => String(t.id || "").trim())
        .filter(id => id);

      const isPanelOpen = overlayEl && overlayEl.style.display !== "none";

      if (isPanelOpen) {
        // Panel is open: user "sees" everything -> Read All
        const oldRead = readStorageJson(STORAGE_READ_TASKS) || [];
        const newSet = new Set([...oldRead, ...successIds]);
        writeStorageJson(STORAGE_READ_TASKS, Array.from(newSet));
        updateBadge(0);
      } else {
        // Panel closed: Calculate unread
        const readRaw = readStorageJson(STORAGE_READ_TASKS);
        const readSet = new Set(Array.isArray(readRaw) ? readRaw : []);

        let unreadCount = 0;
        for (const id of successIds) {
          if (!readSet.has(id)) {
            unreadCount++;
          }
        }
        updateBadge(unreadCount);
      }
    } catch (_) {
      // ignore badge errors
    }

    setStatus("✅ 已刷新");
  } catch (err) {
    setStatus(`❌ 获取任务失败：${err?.message || err}`);
  } finally {
    isFetching = false;
  }
}

function updateBadge(count) {
  if (!badgeEl) return;
  const num = parseInt(count, 10);
  if (num > 0) {
    badgeEl.textContent = num > 99 ? "99+" : String(num);
    badgeEl.style.display = "flex";
  } else {
    badgeEl.style.display = "none";
  }
}

function showOverlay() {
  ensureOverlay();
  overlayEl.style.display = "flex";

  // Mark all current success tasks as read
  // We need to fetch latest to know what serves as "read"
  // But strictly, we can just read from the last fetch in memory if we had it,
  // OR we just wait for the next fetch to clear it?
  // Better: When opening, we assume user WILL see everything.
  // So we assume the badge count becomes 0 immediately.
  updateBadge(0);

  // But we must persist this "read" state so next poll doesn't revive the badge.
  // We'll do this effectively by:
  // 1. Fetching immediately (already called below)
  // 2. IN THE RENDER loop? No, render loop sets badge based on storage.
  // So we need to update storage.
  // Ideally, we wait for fetch to return, then mark those IDs as read.
  // Let's do a "fire and forget" mark-as-read in fetchAndRender?
  // No, fetchAndRender is called periodically in background too.

  // Implementation:
  // We trigger a fetch. When it returns, if the overlay is visible, we update the read-set.
  // To avoid complex callback chains, let's just piggyback on the fact that 
  // we are about to call fetchAndRender().
  // Actually, let's just modify fetchAndRender to specific behavior:
  // If overlay is OPEN, then treat all fetched success tasks as read immediately.

  // Revised approach for fetchAndRender logic (see next block update if I could, but I can't change previous block dynamically here)
  // So I will just implement a naive "mark all currently *known* (from last render?)" or just
  // rely on the next fetch.
  // Simpler: Just set a flag or update storage logic in fetchAndRender.

  // Let's do this:
  // We iterate current DOM or memory? We don't have tasks in memory easily exposed.
  // Let's just rely on the `fetchAndRender` call inside showOverlay to handle it?
  // No, `fetchAndRender` is generic.

  // OK, let's look at `fetchAndRender` again. It calls `renderTasks(tasks)`.
  // We can modify `renderTasks` or just `fetchAndRender` to return the tasks.
  // But `fetchAndRender` is async.

  // Let's just define that when overlay is opened, we *will* read the tasks.
  // We can add a one-off "mark read" action.

  // Strategy:
  // Inside showOverlay, we set a global flag `isPanelOpen = true` (we can infer from `overlayEl.style.display`).
  // In `fetchAndRender`, if `isPanelOpen` is true, we [Update Storage with All Success IDs] AND [Set Badge 0].
  // Else, we [Calc Unread] AND [Set Badge count].

  // Yes, that is cleaner. I will need to update the `fetchAndRender` chunk logic slightly? 
  // I already wrote the chunk for `fetchAndRender` above... 
  // Wait, I can't edit the previous chunk in this tool call. 
  // LIMITATION: 'fetchAndRender` chunk above does NOT have the "if panel open" logic.
  // It only has "Calc unread".

  // Logic fix:
  // I will update the `fetchAndRender` replacement above to include checking `overlayEl.style.display`.
  // WAIT, I cannot change the *previous* chunk argument in the *current* tool call generation if I already outputted it?
  // Actually I am generating the arguments right now.
  // So I should fix the `fetchAndRender` chunk in THIS tool call before sending it.

  // RE-PLANNING FETCH CHUNK (Mental Draft):
  /*
    // ... inside fetchAndRender ...
    renderTasks(tasks);

    // Update Badge Logic
    try {
        const successIds = tasks.filter(...).map(...)
        
        // If panel is open, mark all as read immediately
        if (overlayEl && overlayEl.style.display !== "none") {
            writeStorageJson(STORAGE_READ_TASKS, successIds);
            updateBadge(0);
        } else {
            // Panel closed, calc unread
            const readRaw = readStorageJson(STORAGE_READ_TASKS);
            const readSet = new Set(...);
            let unread = 0;
            // ... count ...
            updateBadge(unread);
        }
    } ...
  */

  // This looks correct. I will apply this logic in the `fetchAndRender` chunk below.
  // And `showOverlay` just needs to do the standard show stuff.

  ensureOverlay();
  overlayEl.style.display = "flex";
  // 展示后再应用位置（需要先完成 layout 才能 clamp）
  window.requestAnimationFrame(() => {
    try {
      applyStoredFixedPosition(panelEl, STORAGE_PANEL_POS);
    } catch (_) {
      // ignore
    }
  });
  void fetchAndRender();
  if (!pollTimer) {
    pollTimer = window.setInterval(() => void fetchAndRender(), POLL_INTERVAL_MS);
  }
}

function hideOverlay() {
  if (overlayEl) overlayEl.style.display = "none";
  if (pollTimer) {
    window.clearInterval(pollTimer);
    pollTimer = null;
  }
}

function ensureFloatingButton() {
  const btn = document.createElement("button");
  btn.textContent = "🎬 心宝❤任务中心";
  btn.title = "心宝❤任务中心";
  btn.style.cssText = `
    position: fixed;
    right: 18px;
    bottom: 18px;
    z-index: 9999;
    padding: 10px 12px;
    border-radius: 999px;
    border: 1px solid rgba(255,255,255,0.22);
    background: rgba(31,31,31,0.85);
    color: #f2f2f2;
    cursor: pointer;
    box-shadow: 0 10px 34px rgba(0,0,0,0.35);
    user-select: none;
    touch-action: none;
    display: flex;
    align-items: center;
    justify-content: center;
  `;
  floatingBtnEl = btn;
  document.body.appendChild(btn);

  // Badge
  badgeEl = document.createElement("div");
  badgeEl.style.cssText = `
    position: absolute;
    top: -5px;
    right: -5px;
    background-color: #f44336;
    color: white;
    border-radius: 10px;
    padding: 2px 6px;
    font-size: 10px;
    font-weight: bold;
    min-width: 14px;
    text-align: center;
    box-shadow: 0 2px 4px rgba(0,0,0,0.2);
    display: none; /* hidden by default */
    z-index: 10001;
  `;
  btn.appendChild(badgeEl);

  // 恢复用户上次拖动位置
  window.requestAnimationFrame(() => {
    try {
      applyStoredFixedPosition(btn, STORAGE_BUTTON_POS);
    } catch (_) {
      // ignore
    }
  });

  // 点击打开 + 可拖动（拖动后记住位置）
  (() => {
    let dragging = false;
    let pointerId = null;
    let offsetX = 0;
    let offsetY = 0;
    let startLeft = 0;
    let startTop = 0;
    let moved = false;

    const finalize = (cancelled = false) => {
      if (!dragging) return;
      dragging = false;
      pointerId = null;
      if (moved) {
        const rect = btn.getBoundingClientRect();
        writeStorageJson(STORAGE_BUTTON_POS, { left: Math.round(rect.left), top: Math.round(rect.top) });
        return;
      }
      if (!cancelled) showOverlay();
    };

    btn.addEventListener("pointerdown", (event) => {
      if (typeof event.button === "number" && event.button !== 0) return;
      const rect = btn.getBoundingClientRect();
      dragging = true;
      moved = false;
      pointerId = event.pointerId;
      offsetX = event.clientX - rect.left;
      offsetY = event.clientY - rect.top;
      startLeft = rect.left;
      startTop = rect.top;
      setFixedPosition(btn, rect.left, rect.top);
      try {
        btn.setPointerCapture(pointerId);
      } catch (_) {
        // ignore
      }
    });

    btn.addEventListener("pointermove", (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      const nextLeft = event.clientX - offsetX;
      const nextTop = event.clientY - offsetY;
      const clamped = clampToViewport(btn, nextLeft, nextTop);
      setFixedPosition(btn, clamped.left, clamped.top);
      const dx = clamped.left - startLeft;
      const dy = clamped.top - startTop;
      if (!moved && Math.abs(dx) + Math.abs(dy) >= DRAG_THRESHOLD_PX) moved = true;
    });

    btn.addEventListener("pointerup", (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      try {
        btn.releasePointerCapture(pointerId);
      } catch (_) {
        // ignore
      }
      finalize(false);
    });

    btn.addEventListener("pointercancel", (event) => {
      if (!dragging || event.pointerId !== pointerId) return;
      finalize(true);
    });
  })();
}

app.registerExtension({
  name: EXTENSION,
  setup() {
    const SETTING_ID = `${EXTENSION}.enabled`;

    // 默认启用
    // 默认启用


    app.ui.settings.addSetting({
      id: SETTING_ID,
      name: "🎬 启用心宝❤任务中心",
      type: "boolean",
      defaultValue: true,
      onChange: (value) => {
        if (floatingBtnEl) {
          floatingBtnEl.style.display = value ? "flex" : "none";
        }
      },
    });

    // 延迟到 DOM 就绪后再挂载悬浮按钮
    const mount = () => {
      try {
        ensureFloatingButton();
        // 初始化显隐状态
        if (floatingBtnEl) {
          const isEnabled = app.ui.settings.getSettingValue(SETTING_ID);
          floatingBtnEl.style.display = isEnabled ? "flex" : "none";
        }
      } catch (e) {
        console.warn(`[${EXTENSION}] 悬浮按钮挂载失败`, e);
      }
    };
    if (document.readyState === "complete" || document.readyState === "interactive") {
      setTimeout(mount, 0);
    } else {
      window.addEventListener("DOMContentLoaded", () => setTimeout(mount, 0), { once: true });
    }
  },
});
