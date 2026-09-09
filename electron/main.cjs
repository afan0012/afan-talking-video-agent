const { app, BrowserWindow, dialog, session } = require('electron');
const { spawn } = require('child_process');
const net = require('net');
const path = require('path');

let backend = null;
let windowRef = null;
let backendPort = null;

function findFreePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
  });
}

function backendCommand() {
  if (app.isPackaged) {
    return {
      command: path.join(process.resourcesPath, 'backend', 'afan Talking Video Agent.exe'),
      args: [],
    };
  }
  const python = process.env.AFAN_PYTHON || 'python';
  return {
    command: python,
    args: [path.join(__dirname, '..', 'desktop_launcher.py')],
  };
}

function waitForServer(url, timeoutMs = 30000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const probe = () => {
      fetch(url).then(() => resolve()).catch(() => {
        if (Date.now() - started > timeoutMs) {
          reject(new Error('本地服务启动超时，请查看日志或检查 Python/后端文件。'));
          return;
        }
        setTimeout(probe, 150);
      });
    };
    probe();
  });
}

async function startBackend() {
  backendPort = await findFreePort();
  const { command, args } = backendCommand();
  backend = spawn(command, args, {
    cwd: path.join(__dirname, '..'),
    env: { ...process.env, AFAN_PORT: String(backendPort), AFAN_NO_BROWSER: '1' },
    windowsHide: true,
    stdio: 'ignore',
  });
  backend.once('error', (error) => {
    if (windowRef) dialog.showErrorBox('afan Talking Video Agent启动失败', error.message);
  });
  await waitForServer(`http://127.0.0.1:${backendPort}/`);
}

function stopBackend() {
  if (!backend || backend.killed) return;
  if (process.platform === 'win32') {
    spawn('taskkill', ['/pid', String(backend.pid), '/T', '/F'], { windowsHide: true });
  } else {
    backend.kill('SIGTERM');
  }
  backend = null;
}

function createWindow() {
  windowRef = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 1080,
    minHeight: 720,
    title: 'afan Talking Video Agent',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
    },
  });
  windowRef.loadURL(`http://127.0.0.1:${backendPort}/`);
  windowRef.on('closed', () => { windowRef = null; });
}

app.whenReady().then(async () => {
  session.defaultSession.setPermissionRequestHandler((_webContents, permission, callback) => {
    callback(['media'].includes(permission));
  });
  try {
    await startBackend();
    createWindow();
  } catch (error) {
    dialog.showErrorBox('afan Talking Video Agent启动失败', error.message);
    app.quit();
  }
});

app.on('before-quit', stopBackend);
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0 && backendPort) createWindow(); });
