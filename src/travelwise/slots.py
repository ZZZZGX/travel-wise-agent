# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""slots.py —— 把用户对追问的回话解析成槽位。

## 为什么不能直接把回话丢回 router

router.route() 是为**完整请求**设计的：「8月28号从上海飞成都」。
而追问的回话通常是残句：

    Agent：还需要你补充：出发城市。
    用户：上海

「上海」两个字进 route() 会被判成 OUT_OF_SCOPE（没有任何意图信号），
于是整轮对话原地打转——这是多轮 Agent 最常见的一种烂尾。

所以这里做的是**有上下文的解析**：已经知道缺的是 origin，
那么一个孤零零的城市名就应当填进 origin。

## 三条不肯让步的规则

1. **歧义就问，不猜。** 出发地和目的地同时缺，用户只回了「上海」——
   这是真歧义。返回 `ambiguous`，由上层追问「上海是出发地还是目的地」，
   而不是按"先缺先得"塞给 origin。塞错的代价是查错航线、烧掉真实额度。

2. **纠错优先于补全。** 「不对，是从虹桥走」既是补充也是修改，
   显式的方向词（从 / 出发 / 飞往 / 到）永远压过位置推断。

3. **认不出就如实说认不出。** 返回空槽位 + `unparsed=True`，
   上层据此重新追问，而不是把用户的话硬塞进某个字段。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .router import (_clean_place, extract_date, extract_month_hint,
                     looks_like_place as _looks_like_place)
from .tools import city_codes

#: 明确指向出发地的说法
_ORIGIN_MARK = re.compile(
    r"(?:从|由|自)\s*([\u4e00-\u9fff]{2,8}?)(?:出发|走|飞|去|到|$)"
    r"|([\u4e00-\u9fff]{2,8})\s*(?:出发|起飞)")
#: 明确指向目的地的说法
_DEST_MARK = re.compile(
    r"(?:去|到|飞往|飞|前往|落地)\s*([\u4e00-\u9fff]{2,8})")
#: 「A 到 B」「A 飞 B」这类一句话给全的
_PAIR = re.compile(
    r"([\u4e00-\u9fff]{2,8}?)\s*(?:到|飞往|飞|去|-|—|→|至)\s*([\u4e00-\u9fff]{2,8})")

#: 肯定 / 否定。用于 AWAITING_APPROVAL 的确认解析。
#: 只认明确表态，含糊的（"看情况""再说吧"）一律当作**没表态**——
#: 副作用操作上，把"嗯…"读成"是"是不可接受的。
AFFIRMATIVE = re.compile(
    r"^(y|yes|ok|okay|好|好的|好吧|行|可以|确认|确定|同意|是|对|嗯嗯|"
    r"创建|建吧|加吧|设吧|就这样|没问题|continue|go)[\s。！!.]*$",
    re.IGNORECASE)
NEGATIVE = re.compile(
    r"^(n|no|不|不用|不要|算了|取消|别|先不|不必|停|cancel|stop|abort)[\s。！!.]*$",
    re.IGNORECASE)

#: 方向词。出现任何一个，就说明用户给了显式线索，
#: 位置推断（第 4 步）必须让位——见 parse_reply 里的说明。
_DIRECTION_CHAR = re.compile(r"(从|由|自|去|到|飞|前往|出发|落地|起飞|返程)")

#: 常见的礼貌前缀 / 语气词，剥掉之后才好判断"这是不是一个光秃秃的地名"
_FILLER = re.compile(
    r"^(哦|嗯|那|就|是|我|想|要|在|从|的|吧|呢|啊|好的?|嗯嗯|"
    r"对了|另外|其实|应该|大概|可能)+|[\s，。！？,.!?~、]+$")


def _strip_filler(text: str) -> str:
    prev = None
    out = (text or "").strip()
    while out != prev:
        prev = out
        out = _FILLER.sub("", out).strip()
    return out


@dataclass
class SlotParse:
    """一次回话的解析结果。"""

    slots: dict[str, Any] = field(default_factory=dict)
    #: 需要用户二选一的歧义项：[(候选值, [可能的字段…])]
    ambiguous: list[tuple] = field(default_factory=list)
    #: 一个槽位都没解析出来
    unparsed: bool = False
    #: 解析依据，进 trace 用，也方便在报告里解释"为什么这么填"
    evidence: list[str] = field(default_factory=list)

    def question(self) -> str:
        """歧义追问句。没有歧义时返回空串。"""
        if not self.ambiguous:
            return ""
        label = {"origin": "出发地", "destination": "目的地", "place": "想玩的地方"}
        value, fields = self.ambiguous[0]
        return ("「%s」我不确定该当成%s——直接说清楚就行，"
                "比如「从%s出发」或「去%s」。"
                % (value, "还是".join(label.get(f, f) for f in fields), value, value))


def parse_reply(reply: str, missing: list[str], today: date | None = None,
                intents: list[str] | None = None) -> SlotParse:
    """在"知道还缺什么"的前提下解析一句回话。

    `missing` 是当前还缺的槽位；解析结果**只填这里面的**，
    不会顺手改掉已经确定的字段——除非用户用了显式方向词（那是纠错，
    见 `parse_correction`）。
    """
    out = SlotParse()
    text = (reply or "").strip()
    if not text:
        out.unparsed = True
        return out

    missing = list(missing or [])
    intents = list(intents or [])
    core = _strip_filler(text)

    # ---- 1. 日期：任何时候都先试，它和地名不会互相抢 ----
    if "travel_date" in missing:
        got = extract_date(text, today)
        if got:
            out.slots["travel_date"] = got
            out.evidence.append("travel_date ← 日期表达「%s」" % text[:16])

    # ---- 2. 一句话给全航线：「上海到成都」 ----
    if {"origin", "destination"} <= set(missing):
        m = _PAIR.search(core)
        if m:
            origin, dest = _clean_place(m.group(1)), _clean_place(m.group(2))
            if origin and dest and origin != dest:
                out.slots["origin"], out.slots["destination"] = origin, dest
                out.evidence.append("origin/destination ← 航线表达「%s→%s」"
                                    % (origin, dest))

    # ---- 3. 显式方向词 ----
    if "origin" not in out.slots and "origin" in missing:
        m = _ORIGIN_MARK.search(text)
        if m:
            value = _clean_place(m.group(1) or m.group(2) or "")
            if value:
                out.slots["origin"] = value
                out.evidence.append("origin ← 出发方向词")
    if "destination" not in out.slots and "destination" in missing:
        m = _DEST_MARK.search(text)
        if m:
            value = _clean_place(m.group(1))
            if value:
                out.slots["destination"] = value
                out.evidence.append("destination ← 到达方向词")

    # ---- 4. 光秃秃的地名 ----
    #
    # 这一步只在**整句里没有任何方向词**时才允许触发。
    # 试跑抓到过的实际 bug：「从上海出发」先被方向词填进 origin，
    # 剩下的文本「上海出发」又被当成光秃秃地名塞进 destination，
    # 于是凭空多出一条「上海 → 上海出发」的航线。
    # 位置推断只有在没有任何显式线索时才是合理的兜底；
    # 一旦有方向词，就该完全交给方向词，不再叠加猜测。
    has_direction = bool(_DIRECTION_CHAR.search(text))
    if not has_direction and _looks_like_place(core):
        open_slots = [s for s in ("origin", "destination", "place")
                      if s in missing and s not in out.slots]
        if len(open_slots) == 1:
            out.slots[open_slots[0]] = core
            out.evidence.append("%s ← 唯一待补的地名槽位" % open_slots[0])
        elif len(open_slots) > 1:
            # 真歧义。这里**不猜**，交给上层追问一句。
            out.ambiguous.append((core, open_slots))
            out.evidence.append("地名「%s」有 %d 个可能的去处，判为歧义"
                                % (core, len(open_slots)))

    # ---- 5. place 兜底：目的地技能里 place 允许是省名 / 景区名 ----
    if ("place" in missing and "place" not in out.slots and not out.ambiguous
            and not has_direction):
        if _looks_like_place(core) or (core and len(core) <= 10
                                       and re.fullmatch(r"[\u4e00-\u9fff]{2,10}", core)):
            out.slots["place"] = core
            out.evidence.append("place ← 回话主体")

    if not out.slots and not out.ambiguous:
        out.unparsed = True
    return out


def parse_correction(reply: str, today: date | None = None) -> dict[str, Any]:
    """从回话里解析**显式的纠错**，无视当前 missing。

    「不对，是从北京飞」「改成9月2号」这类，用户是在改已经填好的字段。
    只认带明确方向词 / 日期的表达 —— 光秃秃一个地名不算纠错，
    因为无从判断他想改哪一个。
    """
    out: dict[str, Any] = {}
    text = (reply or "").strip()
    if not text:
        return out

    m = _PAIR.search(text)
    if m and re.search(r"(改|不对|不是|应该|其实|换成|重新)", text):
        origin, dest = _clean_place(m.group(1)), _clean_place(m.group(2))
        if origin and dest and origin != dest:
            return {"origin": origin, "destination": dest}

    m = _ORIGIN_MARK.search(text)
    if m:
        value = _clean_place(m.group(1) or m.group(2) or "")
        if value:
            out["origin"] = value
    m = _DEST_MARK.search(text)
    if m:
        value = _clean_place(m.group(1))
        if value:
            out["destination"] = value
    got = extract_date(text, today)
    if got:
        out["travel_date"] = got
    return out


def parse_approval(reply: str) -> bool | None:
    """解析确认回话。True=同意，False=拒绝，**None=没表态**。

    三态而不是两态是刻意的：副作用操作前，"没听懂"和"拒绝"必须区分开——
    前者要再问一次，后者要明确取消。把 None 折叠成 False 会让用户
    每次说错话都得从头再来；折叠成 True 则是灾难。
    """
    text = _strip_filler(reply or "")
    if not text:
        return None
    if AFFIRMATIVE.match(text):
        return True
    if NEGATIVE.match(text):
        return False
    # 长句里的明确表态也认，但要求关键词靠前，避免
    # 「不要在没确认前创建」被读成肯定
    head = text[:8]
    if re.search(r"(确认|同意|可以|创建吧|建吧|好的)", head):
        return True
    if re.search(r"(不用|不要|取消|算了|别)", head):
        return False
    return None


def month_hint(text: str) -> int | None:
    return extract_month_hint(text or "")
