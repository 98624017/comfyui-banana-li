import { app } from "../../../scripts/app.js";

/**
 * 修复：选择“清空图片”后再次选择同一文件不触发上传/加载
 *
 * 根因：浏览器对 <input type="file"> 的 change 事件有特殊行为——
 * 当再次选择同一个文件时，如果 input.value 未变化，change 不会触发。
 *
 * 方案 (V2 Monkey Patch)：
 * 劫持 HTMLInputElement.prototype.click，在点击前强制清空 value。
 * 这种方式可以兼容 ComfyUI 可能使用的 detached input (未挂载到 DOM 的元素)，
 * 确保无论如何触发上传，值都被重置。
 */

const EXTENSION_NAME = "Xinbao_Nodes.file_upload_same_file_fix";
const GLOBAL_GUARD_KEY = "__xinbao_file_upload_same_file_fix_installed__";

function installFixOnce() {
  if (globalThis[GLOBAL_GUARD_KEY]) return;
  globalThis[GLOBAL_GUARD_KEY] = true;

  // 方案 A: 劫持原型方法 (最强力，兼容 detached elements)
  const originalClick = HTMLInputElement.prototype.click;
  HTMLInputElement.prototype.click = function () {
    if (this.type === "file") {
      try {
        this.value = "";
      } catch (e) {
        // quiet failure
      }
    }
    return originalClick.apply(this, arguments);
  };

  // 方案 B: 保留全局监听作为兜底 (兼容用户手动点击 input 的情况，虽少见)
  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!target || target.tagName !== "INPUT" || target.type !== "file") return;
      try {
        target.value = "";
      } catch (e) { }
    },
    true
  );

  console.log("[Xinbao] File upload fix applied (Monkey Patch Mode).");
}

app.registerExtension({
  name: EXTENSION_NAME,
  async init() {
    installFixOnce();
  },
});
