/* 在 node 里跑 webapp/app.js：假 DOM + 假 Worker，Worker 里真的加载 wasm。
 * 目的不是替代浏览器，是抓接线错误（id 拼错、函数未定义、消息协议对不上）。
 *
 * 用法（先 `sh webapp/make_dist.sh` 打好 dist/）::
 *
 *     node webapp/t_webapp.js                # Worker 路径
 *     FALLBACK=1 node webapp/t_webapp.js     # 主线程退路（file:// 的情形）
 *
 * 断言的是"网页版的接线"，不是浏览器渲染 —— 视觉靠 DEPLOY.md 里那套
 * firefox-esr 截图。棋力那一侧另有三条：入门档开局与桌面版同手（第 10 段、
 * 第 3 段各钉一次），以及 tests/test_book_web.py 守 book.js 的同步。 */
'use strict';
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const DIR = path.join(__dirname, 'dist');   // 打好的扁平包，线上就是这一份
const FALLBACK = process.env.FALLBACK === '1';   // 模拟 file:// / 平台禁 Worker

/* ---- 从 index.html 里抠出每个 id、tag 与初始 class ---- */
const html = fs.readFileSync(path.join(DIR, 'index.html'), 'utf8');
const tags = [...html.matchAll(/<([a-zA-Z][\w-]*)\b([^>]*)>/g)].map((m) => ({
  tag: m[1].toLowerCase(),
  attrs: m[2],
}));
const specs = [];
for (const t of tags) {
  const id = /\bid="([^"]+)"/.exec(t.attrs);
  if (!id) continue;
  const cls = /\bclass="([^"]*)"/.exec(t.attrs);
  specs.push({ tag: t.tag, id: id[1], cls: cls ? cls[1] : '' });
}

function mkEl(tag, id, cls) {
  const el = {
    tagName: tag, id: id || '', className: cls || '', textContent: '', innerHTML: '',
    dataset: {}, children: [], listeners: {}, style: {}, clientWidth: 420, clientHeight: 0,
    classList: {
      _s: new Set(String(cls || '').split(/\s+/).filter(Boolean)),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      contains(c) { return this._s.has(c); },
      toggle(c, f) { const on = f === undefined ? !this._s.has(c) : !!f; on ? this._s.add(c) : this._s.delete(c); return on; },
    },
    setAttribute(k, v) { this[k] = v; },
    getAttribute(k) { return this[k]; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    appendChild(c) { this.children.push(c); return c; },
    querySelectorAll(sel) { const cls2 = sel.replace(/^\./, ''); return this.children.filter(c => String(c.className).split(' ').includes(cls2)); },
    closest(sel) { return String(this.className || '').split(' ').includes(sel.replace(/^\./, '')) ? this : null; },
    getBoundingClientRect() { return { left: 0, top: 0, width: this.clientWidth, height: this.clientHeight || this.clientWidth }; },
    click() { for (const f of this.listeners.click || []) f({ target: this, clientX: 0, clientY: 0 }); },
  };
  if (tag === 'canvas') el.getContext = () => ctx2d();
  return el;
}

const byId = {};
for (const s of specs) byId[s.id] = mkEl(s.tag, s.id, s.cls);
byId.board.clientHeight = 420;

function ctx2d() {
  const grad = { addColorStop() {} };
  const img = (w, h) => ({ data: new Uint8ClampedArray((w || 0) * (h || 0) * 4), width: w, height: h });
  const t = {};
  return new Proxy(t, {
    get(o, k) {
      if (k === 'createLinearGradient' || k === 'createRadialGradient') return () => grad;
      if (k === 'measureText') return () => ({ width: 10 });
      if (k === 'createImageData') return (w, h) => img(w, h);
      if (k === 'getImageData') return (x, y, w, h) => img(w, h);
      if (k in o) return o[k];
      return () => {};
    },
    set(o, k, v) { o[k] = v; return true; },
  });
}

const body = mkEl('body');
const head = mkEl('head');
const documentElement = mkEl('html');
const document = {
  body, head, documentElement,
  getElementById: (id) => byId[id] || null,
  createElement: (tag) => mkEl(tag),
};

head.appendChild = function (c) {
  this.children.push(c);
  if (c.tagName === 'script') setTimeout(() => loadScriptInto(c, sandbox), 0);
  return c;
};

//: <script src> 的加载：把文件跑进同一个 vm 上下文，再把 emscripten 的工厂
//: 挂成全局（浏览器里它本来就是 window.createGomoku），最后回调 onload。
function loadScriptInto(el, ctx) {
  const file = path.join(DIR, el.src.replace(/^\.\//, ''));
  try {
    const src = fs.readFileSync(file, 'utf8');
    if (/gomoku-(free|ban)\.js$/.test(file)) {
      ctx.createGomoku = require(file);      // node 分支导出的是 module.exports
    } else {
      vm.runInContext(src, ctx, { filename: path.basename(file) });
    }
    if (el.onload) el.onload();
  } catch (e) {
    if (el.onerror) el.onerror(e); else throw e;
  }
}

const store = {};
const localStorage = { getItem: (k) => (k in store ? store[k] : null), setItem: (k, v) => { store[k] = String(v); } };
const getComputedStyle = () => ({ getPropertyValue: () => '' });
const win = {
  devicePixelRatio: 2, location: { hash: '' }, listeners: {},
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
};

/* ---- 假 Worker：真的执行 engine-worker.js，真的加载 wasm ---- */
class FakeWorker {
  constructor(url) {
    this.dead = false;
    this.url = url;
    const hash = url.includes('#') ? '#' + url.split('#')[1] : '';
    let mod = null;
    const selfObj = {
      location: { hash },
      onmessage: null,
      postMessage: (msg) => { if (!this.dead && this.onmessage) this.onmessage({ data: msg }); },
    };
    const sandbox = {
      self: selfObj,
      location: selfObj.location,
      console,
      Promise, Error, JSON, Math, String, Number, Object, Array, Uint8Array, Int8Array,
      importScripts: (p) => {
        const file = path.join(DIR, p.replace(/^\.\//, ''));
        mod = require(file);
        sandbox.createGomoku = mod;   // emscripten 的 node 分支导出工厂
      },
      createGomoku: null,
    };
    const ctx = vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(path.join(DIR, 'engine-worker.js'), 'utf8'), ctx, { filename: 'engine-worker.js' });
    this._self = selfObj;
  }
  postMessage(msg) {
    if (this.dead) return;
    // 异步派发，模拟真实的跨线程时序
    setTimeout(() => { if (!this.dead && this._self.onmessage) this._self.onmessage({ data: msg }); }, 0);
  }
  terminate() { this.dead = true; }
}

/* ---- 装载 book.js（页面里排在 app.js 之前）---- */
const bookSrc = fs.readFileSync(path.join(DIR, 'book.js'), 'utf8');

/* ---- 装载 app.js ---- */
let src = fs.readFileSync(path.join(DIR, 'app.js'), 'utf8');
src += '\n;globalThis.__api = { S, el, idx, winLine, place, undo, engine, refreshForbidden,'
     + ' LEVELS, N, geom, toGrid, BOOK, bookLookup, reviewMoves, reviewRecord,'
     + ' reviewLine, openReviewPick, startReview, cancelReview, showReviewView,'
     + ' exitReview, NET, netHandle, netOpen, netClose, netSendState, netTurnCard,'
     + ' enterOnline, newGame, finish, applyModeGroups, clearNode };\n';

const WorkerImpl = FALLBACK ? class { constructor() { throw new Error('Worker blocked'); } } : FakeWorker;
const sandbox = {
  document, window: win, self: win, localStorage, getComputedStyle, Worker: WorkerImpl,
  console, setTimeout, clearTimeout, setInterval, clearInterval,
  requestAnimationFrame: (f) => setTimeout(f, 0),
  Math, JSON, Number, String, Object, Array, Int8Array, Uint8Array, Uint8ClampedArray,
  Promise, Error,
};
const ctx = vm.createContext(sandbox);
vm.runInContext(bookSrc, ctx, { filename: 'book.js' });
vm.runInContext(src, ctx, { filename: 'app.js' });
const A = sandbox.__api;

let fails = 0;
function ok(cond, label, extra) {
  console.log((cond ? 'PASS  ' : 'FAIL  ') + label + (extra === undefined ? '' : '  ' + extra));
  if (!cond) { fails++; process.exitCode = 1; }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

//: 落子的坐标按 app.js 自己的几何反推，避免测试里再抄一份 pad/cell 公式
function clickCell(r, c) {
  const w = byId.board.clientWidth;
  const g = A.geom(w);
  for (const f of byId.board.listeners.click || []) {
    f({ clientX: g.ox + c * g.cell, clientY: g.oy + r * g.cell, target: byId.board });
  }
}

//: 等某个条件成立，最多 ms 毫秒
async function until(fn, ms) {
  const t0 = Date.now();
  while (!fn()) { if (Date.now() - t0 > ms) return false; await sleep(20); }
  return true;
}

function pickLevel(i) {
  const card = byId['choose-level'].children[i];
  for (const f of byId['choose-level'].listeners.click || []) f({ target: card });
}

function pickChip(groupId, attr, value) {
  const chip = mkEl('button', '', 'card');
  chip.dataset[attr] = String(value);
  for (const f of byId[groupId].listeners.click || []) f({ target: chip });
}

function turnState() { return byId['turn-card'].dataset.state || ''; }

(async () => {
  // 1 初始界面
  ok(byId['choose-level'].children.length === 6, '6 level cards rendered', byId['choose-level'].children.length);
  ok(A.S.level === 3, 'default level is 3', A.S.level);

  // 2 选入门档（0.5 秒），开始
  pickLevel(0);
  ok(A.S.level === 1, 'level picked', A.S.level);
  if (FALLBACK) console.log('  (FALLBACK 模式：Worker 构造直接抛错)');
  byId.start.click();
  await sleep(30);                       // start 用的是 requestAnimationFrame
  ok(!byId.game.classList.contains('hidden'), 'game screen shown');
  ok(byId.setup.classList.contains('hidden'), 'setup screen hidden');
  ok(byId['info-rules'].textContent === '无禁手', 'rules row', byId['info-rules'].textContent);
  ok(A.S.board.length === 361, 'board is 19x19');

  // 3 人类（黑）天元落子，AI 应答
  clickCell(9, 9);
  ok(A.S.board[A.idx(9, 9)] === 1, 'human stone placed');
  // 开局库命中时 AI 是同步落子的（无搜索可等），"思考中"这一帧根本留不住。
  if (A.S.moves.length === 1) ok(turnState() === 'think', 'thinking state on', turnState());
  const replied = await until(() => A.S.moves.length === 2, 8000);
  ok(replied, 'AI replied', replied ? '' : byId.turn.textContent);
  if (replied) {
    const mv = A.S.moves[1];
    ok(mv.s === 2 && A.S.board[A.idx(mv.r, mv.c)] === 2, 'AI stone is white', `at ${mv.r},${mv.c}`);
    ok(A.S.turn === 1, 'turn back to human');
    ok(byId.turn.textContent === '轮到你落子', 'turn label', byId.turn.textContent);
    ok(byId['score-readout'].textContent.length > 0, 'score readout', byId['score-readout'].textContent);
    ok(byId['info-moves'].textContent === '2', 'move counter', byId['info-moves'].textContent);
    // 这一条钉住"网页版入门与桌面版同手"：桌面 engine.ai_move(..., 1) 对天元
    // 回 (9,10)，来源是 opening_book.BOOK 里的 "180:1|2"。
    ok(mv.r === 9 && mv.c === 10, 'level-1 reply comes from the book', `at ${mv.r},${mv.c}`);
    ok(byId['score-readout'].textContent === '开局库 d9', 'book readout',
       byId['score-readout'].textContent);
  }

  // 4 悔棋：一次撤一个来回
  byId['btn-undo'].click();
  ok(A.S.moves.length === 0, 'undo cleared the exchange', A.S.moves.length);
  ok(A.S.board.every(v => v === 0), 'board cleared');
  ok(A.S.turn === A.S.human, 'turn is human again');
  ok(byId['info-undo'].textContent === '1', 'undo counter', byId['info-undo'].textContent);

  // 5 五连判定：直接摆一条横线
  const b = new Int8Array(361);
  for (let c = 3; c < 8; c++) b[A.idx(10, c)] = 1;          // 10 行 3~7 列，五连
  const line = A.winLine(b, 10, 5, 1);
  ok(line && line.length === 5, 'winLine finds five', line && line.length);
  const b4 = new Int8Array(361);
  for (let c = 3; c < 7; c++) b4[A.idx(10, c)] = 1;         // 只有四子
  ok(A.winLine(b4, 10, 6, 1) === null, 'four in a row is not a win');
  const b5 = new Int8Array(361);
  for (let i = 0; i < 4; i++) b5[A.idx(2 + i, 2 + i)] = 1;  // 斜向四子
  ok(A.winLine(b5, 5, 5, 1) === null, 'diagonal four is not a win');
  b5[A.idx(6, 6)] = 1;
  ok((A.winLine(b5, 6, 6, 1) || []).length === 5, 'diagonal five wins');

  // 6 禁手：换成 RIF，摆一个标准的三三点，看它被标出来
  pickChip('choose-rules', 'rules', 'ban');
  ok(A.S.rules === 'ban', 'rules switched to ban');
  byId['btn-restart'].click();
  await sleep(30);
  // 黑在 (9,7)(9,8) 与 (7,9)(8,9)，(9,9) 落下会同时形成两个活三 —— 3-3 禁手
  for (const [r, c] of [[9, 7], [9, 8], [7, 9], [8, 9]]) A.S.board[A.idx(r, c)] = 1;
  A.S.moves = [[9, 7], [9, 8], [7, 9], [8, 9]].map(([r, c]) => ({ r, c, s: 1 }));
  A.S.turn = 1;
  await sleep(30);                     // 让 newGame 自己那次 bancheck 先落地
  await A.refreshForbidden();
  const has = (r, c) => A.S.forbidden.some(([fr, fc]) => fr === r && fc === c);
  ok(has(9, 9), '3-3 point flagged', JSON.stringify(A.S.forbidden));

  // 7 点禁手点 → 先弹确认，再落子 → 判负
  clickCell(9, 9);
  ok(!byId.overlay.classList.contains('hidden'), 'foul overlay shown');
  ok(byId['overlay-title'].textContent.includes('禁手'), 'foul overlay title', byId['overlay-title'].textContent);
  ok(A.S.board[A.idx(9, 9)] === 0, 'foul stone not placed yet');
  byId['btn-again'].click();
  ok(A.S.board[A.idx(9, 9)] === 1, 'foul stone placed after confirm');
  ok(A.S.over && A.S.winner === 2, 'black loses on foul', 'winner=' + A.S.winner);
  ok(byId['overlay-title'].textContent === 'AI 获胜', 'overlay title', byId['overlay-title'].textContent);
  ok(turnState() === 'lose', 'turn card shows loss', turnState());

  // 8 执白：AI 应该直接下天元，不搜
  byId['btn-back'].click();
  ok(byId.game.classList.contains('hidden'), 'back to setup');
  pickChip('choose-color', 'color', '2');
  ok(A.S.human === 2, 'human is white now');
  byId.start.click();
  await sleep(30);
  const opened = await until(() => A.S.moves.length === 1, 4000);
  ok(opened && A.S.board[A.idx(9, 9)] === 1, 'AI opens at tengen', opened ? 'ok' : byId.turn.textContent);

  // 9 取消：人类执黑、宗师档，落子后立刻悔棋，必须秒回
  byId['btn-back'].click();
  pickChip('choose-color', 'color', '1');
  pickLevel(4);
  ok(A.S.level === 5, 'level 5 picked');
  byId.start.click();
  await sleep(30);
  clickCell(9, 9);
  ok(turnState() === 'think', 'thinking again', turnState());
  const t0 = Date.now();
  byId['btn-undo'].click();
  const elapsed = Date.now() - t0;
  ok(elapsed < 1500, 'undo cancels a running search', elapsed + ' ms');
  ok(A.S.moves.length === 0 && turnState() !== 'think', 'state clean after cancel', turnState());

  // 10 入门档整条开局：第一手来自开局库，其余按 depth 2 搜索。
  // 期望值来自桌面版 engine.ai_move(board, 2, 1) 的实测输出（同一张表、同一份
  // C++ 内核）：天元后 (9,10)，再之后 (10,10) 与 (8,10)。这一条是"网页版入门
  // 不降智"的端到端钉住 —— 只比对库那一条会漏掉搜索路径的差异。
  A.S.level = 1;
  A.S.rules = 'free';        // 第 6 段把规则换成了禁手；开局库只在无禁手下列
  const expect = [181, 200, 162];
  const humans = [[9, 9], [8, 8], [12, 12]];
  const got = [];
  for (let i = 0; i < humans.length; i++) {
    const before = A.S.moves.length;
    clickCell(humans[i][0], humans[i][1]);
    const arrived = await until(() => A.S.moves.length === before + 2, 8000);
    ok(arrived, `level-1 reply #${i + 1} arrived`, A.S.moves.length);
    const last = A.S.moves[A.S.moves.length - 1];
    got.push(last ? last.r * 19 + last.c : -1);
  }
  ok(JSON.stringify(got) === JSON.stringify(expect), 'level-1 opening matches the desktop',
     got.join(','));

  // 11 主题胶囊：面板上那个 ⚙ 已经删了（它和胶囊做的是同一件事），
  // 所以胶囊必须自己把深色主题切得动。
  byId['btn-theme-row'].click();
  ok(document.documentElement.classList.contains('dark'), 'theme capsule goes dark');
  ok(byId['theme-icon'].textContent === '☾', 'theme icon follows', byId['theme-icon'].textContent);
  byId['btn-theme-row'].click();
  ok(!document.documentElement.classList.contains('dark'), 'theme capsule goes back to light');

  // 12 几何：to_grid 的接受域与取整
  {
    const g = A.geom(420);
    ok(Math.abs(g.ox - (420 - 18 * g.cell) / 2) < 1e-9, 'grid is centred');
    const c0 = A.toGrid(g, g.ox, g.oy);
    ok(c0 && c0.r === 0 && c0.c === 0, 'top-left hits 0,0');
    const edge = A.toGrid(g, g.ox + 18 * g.cell, g.oy);
    ok(edge && edge.c === 18, 'bottom-right hits 18,18');
    ok(A.toGrid(g, g.ox - g.cell * 0.6, g.oy) === null, 'half a cell outside is rejected');
    const near = A.toGrid(g, g.ox + g.cell * 0.4, g.oy + g.cell * 0.4);
    ok(near && near.r === 0 && near.c === 0, 'nudged inside still rounds to 0,0');
  }

  // 13 复盘：素材只含**我自己**的手，空盘起始手不评分，逐手给结论
  {
    ok(A.reviewMoves().length === 3, '复盘素材只收我的三手', A.reviewMoves().length);
    A.S.reviewLevel = 1;
    A.openReviewPick();
    ok(!byId['review-pick'].classList.contains('hidden'), '强度页弹出');
    ok(byId['pick-levels'].children.length === 6, '强度页列出全部六档',
       byId['pick-levels'].children.length);
    await A.startReview();
    const recs = A.S.review.records;
    ok(recs.length === 3, '三手各一条记录', recs.length);
    ok(recs[0].empty === true && recs[0].best === null, '空盘起始手不评分');
    ok(A.reviewLine(recs[0]).includes('空盘起始手'), '起始手不编结论', A.reviewLine(recs[0]));
    ok(recs.slice(1).every((r) => r.best && r.delta >= 0), '评分的手分值差非负',
       JSON.stringify(recs.slice(1).map((r) => r.delta)));
    ok(byId['review-rows'].children.length === 3, '列表一行一手',
       byId['review-rows'].children.length);
    ok(byId.game.classList.contains('reviewing'), '进入复盘视图');
    ok(A.el['btn-review-exit'] && !byId['review-box'].classList.contains('hidden'),
       '复盘结果面板可见');

    // 点开第二手：棋盘换成那一手落子**之前**的局面
    const row = byId['review-rows'].children[1];
    for (const f of byId['review-rows'].listeners.click || []) f({ target: row });
    ok(A.S.reviewView === recs[1], '点行回看那一手');
    ok(recs[1].before[A.idx(9, 9)] === 1 && recs[1].before[A.idx(8, 8)] === 0,
       '回看的是落子前的局面');
    for (const f of byId['review-rows'].listeners.click || []) f({ target: row });
    ok(A.S.reviewView === null, '再点一次收起');

    byId['btn-review-exit'].click();
    ok(!byId.game.classList.contains('reviewing'), '退出复盘');
    ok(!byId.overlay.classList.contains('hidden'), '回到终局遮罩');
  }

  // 14 联机：房主权威 / 加入方发意向 / 观战只看 / 旧序号丢弃
  //     中转用两个桩替掉（真实 EventSource 与 fetch 在 node 里没有）。
  {
    const sent = [];
    win.fetch = (url, opt) => { sent.push({ url, body: JSON.parse(opt.body) }); return Promise.resolve({}); };
    win.crypto = { getRandomValues(a) { for (let i = 0; i < a.length; i++) a[i] = (i * 37 + 11) & 255; return a; } };
    let es = null;
    win.EventSource = function (url) {
      this.url = url; this.onopen = null; this.onmessage = null; this.onerror = null;
      this.closed = false;
      this.close = () => { this.closed = true; };
      es = this;
    };
    const frame = (msg) => es.onmessage({ data: JSON.stringify({ event: 'message', message: JSON.stringify(msg) }) });

    // — 房主 —
    A.enterOnline('host', 'k3m8qz');
    await sleep(30);
    ok(A.S.mode === 'online' && A.NET.role === 'host', '房主进房');
    ok(A.NET.topic === 'gomoku-k3m8qz', '主题名由房间码推出', A.NET.topic);
    ok(A.S.human === 1 && A.NET.seat === 1, '房主执黑', A.S.human);
    ok(byId['info-room'].textContent === 'K3M8QZ', '面板显示房间码', byId['info-room'].textContent);
    ok(byId.start.style.display === 'none', '"开始对局"在联机页收起');
    ok(byId['group-level'].style.display === 'none', '联机页收起难度组');

    es.onopen();
    ok(sent.length === 1 && sent[0].body.t === 'hello', '连上中转先打招呼',
       sent.map((m) => m.body.t).join(','));
    ok(sent[0].url === 'https://ntfy.sh/gomoku-k3m8qz', '发到房间主题', sent[0].url);

    clickCell(9, 9);                       // 房主落子
    ok(A.S.board[A.idx(9, 9)] === 1, '房主就地落子');
    const st = sent.filter((m) => m.body.t === 'state').pop();
    ok(st && st.body.board[A.idx(9, 9)] === 1, '房主广播整盘');
    ok(st && st.body.mv.length === 1 && st.body.mv[0] === A.idx(9, 9), '着法序列只发下标');
    ok(st && st.body.rev === 1, '序号从 1 起', st && st.body.rev);

    // 有人敲门 → 弹「想上桌」→ 同意后他执白
    frame({ from: 'peer', t: 'hello', name: '玩家AB12' });
    ok(!byId.overlay.classList.contains('hidden'), '弹上桌申请');
    ok(byId['overlay-title'].textContent.includes('玩家AB12'), '写明是谁',
       byId['overlay-title'].textContent);
    byId['btn-again'].click();
    const admit = sent.filter((m) => m.body.t === 'admit').pop();
    ok(admit && admit.body.to === 'peer' && admit.body.seat === 2, '同意他执白',
       JSON.stringify(admit && admit.body));

    frame({ from: 'peer', t: 'move', r: 8, c: 8 });    // 加入方的意向
    ok(A.S.board[A.idx(8, 8)] === 2, '房主替他落地');
    const st2 = sent.filter((m) => m.body.t === 'state').pop();
    ok(st2.body.mv.length === 2, '广播里带着他的那一手', st2.body.mv.length);

    // 轮不到他的时候发意向：房主不收，但**要回一次局面** —— 对面那颗
    // "等待确认"只有收到新 state 才松得开。
    const nState = sent.filter((m) => m.body.t === 'state').length;
    frame({ from: 'peer', t: 'move', r: 3, c: 3 });
    ok(A.S.board[A.idx(3, 3)] === 0, '不是他的回合就不收');
    ok(sent.filter((m) => m.body.t === 'state').length === nState + 1,
       '拒绝也要回一次局面');

    // 第三人敲门：桌上已经有人了 → 直接告诉他观战，别让他干等
    frame({ from: 'third', t: 'hello', name: '玩家CC33' });
    const deny = sent.filter((m) => m.body.t === 'deny').pop();
    ok(deny && deny.body.to === 'third', '后来的人只能观战',
       JSON.stringify(deny && deny.body));
    ok(byId.overlay.classList.contains('hidden'), '不再弹上桌申请');

    // — 加入方 —
    A.enterOnline('join', 'k3m8qz');
    await sleep(30);
    ok(A.NET.role === 'join' && A.NET.seat === 0, '加入方先没座位');
    ok(A.S.human === 0, '观战视角', A.S.human);
    es.onopen();
    ok(sent.filter((m) => m.body.t === 'sync').length === 1, '连上后先要局面');

    const board = new Array(361).fill(0);
    board[A.idx(9, 9)] = 1;
    frame({ from: 'host', t: 'state', rev: 5, board, mv: [A.idx(9, 9)],
            turn: 2, over: false, winner: 0, line: null, seat: 1, rules: 'free' });
    ok(A.S.board[A.idx(9, 9)] === 1, '加入方收下权威局面');
    ok(A.S.moves.length === 1 && A.S.history.length === 1, '着法序列重建');
    ok(A.S.history[0].before.every((v) => v === 0), '复盘快照也重建出来');
    ok(byId.turn.textContent.includes('等待房主同意'), '座位未定时写等待',
       byId.turn.textContent);

    const revBefore = A.NET.rev;
    frame({ from: 'host', t: 'state', rev: 3, board: new Array(361).fill(0), mv: [],
            turn: 1, over: false, winner: 0, line: null, seat: 1 });
    ok(A.S.board[A.idx(9, 9)] === 1 && A.NET.rev === revBefore, '旧序号一律丢弃');

    frame({ from: 'host', t: 'deny', to: A.NET.id });
    ok(byId.turn.textContent === '观战中', '被拒后角标写观战', byId.turn.textContent);
    ok(A.S.human === 0, '观战视角不变', A.S.human);

    frame({ from: 'host', t: 'admit', to: A.NET.id, seat: 2 });
    ok(A.NET.seat === 2 && A.S.human === 2, '同意后执白');
    ok(byId.turn.textContent.includes('轮到你'), '轮到自己写轮到你', byId.turn.textContent);

    const before = A.S.board.slice(0);
    clickCell(0, 0);
    ok(A.S.board[A.idx(0, 0)] === 0 && A.S.awaiting, '加入方只发意向，不先摆子');
    const mv = sent.filter((m) => m.body.t === 'move').pop();
    ok(mv && mv.body.r === 0 && mv.body.c === 0, '意向带上坐标', JSON.stringify(mv && mv.body));

    // 同一个意向再来一次不能变成两手
    clickCell(0, 1);
    ok(!sent.filter((m) => m.body.t === 'move').some((m) => m.body.c === 1),
       '等确认期间不收第二手');
    ok(A.S.board.every((v, i) => v === before[i]), '局面没被本地改动');

    // — 观战：没座位的人点了不动 —
    frame({ from: 'host', t: 'state', rev: 7,
            board: new Array(361).fill(0), mv: [], turn: 1,
            over: false, winner: 0, line: null, seat: 1 });
    A.NET.seat = 0; A.S.human = 0;
    clickCell(3, 3);
    ok(A.S.board[A.idx(3, 3)] === 0, '观战者不能落子');

    A.netClose();
    ok(!A.NET.active && A.NET.seat === 0, '退席清干净');
  }

  console.log('---');
  console.log(fails ? `${fails} FAILED` : 'all passed');
  console.log('done, exit', process.exitCode || 0);
  setTimeout(() => process.exit(process.exitCode || 0), 50);
})();
