import { app } from "../../../scripts/app.js";

const EXTENSION = "Banana.FailedUrlAggregator";
const TARGET_NODE = "BananaFailedUrlAggregator";
const INPUT_PREFIX = "failed_urls_";
const MAX_INPUTS = 20;
const MIN_INPUTS = 2; // 需与 Python 端 _MIN_INPUTS 保持一致
const INPUT_TYPE = "STRING";

/**
 * 获取当前所有 failed_urls_N 输入名称中最大的序号 N。
 */
function getMaxN(node) {
  if (!node || !Array.isArray(node.inputs)) return 0;
  let maxN = 0;
  for (const input of node.inputs) {
    if (input?.name?.startsWith(INPUT_PREFIX)) {
      const n = parseInt(input.name.slice(INPUT_PREFIX.length), 10);
      if (Number.isFinite(n) && n > maxN) maxN = n;
    }
  }
  return maxN;
}

/**
 * 计算当前已连接的 failed_urls_N 输入数量。
 */
function countConnectedInputs(node) {
  if (!node || !Array.isArray(node.inputs)) return 0;
  let count = 0;
  for (const input of node.inputs) {
    if (input?.name?.startsWith(INPUT_PREFIX) && input.link != null) {
      count++;
    }
  }
  return count;
}

/**
 * 获取当前所有 failed_urls_N 输入在 node.inputs 中的索引列表。
 */
function getFailedUrlInputIndices(node) {
  if (!node || !Array.isArray(node.inputs)) return [];
  const indices = [];
  for (let i = 0; i < node.inputs.length; i++) {
    if (node.inputs[i]?.name?.startsWith(INPUT_PREFIX)) {
      indices.push(i);
    }
  }
  return indices;
}

/**
 * 裁剪输入到指定数量（从尾部移除未连接的输入）。
 *
 * 安全性：每次移除后重新获取索引，避免 removeInput 导致索引失效。
 * 仅移除 link == null 的输入，已连接的输入不会被破坏。
 */
function trimInputsTo(node, targetCount) {
  if (!node || !Array.isArray(node.inputs)) return;
  let indices = getFailedUrlInputIndices(node);
  while (indices.length > targetCount) {
    const lastIdx = indices[indices.length - 1];
    const input = node.inputs[lastIdx];
    // 仅移除未连接的尾部输入，遇到已连接则停止
    if (input && input.link == null) {
      node.removeInput(lastIdx);
      indices = getFailedUrlInputIndices(node); // 重新获取索引
    } else {
      break;
    }
  }
}

/**
 * 确保节点有 targetCount 个 failed_urls_N 输入。
 * 从当前最大序号之后开始追加，避免序号冲突。
 */
function ensureInputCount(node, targetCount) {
  if (!node) return;
  let current = getFailedUrlInputIndices(node).length;
  const clamped = Math.max(MIN_INPUTS, Math.min(MAX_INPUTS, targetCount));
  let nextN = getMaxN(node) + 1;
  while (current < clamped) {
    node.addInput(`${INPUT_PREFIX}${nextN}`, INPUT_TYPE);
    nextN++;
    current++;
  }
}

/**
 * 核心逻辑：根据连接状态调整输入数量。
 * - 当所有 failed_urls 输入都已连接时，新增一个（不超过 MAX）。
 * - 当尾部存在多余的未连接输入时，收缩到 "最后一个已连接 + 1"（不低于 MIN）。
 */
function adjustInputs(node) {
  if (!node || !Array.isArray(node.inputs)) return;

  const indices = getFailedUrlInputIndices(node);
  const total = indices.length;
  const connected = countConnectedInputs(node);

  // 所有输入都已连接 → 新增一个空位
  if (connected >= total && total < MAX_INPUTS) {
    const nextN = getMaxN(node) + 1;
    node.addInput(`${INPUT_PREFIX}${nextN}`, INPUT_TYPE);
    node.setDirtyCanvas(true, true);
    return;
  }

  // 收缩：找到最后一个已连接输入的位置（在 indices 数组中的下标）
  let lastConnectedPos = -1;
  for (let i = indices.length - 1; i >= 0; i--) {
    const input = node.inputs[indices[i]];
    if (input && input.link != null) {
      lastConnectedPos = i;
      break;
    }
  }

  // 目标数量 = 最后已连接位置 + 2（保留一个空位），但不低于 MIN
  const desiredCount = Math.max(MIN_INPUTS, lastConnectedPos + 2);
  if (total > desiredCount) {
    // 安全收缩：每次移除后重新获取索引
    let currentIndices = getFailedUrlInputIndices(node);
    while (currentIndices.length > desiredCount) {
      const lastIdx = currentIndices[currentIndices.length - 1];
      const input = node.inputs[lastIdx];
      if (input && input.link == null) {
        node.removeInput(lastIdx);
        currentIndices = getFailedUrlInputIndices(node);
      } else {
        break; // 尾部已连接，停止收缩
      }
    }
    node.setDirtyCanvas(true, true);
  }
}

app.registerExtension({
  name: EXTENSION,

  beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData?.name !== TARGET_NODE) return;

    // 拦截 onNodeCreated：将 Python 声明的 20 个输入裁减到 MIN_INPUTS
    const originalOnNodeCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const result = originalOnNodeCreated?.apply(this, arguments);
      try {
        trimInputsTo(this, MIN_INPUTS);
      } catch (e) {
        console.warn("[BananaFailedUrlAggregator] trimInputsTo failed:", e);
      }
      return result;
    };

    // 拦截 onConnectionsChange：连接变化时动态调整输入数量
    const originalOnConnectionsChange = nodeType.prototype.onConnectionsChange;
    nodeType.prototype.onConnectionsChange = function (side) {
      const result = originalOnConnectionsChange?.apply(this, arguments);
      try {
        // side === 1 表示输入端（LiteGraph: INPUT = 1）
        if (side === 1) {
          adjustInputs(this);
        }
      } catch (e) {
        console.warn("[BananaFailedUrlAggregator] adjustInputs failed:", e);
      }
      return result;
    };

    // 拦截 onConfigure：从保存的工作流恢复时，根据保存的输入还原数量
    const originalOnConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function (info) {
      const result = originalOnConfigure?.apply(this, arguments);
      try {
        if (info && Array.isArray(info.inputs)) {
          // 计算保存的 failed_urls 输入中最大的序号
          let maxN = 0;
          for (const input of info.inputs) {
            if (input?.name?.startsWith(INPUT_PREFIX)) {
              const n = parseInt(input.name.slice(INPUT_PREFIX.length), 10);
              if (Number.isFinite(n) && n > maxN) maxN = n;
            }
          }
          // 确保至少有 maxN 个输入（恢复保存时的数量），然后安全裁剪多余的
          const target = Math.max(MIN_INPUTS, maxN);
          ensureInputCount(this, target);
          trimInputsTo(this, target);
        }
      } catch (e) {
        console.warn("[BananaFailedUrlAggregator] onConfigure restore failed:", e);
      }
      return result;
    };
  },
});
