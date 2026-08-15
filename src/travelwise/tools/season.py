# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""
season_detector.py —— 季节性判断

两条路径（优先用第一条，省抓取）：
  1) 名录自带"适宜季节"字段（你的数据里 857 条有 720 条带此字段）：
     直接归一化成 {春,夏,秋,冬} 集合。
  2) 兜底：若某地点没有该字段，才用小红书帖子的发布月份分布来推断
     （对应交接文档的"统计帖子发布时间分布"逻辑）。

再拿用户出行月份去比对，不匹配就产出警告文案，供 Skill 标注。
"""

import re

_SEASONS = ["春", "夏", "秋", "冬"]
_MONTH_SEASON = {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春",
                 6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}


def normalize_season(raw):
    """
    把自由文本的"适宜季节"归一化为季节集合。
    覆盖形如：全年 / 四季 / 夏季 / 春秋季 / 夏秋季 / 4月~10月 / 秋季 等。

    ⚠️ 关键区分（修复"空字段被当全年"的隐患）：
      - 字段为空 / 完全无法解析 → 返回【空集合】，代表"未标注/未知"，
        由 check_match 明示"季节未知"，而不是默默当成"全年皆宜"掩盖季节不匹配。
      - 明确写"全年/四季/皆宜"才返回四季全集。
    """
    if not raw:
        return set()  # 未标注：留空，交给上层判断，绝不冒充"全年"
    raw = raw.strip()
    if not raw:
        return set()
    if any(k in raw for k in ("全年", "四季", "皆宜")):
        return set(_SEASONS)

    found = set(s for s in _SEASONS if s in raw)

    # 处理"4月~10月"这类月份区间
    months = re.findall(r"(\d{1,2})\s*月", raw)
    if len(months) >= 2:
        a, b = int(months[0]), int(months[-1])
        rng = range(a, b + 1) if a <= b else list(range(a, 13)) + list(range(1, b + 1))
        for m in rng:
            found.add(_MONTH_SEASON[m])

    # 有文本但一个季节/月份都没解析出来 → 视为未知（空集），不臆造"全年"
    return found


def season_of_month(month):
    return _MONTH_SEASON[int(month)]


def infer_from_post_months(months):
    """
    兜底：给一组帖子发布月份（list[int]），返回推断的适宜季节集合。
    规则：某季节占比明显偏高（>40%）则命中；否则视为全年皆宜。
    """
    if not months:
        return set(_SEASONS)
    from collections import Counter
    c = Counter(_MONTH_SEASON[int(m)] for m in months)
    total = sum(c.values())
    hot = {s for s, n in c.items() if n / total > 0.40}
    return hot or set(_SEASONS)


def check_match(spot_season_raw, travel_month, post_months=None):
    """
    判断出行月份是否落在景点适宜季节内。
    返回 dict：{best_seasons, travel_season, match, note}
    """
    seasons = normalize_season(spot_season_raw)
    tseason = season_of_month(travel_month)

    # 未标注：字段为空且解析不出 → 若有帖子月份才兜底推断，否则如实标"季节未知"。
    if not seasons:
        if post_months:
            seasons = infer_from_post_months(post_months)  # 仅无字段时才走兜底统计
        if not seasons:
            return {"best_seasons": [], "travel_season": tseason,
                    "match": True, "unknown": True,
                    "note": "ℹ️ 该景点名录未标注适宜季节，无法判断 %d 月是否合适，"
                            "建议自行核实当季景观。" % travel_month}

    match = tseason in seasons or seasons == set(_SEASONS)
    note = ""
    if not match:
        note = ("⚠️ 此景点适宜季节为「%s」，你的出行在 %d 月（%s），"
                "当前时段可能看不到预期景观。"
                % ("、".join(s for s in _SEASONS if s in seasons), travel_month, tseason))
    return {"best_seasons": sorted(seasons, key=_SEASONS.index),
            "travel_season": tseason, "match": match, "unknown": False, "note": note}


if __name__ == "__main__":
    for raw in ["全年", "夏季", "春秋季", "4月~10月", "秋季", ""]:
        print("%-8s ->" % (raw or "(空)"), sorted(normalize_season(raw), key=_SEASONS.index))
    print(check_match("夏季", 12))
    print(check_match("全年", 12))
