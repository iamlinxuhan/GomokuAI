/* 承载 wasm 引擎的 Worker —— 同时也是"没有 Worker 时"的退路。
 *
 * 同一份文件在两种环境里跑：
 *
 *   - 在 Worker 里：`importScripts` 存在，装好 `self.onmessage` 自己收发消息；
 *   - 在主线程里：`importScripts` 不存在（页面脚本），于是改用 <script> 标签把
 *     wasm 模块加载进 window 全局，只把 `GomokuCore` 挂上，由 app.js 直接调用。
 *
 * 为什么值得留这条退路：用 file:// 直接打开 index.html 时，Chrome 会在构造
 * Worker 的那一刻抛 SecurityError，页面就只剩一张死棋盘。主线程跑会卡住界面
 * （3 秒档就是 3 秒），但那是"能用但难受"，比"打不开"好。
 *
 * 规则从 URL 的 # 片段里读。禁手是编译期分支（`#if GOMOKU_BAN`），所以网页版
 * 和桌面版一样有两份引擎。片段不会发给服务器（浏览器从不发送 fragment），
 * 因此在任何静态托管上都不会变成一次找不到文件的请求；`?rules=ban` 会真的
 * 发出去，某些 CDN 对带查询串的静态资源处理得没那么宽容。
 */
'use strict';

const IS_WORKER = typeof importScripts === 'function';
//: 只有 Worker 才从自己的 URL 片段里读规则；主线程模式下规则由调用方传进来
//: （页面的 location.hash 与这个文件无关）。
const RULES = IS_WORKER ? (self.location.hash || '#free').slice(1) : null;

const boots = {};       // rules -> Promise<Module>，同一份模块只加载一次

function moduleFile(rules) {
  return rules === 'ban' ? './gomoku-ban.js' : './gomoku-free.js';
}

function boot(rules) {
  if (boots[rules]) return boots[rules];
  const file = moduleFile(rules);

  boots[rules] = new Promise((resolve, reject) => {
    const finish = (api) => {
      api._gm_init();          // 建表：lineMask / touchMask 全在这里生成
      resolve(api);
    };
    if (IS_WORKER) {
      try {
        importScripts(file);
      } catch (e) {
        reject(e);
        return;
      }
      //: `createGomoku` 是 `-sMODULARIZE=1 -sEXPORT_NAME=createGomoku` 产生的
      //: 全局工厂函数，返回一个 Promise<Module>。
      createGomoku().then(finish, reject);
      return;
    }
    const tag = document.createElement('script');
    tag.src = file;
    tag.onload = () => createGomoku().then(finish, reject);
    tag.onerror = () => reject(new Error('加载失败：' + file));
    document.head.appendChild(tag);
  });
  return boots[rules];
}

function copyBoard(api, cells) {
  //: 主线程传的是 Int8Array(361)。引擎要的是 uint8_t*，逐值相同，按字节拷。
  //:
  //: 缓冲**挂在模块自己身上**而不是这个文件的模块级变量：主线程退路里两份
  //: wasm（free / ban）会先后加载进同一个上下文，各自有独立的堆。共用一个
  //: 指针等于往另一个模块的地址空间里瞎写 —— 症状是禁手点悄悄变成空数组。
  if (!api.__cellsPtr) api.__cellsPtr = api._malloc(361);
  api.HEAPU8.set(cells, api.__cellsPtr);
  return api.__cellsPtr;
}

//: 一条消息进、一个结果出。Worker 与主线程走的是同一段代码 —— 两边行为
//: 要是不一致，那就是最难查的一类 bug。
function run(api, msg) {
  switch (msg.cmd) {
    case 'think': {
      const lv = msg.level;
      const ptr = api._gm_think(
        copyBoard(api, msg.cells), msg.me,
        lv.time, lv.depth, lv.vcf, lv.qply,
        lv.enhance ? 1 : 0,
        lv.bias[0], lv.bias[1], lv.bias[2],
        lv.reserve || 0.0,
        msg.collect ? 1 : 0,  // collect=1：连根候选表一起回传（复盘页用）
      );
      return JSON.parse(api.UTF8ToString(ptr));
    }
    case 'bancheck':
      return JSON.parse(api.UTF8ToString(api._gm_bancheck(copyBoard(api, msg.cells), 1)));
    case 'newgame':
      api._gm_new_game();
      return { ok: true };
    default:
      throw new Error('unknown command: ' + msg.cmd);
  }
}

if (IS_WORKER) {
  self.onmessage = async (ev) => {
    const msg = ev.data;
    try {
      const api = await boot(RULES);
      self.postMessage({ id: msg.id, result: run(api, msg) });
    } catch (e) {
      self.postMessage({ id: msg.id, error: String((e && e.message) || e) });
    }
  };
}

//: 主线程退路用的出口。在 Worker 里挂上去无害；在主线程里，app.js 会 await
//: 加载完这个文件之后从这里取。
self.GomokuCore = { boot: boot, run: run };
