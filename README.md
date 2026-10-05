# 🎮 Gomoku AI

[简体中文](README.zh-CN.md) | **English**

> **macOS users: this repository ships no macOS build** (it has never been
> developed or verified on macOS). For macOS, go to
> **[GomokuAI-Py](https://github.com/iamlinxuhan/GomokuAI-Py)** — that is the
> older pure-Python engine, which needs no platform-specific binary and
> therefore runs on macOS.

A human-vs-AI Gomoku (five-in-a-row) program built on **PyQt5**. The engine is
**bitboard + fully incremental evaluation + Negamax/PVS + transposition table +
quiescence search + VCF (continuous forced fours)**, running on CPU only. Five
difficulty levels; a cool-toned design system around a warm wooden board, with a
side panel that plots the AI's score and the estimated human win rate live.

The search core was rewritten in **C++17** (same algorithm, aligned line by
line) and exposed to the UI over a local TCP socket. Measured nps went from
around 48k to **0.6–3.3M (roughly 20–30×)**. The Python implementation is kept
in full as `engine_local.py` — both a fallback when C++ is unavailable and the
reference for A/B comparisons.

![Python](https://img.shields.io/badge/Python-3.11%20%7C%203.13-blue)
![PyQt5](https://img.shields.io/badge/PyQt5-5.x-green)
![NumPy](https://img.shields.io/badge/NumPy-✓-orange)
![Version](https://img.shields.io/badge/version-3.0.8-brightgreen)
![License](https://img.shields.io/badge/license-GPL--3.0--or--later-blue)

---

## ✨ Features

### 🤖 AI algorithms

| Technique | Notes |
|------|------|
| **Bitboard** | 361-bit integer bitmap + fully incremental state (candidate set / neighbour counts / hash / threat aggregation). `make`/`unmake` are strictly symmetric and support out-of-order undo |
| **Pattern classification** | Defined by **set semantics**: `F(S) = {empty e : playing at e makes five}`, so `\|F\|≥2` is an open four and `\|F\|=1` is a four. It counts the **number of winning points**, not intermediate notions like "how many open fours" that would first need explaining |
| **Zero-sum evaluation** | `evaluate(pos, black) == -evaluate(pos, white)`, maintained incrementally, with no asymmetric coefficients |
| **Negamax + PVS** | Principal Variation Search, fast pruning on zero-window searches |
| **Transposition table** | Fixed-size array with age-based replacement; mate scores are stored normalised (store `value+ply`, read back `value-ply`). Levels 4/5 use a **2-way bucket** (48 MiB resident) |
| **Quiescence search** | At leaf nodes, only forced moves are searched, removing misjudgements caused by the evaluation being cut off at the horizon |
| **VCF (continuous forced fours)** | Searches specifically for forced sequences where "every move is a four"; **three-state return** (win / no win / could not finish), with AND semantics on the defending side. **Enabled on all five levels** — the lower levels are separated by their depth cap, not by making them blind |
| **Move ordering** | History heuristic + killer moves + winning-point priority |
| **Time control** | Hard time limit + iterative deepening + aspiration windows + cooperative cancellation (restarting mid-think interrupts immediately) |
| **Opening book** | The strongest configuration's best moves for the first four opening moves, precomputed offline into a table (41 entries, covering positions with ≤3 stones on the board). A hit returns with zero delay; anything off-table falls back to search. The empty board still plays tengen, bypassing the table |
| **Enhanced search** | LMR + forced-move extension + 2-way bucket TT + iterative budget estimation, **enabled only for 高级 (Advanced) and 宗师 (Grandmaster)** |

### 🎨 UI design

- **One design system**: colours, font sizes, spacing and corner radii each have
  a single source of truth (`theme.py`), and the whole app shares one QSS. No
  hard-coded colour literals appear in the UI (guarded by
  `tests/test_no_literal_colors.py`)
- **Cool chrome, warm board**: the interface chrome is cool cyan while the board
  stays warm wood — the board is the only warm focal point in the app. The wood
  grain is generated procedurally; stones are sprites with a specular highlight
  and a contact shadow
- **Every button has all four states**: hover / pressed / focus / **disabled**
- **Side panel**: live turn / difficulty / status / undo count / move count,
  with two charts below
  - **`AI (white) score`** (for debugging): a line of per-move scores, with a
    symlog y-axis whose order of magnitude only ever grows. **A filled dot = a
    search result (has depth, i.e. evidence); a hollow dot = a static estimate.**
    Below it, a monospaced readout shows the search parameters (depth / nodes /
    nodes per second / elapsed). The stone colour in the title follows the
    player's choice (`AI (black) score` / `AI (white) score`) — the two charts
    sit next to each other, and without stating which colour the AI plays, the
    two curves get read as each other's
  - **`Human win rate (est.)`**: a monotonic curve converted from the engine's
    score (the AI's perspective is negated), on a fixed 0–100% y-axis that does
    not auto-scale. **This is an estimate under the engine's model, not a
    statistically calibrated probability** — 50% only means "the engine thinks
    it is balanced", and 100%/0% appear only for a mate the search has proven
  - **`Engine` / `TCP port`** (for diagnostics): the first says *who* computed
    this move (`C++` / `Python (local)` / `Book`), the second says which port
    the connection landed on
- **Asynchronous AI**: threaded on a QThread, so the UI never stalls
- **Two ways to play**: right after the splash you choose "Challenge the AI" or
  "Local match". A local match involves no AI at all — two people take turns on
  one machine — so the turn hint and the final result both speak in stone
  colours (`Black wins` / `White wins` / `Draw`), and its endgame overlay offers
  no review: with no AI there is no algorithm to review against
- **Post-game algorithmic review**: after losing to the AI you can pick a
  strength no lower than the one you played and have the engine re-compute each
  move in turn. **Every move gets a row** — the good ones marked green and
  labelled "optimal", the rest as original point → best point + score
  difference — with a progress bar and a cancel that keeps what was already
  computed. "Show position" displays the board **as it stood before that move**,
  with the best point ringed and your actual move drawn as a ghost stone, so the
  two sit on one board. The review runs on its own background thread
  (`ReviewWorker`) at the time limit of the chosen level

---

## 📦 Getting started

### Option 1: download a Release

Download the package for your platform from
[Releases](https://github.com/iamlinxuhan/GomokuAI/releases) (no Python
installation needed). Windows and Linux each have a stable build; **there is no
macOS build** (this project has never been developed or verified on macOS).

> **macOS users: please go to <https://github.com/iamlinxuhan/GomokuAI-Py> to
> download the macOS-supported version.** This repository's C++ core ships only
> Windows / Linux binaries and cannot run on macOS; that other repository is the
> older pure-Python engine and provides `.dmg` / `.pkg` for both arm64 and Intel.

Filenames look like: `GomokuAI_<platform>_<arch>_<setup|run>[_debug|_play].<ext>`.

**Three builds per platform**, differing only in whether they write logs:

| Middle segment | What it is | Writes logs? |
|---|---|---|
| `..._setup.*` | Installer, recommended for ordinary users | No |
| `..._run_play.*` | Standalone single file | No |
| `..._run_debug.*` | Standalone single file, for showing someone the scene | **Yes**, to `game_log_*.txt` next to the executable |

When something goes wrong, download the `debug` build — the log lands right
beside the executable, so you don't need to set environment variables or know
where you installed it. The logs exist for developers diagnosing AI decisions
(they are the source of the position suite in `tools/positions.py`); ordinary
use doesn't need them, which is why the installers ship `play`.

**Match the arch segment with `uname -m`**: `x86_64` → `amd_x86_64`, `i686` →
`amd_x32`, `aarch64` → `aarch64`, `armv7l` / `armv6l` → `armv7`. `amd` covers
the whole x86 family (Intel, AMD and virtual machines all take this one). **The
two ARM entries are merged into one name**, rather than a self-contradictory
combination like `arm_x86_64`.

| Platform | Arch | Installer | How to install |
|---|---|---|---|
| Linux | `amd_x86_64` | `GomokuAI_Linux_amd_x86_64_setup.deb` | `sudo dpkg -i` → 「五子棋AI」 in the menu |
| Linux | `amd_x32` | `GomokuAI_Linux_amd_x32_setup.deb` | Same |
| Linux | `aarch64` | `GomokuAI_Linux_aarch64_setup.tar.gz` | Unpack, then `sudo ./install.sh` |
| Linux | `armv7` | `GomokuAI_Linux_armv7_setup.tar.gz` | Same |
| Windows | `amd_x86_64` | `GomokuAI_Windows_amd_x86_64_setup.exe` | Double-click; installs to `%LOCALAPPDATA%\GomokuAI`, no admin rights needed |

If unsure, take the installer: it declares the graphics libraries, a CJK font
candidate chain and `hicolor-icon-theme` in `Depends:`, and installs its icon
under `/usr/share/icons/hicolor/` alongside the menu entry, so the app shows up
with its real icon rather than a blank one. A bare file cannot
declare dependencies — it just reports whatever is missing. Bare files also need
the executable bit set by hand (Release assets do not carry file permissions):

```bash
chmod +x GomokuAI_Linux_amd_x86_64_run_play
./GomokuAI_Linux_amd_x86_64_run_play
```

**Everything runs on CPU only — no GPU driver, no PyTorch/CUDA.** `play` and the
installer builds leave no files on disk at all (no logs, no config, no cache
directory); `debug` writes only `game_log_*.txt`. To point any build at a log
directory (say you temporarily want logs from `play`), use an environment
variable: `GOMOKU_AI_LOGDIR=/tmp/gomoku ./GomokuAI_Linux_amd_x86_64_run_play`.

**On Windows on ARM, take the x64 build.** The OS ships x64 emulation and it
runs fine; **there is no native Windows ARM package** — PyQt5 has no `win_arm64`
wheels across the entire series (PyPI publishes Windows wheels only as
`win_amd64` and `win32`), so it cannot be built.

### Option 2: run from source

```bash
git clone https://github.com/iamlinxuhan/GomokuAI.git
cd GomokuAI
uv sync                              # Python >= 3.11; runtime deps are just numpy + PyQt5
uv run python main.py
```

Dependencies are managed with **uv** (`pyproject.toml` + a committed
`uv.lock`). There is no `requirements.txt` anymore; without uv,
`pip install numpy PyQt5` is exactly equivalent for running from source.

---

## 🎯 Rules

1. Standard Gomoku rules, 19×19 board
2. Black moves first; whoever first gets **five in a row** horizontally,
   vertically or diagonally wins
3. Players alternate, and a point cannot be played twice

---

## ⚙️ The C++ compute core

The algorithm is not newly written: constant tables, the pattern classifier, the
search structures and the three-state VCF semantics all correspond one-to-one
with `engine_local.py`, and the comments in `cpp/src/*.h` record, item by item,
the historical bugs where "these few lines **must not be helpfully fixed**".

### Build

```bash
cmake -S cpp -B cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build cpp/build -j
./cpp/build/gomoku_engine --selftest        # in-process self-test, exit 0 = pass
```

**Zero third-party dependencies**: only the standard library plus platform
sockets (platform differences are compiled away into a single file,
`tcp_server.cpp`). Every build automatically runs `--verify-tables` — the
112-line numbering being off by one is the only thing in this port that **fails
silently** (the symptom is "the scores are slightly off", with no error), so it
has to be surfaced at build time.

### Architecture: two engines, one façade

```
┌─ Python (UI, PyQt5) ────────────┐        ┌─ C++17 ───────────────────┐
│  main.py                        │        │  gomoku_engine            │
│    └ AIWorker(QThread)          │  TCP   │   ├ Board (bitboard)      │
│         └ engine.ai_move() ─────┼───────►│   ├ evaluate (incremental)│
│              │                  │ JSON   │   ├ search (PVS+TT+QS)    │
│              ├─ ok → C++ result │◄───────┤   └ vcf (forced fours)    │
│              └─ fail → silent ↓ │        │  one client / cancellable │
│  engine_local.py  (the old one) │        └───────────────────────────┘
│    fallback + all light funcs   │
└─────────────────────────────────┘
```

**Only the search goes over TCP, and there are exactly two kinds of it.**
`ai_move` (playing) and `analyze` (post-game review) go over TCP; `check_win` /
`win_line` / `evaluate` / `opening_move` stay local, because they **all run
synchronously on the UI main thread** (every stone placed runs each of them).
Moving them onto TCP would mean the interface blocking on a network round trip
every move, which directly conflicts with "the UI never stalls".

`analyze`'s request fields are **byte-identical to `compute`'s** (including the
conditional `bias_*` and `enhanced/lmr/extend`) — a review has to use the same
configuration the game did, or it is reviewing a different game. The only
difference is the extra `cands` array in the reply. It too runs on a background
thread (`ReviewWorker`), one move per time limit, so going over TCP does not
block the main thread.

**Difficulty parameters are authoritatively sent down by Python.** Every request
carries `time/max_depth/vcf_budget/qply` plus the entry level's bias
coefficient; the C++ side hard-codes no difficulty table. The benefit is that
calibration parameters need no recompilation, and that "difficulty" stays a UI
concept — consistent with the abstraction that "in PvP the AI is just one of the
players".

**The `gomoku_engine` binary is a server; you never start it yourself.** The UI
connects lazily through `engine.py`, launches it if it cannot connect, and stops
it via `atexit` on exit. A fixed port would collide with leftover processes from
older versions, so ports come from a **pool** (`config.PORT_POOL`, 15 uncommon
ports), tried one by one; each one must **pass a `hello` check** before any
search is run on it — "you can connect" is not the same as "it is our engine".
It listens on loopback only.

**Degradation is silent, and never betrays the game.** Server not compiled,
failed to start, crashed, displaced by another window, all 15 ports
unavailable — every case is translated into "this one used the local engine",
the game continues, and **no dialog is shown and no exception is raised**. Only
one line of reason goes to stderr, and the panel's `Engine` row flips to
`Python (local)`. **An opening-book hit is not degradation**: that move neither
searched nor used TCP, but that is **by design**, so it reports `Book`
(reporting `Python (local)` would raise a false alarm on the very first move of
every game). When a level exceeds the local table (which has only 3 levels),
levels 4/5 fall back to **the strongest local level** rather than erroring out.

### The two engines' scales must agree

The C++ port follows Python's scoring scale and changes not a single constant.
The win-rate anchors in `analysis.py` are pinned directly to `LV_THREE_LIVE`,
`BONUS_DOUBLE_FOUR` and `STATIC_MAX` (verified one by one by
`tests/test_analysis.py::test_anchors_are_engine_constants`). When the scales
agree, the two charts on the right are correct by construction; when they
disagree, the charts are drawn **silently** wrong. Hence the client validates
`board_size` / `win_score` / `static_max` during the handshake, and switches
ports and launches its own instance if they don't match.

### A counter-intuitive pitfall: `_VCF_NODE_CAP`

Python's `_VCF_NODE_CAP = 120000` works out to about 2.4 seconds, far more than
the highest level's 1.5 s VCF budget, so it essentially never triggers. **In
C++, 120k nodes take about 0.05 s, so this cap triggers on every single level** —
a large number of provable mates get classified as `EXHAUSTED` ("could not
finish", i.e. no verdict), making **the engine actually weaker at VCF than the
Python version**. This is the opposite of the intuition that "C++ is stronger",
and it is the easiest thing in this kind of port to miss. It is now called
`VCF_NODE_CAP` and sent down with the request.

### Measured throughput

| Engine | Median nps (5 fixed midgames) |
|---|---|
| `engine_local.py` (Python + numpy) | 47,000 – 50,000 |
| `gomoku_engine` (C++17) | 0.6M – 3.3M |

Around **20–30×**. Note that nps **decreases** as the level rises (about 430k at
level 2, 940k at level 5) — that is not a regression: higher levels use a larger
qply, so the quiescence share rises to 40%–90%, and quiescence nodes are much
cheaper than main-search nodes, so mixing them into the same denominator drags
nps down.

---

## 🎛️ Difficulty

The five parameter sets are exactly `DIFFICULTY` in `engine.py`; units and values
can be compared directly:

| Level | Time per move | Depth cap | Quiescence plies | VCF budget | Enhanced | Attack bias (tolerance band) |
|------|---------|---------|------------|---------|---------|------------------|
| **入门 (Entry)** | 0.5 s | 2 | 2 | 0 s | — | 1.5 / 0.5, band 0 |
| **初级 (Novice)** | 3 s | 4 | 4 | 0.3 s | — | — |
| **中级 (Intermediate)** | 7 s | 10 | 8 | 0.5 s | — | — |
| **高级 (Advanced)** | 15 s | 24 | 10 | 1.5 s | ✅ | — |
| **宗师 (Grandmaster)** | 20 s | 24 | 12 | 3.0 s | ✅ | — |

- **"Depth cap" is a cap, not a promise.** The depth actually reached is decided
  by iterative deepening within the time limit — the first few opening moves
  come back immediately (the evaluation finds nothing worth searching), and only
  the midgame runs out the clock.
- **VCF time counts inside that limit**, not as extra overhead.
- **All five levels have VCF on.** The Novice level once had it off, and that was
  a mistake: with it off, Novice could neither work out its own chains of fours
  nor see the opponent's — that is not "a lower difficulty", that is
  **blindness**. In a real lost game, the human won with an 11-move VCF chain,
  and Novice had no mechanism at all that could see it. The lower levels are now
  separated by **depth cap**, not by a missing subsystem.
- **Only the Entry level carries a "root-node attack bias"**, and it applies at
  the root only, never injected into `evaluate()` — making the evaluation itself
  asymmetric would destroy its zero-sum property, and zero-sumness is a
  precondition for search correctness. The mechanism is a **tolerance band**:
  moves whose scores are within the band of the best move (the Entry band is 0)
  remain candidates, and within the band one is chosen by
  `1.5 × own pattern − 0.5 × opponent threat`; outside the band not a single
  point is conceded, and mate verdicts (`is_mate`) do not take part in the
  reordering. So it **only reorders moves whose scores are exactly equal**,
  sacrificing nothing.
  Novice and Intermediate once carried a band of 3000 as well (to be "more
  aggressive"), and it was **measured and removed**: in same-level self-play the
  biased side scored 25–35% at Novice and **0% (0–8)** at Intermediate. Even
  "only reordering among equal scores" lost 6–14, which shows the real cause is
  not the band width but that **this criterion judges the opposite of what the
  search judges** — any move creating a four lifts "own pattern" to about
  100000, overwhelming the reordering; and at a depth of 4–6 plies "creating a
  four that the opponent casually blocks" is not progress. **"More aggressive"
  and "weaker" have to be measured separately before they can be discussed — and
  here they measured out to be the same thing.**

**Measured (`python tools/bench.py --engine cpp --level N`, 5 fixed positions).**
Note that "depth cap" is the configured value while "median depth" is what was
actually reached:

| Level | Wall clock median / max | Depth median / max | nps median | Time compliant |
|---|---|---|---|---|
| 入门 | 44 ms / 65 ms | 2 / 2 | 12,504 | 5/5 |
| 初级 | 76 ms / 260 ms | 4 / 4 | 488,899 | 5/5 |
| 中级 | 5992 ms / 5996 ms | 5 / 6 | 1,279,829 | 5/5 |
| 高级 | 8640 ms / 12 792 ms | 6 / **9** | 977,729 | 5/5 |
| 宗师 | 9216 ms / 17 048 ms | 6 / **9** | 894,351 | 5/5 |

**Entry and Novice come nowhere near using their time** (44 ms / 76 ms): their
`max_depth` is 2 / 4, so the cap takes effect before the clock does — that is
deliberate. **Advanced and Grandmaster reach the same depth on these 5
positions**: several of the five are **already decided** (for example, the side
to move in `mid_10stones` has already lost), and those should return instantly.
**A staircase only spreads out on narrow trees**; the real distinction between
these two levels is 15 s vs 20 s and a `vcf_budget` of 1.5 s vs 3.0 s, which
shows up only in wide midgames. This table reads "how quickly these 5 positions
resolve", not "the engine is always this fast".

**All five levels share one opening book.** That is a deliberate trade-off:
Entry and Novice now also play strong opening moves, so **the gradient across
the first few opening moves is narrow**, in exchange for "no level's opening is
the first move that a raw search happened to produce". There is also an
independent reason Advanced/Grandmaster carry no attack bias: the book was built
with an unbiased Grandmaster, and giving them a bias would make the moves stored
in it disagree with the move a fresh search would produce.

**Intermediate's 7 seconds was set by one specific position, not guessed.** In a
real lost game (Intermediate playing white, human winning on move 31 at J8), the
decisive move is move 4: with only three stones on the board, **ply 4 picks K9
while ply 5 picks K7/K11** — adjacent plies reaching opposite conclusions. Re-
searching this move with a 5.0 s budget gives **two different answers**, in a
ratio that varies with how busy the machine is (with 6 saturated processes
running, 4/4 all played the losing move); 7.0 s gives the right answer stably at
4/4. **This pushes the horizon farther out, it does not cure anything** — any
fixed budget has a horizon, and this limit only guarantees "this one position is
visible".

---

## ⚡ Enhanced search (Advanced and Grandmaster only)

Three `SearchConfig` fields carry this, and **all default to 0**: `enhanced` (the
master switch), `lmr`, `extend` (the last two exist only to isolate
measurements).

**Defaulting to 0 is the key to the whole design.** With `enhanced == 0`,
**every code path is bit-for-bit identical to before** — so the exact moves
pinned in `--selftest`, and that ply encoding of `WIN_SCORE-2`, pass unchanged;
the line-for-line agreement between the Novice/Intermediate levels and the Python
reference implementation `engine_local` continues to hold; and LMR cannot leak
into the lower levels unnoticed.

### It really does search deeper within the same time limit

Five fixed positions, **not one second more of budget** (L4 = 15 s), toggling only
`enhanced`; `lmr` and `extend` can each be turned on alone, precisely to answer
"which one is doing the work":

| Position | Off | LMR only | Extension only | Both |
|---|---|---|---|---|
| `mid_10stones` | 1 | 1 | 1 | 1 |
| `mid_clash` | 5 | 5 | 5 | 5 |
| `spread_wide` | 5 | **6** | 5 | **6** |
| `dense_threat` | 6 | **9** | **8** | **9** |
| `sparse_early` | 6 | **8** | 6 | **8** |
| **median / max** | **5 / 6** | **6 / 9** | 5 / 8 | **6 / 9** |

**LMR is the main lever**: on its own it achieves exactly the same depth as "both
on". **The extension alone does not move the median on open positions**, and
contributes 6 → 8 only on the threat-dense `dense_threat` — consistent with its
design intent (it extends moves where "you created a threat that must be
answered", which barely triggers at all in positions with sparse threats).
**Both together are never worse than either alone**, so production takes `both`.

On `dense_threat` it reaches depth 9 in 11.1 seconds, **not using the full 12.8
seconds** — this is not bought by piling on time. The same position in the
`--selftest` midgame probe goes from
`dep=6 / 1.892M nodes / 1700 ms` to `dep=7 / 1.450M nodes / 1074 ms`:
**deeper, fewer nodes, less time**.

### A/B win rate: the criterion has to be measured at a budget where the treatment actually takes effect

`tools/ab_enhance.py` is a paired A/B (same openings, colours swapped each way).
Lowering the per-move budget first is deliberate — measure "does the treatment
take effect" first:

| Per-move budget | Depth off | Depth on | Depth difference? |
|---|---|---|---|
| 1.5 s | median 5, max 6 | median 5, max 6 | **No** |
| 6 s | median 5, max 6 | median 6, max 8 | Yes |
| 15 s | median 5, max 6 | median 6, max 9 | Yes |

The reason is not complicated: LMR does not apply at all when `depth < 3`, the
extension needs `depth >= 3`, and 1.5 s only reaches 4–6 plies. **At 1.5 s both
arms play the same moves, so of course the win rate can only be 50%.** So the
test is run at 6 s, where the treatment genuinely takes effect (30 games,
paired):

| Games | Enhanced wins / losses | Win rate | Overall p | Paired McNemar p | Fisher p as black |
|---|---|---|---|---|---|
| 30 (15 pairs) | **21 / 9** | **70.0%** | one-sided 0.021 | one-sided 0.035 | two-sided 0.035 |

**The most informative column is the black one**: Gomoku is a game with a huge
first-move advantage, and across the 30 games the first player won 22. The
enhanced arm went **14–1 as black (93%)**, while the baseline as black was only
8–7 — nearly the entire gap comes from "can you cash in when you get the first
move". As white the enhanced arm was 7/15 (47%), roughly level with the
baseline: white's disadvantage is not something two extra plies can turn around.

**One limitation needs stating clearly**: 6 s is the budget at which the
treatment takes effect, whereas production levels are 15 s / 20 s. **70% should
not be extrapolated to "it also wins 70% at production levels"** — the strongest
statement available is "on a budget where the enhancement genuinely takes effect,
it is clearly stronger". The full chain of evidence is: depth (same budget,
median 5→6, max 6→9, with fewer nodes and less time) → win rate (70%, p ≈
0.02–0.035) → no drop in solve rate (L4/L5 are still 8/8 on
`tools/positions.py`, and the moves and scores for all 8 positions are unchanged
character for character).

### Two pitfalls that were quantified

- **Iterative budget estimation bailed out far too conservatively.** The
  criterion for "is the next iteration worth starting" multiplies a very noisy
  estimate of the branching factor; at a 1.5 s budget it finished having spent
  only 26%, and one ply shallower than the baseline. The crux is that "try one
  more iteration" and "bail out now" have **asymmetric** costs: trying and not
  finishing = the same depth + the time spent; bailing out = the same depth +
  the time saved. **Trying once is a free option**, so a second gate was added:
  at least half the budget must be spent before an early exit is allowed. **This
  deserves recording separately because its symptom is indistinguishable from
  "the algorithm got dumber"** — the enhanced arm was neither slower nor wrong
  at short budgets, it just "decided it was satisfied early".
- **The 2-way bucket lookup threw away half its hits.** When the TT changed from
  "direct mapped" to "2 ways per bucket", the two geometries shared one array,
  and the `lookup()` branch for "only way 0 hit" never updated the index pointer
  — so an entry was hit, but compared against the key using a stale index;
  failing that comparison, it was returned as a miss. **Hits were silently
  discarded**, with no abnormal behaviour from the engine, only reduced
  intelligence (TT hit rate 1.5%–5.9% → **0.0%–0.2%**). So `--selftest` gained a
  `ttHitRate > 0.005` guard — **the threshold must not be written as `> 0`**,
  which would be entirely insensitive to a hit rate collapsing to 0.1%.

---

## 📖 Opening book

The strongest configuration's best moves for the first four opening moves are
precomputed offline into a table that is looked up at runtime. The artefact is
`GomokuAI/opening_book.py` (**generated by `tools/build_book.py`, do not edit by
hand**), with 41 entries covering positions with ≤3 stones on the board.

```bash
python tools/build_book.py               # 20 s/node by default, resumable if interrupted
python tools/build_book.py --port 8899   # must change ports while the game is running
```

**The tree branches only on the opponent's turn.** Our own turn stores a single
move (in real play we **always** play it, so building a book for a world where
"we don't play our own best move" would be money burnt); the opponent's turn
expands to 4 moves — only the opponent can deviate, and every deviation needs an
answer. The whole tree is therefore only `1 + 8 + 8×4 = 41` nodes, and the 8
branches for the second move are the `OPENING_SECOND_MOVES` list in
`tools/selfplay.py`. **The branches must include "the opponent's own move" as a
position** — the book is partitioned by `(position, side to move)`, and after
black plays its best move it is white to move, which is a **new key**, and the
highest-probability one at that.

**There is only one Python table; the C++ side has no mirror.** The book is hit
inside `engine.py`'s `ai_move`, **before the request is even sent** — C++'s
`openingMove()` is not on the move-generation path at all. (`analyze`, the other
thing that goes over TCP, does **not** consult the book either: a review wants
"the candidate scores for every move", and the book supplies a single move —
not the same thing. Worse, letting the book in would let a review report "the
book plays here" as "the engine thinks this is best", which is a different
claim.) So the only effect of
"mirroring it" would be one more table that can drift: each side has its own
Zobrist hashing, and once they diverge C++ would **silently** find no entries at
all (presenting as "the book seems not to work"). **Write no second table, and
there is no divergence to have.**

**The empty board still plays tengen, bypassing the table.** `opening_move`'s
"empty board → tengen" is a **pinned rule**, and both `main.py` and the docs
depend on it. ⚠️ **The gate must be based on "a table hit", not on "few stones on
the board"** — the latter would also activate the old rule that "places a point
at a fixed offset hugging the opponent's first stone", which is not a
strengthening but a **lobotomy**.

**The book covers only the standard opening set — going off it means "fall back
to search", not "play wrong".** As soon as the opponent plays anything outside
those 4 branches, the position is no longer a key for any entry → no lookup →
**fall back to a regular search, character for character identical to not having
the book at all**. The book has no "approximate match" escape hatch (hashing is
an exact comparison), so even if it stored a bad move, that move could only take
effect **in its own positions** and could not spill outside them.

**Scores are reported faithfully.** What the table stores is the **true
evaluation at build time** (from the side to move's perspective), reported as-is
on a hit, so `nodes` is 0 while `best_val` is non-zero. The cost is that Entry
and Novice show grandmaster-level readings on the win-rate chart for the first
few opening moves — what the book stores is a grandmaster evaluation, and
filling in 0 would be lying instead.

**The panel reports `Book` on that row.** The `Engine` row has three values,
corresponding to three genuinely **different** execution paths: `C++` (went
through the server), `Python (local)` (degraded), `Book` (table hit, zero
delay). Listing the third separately is necessary — reporting it as `C++` would
be a lie (it never connected), and reporting it as `Python (local)` is worse:
that is a synonym for "degraded". Note that the AI's first move when playing
first goes through `opening_move` and **does not pass through `ai_move`**, so
the indicator is broken on that path; hence the public entry point
`engine.note_book()`, with the two paths reporting the same value asserted
directly by `tests/test_opening_book.py`.

---

## 🧠 Engine design

### Mate scores and static scores are banded apart

Static scores are clamped to ±`STATIC_MAX`, mate scores start above
`WIN_SCORE - MAX_PLY`, and a vacuum band of 128 sits between them — so "this is
a forced mate" and "this position looks good" cannot be confused numerically, and
`is_mate()`'s verdict is reliable rather than a guess.

The old engine's mate score (`-10000000 - depth`) **overlapped in range** with
its own composite static score — the attack/defence weighting of the "desperate
mode" could reach ±3.45e7, larger than the mate score. So "I am dead in three
moves" and "this position is pretty bad" were numerically indistinguishable, and
the UI just saw a large negative number. The key difference is not speed but
**whether that number can be read**.

### Time is the hard limit; the depth cap is a safety valve

After taking the level, `think` sets a **hard** deadline (`t0 + time × 0.85`,
keeping 15% for the return trip and the UI); iterative deepening goes as deep as
it can before that deadline, and `max_depth` exists only to stop a midgame that
"looks quiet but is full of fours everywhere" from dragging even the low levels
to 20 plies.

**The safety valve has to actually constrain.** Novice once did not: its time
was only enough for ply 3, and on the midgame from that real lost game, ply 3
and ply 4 gave **opposite** verdicts (ply 3 reported "slightly behind", ply 4
reported "nearly dead"). `tests/test_difficulty.py::test_low_level_reaches_its_
own_depth_cap` pins this down.

### Why the threat-response layer was deleted

The old version had a roughly 130-line `_check_immediate_threat` layer before
the search, using seven heuristic rules to answer "what should I play now?" Its
failure mode was structural: **the time saved when the heuristic is right is far
smaller than the game lost when it is wrong.**

The most typical criterion read `opp_win >= 2 or opp_live4 >= 2`, whereas the
most common double-threat shape in real play is `opp_win == 1 and opp_live4 >= 1`
— **which falls exactly outside it**. The patch was to add a rule, and rules can
never be finished; the real problem is "approximating a decidable problem with a
finite set of rules". The new engine splits it into two mechanisms that give
deterministic verdicts: quiescence search counts the number of winning points
(exact), and VCF handles longer forced sequences (equally exact, and able to
distinguish clearly between "there is no mate" and "I could not finish").
`tests/test_threat.py` asserts precisely that "**these names should no longer
exist**" — the positions they used to handle must still be handled correctly, but
no longer by heuristic.

---

## 📁 Project layout

```
GomokuAI/
├── main.py            # UI assembly + game-flow wiring (no search/eval logic)
├── engine.py          # engine façade: TCP client + player abstraction + silent degradation (**no algorithms**)
├── engine_local.py    # the AI engine proper: bitboard / eval / search / VCF (no Qt, numpy only, unit-testable standalone)
├── config.py          # locating the C++ binary and connection parameters (path resolution only, no IO)
├── cpp/               # C++17 compute core (zero third-party deps, CMake)
│   └── src/           # board / evaluate / search / vcf / opening / tcp_server + json_util
├── analysis.py        # score -> win-rate conversion, symlog mapping, readout formatting (pure functions, no Qt)
├── charts.py          # the two self-drawn charts on the panel (line / grid / markers)
├── gamelog.py         # game logs and board-coordinate format
├── theme.py           # design system: palette / font sizes / spacing / radii / QSS generation
├── ui_kit.py          # reusable widget primitives (titles, info rows, buttons, page skeleton, tech texture)
├── board_geometry.py  # pixel <-> cell conversion (pure math, no Qt)
├── board_render.py    # offscreen board rendering (wood grain / stone sprites / layer cache)
├── tests/             # pytest: geometry, incremental engine, position suite, win-rate conversion, review candidates, colour-literal guard
├── tools/             # bench / selfplay / positions / gui_smoke / ui_e2e / build_book / ab_enhance …
│   ├── BASELINE.md    # measured baselines and thresholds per stage
│   └── legacy_engine.py  # verbatim snapshot of the old engine (must not be modified; the A/B control)
├── packaging/         # shared packaging recipe for the four Linux architectures (built in a container)
├── installer/         # Windows installer script (Inno Setup, UTF-8 with BOM)
├── pyproject.toml     # dependency manifest (uv): numpy / PyQt5, no version pins
└── uv.lock            # locked versions per platform and Python (committed)
```

---

## 🧪 Tests

```bash
uv sync                              # runtime + dev dependencies (pytest / pyinstaller)
uv run pytest -q                     # full suite, 359 tests
uv run pytest -q -m "not perf"       # skip machine-speed-dependent thresholds (what CI runs, 338 tests)
```

**About the `perf` marker**: a handful of thresholds test "has the engine
regressed", but their readings are "how many nodes per second" or "what ply did
it reach within 3 seconds" — on a slower machine, a regression and a slow machine
are numerically indistinguishable. **Correctness, incremental consistency, time
compliance, the position suite, VCF and pattern classification carry no marker**;
they should pass on any machine — the time bounds are enforced by the engine
itself, independent of machine speed.

Main coverage: bitboard five-in-a-row detection and incremental state diffing
(`test_win` / `test_incremental`), pattern partial ordering and zero-sumness
(`test_eval`), transposition entry types and mate-score normalisation (`test_tt`),
**finding mates and recognising lost positions** (`test_search_mate`), **not
resigning when sentenced** (`test_escape`), VCF's three states and "all three
levels really do have VCF on" (`test_vcf`), difficulty thresholds and **low
levels reaching their own depth cap** (`test_difficulty`), cancellation latency
(`test_cancel`), win-rate anchors having to be `engine` constants
(`test_analysis`), the opening book's **wiring** (`test_opening_book`), the
review's candidate table (`test_analyze`: the table is **complete**, its maximum
equals the reported best value, collecting does not change the chosen move, and
a fresh engine is used each time), and the colour-literal guard
(`test_no_literal_colors`).

There is also `tools/gui_smoke.py` (headless UI smoke test, testing **wiring**)
and `tools/ui_e2e.py` (**actually plays one full game at each of the five
levels**, through the real click paths). The human side in the latter is a
deterministic policy (tengen first move), so **the tengen first move makes the
opening book genuinely get hit** — "the book takes effect in the UI" is therefore
verifiable in the logs. Measured across the five levels:

| Level | Moves | Max AI search depth | Book hits |
|---|---|---|---|
| 入门 | 10 | 2 | 2 moves |
| 初级 | 10 | 4 | 2 moves |
| 中级 | 10 | 6 | 2 moves |
| 高级 | 10 | 8 | 2 moves |
| 宗师 | 10 | 8 | 2 moves |

**This is exactly the gradient that was supposed to open up, measured in real
games** (2 / 4 / 6 / 8). The same table incidentally confirms the book is hit at
every level (`0 ms`), and that **the Entry level's book reading is `dep=9`** —
what the book stores is the true depth at build time, reported as-is, which is
deliberately preserved behaviour.

---

## 📄 License

Released under the **GNU General Public License, version 3 or later**; SPDX
identifier `GPL-3.0-or-later`. Full text in [LICENSE](LICENSE).

```
Copyright (C) 2026 Lin Xuhan <2276677131@qq.com>
Copyright (C) 2026 YFY0109 <yfy0109@qq.com>
```

### Why GPLv3

The UI is built on **PyQt5**, which is itself released under **GPLv3** (Riverbank
offers only GPLv3 or a commercial licence). A binary that links it is therefore
already covered by GPLv3 as a whole, so the source licence has to agree with it —
the GPL-2.0 text this repository previously carried had no "or later" clause,
reading as GPL-2.0-only, which is **incompatible with a GPLv3 PyQt5**. Both
copyright holders agreed to move to GPL-3.0-or-later.

If a permissive licence (MIT, say) is ever wanted, the real path is swapping
PyQt5 for **PySide6** (LGPL), which an MIT project may call at runtime; that
needs an API migration (`pyqtSignal` → `Signal`, `pyqtSlot` → `Slot`, and so on).

---

## 📝 Changelog

| Version | Date | Contents |
|---|---|---|
| **v3.0.8** | 2026-10-03 | **Fixed: the Windows build silently fell back to the Python engine.** A user reported the Windows version dropping to the local Python engine "for unknown reasons" — the symptom is an **AI that is visibly slower and weaker, with no hint anywhere in the UI**, because the fallback path only prints one line to stdout (`_note("local", exc)` in `engine.py`) and packaged builds write no log, so the user never sees it. **The cause was not the engine's algorithms but the seam between packaging and build**: a PyInstaller bundle's root contains `VCRUNTIME140.dll`, `VCRUNTIME140_1.dll` and `ucrtbase.dll` but **not `MSVCP140.dll`** (the copies Qt and numpy ship both sit in subdirectories, and `pyi_rth_pyqt5.py` only prepends the bundle root to PATH, so the child process's loader cannot reach them), while `gomoku_engine.exe` was built with MSVC's default **`/MD`** and therefore imports it. On a machine without the VC++ Redistributable the engine dies at load with **`STATUS_DLL_NOT_FOUND` (`0xC0000135`)**; on a machine that has it, everything works — which is exactly why some users were hit and others were not. **How to tell which engine a log came from**: the `reason` column is useless (both engines write `PVS搜索`), and the `引擎:` line only says the file was *found*, not that it ran — you have to look at **`val` + `dep`**: C++ level 1 is `time=0.5 / max_depth=2` while local Python level 1 is `time=3.0 / max_depth=4`, so on the same position C++ returns `val=-610 / dep=2` and local Python returns `val=-540 / dep=4`; the reported log showed `val=-540 / dep=4 / 1640ms`, **matching the local Python engine character for character**, with a `dep=4` that physically exceeds the C++ level's depth cap. **The fix**: `cpp/CMakeLists.txt` now sets `CMAKE_MSVC_RUNTIME_LIBRARY` to `MultiThreaded` under MSVC, **linking the runtime statically** so the engine carries everything it needs and no longer depends on the target machine having the Redistributable (the variable is read when `add_executable` runs, so it has to come first); and the `windows` job in `.github/workflows/build.yml` now scans the bytes of `gomoku_engine.exe` right after building and **fails if `MSVCP140.dll` is present** — this regression is silent, so CI has to go red on the spot rather than waiting for a user to report it. Linux is untouched (the whole block is inside `if(MSVC)`); the build plus `--verify-tables --selftest` and all 345 tests stay green |
| **v3.0.7** | 2026-10-03 | **The review screen now lists every move, none skipped.** It used to show only the moves that were *not* optimal, but a review can run for minutes — coming back to check "where did I play on move 9?" found nothing, because that move wasn't in the list. Now **every move is a row**, good ones included, marked green and labelled "optimal", and the subtitle reads "N moves, M improvable". **The opening move on an empty board and "the search never finished a round" are now their own category**: they were *never compared*, not "compared and found fine", so they say so plainly ("no candidates to compare" / "could not compare") and are **excluded from the improvable count** — counting them would have the result screen accusing the player of mistakes they never made. **A tied optimum now rings the move you actually played**: `delta == 0` only means your move scored the same as the engine's best, and `best_idx` is merely one of the tied points — it can easily be a different one, and ringing elsewhere while the row says "optimal" reads like the program contradicting itself. On a tie the best point is repointed at your own stone, so the ghost stone and the ring coincide. **The move list must not break**: `ReviewWorker` used to drop an entire row whenever the candidate table came back empty; now only a cancel drops a row. Also fixed a page leak — clicking "Show position" repeatedly stacked one full board widget tree per move viewed (each with two layers of pixmap cache), the same bug `_drop_pages` documents as "one tree left behind per game restarted"; the previous board is now reclaimed on every switch. The list height shrinks to its content under the same 420px cap, so two records no longer sit in an empty 420px box |
| **v3.0.6** | 2026-10-03 | **Local two-player mode**: a mode screen now sits between the splash and the colour screen ("Challenge the AI" / "Local match"). The latter involves no AI at all — two people take turns on one machine — so the turn hint and the final result both speak in stone colours ("Black wins" / "White wins" / "Draw"), and its endgame overlay offers no review, since with no AI there is no algorithm to review against. **Post-game algorithmic review**: after losing to the AI the overlay gains a "Review" button. Pick a strength no lower than the difficulty you played, and the engine re-computes each of *your* moves in turn, listing the ones that were not optimal (original point → best point + score difference), with a progress bar and a cancel that keeps whatever was already computed. "Show position" displays the board **as it stood before that move**, with the best point ringed and your actual move drawn as a ghost stone so the two can be compared on one board. The result screen ends with "Finish" / "Quit". **The engine side extended the TCP protocol**: a new `analyze` request carries fields byte-identical to `compute` (including the conditional `bias_*` and `enhanced/lmr/extend` — review must use the same configuration the game did) and replies with a `cands` array. Root candidates come from a new `outRoot` out-parameter on `Engine::think`; when `outRoot == nullptr`, `collectOn == biasOn`, so the entire old path is bit-for-bit unchanged and the `enhanced == 0` C++↔Python identity is untouched. An older C++ build answers `analyze` with "unknown type", which the Python side silently degrades to local analysis — both directions of the protocol are safe. Worth recording: **to get a complete candidate table you must disable the aspiration window while collecting** — a narrow window prunes some root moves, leaving them with no score to compare; missing that one spot raises no error, it just yields a half-filled table |
| **v3.0.5** | 2026-10-02 | **Windows promoted to stable** (verified on real hardware, `_testing` dropped), and "log or not" moved from a platform-implied default to an explicit filename declaration: every platform now has `run_debug` (writes logs next to the executable) and `run_play` (writes none), with installers shipping play. The naming scheme was overhauled — `AMD` → `amd`, `x86_32` → `x32`, and ARM's family and bit-width merged into `aarch64` / `armv7`. Variants are baked in **at build time** (by injecting a `_build_flavor.py` line), not inferred back from the filename. Fixed the black console window that popped up when launching the engine from the GUI process (`CREATE_NO_WINDOW`). From earlier: `cpp/src/bitops.h` gathers the `__builtin_*` calls into a cross-compiler wrapper, falling back to `<intrin.h>` where MSVC lacks them — before this the C++ engine simply did not compile under MSVC; the workflow's manual dispatch gained a `linux` input so you can build amd64 only, or skip Linux entirely. **The Linux installers now ship an application icon**: previously they installed no icon file at all and the `.desktop` entry had no `Icon=` key, so the menu fell back to a blank one. All four architectures now generate per-size PNGs from the existing `.ico` at build time (`python3-pil` via apt, no new runtime dependency) and install them under `/usr/share/icons/hicolor/<size>/apps/gomoku-ai.png`; the `.deb` gained `hicolor-icon-theme` in `Depends:` so `index.theme` and the icon-cache trigger are present, and the `.tar.gz` `install.sh` copies the tree and refreshes the cache when the tool exists. **The window icon is now set by the program itself**: the `.desktop` alone could not fix it, because the titlebar and taskbar read the window's own icon property (`_NET_WM_ICON` on X11, `app_id` on Wayland) and only `setWindowIcon()` writes that — with the icon installed, a running window still showed X.Org's fallback logo. `main.py` now loads a PNG generated from the same `.ico` at build time, bundles it into all three PyInstaller variants via `--add-data`, and calls `setDesktopFileName("gomoku-ai")` so KWin ties the window to the menu entry. The icon is downscaled to 128×128 before being handed to `setWindowIcon()`: a 256×256 ARGB image is 262152 bytes and does not fit in a single X request (262140), so the `_NET_WM_ICON` write silently failed and left the titlebar blank while the taskbar — which goes through the `.desktop` — looked fine. Release asset names and count are unchanged |
| **v3.0.4** | 2026-10-01 | The endgame now lights up the five first and settles afterwards: a red line sweeps across the five (extending half a cell past each end, scaled by √2/2 on diagonals), the overlay is delayed by one second, and "elapsed" stops the moment the stone lands — previously the overlay covered the whole screen and the player could not see where they had lost. Also removed the endgame "last move" ring (it sat right on the line's endpoint and cut a notch out of it). The panel's theme toggle became its own row: text 12px → 14px and centred, with the sun / moon now self-drawn (emoji never resolves to a colour font on most machines, yielding only monochrome glyphs that vary by machine) |
| **v3.0.3** | 2026-09-26 | Two fixes on the difficulty screen: under the dark theme the strength bar takes its stone colour from the theme (black stones are only 1.11:1 against the dark card); the Grandmaster card's strength bar now compresses the box spacing instead of shrinking the stones (the original 191px did not fit the card's 136px inner area). The v3.0.2 tag failed `test` and produced no Release; its contents are folded into this version |
| v3.0.1 | 2026-09-25 | Port pool (fixed 8888 → 15 ports, each passing a `hello` check); the panel gained two diagnostic values, "current TCP port" and "book"; artefact names split into family / bit-width / purpose, `.pkg` → `.tar.gz`; the release gate was rewritten (the original expanded a glob in an empty directory and reported "missing artefacts" with all 8 present) |
| v3.0.0 | 2026-09-25 | Search core replaced with C++17, difficulty expanded from three levels to five, real search pruning enabled for Advanced/Grandmaster (LMR + forced-move extension + 2-way bucket TT); opening book added |
| v2.0.2 | 2026-09-24 | Intermediate's time limit 5.0 s → 7.0 s (set by one real lost game, see "Difficulty") |
| v2.0.1 | 2026-09-24 | Fixed an evaluation symmetry issue and a board-coordinate format issue |
| v2.0.0 | 2026-09-19 | **Complete rewrite**: evaluation and search layers replaced wholesale, UI rebuilt as a unified design system. The old "layered TSS threat response", "multi-line defence", "desperate mode" and the GPU branch were all deleted |
| v1.5 – v1.8.0 | — | Early versions. The old README had a 166-line game analysis covering mechanisms that have since been deleted; the full text is still in history: `git show d232fa7:README.md \| sed -n '236,401p'` |
