
import { app } from "../../../scripts/app.js";


/**
 * 香蕉Banana 菜单置顶增强脚本
 * 
 * 作用：拦截 ComfyUI 的环境菜单（右键菜单、拖拽搜索菜单），
 * 将包含 "❤️‍🔥" 或 "Banana" 的关键词强制排在最前面。
 * 
 * 安全性：采用 AOP 切面拦截模式，不破坏原有功能，不覆盖其他插件的数据，仅调整顺序。
 */

app.registerExtension({
    name: "Comfy.Banana.MenuPriority",
    async setup() {
        // 确保 LiteGraph 已加载
        if (!window.LiteGraph) {
            return;
        }

        // 保存原始的 ContextMenu 构造函数
        const OriginalContextMenu = LiteGraph.ContextMenu;

        /**
         * 劫持 ContextMenu 构造函数
         * @param {Array} values 菜单项数组 (可能是字符串或对象)
         * @param {Object} options 配置项
         */
        LiteGraph.ContextMenu = function (...args) {
            const [values, options] = args;
            // 只有当 values 是数组时才尝试排序
            if (Array.isArray(values)) {
                try {
                    // 定义需要置顶的关键词
                    const priorityKeywords = ["❤️‍🔥", "香蕉", "Banana"];

                    // 对菜单项进行排序
                    values.sort((a, b) => {
                        // 获取用于显示的文本
                        const textA = (typeof a === "string" ? a : (a?.content || a?.title || "")).toLowerCase();
                        const textB = (typeof b === "string" ? b : (b?.content || b?.title || "")).toLowerCase();

                        // 检查是否包含关键词
                        const aHas = priorityKeywords.some(k => textA.includes(k.toLowerCase()));
                        const bHas = priorityKeywords.some(k => textB.includes(k.toLowerCase()));

                        // 如果 A 有关键词，B 没有，A 排前面 (-1)
                        if (aHas && !bHas) return -1;
                        // 如果 B 有关键词，A 没有，B 排前面 (1)
                        if (!aHas && bHas) return 1;

                        // 都不包含或都包含，维持原位置 (实际上可以使用 localeCompare 进行二级排序，这里保持原样即可)
                        return 0;
                    });
                } catch (e) {
                    console.warn("[BananaPriority] 菜单排序异常，已自动忽略:", e);
                }
            }

            // 调用原始构造函数，确保所有原生行为一致
            // 注意：使用 call/apply 可能会丢失 prototype 链，
            // 但 ContextMenu 往往只是一个类实例化过程，更稳妥的方式是 new OriginalContextMenu
            // 但这里是覆盖函数本身。

            // 为了保证 `new LiteGraph.ContextMenu` 能正常工作，
            // 我们需要返回一个真正的实例。
            return new OriginalContextMenu(...args);
        };

        // 恢复原型链，防止某些插件检查 instanceof
        LiteGraph.ContextMenu.prototype = OriginalContextMenu.prototype;

        console.log("[Banana] 菜单置顶增强已激活 🚀");
    }
});
