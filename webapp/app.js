/* 网页版五子棋：Canvas 画盘 + 一个 Web Worker 跑 wasm 引擎。
 *
 * 与桌面版的关系：
 *   - 棋盘是照着 `board_render.py` 重写的（木纹、内嵌面、星位、坐标、棋子
 *     sprite、最后一手环、禁手标记），连"深色衬 + 亮色芯"这类细节都照搬；
 *   - 配色逐个取自 `theme.py`，两套主题的 token 名一一对应；
 *   - 难度表、禁手规则、开局天元，照 `engine.py` / `session.py`；
 *   - 引擎是同一份 C++ 源码编出来的 wasm（见 `cpp/web/build_wasm.sh`）。
 *
 * 几个约定值得写下来：
 *   1. **一切路径都是相对的**（`./app.js`、`./gomoku-free.js`）。Toy 把作品挂在
 *      bilibili.com/toy/<slug>/ 下，根绝对路径会 404 成白屏。
 *   2. **搜索在主线程之外**。3 秒的中级档会把主线程冻住 3 秒；Worker +
 *      terminate 也是唯一能真正打断搜索的手段。
 *   3. 棋盘状态只存在这里（`S.board`），Worker 每次都收一份完整棋盘 —— 引擎
 *      无状态调用，和桌面版每步都发整盘是同一个约定。
 */
'use strict';

const N = 19;                 // 棋盘边长，与 constants.h 的 BOARD_SIZE 一致
const CELLS = N * N;
const CENTER = 9;             // 天元

//: 列名字母表，跳过 I —— 与 gamelog.COL_LETTERS 同一张表。不跳 I 的写法会让
//: 19 列里有 11 列对不上棋谱记法。
const COL_LETTERS = 'ABCDEFGHJKLMNOPQRSTUVWXYZ';

//: 难度表。逐项对应 `engine.py` 的 `DIFFICULTY`，只有第 6 档改了时限：
//: 桌面版是 300 秒一手（Gomocup 决赛圈口径），网页里让人盯着转一分钟已经很
//: 极限，所以按比例缩到 60 秒、余量 4 秒。**这是网页版唯一一处参数偏离**，
//: 卡片上会写明。
const LEVELS = [
  { id: 1, name: '入门', time: 0.5, depth: 2, vcf: 0.0, qply: 2, enhance: false,
    bias: [1.5, 0.5, 0.0] },
  { id: 2, name: '初级', time: 3.0, depth: 4, vcf: 0.3, qply: 4, enhance: false },
  { id: 3, name: '中级', time: 7.0, depth: 10, vcf: 0.5, qply: 8, enhance: false },
  { id: 4, name: '高级', time: 15.0, depth: 24, vcf: 1.5, qply: 10, enhance: true },
  { id: 5, name: '宗师', time: 20.0, depth: 24, vcf: 3.0, qply: 12, enhance: true },
  { id: 6, name: 'Gomocup', time: 60.0, depth: 40, vcf: 8.0, qply: 16,
    enhance: true, reserve: 4.0, note: '比赛档' },
];

/* ------------------------------------------------------- 棋盘配色
 *
 * 全部来自 theme.py 的"棋盘（共享）"那一段。这些颜色**不随主题变** ——
 * 木盘是全局唯一的暖色焦点，深色主题下也不该换一块木板。
 */
const WOOD_DARK = '#cf8f42';
const WOOD_LITE = '#e5ab5c';
const WOOD_EDGE = '#8a6634';
const GRID = '#5f3d15';
const STAR = '#4f2f0c';
const COORD = '#41250a';
const STONE_B_RIM = '#3a382f';
const STONE_W_RIM = GRID;
const LAST_LIGHT = '#f2ead9';
const LAST_DARK = '#4a2c0f';
const WIN_LINE = '#b0303a';        // = light 主题的 DANGER，跨主题固定
const FORBIDDEN = WIN_LINE;
const FORBIDDEN_EDGE = 'rgba(30, 14, 4, 0.667)';
const STONE_B_GRAD = [[0, '#5c6169'], [0.55, '#23262b'], [1, '#0d0f12']];
const STONE_W_GRAD = [[0, '#fffdf9'], [0.55, '#efece4'], [1, '#cdc6b8']];
const STONE_SHADOW_NEAR = 'rgba(74, 44, 15, 0.275)';
const STONE_SHADOW_FAR = 'rgba(74, 44, 15, 0.216)';
const FACE_TINT = 'rgba(255, 240, 215, 0.102)';
const PLATE_TOP_TINT = 'rgba(255, 244, 220, 0.18)';
const PLATE_BOT_SHADOW = 'rgba(0, 0, 0, 0.235)';

//: 几何设计基准，与 board_geometry.py 的 `design()` 同一组数：18 段 × 34 +
//: 两侧各 57 = 726。缩放比 k = 控件边长 / 726。
const DESIGN = { cell: 34, pad: 57, side: 726 };

/* ------------------------------------------------------- 状态 */

const S = {
  rules: 'free',      // free | ban
  mode: 'ai',         // ai | online
  human: 1,           // 1 黑 2 白（联机里是**我**这一席的颜色；观战为 0）
  level: 3,
  board: new Int8Array(CELLS),
  moves: [],          // {r, c, s}
  turn: 1,
  over: false,
  winner: 0,
  winLine: null,
  thinking: false,
  forbidden: [],      // [[r,c], ...]
  hover: -1,
  pendingFoul: -1,
  undos: 0,
  startedAt: 0,
  elapsed: 0,
  series: [],         // 每一手 AI 的搜索结果，图表用
  lastReadout: '',
  history: [],        // 复盘素材：每一手**落子前**的局面 {seq, r, c, s, before}
  awaiting: false,    // 联机：着法已发出，等房主把权威局面同步回来
  review: null,       // {records, level}
  reviewLevel: 0,     // 复盘强度，0 = 跟随本局难度
  reviewView: null,   // 正在回看的那一手（棋盘画的是它落子前的局面）
};

/* ------------------------------------------------------------ 引擎 */

//: Worker 起不来时的兜底信号：区别于"被用户打断"（cancelled），前者要换条路
//: 重试，后者必须原样往上传。
const DEAD = { dead: true };

class Engine {
  constructor() {
    this.worker = null;
    this.rules = null;
    this.seq = 0;
    this.waiting = new Map();
    this.fallback = false;   // true = 在主线程上跑，界面会卡
    this.core = null;
    this.api = null;
    //: 每 kill 一次 +1。主线程退路里搜索是同步的，`kill()` 拦不住已经开始的那次；
    //: 但**还没开始**的那次（卡在 `await loadCore()` / `await boot()` 上）必须作废
    //: —— 否则用户悔棋、重开之后，旧的那次搜索还会在主线程上冻住十几秒，算出来
    //: 的结果也可能被下一手当成自己的。
    this.gen = 0;
  }

  //: 换规则就要换模块（两份 wasm 是编译期分开的），所以规则变了就重建 Worker。
  ensure(rules) {
    if (this.rules === rules && (this.worker || this.fallback)) return;
    this.kill();
    this.rules = rules;
    if (this.fallback) return;
    try {
      //: `#ban` / `#free` 决定 Worker 里 import 哪一份 wasm —— 片段不会发给
      //: 服务器，见 engine-worker.js 顶部的说明。
      this.worker = new Worker('./engine-worker.js#' + rules);
      this.worker.onmessage = (ev) => {
        const msg = ev.data;
        const slot = this.waiting.get(msg.id);
        if (!slot) return;
        this.waiting.delete(msg.id);
        if (msg.error) slot.reject(new Error(msg.error));
        else slot.resolve(msg.result);
      };
      this.worker.onerror = () => {
        for (const slot of this.waiting.values()) slot.reject(DEAD);
        this.waiting.clear();
      };
    } catch (e) {
      this.useFallback();
    }
  }

  //: file:// 直接打开页面时走这里。搜索期间界面会僵住，但能下棋。
  useFallback() {
    this.fallback = true;
    this.worker = null;
    //: 面板上如实写出来。主线程跑 wasm 时思考期间整页僵住，用户有权知道
    //: 这是环境限制而不是程序卡死。
    if (el['info-engine']) updateInfo();
  }

  kill() {
    this.gen += 1;
    if (this.worker) this.worker.terminate();
    this.worker = null;
    this.rules = null;
    this.api = null;        // 主线程模式换规则时要重新 boot，别拿着旧模块用
    for (const slot of this.waiting.values()) slot.reject(new Error('cancelled'));
    this.waiting.clear();
  }

  request(payload) {
    const id = ++this.seq;
    return new Promise((resolve, reject) => {
      this.waiting.set(id, { resolve, reject });
      this.worker.postMessage(Object.assign({ id }, payload));
    });
  }

  async loadCore() {
    if (this.core) return this.core;
    await new Promise((resolve, reject) => {
      if (self.GomokuCore) { resolve(); return; }
      const tag = document.createElement('script');
      tag.src = './engine-worker.js';
      tag.onload = resolve;
      tag.onerror = () => reject(new Error('引擎脚本加载失败'));
      document.head.appendChild(tag);
    });
    this.core = self.GomokuCore;
    return this.core;
  }

  async call(payload) {
    if (this.fallback) {
      //: rules 要在 await 之前抓下来：搜索期间用户可能已经重开，kill() 会把
      //: this.rules 清空。两个 await 之间都要重新验一次 gen —— 加载脚本、建表
      //: 各自要花掉几十毫秒，这期间用户完全来得及按悔棋或重开。作废的请求按
      //: "被取消"处理，与 Worker 那条路上 terminate 的效果对齐。
      const rules = this.rules;
      const gen = this.gen;
      const core = await this.loadCore();
      if (gen !== this.gen) throw new Error('cancelled');
      this.api = this.api || await core.boot(rules);
      if (gen !== this.gen) throw new Error('cancelled');
      return core.run(this.api, payload);
    }
    try {
      return await this.request(payload);
    } catch (e) {
      if (e !== DEAD) throw e;
      this.useFallback();
      return this.call(payload);
    }
  }

  //: ``opts.collect`` 要根候选表（每一手的评分），复盘页靠它判"这一手差多少"。
  async think(cells, me, level, opts) {
    this.ensure(S.rules);
    return this.call({ cmd: 'think', cells, me, level,
                       collect: !!(opts && opts.collect) });
  }

  async bancheck(cells) {
    if (S.rules !== 'ban') return [];
    this.ensure(S.rules);
    const res = await this.call({ cmd: 'bancheck', cells });
    return res.points;
  }
}

const engine = new Engine();

/* ------------------------------------------------------------ 开局库
 *
 * 桌面版 `engine.ai_move` 的第一道闸门就是这张表：命中就直接落库里的那手，
 * 不进搜索。网页版前几版没有这张表，于是"入门"档（深度 2）在天元之后回了
 * (7,7) —— 而桌面版同一档回的是库里的 (9,10)，整整差了两档的棋力。用户说的
 * "网页版入门降智"就是它。
 *
 * `book.js` 由 `tools/build_book_web.py` 从 `opening_book.py` 生成：那边的键
 * 是 Zobrist 哈希，这边换成"按索引升序的 `格子:子色` 列表 + `|` + 走子方"的
 * 明文键，省掉在浏览器里重算 MT19937。**生成物，不要手改。**
 *
 * 闸门与桌面版逐条对齐：只认**查表命中**（不是"盘上子少"），且只在无禁手
 * 规则下查 —— 这张表是无禁手规则下建的（`engine_ban.USE_OPENING_BOOK`）。
 */
const BOOK = (typeof self !== 'undefined' && self.GomokuBook) ||
             { stones: 3, table: {} };

function bookLookup(cells, me) {
  if (S.rules !== 'free') return null;
  let key = '';
  let n = 0;
  for (let i = 0; i < CELLS; i++) {
    const s = cells[i];
    if (!s) continue;
    if (++n > BOOK.stones) return null;      // 库只覆盖开局，多一颗就退出
    key += (n > 1 ? ',' : '') + i + ':' + s;
  }
  if (!n) return null;                        // 空盘另走天元，不经这里
  return BOOK.table[key + '|' + me] || null;
}

/* ------------------------------------------------------------ 棋盘规则 */

function idx(r, c) { return r * N + c; }

//: 以 (r, c) 为起点、沿 (dr, dc) 方向的最长同色连。**只看刚落的那一子**，
//: 这是桌面版 `check_win` 的做法：新棋子不参与的那条线不可能因为这一手变成五连。
function lineLen(board, r, c, dr, dc, stone) {
  let n = 1;
  for (let i = 1; i < N; i++) {
    const rr = r + dr * i, cc = c + dc * i;
    if (rr < 0 || rr >= N || cc < 0 || cc >= N || board[idx(rr, cc)] !== stone) break;
    n++;
  }
  for (let i = 1; i < N; i++) {
    const rr = r - dr * i, cc = c - dc * i;
    if (rr < 0 || rr >= N || cc < 0 || cc >= N || board[idx(rr, cc)] !== stone) break;
    n++;
  }
  return n;
}

const DIRS = [[1, 0], [0, 1], [1, 1], [1, -1]];

//: 返回获胜的那条线（连成五子的格索引），没有则返回 null。
function winLine(board, r, c, stone) {
  for (const [dr, dc] of DIRS) {
    const len = lineLen(board, r, c, dr, dc, stone);
    if (len < 5) continue;
    const line = [];
    for (let i = -(len - 1); i <= len - 1; i++) {
      const rr = r + dr * i, cc = c + dc * i;
      if (rr < 0 || rr >= N || cc < 0 || cc >= N) continue;
      if (board[idx(rr, cc)] === stone) line.push(idx(rr, cc));
    }
    return line;
  }
  return null;
}

/* ------------------------------------------------------------ 几何 */

//: 与 board_geometry.BoardGeometry.fit() 同一套：棋盘控件是正方形，网格居中，
//: 缩放比取短边 / 726。
function geom(size) {
  const k = size / DESIGN.side;
  const cell = DESIGN.cell * k;
  const span = (N - 1) * cell;
  return { k, cell, span, ox: (size - span) / 2, oy: (size - span) / 2, size };
}

//: 像素 -> 最近的格点，落在框外半格则 null。与 `to_grid()` 的三条修正一致：
//: 接受域对称、不用银行的 round、越界返回 null 而不是钳边。
function toGrid(g, x, y) {
  const fx = (x - g.ox) / g.cell;
  const fy = (y - g.oy) / g.cell;
  if (!(fx >= -0.5 && fx <= N - 0.5 && fy >= -0.5 && fy <= N - 0.5)) return null;
  const c = Math.min(N - 1, Math.max(0, Math.floor(fx + 0.5)));
  const r = Math.min(N - 1, Math.max(0, Math.floor(fy + 0.5)));
  return { r, c };
}

/* ------------------------------------------------------------ 木纹 */

let woodCanvas = null;

//: 程序化木纹，照 `_build_wood()` 的公式：两条不同周期的正弦叠加成弯曲的竖纹，
//: 再压一层 ±10% 的光照梯度（左上亮、右下暗）。
//:
//: 与 Python 版唯一的差别是噪声：那边用盒式模糊，这里用"64×64 随机灰度放大
//: 后双线性插值"—— 效果一样（都是柔和斑块），代码少一个卷积。**逐像素的
//: 白噪声绝对不行**，在木色上看像砂纸。
function buildWood() {
  const n = 512;
  const sx = 768 / n;              // 采样缩放：保持与 Python 版相同的纹理频率

  const small = document.createElement('canvas');
  small.width = small.height = 64;
  const sg = small.getContext('2d');
  const sImg = sg.createImageData(64, 64);
  let seed = 20240607;
  const rnd = () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
  for (let i = 0; i < 64 * 64; i++) {
    const v = 128 + (rnd() - 0.5) * 92;
    sImg.data[i * 4] = sImg.data[i * 4 + 1] = sImg.data[i * 4 + 2] = v;
    sImg.data[i * 4 + 3] = 255;
  }
  sg.putImageData(sImg, 0, 0);

  const nc = document.createElement('canvas');
  nc.width = nc.height = n;
  const ng = nc.getContext('2d');
  ng.imageSmoothingEnabled = true;
  ng.imageSmoothingQuality = 'high';
  ng.drawImage(small, 0, 0, n, n);
  const noise = ng.getImageData(0, 0, n, n).data;

  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = n;
  const c = canvas.getContext('2d');
  const out = c.createImageData(n, n);

  const dark = [0xcf, 0x8f, 0x42];
  const lite = [0xe5, 0xab, 0x5c];

  for (let y = 0; y < n; y++) {
    const yy = y * sx;
    for (let x = 0; x < n; x++) {
      const xx = x * sx;
      const warp = 18 * Math.sin(xx / 120) + 9 * Math.sin(xx / 37 + 1.7);
      const grain = Math.sin((yy + warp) / 7.5) ** 2 * 0.55
                  + Math.sin((yy + warp) / 2.3) * 0.18;
      const nz = (noise[(y * n + x) * 4 + 1] - 128) / 128 * 0.10;
      let tex = 0.5 + 0.5 * (grain + nz);
      tex = tex < 0 ? 0 : tex > 1 ? 1 : tex;
      const lum = ((n - 1 - x) + (n - 1 - y)) / (2 * (n - 1));
      let t = 0.5 * lum + 0.5 * tex;
      t = t < 0 ? 0 : t > 1 ? 1 : t;
      const i = (y * n + x) * 4;
      out.data[i] = dark[0] + (lite[0] - dark[0]) * t;
      out.data[i + 1] = dark[1] + (lite[1] - dark[1]) * t;
      out.data[i + 2] = dark[2] + (lite[2] - dark[2]) * t;
      out.data[i + 3] = 255;
    }
  }
  c.putImageData(out, 0, 0);
  return canvas;
}

/* ------------------------------------------------------------ 绘制 */

const canvas = document.getElementById('board');
const ctx = canvas.getContext('2d');

function roundRect(c, x, y, w, h, r) {
  const rr = Math.min(r, w / 2, h / 2);
  c.beginPath();
  c.moveTo(x + rr, y);
  c.lineTo(x + w - rr, y);
  c.arcTo(x + w, y, x + w, y + rr, rr);
  c.lineTo(x + w, y + h - rr);
  c.arcTo(x + w, y + h, x + w - rr, y + h, rr);
  c.lineTo(x + rr, y + h);
  c.arcTo(x, y + h, x, y + h - rr, rr);
  c.lineTo(x, y + rr);
  c.arcTo(x, y, x + rr, y, rr);
  c.closePath();
}

//: 棋子：两层接触投影 + 径向渐变 + 描边。高光固定在左上（光源不变，所以整盘
//: 共用同一个渐变配方）。
function drawStone(c, x, y, r, stone, alpha) {
  c.save();
  if (alpha !== undefined) c.globalAlpha = alpha;

  c.fillStyle = STONE_SHADOW_NEAR;
  c.beginPath();
  c.ellipse(x, y + r * 0.16, r * 0.98, r * 0.98, 0, 0, 7);
  c.fill();
  c.fillStyle = STONE_SHADOW_FAR;
  c.beginPath();
  c.ellipse(x, y + r * 0.27, r * 0.86, r * 0.86, 0, 0, 7);
  c.fill();

  const stops = stone === 1 ? STONE_B_GRAD : STONE_W_GRAD;
  const g = c.createRadialGradient(x - r * 0.32, y - r * 0.34, r * 0.05,
                                   x, y, r * 1.45);
  for (const [pos, col] of stops) g.addColorStop(pos, col);
  c.fillStyle = g;
  c.beginPath();
  c.arc(x, y, r, 0, 7);
  c.fill();

  c.strokeStyle = stone === 1 ? STONE_B_RIM : STONE_W_RIM;
  c.lineWidth = Math.max(1, r * 0.11);
  c.stroke();
  c.restore();
}

//: 禁手标记：半透明红圆 + 红环 + 红叉，全部几何图形。环与叉先描一层深棕衬、
//: 再压红色芯 —— 这支红落在橙金木盘上对比度不到 3.0，不衬底读不出来。
function drawForbidden(c, x, y, r) {
  const ring = r * 0.80;
  const lineW = Math.max(1.4, r * 0.20);
  const backW = lineW + Math.max(1, r * 0.14);
  const a = ring * 0.50;

  c.save();
  c.fillStyle = FORBIDDEN;
  c.globalAlpha = 46 / 255;
  c.beginPath();
  c.arc(x, y, ring, 0, 7);
  c.fill();
  c.globalAlpha = 1;

  for (const [col, w] of [[FORBIDDEN_EDGE, backW], [FORBIDDEN, lineW]]) {
    c.strokeStyle = col;
    c.lineWidth = w;
    c.lineCap = 'round';
    c.beginPath();
    c.arc(x, y, ring, 0, 7);
    c.moveTo(x - a, y - a); c.lineTo(x + a, y + a);
    c.moveTo(x - a, y + a); c.lineTo(x + a, y - a);
    c.stroke();
  }
  c.restore();
}

//: 把画布按 devicePixelRatio 调成实际像素，并把坐标系缩回 CSS 像素。
function sizeCanvas(cv) {
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  const w = cv.clientWidth || 0;
  const h = cv.clientHeight || 0;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
  }
  const c = cv.getContext('2d');
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { c, w, h };
}

function draw() {
  const w = canvas.clientWidth || 600;
  const { c } = sizeCanvas(canvas);
  const g = geom(w);
  const k = g.k;
  //: 复盘棋盘展示的是"那一手**落子之前**"的局面，不是终局 —— 快照在
  //: `S.reviewView.before` 里，画完就丢，不动 `S.board`。
  const view = S.reviewView;
  const board = view ? view.before : S.board;

  c.clearRect(0, 0, w, w);

  const plateR = 16 * Math.max(0.6, Math.min(1.4, k));
  const lineW = Math.max(1, 1.7 * k);

  // ① 木盘 + 外投影（木牌压在页面底上的厚度感）
  c.save();
  c.shadowColor = 'rgba(0, 0, 0, 0.18)';
  c.shadowBlur = 18 * k;
  c.shadowOffsetY = 4 * k;
  c.fillStyle = WOOD_DARK;
  roundRect(c, 0, 0, w, w, plateR);
  c.fill();
  c.restore();

  // ② 木纹铺满木盘
  c.save();
  roundRect(c, 0, 0, w, w, plateR);
  c.clip();
  if (!woodCanvas) woodCanvas = buildWood();
  c.drawImage(woodCanvas, 0, 0, w, w);

  // ②' 内缘光照收边：顶缘提亮、底缘压暗
  const edge = Math.max(6, w * 0.025);
  let grd = c.createLinearGradient(0, 0, 0, edge);
  grd.addColorStop(0, PLATE_TOP_TINT);
  grd.addColorStop(1, 'rgba(255, 244, 220, 0)');
  c.fillStyle = grd;
  c.fillRect(0, 0, w, edge);
  grd = c.createLinearGradient(0, w - edge, 0, w);
  grd.addColorStop(0, 'rgba(0, 0, 0, 0)');
  grd.addColorStop(1, PLATE_BOT_SHADOW);
  c.fillStyle = grd;
  c.fillRect(0, w - edge, w, edge);
  c.restore();

  // ③ 内嵌棋盘面：略亮的凹面，制造"棋盘嵌进木框"的层次
  const facePad = g.cell * 0.62;
  c.fillStyle = FACE_TINT;
  c.strokeStyle = WOOD_EDGE;
  c.lineWidth = lineW;
  roundRect(c, g.ox - facePad, g.oy - facePad,
            g.span + 2 * facePad, g.span + 2 * facePad, 8 * k);
  c.fill();
  c.stroke();

  // ④ 木盘外描边
  roundRect(c, lineW / 2, lineW / 2, w - lineW, w - lineW, plateR);
  c.stroke();

  // ⑤ 网格：19 个格点 = 18 段
  c.strokeStyle = GRID;
  c.lineWidth = lineW;
  c.beginPath();
  for (let i = 0; i < N; i++) {
    const p = g.ox + i * g.cell;
    c.moveTo(p, g.oy);
    c.lineTo(p, g.oy + g.span);
    c.moveTo(g.ox, p);
    c.lineTo(g.ox + g.span, p);
  }
  c.stroke();

  // ⑥ 星位：19 路是传统九星
  c.fillStyle = STAR;
  const starR = Math.max(1.8, g.cell * 0.11);
  for (const [r, col] of [[3, 3], [3, 9], [3, 15], [9, 3], [9, 9],
                          [9, 15], [15, 3], [15, 9], [15, 15]]) {
    c.beginPath();
    c.arc(g.ox + col * g.cell, g.oy + r * g.cell, starR, 0, 7);
    c.fill();
  }

  // ⑦ 坐标：列字母跳过 I，行号右侧对齐
  const fs = Math.max(9, 12 * k);
  c.font = `${fs}px "DejaVu Sans Mono", Menlo, Consolas, monospace`;
  c.fillStyle = COORD;
  const gap = g.cell * 0.18;
  c.textAlign = 'center';
  c.textBaseline = 'bottom';
  for (let i = 0; i < N; i++) {
    c.fillText(COL_LETTERS[i], g.ox + i * g.cell, g.oy - gap);
  }
  c.textAlign = 'right';
  c.textBaseline = 'middle';
  for (let i = 0; i < N; i++) {
    c.fillText(String(i + 1), g.ox - gap, g.oy + i * g.cell);
  }

  // ⑧ 悬停幽灵（复盘时不出：那张盘是只读的回放）
  if (!view && S.hover >= 0 && !S.over && !S.thinking
      && board[S.hover] === 0) {
    const r = Math.floor(S.hover / N), col = S.hover % N;
    drawStone(c, g.ox + col * g.cell, g.oy + r * g.cell,
              g.cell * 0.44, S.turn, 0.25);
  }

  // ⑨ 棋子
  const stoneR = g.cell * 0.44;
  for (let i = 0; i < CELLS; i++) {
    const s = board[i];
    if (!s) continue;
    drawStone(c, g.ox + (i % N) * g.cell, g.oy + Math.floor(i / N) * g.cell,
              stoneR, s);
  }

  // ⑨' 复盘标记：幽灵子 = 实际下的那一手（它不在 `before` 里），圈 = 引擎
  // 认为该下的那一手。两层都画才对照得起来 —— 与桌面版 `set_review_marker`
  // 同一套画法（幽灵子先铺底，圈压在上面）。
  if (view) {
    const gx = g.ox + view.played[1] * g.cell;
    const gy = g.oy + view.played[0] * g.cell;
    drawStone(c, gx, gy, stoneR, view.s, 0.47);
    if (view.best) {
      const bx = g.ox + view.best[1] * g.cell;
      const by = g.oy + view.best[0] * g.cell;
      c.save();
      c.beginPath();
      c.arc(bx, by, stoneR * 0.9, 0, 7);
      c.strokeStyle = LAST_DARK;
      c.lineWidth = Math.max(2, stoneR * 0.36);
      c.stroke();
      c.strokeStyle = WIN_LINE;
      c.lineWidth = Math.max(1.5, stoneR * 0.20);
      c.stroke();
      c.restore();
    }
  }

  // ⑩ 最后一手环：黑子上用近白环，白子上用深棕环
  if (!view && S.moves.length && !S.winLine) {
    const m = S.moves[S.moves.length - 1];
    c.strokeStyle = m.s === 1 ? LAST_LIGHT : LAST_DARK;
    c.lineWidth = Math.max(1.5, stoneR * 0.16);
    c.beginPath();
    c.arc(g.ox + m.c * g.cell, g.oy + m.r * g.cell, stoneR * 0.74, 0, 7);
    c.stroke();
  }

  // ⑪ 禁手点（复盘回放里不提示：那张盘已经下完了）
  for (const [r, col] of (view ? [] : S.forbidden)) {
    drawForbidden(c, g.ox + col * g.cell, g.oy + r * g.cell, stoneR);
  }

  // ⑫ 胜利连线
  if (!view && S.winLine) {
    const a = S.winLine[0], b = S.winLine[S.winLine.length - 1];
    c.strokeStyle = WIN_LINE;
    c.lineWidth = Math.max(3, g.cell * 0.14);
    c.lineCap = 'round';
    c.beginPath();
    c.moveTo(g.ox + (a % N) * g.cell, g.oy + Math.floor(a / N) * g.cell);
    c.lineTo(g.ox + (b % N) * g.cell, g.oy + Math.floor(b / N) * g.cell);
    c.stroke();
    c.lineCap = 'butt';
  }
}

/* ------------------------------------------------------------ 图表 */

//: analysis.win_probability 的锚点表：引擎原始分取 log10，在相邻锚点之间线性
//: 插值；杀棋分直接钉到 100 / 0（那是搜索证明出来的，不是估计）。
const STATIC_MAX = 9999872;
const WIN_ANCHORS = [
  [10, 50.5], [100, 51.0], [500, 53.0], [1000, 55.0], [30000, 65.0],
  [100000, 76.0], [500000, 88.0], [600000, 91.0], [700000, 93.0],
  [1000000, 97.0], [STATIC_MAX, 98.5],
];

function winProbability(score) {
  if (score > STATIC_MAX) return 100;
  if (score < -STATIC_MAX) return 0;
  if (score === 0) return 50;
  const sign = score > 0 ? 1 : -1;
  const x = Math.log10(Math.abs(score));
  const logs = WIN_ANCHORS.map(([s]) => Math.log10(s));
  let p = 98.5;
  if (x <= logs[0]) {
    p = WIN_ANCHORS[0][1];
  } else if (x < logs[logs.length - 1]) {
    for (let i = 1; i < logs.length; i++) {
      if (x <= logs[i]) {
        const p1 = WIN_ANCHORS[i - 1][1], p2 = WIN_ANCHORS[i][1];
        p = p1 + (x - logs[i - 1]) / (logs[i] - logs[i - 1]) * (p2 - p1);
        break;
      }
    }
  }
  return 50 + sign * (p - 50);
}

function cssVar(name) {
  return getComputedStyle(document.body).getPropertyValue(name).trim();
}

//: 一个极简折线图：横向等距取点，纵向由 `map` 归一化到 0..1（0 在底）。
function drawChart(cv, values, mapY, ticks, color) {
  if (!cv) return;
  const { c, w, h } = sizeCanvas(cv);
  c.clearRect(0, 0, w, h);

  const padL = 26, padR = 4, padT = 4, padB = 6;
  const plotW = Math.max(1, w - padL - padR);
  const plotH = Math.max(1, h - padT - padB);

  c.strokeStyle = cssVar('--border');
  c.lineWidth = 1;
  c.font = '10px "DejaVu Sans Mono", Menlo, monospace';
  c.fillStyle = cssVar('--text-faint');
  c.textAlign = 'left';
  c.textBaseline = 'middle';

  for (const [t, label] of ticks) {
    const y = padT + (1 - t) * plotH;
    c.globalAlpha = 0.35;
    c.beginPath();
    c.moveTo(padL, y);
    c.lineTo(w - padR, y);
    c.stroke();
    if (label) {
      c.globalAlpha = 1;
      c.fillText(label, 0, y);
    }
  }
  c.globalAlpha = 1;

  if (values.length < 1) return;

  const n = values.length;
  const px = (i) => padL + (n === 1 ? plotW / 2 : i / (n - 1) * plotW);
  const py = (v) => padT + (1 - mapY(v)) * plotH;

  c.strokeStyle = color;
  c.lineWidth = 1.6;
  c.lineJoin = 'round';
  c.beginPath();
  values.forEach((v, i) => (i ? c.lineTo(px(i), py(v)) : c.moveTo(px(i), py(v))));
  c.stroke();

  c.fillStyle = color;
  c.beginPath();
  c.arc(px(n - 1), py(values[n - 1]), 2.2, 0, 7);
  c.fill();
}

function renderCharts() {
  const vals = S.series.map((s) => s.val);

  //: 评分图纵轴用 symlog：分值跨 1e1…1e7，线性轴会把除了最后一手以外的一切
  //: 压成零线。数量级只增不减，与桌面版 `_decade` 同一个规矩。
  let decade = 2;
  for (const v of vals) {
    const d = Math.log10(Math.abs(v) + 1);
    if (d > decade) decade = Math.ceil(d);
  }
  const span = Math.log10(Math.pow(10, decade) + 1);

  drawChart(
    document.getElementById('chart-score'), vals,
    (v) => 0.5 + 0.5 * (Math.sign(v) * Math.log10(Math.abs(v) + 1) / span),
    [[1, '1e' + decade], [0.5, '0'], [0, '-1e' + decade]],
    cssVar('--info'));

  drawChart(
    document.getElementById('chart-win'), vals.map((v) => winProbability(-v)),
    (p) => p / 100,
    [[1, '100'], [0.5, '50'], [0, '0']],
    cssVar('--accent'));
}

/* ------------------------------------------------------------ 界面 */

const el = {};
for (const id of ['setup', 'game', 'choose-color', 'choose-level', 'choose-rules',
                  'start', 'setup-hint', 'overlay', 'overlay-title', 'overlay-text',
                  'btn-again', 'btn-overlay-back', 'btn-undo', 'btn-restart',
                  'btn-back', 'btn-theme-row', 'theme-icon',
                  'turn', 'turn-card', 'turn-stone', 'info-rules', 'info-level',
                  'info-undo', 'info-moves', 'info-time', 'info-engine',
                  'score-readout', 'chart-score-label',
                  // 联机
                  'choose-mode', 'group-online', 'group-color', 'group-level',
                  'group-rules',
                  'btn-create-room', 'btn-join-room', 'room-code', 'online-status',
                  'info-room-row', 'info-room', 'online-note',
                  // 复盘
                  'btn-review', 'review-pick', 'pick-levels', 'btn-review-start',
                  'btn-review-close', 'review-run', 'run-step', 'run-bar',
                  'btn-run-cancel', 'review-box', 'review-title', 'review-count',
                  'review-rows',
                  'review-readout', 'btn-review-exit']) {
  el[id] = document.getElementById(id);
}

function renderLevelCards() {
  el['choose-level'].innerHTML = '';
  for (const lv of LEVELS) {
    const b = document.createElement('button');
    b.className = 'card' + (lv.id === S.level ? ' is-on' : '');
    b.dataset.level = String(lv.id);
    //: 卡面是 N 颗棋子 —— 与桌面版 `_strength_bar([bar_player] * level)` 同一
    //: 个意思：难度是一条"棋子越排越长"的刻度，比六张纯文字的卡片好认。
    b.innerHTML =
      `<span class="card-index">${String(lv.id).padStart(2, '0')}</span>` +
      `<span class="card-face">${'<i class="stone-icon black"></i>'.repeat(lv.id)}</span>` +
      `<span class="card-text">${lv.name}</span>` +
      `<span class="card-sub">思考上限 ${lv.time} 秒` +
      (lv.note ? `<br>${lv.note}` : '') + '</span>';
    el['choose-level'].appendChild(b);
  }
}

function markSelected(group, attr, value) {
  for (const b of group.querySelectorAll('.card')) {
    b.classList.toggle('is-on', b.dataset[attr] === String(value));
  }
}

function setTurnCard(text, state) {
  el.turn.textContent = text;
  el['turn-card'].dataset.state = state || '';
}

function setStoneIcon(node, player) {
  node.classList.toggle('black', player === 1);
  node.classList.toggle('white', player === 2);
}

function fmtTime(sec) {
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function updateInfo() {
  //: 图题的"AI（白棋）"跟着执棋颜色走 —— 桌面版同一处也这么写，省得看曲线时
  //: 要回头想这一局自己执的什么色。
  el['chart-score-label'].textContent =
    'AI（' + (S.human === 1 ? '白棋' : '黑棋') + '）评分';
  el['info-engine'].textContent = engine.fallback ? 'C++ · WASM（主线程）' : 'C++ · WASM';
  if (engine.fallback) {
    el['info-engine'].title = '这个环境不允许 Web Worker，引擎跑在页面主线程上：'
      + 'AI 思考期间界面会卡住，但棋力与 Worker 模式完全相同。';
  }
  el['info-rules'].textContent = S.rules === 'ban' ? '禁手' : '无禁手';
  //: 联机局里没有 AI，"难度"一行写个"—"，别让人以为对面是引擎。
  el['info-level'].textContent =
    S.mode === 'online' ? '—' : LEVELS[S.level - 1].name;
  el['info-undo'].textContent = String(S.undos);
  el['info-moves'].textContent = String(S.moves.length);
  el['info-time'].textContent = fmtTime(S.elapsed);
  el['btn-undo'].disabled = S.moves.length === 0;
  //: 联机里悔棋与重开都由房主掌握（重开由房主一点，两边同步）。加入方那
  //: 两颗按钮收起来，免得点了没反应像是坏了。
  const guest = S.mode === 'online' && NET.role !== 'host';
  el['btn-undo'].style.display = S.mode === 'online' ? 'none' : '';
  el['btn-restart'].style.display = guest ? 'none' : '';
}

let timer = null;

function startTimer() {
  stopTimer();
  S.startedAt = Date.now();
  S.elapsed = 0;
  timer = setInterval(() => {
    if (S.over) return;
    S.elapsed = (Date.now() - S.startedAt) / 1000;
    el['info-time'].textContent = fmtTime(S.elapsed);
  }, 250);
}

function stopTimer() {
  if (timer) clearInterval(timer);
  timer = null;
}

//: 落子后刷新禁手标记。只在轮到黑方且规则为 ban 时算 —— 白方不受禁手约束，
//: 黑方不在走棋时算出来的点也没有意义。
async function refreshForbidden() {
  if (S.rules !== 'ban' || S.over || S.turn !== 1) {
    S.forbidden = [];
    draw();
    return;
  }
  try {
    S.forbidden = await engine.bancheck(S.board);
  } catch (e) {
    S.forbidden = [];
  }
  draw();
}

/* ------------------------------------------------------------ 复盘
 *
 * 终局后把**我自己下过的每一手**放回当时的局面重算一遍 —— 与桌面版同一条
 * 流程：选强度 → 逐手重算 → 列表 → 点开某一手看那张盘。
 *
 * 只评自己的手：人机局里 AI 的着法不归我负责，联机局里对手的棋同理（桌面版
 * 的局域网复盘也只算己方）。素材来自 `S.history`，每一手都带着落子前的快照。
 *
 * 网页里没有本地 Python 引擎来算候选点，"偏离战场"改成邻接判据：不在根候选
 * 表里、八邻域又挨不着任何一颗子，就是下到战场外面去了 —— 与 C++ 的
 * `candMask`（`board.cpp` 的 `make()`）同一条规则。
 */

const REVIEW_TIP =
  '点任意一手，在棋盘上回看当时的局面：幽灵子是当时下的那一手，红圈是引擎推荐的点。';

let reviewToken = 0;       // 每开始/取消一次 +1，旧的那一轮自己退出

function fmtPoint(r, c) {
  return COL_LETTERS[c] + (r + 1);
}

//: 假 DOM（t_webapp.js）里 `innerHTML = ''` 不会真的清掉子节点，`children`
//: 却是普通数组。真浏览器里反过来。两句都写上才两边都对。
function clearNode(node) {
  node.innerHTML = '';
  if (Array.isArray(node.children)) node.children.length = 0;
}

function nearStone(board, r, c) {
  for (let dr = -1; dr <= 1; dr++) {
    for (let dc = -1; dc <= 1; dc++) {
      if (!dr && !dc) continue;
      const rr = r + dr, cc = c + dc;
      if (rr < 0 || rr >= N || cc < 0 || cc >= N) continue;
      if (board[idx(rr, cc)]) return true;
    }
  }
  return false;
}

function reviewMoves() {
  return S.history.filter((m) => m.s === S.human);
}

//: 一条记录，字段含义与桌面版 `ReviewWorker._one` 对齐：`best === null` 是
//: "没得比"（空盘起始手 / 搜索一轮都没跑完），与"比过了、没问题"是两件事。
function reviewRecord(m, st) {
  const rec = {
    seq: m.seq, played: [m.r, m.c], s: m.s, before: m.before,
    best: null, bestVal: null, delta: null, mate: false,
    offboard: false, empty: false,
  };
  if (!st) {
    rec.empty = !m.before.some((v) => v);
    return rec;
  }
  const playedIdx = idx(m.r, m.c);
  const table = new Map(st.root || []);
  rec.best = [Math.floor(st.move / N), st.move % N];
  rec.bestVal = st.val;
  //: 杀棋分（|v| > 静态上限）之间的差值是 2×10⁷ 这种没有量纲意义的数，
  //: 结果页要另给一句文字结论（见 `reviewLine`）。
  rec.mate = Math.abs(st.val) > STATIC_MAX;
  if (!table.size) {
    // 根候选表没拿到（缓冲被截断）：能给出最优手，但比不了差多少。
    return rec;
  }
  if (!table.has(playedIdx)) {
    if (winLine(m.before, m.r, m.c, m.s)) {
      // 这一手直接成五：根节点见到必胜手就提前返回，后面的候选一个都没打
      // 分。它在表外不是因为差，是因为赢定了。
      rec.delta = 0;
      rec.best = [m.r, m.c];
      return rec;
    }
    rec.offboard = !nearStone(m.before, m.r, m.c);
    return rec;
  }
  rec.delta = st.val - table.get(playedIdx);
  if (rec.delta === 0) {
    // 并列最优。`best` 只是同分者里被挑中的一个，圈在别处却写着"最优"就是
    // 在说反话 —— 把圈指回玩家下的这颗（与桌面版 `rec['best'] = played` 同）。
    rec.best = [m.r, m.c];
  }
  return rec;
}

//: 段与段之间用**全角空格**：这是一段塞进按钮的纯文本，连续半角空格会被
//: HTML 折成一个，读起来就粘成一句了。
function reviewLine(rec) {
  const GAP = '\u3000';
  let head = `第 ${rec.seq} 手${GAP}${fmtPoint(rec.played[0], rec.played[1])}`;
  if (!rec.best) {
    return head + GAP + (rec.empty ? '空盘起始手，无候选可比'
                                   : '未能比较（搜索未跑完一轮）');
  }
  if (rec.delta === 0) return head + GAP + '最优';
  head += GAP + '→ 最优点 ' + fmtPoint(rec.best[0], rec.best[1]);
  if (rec.offboard) return head + GAP + '偏离战场（不在候选点内）';
  if (rec.mate) return head + GAP + (rec.bestVal > 0 ? '错失必胜' : '漏防必败');
  if (rec.delta === null) return head + GAP + '未能比较（搜索被截断）';
  return head + GAP + '分值差 ' + Math.trunc(rec.delta);
}

function openReviewPick() {
  if (!reviewMoves().length) return;
  if (!S.reviewLevel) S.reviewLevel = S.level;
  clearNode(el['pick-levels']);
  for (const lv of LEVELS) {
    const b = document.createElement('button');
    b.className = 'chip' + (lv.id === S.reviewLevel ? ' is-on' : '');
    b.dataset.level = String(lv.id);
    b.textContent = lv.name;
    el['pick-levels'].appendChild(b);
  }
  el.overlay.classList.add('hidden');
  el['review-pick'].classList.remove('hidden');
}

function cancelReviewPick() {
  el['review-pick'].classList.add('hidden');
  el.overlay.classList.remove('hidden');
}

//: 逐手重算。**串行**：Worker 一次只跑一个搜索，并发没有意义，而串行才能让
//: 进度条老实反映位置。取消靠 `reviewToken` —— `engine.kill()` 会让正在跑的
//: 那次 think 以 "cancelled" 抛出来，这里认这个标记就收手。
async function startReview() {
  const moves = reviewMoves();
  if (!moves.length) return;
  const lv = LEVELS[(S.reviewLevel || S.level) - 1];
  const my = ++reviewToken;
  S.review = { records: [], level: lv.id };
  S.reviewView = null;
  el['review-pick'].classList.add('hidden');
  el['review-run'].classList.remove('hidden');
  el['run-bar'].style.width = '0%';
  el['run-step'].textContent = `第 0/${moves.length} 手`;

  for (let i = 0; i < moves.length; i++) {
    const m = moves[i];
    el['run-step'].textContent = `第 ${i + 1}/${moves.length} 手`;
    el['run-bar'].style.width = (i / moves.length * 100).toFixed(1) + '%';
    let st = null;
    if (m.before.some((v) => v)) {          // 空盘没有候选可比，不必搜
      try {
        st = await engine.think(m.before, m.s, {
          time: lv.time, depth: lv.depth, vcf: lv.vcf, qply: lv.qply,
          enhance: lv.enhance, bias: lv.bias || [0, 0, 0],
          reserve: lv.reserve || 0,
        }, { collect: true });
      } catch (e) {
        if (my !== reviewToken) return;     // 被取消：kill 让这次 think 抛出来
        st = null;                          // 引擎真出错：这一手按"未能比较"走
      }
    }
    if (my !== reviewToken) return;
    S.review.records.push(reviewRecord(m, st));
  }
  el['review-run'].classList.add('hidden');
  showReviewResults(false);
}

function cancelReview() {
  if (!S.review) return;
  reviewToken += 1;
  engine.kill();
  el['review-run'].classList.add('hidden');
  //: 已经算完的那部分照样给人看 —— 复盘动辄几分钟，中途取消不该把前面的
  //: 结果一起丢掉（桌面版同样只加一句"已取消"）。
  if (S.review.records.length) showReviewResults(true);
  else { S.review = null; el.overlay.classList.remove('hidden'); }
}

function showReviewResults(cancelled) {
  const recs = S.review.records;
  const level = LEVELS[S.review.level - 1];
  const bad = recs.filter((r) => r.best && r.delta !== 0).length;
  el['review-title'].textContent = '复盘结果 · ' + level.name;
  el['review-count'].textContent =
    `共 ${recs.length} 手 · ${bad} 手可改进` +
    (cancelled ? '（已取消）' : '');
  clearNode(el['review-rows']);
  recs.forEach((rec, i) => {
    const b = document.createElement('button');
    b.className = 'review-row'
      + (rec.best && rec.delta === 0 ? ' is-optimal' : '');
    b.dataset.i = String(i);
    b.textContent = reviewLine(rec);
    el['review-rows'].appendChild(b);
  });
  el['review-readout'].textContent = REVIEW_TIP;
  el.overlay.classList.add('hidden');
  el['review-box'].classList.remove('hidden');
  el.game.classList.add('reviewing');
  S.reviewView = null;
  draw();
}

//: 点一行 → 棋盘换成那一手**落子之前**的局面；再点同一行收起，回到终局。
function showReviewView(i) {
  const rec = S.review && S.review.records[i];
  if (!rec) return;
  const same = S.reviewView === rec;
  S.reviewView = same ? null : rec;
  for (const b of el['review-rows'].children) {
    b.classList.toggle('is-on', !same && b.dataset.i === String(i));
  }
  el['review-readout'].textContent = same ? REVIEW_TIP : reviewLine(rec);
  draw();
}

function exitReview() {
  reviewToken += 1;
  engine.kill();
  S.review = null;
  S.reviewView = null;
  el.game.classList.remove('reviewing');
  el['review-box'].classList.add('hidden');
  draw();
  el.overlay.classList.remove('hidden');
}

/* ------------------------------------------------------------ 联机
 *
 * B 站 Toy 没有实时通道（SDK 里只有按玩家隔离的云存档），所以不额外部署，
 * 直接用公共消息中转 ntfy：CORS 全开、不用注册，页面里 SSE 收、fetch 发。
 *
 * 权威在房主：房主落子直接改本地局面再广播整盘（`state`）；加入方落子只是
 * 发一个 `move` 意向，房主校验后代为落地并广播。这样两边不会各自漂移。
 *
 * 房间码就是钥匙 —— 知道这 6 位的人都能进；先到的那位被房主同意后执白，
 * 其余只能观战。中转是公共 topic，消息在公共缓存里留约 12 小时，别拿它传
 * 隐私内容。
 */

const RELAY = 'https://ntfy.sh';
const ROOM_ALPHABET = 'abcdefghijkmnpqrstuvwxyz23456789';

const NET = {
  active: false,
  role: '',       // host | join
  id: '',
  code: '',
  topic: '',
  es: null,
  rev: -1,        // 已应用的权威局面序号；旧的 `state` 一律丢掉
  seat: 0,        // 我这一席的颜色，0 = 还没上桌（观战）
  refused: false, // 房主已经明确让我只观战
  name: '',
  guest: null,    // 房主：拿到座位的加入方 id
  pending: null,  // 房主：正在请求上桌的 {id, name}
  beat: null,     // 房主的心跳定时器
};

function netRandom(n) {
  const a = new Uint8Array(n);
  if (self.crypto && self.crypto.getRandomValues) self.crypto.getRandomValues(a);
  else for (let i = 0; i < n; i++) a[i] = Math.floor(Math.random() * 256);
  let s = '';
  for (let i = 0; i < n; i++) s += ROOM_ALPHABET[a[i] % ROOM_ALPHABET.length];
  return s;
}

//: 状态行在**两处**同时写：选择页那一行（创建/加入时看得见），以及对局面板
//: 里那一行（进了房间之后选择页就藏起来了，中转断线只有这里说得清）。
function netStatus(text) {
  el['online-status'].textContent = text;
  el['online-note'].textContent = text;
  el['online-note'].classList.toggle('hidden', !text);
}

function netPublish(msg) {
  if (!NET.active || typeof self.fetch !== 'function') return;
  const body = JSON.stringify(Object.assign({ from: NET.id }, msg));
  return self.fetch(RELAY + '/' + NET.topic, {
    method: 'POST',
    headers: { 'Content-Type': 'text/plain' },
    body,
  }).catch(() => { netStatus('联机服务连不上，正在重试…'); });
}

//: 我的名字：没有登录也没有昵称，用 id 的前四位拼一个短标签，房主那边
//: 「XX 想上桌」总得有个称呼。
function netName(id) {
  return '玩家' + String(id).slice(0, 4).toUpperCase();
}

function netOpen(role, code) {
  netClose();
  NET.active = true;
  NET.role = role;
  NET.code = code.toLowerCase();
  NET.topic = 'gomoku-' + NET.code;
  NET.id = netRandom(10);
  NET.name = netName(NET.id);
  NET.rev = 0;            // 广播序号：房主每发一次 +1，收到旧序号的直接丢
  NET.seat = role === 'host' ? 1 : 0;
  NET.refused = false;
  S.human = NET.seat;
  el['info-room-row'].style.display = '';
  el['info-room'].textContent = NET.code.toUpperCase();
  el.game.classList.add('online');

  if (typeof self.EventSource !== 'function') {
    netStatus('这个浏览器没有 EventSource，联机用不了；人机对战不受影响。');
    return;
  }
  const es = new self.EventSource(RELAY + '/' + NET.topic + '/sse');
  NET.es = es;
  es.onopen = () => {
    netStatus(NET.role === 'host'
      ? '房间已开：把房间码发给朋友' : '已连上房间，等房主同意');
    netPublish({ t: 'hello' });
    //: SSE 断线重连后会再走一次 onopen —— 借这个时机把当前局面要回来。
    if (NET.role !== 'host') netPublish({ t: 'sync' });
  };
  es.onmessage = (ev) => {
    let frame;
    try { frame = JSON.parse(ev.data); } catch (e) { return; }
    //: 外层是 ntfy 的帧（event / message），正文才是我自己那份 JSON。
    if (!frame || frame.event !== 'message' || !frame.message) return;
    let msg;
    try { msg = JSON.parse(frame.message); } catch (e) { return; }
    netHandle(msg);
  };
  es.onerror = () => netStatus('联机服务连不上，正在重试…');

  if (role === 'host') {
    //: 心跳：后来的人发 hello 收得到回复，但 hello 丢一次就卡住了；每 15 秒
    //: 重播一遍权威局面，顺带把 topic 的缓存续上。
    NET.beat = setInterval(() => { if (NET.active) netSendState(); }, 15000);
  }
}

function netClose() {
  if (NET.es) { try { NET.es.close(); } catch (e) { /* 已经断了 */ } }
  if (NET.beat) clearInterval(NET.beat);
  const was = NET.active;
  const topic = NET.topic;
  NET.active = false;
  NET.es = null;
  NET.beat = null;
  NET.role = '';
  NET.seat = 0;
  NET.guest = null;
  NET.pending = null;
  NET.refused = false;
  NET.rev = 0;
  if (was && typeof self.fetch === 'function') {
    //: 告别消息：房主据此把人从名单里挪走。发不出去也无所谓。
    self.fetch(RELAY + '/' + topic, {
      method: 'POST', headers: { 'Content-Type': 'text/plain' },
      body: JSON.stringify({ from: NET.id, t: 'bye' }),
    }).catch(() => {});
  }
}

//: 房主广播整盘局面。`mv` 只发落点下标，颜色由先后手推出来，比一份
//: {r,c,s} 的数组省一半字节；361 子全满时两者加起来约 3 KB，还在 ntfy 的
//: 上限之内。
function netSendState() {
  if (!NET.active || NET.role !== 'host') return;
  NET.rev += 1;
  netPublish({
    t: 'state', rev: NET.rev,
    board: Array.from(S.board),
    mv: S.moves.map((m) => idx(m.r, m.c)),
    turn: S.turn, over: S.over, winner: S.winner,
    line: S.winLine || null, seat: NET.seat, rules: S.rules,
  });
}

//: 从着法序列重建复盘快照。加入方拿到的只有局面与落点，`before` 得自己
//: 攒 —— 复盘素材在两边必须同构，否则"能不能复盘"会因角色而不同。
function rebuildHistory(moves) {
  const board = new Int8Array(CELLS);
  return moves.map((m, i) => {
    const before = board.slice(0);
    board[idx(m.r, m.c)] = m.s;
    return { seq: i + 1, r: m.r, c: m.c, s: m.s, before };
  });
}

function netApplyState(msg) {
  if (typeof msg.rev === 'number' && msg.rev <= NET.rev) return;
  if (typeof msg.rev === 'number') NET.rev = msg.rev;
  if (msg.rules && msg.rules !== S.rules) S.rules = msg.rules;
  S.board = Int8Array.from(msg.board || []);
  S.moves = (msg.mv || []).map((i, k) => ({
    r: Math.floor(i / N), c: i % N, s: k % 2 ? 2 : 1,
  }));
  S.history = rebuildHistory(S.moves);
  S.turn = msg.turn || 1;
  S.winLine = msg.line || null;
  S.awaiting = false;
  S.thinking = false;
  S.forbidden = [];
  if (msg.over) { finish(msg.winner || 0, msg.line || null, null); return; }
  S.over = false;
  S.winner = 0;
  //: 房主开了新局：上一局的终局遮罩还挂在屏幕上，得收掉。
  hideOverlay();
  updateInfo();
  renderCharts();
  draw();
  netTurnCard();
}

function netTurnCard() {
  if (S.over) return;
  if (!NET.seat) {
    setTurnCard(NET.role === 'join' && !NET.refused ? '等待房主同意…' : '观战中', '');
    setStoneIcon(el['turn-stone'], S.turn);
    return;
  }
  if (S.turn === NET.seat) setTurnCard('轮到你落子', '');
  else setTurnCard(NET.seat === 1 ? '等待对手落子…' : '等房主落子…', 'think');
  setStoneIcon(el['turn-stone'], S.turn);
}

function netHostMove(r, c) {
  if (S.over || S.turn !== 2 || S.board[idx(r, c)] !== 0) {
    //: 拒绝也要回一句话：对面那颗"等待房主确认…"只有收到新 state 才松得开，
    //: 不然它会一直卡在那儿，棋盘整个僵住。
    netSendState();
    return;
  }
  place(r, c, 2);
  const line = winLine(S.board, r, c, 2);
  //: `finish` 里会广播终局局面（房主那一支），这里不用再来一次。
  if (line) return finish(2, line, null);
  if (S.moves.length === CELLS) return finish(0, null, null);
  netSendState();
  netTurnCard();
}

function netHandle(msg) {
  if (!msg || msg.from === NET.id) return;

  if (msg.t === 'hello') {
    if (NET.role === 'host') {
      if (!NET.guest && !NET.pending) {
        NET.pending = { id: msg.from, name: msg.name || netName(msg.from) };
        showOverlay(`${NET.pending.name} 想上桌`,
          `${NET.pending.name} 申请执白与你对局。同意就请他上桌，`
          + `不同意他会以观战身份看棋。`,
          { again: '同意上桌', back: '只让观战' }, 'guest-request');
      } else {
        //: 已经有人坐下了：明确告诉他只能观战，再把局面给他 —— 不吭声的话
        //: 他那边会一直停在「等待房主同意…」，像是卡住了。
        if (msg.seat !== 1) netPublish({ t: 'deny', to: msg.from });
        netSendState();
      }
    } else {
      netPublish({ t: 'sync' });
    }
    return;
  }

  if (msg.t === 'sync') {
    if (NET.role === 'host') netSendState();
    return;
  }

  if (msg.t === 'admit' && msg.to === NET.id) {
    NET.seat = msg.seat;
    S.human = msg.seat;
    netStatus('房主同意了，你执' + (msg.seat === 1 ? '黑' : '白'));
    netTurnCard();
    return;
  }

  if (msg.t === 'deny' && msg.to === NET.id) {
    if (NET.seat) return;          // 已经坐下了，迟到的拒绝不把人赶下去
    NET.seat = 0;
    NET.refused = true;
    S.human = 0;
    netStatus('房主没让你上桌，这局你在观战');
    netTurnCard();
    return;
  }

  if (msg.t === 'state') { netApplyState(msg); return; }

  if (msg.t === 'move' && NET.role === 'host' && msg.from === NET.guest) {
    netHostMove(msg.r, msg.c);
    return;
  }

  if (msg.t === 'bye' && msg.from === NET.guest) {
    NET.guest = null;
    netStatus('对手离开了房间');
  }
}

function enterOnline(role, code) {
  S.mode = 'online';
  netOpen(role, code);
  applyModeGroups();
  el.setup.classList.add('hidden');
  el.game.classList.remove('hidden');
  //: 与 `#start` 同理：板子刚从 display:none 出来，等一帧才有真实宽度。
  requestAnimationFrame(() => newGame());
}

/* ------------------------------------------------------------ 对局流程 */

function newGame() {
  engine.kill();
  stopTimer();
  S.board = new Int8Array(CELLS);
  S.moves = [];
  S.turn = 1;
  S.over = false;
  S.winner = 0;
  S.winLine = null;
  S.forbidden = [];
  S.thinking = false;
  S.pendingFoul = -1;
  S.undos = 0;
  S.series = [];
  S.lastReadout = '';
  S.hover = -1;
  S.history = [];
  S.awaiting = false;
  S.review = null;
  S.reviewView = null;
  el.game.classList.toggle('online', S.mode === 'online');
  hideOverlay();
  startTimer();
  el['score-readout'].textContent = '';
  updateInfo();
  renderCharts();
  draw();
  if (S.mode === 'online') {
    //: 联机开局：房主执黑先下，加入方执白等着；观战两边都不轮到。
    netTurnCard();
    return;
  }
  setTurnCard('轮到你落子', '');
  setStoneIcon(el['turn-stone'], S.human);
  if (S.human === 2) aiTurn();
  else refreshForbidden();
}

function place(r, c, stone) {
  //: 复盘素材在落子**之前**抓：`before` 是那一手开局时的真实局面，结果页
  //: 展示的就是它（与桌面版 `human_moves` 的 before 同一个口径）。副本不能省
  //: —— `S.board` 是同一块内存，不拷下来每一手的快照都会变成终局。
  S.history.push({ seq: S.moves.length + 1, r, c, s: stone,
                   before: S.board.slice(0) });
  S.board[idx(r, c)] = stone;
  S.moves.push({ r, c, s: stone });
  S.turn = 3 - stone;
  updateInfo();
  draw();
}

function isFoul(r, c) {
  return S.forbidden.some(([fr, fc]) => fr === r && fc === c);
}

function onBoardClick(ev) {
  if (S.over || S.thinking || S.reviewView) return;
  if (S.mode === 'online') {
    //: 观战（没座位）、还没轮到我、或者上一手还等房主确认 —— 一律不收。
    if (S.awaiting || !NET.seat || S.turn !== NET.seat) return;
  } else if (S.turn !== S.human) {
    return;
  }
  const rect = canvas.getBoundingClientRect();
  const w = canvas.clientWidth;
  const x = (ev.clientX - rect.left) * (w / rect.width);
  const y = (ev.clientY - rect.top) * (w / rect.height);
  const hit = toGrid(geom(w), x, y);
  if (!hit) return;
  if (S.board[idx(hit.r, hit.c)] !== 0) return;

  if (S.mode === 'online') { onlinePlace(hit.r, hit.c); return; }

  if (S.rules === 'ban' && S.human === 1 && isFoul(hit.r, hit.c)) {
    S.pendingFoul = idx(hit.r, hit.c);
    showOverlay('这里是禁手点',
      '三三、四四或长连都是禁手。落下去这一手就判负，确定要下吗？',
      { again: '仍然落子', back: '换一手' }, 'foul');
    return;
  }
  humanPlace(hit.r, hit.c);
}

//: 联机落子：房主就地落子（他就是权威），加入方只发一个意向等房主确认。
//: 不乐观地先把子摆上去 —— 摆错了要再撤回，而撤回的那一瞬比等半秒更难看。
function onlinePlace(r, c) {
  if (NET.seat === 1) {
    place(r, c, 1);
    const line = winLine(S.board, r, c, 1);
    if (line) return finish(1, line, null);
    if (S.moves.length === CELLS) return finish(0, null, null);
    S.forbidden = [];
    draw();
    netSendState();
    netTurnCard();
    return;
  }
  S.awaiting = true;
  netPublish({ t: 'move', r, c });
  setTurnCard('等待房主确认…', 'think');
}

function humanPlace(r, c) {
  place(r, c, S.human);
  const line = winLine(S.board, r, c, S.human);
  if (line) return finish(S.human, line, null);
  if (S.moves.length === CELLS) return finish(0, null, null);
  S.forbidden = [];
  draw();
  setTurnCard('AI 思考中…', 'think');
  aiTurn();
}

async function aiTurn() {
  const lv = LEVELS[S.level - 1];
  const me = 3 - S.human;
  S.thinking = true;
  setTurnCard('AI 思考中…', 'think');
  setStoneIcon(el['turn-stone'], me);

  //: 空盘（人类执白）时不下搜索，直接走天元 —— 与桌面版的开局同一条。
  //: 之后先查开局库，命中就照库里那手落子（`engine.ai_move` 的第二道闸门）。
  let move = -1;
  let st = null;
  const hit = S.moves.length === 0 ? null : bookLookup(S.board, me);
  if (S.moves.length === 0) {
    move = idx(CENTER, CENTER);
  } else if (hit) {
    move = hit[0];
    //: 形状照 gm_think 的输出补齐，图表与读数两条消费路径不用分叉；
    //: nodes / nps / ms 都是 0 —— 查表本来就没有搜索，桌面版也这么记。
    st = { move: hit[0], depth: hit[2], actual: hit[2], val: hit[1],
           ms: 0, nodes: 0, nps: 0, scoreType: 'book' };
  } else {
    try {
      st = await engine.think(S.board, me, {
        time: lv.time, depth: lv.depth, vcf: lv.vcf, qply: lv.qply,
        enhance: lv.enhance, bias: lv.bias || [0, 0, 0], reserve: lv.reserve || 0,
      });
      move = st.move;
    } catch (e) {
      if (String(e.message || e).includes('cancelled')) return;
      S.thinking = false;
      setTurnCard('引擎出错：' + (e.message || e), 'lose');
      return;
    }
  }

  //: 思考期间用户可能已经重开 / 悔棋 —— 那种情况下这一步必须丢掉
  if (!S.thinking) return;
  S.thinking = false;
  if (move < 0) return finish(S.human, null, null);

  const r = Math.floor(move / N), c = move % N;
  if (S.board[move] !== 0) return finish(0, null, null);
  place(r, c, me);

  if (st) {
    S.series.push(st);
    //: 库里的那手没有搜索可言，写成 "d9 0 0/s 0.0s" 会被当成坏掉的读数。
    S.lastReadout = st.scoreType === 'book'
      ? `开局库 d${st.depth}`
      : `d${st.depth} ${fmtNodes(st.nodes)} ${fmtNps(st.nps)} ` +
        `${(st.ms / 1000).toFixed(1)}s`;
    el['score-readout'].textContent = S.lastReadout;
    renderCharts();
  }
  const line = winLine(S.board, r, c, me);
  if (line) return finish(me, line, null);
  if (S.moves.length === CELLS) return finish(0, null, null);
  setTurnCard('轮到你落子', '');
  setStoneIcon(el['turn-stone'], S.human);
  refreshForbidden();
}

function fmtNodes(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function fmtNps(n) {
  if (n >= 1e6) return (n / 1e6).toFixed(2) + 'M/s';
  if (n >= 1e3) return (n / 1e3).toFixed(0) + 'k/s';
  return n + '/s';
}

function finish(winner, line, note) {
  S.over = true;
  S.winner = winner;
  S.winLine = line;
  S.thinking = false;
  S.forbidden = [];
  stopTimer();
  draw();
  updateInfo();

  const online = S.mode === 'online';
  const who = winner === 0 ? '平局'
    : !S.human ? (winner === 1 ? '黑方胜' : '白方胜')
    : winner === S.human ? '你赢了'
    : (online ? '对手获胜' : 'AI 获胜');
  setTurnCard(who, winner === 0 ? '' : (winner === S.human ? 'win' : 'lose'));
  if (winner) setStoneIcon(el['turn-stone'], winner);

  let text = note || '';
  text += winner === 0 ? '棋盘已满，没有胜负。' : `共 ${S.moves.length} 手。`;
  showOverlay(who, text, { again: '再来一局', back: '改设置' }, 'result');
  //: 「复盘」只在**我自己下过棋**时给：观战者没有可复盘的手，双人局里
  //: 联机对手的手也不归我复盘（人机局同理，AI 的着法不在素材里）。
  el['btn-review'].style.display =
    S.history.some((m) => m.s === S.human) ? '' : 'none';
  //: 联机开新局只有房主说了算（两边各点各的会各开各的）。加入方那颗按钮
  //: 干脆收起来，留「改设置」退席。
  el['btn-again'].style.display =
    online && NET.role !== 'host' ? 'none' : '';
  if (online && NET.role === 'host') netSendState();
}

//: 明知是禁手还要落，就按 RIF 的规矩判负。这一手**真的摆上去** —— 判负得让人
//: 看见是哪一手输的。
function foulLose(at) {
  const r = Math.floor(at / N), c = at % N;
  place(r, c, S.human);
  finish(3 - S.human, null, '这一手是禁手（三三 / 四四 / 长连），黑方判负。');
}

//: 悔棋一次撤一个来回：先甩掉 AI 的应手，再撤掉自己那一手。搜索树和置换表
//: 跟着 Worker 一起被掐掉 —— 反正下一手会重建。
function undo() {
  //: 联机禁悔棋：对面那张盘已经落了子，单方面撤回只会把两边扯开。
  if (S.mode === 'online') return;
  if (!S.moves.length) return;
  engine.kill();
  S.thinking = false;
  hideOverlay();
  S.over = false;
  S.winner = 0;
  S.winLine = null;

  const who = S.human;
  while (S.moves.length) {
    const m = S.moves.pop();
    S.board[idx(m.r, m.c)] = 0;
    if (m.s === who) break;
  }
  S.turn = S.moves.length ? 3 - S.moves[S.moves.length - 1].s : 1;
  S.undos += 1;
  S.series.pop();               // 撤掉那一手，它的评分点也要撤
  //: 复盘素材跟着剪：撤掉的那几手不能在复盘里又冒出来。
  S.history = S.history.filter((h) => h.seq <= S.moves.length);
  S.forbidden = [];
  S.lastReadout = '';
  el['score-readout'].textContent = '';
  updateInfo();
  renderCharts();
  draw();

  if (S.turn !== S.human) { aiTurn(); return; }
  setTurnCard('轮到你落子', '');
  setStoneIcon(el['turn-stone'], S.human);
  refreshForbidden();
}

/* ------------------------------------------------------------ 覆盖层 */

let overlayMode = 'result';

function showOverlay(title, text, labels, mode) {
  overlayMode = mode || 'result';
  el['overlay-title'].textContent = title;
  el['overlay-text'].textContent = text;
  el['btn-again'].textContent = labels.again;
  el['btn-overlay-back'].textContent = labels.back;
  el.overlay.classList.remove('hidden');
}

function hideOverlay() {
  el.overlay.classList.add('hidden');
  S.pendingFoul = -1;
  overlayMode = 'result';
}

/* ------------------------------------------------------------ 事件 */

function onCardGroup(group, attr, handler) {
  group.addEventListener('click', (ev) => {
    const b = ev.target.closest('.card');
    if (!b) return;
    handler(b.dataset[attr]);
  });
}

//: 联机时"执棋颜色"与"AI 难度"都没有意义：房主执黑、对手执白。整块收起来
//: 比留着让人点了没反应干净。规则同理 —— 联机局一律无禁手（黑方就是房主，
//: 判禁手的时机在两张盘上对不齐）。
function applyModeGroups() {
  const online = S.mode === 'online';
  el['group-online'].style.display = online ? '' : 'none';
  el['group-color'].style.display = online ? 'none' : '';
  el['group-level'].style.display = online ? 'none' : '';
  el['group-rules'].style.display = online ? 'none' : '';
  el.start.style.display = online ? 'none' : '';
  el['setup-hint'].textContent = online
    ? '创建房间后把房间码（或邀请链接）发给朋友；他执白，其余人以观战身份看棋。'
    : '黑先白后，先连成五子者胜';
}

onCardGroup(el['choose-mode'], 'mode', (v) => {
  S.mode = v;
  markSelected(el['choose-mode'], 'mode', S.mode);
  applyModeGroups();
});

el['btn-create-room'].addEventListener('click', () => {
  //: 房主执黑先手；房间码是唯一钥匙，主题名由它推出来。
  enterOnline('host', netRandom(6));
});

el['btn-join-room'].addEventListener('click', () => {
  const code = String(el['room-code'].value || '').trim().toLowerCase();
  if (!/^[a-z0-9]{4,8}$/.test(code)) {
    netStatus('房间码是 6 位字母数字，请检查后重输');
    return;
  }
  enterOnline('join', code);
});

onCardGroup(el['choose-color'], 'color', (v) => {
  S.human = Number(v);
  markSelected(el['choose-color'], 'color', S.human);
});

onCardGroup(el['choose-level'], 'level', (v) => {
  S.level = Number(v);
  markSelected(el['choose-level'], 'level', S.level);
});

onCardGroup(el['choose-rules'], 'rules', (v) => {
  S.rules = v;
  markSelected(el['choose-rules'], 'rules', S.rules);
});

el.start.addEventListener('click', () => {
  el.setup.classList.add('hidden');
  el.game.classList.remove('hidden');
  //: 等一帧再开：板子刚从 display:none 出来，clientWidth 还是 0，
  //: 这一帧不等的话第一张盘会按 600px 的兜底值画，然后被 resize 覆盖。
  requestAnimationFrame(() => newGame());
});

function toggleTheme() {
  const dark = document.documentElement.classList.toggle('dark');
  el['theme-icon'].textContent = dark ? '☾' : '☀';
  try {
    localStorage.setItem('gomoku-theme', dark ? 'dark' : 'light');
  } catch (e) { /* 隐私模式里写不进去，不影响玩 */ }
  renderCharts();
}

el['btn-theme-row'].addEventListener('click', toggleTheme);

function backToSetup() {
  engine.kill();
  stopTimer();
  S.thinking = false;
  netClose();
  S.mode = 'ai';
  markSelected(el['choose-mode'], 'mode', S.mode);
  applyModeGroups();
  el.game.classList.remove('online', 'reviewing');
  el['info-room-row'].style.display = 'none';
  el['review-pick'].classList.add('hidden');
  el['review-run'].classList.add('hidden');
  el['review-box'].classList.add('hidden');
  el.game.classList.add('hidden');
  el.setup.classList.remove('hidden');
}

el['btn-back'].addEventListener('click', backToSetup);

el['btn-undo'].addEventListener('click', undo);

el['btn-restart'].addEventListener('click', () => {
  hideOverlay();
  newGame();
  if (S.mode === 'online') netSendState();
});

el['btn-again'].addEventListener('click', () => {
  if (overlayMode === 'foul') {
    const at = S.pendingFoul;
    hideOverlay();
    foulLose(at);
    return;
  }
  //: 「X 想上桌」那张遮罩的第二颗按钮复用的是这套——同一张卡片，两种问法。
  if (overlayMode === 'guest-request') {
    const p = NET.pending;
    hideOverlay();
    if (p) {
      NET.pending = null;
      NET.guest = p.id;
      netPublish({ t: 'admit', to: p.id, seat: 2 });
      netSendState();
      netStatus(p.name + ' 已上桌（执白）');
    }
    return;
  }
  hideOverlay();
  newGame();
  if (S.mode === 'online') netSendState();
});

el['btn-overlay-back'].addEventListener('click', () => {
  if (overlayMode === 'guest-request') {
    const p = NET.pending;
    NET.pending = null;
    hideOverlay();
    if (p) netPublish({ t: 'deny', to: p.id });
    return;
  }
  if (overlayMode === 'foul') { hideOverlay(); return; }
  hideOverlay();
  backToSetup();
});

/* ---- 复盘：选强度 → 开始 → 点某一行回看 ---- */

el['pick-levels'].addEventListener('click', (ev) => {
  const b = ev.target.closest('.chip');
  if (!b) return;
  S.reviewLevel = Number(b.dataset.level);
  for (const c of el['pick-levels'].children) {
    c.classList.toggle('is-on', c === b);
  }
});

el['btn-review'].addEventListener('click', openReviewPick);
el['btn-review-close'].addEventListener('click', cancelReviewPick);
el['btn-review-start'].addEventListener('click', startReview);
el['btn-run-cancel'].addEventListener('click', cancelReview);
el['btn-review-exit'].addEventListener('click', exitReview);
el['review-rows'].addEventListener('click', (ev) => {
  const b = ev.target.closest('.review-row');
  if (b) showReviewView(Number(b.dataset.i));
});

canvas.addEventListener('click', onBoardClick);

canvas.addEventListener('mousemove', (ev) => {
  if (S.over || S.thinking) return;
  const rect = canvas.getBoundingClientRect();
  const w = canvas.clientWidth;
  const hit = toGrid(geom(w),
                     (ev.clientX - rect.left) * (w / rect.width),
                     (ev.clientY - rect.top) * (w / rect.height));
  const next = hit ? idx(hit.r, hit.c) : -1;
  if (next !== S.hover) { S.hover = next; draw(); }
});

canvas.addEventListener('mouseleave', () => {
  if (S.hover !== -1) { S.hover = -1; draw(); }
});

let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    if (!el.game.classList.contains('hidden')) { draw(); renderCharts(); }
  }, 80);
});

/* ------------------------------------------------------------ 启动 */

try {
  if (localStorage.getItem('gomoku-theme') === 'dark') {
    document.documentElement.classList.add('dark');
  }
} catch (e) { /* 同上 */ }
el['theme-icon'].textContent =
  document.documentElement.classList.contains('dark') ? '☾' : '☀';

renderLevelCards();
markSelected(el['choose-color'], 'color', S.human);
markSelected(el['choose-rules'], 'rules', S.rules);
markSelected(el['choose-mode'], 'mode', S.mode);
applyModeGroups();
el['info-time'].textContent = '00:00';

//: 邀请链接（`?room=K3M8QZ`）直接进房间 —— 这是把房间发给朋友最快的一条
//: 路，打开就已经在申请上桌了，不用再输一遍房间码。
function queryParam(name) {
  const q = (window.location && window.location.search) || '';
  for (const part of String(q).replace(/^\?/, '').split('&')) {
    const eq = part.indexOf('=');
    if (eq < 0) continue;
    if (decodeURIComponent(part.slice(0, eq)) !== name) continue;
    try { return decodeURIComponent(part.slice(eq + 1)); } catch (e) { return ''; }
  }
  return '';
}

const invite = queryParam('room').trim().toLowerCase();
if (/^[a-z0-9]{4,8}$/.test(invite)) enterOnline('join', invite);
