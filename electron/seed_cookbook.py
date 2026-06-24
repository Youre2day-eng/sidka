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

# ---- GOLD: Pong -------------------------------------------------------------
rec("gold-pong", "Gold-standard Pong (player vs CPU)",
    ["pong", "game", "canvas", "paddle", "ball", "ai", "cpu", "gold", "physics", "reference"],
    "When the user asks for Pong or a paddle/ball game. ADAPT this baseline (mouse + "
    "W/S paddle, tracking CPU, ball angle off paddle, scoring to 7).",
    "html", """
<!doctype html><html><head><meta charset="utf-8"><title>Pong</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#11131a;
    color:#e8ecf4;font-family:system-ui,sans-serif}
  .wrap{text-align:center} #score{font:600 16px system-ui;margin-bottom:10px;letter-spacing:.1em}
  canvas{background:#1b1f2a;border-radius:14px;box-shadow:0 12px 40px #0008,0 0 0 1px #fff1}
</style></head>
<body>
  <div class="wrap"><div id="score">0 — 0</div><canvas id="c" width="640" height="400"></canvas></div>
  <script>
    const cv=document.getElementById('c'),ctx=cv.getContext('2d'),scoreEl=document.getElementById('score');
    const W=cv.width,H=cv.height,PH=70,PW=10;
    let ly,ry,bx,by,bvx,bvy,ls,rs,up=false,down=false;
    function reset(serveLeft){
      ly=ry=H/2-PH/2; bx=W/2; by=H/2;
      bvx=(serveLeft?-1:1)*4; bvy=(Math.random()*4-2);
    }
    function newGame(){ls=rs=0;reset(Math.random()<.5);}
    addEventListener('keydown',e=>{if(e.code==='KeyW')up=true;if(e.code==='KeyS')down=true;});
    addEventListener('keyup',e=>{if(e.code==='KeyW')up=false;if(e.code==='KeyS')down=false;});
    cv.addEventListener('mousemove',e=>{const r=cv.getBoundingClientRect();ly=e.clientY-r.top-PH/2;});
    function clamp(v){return Math.max(0,Math.min(H-PH,v));}
    function update(){
      if(up)ly-=6; if(down)ly+=6; ly=clamp(ly);
      // CPU tracks the ball with a speed cap so it is beatable
      const target=by-PH/2; ry+=Math.max(-4.5,Math.min(4.5,target-ry)); ry=clamp(ry);
      bx+=bvx; by+=bvy;
      if(by<0||by>H){bvy*=-1; by=Math.max(0,Math.min(H,by));}
      // left paddle
      if(bx<PW+8 && by>ly && by<ly+PH){bvx=Math.abs(bvx)+0.3; bvy+=((by-(ly+PH/2))/PH)*5;}
      // right paddle
      if(bx>W-PW-8 && by>ry && by<ry+PH){bvx=-(Math.abs(bvx)+0.3); bvy+=((by-(ry+PH/2))/PH)*5;}
      if(bx<0){rs++; reset(false);}
      if(bx>W){ls++; reset(true);}
      scoreEl.textContent=ls+' — '+rs;
    }
    function draw(){
      ctx.clearRect(0,0,W,H);
      ctx.strokeStyle='#ffffff22'; ctx.setLineDash([8,12]);
      ctx.beginPath();ctx.moveTo(W/2,0);ctx.lineTo(W/2,H);ctx.stroke();ctx.setLineDash([]);
      ctx.fillStyle='#5ce6a8'; ctx.fillRect(4,ly,PW,PH);
      ctx.fillStyle='#7c6af7'; ctx.fillRect(W-PW-4,ry,PW,PH);
      ctx.fillStyle='#e8ecf4'; ctx.beginPath();ctx.arc(bx,by,6,0,7);ctx.fill();
    }
    let last=0,acc=0;const STEP=1000/60;
    function frame(t){acc+=t-last;last=t;while(acc>=STEP){update();acc-=STEP;}draw();requestAnimationFrame(frame);}
    newGame();requestAnimationFrame(frame);
  </script>
</body></html>
""", kind="gold")

# ---- GOLD: Tetris -----------------------------------------------------------
rec("gold-tetris", "Gold-standard Tetris",
    ["tetris", "game", "canvas", "blocks", "tetromino", "rotate", "lines", "gold", "grid", "reference"],
    "When the user asks for Tetris or a falling-blocks game. ADAPT this baseline "
    "(7 pieces, rotation with bounds check, line clears, scoring, soft drop).",
    "html", """
<!doctype html><html><head><meta charset="utf-8"><title>Tetris</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#11131a;
    color:#e8ecf4;font-family:system-ui,sans-serif}
  .wrap{text-align:center} #score{font:600 16px system-ui;margin-bottom:10px}
  canvas{background:#1b1f2a;border-radius:12px;box-shadow:0 12px 40px #0008,0 0 0 1px #fff1}
</style></head>
<body>
  <div class="wrap"><div id="score">Lines 0</div><canvas id="c" width="240" height="480"></canvas></div>
  <script>
    const cv=document.getElementById('c'),ctx=cv.getContext('2d'),scoreEl=document.getElementById('score');
    const COLS=10,ROWS=20,S=24;
    const SHAPES={
      I:[[1,1,1,1]], O:[[1,1],[1,1]], T:[[0,1,0],[1,1,1]],
      S:[[0,1,1],[1,1,0]], Z:[[1,1,0],[0,1,1]], J:[[1,0,0],[1,1,1]], L:[[0,0,1],[1,1,1]]
    };
    const COLORS={I:'#5ce6a8',O:'#f7d65c',T:'#c06af7',S:'#6af7a0',Z:'#ff6b8b',J:'#6a9bf7',L:'#f7a86a'};
    let grid,piece,px,py,lines,dropAcc,last,dead;
    function empty(){return Array.from({length:ROWS},()=>Array(COLS).fill(''));}
    function rotate(m){return m[0].map((_,i)=>m.map(r=>r[i]).reverse());}
    function spawn(){
      const keys=Object.keys(SHAPES),k=keys[Math.floor(Math.random()*keys.length)];
      piece={m:SHAPES[k].map(r=>r.slice()),c:COLORS[k]};
      px=Math.floor((COLS-piece.m[0].length)/2); py=0;
      if(collide(px,py,piece.m)){dead=true;}
    }
    function collide(nx,ny,m){
      for(let y=0;y<m.length;y++)for(let x=0;x<m[y].length;x++){
        if(!m[y][x])continue;
        const gx=nx+x,gy=ny+y;
        if(gx<0||gx>=COLS||gy>=ROWS)return true;
        if(gy>=0&&grid[gy][gx])return true;
      } return false;
    }
    function merge(){
      piece.m.forEach((row,y)=>row.forEach((v,x)=>{if(v&&py+y>=0)grid[py+y][px+x]=piece.c;}));
      let cleared=0;
      for(let y=ROWS-1;y>=0;y--){
        if(grid[y].every(c=>c)){grid.splice(y,1);grid.unshift(Array(COLS).fill(''));cleared++;y++;}
      }
      if(cleared){lines+=cleared;scoreEl.textContent='Lines '+lines;}
      spawn();
    }
    function step(){ if(collide(px,py+1,piece.m)){merge();} else {py++;} }
    function reset(){grid=empty();lines=0;dead=false;dropAcc=0;last=performance.now();spawn();scoreEl.textContent='Lines 0';}
    addEventListener('keydown',e=>{
      if(dead){if(e.code==='Enter'||e.code==='Space')reset();return;}
      if(e.code==='ArrowLeft'&&!collide(px-1,py,piece.m))px--;
      else if(e.code==='ArrowRight'&&!collide(px+1,py,piece.m))px++;
      else if(e.code==='ArrowDown')step();
      else if(e.code==='ArrowUp'){const r=rotate(piece.m); if(!collide(px,py,r))piece.m=r;}
      else return; e.preventDefault();
    });
    function draw(){
      ctx.clearRect(0,0,cv.width,cv.height);
      for(let y=0;y<ROWS;y++)for(let x=0;x<COLS;x++)if(grid[y][x]){ctx.fillStyle=grid[y][x];ctx.fillRect(x*S+1,y*S+1,S-2,S-2);}
      if(piece)piece.m.forEach((row,y)=>row.forEach((v,x)=>{if(v){ctx.fillStyle=piece.c;ctx.fillRect((px+x)*S+1,(py+y)*S+1,S-2,S-2);}}));
      if(dead){ctx.fillStyle='rgba(0,0,0,.6)';ctx.fillRect(0,0,cv.width,cv.height);
        ctx.fillStyle='#fff';ctx.textAlign='center';ctx.font='bold 22px system-ui';
        ctx.fillText('Game Over',cv.width/2,cv.height/2-8);
        ctx.font='14px system-ui';ctx.fillText('Enter to restart',cv.width/2,cv.height/2+16);}
    }
    function frame(t){
      const dt=t-last;last=t;
      if(!dead){dropAcc+=dt; if(dropAcc>600){step();dropAcc=0;}}
      draw();requestAnimationFrame(frame);
    }
    reset();requestAnimationFrame(frame);
  </script>
</body></html>
""", kind="gold")

# ---- GOLD: Calculator -------------------------------------------------------
rec("gold-calculator", "Gold-standard calculator",
    ["calculator", "calc", "math", "buttons", "keypad", "app", "tool", "gold", "reference"],
    "When the user asks for a calculator. ADAPT this baseline (button grid + keyboard, "
    "safe expression evaluation via a tiny parser - NO eval()).",
    "html", """
<!doctype html><html><head><meta charset="utf-8"><title>Calculator</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#11131a;font-family:system-ui,sans-serif}
  .calc{width:260px;background:#171a22;border:1px solid #252a36;border-radius:18px;padding:16px;
    box-shadow:0 16px 50px #0009}
  #disp{height:64px;display:flex;align-items:flex-end;justify-content:flex-end;color:#e8ecf4;
    font:600 30px system-ui;padding:6px 8px;overflow:hidden;word-break:break-all}
  .grid{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:10px}
  button{border:0;border-radius:12px;padding:16px 0;font:600 18px system-ui;cursor:pointer;
    background:#222735;color:#e8ecf4} button:hover{filter:brightness(1.15)}
  .op{background:#7c6af7;color:#fff} .eq{background:#5ce6a8;color:#0b0b0c} .wide{grid-column:span 2}
</style></head>
<body>
  <div class="calc">
    <div id="disp">0</div>
    <div class="grid" id="pad"></div>
  </div>
  <script>
    const disp=document.getElementById('disp'),pad=document.getElementById('pad');
    let expr='';
    const keys=['C','(',')','/','7','8','9','*','4','5','6','-','1','2','3','+','0','.','='];
    // safe shunting-yard evaluator (no eval)
    function evaluate(s){
      const out=[],ops=[],prec={'+':1,'-':1,'*':2,'/':2};
      const tok=s.match(/\\d+\\.?\\d*|[()+\\-*/]/g)||[];
      const apply=()=>{const o=ops.pop(),b=out.pop(),a=out.pop();
        out.push(o==='+'?a+b:o==='-'?a-b:o==='*'?a*b:a/b);};
      for(const t of tok){
        if(/\\d/.test(t))out.push(parseFloat(t));
        else if(t==='(')ops.push(t);
        else if(t===')'){while(ops.length&&ops[ops.length-1]!=='(')apply();ops.pop();}
        else{while(ops.length&&prec[ops[ops.length-1]]>=prec[t])apply();ops.push(t);}
      }
      while(ops.length)apply();
      const r=out.pop(); return r===undefined||!isFinite(r)?'Error':String(Math.round(r*1e10)/1e10);
    }
    function render(){disp.textContent=expr||'0';}
    function press(k){
      if(k==='C'){expr='';}
      else if(k==='='){expr=evaluate(expr);}
      else{if(expr==='Error')expr='';expr+=k;}
      render();
    }
    keys.forEach(k=>{const b=document.createElement('button');b.textContent=k;
      if('+-*/'.includes(k))b.className='op'; if(k==='=')b.className='eq';
      b.onclick=()=>press(k);pad.appendChild(b);});
    addEventListener('keydown',e=>{
      const k=e.key;
      if(/[0-9.+\\-*/()]/.test(k)&&k.length===1)press(k);
      else if(k==='Enter')press('=');
      else if(k==='Escape'||k==='c'||k==='C')press('C');
      else if(k==='Backspace'){expr=expr.slice(0,-1);render();}
    });
    render();
  </script>
</body></html>
""", kind="gold")

# ---- GOLD: Timer / Pomodoro -------------------------------------------------
rec("gold-timer", "Gold-standard countdown / Pomodoro timer",
    ["timer", "countdown", "pomodoro", "clock", "stopwatch", "app", "tool", "gold", "reference"],
    "When the user asks for a timer, countdown, or Pomodoro. ADAPT this baseline "
    "(start/pause/reset, SVG ring progress, preset minutes, drift-free via timestamps).",
    "html", """
<!doctype html><html><head><meta charset="utf-8"><title>Timer</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#11131a;
    color:#e8ecf4;font-family:system-ui,sans-serif}
  .card{text-align:center} .ring{position:relative;width:240px;height:240px;margin:auto}
  #time{position:absolute;inset:0;display:grid;place-items:center;font:700 48px system-ui;font-variant-numeric:tabular-nums}
  .row{margin-top:18px;display:flex;gap:8px;justify-content:center;flex-wrap:wrap}
  button{border:0;border-radius:9999px;padding:10px 18px;font:600 14px system-ui;cursor:pointer;
    background:#222735;color:#e8ecf4} button:hover{filter:brightness(1.15)}
  .primary{background:#5ce6a8;color:#0b0b0c} .preset{background:#1b1f2a;border:1px solid #2a3040}
</style></head>
<body>
  <div class="card">
    <div class="ring">
      <svg width="240" height="240">
        <circle cx="120" cy="120" r="108" fill="none" stroke="#252a36" stroke-width="12"/>
        <circle id="prog" cx="120" cy="120" r="108" fill="none" stroke="#5ce6a8" stroke-width="12"
          stroke-linecap="round" transform="rotate(-90 120 120)"/>
      </svg>
      <div id="time">25:00</div>
    </div>
    <div class="row">
      <button class="preset" data-m="5">5</button>
      <button class="preset" data-m="15">15</button>
      <button class="preset" data-m="25">25</button>
      <button class="preset" data-m="50">50</button>
    </div>
    <div class="row">
      <button id="toggle" class="primary">Start</button>
      <button id="reset">Reset</button>
    </div>
  </div>
  <script>
    const timeEl=document.getElementById('time'),prog=document.getElementById('prog'),
          toggle=document.getElementById('toggle');
    const C=2*Math.PI*108; prog.style.strokeDasharray=C;
    let total=25*60, remain=total, endAt=0, running=false, raf=0;
    function fmt(s){const m=Math.floor(s/60),x=Math.floor(s%60);return String(m).padStart(2,'0')+':'+String(x).padStart(2,'0');}
    function render(){
      timeEl.textContent=fmt(Math.ceil(remain));
      prog.style.strokeDashoffset=C*(1-remain/total);
    }
    function tick(){
      remain=Math.max(0,(endAt-performance.now())/1000);
      render();
      if(remain<=0){running=false;toggle.textContent='Start';timeEl.textContent='Done';return;}
      raf=requestAnimationFrame(tick);
    }
    function start(){running=true;toggle.textContent='Pause';endAt=performance.now()+remain*1000;tick();}
    function pause(){running=false;toggle.textContent='Start';cancelAnimationFrame(raf);}
    toggle.onclick=()=>running?pause():start();
    document.getElementById('reset').onclick=()=>{pause();remain=total;render();};
    document.querySelectorAll('.preset').forEach(b=>b.onclick=()=>{
      pause();total=remain=(+b.dataset.m)*60;render();});
    render();
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
