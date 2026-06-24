// Pure-Node headless DOM/canvas/WebGL stub — proves a generated artifact's JS
// runs through init + N animation frames with no uncaught throw. No browser, no
// deps. Reproduces the in-browser preview harness's bar: "runs clean for ~2s".
//
// Usage: node _shim.mjs <candidate.mjs> [frames]
//   prints SMOKE_OK + exit 0 on success, SMOKE_THROW <stack> + exit 2 on failure.

const candidate = process.argv[2];
const FRAMES = parseInt(process.argv[3] || '120', 10);

let _t = 0;
const rafQ = [];

function noop() {}
function stubGfx() {
  return new Proxy({}, { get: () => () => undefined, set: () => true });
}
function stub2D() {
  const grad = { addColorStop: noop };
  const api = {
    canvas: cv, measureText: () => ({ width: 10 }),
    createLinearGradient: () => grad, createRadialGradient: () => grad,
    createPattern: () => ({}), getImageData: () => ({ data: new Uint8ClampedArray(4), width: 1, height: 1 }),
    putImageData: noop, drawImage: noop, setTransform: noop, getContextAttributes: () => ({}),
    save: noop, restore: noop, beginPath: noop, closePath: noop, fill: noop, stroke: noop,
    moveTo: noop, lineTo: noop, arc: noop, rect: noop, fillRect: noop, strokeRect: noop,
    clearRect: noop, fillText: noop, strokeText: noop, translate: noop, rotate: noop,
    scale: noop, clip: noop, ellipse: noop, bezierCurveTo: noop, quadraticCurveTo: noop,
    setLineDash: noop, arcTo: noop, createImageData: () => ({ data: new Uint8ClampedArray(4) }),
  };
  return new Proxy(api, { get: (t, k) => (k in t ? t[k] : noop), set: () => true });
}
function stubGL() {
  const api = {
    canvas: cv, createShader: () => ({}), createProgram: () => ({}), createBuffer: () => ({}),
    createTexture: () => ({}), createFramebuffer: () => ({}), createVertexArray: () => ({}),
    getAttribLocation: () => 0, getUniformLocation: () => ({}), getShaderParameter: () => true,
    getProgramParameter: () => true, getExtension: () => null, getParameter: () => 0,
    viewport: noop, clear: noop, clearColor: noop, enable: noop, disable: noop,
  };
  return new Proxy(api, { get: (t, k) => (k in t ? t[k] : noop), set: () => true });
}

const cv = {
  width: 800, height: 600, style: {}, dataset: {},
  getContext: (t) => (/webgl/.test(String(t)) ? stubGL() : stub2D()),
  getBoundingClientRect: () => ({ left: 0, top: 0, right: 800, bottom: 600, width: 800, height: 600 }),
  addEventListener: noop, removeEventListener: noop, requestPointerLock: noop,
  toDataURL: () => '', focus: noop, setAttribute: noop, getAttribute: () => null,
};

function el() {
  const node = {
    style: {}, dataset: {}, children: [], childNodes: [], textContent: '', innerHTML: '',
    innerText: '', value: '', checked: false, width: 300, height: 150,
    classList: { add: noop, remove: noop, toggle: noop, contains: () => false },
    appendChild: (c) => c, removeChild: (c) => c, insertBefore: (c) => c,
    addEventListener: noop, removeEventListener: noop, setAttribute: noop,
    getAttribute: () => null, removeAttribute: noop, querySelector: () => el(),
    querySelectorAll: () => [], getContext: cv.getContext, focus: noop, blur: noop, click: noop,
    getBoundingClientRect: cv.getBoundingClientRect, requestPointerLock: noop,
    setProperty: noop, append: noop, remove: noop, cloneNode: () => el(),
  };
  return new Proxy(node, { get: (t, k) => (k in t ? t[k] : noop), set: (t, k, v) => { t[k] = v; return true; } });
}

// Source-order model: ids become visible to synchronous getElementById only once
// the HTML "parser" has revealed the element preceding the running <script>. The
// Python driver interleaves __reveal({id:isCanvas}) calls in document order. This
// reproduces the browser's #1 black-box bug: a <script> that runs getElementById
// before its target element is parsed -> null -> throw.
const _ids = Object.create(null);
globalThis.__reveal = (map) => { for (const k in map) _ids[k] = map[k]; };
const _elCache = Object.create(null);
function _getById(id) {
  id = String(id);
  if (!(id in _ids)) return null;            // not yet parsed -> null, like a browser
  if (_ids[id] === 'canvas') return cv;
  return _elCache[id] || (_elCache[id] = el());
}

const doc = {
  getElementById: _getById,
  querySelector: (s) => (/canvas/i.test(String(s)) ? cv : el()),
  querySelectorAll: () => [], createElement: (t) => (/canvas/i.test(String(t)) ? cv : el()),
  createElementNS: () => el(), createTextNode: () => el(), getElementsByTagName: () => [cv],
  getElementsByClassName: () => [], addEventListener: noop, removeEventListener: noop,
  body: el(), head: el(), documentElement: el(), title: '', cookie: '',
  exitPointerLock: noop, pointerLockElement: null, hidden: false, visibilityState: 'visible',
  readyState: 'complete', fonts: { ready: Promise.resolve(), add: noop },
};

// Some globals (navigator, performance, location) are read-only in Node — define
// them with a configurable descriptor instead of plain assignment.
function G(k, v) {
  try { globalThis[k] = v; }
  catch { Object.defineProperty(globalThis, k, { value: v, configurable: true, writable: true }); }
}

globalThis.document = doc;
globalThis.window = globalThis;
globalThis.self = globalThis;
G('navigator', { userAgent: 'node', platform: 'node', getGamepads: () => [], language: 'en' });
G('location', { href: 'http://localhost/', search: '', hash: '', reload: noop });
globalThis.devicePixelRatio = 1;
globalThis.innerWidth = 800; globalThis.innerHeight = 600;
globalThis.requestAnimationFrame = (cb) => { rafQ.push(cb); return rafQ.length; };
globalThis.cancelAnimationFrame = noop;
globalThis.setTimeout = (cb) => { if (typeof cb === 'function') rafQ.push(() => cb()); return 0; };
globalThis.clearTimeout = noop;
globalThis.setInterval = (cb) => { if (typeof cb === 'function') rafQ.push(() => cb()); return 0; };
globalThis.clearInterval = noop;
globalThis.addEventListener = noop;
globalThis.removeEventListener = noop;
globalThis.dispatchEvent = () => true;
G('performance', { now: () => _t });
globalThis.matchMedia = () => ({ matches: false, addEventListener: noop, addListener: noop });
globalThis.getComputedStyle = () => new Proxy({}, { get: () => '' });
globalThis.alert = noop; globalThis.confirm = () => true; globalThis.prompt = () => '';
globalThis.localStorage = { getItem: () => null, setItem: noop, removeItem: noop, clear: noop };
globalThis.sessionStorage = globalThis.localStorage;
globalThis.WebGLRenderingContext = function () {};
globalThis.WebGL2RenderingContext = function () {};
globalThis.AudioContext = function () { return new Proxy({}, { get: () => () => ({ connect: noop, start: noop, stop: noop, gain: { value: 0 }, frequency: { value: 0 } }) }); };
globalThis.webkitAudioContext = globalThis.AudioContext;
globalThis.Image = class { constructor() { this.width = 1; this.height = 1; } set src(_) { if (this.onload) this.onload(); } };
globalThis.Audio = class { play() { return Promise.resolve(); } pause() {} };
globalThis.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({}), text: () => Promise.resolve('') });
const _KeyEvent = class { constructor(t, o = {}) { Object.assign(this, o); this.type = t; this.preventDefault = noop; this.stopPropagation = noop; } };
globalThis.KeyboardEvent = _KeyEvent; globalThis.MouseEvent = _KeyEvent;
globalThis.Event = _KeyEvent; globalThis.PointerEvent = _KeyEvent; globalThis.CustomEvent = _KeyEvent;

process.on('uncaughtException', (e) => { console.error('SMOKE_THROW ' + (e && e.stack || e)); process.exit(2); });
process.on('unhandledRejection', (e) => { console.error('SMOKE_THROW ' + (e && e.stack || e)); process.exit(2); });

import('file://' + candidate)
  .then(() => {
    for (let f = 0; f < FRAMES; f++) {
      _t += 16.7;
      const q = rafQ.splice(0);
      for (const cb of q) cb(_t);
    }
    console.log('SMOKE_OK frames=' + FRAMES);
    process.exit(0);
  })
  .catch((e) => { console.error('SMOKE_THROW ' + (e && e.stack || e)); process.exit(2); });
