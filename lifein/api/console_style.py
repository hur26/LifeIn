"""控制台的那份 CSS。**一个文件,两层共用。**

拆出来不是因为它长,是因为它**不该被翻到**:改一个按钮的行为要读
`ui.py`,而那时不该先滚过四百行颜色。

## 颜色写成变量,不是写在规则里

深色模式跟随系统,不做开关 —— 做开关就要存偏好,而存偏好要么进库
要么进 cookie,两样都比这件事本身重。跟随系统的代价是"我想在白天用深色"
做不到,而那件事浏览器和操作系统自己有开关。

## 为什么是这个绿

控制台上最重要的颜色其实是**红**(超时、吊销、删除),而一个蓝色或紫色的
主色会和红抢注意力 —— 它们在色环上离得近,并排放在一张表里时,
"这一行不对劲"要看两眼才看得出来。

一个偏冷的绿离红最远,而且它在"正常"这件事上不需要解释。

## 间距只有一把尺子

`--sp` 的整数倍,4px 起步。不给每个组件单独调间距,是因为**调过的那个
组件会变成唯一对不齐的那个** —— 而一份对不齐的界面,人说不出哪里不对,
只会觉得它不像一个正经东西。
"""

# ruff: noqa: E501 —— 这个文件里超过 100 列的全是 CSS 声明。
# 把一条规则折成三行只会让"这一条管的是哪个类"更难看出来,
# 而这里没有任何 Python 逻辑需要靠行宽来读。
from __future__ import annotations

CSS = """
:root {
  --sp: 4px;
  --radius: 10px;
  --radius-sm: 7px;
  --bg: #f5f6f8;
  --surface: #ffffff;
  --surface-2: #f0f2f5;
  --surface-3: #e8ebef;
  --border: #e3e6eb;
  --border-strong: #ccd2da;
  --text: #191d23;
  --text-dim: #59616e;
  --text-faint: #8a93a1;
  --accent: #2c6b5c;
  --accent-hover: #22564a;
  --accent-soft: #e4efec;
  --accent-line: #b7d5cd;
  --ok: #2b6a4a;
  --ok-soft: #e4f0e8;
  --warn: #8a5600;
  --warn-soft: #faefda;
  --danger: #a52c26;
  --danger-soft: #fbe9e7;
  --shadow: 0 1px 2px rgba(16,20,26,.05), 0 4px 14px rgba(16,20,26,.05);
  --shadow-sm: 0 1px 2px rgba(16,20,26,.06);
  color-scheme: light;
}

@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101318;
    --surface: #181c22;
    --surface-2: #1f242c;
    --surface-3: #262c35;
    --border: #2a303a;
    --border-strong: #3a4250;
    --text: #e6eaf0;
    --text-dim: #a0aab8;
    --text-faint: #78828f;
    --accent: #63bda7;
    --accent-hover: #7cccb8;
    --accent-soft: #16302a;
    --accent-line: #2a5449;
    --ok: #6cc08d;
    --ok-soft: #16301f;
    --warn: #e0ab55;
    --warn-soft: #322613;
    --danger: #ef8b83;
    --danger-soft: #351d1b;
    --shadow: 0 1px 2px rgba(0,0,0,.35), 0 6px 18px rgba(0,0,0,.28);
    --shadow-sm: 0 1px 2px rgba(0,0,0,.4);
    color-scheme: dark;
  }
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 15px/1.65 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
        "Hiragino Sans GB", "Microsoft YaHei", "Source Han Sans SC", sans-serif;
  -webkit-font-smoothing: antialiased;
}

h1, h2, h3, h4 { margin: 0; font-weight: 600; letter-spacing: -.01em; }
h1 { font-size: 24px; }
h2 { font-size: 15px; }
h3 { font-size: 14px; }
p { margin: 0 0 calc(var(--sp) * 2); }
p:last-child { margin-bottom: 0; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }
strong { font-weight: 650; }

/* ---------------------------------------------------------- 骨架 */

.shell { display: grid; grid-template-columns: 244px minmax(0, 1fr); min-height: 100vh; }

.sidebar {
  background: var(--surface);
  border-right: 1px solid var(--border);
  padding: calc(var(--sp) * 5) calc(var(--sp) * 3);
  display: flex; flex-direction: column; gap: calc(var(--sp) * 5);
  position: sticky; top: 0; height: 100vh; overflow-y: auto;
}

.brand {
  display: flex; align-items: center; gap: calc(var(--sp) * 2);
  font-weight: 650; font-size: 16px; letter-spacing: -.02em;
  padding: 0 calc(var(--sp) * 2);
}
.brand .mark { color: var(--accent); flex: none; }
.brand-lg { font-size: 18px; margin-bottom: calc(var(--sp) * 4); padding: 0; }

.layer {
  margin-left: auto; font-size: 11px; font-weight: 600; letter-spacing: .02em;
  padding: 2px 7px; border-radius: 20px;
  background: var(--surface-2); color: var(--text-dim);
  border: 1px solid var(--border);
}
.layer-admin {
  background: var(--accent-soft); color: var(--accent); border-color: var(--accent-line);
}

.nav-group { display: flex; flex-direction: column; gap: 2px; }
.nav-title {
  font-size: 11px; font-weight: 600; letter-spacing: .06em; color: var(--text-faint);
  padding: 0 calc(var(--sp) * 2) calc(var(--sp) * 2);
}
.nav-item {
  display: flex; align-items: center; gap: calc(var(--sp) * 2.5);
  padding: calc(var(--sp) * 2) calc(var(--sp) * 2);
  border-radius: var(--radius-sm); color: var(--text-dim); font-size: 14px;
}
.nav-item:hover { background: var(--surface-2); color: var(--text); text-decoration: none; }
.nav-item .ic { color: var(--text-faint); flex: none; }
.nav-item.is-active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.nav-item.is-active .ic { color: var(--accent); }

.main { display: flex; flex-direction: column; min-width: 0; }

.topbar {
  display: flex; align-items: center; gap: calc(var(--sp) * 3);
  padding: calc(var(--sp) * 3) calc(var(--sp) * 7);
  border-bottom: 1px solid var(--border); background: var(--surface);
}
.who {
  display: flex; align-items: center; gap: calc(var(--sp) * 2);
  font-size: 14px; color: var(--text-dim);
}
.who .ic { color: var(--text-faint); }
.topbar-actions { margin-left: auto; display: flex; gap: calc(var(--sp) * 2); }

.page { padding: calc(var(--sp) * 7); max-width: 1080px; width: 100%; }
.page-head { margin-bottom: calc(var(--sp) * 6); }
.page-head .lede { color: var(--text-dim); margin-top: calc(var(--sp) * 2); max-width: 62ch; }
.page-actions { margin-top: calc(var(--sp) * 4); display: flex; gap: calc(var(--sp) * 2); flex-wrap: wrap; }

.bare {
  min-height: 100vh; display: flex; align-items: center; justify-content: center;
  padding: calc(var(--sp) * 6);
}
.bare-card {
  background: var(--surface); border: 1px solid var(--border); border-radius: 14px;
  box-shadow: var(--shadow); padding: calc(var(--sp) * 9); max-width: 30rem; width: 100%;
}
.bare-card h1 { font-size: 20px; margin-bottom: calc(var(--sp) * 4); }

/* ---------------------------------------------------------- 卡片 */

.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: var(--radius); box-shadow: var(--shadow-sm);
  margin-bottom: calc(var(--sp) * 5); overflow: hidden;
}
.card-head {
  display: flex; align-items: center; gap: calc(var(--sp) * 3);
  padding: calc(var(--sp) * 3.5) calc(var(--sp) * 5);
  border-bottom: 1px solid var(--border);
  background: var(--surface);
}
.card-actions { margin-left: auto; display: flex; gap: calc(var(--sp) * 2); flex-wrap: wrap; }
.card-body { padding: calc(var(--sp) * 5); }
.card-danger { border-color: var(--danger-soft); }
.card-danger .card-head { background: var(--danger-soft); border-bottom-color: var(--danger-soft); }
.card-danger .card-head h2 { color: var(--danger); }

.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: calc(var(--sp) * 5); }
.grid-2 > .card { margin-bottom: 0; }

/* ---------------------------------------------------------- 数字 */

.stats {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: calc(var(--sp) * 3); margin-bottom: calc(var(--sp) * 5);
}
.stat {
  background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: calc(var(--sp) * 4); display: flex; flex-direction: column; gap: calc(var(--sp) * 1.5);
  box-shadow: var(--shadow-sm);
}
.stat-label { font-size: 12px; color: var(--text-faint); font-weight: 600; letter-spacing: .02em; }
.stat-value {
  font-size: 26px; font-weight: 640; letter-spacing: -.02em; line-height: 1.15;
  font-variant-numeric: tabular-nums;
}
.stat-value .unit { font-size: 14px; font-weight: 500; color: var(--text-dim); margin-left: 2px; }
.stat-hint { font-size: 12.5px; color: var(--text-dim); }
.stat-ok .stat-value { color: var(--ok); }
.stat-warn .stat-value { color: var(--warn); }
.stat-danger .stat-value { color: var(--danger); }

/* ---------------------------------------------------------- 表格 */

.table-wrap { overflow-x: auto; margin: 0 calc(var(--sp) * -5); padding: 0 calc(var(--sp) * 5); }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
thead th {
  text-align: left; font-weight: 600; font-size: 12px; letter-spacing: .03em;
  color: var(--text-faint); padding: 0 calc(var(--sp) * 3) calc(var(--sp) * 2) 0;
  border-bottom: 1px solid var(--border); white-space: nowrap;
}
tbody td {
  padding: calc(var(--sp) * 3) calc(var(--sp) * 3) calc(var(--sp) * 3) 0;
  border-bottom: 1px solid var(--border); vertical-align: middle;
}
tbody tr:last-child td { border-bottom: none; }
tbody td:last-child, thead th:last-child { padding-right: 0; }
.num { text-align: right; font-variant-numeric: tabular-nums; }
.row-actions { display: flex; gap: calc(var(--sp) * 2); justify-content: flex-end; }
.row-off td { color: var(--text-faint); }

/* ---------------------------------------------------------- 零件 */

.badge {
  display: inline-flex; align-items: center; gap: 5px;
  font-size: 12px; font-weight: 600; padding: 2px 8px; border-radius: 20px;
  border: 1px solid transparent; white-space: nowrap;
}
.badge-neutral { background: var(--surface-2); color: var(--text-dim); border-color: var(--border); }
.badge-ok { background: var(--ok-soft); color: var(--ok); }
.badge-warn { background: var(--warn-soft); color: var(--warn); }
.badge-danger { background: var(--danger-soft); color: var(--danger); }
.badge-accent { background: var(--accent-soft); color: var(--accent); }

.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  font: inherit; font-size: 14px; font-weight: 550; line-height: 1;
  padding: calc(var(--sp) * 2.4) calc(var(--sp) * 4);
  border-radius: var(--radius-sm); border: 1px solid transparent;
  cursor: pointer; white-space: nowrap; transition: background .12s, border-color .12s;
}
.btn:hover { text-decoration: none; }
.btn-primary { background: var(--accent); color: #fff; }
.btn-primary:hover { background: var(--accent-hover); }
.btn-ghost { background: var(--surface); color: var(--text-dim); border-color: var(--border-strong); }
.btn-ghost:hover { background: var(--surface-2); color: var(--text); }
.btn-danger { background: var(--surface); color: var(--danger); border-color: var(--danger-soft); }
.btn-danger:hover { background: var(--danger-soft); }
.btn-quiet { background: transparent; color: var(--text-dim); padding: 5px 9px; font-size: 13px; }
.btn-quiet:hover { background: var(--surface-2); color: var(--text); }
.btn-block { width: 100%; }

.hint { color: var(--text-dim); font-size: 13px; margin-top: calc(var(--sp) * 3); }
.hint:first-child { margin-top: 0; }
.faint { color: var(--text-faint); }
time { color: var(--text-dim); font-variant-numeric: tabular-nums; font-size: 13.5px; }

.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
  font-size: 12.5px; background: var(--surface-2); border: 1px solid var(--border);
  border-radius: 5px; padding: 1px 6px; word-break: break-all;
}

.banner {
  display: flex; gap: calc(var(--sp) * 3); align-items: flex-start;
  padding: calc(var(--sp) * 3.5) calc(var(--sp) * 4);
  border-radius: var(--radius); margin-bottom: calc(var(--sp) * 5);
  font-size: 14px; border: 1px solid transparent;
}
.banner .ic { flex: none; margin-top: 1px; }
.banner-info { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-line); }
.banner-warn { background: var(--warn-soft); color: var(--warn); }
.banner-danger { background: var(--danger-soft); color: var(--danger); }
.banner strong { font-weight: 650; }

.empty {
  padding: calc(var(--sp) * 8) calc(var(--sp) * 4); text-align: center;
  color: var(--text-faint); font-size: 14px;
  border: 1px dashed var(--border-strong); border-radius: var(--radius);
}

.fold { border-top: 1px solid var(--border); margin-top: calc(var(--sp) * 4); padding-top: calc(var(--sp) * 3); }
.fold summary {
  cursor: pointer; font-size: 13.5px; color: var(--text-dim); font-weight: 550;
  list-style: none; display: flex; align-items: center; gap: 6px;
}
.fold summary::before { content: "▸"; color: var(--text-faint); }
.fold[open] summary::before { content: "▾"; }
.fold summary::-webkit-details-marker { display: none; }
.fold > div { padding-top: calc(var(--sp) * 3); }

/* ---------------------------------------------------------- 表单 */

.field { display: flex; flex-direction: column; gap: calc(var(--sp) * 1.5); margin-bottom: calc(var(--sp) * 4); }
.field label { font-size: 13px; font-weight: 600; color: var(--text-dim); }
input[type=text], input[type=password], select {
  font: inherit; font-size: 14px; color: var(--text);
  background: var(--surface); border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm); padding: calc(var(--sp) * 2.4) calc(var(--sp) * 3);
  width: 100%;
}
input:focus, select:focus, .btn:focus-visible, .nav-item:focus-visible {
  outline: 2px solid var(--accent); outline-offset: 1px;
}
.inline-form { display: flex; gap: calc(var(--sp) * 2); align-items: flex-end; flex-wrap: wrap; }
.inline-form .field { flex: 1 1 220px; margin-bottom: 0; }

/* ---------------------------------------------------------- 配码 */

.qr {
  display: flex; justify-content: center; padding: calc(var(--sp) * 5);
  background: #fff; border: 1px solid var(--border); border-radius: var(--radius);
  margin-bottom: calc(var(--sp) * 4);
}
.qr svg { width: 260px; height: 260px; }
.payload {
  display: block; word-break: break-all; background: var(--surface-2);
  border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: calc(var(--sp) * 3); font-size: 12.5px;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

/* ---------------------------------------------------------- 长文 */

.prose { max-width: 70ch; }
.prose h2 { font-size: 19px; margin: calc(var(--sp) * 8) 0 calc(var(--sp) * 3); }
.prose h3 { font-size: 16px; margin: calc(var(--sp) * 6) 0 calc(var(--sp) * 2); }
.prose h4 { font-size: 14px; margin: calc(var(--sp) * 5) 0 calc(var(--sp) * 2); color: var(--text-dim); }
.prose > *:first-child { margin-top: 0; }
.prose ul { padding-left: calc(var(--sp) * 5); margin: 0 0 calc(var(--sp) * 3); }
.prose li { margin-bottom: calc(var(--sp) * 1.5); }
.prose blockquote {
  margin: calc(var(--sp) * 4) 0; padding: calc(var(--sp) * 2) calc(var(--sp) * 4);
  border-left: 3px solid var(--accent-line); color: var(--text-dim); background: var(--surface-2);
  border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
}
.prose hr { border: none; border-top: 1px solid var(--border); margin: calc(var(--sp) * 8) 0; }
.prose pre {
  background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-sm);
  padding: calc(var(--sp) * 4); overflow-x: auto; font-size: 13px;
}
.prose pre code { background: none; border: none; padding: 0; }
.prose .table-wrap { margin: calc(var(--sp) * 4) 0; padding: 0; }

/* ---------------------------------------------------------- 窄屏 */

@media (max-width: 900px) {
  .shell { grid-template-columns: 1fr; }
  .sidebar {
    position: static; height: auto; border-right: none;
    border-bottom: 1px solid var(--border); gap: calc(var(--sp) * 4);
    padding: calc(var(--sp) * 4);
  }
  .sidebar nav { display: flex; gap: calc(var(--sp) * 5); overflow-x: auto; }
  .nav-group { flex-direction: row; gap: calc(var(--sp) * 1); }
  .nav-title { display: none; }
  .nav-item { white-space: nowrap; }
  .page, .topbar { padding-left: calc(var(--sp) * 4); padding-right: calc(var(--sp) * 4); }
  .table-wrap { margin: 0 calc(var(--sp) * -5); padding: 0 calc(var(--sp) * 5); }
}
"""
