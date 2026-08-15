#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""view_trace.py —— 把一份 trace 渲染成一页可以直接打开的 HTML。

    python scripts/view_trace.py --latest --open      # 看最近一次
    python scripts/view_trace.py --list               # 列出现有 trace
    python scripts/view_trace.py path/to/trace.jsonl  # 看指定的一份

## 为什么要有这一页

JSONL 已经能用 `jq` 看了，再做一页 HTML 的理由只有一个：
**「哪一步慢」和「哪一步红」这两个问题，眼睛比 grep 快得多。**
一次跑了 12 个 span、其中第 7 个耗了 4 秒，这件事在文本流水里
要靠人做减法才能发现；画成对齐同一条时间轴的横条，一眼就看出来了。

所以这一页只回答三个问题，别的都不做：

    这次跑了多久、花了多少？        —— 顶部一条
    哪一步失败了 / 被闸门挡下了？    —— 颜色
    那一步当时收到的参数是什么？    —— 点开

不做检索、不做跨 trace 对比、不做实时刷新。那些都需要一个服务端，
而一个需要先起服务的调试工具，多半在最需要它的时候没起着。
单文件 HTML 可以直接拖进浏览器、可以粘进 issue、可以发给同事。

## 参数是摘要，不是原文

页面上展示的 `arguments` 来自 `tracing.digest_arguments`：已经截断、
已经打码。这一页不做二次脱敏，也**不应该**做——脱敏发生在写入时，
如果等到渲染时才做，那份没脱敏的 JSONL 已经躺在磁盘上了。
"""

from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from travelwise.tracing import load_trace  # noqa: E402


# ---------------------------------------------------------------- 找文件

def default_dir() -> Path:
    from travelwise.paths import CACHE_DIR
    return CACHE_DIR / "traces"


def list_traces(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob("trace-*.jsonl"),
                  key=lambda p: p.stat().st_mtime, reverse=True)


# ---------------------------------------------------------------- 整形

def build_rows(spans: list) -> list[dict]:
    """把扁平的 span 列表整成一棵按时间排好的树。

    JSONL 里 span 是**子先父后**的（父 span 在 finally 里才写出来），
    直接按文件顺序画，会看到子节点出现在父节点上面。所以这里必须重排。
    父 id 找不到的 span 挂到根上，不丢弃——一份被 Ctrl-C 截断的 trace
    正是最需要被看到的那种。
    """
    by_id = {s.span_id: s for s in spans}
    children: dict[str, list] = {}
    for s in spans:
        parent = s.parent_id if s.parent_id in by_id else ""
        children.setdefault(parent, []).append(s)

    def sort_key(s):
        return (s.timestamp, s.span_id)

    rows: list[dict] = []

    def walk(parent_id: str, depth: int) -> None:
        for s in sorted(children.get(parent_id, []), key=sort_key):
            rows.append({"span": s, "depth": depth})
            walk(s.span_id, depth + 1)

    walk("", 0)
    # 万一有环或自引用，兜住：没被走到的 span 一律补在末尾
    seen = {r["span"].span_id for r in rows}
    for s in spans:
        if s.span_id not in seen:
            rows.append({"span": s, "depth": 0})
    return rows


def _parse_ts(text: str) -> float:
    try:
        return datetime.fromisoformat(text).timestamp() * 1000.0
    except (ValueError, TypeError):
        return 0.0


def layout(rows: list[dict]) -> tuple[list[dict], float, bool]:
    """算出每个条形的左偏移与宽度（百分比）。

    偏移取自 `timestamp`，精度是 1 毫秒——离线回放整轮跑完还不到 1ms，
    于是所有条形都从 0 开始。这不是 bug，是精度不够。页面会把这件事
    写出来，而不是让人对着一堆左对齐的条形猜自己是不是看错了。
    """
    stamps = [_parse_ts(r["span"].timestamp) for r in rows]
    valid = [s for s in stamps if s > 0]
    t0 = min(valid) if valid else 0.0
    span_end = max((s - t0) + r["span"].duration_ms
                   for s, r in zip(stamps, rows)) if rows else 0.0
    total = max(span_end, 1e-6)

    out = []
    for stamp, row in zip(stamps, rows):
        offset = max(0.0, (stamp - t0)) if stamp else 0.0
        out.append({**row,
                    "offset_pct": min(99.0, offset / total * 100.0),
                    "width_pct": max(0.6, min(100.0 - offset / total * 100.0,
                                              row["span"].duration_ms / total * 100.0))})
    coarse = total < 8.0            # 全程不到 8ms → 时间轴没有意义
    return out, total, coarse


# ---------------------------------------------------------------- 渲染

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --board:#0E1A2B; --panel:#16263D; --panel-2:#1B2E49; --rule:#24384F;
  --ink:#E9EFF7; --muted:#90A6C4;
  --ok:#46C89A; --err:#F1706B; --gate:#F2B33D;
  --mono: ui-monospace,"SF Mono","Cascadia Mono",Menlo,Consolas,"Noto Sans Mono",monospace;
  --sans: system-ui,-apple-system,"Segoe UI","Noto Sans SC","PingFang SC",sans-serif;
}
*{box-sizing:border-box}
body{margin:0;background:var(--board);color:var(--ink);font-family:var(--mono);
     font-size:13px;line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:1040px;margin:0 auto;padding:28px 20px 72px}

/* ---- 抬头：航班信息牌的结构，时间在左、状态在右 ---- */
header{border-bottom:1px solid var(--rule);padding-bottom:18px;margin-bottom:22px}
.eyebrow{color:var(--gate);letter-spacing:.28em;font-size:11px;text-transform:uppercase}
h1{font-size:26px;font-weight:600;letter-spacing:-.01em;margin:8px 0 4px}
h1 .id{color:var(--muted);font-weight:400}
.req{font-family:var(--sans);color:var(--muted);margin:10px 0 0;max-width:70ch}
.strip{display:flex;flex-wrap:wrap;gap:1px;background:var(--rule);
       border:1px solid var(--rule);margin-top:20px}
.cell{background:var(--panel);padding:11px 16px;flex:1 1 130px}
.cell b{display:block;font-size:19px;font-weight:600;letter-spacing:-.01em}
.cell span{color:var(--muted);font-size:11px;letter-spacing:.1em}
.cell.bad b{color:var(--err)}

/* ---- 签名元素：对齐同一条时间轴的甘特条 ---- */
.axis{display:flex;justify-content:space-between;color:var(--muted);
      font-size:11px;padding:0 0 6px 300px;border-bottom:1px solid var(--rule)}
.row{display:flex;align-items:center;gap:0;border-bottom:1px solid var(--rule);
     cursor:pointer;background:none;border-left:0;border-right:0;border-top:0;
     width:100%;text-align:left;color:inherit;font:inherit;padding:0}
.row:hover,.row:focus-visible{background:var(--panel)}
.row:focus-visible{outline:2px solid var(--gate);outline-offset:-2px}
.gutter{width:300px;flex:none;padding:9px 12px 9px 0;display:flex;
        align-items:baseline;gap:8px;overflow:hidden}
.kind{color:var(--muted);font-size:10px;letter-spacing:.12em;flex:none;
      border:1px solid var(--rule);padding:1px 5px}
.name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.track{flex:1;position:relative;height:34px}
.bar{position:absolute;top:11px;height:12px;background:var(--ok);min-width:3px}
.bar.error{background:var(--err)}
.bar.rejected{background:var(--gate)}
.ms{position:absolute;top:9px;color:var(--muted);font-size:11px;
    white-space:nowrap;padding-left:8px}
.gateflag{border-left:3px solid var(--gate)}
.gateflag .name{color:var(--gate)}

/* ---- 详情 ---- */
.detail{display:none;background:var(--panel-2);border-bottom:1px solid var(--rule);
        padding:14px 20px 18px 300px}
.detail.open{display:block}
.detail dl{display:grid;grid-template-columns:auto 1fr;gap:4px 18px;margin:0}
.detail dt{color:var(--muted);white-space:nowrap}
.detail dd{margin:0;overflow-wrap:anywhere}
.detail pre{background:var(--board);border:1px solid var(--rule);padding:10px 12px;
            margin:10px 0 0;overflow-x:auto;font-size:12px;color:var(--ink)}
.detail .err{color:var(--err);font-family:var(--sans);margin-top:10px}
.legend{display:flex;gap:20px;flex-wrap:wrap;color:var(--muted);font-size:11px;
        margin:16px 0 26px}
.swatch{display:inline-block;width:22px;height:8px;margin-right:7px;vertical-align:1px}
.note{font-family:var(--sans);color:var(--muted);border-left:2px solid var(--gate);
      padding:8px 0 8px 14px;margin:22px 0 0;max-width:78ch}
.empty{font-family:var(--sans);color:var(--muted);padding:40px 0}
h2{font-size:12px;letter-spacing:.22em;color:var(--muted);font-weight:500;
   text-transform:uppercase;margin:32px 0 12px}
@media (max-width:720px){
  .gutter{width:150px}.axis{padding-left:150px}.detail{padding-left:20px}
}
@media (prefers-reduced-motion:no-preference){
  .bar{animation:grow .32s ease-out both}
  @keyframes grow{from{transform:scaleX(.02);transform-origin:left}to{transform:scaleX(1)}}
}
</style>
<div class="wrap">
<header>
  <div class="eyebrow">TravelWise · 调用链</div>
  <h1>__MODE__ <span class="id">__TRACE_ID__</span></h1>
  <div class="strip">__STRIP__</div>
  __REQUEST__
</header>

<h2>时间轴</h2>
<div class="axis"><span>0 ms</span><span>__TOTAL__ ms</span></div>
__ROWS__

<div class="legend">
  <span><i class="swatch" style="background:var(--ok)"></i>正常</span>
  <span><i class="swatch" style="background:var(--err)"></i>失败：外部或内部真的坏了</span>
  <span><i class="swatch" style="background:var(--gate)"></i>被挡下：参数不合法 / 等人工确认</span>
</div>
__NOTES__
</div>
<script>
document.querySelectorAll('.row').forEach(function(row){
  row.addEventListener('click', function(){
    var d = document.getElementById('d-' + row.dataset.span);
    if (!d) return;
    var open = d.classList.toggle('open');
    row.setAttribute('aria-expanded', open ? 'true' : 'false');
  });
});
</script>
</html>
"""


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _fmt_ms(ms: float) -> str:
    if ms >= 1000:
        return "%.2f s" % (ms / 1000.0)
    if ms >= 10:
        return "%.0f ms" % ms
    return "%.2f ms" % ms


def render_strip(summary: dict, spans: list) -> str:
    errors = sum(1 for s in spans if s.status == "error")
    gated = sum(1 for s in spans if s.status == "rejected")
    cells = [
        ("全程", _fmt_ms(summary.get("wall_ms") or 0.0), False),
        ("SPAN", str(summary.get("spans") or len(spans)), False),
        ("模型调用", str(summary.get("llm_calls", 0)), False),
        ("工具调用", str(summary.get("tool_calls", 0)), False),
        ("失败", str(errors), errors > 0),
        ("被挡下", str(gated), False),
        ("TOKEN", "{:,}".format(summary.get("total_tokens", 0)), False),
        ("成本", summary.get("cost_text") or "—", False),
    ]
    return "".join(
        '<div class="cell%s"><b>%s</b><span>%s</span></div>'
        % (" bad" if bad else "", esc(v), esc(k)) for k, v, bad in cells)


def render_rows(rows: list[dict]) -> str:
    out = []
    for row in rows:
        s = row["span"]
        gate = s.name.startswith("hitl.") or s.attributes.get("awaiting_approval")
        indent = "　" * row["depth"]
        bar_cls = {"error": " error", "rejected": " rejected"}.get(s.status, "")
        out.append(
            '<button class="row%s" data-span="%s" aria-expanded="false">'
            '<span class="gutter"><span class="kind">%s</span>'
            '<span class="name">%s%s</span></span>'
            '<span class="track">'
            '<span class="bar%s" style="left:%.3f%%;width:%.3f%%"></span>'
            '<span class="ms" style="left:%.3f%%">%s</span>'
            '</span></button>'
            % (" gateflag" if gate else "", esc(s.span_id), esc(s.kind),
               indent, esc(s.name), bar_cls,
               row["offset_pct"], row["width_pct"],
               min(88.0, row["offset_pct"] + row["width_pct"]),
               esc(_fmt_ms(s.duration_ms))))
        out.append(render_detail(s))
    return "\n".join(out)


def render_detail(span) -> str:
    items = [("状态", span.status), ("开始", span.timestamp)]
    if span.model:
        items.append(("模型", span.model))
    if span.input_tokens or span.output_tokens:
        items.append(("Token", "输入 %d ／ 输出 %d" % (span.input_tokens,
                                                  span.output_tokens)))
        cost = span.cost()
        items.append(("成本", cost.text()))
    items.append(("span", "%s ← %s" % (span.span_id, span.parent_id or "根")))

    body = "".join("<dt>%s</dt><dd>%s</dd>" % (esc(k), esc(v)) for k, v in items)
    extra = ""
    if span.error:
        extra += '<div class="err">%s</div>' % esc(span.error)
    if span.arguments is not None:
        extra += "<pre>%s</pre>" % esc(
            json.dumps(span.arguments, ensure_ascii=False, indent=2))
    if span.attributes:
        extra += "<pre>%s</pre>" % esc(
            json.dumps(span.attributes, ensure_ascii=False, indent=2))
    return ('<div class="detail" id="d-%s"><dl>%s</dl>%s</div>'
            % (esc(span.span_id), body, extra))


def render(meta: dict, spans: list, summary: dict) -> str:
    rows, total, coarse = layout(build_rows(spans))

    notes = []
    if coarse:
        notes.append(
            "本次全程 %s，而 span 起始时间的精度是 1 毫秒——条形的<b>长度</b>可信，"
            "<b>先后位置</b>不可信（离线回放整轮跑完还不到一毫秒，"
            "所有条形都会挤在最左边）。接真实模型时这条提示不会出现。"
            % _fmt_ms(total))
    if not summary:
        notes.append(
            "这份 trace 没有结尾的汇总行，通常意味着进程是被中断的（Ctrl-C 或崩溃）。"
            "上面的数字由已写入的 span 现算，可能不完整——但把残缺的部分显示出来，"
            "总好过整份打不开。")
    note_html = "".join('<p class="note">%s</p>' % n for n in notes)

    request = meta.get("request") or ""
    request_html = ('<p class="req">%s</p>' % esc(request)) if request else ""

    body = render_rows(rows) if rows else (
        '<p class="empty">这份 trace 里没有 span。'
        '要记录调用链，跑命令时加上 <code>--trace</code>。</p>')

    return (TEMPLATE
            .replace("__TITLE__", esc("trace %s" % (meta.get("trace_id") or "")))
            .replace("__MODE__", esc(meta.get("mode") or "trace"))
            .replace("__TRACE_ID__", esc(meta.get("trace_id")
                                         or summary.get("trace_id") or ""))
            .replace("__STRIP__", render_strip(summary, spans))
            .replace("__REQUEST__", request_html)
            .replace("__TOTAL__", esc("%.0f" % total))
            .replace("__ROWS__", body)
            .replace("__NOTES__", note_html))


# ---------------------------------------------------------------- 入口

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="把一份 trace 渲染成一页 HTML")
    ap.add_argument("path", nargs="?", help="trace 的 .jsonl 路径")
    ap.add_argument("--latest", action="store_true", help="用最近的一份")
    ap.add_argument("--list", action="store_true", dest="do_list")
    ap.add_argument("--out", help="HTML 输出路径，默认与 trace 同名同目录")
    ap.add_argument("--open", action="store_true", dest="do_open",
                    help="渲染完直接在浏览器里打开")
    args = ap.parse_args(argv)

    directory = default_dir()

    if args.do_list:
        found = list_traces(directory)
        if not found:
            print("%s 下还没有 trace。跑命令时加 --trace 就会生成。" % directory)
            return 1
        print("%s：" % directory)
        for p in found[:20]:
            when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M")
            print("  %s  %6.1f KB  %s" % (when, p.stat().st_size / 1024, p.name))
        return 0

    if args.path:
        path = Path(args.path)
    elif args.latest:
        found = list_traces(directory)
        if not found:
            print("%s 下还没有 trace。跑命令时加 --trace 就会生成。" % directory)
            return 1
        path = found[0]
    else:
        ap.print_help()
        return 1

    if not path.is_file():
        print("找不到 %s" % path)
        return 1

    meta, spans, summary = load_trace(path)
    out = Path(args.out) if args.out else path.with_suffix(".html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(meta, spans, summary), encoding="utf-8")

    print("已渲染 %d 个 span → %s" % (len(spans), out))
    if not summary:
        print("（这份 trace 没有汇总行，多半是被中断的；残缺部分照常显示）")
    if args.do_open:
        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
