#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app.py —— TravelWise 的网页交互入口（Gradio，对话式）。

    pip install gradio
    python app.py            # 本地 http://127.0.0.1:7860

放在项目**根目录**（和 README.md 同级）。必须在根目录下跑 ——
`.env`、`config/`、`data/source/` 都是相对当前目录找的。

## 这个文件在做什么

它不是重写 Agent，而是给现有 CLI 套一层可点击的壳：
subprocess 调 `python -m travelwise --json`，把结果渲染成网页。

为什么走 subprocess 而不是 import orchestrator：

  1. 网页层不依赖任何内部 API，你改 orchestrator 签名它不用动
  2. **网页看到的和命令行看到的是同一份输出** —— 不会出现
     「demo 里好好的，面试官照 README 敲一遍却不一样」
  3. 子进程崩了不会把网页拖下水

代价是每轮多一次进程启动（约 0.3 秒），对演示用途可以忽略。

## 多轮怎么体现

演示时的标准动作：

    第一句：我想去新疆玩            → 它追问出发地和日期
    第二句：8月29号从大连出发        → 从上一轮状态接着走，不用重说

第二句故意只给一半信息，就是为了让「它没让我重来」被看见。
对话历史留在页面上，右侧轨迹与代价跟着当前轮更新。

## 关于 Gradio 版本

Gradio 6 改了一批参数（`Chatbot` 去掉了 `type`，`theme` 从 Blocks
移到了 launch）。与其锁死某个版本，这里用 `_component()` 做兼容：
构造组件时撞上「不认识的参数」就丢掉它重试。5 和 6 都能跑。

同理，早退分支不用 `gr.update()`，而是把当前的轨迹/代价当输入传进来、
原样传回去 —— 少依赖一个可能变的 API。

## 你需要确认的一件事

CLI 的 `--json` 到底吐什么。先在命令行跑一次：

    python -m travelwise --json --provider mock --days 3 "8月29号从大连飞郑州"

把顶层键名对照 `render_result()` 里的取值逻辑。我按 `ok` / `text` /
`skills` / `trace` / `usage` 做了兼容，对不上的话只改这一个函数就够。

## 公开分享前必须处理的三件事

**钱**：航班接口按次计费，7 天扫描约 ¥1.4。公开链接被脚本刷是无底洞。
所以真实模式默认关闭，且可以用 `TRAVELWISE_DEMO_PASSCODE` 加口令 ——
面试时当场输口令跑一次真实的，效果比一直开着更好。

**Key**：不要把 `.env` 传上去。Hugging Face Space 用 Settings → Secrets
配环境变量，代码两边都从 `os.environ` 读。

**数据**：`data/source/` 跟着上线就是**公开分发**了。NOTICE 里那句
「再分发前应自行确认可分发性」，说的就是这一刻。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

import gradio as gr

ROOT = Path(__file__).resolve().parent

#: 真实模式口令。留空 = 任何人都能触发付费调用（别这么干）。
REAL_MODE_PASSCODE = os.environ.get("TRAVELWISE_DEMO_PASSCODE", "")

#: 单轮超时上限，防止网页挂死
TIMEOUT_SECONDS = 180

EXAMPLES = [
    "我想去新疆玩",
    "8月29号从大连飞郑州，机票什么时候买划算",
    "别给我查机票，我就想知道成都有什么好玩的",
    "从上海飞成都的高铁票什么时候买划算",
]

INTRO = (
    "帮你决定**什么时候买票**、**去哪儿玩**。不订票、不接支付、不返佣。\n\n"
    "试试先说半句「我想去新疆玩」，看它追问什么；"
    "再补一句「8月29号从大连出发」，它会接着上一轮往下算，不用重说一遍。"
)

_UNEXPECTED = re.compile(r"unexpected keyword argument '([^']+)'")


def _component(factory, **kwargs):
    """构造 Gradio 组件，自动丢弃当前版本不认识的参数。

    Gradio 大版本之间会增删参数（6.0 就砍掉了 Chatbot 的 `type`）。
    与其把版本锁死，不如让不认识的参数静默降级 —— 这些参数全是外观项，
    掉了不影响功能。真正的错误（比如参数值非法）仍然会照常抛出。
    """
    kwargs = dict(kwargs)
    while True:
        try:
            return factory(**kwargs)
        except TypeError as exc:
            match = _UNEXPECTED.search(str(exc))
            if not match or match.group(1) not in kwargs:
                raise
            kwargs.pop(match.group(1))


def run_cli(request: str, session_id: str, router: str, days: int,
            real_data: bool) -> tuple[dict, str, float]:
    """调一次 CLI，返回 (解析后的 JSON, stderr, 耗时秒)。"""
    # 注意：这里**不传 `--json`**。带 `--session` 时 CLI 走的是会话路径，
    # 直接 print 给人看的排版文本，`--json` 不会被处理。而多轮续跑必须靠
    # `--session`，所以这条路上拿不到 JSON —— 接受文本反而更好，
    # CLI 的排版比网页现拼的表格清楚。
    cmd = [
        sys.executable, "-m", "travelwise",
        "--trace",
        "--session", session_id,
        "--router", router,
        "--days", str(days),
    ]
    if not real_data:
        cmd += ["--provider", "mock"]
    cmd.append(request)

    env = dict(os.environ)
    env.setdefault("PYTHONPATH", str(ROOT / "src"))
    # Windows 上 stdout 被管道接走时，Python 用系统区域编码（cp936）而不是
    # UTF-8。输出里的中文一编码就抛 UnicodeEncodeError，结果 stdout 一个字
    # 都没有、报错全进了 stderr —— 在控制台里直接跑却完全正常，因为那时
    # stdout 不是管道。这两行把子进程钉死在 UTF-8 上。
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    started = time.time()
    proc = subprocess.run(
        cmd, cwd=str(ROOT), env=env, capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=TIMEOUT_SECONDS,
    )
    elapsed = time.time() - started

    stdout = (proc.stdout or "").strip()
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # 会话模式下 CLI 输出的是给人看的排版文本，不是 JSON。
        # **这不是错误。** 只有 stdout 完全为空才说明它崩了。
        data = {"ok": bool(stdout), "text": stdout or "（没有输出）",
                "_plain": True, "_parse_failed": not stdout}
    return data, (proc.stderr or "").strip(), elapsed


#: `--trace` 的报告段以哪一行开头。CLI 改了标题就在这里加一条。
TRACE_HEADINGS = ("执行轨迹", "调用轨迹", "追踪", "trace", "Trace", "TRACE")


def split_trace(text: str) -> tuple[str, str]:
    """把 `--trace` 的报告段从正文里切出来。切不出来就整段当正文。"""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        stripped = line.strip().lstrip("#=-—①②③④⑤ ")
        if any(stripped.startswith(h) for h in TRACE_HEADINGS):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:]).strip()
    return text, ""


def render_result(data: dict, stderr: str, elapsed: float,
                  real_data: bool) -> tuple[str, str, str]:
    """渲染成三块：对话气泡内容、执行轨迹、这一轮的代价。"""

    # --- 气泡内容 -----------------------------------------------------
    if data.get("_parse_failed"):
        # 解析失败时最有用的是 stderr，不是空的 stdout。直接放进气泡，
        # 别让人去侧栏翻 —— 一个要靠翻找才能看到的错误信息等于没有。
        reply = ("**这一轮没有产出结论。** 命令行没有返回可解析的 JSON。\n\n"
                 "stdout：\n\n```\n%s\n```" % (data.get("text", "") or "（空）")[:1500])
        if stderr:
            reply += "\n\nstderr：\n\n```\n%s\n```" % stderr[-2500:]
    else:
        reply = data.get("text") or data.get("summary") or ""
        if not reply:
            reply = "```json\n%s\n```" % json.dumps(
                data, ensure_ascii=False, indent=2)[:4000]

    if not real_data:
        reply = ("*演示数据：航班接口未真实调用，价格由 mock provider 生成，"
                 "不能拿来买票。*\n\n") + reply

    # --- 执行轨迹 -----------------------------------------------------
    # 这是整个页面最该被看见的东西：调了什么、哪一步失败了。
    plain_trace = ""
    if data.get("_plain"):
        reply, plain_trace = split_trace(reply)

    trace = data.get("trace") or data.get("skills") or []
    if isinstance(trace, dict):
        trace = [trace]
    if trace:
        rows = ["| 步骤 | 状态 | 说明 |", "|---|---|---|"]
        for i, node in enumerate(trace, 1):
            if not isinstance(node, dict):
                rows.append("| %d | — | %s |" % (i, node))
                continue
            name = (node.get("name") or node.get("skill")
                    or node.get("tool") or "?")
            ok = node.get("ok")
            status = ("失败" if ok is False
                      else "部分完成" if node.get("partial") else "完成")
            note = (node.get("error") or node.get("note")
                    or node.get("detail") or "")
            rows.append("| %d. %s | %s | %s |" % (i, name, status, note))
        trace_md = "\n".join(rows)
    elif plain_trace:
        trace_md = "```\n%s\n```" % plain_trace
    else:
        trace_md = "本轮没有记录到工具调用（可能是纯追问轮，或未输出 trace）。"

    if stderr:
        trace_md += "\n\n**stderr**\n\n```\n%s\n```" % stderr[:1500]

    # --- 代价 ---------------------------------------------------------
    usage = data.get("usage") or data.get("cost") or {}
    rows = ["| 项 | 值 |", "|---|---|",
            "| 端到端耗时 | %.1f 秒 |" % elapsed,
            "| 航班接口 | %s |" % ("真实调用" if real_data else "未调用（mock）")]
    for key, label in (("tokens", "Token"), ("total_tokens", "Token"),
                       ("cny", "折合人民币"), ("calls", "工具调用次数")):
        if key in usage:
            rows.append("| %s | %s |" % (label, usage[key]))
    cost_md = "\n".join(rows)

    return reply, trace_md, cost_md


def respond(request, history, session_id, router, days, real_data, passcode,
            trace_md, cost_md):
    """一轮对话。history 是消息列表，元素形如 {"role": ..., "content": ...}。

    trace_md / cost_md 既是输入也是输出：早退时原样传回，
    这样不需要用 gr.update()。
    """
    history = list(history or [])
    request = (request or "").strip()
    if not request:
        return history, "", session_id, trace_md, cost_md

    history.append({"role": "user", "content": request})

    if real_data and REAL_MODE_PASSCODE and passcode != REAL_MODE_PASSCODE:
        history.append({"role": "assistant", "content":
                        "真实数据模式需要口令。航班接口按次计费，"
                        "14 天扫描约 ¥2.8，所以这里加了一道闸。"})
        return history, "", session_id, trace_md, cost_md

    if not session_id:
        session_id = "web-" + uuid.uuid4().hex[:8]

    try:
        data, stderr, elapsed = run_cli(request, session_id, router,
                                        int(days), bool(real_data))
    except subprocess.TimeoutExpired:
        history.append({"role": "assistant", "content":
                        "超过 %d 秒没有返回，已中止。扫描天数多时容易撞到这里，"
                        "把天数调小再试。" % TIMEOUT_SECONDS})
        return history, "", session_id, trace_md, cost_md
    except Exception as exc:  # noqa: BLE001
        history.append({"role": "assistant",
                        "content": "调用失败：%s" % exc})
        return history, "", session_id, trace_md, cost_md

    reply, new_trace, new_cost = render_result(data, stderr, elapsed, real_data)
    history.append({"role": "assistant", "content": reply})
    return history, "", session_id, new_trace, new_cost


def new_topic():
    """开新话题：清空对话并换一个 session，让状态续跑从头开始。"""
    return [], "", "", "", ""


def build_ui():
    with gr.Blocks(title="TravelWise") as demo:
        gr.Markdown("# TravelWise\n" + INTRO)

        session_state = gr.State("")

        with gr.Row():
            with gr.Column(scale=3):
                chat = _component(gr.Chatbot, type="messages", height=520,
                                  label="对话", show_copy_button=True)
                request = _component(
                    gr.Textbox, label="", show_label=False, lines=2,
                    placeholder="例：8月29号从大连飞郑州，机票什么时候买划算",
                )
                with gr.Row():
                    submit = gr.Button("发送", variant="primary", scale=3)
                    reset = gr.Button("开新话题", scale=1)
                gr.Examples(EXAMPLES, inputs=request, label="试试这些")

            with gr.Column(scale=2):
                router = _component(
                    gr.Radio, choices=["llm", "rule"], value="llm",
                    label="路由方式",
                    info="rule 是零成本基线，认不出「29号」这类表达；"
                         "llm 每轮约 ¥0.0025",
                )
                days = _component(
                    gr.Slider, minimum=3, maximum=21, value=7, step=1,
                    label="扫描天数", info="真实模式下每天约 ¥0.2",
                )
                real_data = _component(
                    gr.Checkbox, value=False, label="使用真实航班数据",
                    info="不勾选则走 mock，不产生任何接口费用",
                )
                passcode = _component(
                    gr.Textbox, label="口令", type="password",
                    visible=bool(REAL_MODE_PASSCODE),
                )
                gr.Markdown("### 执行轨迹")
                trace = gr.Markdown()
                gr.Markdown("### 这一轮的代价")
                cost = gr.Markdown()

        inputs = [request, chat, session_state, router, days,
                  real_data, passcode, trace, cost]
        outputs = [chat, request, session_state, trace, cost]
        submit.click(respond, inputs=inputs, outputs=outputs)
        request.submit(respond, inputs=inputs, outputs=outputs)
        reset.click(new_topic, outputs=[chat, request, session_state,
                                        trace, cost])

        gr.Markdown(
            "---\n"
            "信息不全时它会追问，答完从上次状态接着走。"
            "问范围外的（酒店、火车、签证）它会直接说不在范围内 —— "
            "这是设计，不是故障。工具挂掉时它会说挂了，不会编一个价格出来。"
        )
    return demo


def launch(demo):
    """Gradio 6 的 theme 在 launch 上，Gradio 5 在 Blocks 上。都试一遍。"""
    opts = {"server_name": "0.0.0.0", "server_port": 7860}
    try:
        return demo.launch(theme=gr.themes.Soft(), **opts)
    except TypeError:
        return demo.launch(**opts)


if __name__ == "__main__":
    launch(build_ui())
