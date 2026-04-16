import { app } from "/scripts/app.js";
import { api } from "/scripts/api.js";

// ─────────────────────────────────────────────────────────────
// 心宝任务中心 — Phase 1: 卡片式布局 + Tab/状态过滤/排序/搜索
// ─────────────────────────────────────────────────────────────

const EXTENSION = "banana.videoTaskManager";

// ── API Endpoints ──
const API_LIST = "/banana/video_tasks";
const API_REFRESH = "/banana/video_tasks/refresh";
const API_DELETE = "/banana/video_tasks/delete";
const API_KEY = "/banana/video_tasks/key";
const API_SETTINGS = "/banana/video_tasks/settings";
const API_OPEN_LOCAL = "/banana/video_tasks/open_local";
const API_PUSH_TO_CANVAS = "/banana/video_tasks/push_to_canvas";
const API_DOWNLOAD = "/banana/video_tasks/download";
const API_BROWSE_DIR = "/banana/video_tasks/browse_dir";

// ── Constants ──
const POLL_ACTIVE_MS = 5000;     // 面板打开 + Tab 可见
const POLL_IDLE_MS = 15000;      // 面板关闭 + Tab 可见
const POLL_BACKGROUND_MS = 30000; // Tab 后台（保障自动下载）
const DRAG_THRESHOLD_PX = 4;
const VIEWPORT_MARGIN_PX = 8;

// ── localStorage Keys ──
const LS_BUTTON_POS = "banana_tc_buttonPos";
const LS_PANEL_POS = "banana_tc_panelPos";
const LS_READ_TASKS = "banana_tc_read_tasks";
const LS_LAST_DOWNLOAD_DIR = "banana_tc_last_download_dir";
const LS_PINNED_TASKS = "banana_tc_pinned_tasks";

// ── State ──
const state = {
  tasks: [],
  selectedIds: new Set(), // Phase 2: multi-select
  activeTab: "all", // "all"|"image"|"video"
  statusFilter: "all", // "all"|"processing"|"success"|"failed"|"pending"
  sortMode: "newest", // "newest"|"oldest"|"status"
  autoRefresh: true,
  refreshTimer: null,
  searchQuery: "",
};

// ── DOM references ──
let floatingBtnEl = null;
let badgeEl = null;
let overlayEl = null;
let panelEl = null;
let isFetching = false;
let autoDownloadCheckbox = null;

// ── Lazy DOM refs (built once in ensureOverlay) ──
let statusTextEl = null;
let settingsDrawerEl = null;
let cardsContainerEl = null;
let tabEls = {};
let statusChipEls = {};
let footerLeftEl = null;
let footerKeyEl = null;
let searchInputEl = null;
let sortSelectEl = null;
let autoRefreshCheckboxEl = null;
let batchToolbarEl = null; // Phase 2 placeholder

// ── Incremental render state ──
const cardElementMap = new Map(); // taskId → { element, snapshot }
let lastFilterKey = "";

// ─────────────────────────────────────────────────────────────
// CSS Injection
// ─────────────────────────────────────────────────────────────
function injectStyles() {
  if (document.getElementById("banana-tc-styles")) return;
  const style = document.createElement("style");
  style.id = "banana-tc-styles";
  style.textContent = `
/* ── Keyframes ── */
@keyframes banana-tc-spin {
  to { transform: rotate(360deg); }
}
@keyframes banana-tc-fadeIn {
  from { opacity: 0; transform: scale(0.96); }
  to   { opacity: 1; transform: scale(1); }
}
@keyframes banana-tc-shimmer {
  0% { background-position: -200% 0; }
  100% { background-position: 200% 0; }
}
@keyframes banana-tc-fadeSlideIn {
  from { opacity: 0; transform: translateY(10px) scale(0.97); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}
@keyframes banana-tc-progressPulse {
  0% { left: -30%; }
  100% { left: 130%; }
}
@keyframes banana-tc-breathe {
  0%, 100% { box-shadow: 0 10px 34px rgba(0,0,0,0.40), 0 0 0 0 rgba(226,169,59,0.35); }
  50% { box-shadow: 0 10px 34px rgba(0,0,0,0.40), 0 0 0 10px rgba(226,169,59,0); }
}

/* ── Design Tokens ── */
:root {
  --tc-bg-deep: #0a0a0f;
  --tc-bg-deep-alt: #06060c;
  --tc-bg-base: #0d0d16;
  --tc-bg-raised: #12121f;
  --tc-bg-hover: #1a1a2e;
  --tc-border: #1e1e35;
  --tc-border-active: #3a3a55;

  --tc-accent: #e2a93b;
  --tc-accent-dim: #e2a93b22;
  --tc-accent-muted: #e2a93b44;
  --tc-accent-focus: #e2a93b88;
  --tc-video: #c06070;

  --tc-text-heading: #f2f2f2;
  --tc-text-primary: #e0e0e0;
  --tc-text-secondary: #a0a0b0;
  --tc-text-label: #d0d0e0;
  --tc-text-muted: #666;
  --tc-text-dim: #555;

  --tc-status-pending: #6b8aaf;
  --tc-status-processing: #d49a3b;
  --tc-status-success: #5aad70;
  --tc-status-failed: #c45454;

  --tc-r-sm: 6px;
  --tc-r-md: 8px;
  --tc-r-lg: 12px;
  --tc-r-xl: 14px;
  --tc-r-pill: 999px;
}

/* ── Overlay ── */
.banana-tc-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.55);
  display: none;
  align-items: center;
  justify-content: center;
  z-index: 10000;
}
.banana-tc-overlay.open {
  display: flex;
}

/* ── Panel ── */
.banana-tc-panel {
  position: fixed;
  left: 50%; top: 50%;
  transform: translate(-50%, -50%);
  width: 960px;
  max-width: calc(100vw - 32px);
  max-height: calc(100vh - 32px);
  overflow: hidden;
  background: var(--tc-bg-deep);
  border: 1px solid var(--tc-border);
  border-radius: 14px;
  box-shadow: 0 18px 60px rgba(0,0,0,0.50);
  color: var(--tc-text-primary);
  display: flex;
  flex-direction: column;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 13px;
  animation: banana-tc-fadeIn 0.15s ease-out;
}

/* ── Header ── */
.banana-tc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 20px;
  border-bottom: 1px solid var(--tc-border);
  cursor: move;
  user-select: none;
  touch-action: none;
  flex-shrink: 0;
}
.banana-tc-header-title {
  font-size: 16px;
  font-weight: 700;
  color: var(--tc-text-heading);
}
.banana-tc-header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}

/* ── Batch Toolbar (Phase 2 placeholder) ── */
.banana-tc-batch-toolbar {
  display: none;
  padding: 8px 20px;
  border-bottom: 1px solid var(--tc-border);
  background: var(--tc-bg-base);
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}
.banana-tc-batch-toolbar.visible {
  display: flex;
}

/* ── Toolbar ── */
.banana-tc-toolbar {
  padding: 12px 20px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex-shrink: 0;
  /* RISK: Glassmorphism toolbar */
  background: rgba(10,10,15,0.72);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  position: relative;
  z-index: 2;
}
.banana-tc-toolbar-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.banana-tc-tabs {
  display: flex;
  gap: 4px;
}
.banana-tc-tab {
  background: var(--tc-bg-raised);
  color: var(--tc-text-secondary);
  padding: 4px 12px;
  border-radius: 8px;
  font-size: 12px;
  cursor: pointer;
  border: 1px solid var(--tc-border);
  transition: all 0.15s;
  white-space: nowrap;
}
.banana-tc-tab:hover {
  background: var(--tc-bg-hover);
  color: var(--tc-text-label);
}
.banana-tc-tab:focus-visible {
  outline: 2px solid #e2a93b88;
  outline-offset: 1px;
}
.banana-tc-tab.active {
  background: linear-gradient(135deg, #e2a93b22, #e2a93b11);
  color: #e2a93b;
  border-color: #e2a93b44;
}
.banana-tc-tab .banana-tc-badge-count {
  font-weight: 600;
  margin-left: 4px;
}

.banana-tc-status-filters {
  display: flex;
  gap: 4px;
}
.banana-tc-status-chip {
  background: var(--tc-bg-raised);
  color: var(--tc-text-secondary);
  padding: 4px 12px;
  border-radius: 6px;
  font-size: 11px;
  cursor: pointer;
  border: 1px solid var(--tc-border);
  transition: all 0.15s;
  white-space: nowrap;
}
.banana-tc-status-chip:hover {
  background: var(--tc-bg-hover);
  color: var(--tc-text-label);
}
.banana-tc-status-chip:focus-visible {
  outline: 2px solid #e2a93b88;
  outline-offset: 1px;
}
.banana-tc-status-chip.active {
  background: var(--tc-border);
  color: #e0e0f0;
  border-color: var(--tc-border-active);
}
.banana-tc-status-chip .banana-tc-chip-count {
  opacity: 0.7;
  margin-left: 4px;
}

.banana-tc-search {
  background: var(--tc-bg-raised);
  border: 1px solid var(--tc-border);
  color: var(--tc-text-primary);
  padding: 4px 12px;
  border-radius: 8px;
  font-size: 12px;
  width: 160px;
  outline: none;
  transition: border-color 0.15s;
}
.banana-tc-search:focus {
  border-color: #e2a93b66;
}
.banana-tc-search::placeholder {
  color: #555;
}

.banana-tc-sort {
  background: var(--tc-bg-raised);
  border: 1px solid var(--tc-border);
  color: var(--tc-text-primary);
  padding: 4px 8px;
  border-radius: 8px;
  font-size: 11px;
  outline: none;
  cursor: pointer;
}
.banana-tc-sort option {
  background: var(--tc-bg-raised);
  color: var(--tc-text-primary);
}

.banana-tc-auto-refresh {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: #888;
  cursor: pointer;
  white-space: nowrap;
}
.banana-tc-auto-refresh input {
  cursor: pointer;
  accent-color: #e2a93b;
}

/* ── Cards Container ── */
.banana-tc-cards-scroll {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 16px 20px;
  overscroll-behavior: contain;
}
.banana-tc-cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 12px;
}
.banana-tc-empty {
  width: 100%;
  text-align: center;
  color: #555;
  padding: 48px 0;
  font-size: 13px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  animation: banana-tc-fadeIn 0.3s ease-out;
}
.banana-tc-empty-icon {
  font-size: 32px;
  opacity: 0.3;
  margin-bottom: 4px;
}
.banana-tc-empty-illustration {
  width: 100px;
  height: 100px;
  opacity: 0.7;
  margin-bottom: 8px;
}
.banana-tc-empty-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--tc-text-secondary);
}
.banana-tc-empty-hint {
  font-size: 11px;
  color: #444;
  max-width: 220px;
  line-height: 1.5;
}

/* SAFE: Skeleton shimmer card */
.banana-tc-skeleton {
  box-sizing: border-box;
  background: var(--tc-bg-raised);
  border-radius: 12px;
  border: 1px solid var(--tc-border);
  overflow: hidden;
}
.banana-tc-skeleton-preview {
  height: 130px;
  background: linear-gradient(90deg, #12121f 25%, #1a1a2e 50%, #12121f 75%);
  background-size: 200% 100%;
  animation: banana-tc-shimmer 1.8s ease-in-out infinite;
}
.banana-tc-skeleton-info {
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.banana-tc-skeleton-line {
  height: 10px;
  border-radius: 4px;
  background: linear-gradient(90deg, #1a1a2e 25%, #22223a 50%, #1a1a2e 75%);
  background-size: 200% 100%;
  animation: banana-tc-shimmer 1.8s ease-in-out infinite;
}
.banana-tc-skeleton-line.short { width: 55%; }
.banana-tc-skeleton-line.medium { width: 80%; }

/* ── Card ── */
.banana-tc-card {
  box-sizing: border-box;
  background: var(--tc-bg-raised);
  border-radius: 12px;
  border: 1px solid var(--tc-border);
  overflow: hidden;
  transition: transform 0.2s ease, box-shadow 0.2s ease, border-color 0.2s ease;
  cursor: pointer;
  position: relative;
}
/* Stagger entrance: animation applied via JS only on filter-changed rebuild */
.banana-tc-card.tc-enter {
  animation: banana-tc-fadeSlideIn 0.35s ease-out both;
}
/* RISK: Gradient surfaces — image cards warm gold, video cards rose tint */
.banana-tc-card.image-type {
  background: linear-gradient(165deg, #16140f 0%, var(--tc-bg-raised) 55%);
}
.banana-tc-card.video-type {
  background: linear-gradient(165deg, #18121a 0%, var(--tc-bg-raised) 55%);
}
.banana-tc-card:hover {
  transform: translateY(-3px);
  box-shadow: 0 12px 32px rgba(0,0,0,0.35);
}
/* RISK: Ambient glow on hover — box-shadow approach (overflow:hidden safe) */
.banana-tc-card.image-type:hover {
  border-color: rgba(226,169,59,0.25);
  box-shadow: 0 12px 32px rgba(0,0,0,0.35), 0 0 12px rgba(226,169,59,0.10);
}
.banana-tc-card.video-type:hover {
  border-color: rgba(192,96,112,0.25);
  box-shadow: 0 12px 32px rgba(0,0,0,0.35), 0 0 12px rgba(192,96,112,0.10);
}
.banana-tc-card.failed {
  border-color: #3b1c1c;
}

/* Card preview area */
.banana-tc-card-preview {
  height: 130px;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}
.banana-tc-card-preview.pending,
.banana-tc-card-preview.processing-image,
.banana-tc-card-preview.processing-video {
  background: var(--tc-bg-hover);
}
.banana-tc-card-preview.success-image,
.banana-tc-card-preview.success-video {
  background: var(--tc-bg-raised);
}
.banana-tc-card-preview.failed {
  background: #1e1418;
}

/* Type tag (top-left) */
.banana-tc-type-tag {
  position: absolute;
  top: 8px;
  left: 8px;
  padding: 2px 8px;
  border-radius: 5px;
  font-size: 10px;
  font-weight: 600;
  z-index: 1;
}
.banana-tc-type-tag.image {
  background: #e2a93b33;
  color: #e2a93b;
}
.banana-tc-type-tag.video {
  background: #c0607033;
  color: #c06070;
}

/* Status tag (top-right) */
.banana-tc-status-tag {
  position: absolute;
  top: 8px;
  right: 8px;
  padding: 2px 8px;
  border-radius: 5px;
  font-size: 10px;
  z-index: 1;
}
.banana-tc-status-tag.pending {
  background: #6b8aaf22;
  color: #6b8aaf;
}
.banana-tc-status-tag.processing {
  background: #d49a3b22;
  color: #d49a3b;
}
.banana-tc-status-tag.success {
  background: #5aad7022;
  color: #5aad70;
}
.banana-tc-status-tag.failed {
  background: #c4545422;
  color: #c45454;
}

/* Image count badge (bottom-right) */
.banana-tc-image-count {
  position: absolute;
  bottom: 8px;
  right: 8px;
  background: #000000aa;
  color: white;
  padding: 2px 8px;
  border-radius: 5px;
  font-size: 10px;
  z-index: 1;
}

/* Spinner */
.banana-tc-spinner {
  width: 28px;
  height: 28px;
  border: 3px solid #e2a93b;
  border-top-color: transparent;
  border-radius: 50%;
  animation: banana-tc-spin 1s linear infinite;
}
.banana-tc-spinner.video {
  border-color: #c06070;
  border-top-color: transparent;
}

/* Pending text */
.banana-tc-pending-text {
  color: #555;
  font-size: 13px;
}

/* Warning icon */
.banana-tc-fail-icon {
  font-size: 28px;
  opacity: 0.5;
}

/* Play icon */
.banana-tc-play-icon {
  font-size: 40px;
  opacity: 0.4;
}

/* Thumbnail */
.banana-tc-thumbnail {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* Card info area */
.banana-tc-card-info {
  padding: 12px;
}
.banana-tc-card-meta {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 5px;
}
.banana-tc-card-model {
  color: var(--tc-text-primary);
  font-size: 12px;
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 130px;
}
.banana-tc-card-time {
  color: #555;
  font-size: 10px;
  white-space: nowrap;
  flex-shrink: 0;
}

/* Progress bar */
.banana-tc-progress {
  background: var(--tc-bg-hover);
  height: 3px;
  border-radius: 2px;
  overflow: hidden;
  margin-bottom: 8px;
}
.banana-tc-progress-fill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s ease;
}
.banana-tc-progress-fill.image {
  background: linear-gradient(90deg, #e2a93b, #d4a042);
  position: relative;
  overflow: hidden;
}
.banana-tc-progress-fill.video {
  background: linear-gradient(90deg, #c06070, #d07080);
  position: relative;
  overflow: hidden;
}
/* SAFE: Progress pulse sweep */
.banana-tc-progress-fill::after {
  content: '';
  position: absolute;
  top: 0;
  left: -30%;
  width: 30%;
  height: 100%;
  background: linear-gradient(90deg, transparent, rgba(255,255,255,0.35), transparent);
  animation: banana-tc-progressPulse 2s ease-in-out infinite;
}

/* Prompt summary */
.banana-tc-prompt {
  color: #777;
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── (expand area removed — actions moved to detail modal) ── */

/* ── Buttons ── */
.banana-tc-btn {
  padding: 4px 12px;
  border-radius: 8px;
  border: 1px solid rgba(255,255,255,0.15);
  background: rgba(255,255,255,0.05);
  color: #ccc;
  cursor: pointer;
  font-size: 11px;
  transition: background 0.1s, color 0.1s;
  white-space: nowrap;
}
.banana-tc-btn:hover {
  background: rgba(255,255,255,0.10);
  color: #fff;
}
.banana-tc-btn:focus-visible {
  outline: 2px solid #e2a93b88;
  outline-offset: 2px;
}
.banana-tc-btn.danger {
  border-color: #ef444444;
  color: #ef4444;
  background: #ef444411;
}
.banana-tc-btn.danger:hover {
  background: #ef444422;
}
.banana-tc-btn.primary {
  border-color: #e2a93b44;
  color: #e2a93b;
  background: #e2a93b11;
}
.banana-tc-btn.primary:hover {
  background: #e2a93b22;
}
.banana-tc-btn.header {
  padding: 8px 16px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.18);
  background: transparent;
  color: #ccc;
  font-size: 12px;
}
.banana-tc-btn.header:hover {
  background: rgba(255,255,255,0.06);
  color: #fff;
}

/* ── Footer ── */
.banana-tc-footer {
  padding: 12px 20px;
  background: var(--tc-bg-base);
  border-top: 1px solid var(--tc-border);
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-shrink: 0;
  flex-wrap: wrap;
  gap: 8px;
}
.banana-tc-footer-left {
  color: #666;
  font-size: 11px;
}
.banana-tc-footer-right {
  display: flex;
  gap: 12px;
  align-items: center;
}
.banana-tc-footer-key {
  color: #555;
  font-size: 11px;
}
.banana-tc-footer-settings-btn {
  background: transparent;
  color: #e2a93b;
  border: none;
  font-size: 11px;
  cursor: pointer;
  padding: 0;
}
.banana-tc-footer-settings-btn:hover {
  text-decoration: underline;
}

/* ── Settings Drawer ── */
.banana-tc-settings-drawer {
  display: none;
  padding: 16px 20px;
  border-bottom: 1px solid var(--tc-border);
  background: var(--tc-bg-base);
  flex-shrink: 0;
}
.banana-tc-settings-drawer.open {
  display: block;
}
.banana-tc-settings-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.banana-tc-settings-section {
  border: 1px solid var(--tc-border);
  border-radius: 12px;
  padding: 12px;
  background: rgba(255,255,255,0.02);
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.banana-tc-settings-title {
  font-weight: 600;
  font-size: 13px;
  color: var(--tc-text-label);
}
.banana-tc-key-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.banana-tc-key-input {
  flex: 1;
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid var(--tc-border);
  background: rgba(0,0,0,0.3);
  color: var(--tc-text-primary);
  outline: none;
  font-size: 12px;
  transition: border-color 0.15s;
}
.banana-tc-key-input:focus {
  border-color: #e2a93b66;
}
.banana-tc-auto-dl-label {
  display: flex;
  gap: 8px;
  align-items: center;
  cursor: pointer;
  font-size: 12px;
  color: #aaa;
}
.banana-tc-auto-dl-label input {
  accent-color: #e2a93b;
  cursor: pointer;
}
.banana-tc-status-text {
  font-size: 11px;
  color: rgba(255,255,255,0.55);
  min-height: 16px;
}

/* ── Floating Button ── */
.banana-tc-float-btn {
  position: fixed;
  right: 18px;
  bottom: 18px;
  z-index: 9999;
  padding: 12px 16px;
  border-radius: 999px;
  border: 1px solid rgba(255,255,255,0.18);
  background: rgba(10,10,15,0.92);
  color: var(--tc-text-primary);
  cursor: pointer;
  box-shadow: 0 10px 34px rgba(0,0,0,0.40);
  user-select: none;
  touch-action: none;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  font-size: 13px;
  transition: box-shadow 0.15s, border-color 0.2s;
}
.banana-tc-float-btn:hover {
  box-shadow: 0 12px 40px rgba(0,0,0,0.50);
  border-color: var(--tc-accent-muted);
}
.banana-tc-float-btn:hover > svg:first-child {
  color: var(--tc-accent);
}
/* SAFE: Breathing pulse when tasks are active */
.banana-tc-float-btn.has-active {
  animation: banana-tc-breathe 2.5s ease-in-out infinite;
}
.banana-tc-float-btn.has-active > svg:first-child {
  color: var(--tc-accent);
}
.banana-tc-float-badge {
  position: absolute;
  top: -5px;
  right: -5px;
  background-color: #ef4444;
  color: white;
  border-radius: 10px;
  padding: 2px 8px;
  font-size: 10px;
  font-weight: bold;
  min-width: 14px;
  text-align: center;
  box-shadow: 0 2px 4px rgba(0,0,0,0.3);
  display: none;
  z-index: 10001;
}

/* Scrollbar styling */
.banana-tc-cards-scroll::-webkit-scrollbar {
  width: 6px;
}
.banana-tc-cards-scroll::-webkit-scrollbar-track {
  background: transparent;
}
.banana-tc-cards-scroll::-webkit-scrollbar-thumb {
  background: #2a2a3e;
  border-radius: 3px;
}
.banana-tc-cards-scroll::-webkit-scrollbar-thumb:hover {
  background: var(--tc-border-active);
}

/* ── Phase 2: Multi-select checkbox ── */
.banana-tc-card-checkbox {
  position: absolute;
  top: 8px;
  left: 8px;
  z-index: 2;
  width: 18px;
  height: 18px;
  accent-color: #e2a93b;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;
}
.banana-tc-card:hover .banana-tc-card-checkbox,
.banana-tc-card-checkbox.checked {
  opacity: 1;
}
.banana-tc-card.selected {
  border-color: #e2a93b66;
  box-shadow: 0 0 0 1px #e2a93b33;
}
/* Pinned card indicator */
.banana-tc-card.pinned {
  border-color: rgba(226,169,59,0.3);
}
.banana-tc-card.pinned::after {
  content: '\u{1F4CC}';
  position: absolute;
  top: -2px;
  right: -2px;
  font-size: 12px;
  z-index: 4;
  filter: drop-shadow(0 1px 2px rgba(0,0,0,0.5));
}

/* shift type-tag when checkbox visible */
.banana-tc-card:hover .banana-tc-type-tag,
.banana-tc-card.selected .banana-tc-type-tag {
  left: 30px;
}

/* ── Toast ── */
.banana-tc-toast {
  position: fixed;
  bottom: 80px;
  right: 30px;
  z-index: 100010;
  padding: 10px 20px;
  border-radius: 8px;
  font-size: 13px;
  color: white;
  animation: banana-tc-fadeInOut 3s forwards;
  pointer-events: none;
}
.banana-tc-toast.success {
  background: #5aad70;
}
.banana-tc-toast.error {
  background: #c45454;
}

/* ── Phase 2: Batch toolbar contents ── */
.banana-tc-batch-toolbar .banana-tc-batch-count {
  color: #e2a93b;
  font-size: 12px;
  font-weight: 600;
  margin-right: 4px;
}
.banana-tc-batch-toolbar .banana-tc-btn {
  padding: 4px 12px;
  font-size: 11px;
}

/* ── Context Menu ── */
.banana-tc-ctx-menu {
  position: fixed;
  z-index: 100010;
  min-width: 160px;
  background: rgba(13,13,22,0.96);
  backdrop-filter: blur(16px);
  -webkit-backdrop-filter: blur(16px);
  border: 1px solid var(--tc-border-active);
  border-radius: 10px;
  padding: 4px 0;
  box-shadow: 0 12px 40px rgba(0,0,0,0.5);
  animation: banana-tc-fadeIn 0.1s ease-out;
}
.banana-tc-ctx-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 14px;
  font-size: 12px;
  color: var(--tc-text-primary);
  cursor: pointer;
  transition: background 0.1s;
  white-space: nowrap;
}
.banana-tc-ctx-item:hover {
  background: var(--tc-bg-hover);
}
.banana-tc-ctx-item.danger {
  color: #ef4444;
}
.banana-tc-ctx-item.danger:hover {
  background: #ef444418;
}
.banana-tc-ctx-sep {
  height: 1px;
  background: var(--tc-border);
  margin: 4px 8px;
}

/* ── Card hover quick actions ── */
.banana-tc-card-actions {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  display: flex;
  justify-content: center;
  gap: 6px;
  padding: 8px;
  background: linear-gradient(to top, rgba(0,0,0,0.7) 0%, transparent 100%);
  opacity: 0;
  transform: translateY(4px);
  transition: opacity 0.2s, transform 0.2s;
  z-index: 2;
  pointer-events: none;
}
.banana-tc-card:hover .banana-tc-card-actions {
  opacity: 1;
  transform: translateY(0);
  pointer-events: auto;
}
.banana-tc-card-action-btn {
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid rgba(255,255,255,0.2);
  background: rgba(0,0,0,0.5);
  backdrop-filter: blur(8px);
  color: white;
  font-size: 10px;
  cursor: pointer;
  transition: background 0.15s;
  white-space: nowrap;
}
.banana-tc-card-action-btn:hover {
  background: rgba(255,255,255,0.15);
}
.banana-tc-card-action-btn.primary {
  border-color: rgba(226,169,59,0.4);
  color: #e2a93b;
}

/* ── Phase 2: Toast ── */
@keyframes banana-tc-fadeInOut {
  0%   { opacity: 0; transform: translateY(16px); }
  12%  { opacity: 1; transform: translateY(0); }
  80%  { opacity: 1; transform: translateY(0); }
  100% { opacity: 0; transform: translateY(-8px); }
}

/* ── Task Detail Modal (Lightbox) ── */
.banana-tc-detail-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.82);
  backdrop-filter: blur(4px);
  z-index: 100002;
  display: flex;
  align-items: center;
  justify-content: center;
  animation: banana-tc-fadeIn 0.18s ease-out;
}
.banana-tc-detail-content {
  position: relative;
  display: flex;
  width: 90vw;
  max-width: 960px;
  height: 80vh;
  max-height: 700px;
  border-radius: 14px;
  overflow: hidden;
  background: var(--tc-bg-deep);
  border: 1px solid var(--tc-border);
  box-shadow: 0 24px 80px rgba(0,0,0,0.6);
}
/* Left: main preview area */
.banana-tc-detail-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  position: relative;
  background: var(--tc-bg-deep-alt);
  min-width: 0;
  overflow: hidden;
}
.banana-tc-detail-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 4px;
  user-select: none;
}
.banana-tc-detail-video {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
  border-radius: 4px;
  outline: none;
}
.banana-tc-detail-status-placeholder {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  color: #666;
  font-size: 14px;
}
.banana-tc-detail-status-placeholder .icon {
  font-size: 48px;
  opacity: 0.4;
}
/* Nav arrows */
.banana-tc-detail-nav {
  position: absolute;
  top: 50%;
  transform: translateY(-50%);
  background: rgba(0,0,0,0.5);
  color: white;
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 50%;
  width: 40px;
  height: 40px;
  font-size: 18px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: background 0.15s;
  z-index: 2;
}
.banana-tc-detail-nav:hover {
  background: rgba(255,255,255,0.12);
}
.banana-tc-detail-nav.prev { left: 12px; }
.banana-tc-detail-nav.next { right: 12px; }
/* Dot indicators */
.banana-tc-detail-dots {
  position: absolute;
  bottom: 14px;
  left: 50%;
  transform: translateX(-50%);
  display: flex;
  gap: 8px;
  z-index: 2;
}
.banana-tc-detail-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgba(255,255,255,0.3);
  border: none;
  cursor: pointer;
  padding: 0;
  transition: all 0.15s;
}
.banana-tc-detail-dot.active {
  width: 16px;
  border-radius: 4px;
  background: #fff;
}
/* Right: info sidebar */
.banana-tc-detail-sidebar {
  width: 280px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-left: 1px solid var(--tc-border);
  background: var(--tc-bg-base);
}
.banana-tc-detail-sidebar-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 20px 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.banana-tc-detail-field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.banana-tc-detail-label {
  font-size: 11px;
  color: #666;
  font-weight: 500;
}
.banana-tc-detail-value {
  font-size: 13px;
  color: var(--tc-text-label);
  line-height: 1.5;
  word-break: break-word;
}
.banana-tc-detail-value.prompt {
  white-space: pre-wrap;
  color: #999;
  max-height: 200px;
  overflow-y: auto;
}
.banana-tc-detail-value.error {
  color: #ef4444;
}
.banana-tc-detail-meta-row {
  display: flex;
  gap: 12px;
  flex-wrap: wrap;
}
.banana-tc-detail-meta-chip {
  background: rgba(255,255,255,0.05);
  border: 1px solid var(--tc-border);
  border-radius: 6px;
  padding: 4px 8px;
  font-size: 11px;
  color: #999;
}
/* Sidebar action buttons area */
.banana-tc-detail-actions {
  padding: 16px 20px;
  border-top: 1px solid var(--tc-border);
  display: flex;
  flex-direction: column;
  gap: 8px;
  flex-shrink: 0;
}
.banana-tc-detail-actions .banana-tc-btn {
  width: 100%;
  text-align: center;
  padding: 8px 12px;
}
/* Close button */
.banana-tc-detail-close {
  position: absolute;
  top: 12px;
  right: 12px;
  background: rgba(0,0,0,0.4);
  color: white;
  border: 1px solid rgba(255,255,255,0.15);
  border-radius: 50%;
  width: 32px;
  height: 32px;
  font-size: 16px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100003;
  transition: background 0.15s;
}
.banana-tc-detail-close:hover {
  background: rgba(255,255,255,0.12);
}

/* RISK: Full-bleed hero detail layout */
.banana-tc-detail-content.hero-layout {
  flex-direction: column;
}
.banana-tc-detail-content.hero-layout .banana-tc-detail-main {
  flex: 1;
  position: relative;
}
.banana-tc-detail-content.hero-layout .banana-tc-detail-img {
  width: 100%;
  height: 100%;
  object-fit: contain;
  max-width: none;
  max-height: none;
}
.banana-tc-detail-content.hero-layout .banana-tc-detail-sidebar {
  display: none;
}
/* Hero overlay for metadata */
.banana-tc-hero-overlay {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  padding: 20px 24px;
  background: linear-gradient(to top, rgba(6,6,12,0.95) 0%, rgba(6,6,12,0.7) 40%, transparent 100%);
  display: flex;
  flex-direction: column;
  gap: 6px;
  z-index: 2;
  pointer-events: none;
}
.banana-tc-hero-overlay > * { pointer-events: auto; }
.banana-tc-hero-model {
  font-size: 15px;
  font-weight: 700;
  color: var(--tc-text-heading);
}
.banana-tc-hero-prompt {
  font-size: 12px;
  color: rgba(255,255,255,0.55);
  max-width: 500px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.banana-tc-hero-meta {
  display: flex;
  gap: 8px;
  margin-top: 2px;
  flex-wrap: wrap;
}
.banana-tc-hero-chip {
  padding: 3px 10px;
  border-radius: 6px;
  font-size: 10px;
  background: rgba(255,255,255,0.08);
  color: rgba(255,255,255,0.7);
  border: 1px solid rgba(255,255,255,0.06);
}
.banana-tc-hero-actions {
  display: flex;
  gap: 8px;
  margin-top: 6px;
}
.banana-tc-hero-actions .banana-tc-btn {
  backdrop-filter: blur(8px);
  background: rgba(255,255,255,0.08);
  border-color: rgba(255,255,255,0.15);
}

/* ── Phase 2: Compare View ── */
.banana-tc-compare-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.82);
  z-index: 100002;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px;
}
.banana-tc-compare-title {
  color: var(--tc-text-heading);
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 16px;
}
.banana-tc-compare-grid {
  display: flex;
  gap: 16px;
  align-items: flex-end;
  justify-content: center;
  max-width: 95vw;
  overflow-x: auto;
}
.banana-tc-compare-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.banana-tc-compare-item img {
  max-height: 60vh;
  max-width: 400px;
  object-fit: contain;
  border-radius: 8px;
  border: 1px solid var(--tc-border);
}
.banana-tc-compare-label {
  color: #aaa;
  font-size: 11px;
  text-align: center;
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.banana-tc-compare-close {
  position: fixed;
  top: 20px;
  right: 24px;
  background: rgba(0,0,0,0.5);
  color: white;
  border: 1px solid rgba(255,255,255,0.2);
  border-radius: 50%;
  width: 36px;
  height: 36px;
  font-size: 20px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100003;
}
.banana-tc-compare-close:hover {
  background: rgba(255,255,255,0.15);
}

/* ── Phase 2: Download Modal ── */
.banana-tc-dl-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.65);
  z-index: 100002;
  display: flex;
  align-items: center;
  justify-content: center;
}
.banana-tc-dl-modal {
  background: var(--tc-bg-deep);
  border: 1px solid var(--tc-border);
  border-radius: 14px;
  width: 520px;
  max-width: calc(100vw - 40px);
  max-height: calc(100vh - 80px);
  display: flex;
  flex-direction: column;
  color: var(--tc-text-primary);
  font-size: 13px;
  box-shadow: 0 18px 60px rgba(0,0,0,0.5);
}
.banana-tc-dl-header {
  padding: 16px 20px;
  border-bottom: 1px solid var(--tc-border);
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.banana-tc-dl-header-title {
  font-size: 15px;
  font-weight: 700;
}
.banana-tc-dl-body {
  padding: 16px 20px;
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.banana-tc-dl-path-row {
  display: flex;
  gap: 8px;
  align-items: center;
}
.banana-tc-dl-path-input {
  flex: 1;
  padding: 8px 10px;
  border-radius: 8px;
  border: 1px solid var(--tc-border);
  background: rgba(0,0,0,0.3);
  color: var(--tc-text-primary);
  outline: none;
  font-size: 12px;
}
.banana-tc-dl-path-input:focus {
  border-color: #e2a93b66;
}
.banana-tc-dl-shortcuts {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}
.banana-tc-dl-dir-browser {
  border: 1px solid var(--tc-border);
  border-radius: 8px;
  background: var(--tc-bg-base);
  max-height: 200px;
  overflow-y: auto;
  display: none;
}
.banana-tc-dl-dir-browser.open {
  display: block;
}
.banana-tc-dl-dir-item {
  padding: 8px 12px;
  font-size: 12px;
  cursor: pointer;
  color: #ccc;
  border-bottom: 1px solid var(--tc-bg-raised);
  transition: background 0.1s;
  user-select: none;
}
.banana-tc-dl-dir-item:hover {
  background: var(--tc-bg-hover);
  color: #fff;
}
.banana-tc-dl-dir-item:last-child {
  border-bottom: none;
}
.banana-tc-dl-dir-item.parent {
  color: #e2a93b;
  font-weight: 600;
}
.banana-tc-dl-footer {
  padding: 12px 20px;
  border-top: 1px solid var(--tc-border);
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
`;
  document.head.appendChild(style);
}

// ─────────────────────────────────────────────────────────────
// SVG Icon Helper
// ─────────────────────────────────────────────────────────────

// Lucide icon path descriptors (MIT license)
const ICON_PATHS = {
  // "layout-dashboard" — 任务仪表板图标
  dashboard: [
    { tag: "rect", attrs: { x: "3", y: "3", width: "7", height: "9", rx: "1" } },
    { tag: "rect", attrs: { x: "14", y: "3", width: "7", height: "5", rx: "1" } },
    { tag: "rect", attrs: { x: "14", y: "12", width: "7", height: "9", rx: "1" } },
    { tag: "rect", attrs: { x: "3", y: "16", width: "7", height: "5", rx: "1" } },
  ],
};

function createSvgIcon(pathDescs, size = 18, strokeWidth = 1.8) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", strokeWidth);
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const desc of pathDescs) {
    const el = document.createElementNS(ns, desc.tag || "path");
    for (const [k, v] of Object.entries(desc.attrs || {})) el.setAttribute(k, v);
    if (desc.d) el.setAttribute("d", desc.d);
    svg.appendChild(el);
  }
  return svg;
}

// ─────────────────────────────────────────────────────────────
// General Helpers
// ─────────────────────────────────────────────────────────────

function debounce(fn, ms) {
  let timer = null;
  return (...args) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, ms);
  };
}

// ─────────────────────────────────────────────────────────────
// Utility
// ─────────────────────────────────────────────────────────────

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
    // ignore storage failures
  }
}

function relativeTime(ts) {
  const diff = Math.floor(Date.now() / 1000) - ts;
  if (diff < 60) return "\u521A\u521A";
  if (diff < 3600) return `${Math.floor(diff / 60)}\u5206\u949F\u524D`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}\u5C0F\u65F6\u524D`;
  return `${Math.floor(diff / 86400)}\u5929\u524D`;
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

/**
 * 获取任务的图片展示 URL 列表。
 * 优先使用已下载到本地的 local_files（不会过期），
 * 回退到远程 image_urls（API 返回，可能过期）。
 */
function resolveImageUrls(task) {
  const localFiles = task.local_files;
  if (Array.isArray(localFiles) && localFiles.length > 0) {
    const urls = localFiles.map(buildViewUrl).filter(Boolean);
    if (urls.length > 0) return urls;
  }
  // 单文件兼容：旧任务只有 local_file
  const singleUrl = buildViewUrl(task.local_file);
  if (singleUrl) return [singleUrl];
  // 最终回退到远程 URL
  return task.image_urls || [];
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

function isImageTask(task) {
  return (task.provider || "").toLowerCase() === "banana_v3";
}

function isVideoTask(task) {
  return !isImageTask(task);
}

function normalizeStatus(status) {
  const s = (status || "").toLowerCase().trim();
  if (s === "success" || s === "completed") return "success";
  if (s === "processing" || s === "running") return "processing";
  if (s === "pending" || s === "waiting_key" || s === "queued") return "pending";
  if (s === "failed" || s === "error") return "failed";
  return s || "pending";
}

function statusLabel(status) {
  switch (normalizeStatus(status)) {
    case "pending": return "\u7B49\u5F85";
    case "processing": return "\u751F\u6210\u4E2D";
    case "success": return "\u5B8C\u6210";
    case "failed": return "\u5931\u8D25";
    default: return status || "\u672A\u77E5";
  }
}

function maskKey(key) {
  if (!key || key.length < 6) return "***";
  return `${key.slice(0, 3)}***${key.slice(-3)}`;
}

function setStatusText(text) {
  if (statusTextEl) statusTextEl.textContent = text || "";
}

// ─────────────────────────────────────────────────────────────
// Filtering / Sorting
// ─────────────────────────────────────────────────────────────

// ── Pin / unpin tasks (cached) ──

let _pinnedCache = null;
let _pinnedCacheTick = 0;

function getPinnedTaskIds() {
  // Cache for the duration of one render cycle (invalidated by togglePin)
  if (_pinnedCache && _pinnedCacheTick === _renderTick) return _pinnedCache;
  try {
    const raw = localStorage.getItem(LS_PINNED_TASKS);
    if (!raw) { _pinnedCache = new Set(); } else {
      const arr = JSON.parse(raw);
      _pinnedCache = new Set(Array.isArray(arr) ? arr : []);
    }
  } catch { _pinnedCache = new Set(); }
  _pinnedCacheTick = _renderTick;
  return _pinnedCache;
}

function togglePin(taskId) {
  const pinned = getPinnedTaskIds();
  if (pinned.has(taskId)) {
    pinned.delete(taskId);
    showToast("\u5DF2\u53D6\u6D88\u7F6E\u9876");
  } else {
    pinned.add(taskId);
    showToast("\u5DF2\u7F6E\u9876");
  }
  try { localStorage.setItem(LS_PINNED_TASKS, JSON.stringify([...pinned])); } catch {}
  _pinnedCache = null; // invalidate cache
  renderCards();
}

let _renderTick = 0;

function getFilteredTasks() {
  const tabAll = state.activeTab === "all";
  const statusAll = state.statusFilter === "all";
  const q = state.searchQuery.trim().toLowerCase();

  // Single-pass filter (avoids 3 chained .filter() + 1 spread)
  const filtered = (tabAll && statusAll && !q)
    ? [...state.tasks]
    : state.tasks.filter((t) => {
        if (!tabAll) {
          if (state.activeTab === "image" && !isImageTask(t)) return false;
          if (state.activeTab === "video" && isImageTask(t)) return false;
        }
        if (!statusAll && normalizeStatus(t.status) !== state.statusFilter) return false;
        if (q) {
          const prompt = (t.prompt || "").toLowerCase();
          const model = (t.model || "").toLowerCase();
          const id = (t.id || "").toLowerCase();
          if (!prompt.includes(q) && !model.includes(q) && !id.includes(q)) return false;
        }
        return true;
      });

  // Sort
  if (state.sortMode === "newest") {
    filtered.sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
  } else if (state.sortMode === "oldest") {
    filtered.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
  } else if (state.sortMode === "status") {
    const order = { processing: 0, pending: 1, failed: 2, success: 3 };
    filtered.sort((a, b) => {
      const oa = order[normalizeStatus(a.status)] ?? 4;
      const ob = order[normalizeStatus(b.status)] ?? 4;
      if (oa !== ob) return oa - ob;
      return (b.created_at || 0) - (a.created_at || 0);
    });
  }

  // Pinned tasks float to top (regardless of sort mode)
  const pinned = getPinnedTaskIds();
  if (pinned.size > 0) {
    filtered.sort((a, b) => {
      const ap = pinned.has(String(a.id || "").trim()) ? 0 : 1;
      const bp = pinned.has(String(b.id || "").trim()) ? 0 : 1;
      return ap - bp;
    });
  }

  return filtered;
}

function countByTab(tab) {
  if (tab === "all") return state.tasks.length;
  if (tab === "image") return state.tasks.filter(isImageTask).length;
  if (tab === "video") return state.tasks.filter(isVideoTask).length;
  return 0;
}

function countByStatus(statusKey) {
  let pool = state.tasks;
  if (state.activeTab === "image") pool = pool.filter(isImageTask);
  else if (state.activeTab === "video") pool = pool.filter(isVideoTask);

  if (statusKey === "all") return pool.length;
  return pool.filter((t) => normalizeStatus(t.status) === statusKey).length;
}

// ─────────────────────────────────────────────────────────────
// Badge
// ─────────────────────────────────────────────────────────────

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

function computeBadge() {
  const successIds = state.tasks
    .filter((t) => normalizeStatus(t.status) === "success")
    .map((t) => String(t.id || "").trim())
    .filter((id) => id);

  const isPanelOpen = overlayEl && overlayEl.classList.contains("open");

  if (isPanelOpen) {
    // Panel open: mark all success tasks as read, badge = 0
    const oldRead = readStorageJson(LS_READ_TASKS) || [];
    const newSet = new Set([...oldRead, ...successIds]);
    writeStorageJson(LS_READ_TASKS, Array.from(newSet));
    updateBadge(0);
  } else {
    // Panel closed: count unread success tasks
    const readRaw = readStorageJson(LS_READ_TASKS);
    const readSet = new Set(Array.isArray(readRaw) ? readRaw : []);
    let unreadCount = 0;
    for (const id of successIds) {
      if (!readSet.has(id)) unreadCount++;
    }
    updateBadge(unreadCount);
  }
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Toast Notification
// ─────────────────────────────────────────────────────────────

function showToast(message, type = "success") {
  const toast = document.createElement("div");
  toast.className = `banana-tc-toast ${type === "error" ? "error" : "success"}`;
  toast.textContent = message;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 3100);
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Copy to Clipboard
// ─────────────────────────────────────────────────────────────

function copyToClipboard(text) {
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    navigator.clipboard.writeText(text).then(
      () => showToast("已复制到剪贴板"),
      () => copyToClipboardFallback(text)
    );
  } else {
    copyToClipboardFallback(text);
  }
}

function copyToClipboardFallback(text) {
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.cssText = "position:fixed;left:-9999px;top:-9999px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    showToast(ok ? "已复制到剪贴板" : "复制失败", ok ? undefined : "error");
  } catch (_) {
    showToast("复制失败", "error");
  }
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Push to Canvas
// ─────────────────────────────────────────────────────────────

async function pushToCanvas(imageUrl) {
  try {
    const payload = await postJson(API_PUSH_TO_CANVAS, { image_url: imageUrl });
    const filename = payload?.data?.filename;
    if (!filename) {
      showToast("推送失败: 未返回文件名", "error");
      return;
    }
    // Create a LoadImage node on the canvas
    const node = LiteGraph.createNode("LoadImage");
    if (!node) {
      showToast("推送失败: 无法创建 LoadImage 节点", "error");
      return;
    }
    app.graph.add(node);
    // Position near center of visible canvas area
    const canvasEl = app.canvas?.canvas;
    if (canvasEl) {
      const cx = (app.canvas.ds?.offset?.[0] || 0) * -1 + canvasEl.width / 2 / (app.canvas.ds?.scale || 1);
      const cy = (app.canvas.ds?.offset?.[1] || 0) * -1 + canvasEl.height / 2 / (app.canvas.ds?.scale || 1);
      node.pos = [cx - 100, cy - 100];
    } else {
      node.pos = [200, 200];
    }
    const w = node.widgets?.find((w) => w.name === "image");
    if (w) w.value = filename;
    app.graph.setDirtyCanvas(true);

    // Auto-close task center (detail modal + panel)
    closeTaskDetail();
    hideOverlay();

    // Focus viewport on the new node
    if (app.canvas) {
      // LiteGraph centerOnNode if available, otherwise manual offset
      if (typeof app.canvas.centerOnNode === "function") {
        app.canvas.centerOnNode(node);
      } else {
        const [nx, ny] = node.pos;
        const [nw, nh] = node.size || [200, 200];
        const scale = app.canvas.ds?.scale || 1;
        const el = app.canvas.canvas;
        if (el) {
          app.canvas.ds.offset[0] = -(nx + nw / 2) + el.width / 2 / scale;
          app.canvas.ds.offset[1] = -(ny + nh / 2) + el.height / 2 / scale;
        }
      }
      if (typeof app.canvas.selectNode === "function") {
        app.canvas.selectNode(node);
      } else {
        app.canvas.selected_nodes = { [node.id]: node };
        node.is_selected = true;
      }
      app.graph.setDirtyCanvas(true, true);
    }

    showToast("已推送到画布");
  } catch (err) {
    showToast(`推送失败: ${err?.message || err}`, "error");
  }
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Reuse Prompt
// ─────────────────────────────────────────────────────────────

function reusePrompt(promptText) {
  if (!promptText) {
    showToast("无可复用的 prompt", "error");
    return;
  }
  // Find BananaImageNodeV3 nodes on canvas
  const nodes = app.graph?._nodes?.filter(
    (n) => n.type === "BananaImageNodeV3"
  ) || [];

  if (nodes.length === 1) {
    const pw = nodes[0].widgets?.find((w) => w.name === "prompt");
    if (pw) {
      pw.value = promptText;
      app.graph.setDirtyCanvas(true);
      showToast("已填入 prompt");
      return;
    }
  }
  // 0 or multiple nodes: copy to clipboard
  copyToClipboard(promptText);
  if (nodes.length === 0) {
    showToast("未找到 V3 节点，已复制 prompt");
  } else {
    showToast("多个 V3 节点，已复制 prompt");
  }
}

// ─────────────────────────────────────────────────────────────
// Task Detail Modal (Lightbox)
// ─────────────────────────────────────────────────────────────

let activeDetailOverlay = null;

function openTaskDetail(task) {
  closeTaskDetail();

  const ns = normalizeStatus(task.status);
  const isImg = isImageTask(task);
  const taskId = String(task.id || "").trim();
  const imageUrls = resolveImageUrls(task);
  let currentIdx = 0;

  // ── Overlay ──
  const overlay = document.createElement("div");
  overlay.className = "banana-tc-detail-overlay";
  activeDetailOverlay = overlay;

  // ── Content wrapper ──
  const content = document.createElement("div");
  content.className = "banana-tc-detail-content";

  // ── Close button (top-right of content) ──
  const closeBtn = document.createElement("button");
  closeBtn.className = "banana-tc-detail-close";
  closeBtn.textContent = "\u2715";
  closeBtn.addEventListener("click", closeTaskDetail);
  content.appendChild(closeBtn);

  // ════════════════════════════════════════════════════════════
  // LEFT: Main preview area
  // ════════════════════════════════════════════════════════════
  const main = document.createElement("div");
  main.className = "banana-tc-detail-main";

  let imgEl = null;
  let prevBtn = null;
  let nextBtn = null;
  let dotsContainer = null;

  if (ns === "success" && isImg && imageUrls.length > 0) {
    // RISK: Full-bleed hero layout for image tasks
    content.classList.add("hero-layout");

    // Image task — show large image
    imgEl = document.createElement("img");
    imgEl.className = "banana-tc-detail-img";
    imgEl.src = imageUrls[0];
    imgEl.alt = "preview";
    imgEl.draggable = false;
    main.appendChild(imgEl);

    // Nav arrows (only if multiple images)
    if (imageUrls.length > 1) {
      prevBtn = document.createElement("button");
      prevBtn.className = "banana-tc-detail-nav prev";
      prevBtn.textContent = "\u25C0";
      prevBtn.addEventListener("click", (e) => { e.stopPropagation(); navigate(-1); });
      main.appendChild(prevBtn);

      nextBtn = document.createElement("button");
      nextBtn.className = "banana-tc-detail-nav next";
      nextBtn.textContent = "\u25B6";
      nextBtn.addEventListener("click", (e) => { e.stopPropagation(); navigate(1); });
      main.appendChild(nextBtn);

      // Dot indicators
      dotsContainer = document.createElement("div");
      dotsContainer.className = "banana-tc-detail-dots";
      for (let i = 0; i < imageUrls.length; i++) {
        const dot = document.createElement("button");
        dot.className = "banana-tc-detail-dot";
        dot.addEventListener("click", () => { currentIdx = i; updatePreview(); });
        dotsContainer.appendChild(dot);
      }
      main.appendChild(dotsContainer);
    }

    // RISK: Hero overlay with metadata at bottom
    const heroOverlay = document.createElement("div");
    heroOverlay.className = "banana-tc-hero-overlay";

    const heroModel = document.createElement("div");
    heroModel.className = "banana-tc-hero-model";
    heroModel.textContent = task.model || task.provider || "";
    heroOverlay.appendChild(heroModel);

    if (task.prompt) {
      const heroPromptRow = document.createElement("div");
      heroPromptRow.style.cssText = "display:flex;align-items:center;gap:6px;";
      const heroPrompt = document.createElement("div");
      heroPrompt.className = "banana-tc-hero-prompt";
      heroPrompt.textContent = task.prompt;
      heroPrompt.title = task.prompt;
      heroPromptRow.appendChild(heroPrompt);
      const copyBtn = document.createElement("button");
      copyBtn.className = "banana-tc-btn";
      copyBtn.style.cssText = "padding:2px 8px;font-size:10px;flex-shrink:0;backdrop-filter:blur(8px);background:rgba(255,255,255,0.08);border-color:rgba(255,255,255,0.15);";
      copyBtn.textContent = "\u590D\u5236";
      copyBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        navigator.clipboard.writeText(task.prompt).then(
          () => showToast("\u63D0\u793A\u8BCD\u5DF2\u590D\u5236"),
          () => showToast("\u590D\u5236\u5931\u8D25", "error")
        );
      });
      heroPromptRow.appendChild(copyBtn);
      heroOverlay.appendChild(heroPromptRow);
    }

    const heroMeta = document.createElement("div");
    heroMeta.className = "banana-tc-hero-meta";
    const metaItems = [];
    metaItems.push("\u2713 \u5B8C\u6210");
    if (task.image_count) metaItems.push(`${task.image_count} \u5F20`);
    if (task.aspect_ratio) metaItems.push(task.aspect_ratio);
    if (task.seed != null && task.seed !== "") metaItems.push(`seed: ${task.seed}`);
    if (task.created_at) metaItems.push(relativeTime(task.created_at));
    for (const txt of metaItems) {
      const chip = document.createElement("span");
      chip.className = "banana-tc-hero-chip";
      chip.textContent = txt;
      heroMeta.appendChild(chip);
    }
    heroOverlay.appendChild(heroMeta);

    // Hero action buttons
    const heroActions = document.createElement("div");
    heroActions.className = "banana-tc-hero-actions";

    if (imageUrls.length > 0) {
      const openBtn = document.createElement("button");
      openBtn.className = "banana-tc-btn";
      openBtn.textContent = "\u6253\u5F00\u94FE\u63A5";
      openBtn.addEventListener("click", (e) => { e.stopPropagation(); window.open(imageUrls[currentIdx], "_blank"); });
      heroActions.appendChild(openBtn);
    }
    if (taskId) {
      const pushBtn = document.createElement("button");
      pushBtn.className = "banana-tc-btn primary";
      pushBtn.textContent = "\u53D1\u9001\u5230\u753B\u5E03";
      pushBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        const url = imageUrls[currentIdx];
        if (!url) return;
        void pushToCanvas(url);
      });
      heroActions.appendChild(pushBtn);
    }
    heroOverlay.appendChild(heroActions);

    main.appendChild(heroOverlay);
  } else if (ns === "success" && !isImg) {
    // Video task — show video player or link
    const viewUrl = buildViewUrl(task.local_file);
    if (viewUrl) {
      const video = document.createElement("video");
      video.className = "banana-tc-detail-video";
      video.src = viewUrl;
      video.controls = true;
      video.autoplay = false;
      video.preload = "metadata";
      main.appendChild(video);
    } else if (task.video_url) {
      const placeholder = document.createElement("div");
      placeholder.className = "banana-tc-detail-status-placeholder";
      placeholder.innerHTML = '<div class="icon">\u25B6</div><div>\u89C6\u9891\u5DF2\u751F\u6210\uFF0C\u70B9\u51FB\u4FA7\u680F\u201C\u6253\u5F00\u94FE\u63A5\u201D\u67E5\u770B</div>';
      main.appendChild(placeholder);
    } else {
      const placeholder = document.createElement("div");
      placeholder.className = "banana-tc-detail-status-placeholder";
      placeholder.innerHTML = '<div class="icon">\u25B6</div><div>\u89C6\u9891\u5DF2\u5B8C\u6210</div>';
      main.appendChild(placeholder);
    }
  } else if (ns === "processing") {
    const placeholder = document.createElement("div");
    placeholder.className = "banana-tc-detail-status-placeholder";
    const pct = typeof task.progress === "number" ? `${Math.round(task.progress)}%` : "";
    placeholder.innerHTML = `<div class="banana-tc-spinner${isImg ? "" : " video"}" style="width:40px;height:40px;border-width:4px;"></div><div>\u751F\u6210\u4E2D${pct ? " " + pct : ""}...</div>`;
    main.appendChild(placeholder);
  } else if (ns === "failed") {
    const placeholder = document.createElement("div");
    placeholder.className = "banana-tc-detail-status-placeholder";
    placeholder.innerHTML = '<div class="icon">\u26A0</div><div>\u4EFB\u52A1\u5931\u8D25</div>';
    main.appendChild(placeholder);
  } else {
    // pending
    const placeholder = document.createElement("div");
    placeholder.className = "banana-tc-detail-status-placeholder";
    placeholder.innerHTML = '<div class="icon">\u00B7\u00B7\u00B7</div><div>\u7B49\u5F85\u5904\u7406...</div>';
    main.appendChild(placeholder);
  }

  content.appendChild(main);

  // ════════════════════════════════════════════════════════════
  // RIGHT: Info sidebar
  // ════════════════════════════════════════════════════════════
  const sidebar = document.createElement("div");
  sidebar.className = "banana-tc-detail-sidebar";

  // Scrollable info area
  const scrollArea = document.createElement("div");
  scrollArea.className = "banana-tc-detail-sidebar-scroll";

  // Model
  if (task.model || task.provider) {
    const field = createField("\u6A21\u578B", task.model || task.provider);
    scrollArea.appendChild(field);
  }

  // Status
  {
    const statusMap = {
      pending: "\u7B49\u5F85\u4E2D",
      processing: "\u751F\u6210\u4E2D" + (typeof task.progress === "number" ? ` ${Math.round(task.progress)}%` : ""),
      success: "\u2713 \u5DF2\u5B8C\u6210",
      failed: "\u2717 \u5931\u8D25"
    };
    const field = createField("\u72B6\u6001", statusMap[ns] || ns);
    scrollArea.appendChild(field);
  }

  // Prompt (full text)
  if (task.prompt) {
    const field = createField("\u63D0\u793A\u8BCD", task.prompt, "prompt");
    scrollArea.appendChild(field);
  }

  // Error message
  if (task.error) {
    const field = createField("\u9519\u8BEF\u4FE1\u606F", task.error, "error");
    scrollArea.appendChild(field);
  }

  // Meta chips (seed, aspect_ratio, provider)
  {
    const chips = [];
    if (task.seed != null && task.seed !== "") chips.push(`seed: ${task.seed}`);
    if (task.aspect_ratio) chips.push(task.aspect_ratio);
    if (task.provider) chips.push(task.provider);
    if (task.image_count) chips.push(`${task.image_count} \u5F20`);
    if (task.elapsed != null) chips.push(`\u8017\u65F6 ${task.elapsed}s`);
    if (chips.length > 0) {
      const field = document.createElement("div");
      field.className = "banana-tc-detail-field";
      const label = document.createElement("div");
      label.className = "banana-tc-detail-label";
      label.textContent = "\u53C2\u6570";
      field.appendChild(label);
      const row = document.createElement("div");
      row.className = "banana-tc-detail-meta-row";
      for (const c of chips) {
        const chip = document.createElement("span");
        chip.className = "banana-tc-detail-meta-chip";
        chip.textContent = c;
        row.appendChild(chip);
      }
      field.appendChild(row);
      scrollArea.appendChild(field);
    }
  }

  // Created time
  if (task.created_at) {
    const field = createField("\u521B\u5EFA\u65F6\u95F4", relativeTime(task.created_at));
    scrollArea.appendChild(field);
  }

  sidebar.appendChild(scrollArea);

  // ── Action buttons (fixed at bottom) ──
  const actions = document.createElement("div");
  actions.className = "banana-tc-detail-actions";

  // Manual refresh (pending/processing/failed)
  if (taskId && (ns === "pending" || ns === "processing" || ns === "failed")) {
    actions.appendChild(createActionBtn("\u624B\u52A8\u5237\u65B0", "primary", async () => {
      try {
        await postJson(API_REFRESH, { id: taskId });
        showToast("\u5DF2\u89E6\u53D1\u624B\u52A8\u5237\u65B0");
        void fetchAndRender();
      } catch (err) {
        showToast(`\u5237\u65B0\u5931\u8D25: ${err?.message || err}`, "error");
      }
    }));
  }

  // Push to canvas (success image only)
  if (ns === "success" && isImg && imageUrls.length > 0) {
    actions.appendChild(createActionBtn("\u63A8\u9001\u753B\u5E03", "primary", () => {
      void pushToCanvas(imageUrls[currentIdx]);
      closeTaskDetail();
    }));
  }

  // Open local file (success with local file, follows current image index)
  {
    const hasLocal = task.local_file || (Array.isArray(task.local_files) && task.local_files.length > 0);
    if (hasLocal) {
      actions.appendChild(createActionBtn("\u672C\u5730\u6253\u5F00", "", () => {
        // 根据当前浏览索引选择对应的本地文件
        const currentLocal = (Array.isArray(task.local_files) && task.local_files[currentIdx])
          ? task.local_files[currentIdx]
          : task.local_file;
        const viewUrl = buildViewUrl(currentLocal);
        if (!viewUrl) return;
        if (!isWindowsPlatform()) {
          window.open(viewUrl, "_blank");
          return;
        }
        void (async () => {
          const opened = await tryOpenInWindowsExplorer({ taskId, localFile: currentLocal });
          if (opened) {
            showToast("\u5DF2\u5728\u8D44\u6E90\u7BA1\u7406\u5668\u5B9A\u4F4D\u6587\u4EF6");
          } else {
            window.open(viewUrl, "_blank");
          }
        })();
      }));
    }
  }

  // Open video link
  if (task.video_url) {
    actions.appendChild(createActionBtn("\u6253\u5F00\u94FE\u63A5", "", () => {
      window.open(String(task.video_url), "_blank");
    }));
  }

  // Copy link (success with URLs)
  {
    const linkUrl = imageUrls.length > 0 ? imageUrls[currentIdx] : (task.video_url || "");
    if (linkUrl && ns === "success") {
      actions.appendChild(createActionBtn("\u590D\u5236\u94FE\u63A5", "", () => {
        const u = imageUrls.length > 0 ? imageUrls[currentIdx] : (task.video_url || "");
        copyToClipboard(u);
      }));
    }
  }

  // Download
  if (ns === "success") {
    const dlUrl = imageUrls.length > 0 ? imageUrls[0] : (task.video_url || "");
    if (dlUrl) {
      actions.appendChild(createActionBtn("\u4E0B\u8F7D", "", () => {
        const u = imageUrls.length > 0 ? imageUrls[currentIdx] : (task.video_url || "");
        const a = document.createElement("a");
        a.href = u;
        a.download = "";
        a.target = "_blank";
        a.click();
      }));
    }
  }

  // Reuse prompt
  if (task.prompt) {
    actions.appendChild(createActionBtn("\u590D\u7528 Prompt", "", () => {
      reusePrompt(task.prompt);
    }));
  }

  // Delete
  if (taskId) {
    actions.appendChild(createActionBtn("\u5220\u9664", "danger", async () => {
      try {
        await postJson(API_DELETE, { task_ids: [taskId] });
        showToast("\u5DF2\u5220\u9664\u4EFB\u52A1");
        closeTaskDetail();
        void fetchAndRender();
      } catch (err) {
        showToast(`\u5220\u9664\u5931\u8D25: ${err?.message || err}`, "error");
      }
    }));
  }

  sidebar.appendChild(actions);
  content.appendChild(sidebar);
  overlay.appendChild(content);

  // ── Background click to close ──
  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeTaskDetail();
  });

  // ── Keyboard handler ──
  const onKey = (e) => {
    if (e.key === "Escape") { closeTaskDetail(); return; }
    if (imageUrls.length > 1) {
      if (e.key === "ArrowLeft") { navigate(-1); }
      if (e.key === "ArrowRight") { navigate(1); }
    }
    // Delete key: delete current task
    if (e.key === "Delete" && taskId) {
      e.preventDefault();
      if (confirm("\u786E\u8BA4\u5220\u9664\u8FD9\u4E2A\u4EFB\u52A1\uFF1F")) {
        postJson(API_DELETE, { task_ids: [taskId] }).then(() => {
          closeTaskDetail();
          showToast("\u5DF2\u5220\u9664");
          void fetchAndRender();
        }).catch(() => showToast("\u5220\u9664\u5931\u8D25", "error"));
      }
    }
  };
  document.addEventListener("keydown", onKey);
  overlay._keyHandler = onKey;

  document.body.appendChild(overlay);

  // ── Navigation helpers ──
  function navigate(delta) {
    currentIdx = Math.max(0, Math.min(imageUrls.length - 1, currentIdx + delta));
    updatePreview();
  }

  function updatePreview() {
    if (imgEl) imgEl.src = imageUrls[currentIdx];
    if (prevBtn) prevBtn.style.display = currentIdx > 0 ? "flex" : "none";
    if (nextBtn) nextBtn.style.display = currentIdx < imageUrls.length - 1 ? "flex" : "none";
    if (dotsContainer) {
      const dots = dotsContainer.children;
      for (let i = 0; i < dots.length; i++) {
        dots[i].classList.toggle("active", i === currentIdx);
      }
    }
  }

  // Initial state
  if (imageUrls.length > 1) updatePreview();
}

function closeTaskDetail() {
  if (!activeDetailOverlay) return;
  if (activeDetailOverlay._keyHandler) {
    document.removeEventListener("keydown", activeDetailOverlay._keyHandler);
  }
  activeDetailOverlay.remove();
  activeDetailOverlay = null;
}

// ── Context Menu ──

let activeCtxMenu = null;

function closeCtxMenu() {
  if (activeCtxMenu) { activeCtxMenu.remove(); activeCtxMenu = null; }
  document.removeEventListener("click", closeCtxMenu);
  document.removeEventListener("contextmenu", closeCtxMenu);
}

function showCtxMenu(x, y, items) {
  closeCtxMenu();
  const menu = document.createElement("div");
  menu.className = "banana-tc-ctx-menu";
  for (const item of items) {
    if (item === "---") {
      const sep = document.createElement("div");
      sep.className = "banana-tc-ctx-sep";
      menu.appendChild(sep);
      continue;
    }
    const el = document.createElement("div");
    el.className = "banana-tc-ctx-item" + (item.danger ? " danger" : "");
    el.textContent = item.label;
    el.addEventListener("click", (e) => {
      e.stopPropagation();
      closeCtxMenu();
      item.action();
    });
    menu.appendChild(el);
  }
  document.body.appendChild(menu);
  activeCtxMenu = menu;

  // Position: ensure menu stays within viewport
  requestAnimationFrame(() => {
    const rect = menu.getBoundingClientRect();
    if (x + rect.width > window.innerWidth - 8) x = window.innerWidth - rect.width - 8;
    if (y + rect.height > window.innerHeight - 8) y = window.innerHeight - rect.height - 8;
    menu.style.left = `${Math.max(8, x)}px`;
    menu.style.top = `${Math.max(8, y)}px`;
  });

  // Close on next click anywhere
  setTimeout(() => {
    document.addEventListener("click", closeCtxMenu);
    document.addEventListener("contextmenu", closeCtxMenu);
  }, 0);
}

function buildCardCtxMenuItems(task) {
  const taskId = String(task.id || "").trim();
  const ns = normalizeStatus(task.status);
  const isImg = isImageTask(task);
  const imageUrls = resolveImageUrls(task);
  const items = [];

  items.push({ label: "\u6253\u5F00\u8BE6\u60C5", action: () => openTaskDetail(task) });

  if (taskId) {
    const isPinned = getPinnedTaskIds().has(taskId);
    items.push({ label: isPinned ? "\u53D6\u6D88\u7F6E\u9876" : "\u7F6E\u9876", action: () => togglePin(taskId) });
  }

  if (ns === "success" && isImg && imageUrls.length > 0) {
    items.push({ label: "\u53D1\u9001\u5230\u753B\u5E03", action: () => {
      void pushToCanvas(imageUrls[0]);
    }});
  }
  if (ns === "success" && !isImg && task.video_url) {
    items.push({ label: "\u6253\u5F00\u89C6\u9891\u94FE\u63A5", action: () => window.open(task.video_url, "_blank") });
  }

  if (task.prompt) {
    items.push({ label: "\u590D\u5236\u63D0\u793A\u8BCD", action: () => {
      navigator.clipboard.writeText(task.prompt).then(
        () => showToast("\u63D0\u793A\u8BCD\u5DF2\u590D\u5236"),
        () => showToast("\u590D\u5236\u5931\u8D25", "error")
      );
    }});
  }

  if (taskId && (ns === "pending" || ns === "processing" || ns === "failed")) {
    items.push("---");
    items.push({ label: "\u624B\u52A8\u5237\u65B0", action: async () => {
      await postJson(API_REFRESH, { id: taskId });
      showToast("\u5DF2\u89E6\u53D1\u5237\u65B0");
      void fetchAndRender();
    }});
  }

  if (taskId) {
    items.push("---");
    items.push({ label: "\u5220\u9664", danger: true, action: async () => {
      if (!confirm("\u786E\u8BA4\u5220\u9664\u8FD9\u4E2A\u4EFB\u52A1\uFF1F")) return;
      try {
        await postJson(API_DELETE, { task_ids: [taskId] });
        showToast("\u5DF2\u5220\u9664");
        void fetchAndRender();
      } catch { showToast("\u5220\u9664\u5931\u8D25", "error"); }
    }});
  }

  return items;
}

// ── Detail modal helpers ──

function createField(labelText, valueText, valueClass) {
  const field = document.createElement("div");
  field.className = "banana-tc-detail-field";
  const labelRow = document.createElement("div");
  labelRow.style.cssText = "display:flex;justify-content:space-between;align-items:center;";
  const label = document.createElement("div");
  label.className = "banana-tc-detail-label";
  label.textContent = labelText;
  labelRow.appendChild(label);
  // Copy button for prompt fields
  if (valueClass === "prompt" && valueText) {
    const copyBtn = document.createElement("button");
    copyBtn.style.cssText = "background:none;border:none;color:#e2a93b;font-size:10px;cursor:pointer;padding:0;opacity:0.7;transition:opacity 0.15s;";
    copyBtn.textContent = "\u590D\u5236";
    copyBtn.addEventListener("mouseenter", () => { copyBtn.style.opacity = "1"; });
    copyBtn.addEventListener("mouseleave", () => { copyBtn.style.opacity = "0.7"; });
    copyBtn.addEventListener("click", () => {
      navigator.clipboard.writeText(valueText).then(
        () => showToast("\u63D0\u793A\u8BCD\u5DF2\u590D\u5236"),
        () => showToast("\u590D\u5236\u5931\u8D25", "error")
      );
    });
    labelRow.appendChild(copyBtn);
  }
  field.appendChild(labelRow);
  const value = document.createElement("div");
  value.className = "banana-tc-detail-value" + (valueClass ? " " + valueClass : "");
  value.textContent = valueText;
  field.appendChild(value);
  return field;
}

function createActionBtn(text, variant, onClick) {
  const btn = document.createElement("button");
  btn.className = "banana-tc-btn" + (variant ? " " + variant : "");
  btn.textContent = text;
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    onClick();
  });
  return btn;
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Compare View
// ─────────────────────────────────────────────────────────────

let activeCompareOverlay = null;

function openCompareView() {
  closeCompareView();

  const selectedTasks = state.tasks.filter((t) => state.selectedIds.has(String(t.id || "")));
  // Collect tasks that have images
  const withImages = selectedTasks.filter((t) => resolveImageUrls(t).length > 0);
  if (withImages.length < 2) {
    showToast("请选择 2-4 张有图片的任务进行对比", "error");
    return;
  }
  const items = withImages.slice(0, 4);

  const overlay = document.createElement("div");
  overlay.className = "banana-tc-compare-overlay";
  activeCompareOverlay = overlay;

  // Close button
  const closeBtn = document.createElement("button");
  closeBtn.className = "banana-tc-compare-close";
  closeBtn.textContent = "\u2715";
  closeBtn.addEventListener("click", closeCompareView);
  overlay.appendChild(closeBtn);

  const title = document.createElement("div");
  title.className = "banana-tc-compare-title";
  title.textContent = `对比视图 (${items.length} 张)`;
  overlay.appendChild(title);

  const grid = document.createElement("div");
  grid.className = "banana-tc-compare-grid";

  for (const t of items) {
    const item = document.createElement("div");
    item.className = "banana-tc-compare-item";

    const img = document.createElement("img");
    img.src = resolveImageUrls(t)[0];
    img.alt = "compare";
    item.appendChild(img);

    const label = document.createElement("div");
    label.className = "banana-tc-compare-label";
    const labelParts = [];
    if (t.model) labelParts.push(t.model);
    if (t.seed != null && t.seed !== "") labelParts.push(`seed:${t.seed}`);
    label.textContent = labelParts.join(" | ") || String(t.id || "").slice(0, 8);
    label.title = label.textContent;
    item.appendChild(label);

    grid.appendChild(item);
  }

  overlay.appendChild(grid);

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeCompareView();
  });

  const onKey = (e) => { if (e.key === "Escape") closeCompareView(); };
  document.addEventListener("keydown", onKey);
  overlay._keyHandler = onKey;

  document.body.appendChild(overlay);
}

function closeCompareView() {
  if (!activeCompareOverlay) return;
  if (activeCompareOverlay._keyHandler) {
    document.removeEventListener("keydown", activeCompareOverlay._keyHandler);
  }
  activeCompareOverlay.remove();
  activeCompareOverlay = null;
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Download Modal with Directory Browser
// ─────────────────────────────────────────────────────────────

let activeDownloadOverlay = null;

function openDownloadModal(taskIds) {
  closeDownloadModal();

  if (!taskIds || taskIds.length === 0) {
    showToast("没有可下载的任务", "error");
    return;
  }

  const overlay = document.createElement("div");
  overlay.className = "banana-tc-dl-overlay";
  activeDownloadOverlay = overlay;

  const modal = document.createElement("div");
  modal.className = "banana-tc-dl-modal";

  // Header
  const header = document.createElement("div");
  header.className = "banana-tc-dl-header";
  const headerTitle = document.createElement("div");
  headerTitle.className = "banana-tc-dl-header-title";
  headerTitle.textContent = `批量下载 (${taskIds.length} 个任务)`;
  const headerClose = document.createElement("button");
  headerClose.className = "banana-tc-btn";
  headerClose.textContent = "\u2715";
  headerClose.addEventListener("click", closeDownloadModal);
  header.appendChild(headerTitle);
  header.appendChild(headerClose);
  modal.appendChild(header);

  // Body
  const body = document.createElement("div");
  body.className = "banana-tc-dl-body";

  // Path input row
  const pathLabel = document.createElement("div");
  pathLabel.style.cssText = "font-size:12px;color:#aaa;margin-bottom:2px;";
  pathLabel.textContent = "保存路径:";
  body.appendChild(pathLabel);

  const pathRow = document.createElement("div");
  pathRow.className = "banana-tc-dl-path-row";
  const pathInput = document.createElement("input");
  pathInput.className = "banana-tc-dl-path-input";
  pathInput.placeholder = "输入保存路径";
  // Restore last used path
  const lastDir = window.localStorage.getItem(LS_LAST_DOWNLOAD_DIR) || "";
  pathInput.value = lastDir;

  const browseBtn = document.createElement("button");
  browseBtn.className = "banana-tc-btn";
  browseBtn.textContent = "\u6D4F\u89C8";
  browseBtn.title = "浏览目录";
  pathRow.appendChild(pathInput);
  pathRow.appendChild(browseBtn);
  body.appendChild(pathRow);

  // Shortcuts
  const shortcuts = document.createElement("div");
  shortcuts.className = "banana-tc-dl-shortcuts";

  const outputBtn = document.createElement("button");
  outputBtn.className = "banana-tc-btn";
  outputBtn.textContent = "ComfyUI output";
  outputBtn.addEventListener("click", () => { pathInput.value = "output/video_tasks"; });
  shortcuts.appendChild(outputBtn);

  if (lastDir) {
    const lastBtn = document.createElement("button");
    lastBtn.className = "banana-tc-btn";
    lastBtn.textContent = "上次路径";
    lastBtn.addEventListener("click", () => { pathInput.value = lastDir; });
    shortcuts.appendChild(lastBtn);
  }
  body.appendChild(shortcuts);

  // Directory browser
  const dirBrowser = document.createElement("div");
  dirBrowser.className = "banana-tc-dl-dir-browser";

  browseBtn.addEventListener("click", () => {
    const isOpen = dirBrowser.classList.contains("open");
    if (isOpen) {
      dirBrowser.classList.remove("open");
    } else {
      dirBrowser.classList.add("open");
      void loadDirListing(pathInput.value || ".");
    }
  });

  async function loadDirListing(dirPath) {
    dirBrowser.innerHTML = "";
    const loadingEl = document.createElement("div");
    loadingEl.className = "banana-tc-dl-dir-item";
    loadingEl.textContent = "加载中...";
    loadingEl.style.color = "#666";
    dirBrowser.appendChild(loadingEl);

    try {
      const resp = await fetchJson(`${API_BROWSE_DIR}?path=${encodeURIComponent(dirPath)}`, { method: "GET" });
      const data = resp?.data || {};
      dirBrowser.innerHTML = "";

      // Parent directory
      if (data.parent) {
        const parentItem = document.createElement("div");
        parentItem.className = "banana-tc-dl-dir-item parent";
        parentItem.textContent = "\u2190 ..";
        parentItem.addEventListener("click", () => {
          pathInput.value = data.parent;
          void loadDirListing(data.parent);
        });
        dirBrowser.appendChild(parentItem);
      }

      // Sub-directories
      const dirs = data.dirs || [];
      if (dirs.length === 0 && !data.parent) {
        const empty = document.createElement("div");
        empty.className = "banana-tc-dl-dir-item";
        empty.textContent = "(无子目录)";
        empty.style.color = "#666";
        dirBrowser.appendChild(empty);
      }
      for (const d of dirs) {
        const dirItem = document.createElement("div");
        dirItem.className = "banana-tc-dl-dir-item";
        dirItem.textContent = `\u2514 ${d}`;
        // Single click = fill path
        dirItem.addEventListener("click", () => {
          const newPath = data.current ? `${data.current}/${d}`.replace(/\/+/g, "/") : d;
          pathInput.value = newPath;
        });
        // Double click = enter directory
        dirItem.addEventListener("dblclick", () => {
          const newPath = data.current ? `${data.current}/${d}`.replace(/\/+/g, "/") : d;
          pathInput.value = newPath;
          void loadDirListing(newPath);
        });
        dirBrowser.appendChild(dirItem);
      }
    } catch (err) {
      dirBrowser.innerHTML = "";
      const errEl = document.createElement("div");
      errEl.className = "banana-tc-dl-dir-item";
      errEl.textContent = `错误: ${err?.message || err}`;
      errEl.style.color = "#ef4444";
      dirBrowser.appendChild(errEl);
    }
  }

  body.appendChild(dirBrowser);
  modal.appendChild(body);

  // Footer
  const footer = document.createElement("div");
  footer.className = "banana-tc-dl-footer";

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "banana-tc-btn";
  cancelBtn.textContent = "取消";
  cancelBtn.addEventListener("click", closeDownloadModal);

  const startBtn = document.createElement("button");
  startBtn.className = "banana-tc-btn primary";
  startBtn.textContent = "开始下载";
  startBtn.addEventListener("click", async () => {
    const saveDir = pathInput.value.trim();
    if (!saveDir) {
      showToast("请输入保存路径", "error");
      return;
    }
    // Save last used dir
    try { window.localStorage.setItem(LS_LAST_DOWNLOAD_DIR, saveDir); } catch (_) { /* ignore */ }

    startBtn.disabled = true;
    startBtn.textContent = "下载中...";
    try {
      const result = await postJson(API_DOWNLOAD, { task_ids: taskIds, save_dir: saveDir });
      const d = result?.data || {};
      const downloaded = d.downloaded || 0;
      const failed = d.failed || 0;
      if (failed > 0) {
        showToast(`下载完成: ${downloaded} 成功, ${failed} 失败`, "error");
      } else {
        showToast(`下载完成: ${downloaded} 个文件`);
      }
      closeDownloadModal();
    } catch (err) {
      showToast(`下载失败: ${err?.message || err}`, "error");
      startBtn.disabled = false;
      startBtn.textContent = "开始下载";
    }
  });

  footer.appendChild(cancelBtn);
  footer.appendChild(startBtn);
  modal.appendChild(footer);

  overlay.appendChild(modal);

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) closeDownloadModal();
  });

  document.body.appendChild(overlay);
}

function closeDownloadModal() {
  if (!activeDownloadOverlay) return;
  activeDownloadOverlay.remove();
  activeDownloadOverlay = null;
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Drag to Canvas
// ─────────────────────────────────────────────────────────────

function enableCardDragToCanvas(cardEl, task) {
  const ns = normalizeStatus(task.status);
  const isImg = isImageTask(task);
  if (ns !== "success" || !isImg) return;

  const imageUrls = resolveImageUrls(task);
  if (imageUrls.length === 0) return;

  cardEl.draggable = true;
  cardEl.addEventListener("dragstart", (e) => {
    const url = imageUrls[0];
    e.dataTransfer.setData("text/uri-list", url);
    e.dataTransfer.setData("text/plain", url);
    e.dataTransfer.effectAllowed = "copy";
  });
}

// ─────────────────────────────────────────────────────────────
// Phase 2: Multi-select & Batch Operations
// ─────────────────────────────────────────────────────────────

function toggleTaskSelection(taskId, selected) {
  const id = String(taskId || "").trim();
  if (!id) return;
  if (selected) {
    state.selectedIds.add(id);
  } else {
    state.selectedIds.delete(id);
  }
  updateBatchToolbar();
}

function toggleSelectAll(selectAll) {
  const filtered = getFilteredTasks();
  if (selectAll) {
    for (const t of filtered) {
      const id = String(t.id || "").trim();
      if (id) state.selectedIds.add(id);
    }
  } else {
    state.selectedIds.clear();
  }
  renderCards();
  updateBatchToolbar();
}

function updateBatchToolbar() {
  if (!batchToolbarEl) return;
  const count = state.selectedIds.size;
  if (count > 0) {
    batchToolbarEl.classList.add("visible");
    const countEl = batchToolbarEl.querySelector(".banana-tc-batch-count");
    if (countEl) countEl.textContent = `已选 ${count} 项`;
  } else {
    batchToolbarEl.classList.remove("visible");
  }

  // Update select-all checkbox state
  const selectAllCb = batchToolbarEl.querySelector(".banana-tc-select-all-cb");
  if (selectAllCb) {
    const filtered = getFilteredTasks();
    const allIds = filtered.map((t) => String(t.id || "").trim()).filter(Boolean);
    const allSelected = allIds.length > 0 && allIds.every((id) => state.selectedIds.has(id));
    selectAllCb.checked = allSelected;
    selectAllCb.indeterminate = !allSelected && allIds.some((id) => state.selectedIds.has(id));
  }

  // Sync card checkboxes visually
  if (cardsContainerEl) {
    const cards = cardsContainerEl.querySelectorAll(".banana-tc-card");
    cards.forEach((card) => {
      const cb = card.querySelector(".banana-tc-card-checkbox");
      const id = card.dataset.taskId;
      if (cb && id) {
        const isSelected = state.selectedIds.has(id);
        cb.checked = isSelected;
        cb.classList.toggle("checked", isSelected);
        card.classList.toggle("selected", isSelected);
      }
    });
  }
}

async function batchDeleteSelected() {
  const ids = Array.from(state.selectedIds);
  if (ids.length === 0) return;
  if (!confirm(`确认删除 ${ids.length} 个任务？`)) return;
  try {
    await postJson(API_DELETE, { task_ids: ids });
    state.selectedIds.clear();
    showToast(`已删除 ${ids.length} 个任务`);
    void fetchAndRender();
    updateBatchToolbar();
  } catch (err) {
    showToast(`批量删除失败: ${err?.message || err}`, "error");
  }
}

async function batchRetrySelected() {
  const ids = Array.from(state.selectedIds);
  const failedIds = ids.filter((id) => {
    const task = state.tasks.find((t) => String(t.id || "") === id);
    return task && normalizeStatus(task.status) === "failed";
  });
  if (failedIds.length === 0) {
    showToast("选中的任务中没有失败的任务", "error");
    return;
  }
  let successCount = 0;
  let failCount = 0;
  const results = await Promise.allSettled(
    failedIds.map((id) => postJson(API_REFRESH, { id }))
  );
  for (const r of results) {
    if (r.status === "fulfilled") successCount++;
    else failCount++;
  }
  if (failCount > 0) {
    showToast(`重试: ${successCount} 成功, ${failCount} 失败`, "error");
  } else {
    showToast(`已重试 ${successCount} 个任务`);
  }
  void fetchAndRender();
}

function batchDownloadSelected() {
  const ids = Array.from(state.selectedIds);
  const successIds = ids.filter((id) => {
    const task = state.tasks.find((t) => String(t.id || "") === id);
    return task && normalizeStatus(task.status) === "success";
  });
  if (successIds.length === 0) {
    showToast("选中的任务中没有已完成的任务", "error");
    return;
  }
  openDownloadModal(successIds);
}

// ─────────────────────────────────────────────────────────────
// Card Rendering — helpers for incremental update
// ─────────────────────────────────────────────────────────────

function snapshotTask(task) {
  // Use lightweight keys to detect changes — avoid resolveImageUrls per snapshot
  return {
    status: task.status,
    progress: task.progress,
    prompt: task.prompt,
    model: task.model || task.provider || "",
    error: task.error || "",
    // Use raw identifiers instead of resolving URLs (cheaper)
    local_file_key: task.local_file?.filename || "",
    local_files_len: Array.isArray(task.local_files) ? task.local_files.length : 0,
    image_count: task.image_count ?? 0,
    image_urls_len: Array.isArray(task.image_urls) ? task.image_urls.length : 0,
  };
}

function hasTaskChanged(snap, task) {
  if (!snap) return true;
  return (
    snap.status !== task.status ||
    snap.progress !== task.progress ||
    snap.prompt !== task.prompt ||
    snap.model !== (task.model || task.provider || "") ||
    snap.error !== (task.error || "") ||
    snap.local_file_key !== (task.local_file?.filename || "") ||
    snap.local_files_len !== (Array.isArray(task.local_files) ? task.local_files.length : 0) ||
    snap.image_count !== (task.image_count ?? 0) ||
    snap.image_urls_len !== (Array.isArray(task.image_urls) ? task.image_urls.length : 0)
  );
}

function renderCard(task) {
  const card = document.createElement("div");
  card.className = "banana-tc-card";
  const ns = normalizeStatus(task.status);
  const isImg = isImageTask(task);
  const taskId = String(task.id || "").trim();
  // Pre-compute once, reuse everywhere in this card
  const imageUrls = isImg ? resolveImageUrls(task) : [];

  // RISK: Gradient surface based on task type
  card.classList.add(isImg ? "image-type" : "video-type");

  // Pinned indicator
  if (taskId && getPinnedTaskIds().has(taskId)) {
    card.classList.add("pinned");
  }

  if (ns === "failed") card.classList.add("failed");

  // Store task ID for batch selection
  card.dataset.taskId = taskId;

  // Multi-select state
  if (state.selectedIds.has(taskId)) {
    card.classList.add("selected");
  }

  // Drag-to-canvas
  enableCardDragToCanvas(card, task);

  // ── Preview Area ──
  const preview = document.createElement("div");
  preview.className = "banana-tc-card-preview";

  if (ns === "pending") {
    preview.classList.add("pending");
  } else if (ns === "processing") {
    preview.classList.add(isImg ? "processing-image" : "processing-video");
  } else if (ns === "success") {
    preview.classList.add(isImg ? "success-image" : "success-video");
  } else if (ns === "failed") {
    preview.classList.add("failed");
  }

  // Multi-select checkbox (top-left, appears on hover or when selected)
  if (taskId) {
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.className = "banana-tc-card-checkbox";
    if (state.selectedIds.has(taskId)) {
      checkbox.checked = true;
      checkbox.classList.add("checked");
    }
    // Click handled by delegated listener on cardsContainerEl
    preview.appendChild(checkbox);
  }

  // Type tag (top-left, shifts right when checkbox visible)
  const typeTag = document.createElement("span");
  typeTag.className = `banana-tc-type-tag ${isImg ? "image" : "video"}`;
  typeTag.textContent = isImg ? "\u56FE\u7247" : "\u89C6\u9891";
  preview.appendChild(typeTag);

  // Status tag (top-right)
  const statusTag = document.createElement("span");
  statusTag.className = `banana-tc-status-tag ${ns}`;
  if (ns === "pending") {
    statusTag.textContent = "\u7B49\u5F85";
  } else if (ns === "processing") {
    const pct = typeof task.progress === "number" ? `${Math.round(task.progress)}%` : "";
    statusTag.textContent = pct ? `\u751F\u6210\u4E2D ${pct}` : "\u751F\u6210\u4E2D";
  } else if (ns === "success") {
    statusTag.textContent = "\u2713 \u5B8C\u6210";
  } else if (ns === "failed") {
    statusTag.textContent = "\u2717 \u5931\u8D25";
  }
  preview.appendChild(statusTag);

  // Central content based on state
  if (ns === "pending") {
    const spinner = document.createElement("div");
    spinner.className = "banana-tc-spinner";
    spinner.style.opacity = "0.5";
    preview.appendChild(spinner);
    const txt = document.createElement("div");
    txt.className = "banana-tc-pending-text";
    txt.textContent = "\u7B49\u5F85\u5904\u7406";
    txt.style.marginTop = "8px";
    preview.appendChild(txt);
  } else if (ns === "processing") {
    const spinner = document.createElement("div");
    spinner.className = `banana-tc-spinner${isImg ? "" : " video"}`;
    preview.appendChild(spinner);
  } else if (ns === "success" && isImg) {
    // Image task success: try to show thumbnail (prefer local files)
    const imageCount = task.image_count || imageUrls.length || 0;
    if (imageUrls.length > 0) {
      const img = document.createElement("img");
      img.className = "banana-tc-thumbnail";
      img.loading = "lazy";
      img.src = imageUrls[0];
      img.alt = "preview";
      img.onerror = () => {
        img.style.display = "none";
      };
      preview.appendChild(img);
    }
    if (imageCount > 0) {
      const countBadge = document.createElement("span");
      countBadge.className = "banana-tc-image-count";
      countBadge.textContent = `${imageCount} \u5F20`;
      preview.appendChild(countBadge);
    }
  } else if (ns === "success" && !isImg) {
    // Video task success: play icon
    const play = document.createElement("div");
    play.className = "banana-tc-play-icon";
    play.textContent = "\u25B6";
    preview.appendChild(play);
  } else if (ns === "failed") {
    const warn = document.createElement("div");
    warn.className = "banana-tc-fail-icon";
    warn.textContent = "\u26A0";
    preview.appendChild(warn);
  }

  // ── Hover quick actions (bottom of preview area) ──
  {
    const actions = document.createElement("div");
    actions.className = "banana-tc-card-actions";
    if (ns === "success" && isImg) {
      const pushBtn = document.createElement("button");
      pushBtn.className = "banana-tc-card-action-btn primary";
      pushBtn.textContent = "\u53D1\u9001\u5230\u753B\u5E03";
      pushBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (imageUrls.length === 0) return;
        void pushToCanvas(imageUrls[0]);
      });
      actions.appendChild(pushBtn);
    }
    if (ns === "failed" && taskId) {
      const retryBtn = document.createElement("button");
      retryBtn.className = "banana-tc-card-action-btn";
      retryBtn.textContent = "\u91CD\u8BD5";
      retryBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        await postJson(API_REFRESH, { id: taskId });
        showToast("\u5DF2\u89E6\u53D1\u91CD\u8BD5");
        void fetchAndRender();
      });
      actions.appendChild(retryBtn);
    }
    if (ns === "success" && !isImg && task.video_url) {
      const openBtn = document.createElement("button");
      openBtn.className = "banana-tc-card-action-btn primary";
      openBtn.textContent = "\u6253\u5F00\u89C6\u9891";
      openBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        window.open(task.video_url, "_blank");
      });
      actions.appendChild(openBtn);
    }
    if (actions.children.length > 0) {
      preview.appendChild(actions);
    }
  }

  card.appendChild(preview);

  // ── Info Area ──
  const info = document.createElement("div");
  info.className = "banana-tc-card-info";

  // Model + time row
  const meta = document.createElement("div");
  meta.className = "banana-tc-card-meta";

  const modelEl = document.createElement("span");
  modelEl.className = "banana-tc-card-model";
  modelEl.textContent = task.model || task.provider || "-";
  modelEl.title = task.model || task.provider || "";

  const timeEl = document.createElement("span");
  timeEl.className = "banana-tc-card-time";
  timeEl.textContent = task.created_at ? relativeTime(task.created_at) : "-";

  meta.appendChild(modelEl);
  meta.appendChild(timeEl);
  info.appendChild(meta);

  // Progress bar (only for processing status)
  if (ns === "processing") {
    const progress = document.createElement("div");
    progress.className = "banana-tc-progress";
    const fill = document.createElement("div");
    fill.className = `banana-tc-progress-fill ${isImg ? "image" : "video"}`;
    const pct = typeof task.progress === "number" ? Math.min(100, Math.max(0, task.progress)) : 0;
    fill.style.width = `${pct}%`;
    progress.appendChild(fill);
    info.appendChild(progress);
  }

  // Prompt summary (single line, ellipsis)
  const prompt = document.createElement("div");
  prompt.className = "banana-tc-prompt";
  prompt.textContent = task.prompt || (ns === "pending" ? "\u5DF2\u63D0\u4EA4\u5230\u4EFB\u52A1\u4E2D\u5FC3" : task.error || "");
  prompt.title = task.prompt || "";
  info.appendChild(prompt);

  card.appendChild(info);

  // Card click delegated to cardsContainerEl

  return card;
}

// ─────────────────────────────────────────────────────────────
// Panel Rendering
// ─────────────────────────────────────────────────────────────

function renderCards() {
  if (!cardsContainerEl) return;
  _renderTick++;

  const filtered = getFilteredTasks();

  // Detect filter/sort/search change → full rebuild; otherwise incremental
  const currentFilterKey = `${state.activeTab}|${state.statusFilter}|${state.sortMode}|${state.searchQuery}`;
  const filterChanged = currentFilterKey !== lastFilterKey;
  lastFilterKey = currentFilterKey;

  if (filtered.length === 0) {
    // ── Empty state ──
    cardElementMap.clear();
    cardsContainerEl.innerHTML = "";
    const empty = document.createElement("div");
    empty.className = "banana-tc-empty";
    if (state.tasks.length === 0) {
      empty.innerHTML = '<svg class="banana-tc-empty-illustration" viewBox="0 0 120 120" fill="none">'
        + '<rect x="30" y="20" width="60" height="50" rx="4" stroke="#3a3a55" stroke-width="1.5" fill="none"/>'
        + '<line x1="60" y1="70" x2="40" y2="105" stroke="#3a3a55" stroke-width="1.5" stroke-linecap="round"/>'
        + '<line x1="60" y1="70" x2="80" y2="105" stroke="#3a3a55" stroke-width="1.5" stroke-linecap="round"/>'
        + '<line x1="48" y1="90" x2="72" y2="90" stroke="#3a3a55" stroke-width="1.5" stroke-linecap="round"/>'
        + '<circle cx="45" cy="38" r="2" fill="#e2a93b" opacity="0.6"/>'
        + '<circle cx="65" cy="45" r="1.5" fill="#e2a93b" opacity="0.4"/>'
        + '<circle cx="78" cy="33" r="1" fill="#e2a93b" opacity="0.3"/>'
        + '<path d="M42 50 Q52 42 62 48 Q72 54 82 46" stroke="#e2a93b" stroke-width="1.5" stroke-linecap="round" fill="none" opacity="0.35"/>'
        + '</svg>'
        + '<div class="banana-tc-empty-title">\u753B\u5E03\u7B49\u5F85\u4F60\u7684\u7075\u611F</div>'
        + '<div class="banana-tc-empty-hint">\u8FD0\u884C\u751F\u56FE\u6216\u751F\u89C6\u9891\u8282\u70B9\uFF0C\u4F5C\u54C1\u4F1A\u81EA\u52A8\u51FA\u73B0\u5728\u8FD9\u91CC</div>';
    } else {
      empty.innerHTML = '<div class="banana-tc-empty-icon">\u2014</div>'
        + '<div>\u6CA1\u6709\u5339\u914D\u7684\u4EFB\u52A1</div>';
    }
    cardsContainerEl.appendChild(empty);
  } else if (filterChanged) {
    // ── Filter changed: full rebuild (safe fallback) ──
    cardElementMap.clear();
    cardsContainerEl.innerHTML = "";
    for (let i = 0; i < filtered.length; i++) {
      const task = filtered[i];
      const taskId = String(task.id || "").trim();
      const card = renderCard(task);
      // SAFE: Staggered entrance animation (only on filter rebuild, not incremental updates)
      card.classList.add("tc-enter");
      card.style.animationDelay = `${i * 30}ms`;
      cardsContainerEl.appendChild(card);
      if (taskId) cardElementMap.set(taskId, { element: card, snapshot: snapshotTask(task) });
    }
  } else if (filtered.some((t) => !String(t.id || "").trim())) {
    // ── Fallback: tasks with empty id cannot be tracked — full rebuild ──
    cardElementMap.clear();
    cardsContainerEl.innerHTML = "";
    for (const task of filtered) {
      cardsContainerEl.appendChild(renderCard(task));
    }
  } else {
    // ── Incremental update: only mutate what changed ──
    const newIds = filtered.map((t) => String(t.id || "").trim());
    const newIdSet = new Set(newIds);

    // Remove cards that are no longer in filtered list
    for (const [id, entry] of cardElementMap) {
      if (!newIdSet.has(id)) {
        entry.element.remove();
        cardElementMap.delete(id);
      }
    }

    // Add / update / reorder
    let prevElement = null;
    for (const task of filtered) {
      const taskId = String(task.id || "").trim();
      const existing = taskId ? cardElementMap.get(taskId) : null;

      if (existing) {
        // Update in-place if task data changed
        if (hasTaskChanged(existing.snapshot, task)) {
          const newCard = renderCard(task);
          existing.element.replaceWith(newCard);
          cardElementMap.set(taskId, { element: newCard, snapshot: snapshotTask(task) });
          prevElement = newCard;
        } else {
          // Reorder if necessary
          const expectedNext = prevElement
            ? prevElement.nextElementSibling
            : cardsContainerEl.firstElementChild;
          if (existing.element !== expectedNext) {
            cardsContainerEl.insertBefore(existing.element, expectedNext);
          }
          prevElement = existing.element;
        }
      } else {
        // New card
        const newCard = renderCard(task);
        const refNode = prevElement ? prevElement.nextSibling : cardsContainerEl.firstChild;
        cardsContainerEl.insertBefore(newCard, refNode);
        if (taskId) cardElementMap.set(taskId, { element: newCard, snapshot: snapshotTask(task) });
        prevElement = newCard;
      }
    }
  }

  // ── Update chrome (tabs, chips, footer) ──
  updateTabBadges();
  updateStatusChips();
  updateFooterStats();
  updateBatchToolbar();
}

function updateTabBadges() {
  for (const [tab, el] of Object.entries(tabEls)) {
    const cnt = countByTab(tab);
    el.classList.toggle("active", state.activeTab === tab);
    const badgeSpan = el.querySelector(".banana-tc-badge-count");
    if (badgeSpan) badgeSpan.textContent = cnt;
  }
}

function updateStatusChips() {
  for (const [st, el] of Object.entries(statusChipEls)) {
    const cnt = countByStatus(st);
    el.classList.toggle("active", state.statusFilter === st);
    const countSpan = el.querySelector(".banana-tc-chip-count");
    if (countSpan) countSpan.textContent = cnt;
  }
}

function updateFooterStats() {
  if (footerLeftEl) {
    const daemonRunning = state.tasks.some(
      (t) => normalizeStatus(t.status) === "processing" || normalizeStatus(t.status) === "pending"
    );
    footerLeftEl.textContent = `\u5171 ${state.tasks.length} \u4E2A\u4EFB\u52A1 \u00B7 \u540E\u53F0${daemonRunning ? "\u5904\u7406\u4E2D" : "\u7A7A\u95F2"}`;
  }
}

// ─────────────────────────────────────────────────────────────
// Fetch & Render
// ─────────────────────────────────────────────────────────────

async function fetchAndRender() {
  if (isFetching) return;
  isFetching = true;
  try {
    const payload = await fetchJson(API_LIST, { method: "GET" });
    const data = payload?.data || {};
    const tasks = data.tasks || [];
    const settings = data.settings || {};

    state.tasks = tasks;

    // Sync auto-download checkbox with server settings
    if (autoDownloadCheckbox && typeof settings.auto_download === "boolean") {
      autoDownloadCheckbox.checked = settings.auto_download;
    }

    // Update key display in footer
    if (footerKeyEl) {
      const key = settings.api_key_masked || settings.api_key || "";
      footerKeyEl.textContent = key ? `Key: ${maskKey(key)}` : "Key: \u672A\u8BBE\u7F6E";
    }

    renderCards();
    computeBadge();
    // SAFE: Floating button breathing pulse when tasks are active
    if (floatingBtnEl) {
      const hasActive = state.tasks.some((t) => {
        const s = (t.status || "").toLowerCase();
        return s === "pending" || s === "processing";
      });
      floatingBtnEl.classList.toggle("has-active", hasActive);
    }
    setStatusText("\u5DF2\u5237\u65B0");
  } catch (err) {
    setStatusText(`\u83B7\u53D6\u4EFB\u52A1\u5931\u8D25: ${err?.message || err}`);
  } finally {
    isFetching = false;
  }
}

// ─────────────────────────────────────────────────────────────
// Draggable helper
// ─────────────────────────────────────────────────────────────

function makeDraggable(handle, target, storageKey, { onClick } = {}) {
  let dragging = false;
  let pointerId = null;
  let offsetX = 0;
  let offsetY = 0;
  let startLeft = 0;
  let startTop = 0;
  let moved = false;

  const endDrag = (cancelled = false) => {
    if (!dragging) return;
    dragging = false;
    pointerId = null;
    if (moved) {
      const rect = target.getBoundingClientRect();
      writeStorageJson(storageKey, { left: Math.round(rect.left), top: Math.round(rect.top) });
      return;
    }
    if (!cancelled && onClick) onClick();
  };

  handle.addEventListener("pointerdown", (event) => {
    // #10 修复：排除 handle 自身，只过滤 handle 内部的交互子元素
    const closestInteractive = event.target && event.target.closest("button,input,select,a,label");
    if (closestInteractive && closestInteractive !== handle) return;
    if (typeof event.button === "number" && event.button !== 0) return;
    const rect = target.getBoundingClientRect();
    dragging = true;
    moved = false;
    pointerId = event.pointerId;
    offsetX = event.clientX - rect.left;
    offsetY = event.clientY - rect.top;
    startLeft = rect.left;
    startTop = rect.top;
    setFixedPosition(target, rect.left, rect.top);
    try { handle.setPointerCapture(pointerId); } catch (_) { /* ignore */ }
  });

  handle.addEventListener("pointermove", (event) => {
    if (!dragging || event.pointerId !== pointerId) return;
    const nextLeft = event.clientX - offsetX;
    const nextTop = event.clientY - offsetY;
    const clamped = clampToViewport(target, nextLeft, nextTop);
    setFixedPosition(target, clamped.left, clamped.top);
    const dx = clamped.left - startLeft;
    const dy = clamped.top - startTop;
    if (!moved && Math.abs(dx) + Math.abs(dy) >= DRAG_THRESHOLD_PX) moved = true;
  });

  handle.addEventListener("pointerup", (event) => {
    if (!dragging || event.pointerId !== pointerId) return;
    try { handle.releasePointerCapture(pointerId); } catch (_) { /* ignore */ }
    endDrag(false);
  });

  handle.addEventListener("pointercancel", (event) => {
    if (!dragging || event.pointerId !== pointerId) return;
    endDrag(true);
  });
}

// ─────────────────────────────────────────────────────────────
// Panel Construction
// ─────────────────────────────────────────────────────────────

function ensureOverlay() {
  if (overlayEl) return overlayEl;

  injectStyles();

  // ── Overlay ──
  overlayEl = document.createElement("div");
  overlayEl.className = "banana-tc-overlay";
  overlayEl.addEventListener("click", (e) => {
    if (e.target === overlayEl) hideOverlay();
  });

  // ── Panel ──
  panelEl = document.createElement("div");
  panelEl.className = "banana-tc-panel";

  // ── Header (draggable title bar) ──
  const header = document.createElement("div");
  header.className = "banana-tc-header";

  const title = document.createElement("div");
  title.className = "banana-tc-header-title";
  title.textContent = "\u5FC3\u5B9D\u4EFB\u52A1\u4E2D\u5FC3";

  const headerActions = document.createElement("div");
  headerActions.className = "banana-tc-header-actions";

  const refreshBtn = document.createElement("button");
  refreshBtn.className = "banana-tc-btn header";
  refreshBtn.textContent = "\u5237\u65B0";
  refreshBtn.addEventListener("click", () => void fetchAndRender());

  const closeBtn = document.createElement("button");
  closeBtn.className = "banana-tc-btn header";
  closeBtn.textContent = "\u5173\u95ED";
  closeBtn.addEventListener("click", () => hideOverlay());

  headerActions.appendChild(refreshBtn);
  headerActions.appendChild(closeBtn);
  header.appendChild(title);
  header.appendChild(headerActions);

  // Panel drag via header
  makeDraggable(header, panelEl, LS_PANEL_POS);

  // ── Batch Toolbar ──
  batchToolbarEl = document.createElement("div");
  batchToolbarEl.className = "banana-tc-batch-toolbar";

  // Select-all checkbox
  const selectAllCb = document.createElement("input");
  selectAllCb.type = "checkbox";
  selectAllCb.className = "banana-tc-select-all-cb";
  selectAllCb.style.cssText = "accent-color:#e2a93b;cursor:pointer;";
  selectAllCb.addEventListener("change", () => toggleSelectAll(selectAllCb.checked));
  batchToolbarEl.appendChild(selectAllCb);

  // Selected count
  const batchCount = document.createElement("span");
  batchCount.className = "banana-tc-batch-count";
  batchCount.textContent = "\u5DF2\u9009 0 \u9879";
  batchToolbarEl.appendChild(batchCount);

  // Batch download
  const batchDlBtn = document.createElement("button");
  batchDlBtn.className = "banana-tc-btn primary";
  batchDlBtn.textContent = "\u6279\u91CF\u4E0B\u8F7D";
  batchDlBtn.addEventListener("click", () => batchDownloadSelected());
  batchToolbarEl.appendChild(batchDlBtn);

  // Batch retry
  const batchRetryBtn = document.createElement("button");
  batchRetryBtn.className = "banana-tc-btn";
  batchRetryBtn.textContent = "\u6279\u91CF\u91CD\u8BD5";
  batchRetryBtn.addEventListener("click", () => void batchRetrySelected());
  batchToolbarEl.appendChild(batchRetryBtn);

  // Batch delete
  const batchDelBtn = document.createElement("button");
  batchDelBtn.className = "banana-tc-btn danger";
  batchDelBtn.textContent = "\u6279\u91CF\u5220\u9664";
  batchDelBtn.addEventListener("click", () => void batchDeleteSelected());
  batchToolbarEl.appendChild(batchDelBtn);

  // Compare view
  const batchCompareBtn = document.createElement("button");
  batchCompareBtn.className = "banana-tc-btn";
  batchCompareBtn.textContent = "\u5BF9\u6BD4\u89C6\u56FE";
  batchCompareBtn.addEventListener("click", () => openCompareView());
  batchToolbarEl.appendChild(batchCompareBtn);

  // Cancel selection
  const batchCancelBtn = document.createElement("button");
  batchCancelBtn.className = "banana-tc-btn";
  batchCancelBtn.textContent = "\u53D6\u6D88\u9009\u62E9";
  batchCancelBtn.addEventListener("click", () => toggleSelectAll(false));
  batchToolbarEl.appendChild(batchCancelBtn);

  // ── Settings Drawer (hidden by default) ──
  settingsDrawerEl = document.createElement("div");
  settingsDrawerEl.className = "banana-tc-settings-drawer";

  const settingsGrid = document.createElement("div");
  settingsGrid.className = "banana-tc-settings-grid";

  // Key section
  const keySection = document.createElement("div");
  keySection.className = "banana-tc-settings-section";

  const keyTitle = document.createElement("div");
  keyTitle.className = "banana-tc-settings-title";
  keyTitle.textContent = "API Key\uFF08\u4E0D\u843D\u76D8\uFF1B\u901A\u7528\uFF09";
  keySection.appendChild(keyTitle);

  const keyRow = document.createElement("div");
  keyRow.className = "banana-tc-key-row";

  const keyInput = document.createElement("input");
  keyInput.type = "password";
  keyInput.className = "banana-tc-key-input";
  keyInput.placeholder = "\u8F93\u5165 Key\uFF08Sora/Veo/\u8C46\u5305/\u56FE\u7247 \u901A\u7528\uFF09";

  const keySaveBtn = document.createElement("button");
  keySaveBtn.className = "banana-tc-btn primary";
  keySaveBtn.textContent = "\u4FDD\u5B58";
  keySaveBtn.addEventListener("click", async () => {
    const value = String(keyInput.value || "").trim();
    if (!value) return;
    // Send to all 4 providers simultaneously
    const providers = ["sora", "veo", "doubao", "banana_v3"];
    try {
      const results = await Promise.allSettled(
        providers.map((provider) => postJson(API_KEY, { provider, api_key: value }))
      );
      const failed = results
        .map((res, idx) => ({ res, provider: providers[idx] }))
        .filter((item) => item.res.status === "rejected");
      if (!failed.length) {
        keyInput.value = "";
        setStatusText("Key \u5DF2\u4FDD\u5B58\uFF08\u4EC5\u672C\u6B21\u4F1A\u8BDD\uFF09");
      } else {
        const providerList = failed.map((item) => item.provider).join("/");
        const firstErr = failed[0].res.reason;
        setStatusText(`Key \u4FDD\u5B58\u90E8\u5206\u5931\u8D25\uFF08${providerList}\uFF09: ${firstErr?.message || firstErr || "\u672A\u77E5\u9519\u8BEF"}`);
      }
      void fetchAndRender();
    } catch (err) {
      setStatusText(`\u4FDD\u5B58 Key \u5931\u8D25: ${err?.message || err}`);
    }
  });

  keyRow.appendChild(keyInput);
  keyRow.appendChild(keySaveBtn);
  keySection.appendChild(keyRow);

  // Settings section
  const settingsSection = document.createElement("div");
  settingsSection.className = "banana-tc-settings-section";

  const settingsTitle = document.createElement("div");
  settingsTitle.className = "banana-tc-settings-title";
  settingsTitle.textContent = "\u8BBE\u7F6E";
  settingsSection.appendChild(settingsTitle);

  const autoLabel = document.createElement("label");
  autoLabel.className = "banana-tc-auto-dl-label";
  autoDownloadCheckbox = document.createElement("input");
  autoDownloadCheckbox.type = "checkbox";
  autoDownloadCheckbox.addEventListener("change", async () => {
    try {
      const enabled = !!autoDownloadCheckbox.checked;
      await postJson(API_SETTINGS, { auto_download: enabled });
      setStatusText(`\u81EA\u52A8\u4E0B\u8F7D\u5DF2${enabled ? "\u5F00\u542F" : "\u5173\u95ED"}`);
    } catch (err) {
      setStatusText(`\u66F4\u65B0\u8BBE\u7F6E\u5931\u8D25: ${err?.message || err}`);
    }
  });
  const autoText = document.createElement("span");
  autoText.textContent = "\u81EA\u52A8\u4E0B\u8F7D\u5230 output/video_tasks";
  autoLabel.appendChild(autoDownloadCheckbox);
  autoLabel.appendChild(autoText);
  settingsSection.appendChild(autoLabel);

  statusTextEl = document.createElement("div");
  statusTextEl.className = "banana-tc-status-text";
  statusTextEl.textContent = "\u5C31\u7EEA";
  settingsSection.appendChild(statusTextEl);

  settingsGrid.appendChild(keySection);
  settingsGrid.appendChild(settingsSection);
  settingsDrawerEl.appendChild(settingsGrid);

  // ── Toolbar ──
  const toolbar = document.createElement("div");
  toolbar.className = "banana-tc-toolbar";

  // Row 1: Tabs + controls
  const toolbarRow1 = document.createElement("div");
  toolbarRow1.className = "banana-tc-toolbar-row";

  const tabs = document.createElement("div");
  tabs.className = "banana-tc-tabs";

  const tabDefs = [
    { key: "all", label: "\u5168\u90E8" },
    { key: "image", label: "\u56FE\u7247" },
    { key: "video", label: "\u89C6\u9891" },
  ];
  for (const def of tabDefs) {
    const tab = document.createElement("span");
    tab.className = "banana-tc-tab";
    tab.innerHTML = `${def.label} <span class="banana-tc-badge-count">0</span>`;
    tab.addEventListener("click", () => {
      state.activeTab = def.key;
      state.statusFilter = "all"; // reset status filter on tab change
      renderCards();
    });
    tabs.appendChild(tab);
    tabEls[def.key] = tab;
  }

  const controls = document.createElement("div");
  controls.style.cssText = "display:flex;gap:8px;align-items:center;flex-wrap:wrap;";

  searchInputEl = document.createElement("input");
  searchInputEl.className = "banana-tc-search";
  searchInputEl.placeholder = "\u641C\u7D22 prompt / model / id";
  searchInputEl.addEventListener("input", debounce(() => {
    state.searchQuery = searchInputEl.value;
    renderCards();
  }, 250));

  sortSelectEl = document.createElement("select");
  sortSelectEl.className = "banana-tc-sort";
  const sortOptions = [
    { value: "newest", label: "\u6700\u65B0\u4F18\u5148" },
    { value: "oldest", label: "\u6700\u65E9\u4F18\u5148" },
    { value: "status", label: "\u72B6\u6001\u4F18\u5148" },
  ];
  for (const opt of sortOptions) {
    const o = document.createElement("option");
    o.value = opt.value;
    o.textContent = opt.label;
    sortSelectEl.appendChild(o);
  }
  sortSelectEl.value = state.sortMode;
  sortSelectEl.addEventListener("change", () => {
    state.sortMode = sortSelectEl.value;
    renderCards();
  });

  const autoRefreshLabel = document.createElement("label");
  autoRefreshLabel.className = "banana-tc-auto-refresh";
  autoRefreshCheckboxEl = document.createElement("input");
  autoRefreshCheckboxEl.type = "checkbox";
  autoRefreshCheckboxEl.checked = state.autoRefresh;
  autoRefreshCheckboxEl.addEventListener("change", () => {
    state.autoRefresh = autoRefreshCheckboxEl.checked;
    restartPoller(); // 自动刷新开关变化时重新调度
  });
  const arText = document.createElement("span");
  arText.textContent = "\u81EA\u52A8\u5237\u65B0";
  autoRefreshLabel.appendChild(autoRefreshCheckboxEl);
  autoRefreshLabel.appendChild(arText);

  controls.appendChild(searchInputEl);
  controls.appendChild(sortSelectEl);
  controls.appendChild(autoRefreshLabel);

  toolbarRow1.appendChild(tabs);
  toolbarRow1.appendChild(controls);

  // Row 2: Status filters
  const toolbarRow2 = document.createElement("div");
  toolbarRow2.className = "banana-tc-toolbar-row";

  const statusFilters = document.createElement("div");
  statusFilters.className = "banana-tc-status-filters";

  const statusDefs = [
    { key: "all", label: "\u5168\u90E8" },
    { key: "processing", label: "\u8FDB\u884C\u4E2D" },
    { key: "success", label: "\u5DF2\u5B8C\u6210" },
    { key: "failed", label: "\u5931\u8D25" },
    { key: "pending", label: "\u7B49\u5F85\u4E2D" },
  ];
  for (const def of statusDefs) {
    const chip = document.createElement("span");
    chip.className = "banana-tc-status-chip";
    chip.innerHTML = `${def.label} <span class="banana-tc-chip-count">0</span>`;
    chip.addEventListener("click", () => {
      state.statusFilter = def.key;
      renderCards();
    });
    statusFilters.appendChild(chip);
    statusChipEls[def.key] = chip;
  }

  toolbarRow2.appendChild(statusFilters);

  toolbar.appendChild(toolbarRow1);
  toolbar.appendChild(toolbarRow2);

  // ── Cards Scroll Area ──
  const cardsScroll = document.createElement("div");
  cardsScroll.className = "banana-tc-cards-scroll";

  cardsContainerEl = document.createElement("div");
  cardsContainerEl.className = "banana-tc-cards";

  // Event delegation: single listener for all card clicks & checkboxes
  cardsContainerEl.addEventListener("click", (e) => {
    // Checkbox click — browser has already toggled .checked before this handler runs
    const checkbox = e.target.closest(".banana-tc-card-checkbox");
    if (checkbox) {
      e.stopPropagation();
      const cardEl = checkbox.closest(".banana-tc-card");
      const taskId = cardEl?.dataset?.taskId;
      if (taskId) {
        toggleTaskSelection(taskId, checkbox.checked);
        checkbox.classList.toggle("checked", checkbox.checked);
        cardEl.classList.toggle("selected", checkbox.checked);
      }
      return;
    }

    // Quick action button click — don't propagate to card click
    if (e.target.closest(".banana-tc-card-action-btn")) return;

    // Card click → open detail
    const cardEl = e.target.closest(".banana-tc-card");
    if (!cardEl) return;
    const taskId = cardEl.dataset.taskId;
    const task = state.tasks.find((t) => String(t.id || "").trim() === taskId);
    if (task) openTaskDetail(task);
  });

  // ── Double-click to pin/unpin card ──
  cardsContainerEl.addEventListener("dblclick", (e) => {
    const cardEl = e.target.closest(".banana-tc-card");
    if (!cardEl) return;
    e.preventDefault();
    const taskId = cardEl.dataset.taskId;
    if (taskId) togglePin(taskId);
  });

  // ── Right-click context menu on cards ──
  cardsContainerEl.addEventListener("contextmenu", (e) => {
    const cardEl = e.target.closest(".banana-tc-card");
    if (!cardEl) return;
    e.preventDefault();
    const taskId = cardEl.dataset.taskId;
    const task = state.tasks.find((t) => String(t.id || "").trim() === taskId);
    if (task) showCtxMenu(e.clientX, e.clientY, buildCardCtxMenuItems(task));
  });

  cardsScroll.appendChild(cardsContainerEl);

  // ── Footer ──
  const footer = document.createElement("div");
  footer.className = "banana-tc-footer";

  footerLeftEl = document.createElement("div");
  footerLeftEl.className = "banana-tc-footer-left";
  footerLeftEl.textContent = "\u5171 0 \u4E2A\u4EFB\u52A1";

  const footerRight = document.createElement("div");
  footerRight.className = "banana-tc-footer-right";

  footerKeyEl = document.createElement("span");
  footerKeyEl.className = "banana-tc-footer-key";
  footerKeyEl.textContent = "Key: \u672A\u8BBE\u7F6E";

  const settingsBtn = document.createElement("button");
  settingsBtn.className = "banana-tc-footer-settings-btn";
  settingsBtn.textContent = "\u8BBE\u7F6E";
  settingsBtn.addEventListener("click", () => {
    settingsDrawerEl.classList.toggle("open");
  });

  footerRight.appendChild(footerKeyEl);
  footerRight.appendChild(settingsBtn);
  footer.appendChild(footerLeftEl);
  footer.appendChild(footerRight);

  // ── Assemble Panel ──
  // Structure: Header -> BatchToolbar(Phase2) -> SettingsDrawer -> Toolbar -> CardsScroll -> Footer
  panelEl.appendChild(header);
  panelEl.appendChild(batchToolbarEl);
  panelEl.appendChild(settingsDrawerEl);
  panelEl.appendChild(toolbar);
  panelEl.appendChild(cardsScroll);
  panelEl.appendChild(footer);
  overlayEl.appendChild(panelEl);
  document.body.appendChild(overlayEl);

  return overlayEl;
}

// ─────────────────────────────────────────────────────────────
// Unified Poller — adaptive frequency based on panel/tab state
// ─────────────────────────────────────────────────────────────
// 面板打开 + Tab 可见 → 5s (fetchAndRender)
// 面板关闭 + Tab 可见 → 15s (badge only)
// Tab 后台            → 30s (badge only, 保障自动下载)

let pollerTimer = null;
let pollerEnabled = true; // controlled by extension enable/disable

function isPanelOpen() {
  return !!(overlayEl && overlayEl.classList.contains("open"));
}

function getPollerInterval() {
  if (document.hidden) return POLL_BACKGROUND_MS;
  return isPanelOpen() ? POLL_ACTIVE_MS : POLL_IDLE_MS;
}

function pollerTick() {
  if (!pollerEnabled || isFetching) return;

  if (isPanelOpen() && state.autoRefresh) {
    // 面板打开：完整 fetch + render
    void fetchAndRender();
  } else {
    // 面板关闭或后台：仅更新 badge（轻量）
    void (async () => {
      isFetching = true;
      try {
        const payload = await fetchJson(API_LIST, { method: "GET" });
        state.tasks = payload?.data?.tasks || [];
        computeBadge();
      } catch (_) { /* ignore */ }
      finally { isFetching = false; }
    })();
  }
}

function restartPoller() {
  if (pollerTimer) window.clearInterval(pollerTimer);
  pollerTimer = null;
  if (!pollerEnabled) return;
  pollerTimer = window.setInterval(pollerTick, getPollerInterval());
}

function stopPoller() {
  if (pollerTimer) {
    window.clearInterval(pollerTimer);
    pollerTimer = null;
  }
}

// ─────────────────────────────────────────────────────────────
// Show / Hide
// ─────────────────────────────────────────────────────────────

function showOverlay() {
  ensureOverlay();
  overlayEl.classList.add("open");
  updateBadge(0);

  window.requestAnimationFrame(() => {
    try {
      applyStoredFixedPosition(panelEl, LS_PANEL_POS);
    } catch (_) { /* ignore */ }
  });

  void fetchAndRender();
  restartPoller(); // switch to faster interval
}

function hideOverlay() {
  if (overlayEl) overlayEl.classList.remove("open");
  restartPoller(); // switch to slower interval
}

// ─────────────────────────────────────────────────────────────
// Floating Button
// ─────────────────────────────────────────────────────────────

function ensureFloatingButton() {
  injectStyles();

  const btn = document.createElement("button");
  btn.className = "banana-tc-float-btn";
  btn.title = "\u5FC3\u5B9D\u4EFB\u52A1\u4E2D\u5FC3";

  // Icon + label
  const icon = createSvgIcon(ICON_PATHS.dashboard, 16, 1.8);
  icon.style.flexShrink = "0";
  btn.appendChild(icon);
  const label = document.createElement("span");
  label.textContent = "\u5FC3\u5B9D\u4EFB\u52A1\u4E2D\u5FC3";
  btn.appendChild(label);

  floatingBtnEl = btn;
  document.body.appendChild(btn);

  // Badge
  badgeEl = document.createElement("div");
  badgeEl.className = "banana-tc-float-badge";
  btn.appendChild(badgeEl);

  // Restore saved position
  window.requestAnimationFrame(() => {
    try {
      applyStoredFixedPosition(btn, LS_BUTTON_POS);
    } catch (_) { /* ignore */ }
  });

  // Draggable + click to open panel
  makeDraggable(btn, btn, LS_BUTTON_POS, { onClick: showOverlay });

  // Start unified poller
  restartPoller();

  // ── Global keyboard shortcuts ──
  document.addEventListener("keydown", (e) => {
    // Ctrl+Shift+T: toggle panel
    if (e.ctrlKey && e.shiftKey && e.key === "T") {
      e.preventDefault();
      if (isPanelOpen()) { hideOverlay(); } else { showOverlay(); }
      return;
    }
  });

  // Tab visibility: switch poller frequency (降频, 不停止 — 保障后台自动下载)
  document.addEventListener("visibilitychange", () => {
    restartPoller(); // interval 自动根据 document.hidden 调整
    if (!document.hidden && isPanelOpen()) {
      void fetchAndRender(); // 回前台立即刷新一次
    }
  });
}

// ─────────────────────────────────────────────────────────────
// Extension Registration
// ─────────────────────────────────────────────────────────────

app.registerExtension({
  name: EXTENSION,
  setup() {
    const SETTING_ID = `${EXTENSION}.enabled`;

    app.ui.settings.addSetting({
      id: SETTING_ID,
      name: "\u542F\u7528\u5FC3\u5B9D\u4EFB\u52A1\u4E2D\u5FC3",
      type: "boolean",
      defaultValue: true,
      onChange: (value) => {
        if (floatingBtnEl) {
          floatingBtnEl.style.display = value ? "flex" : "none";
        }
        pollerEnabled = !!value;
        if (value) {
          restartPoller();
        } else {
          stopPoller();
        }
      },
    });

    const mount = () => {
      try {
        ensureFloatingButton();
        if (floatingBtnEl) {
          const isEnabled = app.ui.settings.getSettingValue(SETTING_ID);
          floatingBtnEl.style.display = isEnabled ? "flex" : "none";
        }
      } catch (e) {
        console.warn(`[${EXTENSION}] \u60AC\u6D6E\u6309\u94AE\u6302\u8F7D\u5931\u8D25`, e);
      }
    };

    if (document.readyState === "complete" || document.readyState === "interactive") {
      setTimeout(mount, 0);
    } else {
      window.addEventListener("DOMContentLoaded", () => setTimeout(mount, 0), { once: true });
    }
  },
});
