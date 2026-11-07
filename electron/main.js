// 极客港 · 微博导出工具 —— Electron 桌面壳
// 品牌归属：极客港 jikegang.com (jikegang-weibo-export)
//
// 职责：启动后端 Python 服务（作为子进程），然后打开一个窗口加载本地界面。
// 后端引擎与网页版完全共用，桌面壳只是“套了一层窗口”。
//
// 开发运行：cd electron && npm install && npm start
//   （需先在项目根目录装好后端依赖：python -m pip install -r requirements.txt）
// 本期以「本地 Web 界面 + 后端」为主，Electron 打包脚本已就绪但不在本期强求出安装包，
// 因为中国大陆网络下 electron-builder 拉取二进制常超时。

const { app, BrowserWindow, shell } = require('electron');
const { spawn } = require('child_process');
const http = require('http');
const path = require('path');

const PORT = process.env.WEIBO_PORT || 8765;
const HOST = '127.0.0.1';
const URL = `http://${HOST}:${PORT}`;

let backendProc = null;
let win = null;

// 定位后端根目录：开发态为 electron/ 的上一级
const backendRoot = path.resolve(__dirname, '..');
const pythonCandidates = process.platform === 'win32'
  ? ['python', 'python3', 'py']
  : ['python3', 'python'];

function startBackend() {
  const py = pythonCandidates[0];
  backendProc = spawn(py, ['-m', 'weibo_export.server', '--host', HOST, '--port', String(PORT)], {
    cwd: backendRoot,
    stdio: 'inherit',
    shell: process.platform === 'win32',
  });
  backendProc.on('error', (e) => {
    console.error('后端启动失败：请确认已安装 Python 与依赖。', e);
  });
  backendProc.on('exit', (code) => {
    console.log('后端进程退出，代码', code);
  });
}

function waitForBackend(retries = 30, delay = 500) {
  return new Promise((resolve, reject) => {
    const attempt = (left) => {
      const req = http.get(`${URL}/api/status`, (res) => {
        res.resume();
        resolve();
      });
      req.on('error', () => {
        if (left <= 0) return reject(new Error('后端未就绪'));
        setTimeout(() => attempt(left - 1), delay);
      });
    };
    attempt(retries);
  });
}

function createWindow() {
  win = new BrowserWindow({
    width: 1080,
    height: 860,
    title: '极客港 · 微博导出工具',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.loadURL(URL);
  // 外部链接用系统浏览器打开，避免在壳内跳走
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
}

app.whenReady().then(async () => {
  startBackend();
  try {
    await waitForBackend();
  } catch (e) {
    console.error(e.message, '—— 请手动运行 python run.py 后刷新。');
  }
  createWindow();
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  if (backendProc) {
    try { backendProc.kill(); } catch (e) { /* ignore */ }
  }
});
