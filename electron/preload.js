'use strict';
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('sidka', {
  // open a URL in the system browser
  openUrl: (url) => ipcRenderer.send('open-url', url),

  // check if Ollama is reachable
  checkOllama: () => ipcRenderer.invoke('check-ollama'),

  // pull an Ollama model — resolves when done, rejects on failure
  pullModel: (model) => ipcRenderer.invoke('pull-model', model),

  // is a model already installed? (HTTP API check, no download needed)
  hasModel: (model) => ipcRenderer.invoke('has-model', model),

  // tell main process the wizard is done → close wizard, open main window
  wizardComplete: () => ipcRenderer.send('wizard-complete'),
});
