'use strict';
const { app, BrowserWindow, Tray, Menu, shell, nativeImage, ipcMain, dialog, Notification } = require('electron');
const path  = require('path');
const http  = require('http');
const fs    = require('fs');
const os    = require('os');
const { spawn, execFile } = require('child_process');

// ── constants ──────────────────────────────────────────────────────────────
const PORT     = 8765;
const HOME     = os.homedir();
const RUNAI    = path.join(HOME, '.runai');
const SESSIONS = path.join(RUNAI, 'sessions');
const IS_WIN   = process.platform === 'win32';
const IS_MAC   = process.platform === 'darwin';
const DEV      = !app.isPackaged;

// ── state ──────────────────────────────────────────────────────────────────
let mainWindow   = null;
let wizardWindow = null;
let tray         = null;
let server       = null;  // child process

// ── paths ──────────────────────────────────────────────────────────────────
function pythonBin() {
  // 1. app-local venv (packaged)
  const packed = path.join(process.resourcesPath, 'venv', 'bin', 'python3');
  if (!DEV && fs.existsSync(packed)) return packed;
  // 2. user venv
  const userVenv = path.join(HOME, '.runai-venv', 'bin', IS_WIN ? 'python.exe' : 'python');
  if (fs.existsSync(userVenv)) return userVenv;
  // 3. system python
  return IS_WIN ? 'python' : 'python3';
}

function serverScript() {
  return DEV
    ? path.join(__dirname, '..', 'src', 'server.py')
    : path.join(process.resourcesPath, 'server', 'server.py');
}

// ── server lifecycle ───────────────────────────────────────────────────────
function startServer() {
  if (server) return;
  const py     = pythonBin();
  const script = serverScript();
  const env    = {
    ...process.env,
    RUNAI_WEB_MODE: '1',
    PYTHONPATH: path.dirname(script),
  };

  console.log('[sidka] starting server:', py, script);
  server = spawn(py, [script], { env, stdio: ['ignore', 'pipe', 'pipe'] });

  server.stdout.on('data', d => process.stdout.write('[server] ' + d));
  server.stderr.on('data', d => process.stderr.write('[server] ' + d));
  server.on('exit', (code, sig) => {
    console.log('[sidka] server exited', code, sig);
    server = null;
    if (mainWindow && !app.isQuitting) {
      mainWindow.webContents.executeJavaScript(
        `document.body.insertAdjacentHTML('beforeend','<div style="position:fixed;top:0;left:0;right:0;background:#e05050;color:#fff;padding:8px 16px;font-size:13px;z-index:9999">Server stopped — <a href="javascript:location.reload()" style="color:#fff">click to restart</a></div>')`
      );
    }
  });
}

function stopServer() {
  if (server) { server.kill(); server = null; }
}

function waitForServer(maxMs = 15000) {
  return new Promise((resolve, reject) => {
    const start   = Date.now();
    const attempt = () => {
      http.get(`http://127.0.0.1:${PORT}/api/ping`, res => resolve())
           .on('error', () => {
             if (Date.now() - start > maxMs) reject(new Error('Server timed out'));
             else setTimeout(attempt, 400);
           });
    };
    attempt();
  });
}

// ── windows ────────────────────────────────────────────────────────────────
function createMain() {
  mainWindow = new BrowserWindow({
    width:  1280,
    height: 840,
    minWidth:  900,
    minHeight: 600,
    titleBarStyle: IS_MAC ? 'hiddenInset' : 'default',
    backgroundColor: '#0e0e0e',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.loadURL(`http://127.0.0.1:${PORT}`);
  mainWindow.on('closed', () => { mainWindow = null; });

  // In dev, open devtools
  if (DEV) mainWindow.webContents.openDevTools({ mode: 'detach' });
}

function createWizard() {
  wizardWindow = new BrowserWindow({
    width:     580,
    height:    520,
    resizable: false,
    titleBarStyle: IS_MAC ? 'hiddenInset' : 'default',
    backgroundColor: '#0e0e0e',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  wizardWindow.loadFile(path.join(__dirname, '..', 'ui', 'wizard.html'));
  wizardWindow.on('closed', () => { wizardWindow = null; });
}

// ── tray ───────────────────────────────────────────────────────────────────
function createTray() {
  const iconFile = IS_WIN
    ? path.join(__dirname, '..', 'assets', 'icon.ico')
    : path.join(__dirname, '..', 'assets', 'icon.png');

  let icon;
  try {
    icon = nativeImage.createFromPath(iconFile).resize({ width: 16, height: 16 });
  } catch {
    icon = nativeImage.createEmpty();
  }

  tray = new Tray(icon);
  tray.setToolTip('Sidka');

  const updateMenu = () => tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: mainWindow ? 'Focus Sidka' : 'Open Sidka',
      click: () => {
        if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
        else launchApp();
      },
    },
    { type: 'separator' },
    { label: 'Restart server', click: () => { stopServer(); startServer(); }},
    { type: 'separator' },
    { label: 'Quit Sidka', click: () => { app.isQuitting = true; app.quit(); }},
  ]));

  updateMenu();
  tray.on('click', () => {
    if (mainWindow) { mainWindow.show(); mainWindow.focus(); }
    else launchApp();
  });
}

// ── first-run helpers ──────────────────────────────────────────────────────
function isFirstRun() {
  return !fs.existsSync(SESSIONS);
}

function isOllamaRunning() {
  return new Promise(resolve => {
    http.get('http://127.0.0.1:11434/api/tags', res => resolve(true))
        .on('error', () => resolve(false));
  });
}

function ensureRunaiDirs() {
  for (const d of [RUNAI, SESSIONS, path.join(RUNAI, 'skills')]) {
    if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
  }
  // Copy bundled skills into ~/.runai/skills/ if they don't exist yet
  const skillsSrc = DEV
    ? path.join(__dirname, '..', 'src', 'skills')
    : path.join(process.resourcesPath, 'server', 'skills');
  if (fs.existsSync(skillsSrc)) {
    for (const f of fs.readdirSync(skillsSrc)) {
      const dest = path.join(RUNAI, 'skills', f);
      if (!fs.existsSync(dest)) {
        fs.copyFileSync(path.join(skillsSrc, f), dest);
      }
    }
  }
}

// ── IPC handlers ───────────────────────────────────────────────────────────
ipcMain.on('open-url', (_, url) => shell.openExternal(url));

ipcMain.handle('check-ollama', isOllamaRunning);

ipcMain.handle('pull-model', (_, model) => new Promise((resolve, reject) => {
  const proc  = spawn('ollama', ['pull', model]);
  const lines = [];
  proc.stdout.on('data', d => lines.push(d.toString().trim()));
  proc.stderr.on('data', d => lines.push(d.toString().trim()));
  proc.on('exit', code => code === 0 ? resolve(lines.join('\n')) : reject(new Error(lines.slice(-5).join('\n'))));
  proc.on('error', reject);
}));

ipcMain.on('wizard-complete', async () => {
  if (wizardWindow) { wizardWindow.close(); wizardWindow = null; }
  await launchApp();
});

// ── app launch ─────────────────────────────────────────────────────────────
async function launchApp() {
  startServer();
  try {
    await waitForServer();
    createMain();
  } catch (err) {
    dialog.showErrorBox('Sidka — server error', `Could not start the local server.\n\n${err.message}\n\nMake sure Python and Ollama are installed.`);
  }
}

// ── app lifecycle ──────────────────────────────────────────────────────────
app.whenReady().then(async () => {
  app.isQuitting = false;
  createTray();
  ensureRunaiDirs();

  const first  = isFirstRun();
  const ollama = await isOllamaRunning();

  if (first || !ollama) {
    createWizard();
  } else {
    await launchApp();
  }
});

app.on('window-all-closed', () => {
  // Keep running in tray on all platforms
});

app.on('activate', () => {
  if (!mainWindow && !wizardWindow) launchApp();
});

app.on('before-quit', () => {
  app.isQuitting = true;
  stopServer();
});
