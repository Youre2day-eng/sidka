#!/usr/bin/env python3
"""Seed the Sidka code cookbook with curated, composable, KNOWN-GOOD recipes.

Each recipe is a focused building block the build model retrieves and tags
together. Granular blocks (loop, input, collision) compose into many apps;
'gold' recipes are full working references to adapt rather than reinvent.

Idempotent: rewrites only the seed entries (source=='seed'), preserving any
'learned' recipes the system has captured from clean previews.
"""
import json
import os

COOKBOOK = os.path.expanduser("~/.runai/cookbook.jsonl")

R = []  # recipe list


def rec(rid, title, tags, when, lang, code, kind="block"):
    R.append({
        "id": rid, "title": title, "tags": tags, "when": when,
        "lang": lang, "code": code.strip("\n"), "source": "seed",
        "kind": kind, "score": 5,
    })


# ---- the #1 rule: DOM mount order ------------------------------------------
rec("dom-mount-order", "Run script AFTER elements exist",
    ["dom", "script", "canvas", "getElementById", "null", "init", "order"],
    "ALWAYS — the most common cause of a black/blank preview is a <script> that "
    "runs getElementById before the element is parsed, returning null.",
    "html", """
<!-- WRONG: script runs before <canvas> exists -> getElementById returns null -> crash -->
<!-- RIGHT: put <script> at the END of <body>, after the elements, OR wrap in DOMContentLoaded -->
<body>
  <canvas id="c" width="400" height="400"></canvas>
  <script>
    // canvas is guaranteed to exist here
    const cv = document.getElementById('c');
    const ctx = cv.getContext('2d');
  </script>
</body>
<!-- If the script must live in <head>, wrap it:
     document.addEventListener('DOMContentLoaded', () => { ...all init... }); -->
""")

# ---- canvas setup (crisp, responsive) --------------------------------------
rec("canvas-setup", "Crisp responsive canvas (devicePixelRatio)",
    ["canvas", "context", "2d", "responsive", "retina", "devicePixelRatio", "setup"],
    "Any 2D canvas drawing. Avoids blurry rendering on retina and sizes to a square.",
    "js", """
function makeCanvas(size) {
  const cv = document.getElementById('c');
  const dpr = window.devicePixelRatio || 1;
  cv.width = size * dpr; cv.height = size * dpr;
  cv.style.width = cv.style.height = size + 'px';
  const ctx = cv.getContext('2d');
  ctx.scale(dpr, dpr);
  return { cv, ctx, size };
}
""")

# ---- game loop (fixed timestep) --------------------------------------------
rec("game-loop", "Fixed-timestep game loop (update/draw split)",
    ["game", "loop", "requestAnimationFrame", "tick", "update", "draw", "timestep", "fps"],
    "Any game or animation. Separates UPDATE (logic) from DRAW (render) and steps "
    "logic at a fixed rate so speed is frame-rate independent. You MUST call start().",
    "js", """
let _last = 0, _acc = 0, _running = false;
const STEP_MS = 1000 / 8;            // 8 logic ticks/sec (tune per game)
function frame(t) {
  if (!_running) return;
  _acc += t - _last; _last = t;
  while (_acc >= STEP_MS) { update(); _acc -= STEP_MS; }
  draw();
  requestAnimationFrame(frame);
}
function start() { _running = true; _last = performance.now(); requestAnimationFrame(frame); }
function stop()  { _running = false; }
// update() mutates state; draw() renders it. Define both, then call start().
""")

# ---- keyboard direction (no reverse) ---------------------------------------
rec("keyboard-direction", "Arrow/WASD direction with no-reverse guard",
    ["keyboard", "input", "arrow", "wasd", "direction", "keydown", "snake", "movement"],
    "Grid/direction games. Reads arrows + WASD, prevents 180-degree reversals, "
    "and stops the page from scrolling on arrow keys.",
    "js", """
let dir = {x: 1, y: 0}, nextDir = {x: 1, y: 0};
const KEYS = {
  ArrowUp:[0,-1], KeyW:[0,-1], ArrowDown:[0,1], KeyS:[0,1],
  ArrowLeft:[-1,0], KeyA:[-1,0], ArrowRight:[1,0], KeyD:[1,0],
};
addEventListener('keydown', e => {
  const v = KEYS[e.code]; if (!v) return;
  e.preventDefault();
  // block reversing directly into yourself
  if (v[0] === -dir.x && v[1] === -dir.y) return;
  nextDir = {x: v[0], y: v[1]};
});
// in update(): dir = nextDir;  before moving.
""")

# ---- grid food spawn -------------------------------------------------------
rec("grid-food-spawn", "Spawn food on a random empty grid cell",
    ["grid", "food", "spawn", "random", "snake", "collision", "empty"],
    "Grid games that place items avoiding occupied cells. Never spawns on the snake.",
    "js", """
function spawnFood(cols, rows, occupied) {
  // occupied: array of {x,y}. Returns a free cell, or null if board full.
  const taken = new Set(occupied.map(p => p.x + ',' + p.y));
  const free = [];
  for (let x = 0; x < cols; x++)
    for (let y = 0; y < rows; y++)
      if (!taken.has(x + ',' + y)) free.push({x, y});
  if (!free.length) return null;
  return free[Math.floor(Math.random() * free.length)];
}
""")

# ---- game over overlay + restart -------------------------------------------
rec("game-over-restart", "Game-over overlay with restart",
    ["game", "over", "restart", "reset", "overlay", "score"],
    "End-of-game UX. Draws a dim overlay with score and restarts on key/click.",
    "js", """
function drawGameOver(ctx, w, h, score) {
  ctx.fillStyle = 'rgba(0,0,0,.6)';
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = '#fff';
  ctx.textAlign = 'center';
  ctx.font = 'bold 28px system-ui'; ctx.fillText('Game Over', w/2, h/2 - 12);
  ctx.font = '16px system-ui';      ctx.fillText('Score ' + score + ' - press any key', w/2, h/2 + 18);
}
// On game over: stop(); draw overlay; then once:
//   addEventListener('keydown', resetGame, {once:true});
//   canvas.addEventListener('click', resetGame, {once:true});
""")

# ---- nice palette ----------------------------------------------------------
rec("palette-dark", "Pleasant dark game palette",
    ["color", "palette", "theme", "style", "design", "dark"],
    "Use instead of pure black/blue defaults. Cohesive, modern, high-contrast.",
    "css", """
:root {
  --bg:#11131a; --grid:#1b1f2a; --snake:#5ce6a8; --snake-head:#aef7d4;
  --food:#ff6b8b; --text:#e8ecf4; --accent:#7c6af7;
}
/* center the board, dark surround, subtle glow on the play area */
body { margin:0; min-height:100vh; display:grid; place-items:center;
  background:var(--bg); color:var(--text); font-family:system-ui,sans-serif; }
canvas { background:var(--grid); border-radius:14px;
  box-shadow:0 12px 40px rgba(0,0,0,.5), 0 0 0 1px #ffffff10; }
""")

# ---- GOLD: complete, polished snake ----------------------------------------
rec("gold-snake", "Gold-standard Snake (complete, polished)",
    ["snake", "game", "canvas", "grid", "gold", "complete", "reference", "arrow", "food"],
    "When the user asks for Snake. ADAPT this proven baseline (smooth loop, no-reverse "
    "input, grow-on-eat, wall/self death, score, restart) and improve it - do NOT "
    "write a new one from scratch.",
    "html", """
<!doctype html><html><head><meta charset="utf-8"><title>Snake</title>
<style>
  :root{--bg:#11131a;--grid:#1b1f2a;--snake:#5ce6a8;--head:#aef7d4;--food:#ff6b8b;--text:#e8ecf4}
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
    color:var(--text);font-family:system-ui,sans-serif}
  .wrap{text-align:center}
  #score{font:600 16px system-ui;letter-spacing:.04em;margin-bottom:10px;opacity:.85}
  canvas{background:var(--grid);border-radius:14px;box-shadow:0 12px 40px #0008,0 0 0 1px #fff1}
</style></head>
<body>
  <div class="wrap">
    <div id="score">Score 0</div>
    <canvas id="c" width="400" height="400"></canvas>
  </div>
  <script>
    const CELL=20, COLS=20, ROWS=20;
    const cv=document.getElementById('c'), ctx=cv.getContext('2d'), scoreEl=document.getElementById('score');
    let snake, dir, nextDir, food, score, dead, last, acc;
    const STEP=1000/9;
    function reset(){
      snake=[{x:10,y:10},{x:9,y:10},{x:8,y:10}];
      dir={x:1,y:0}; nextDir={x:1,y:0}; score=0; dead=false; acc=0; last=performance.now();
      food=spawnFood();
    }
    function spawnFood(){
      const taken=new Set(snake.map(p=>p.x+','+p.y)); const free=[];
      for(let x=0;x<COLS;x++)for(let y=0;y<ROWS;y++)if(!taken.has(x+','+y))free.push({x,y});
      return free[Math.floor(Math.random()*free.length)];
    }
    addEventListener('keydown',e=>{
      const m={ArrowUp:[0,-1],KeyW:[0,-1],ArrowDown:[0,1],KeyS:[0,1],
               ArrowLeft:[-1,0],KeyA:[-1,0],ArrowRight:[1,0],KeyD:[1,0]}[e.code];
      if(dead){reset();return;}
      if(!m)return; e.preventDefault();
      if(m[0]===-dir.x&&m[1]===-dir.y)return; nextDir={x:m[0],y:m[1]};
    });
    function update(){
      dir=nextDir;
      const head={x:snake[0].x+dir.x, y:snake[0].y+dir.y};
      if(head.x<0||head.y<0||head.x>=COLS||head.y>=ROWS||
         snake.some(p=>p.x===head.x&&p.y===head.y)){dead=true;return;}
      snake.unshift(head);
      if(head.x===food.x&&head.y===food.y){score++;scoreEl.textContent='Score '+score;food=spawnFood();}
      else snake.pop();
    }
    function draw(){
      ctx.clearRect(0,0,cv.width,cv.height);
      ctx.fillStyle='#ff6b8b';
      ctx.fillRect(food.x*CELL+3,food.y*CELL+3,CELL-6,CELL-6);
      snake.forEach((p,i)=>{
        ctx.fillStyle=i===0?'#aef7d4':'#5ce6a8';
        ctx.fillRect(p.x*CELL+1,p.y*CELL+1,CELL-2,CELL-2);
      });
      if(dead){
        ctx.fillStyle='rgba(0,0,0,.6)';ctx.fillRect(0,0,cv.width,cv.height);
        ctx.fillStyle='#fff';ctx.textAlign='center';
        ctx.font='bold 26px system-ui';ctx.fillText('Game Over',cv.width/2,cv.height/2-10);
        ctx.font='15px system-ui';ctx.fillText('Press any key',cv.width/2,cv.height/2+16);
      }
    }
    function frame(t){
      acc+=t-last;last=t;
      while(acc>=STEP){if(!dead)update();acc-=STEP;}
      draw();requestAnimationFrame(frame);
    }
    reset();requestAnimationFrame(frame);
  </script>
</body></html>
""", kind="gold")

# ---- GOLD: starter app shell (no game) -------------------------------------
rec("gold-app-shell", "Clean single-file app shell",
    ["app", "ui", "page", "html", "shell", "starter", "layout", "form", "tool", "gold"],
    "When the user asks for a non-game tool/app/page. A clean, modern, self-contained "
    "starting structure with good defaults - adapt it to the actual request.",
    "html", """
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>App</title>
<style>
  :root{--bg:#0f1117;--card:#171a22;--line:#252a36;--text:#e8ecf4;--dim:#9aa3b2;--accent:#7c6af7}
  *{box-sizing:border-box} body{margin:0;font-family:system-ui,sans-serif;background:var(--bg);
    color:var(--text);min-height:100vh}
  header{padding:20px 24px;border-bottom:1px solid var(--line);font-weight:700;font-size:18px}
  main{max-width:720px;margin:0 auto;padding:24px;display:grid;gap:16px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px}
  button{background:var(--accent);color:#fff;border:0;border-radius:9999px;padding:9px 18px;
    font-weight:600;cursor:pointer} button:hover{filter:brightness(1.08)}
  input,textarea{width:100%;background:#0f1117;border:1px solid var(--line);border-radius:10px;
    color:var(--text);padding:10px 12px;font:inherit}
</style></head>
<body>
  <header>App</header>
  <main>
    <div class="card">Replace with the real UI. State in JS, render into here.</div>
  </main>
  <script>
    // All logic here, at end of body so the DOM exists. Wire events, render state.
  </script>
</body></html>
""", kind="gold")


def main():
    learned = []
    if os.path.exists(COOKBOOK):
        with open(COOKBOOK, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    if r.get("source") != "seed":
                        learned.append(r)
                except Exception:
                    pass
    os.makedirs(os.path.dirname(COOKBOOK), exist_ok=True)
    with open(COOKBOOK, "w", encoding="utf-8") as f:
        for r in R + learned:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"seeded {len(R)} curated recipes (+{len(learned)} learned kept) -> {COOKBOOK}")


if __name__ == "__main__":
    main()
