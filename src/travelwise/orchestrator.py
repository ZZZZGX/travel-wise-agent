# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""orchestrator.py —— 总控：路由 → 调度技能 → 合并结果 → 副作用确认。

三条硬规则贯穿本文件：
  1. 缺参数就问，不猜（missing 非空则直接返回追问）。
  2. 一个技能失败不拖垮另一个（机票挂了照样给目的地清单）。
  3. 有副作用的操作必须先预览、后确认——approval 回调返回 False 就不执行。
"""

from __future__ import annotations

from datetime import date, datetime

from .providers.base import FlightProvider, ReminderProvider, ReminderRequest, ReminderResult
from .providers.reminders import ConsoleReminderProvider
from .router import extract_month_hint, route
from .skills.destination import DestinationSkill
from .skills.flight import FlightSkill
from .state import TaskStatus, TravelState
from .tracing import STATUS_ERROR, Tracer

_FIELD_PROMPTS = {
    "origin": "出发城市",
    "destination": "目的地城市",
    "travel_date": "出行日期",
    "place": "想去玩的地方",
}


class TravelWiseAgent:
    """TravelWise 总控。

    approval_callback(preview_text) -> bool
        副作用操作的人工闸门。默认 None = 不自动执行任何副作用操作，
        只把待确认动作放进 state.pending_action 交给调用方处理。
    """

    def __init__(self, flight_provider: FlightProvider,
                 reminder_provider: ReminderProvider | None = None,
                 approval_callback=None, today: date | None = None,
                 router=None, matrix_days: int = 0,
                 request_interval: float = 0.0,
                 destination_skill: DestinationSkill | None = None,
                 tracer: Tracer | None = None):
        self.flight_skill = FlightSkill(flight_provider)
        # 二层发现要一个网页搜索源，而它是可选的：外部造好传进来，
        # 不传就是只有名录轨 + 一层关键词入口（默认行为，与以前一致）。
        self.destination_skill = destination_skill or DestinationSkill()
        self.reminder_provider = reminder_provider or ConsoleReminderProvider()
        self.approval_callback = approval_callback
        self.today = today or date.today()
        # router=None 时沿用模块级的规则路由函数，行为与 Phase 0 完全一致。
        # 传入 Router 实例（如 LLMRouter）即可替换，其余逻辑一行不变。
        self.router = router
        #: >0 时机票步骤输出「每航班 × 每出发日」价格矩阵（每天消耗 1 次额度）
        self.matrix_days = matrix_days
        #: 串行请求间隔秒数，防付费接口 QPS 限流
        self.request_interval = request_interval
        #: 缺省是 enabled=False 的空转 tracer：不开 trace 时不建目录、不写文件、
        #: 不多花一次 uuid。可观测性不该让「没开它的人」付钱。
        self.tracer = tracer or Tracer(enabled=False)

    # ------------------------------------------------------------------
    def handle(self, user_request: str, want_reminder: bool = False) -> TravelState:
        with self.tracer.span("agent.handle", kind="agent",
                              arguments={"request": user_request},
                              attributes={"want_reminder": want_reminder}) as root:
            state = self._route(user_request)
            if self.tracer.enabled:
                # 只在真的有 trace 时才记 id。没开 trace 却留一个 id，
                # 会让后来查问题的人拿着它去找一份根本不存在的文件。
                state.trace_id = self.tracer.trace_id

            if state.current_step == TaskStatus.OUT_OF_SCOPE:
                state.warnings.append(
                    "这条请求看起来不属于机票或目的地范围。TravelWise 目前只做这两件事，"
                    "火车票 / 酒店 / 签证 / 打车等暂不支持。")
                root.attributes["step"] = state.current_step.value
                return state

            out = self.dispatch(state, want_reminder=want_reminder)
            root.attributes["step"] = out.current_step.value
            return out

    def _route(self, user_request: str) -> TravelState:
        """路由单独成一个 span。

        它值得独立一格，是因为「答错了」和「压根没听懂」是两种完全不同的
        故障：前者要去看技能，后者要去看路由。混在一个 span 里，
        看 trace 的人得靠猜。LLM 路由时把 token 记进去，
        这样一次请求的账单里，**路由那部分花了多少**是看得见的。
        """
        is_llm = self.router is not None
        with self.tracer.span(
                "route", kind="llm" if is_llm else "agent",
                model=(getattr(self.router, "model", "")
                       or getattr(getattr(self.router, "client", None), "model", "")
                       or "") if is_llm else "",
                attributes={"router": getattr(self.router, "name", "rule")}) as sp:
            if is_llm:
                outcome = self.router.route(user_request, self.today)
                state = outcome.state
                if outcome.error:
                    state.warnings.append("路由提示：%s" % outcome.error)
                    sp.attributes["router_error"] = outcome.error
                sp.input_tokens = outcome.usage.input_tokens
                sp.output_tokens = outcome.usage.output_tokens
                # 降级过就必须在 trace 上看得见。一次「悄悄退回规则路由」
                # 的运行和一次真正的 LLM 路由，结果可能一模一样，
                # 但它们对下一步决策的意义完全不同。
                sp.attributes["fell_back"] = bool(outcome.fell_back)
            else:
                state = route(user_request, today=self.today)
            sp.attributes["intents"] = list(state.intents)
            sp.attributes["missing"] = list(state.missing)
            return state

    # ------------------------------------------------------------------
    def dispatch(self, state: TravelState, want_reminder: bool = False) -> TravelState:
        """执行阶段的 span 外壳。真正的逻辑在 `_dispatch`。

        这一层**不因为 `errors` 非空就标红**，而是记 `partial=True`。
        理由是本项目最看重的那条行为——「一个技能失败不拖垮另一个」——
        如果在 trace 上和「整轮全崩」长得一模一样，那这条行为就等于没被观测到。
        真正失败的那个技能，红在它自己的 span 上，位置更准。
        """
        with self.tracer.span(
                "dispatch", kind="agent",
                attributes={"intents": list(state.intents),
                            "missing": list(state.missing)}) as sp:
            out = self._dispatch(state, want_reminder=want_reminder)
            delivered = [k for k in ("flight", "destination")
                         if (getattr(out, k + "_result", None) or {}).get("ok")]
            sp.attributes.update({"step": out.current_step.value,
                                  "delivered": delivered,
                                  "errors": len(out.errors),
                                  "partial": bool(out.errors and delivered)})
            return out

    def _dispatch(self, state: TravelState, want_reminder: bool = False) -> TravelState:
        """执行阶段：判断哪个技能齐活了，跑它们，收尾。

        从 `handle()` 里抽出来是这次多轮改造的**核心动作**。原先执行逻辑
        长在 `handle()` 内部，于是「续跑」只能另写一份——两份执行路径迟早
        分叉，而分叉出来的 bug 长这样：单轮一次说全了没问题，分两轮说
        就少跑了一个技能。抽出来之后，首轮和续跑调的是同一个函数，
        不可能分叉。

        本方法**不路由**，只执行。传进来的 state 必须已经有 intents。
        """
        # 缺参数只挡住【对应的那个技能】，不连坐另一个。
        # 例：「飞乌鲁木齐，新疆有什么玩的」缺出发地 → 追问机票出发地的同时，
        # 目的地清单照常交付，而不是整轮什么都不给。
        flight_ready = "flight" in state.intents and not (
            {"origin", "destination", "travel_date"} & set(state.missing))
        dest_ready = "destination" in state.intents and "place" not in state.missing

        # 已经跑成功过的技能不再重跑。多轮下这点很要紧：
        # 「飞乌鲁木齐，新疆有什么玩的」缺出发地时目的地那节已经交付了，
        # 用户补上「上海」之后若不拦一道，景区清单会被原样再查一遍再打印一遍。
        if state.flight_result and state.flight_result.get("ok"):
            flight_ready = False
        if state.destination_result and state.destination_result.get("ok"):
            dest_ready = False

        if state.needs_clarification():
            fields = "、".join(_FIELD_PROMPTS.get(m, m) for m in state.missing)
            state.warnings.append("还需要你补充：%s。（信息不全时不作假设，先问清楚）" % fields)
            if not flight_ready and not dest_ready:
                state.current_step = TaskStatus.AWAITING_INPUT
                return state

        # -- 调度技能：先机票、再目的地 --
        state.current_step = TaskStatus.EXECUTING

        if flight_ready:
            with self.tracer.span(
                    "skill.flight", kind="skill", tool="flight",
                    arguments={"origin": state.origin,
                               "destination": state.destination,
                               "travel_date": state.travel_date,
                               "matrix_days": self.matrix_days}) as sp:
                res = self.flight_skill.run(
                    state.origin, state.destination, state.travel_date, today=self.today,
                    matrix_days=self.matrix_days,
                    sleep_between=self.request_interval)
                state.flight_result = res
                if not res["ok"]:
                    # 失败如实记录，但不 return——目的地那节还要照常交付
                    sp.status = STATUS_ERROR
                    sp.error = str(res.get("error") or "未知错误")
                    state.errors.append("机票模块：%s" % (res.get("error") or "未知错误"))

        if dest_ready:
            month = extract_month_hint(state.user_request)
            if month is None and state.travel_date:
                month = int(state.travel_date.split("-")[1])
            with self.tracer.span(
                    "skill.destination", kind="skill", tool="destination",
                    arguments={"place": state.place,
                               "scope": state.scope or "city",
                               "travel_month": month}) as sp:
                res = self.destination_skill.run(
                    state.place, scope=state.scope or "city", travel_month=month)
                state.destination_result = res
                if not res["ok"]:
                    sp.status = STATUS_ERROR
                    sp.error = str(res.get("error") or "未知错误")
                    state.errors.append("目的地模块：%s" % (res.get("error") or "未知错误"))
                elif res.get("notice"):
                    sp.attributes["notice"] = res["notice"]
                    state.warnings.append(res["notice"])

        # -- 副作用：购票提醒 --
        # `pending_action` 已存在说明上一轮已经预览过了，别再造一份：
        # 重复预览会让用户看到两张确认卡，也会把已批准的那次覆盖掉。
        if want_reminder and state.flight_result and not state.pending_action:
            self._handle_reminder(state)

        state.current_step = self._final_status(state)
        return state

    # ------------------------------------------------------------------
    def resume(self, state: TravelState, want_reminder: bool = False) -> TravelState:
        """槽位补齐之后，接着上次的地方往下跑。

        与 `dispatch()` 的差别只有两件事，但都不能省：

        1. **先清掉上一轮的追问。** 那句「还需要你补充：出发城市」是
           上一轮的话，用户已经答了。留着它，用户会看到自己刚回答过的
           问题又被问了一遍——足以让人以为回答没被收到。
           执行期产生的 `errors` 则**保留**：一次真实的接口失败不因为
           用户补了个参数就不算数了。
        2. **重算 missing。** 见 `TravelState.recompute_missing()`。
        """
        with self.tracer.span("resume", kind="agent",
                              attributes={"missing_before": list(state.missing)}):
            state.warnings = []
            state.recompute_missing()
            return self.dispatch(state, want_reminder=want_reminder)

    # ------------------------------------------------------------------
    def approve(self, state: TravelState, approved: bool) -> TravelState:
        """人工闸门的 span 外壳。真正的逻辑在 `_approve`。

        闸门是本项目最该被看见的一处：一次运行到底有没有真的写出去、
        是谁点的头、还是根本没人点——这三件事在最终回答里都看不出来。
        """
        with self.tracer.span("hitl.approve", kind="agent",
                              attributes={"answered": bool(approved)}) as sp:
            out = self._approve(state, approved)
            pa = out.pending_action or {}
            sp.attributes.update({
                "executed": "result" in pa,
                "ok": (pa.get("result") or {}).get("ok"),
                "step": out.current_step.value})
            return out

    def _approve(self, state: TravelState, approved: bool) -> TravelState:
        """用户对待确认操作的答复。这是 HITL 闸门在多轮下的落点。

        `approved=False` 时**不执行**，并把决定记进 `pending_action`，
        这样回放会话时能看到「用户拒绝过」，而不是只看到一个没结果的预览。
        """
        pa = state.pending_action
        if not pa:
            state.warnings.append("当前没有待确认的操作。")
            state.current_step = self._final_status(state)
            return state
        if "result" in pa:
            # 已经执行过了。重复确认不该再写一次——副作用操作的幂等性
            # 必须在这一层保证，不能指望 provider 端去重。
            state.warnings.append("这条提醒已经创建过了，未重复创建。")
            state.current_step = self._final_status(state)
            return state

        if not approved:
            pa["approved"] = False
            state.warnings.append("已按你的意思取消，未创建提醒。")
            state.current_step = self._final_status(state)
            return state

        request = self._rebuild_reminder(pa)
        if request is None:
            pa["approved"] = False
            state.errors.append(
                "待确认的提醒内容已损坏（无法还原时间或标题），未创建。")
            state.current_step = self._final_status(state)
            return state

        pa["approved"] = True
        result = self.execute_reminder(request)
        pa["result"] = {"ok": result.ok, "provider": result.provider,
                        "message": result.message, "location": result.location}
        if not result.ok:
            state.errors.append("提醒创建失败（%s）：%s" % (result.provider, result.message))
        state.current_step = self._final_status(state)
        return state

    @staticmethod
    def _rebuild_reminder(pending: dict) -> ReminderRequest | None:
        """从落盘过的 pending_action 还原 ReminderRequest。

        必须能从**纯 JSON** 还原，因为确认往往发生在另一个进程里：
        用户上午跑了查询，下午重开终端 `--session <id>` 再回「确认」。
        内存里的那个 dataclass 早没了。
        """
        raw = (pending or {}).get("request") or {}
        title = raw.get("title")
        at = raw.get("remind_at")
        if not title or not at:
            return None
        try:
            remind_at = datetime.fromisoformat(at)
        except (TypeError, ValueError):
            return None
        return ReminderRequest(title=title, remind_at=remind_at,
                               note=raw.get("note") or "")

    # ------------------------------------------------------------------
    @staticmethod
    def _final_status(state: TravelState) -> TaskStatus:
        """区分四种收尾处境——原先它们都被笼统记成 done，外部无法据此决定下一步。"""
        # 提醒还等着用户点确认 → 任务没结束
        if state.pending_action and "result" not in state.pending_action \
                and state.pending_action.get("approved") is not False:
            return TaskStatus.AWAITING_APPROVAL

        produced = bool(
            (state.flight_result and state.flight_result.get("ok"))
            or (state.destination_result and state.destination_result.get("ok")))

        # 什么都没产出且有错 → 彻底失败
        if not produced and state.errors:
            return TaskStatus.FAILED
        # 还缺参数，或有一部分失败了 → 部分完成
        if state.missing or state.errors:
            return TaskStatus.PARTIAL_COMPLETE
        return TaskStatus.COMPLETED

    # ------------------------------------------------------------------
    def _handle_reminder(self, state: TravelState) -> None:
        request = FlightSkill.build_reminder(state.flight_result)
        if request is None:
            state.warnings.append(
                "当前没有可用的价格分析结论，因此无法推算建议购票日，未创建提醒。")
            return

        preview = self.preview_reminder(request)
        state.pending_action = {
            "type": "create_reminder",
            "preview": preview,
            "provider": self.reminder_provider.name,
            "request": {"title": request.title,
                        "remind_at": request.remind_at.isoformat(timespec="minutes"),
                        "note": request.note},
        }

        self.tracer.event("hitl.awaiting_approval", kind="agent",
                          attributes={"action": "create_reminder",
                                      "provider": self.reminder_provider.name,
                                      "auto_approve": self.approval_callback is not None})

        # 没有审批回调 = 不执行。副作用绝不默认自动发生。
        if self.approval_callback is None:
            state.warnings.append("提醒尚未创建：等待用户确认（见 pending_action）。")
            return

        if not self.approval_callback(preview):
            state.pending_action["approved"] = False
            state.warnings.append("用户未确认，提醒未创建。")
            return

        state.pending_action["approved"] = True
        result = self.execute_reminder(request)
        state.pending_action["result"] = {
            "ok": result.ok, "provider": result.provider,
            "message": result.message, "location": result.location,
        }
        if not result.ok:
            # 失败必须显式冒泡，绝不静默当作成功
            state.errors.append("提醒创建失败（%s）：%s" % (result.provider, result.message))

    # ------------------------------------------------------------------
    @staticmethod
    def preview_reminder(request: ReminderRequest) -> str:
        return ("准备创建提醒，请确认：\n"
                "  标题：%s\n"
                "  时间：%s\n"
                "  备注：%s\n"
                "确认后才会写入。" % (request.title,
                                request.remind_at.strftime("%Y-%m-%d %H:%M"),
                                request.note or "-"))

    def execute_reminder(self, request: ReminderRequest) -> ReminderResult:
        """真正写入的 span 外壳。

        埋在这一层而不是各调用点，是因为它有两个入口（首轮自动确认、
        多轮 approve）。埋在调用点上，迟早有第三个入口忘了埋——
        而漏掉的恰恰是「副作用到底发生没发生」这条最要紧的记录。
        """
        with self.tracer.span(
                "reminder.create", kind="tool", tool="create_reminder",
                arguments={"title": request.title,
                           "remind_at": request.remind_at.isoformat(timespec="minutes")}
                ) as sp:
            result = self._execute_reminder(request)
            sp.attributes.update({
                "provider": result.provider,
                # 降级必须看得见：控制台输出和真的写进日历，
                # 在 ok=True 这一点上完全一样，但对用户的意义天差地别。
                "fell_back": result.provider != self.reminder_provider.name})
            if not result.ok:
                sp.status = STATUS_ERROR
                sp.error = result.message
            return result

    def _execute_reminder(self, request: ReminderRequest) -> ReminderResult:
        """降级逻辑：首选不可用则退到控制台，并在 message 里说明。"""
        provider = self.reminder_provider
        if not provider.available():
            fallback = ConsoleReminderProvider()
            result = fallback.create(request)
            result.message = ("首选提醒方式「%s」在当前环境不可用，已降级为控制台输出。%s"
                              % (provider.name, result.message))
            return result
        return provider.create(request)

    # ------------------------------------------------------------------
    @staticmethod
    def render(state: TravelState) -> str:
        """把 state 渲染成最终回复。各节警告分开保留，不互相吞掉。"""
        blocks: list[str] = []

        if state.flight_result:
            blocks.append("① 购票时机\n" + "-" * 40 + "\n" + state.flight_result["text"])

        if state.destination_result and state.destination_result.get("text"):
            blocks.append("② 目的地推荐\n" + "-" * 40 + "\n" + state.destination_result["text"])

        if state.pending_action:
            pa = state.pending_action
            note = pa["preview"]
            if "result" in pa:
                r = pa["result"]
                note += "\n\n执行结果：%s（%s）%s" % (
                    "✅ 成功" if r["ok"] else "❌ 失败", r["provider"],
                    "\n位置：" + r["location"] if r.get("location") else "")
                note += "\n" + r["message"]
            elif pa.get("approved") is False:
                note += "\n\n执行结果：已取消，未创建。"
            blocks.append("③ 提醒\n" + "-" * 40 + "\n" + note)

        if state.carried_over:
            # 沿用必须看得见。用户没在这一轮说过「上海」，系统却按上海查了，
            # 若不明说，他只会看到一个莫名其妙的结果，且无从判断哪里错了。
            label = {"origin": "出发地", "destination": "目的地",
                     "travel_date": "出行日期", "place": "游玩地", "scope": "范围"}
            items = "、".join(
                "%s=%s" % (label.get(k, k), getattr(state, k))
                for k in state.carried_over if getattr(state, k, None))
            if items:
                blocks.append("↻ 沿用上一轮\n" + "-" * 40 + "\n"
                              + items + "\n（这几项你这轮没提，是从上次带过来的；不对就直接说。）")

        if state.warnings:
            blocks.append("⚠️ 提示\n" + "\n".join("- " + w for w in state.warnings))
        if state.errors:
            blocks.append("❌ 失败（如实报告，未做任何推测填补）\n"
                          + "\n".join("- " + e for e in state.errors))

        return "\n\n".join(blocks) if blocks else "（没有可输出的内容）"
