// 极客港 · 微博导出工具 —— Electron preload
// 出于安全考虑使用 contextIsolation，本 preload 暂不暴露额外 Node 能力。
// 预留：未来如需桌面特有能力（如选择导出目录、原生保存对话框），可在此通过
// contextBridge 暴露受控 API 给前端，例如：
//   const { contextBridge } = require('electron');
//   contextBridge.exposeInMainWorld('desktop', { pickDir: () => ... });
// 本期前端仅通过 HTTP 调用后端引擎，保持与网页版一致的代码路径。

// 占位 preload：保持上下文隔离，不暴露任何特权。
