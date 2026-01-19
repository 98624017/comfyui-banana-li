import { app } from "/scripts/app.js";

/**
 * 修复：选择“清空图片”后再次选择同一文件不触发上传/加载
 *
 * 根因：浏览器对 <input type="file"> 的 change 事件有特殊行为——
 * 当再次选择同一个文件时，如果 input.value 未变化，change 不会触发，
 * 上层逻辑就不会执行上传，从而表现为“加载失败”。
 *
 * 方案：在 file input 被点击（包含程序触发 input.click()）前，将 value 清空，
 * 这样无论选择哪个文件，都能确保 change 事件触发。
 *
 * 说明：该修复是通用的，对 ComfyUI 其它文件上传入口同样有益，且风险极低（KISS）。
 */

const EXTENSION_NAME = "Xinbao_Nodes.file_upload_same_file_fix";
const GLOBAL_GUARD_KEY = "__xinbao_file_upload_same_file_fix_installed__";

function installFixOnce() {
  if (globalThis[GLOBAL_GUARD_KEY]) return;
  globalThis[GLOBAL_GUARD_KEY] = true;

  document.addEventListener(
    "click",
    (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      if (target.type !== "file") return;

      // 关键：重置 value，确保选择同一文件也能触发 change。
      // 注意：仅允许赋值为空字符串（浏览器安全限制）。
      if (target.value) target.value = "";
    },
    true
  );
}

app.registerExtension({
  name: EXTENSION_NAME,
  async init() {
    installFixOnce();
  },
});

