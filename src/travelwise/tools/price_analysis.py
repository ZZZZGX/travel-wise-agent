# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""price_analysis.py —— 「提前量启发式」分析（纯计算，无 IO、无平台依赖）。

方法：在今天这个时点横向扫一批近期出发日，得到一条「价格 ~ 提前天数」曲线；
取最低点对应的提前天数 N，平移到出行日：

    建议购票日 = 出行日 − N

## 曲线取哪个价：不能是「当日最低价」

实测打脸的一处。上海→昆明按「当日最低价」算出「提前 3 天最便宜（¥580）」，
但那 ¥580 全部来自只飞 3~4 天的中转航班，而且在 4 个日期上并列；
同一批全窗口直飞的**中位价**曲线则是一条底在提前 6 天的 U 型，
逐航班统计也有 25/34 班在那天见底。按 ¥580 给建议，
绝大多数真能订到的航班都要多花钱（中位贵 ¥115）。

所以优先级是：

  1. `consensus`  逐航班共识 —— 每班各自算最低点，再看多数落在哪天（最可信）；
  2. `panel_median`  可比面板中位价 —— 每天在同一批航班上取中位数；
  3. `min_price`  全部航班当日最低价 —— 只作对照，因为它每天在**不同的**
     航班集合上取 min，一班只飞那天的特价就能把整条线拉平。

四个必须处理的坑：
  1) 星期几噪声：横截面里最便宜那天，可能只是因为它本身是低需求的星期几。
     → 额外给出「与出行日同星期几对齐」的结果，两个并列，交给用户判断。
  2) 扫描窗口过短：只扫 10 天却在 23 天后出行，观测到的提前量被人为截断。
     → 最低点落在窗口边界时明确警告，不假装结论可靠。
  3) 同星期几样本太少：7 天窗口里同星期几只有 1 个样本，
     「在 1 个样本里取最小值」不是对齐，是同义反复。
     → 少于 MIN_WEEKDAY_SAMPLES 个样本直接不出这个结论。
  4) 并列最低：好几天同价时不能静默取最早的那天。
     → 全部列出；并列时优先取逐航班共识日，否则取提前量最大的（早买更稳）。

本模块【不联网】：接收一个 fetcher 回调，因此可完全脱网单测——
这也是 Tool Failure / 回归评测的接入点。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

# 近似节假日 / 大规模人流窗口。跨年需维护，可外置成配置文件。
HOLIDAY_WINDOWS: list[tuple[date, date]] = [
    (date(2026, 1, 24), date(2026, 3, 3)),    # 2026 春运（示意）
    (date(2026, 5, 1), date(2026, 5, 5)),     # 劳动节
    (date(2026, 10, 1), date(2026, 10, 8)),   # 国庆黄金周
    (date(2027, 1, 15), date(2027, 2, 20)),   # 2027 春运（示意）
]

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

#: 同星期几对齐至少需要几个样本才算一个结论。1 个样本 = 没有结论。
MIN_WEEKDAY_SAMPLES = 2

#: 判定「并列最低」的相对容差。0.1% 只为吸收浮点误差，不做模糊归并。
TIE_TOLERANCE = 0.001

#: 必须印在每一份分析里的方法声明。
#:
#: 模块开头写清楚了这是横截面近似，但那是给读代码的人看的。用户看到的是
#: 「提前 6 天最便宜」这样一句结论，而任何人读到它都会默认那是**他那班航班**
#: 的价格历史。它不是。不把这句话摆在结论旁边，前面处理的四个坑都白处理了——
#: 用户根本不知道自己在看一个近似值。
METHOD_DISCLOSURE = (
    "方法说明：这是**今天**这一时点上、不同出发日之间的横向对比，"
    "不是你那班航班的历史价格曲线。\n"
    "         航班接口只给当前报价、无法回溯，因此用「价格随提前天数变化」的"
    "规律做近似替代。\n"
    "         前提是这条规律在相邻出发日之间大致稳定——"
    "跨节假日、遇突发事件时会失效。")


def in_holiday(d: date) -> bool:
    return any(a <= d <= b for a, b in HOLIDAY_WINDOWS)


def _parse(d) -> date:
    return d if isinstance(d, date) else date.fromisoformat(str(d))


def scan_prices(origin: str, destination: str, today, fetcher: Callable,
                min_window: int = 7, max_window: int = 14,
                validate_after: int = 3, direct_only: bool = False):
    """横向扫描，构造「价格 ~ 提前天数」曲线。

    每查一天消耗一次额度，故带【提前停止】：
      - 至少扫 min_window 天，最多 max_window 天；
      - 全局最低价落在提前 N 天处，其后连续 validate_after 天未再更低即认为谷底
        已确认，提前停止；
      - 扫满上限仍一路走低 → 如实标记 trough_confirmed=False，不假装找到谷底。

    fetcher(origin, destination, "YYYY-MM-DD") -> list[对象或 dict]
    元素需能取到 price / ticket_price 与 transfer_num。
    """
    today = _parse(today)
    max_window = max(max_window, min_window)

    scan: list[dict] = []
    best_price: float | None = None
    best_adv: int | None = None
    api_calls = scanned = 0
    stopped_early = trough_confirmed = False

    for adv in range(1, max_window + 1):
        d = today + timedelta(days=adv)
        flights = fetcher(origin, destination, d.isoformat())
        api_calls += 1
        scanned = adv

        prices = []
        for f in flights:
            price = getattr(f, "price", None)
            if price is None and isinstance(f, dict):
                price = f.get("price", f.get("ticket_price"))
            transfers = getattr(f, "transfer_num", None)
            if transfers is None and isinstance(f, dict):
                transfers = f.get("transfer_num", 1)
            if isinstance(price, (int, float)) and (not direct_only or (transfers or 1) == 1):
                prices.append(float(price))

        if prices:
            mp = min(prices)
            scan.append({"date": d, "advance": adv, "weekday": d.weekday(), "min_price": mp})
            if best_price is None or mp < best_price:
                best_price, best_adv = mp, adv

        if adv >= min_window and best_adv is not None and adv >= best_adv + validate_after:
            trough_confirmed = True
            stopped_early = adv < max_window
            break

    meta = {
        "scanned_days": scanned, "api_calls": api_calls,
        "stopped_early": stopped_early, "trough_confirmed": trough_confirmed,
        "best_advance": best_adv, "min_window": min_window,
        "max_window": max_window, "validate_after": validate_after,
    }
    return scan, meta


def basis_of(scan: list[dict]) -> str:
    """选口径。面板中位价只有在**每一天都算得出来**时才用，否则退回最低价。"""
    if scan and all(isinstance(r.get("panel_median"), (int, float)) for r in scan):
        return "panel_median"
    return "min_price"


BASIS_LABEL = {
    "panel_median": "可比面板中位价（每天在同一批全窗口直飞航班上取中位数）",
    "min_price": "全部航班的当日最低价",
}


def _price(row: dict, basis: str) -> float:
    v = row.get(basis)
    return float(v if isinstance(v, (int, float)) else row["min_price"])


def _minimum(scan: list[dict], basis: str, travel_date: date,
             prefer_advance: int | None = None) -> dict:
    """在给定口径上取最低点。**并列全部保留**，不静默取最早的那天。

    并列时的取舍：能对上逐航班共识日就取它；否则取提前量最大的那天——
    早买的一侧是"多等几天可能涨"，晚买的一侧是"错过就没了"，
    两个风险不对称，所以并列时偏向早买。
    """
    lo = min(_price(r, basis) for r in scan)
    ties = [r for r in scan if _price(r, basis) <= lo * (1 + TIE_TOLERANCE)]
    chosen = None
    if prefer_advance is not None:
        chosen = next((r for r in ties if r["advance"] == prefer_advance), None)
    if chosen is None:
        chosen = max(ties, key=lambda r: r["advance"])

    buy = travel_date - timedelta(days=chosen["advance"])
    return {
        "advance_days": chosen["advance"],
        "cheapest_scan_date": chosen["date"].isoformat(),
        "cheapest_scan_price": round(_price(chosen, basis), 1),
        "recommended_buy_date": buy.isoformat(),
        "buy_date_passed": False,          # 由 analyze_from_scan 统一按 today 判定
        "tie_advances": [r["advance"] for r in ties],
        "tie_dates": [r["date"].isoformat() for r in ties],
    }


def consensus_from_advice(advice: dict, today, travel_date,
                          min_share: float = 0.5) -> dict | None:
    """把 price_matrix.per_flight_advice 的共识日折成一条购票建议。

    共识日是**多数航班各自的最低点落在同一天**，与"哪天的最低价最低"是
    两件不同的事：后者可以被一班特价航班决定，前者需要几十班同时同意。
    低于 min_share 的共识不算共识，返回 None。
    """
    if not advice or not advice.get("ok"):
        return None
    total = advice.get("total") or 0
    agree = advice.get("agree") or 0
    if not total or agree < total * min_share:
        return None

    today = _parse(today)
    travel_date = _parse(travel_date)
    day = _parse(advice["consensus_day"])
    advance = (day - today).days
    buy = travel_date - timedelta(days=advance)
    return {
        "advance_days": advance,
        "cheapest_scan_date": day.isoformat(),
        "cheapest_scan_price": advice.get("median_price_on_consensus"),
        "recommended_buy_date": buy.isoformat(),
        "agree": agree, "total": total,
        "share": round(agree / total, 3),
        "median_saving": advice.get("median_saving"),
    }


def analyze_from_scan(scan: list[dict], today, travel_date,
                      consensus: dict | None = None) -> dict:
    """纯函数：给定扫描结果算出提前量与建议购票日。可脱网单测。

    `consensus` 由 price_matrix.per_flight_advice → consensus_from_advice 给出，
    没有也能跑（退回中位价 / 最低价口径）。
    """
    today = _parse(today)
    travel_date = _parse(travel_date)
    days_until = (travel_date - today).days
    warnings: list[str] = []

    if not scan:
        return {"ok": False, "reason": "扫描窗口内没有任何有效票价数据", "warnings": warnings}

    basis = basis_of(scan)
    panel_size = scan[0].get("panel_size") or 0
    basis_label = BASIS_LABEL[basis]
    if basis == "panel_median":
        basis_label += "，共 %d 班" % panel_size
    else:
        for r in scan:
            if r.get("panel_median") is None and r.get("panel_size") is not None:
                warnings.append("可比面板不足（全窗口都在飞的直飞航班太少），"
                                "只能退回「当日最低价」口径：它每天在不同的航班集合上"
                                "取 min，跨日期比较不严格，结论请当参考。")
                break

    prefer = consensus["advance_days"] if consensus else None

    # 主口径最低点（原键名 raw 保留，含义随口径而变，basis 字段写明是哪个）
    best_raw = _minimum(scan, basis, travel_date, prefer_advance=prefer)
    if len(best_raw["tie_advances"]) > 1:
        warnings.append(
            "%s 口径下有 %d 天并列最低（提前 %s 天，均为 ¥%s）：这几天买都一样，"
            "本次取提前量最大的那天%s。"
            % ("面板中位价" if basis == "panel_median" else "当日最低价",
               len(best_raw["tie_advances"]),
               "/".join(str(a) for a in best_raw["tie_advances"]),
               best_raw["cheapest_scan_price"],
               "（并与逐航班共识日一致）" if prefer in best_raw["tie_advances"] else ""))

    # 对照口径：全量当日最低价
    envelope = None
    if basis != "min_price":
        envelope = _minimum(scan, "min_price", travel_date)

    # 坑 1 & 3：同星期几对齐，样本不足就不出结论
    tw = travel_date.weekday()
    aligned_rows = [r for r in scan if r["weekday"] == tw]
    if len(aligned_rows) >= MIN_WEEKDAY_SAMPLES:
        best_wd = _minimum(aligned_rows, basis, travel_date, prefer_advance=prefer)
        best_wd["aligned_weekday"] = WEEKDAY_CN[tw]
        best_wd["sample_size"] = len(aligned_rows)
    else:
        best_wd = None
        warnings.append(
            "同星期几对齐本次不可用：窗口内与出行日同为「%s」的样本只有 %d 个，"
            "在 1 个样本里取最小值不是对齐（至少要 %d 个）。窗口拉到 %d 天才有 2 个样本。"
            % (WEEKDAY_CN[tw], len(aligned_rows), MIN_WEEKDAY_SAMPLES, 14))

    # 主结论：逐航班共识 > 同星期几对齐 > 主口径最低点
    if consensus:
        primary, primary_method = consensus, "逐航班共识"
    elif best_wd:
        primary, primary_method = best_wd, "同星期几对齐法"
    else:
        primary, primary_method = best_raw, (
            "面板中位价法" if basis == "panel_median" else "当日最低价法")

    buy_primary = _parse(primary["recommended_buy_date"])
    adv_primary = primary["advance_days"]

    # 坑 2：窗口是否够长
    observed_max = max(r["advance"] for r in scan)
    if adv_primary >= observed_max and observed_max < days_until:
        warnings.append(
            "最便宜点出现在扫描窗口最后一天（提前 %d 天），价格可能仍在下降却被窗口上限截断；"
            "距出行还有 %d 天，更长提前量或存在更低点，结论偏保守，可择日重跑或放宽上限。"
            % (observed_max, days_until))

    if in_holiday(travel_date):
        warnings.append("出行日落在春运 / 黄金周窗口，本启发式在大规模人流期不可靠，仅供参考。")
    if in_holiday(buy_primary):
        warnings.append("建议购票日落在节假日窗口，价格可能异常。")

    passed = buy_primary <= today
    if passed:
        gap = (today - buy_primary).days
        if gap <= 0:
            warnings.append("按主结论算出的购票日就是今天——建议立即查实时价格后尽快决定。")
        else:
            warnings.append(
                "推断的最佳提前量约 %d 天，而现在距出行仅剩 %d 天，即最佳购票窗口已过约 %d 天。"
                "此时通常已错过窗口内低点，越等越可能更贵——建议立即查实时价格尽快决定。"
                % (adv_primary, days_until, gap))
    primary = dict(primary)
    primary["buy_date_passed"] = passed
    best_raw["buy_date_passed"] = _parse(best_raw["recommended_buy_date"]) <= today

    return {
        "ok": True, "route": None,
        "today": today.isoformat(), "travel_date": travel_date.isoformat(),
        "days_until_travel": days_until, "scan_points": len(scan),
        "observed_max_advance": observed_max,
        "basis": basis, "basis_label": basis_label, "panel_size": panel_size,
        "raw": best_raw,
        "envelope": envelope,
        "weekday_aligned": best_wd,
        "consensus": consensus,
        "primary_method": primary_method,
        "primary": primary,
        "warnings": warnings,
    }


def pick_recommendation(analysis: dict) -> dict | None:
    """所有下游（提醒、工具返回值、smoke 脚本）都从这里取建议购票日。

    以前每个调用点各写一遍 `weekday_aligned or raw`，口径一改就得改好几处，
    还漏了逐航班共识。收敛到一个函数。
    """
    if not analysis or not analysis.get("ok"):
        return None
    return (analysis.get("primary") or analysis.get("consensus")
            or analysis.get("weekday_aligned") or analysis.get("raw"))


def analyze(origin: str, destination: str, travel_date, fetcher: Callable, today=None,
            min_window: int = 7, max_window: int = 14,
            validate_after: int = 3, direct_only: bool = False) -> dict:
    """端到端：扫描 + 分析。"""
    today = _parse(today) if today else date.today()
    scan, meta = scan_prices(origin, destination, today, fetcher,
                             min_window=min_window, max_window=max_window,
                             validate_after=validate_after, direct_only=direct_only)
    res = analyze_from_scan(scan, today, travel_date)
    res["route"] = "%s→%s" % (origin, destination)
    res["_scan"] = scan
    res["_meta"] = meta
    return res


def render_report(res: dict) -> str:
    """渲染成人读的报告。

    排序即优先级：**主结论在最上面，对照口径在下面并注明为什么只是对照。**
    以前把「当日最低价」的结论摆在第一行，用户照着买就会亏。
    """
    if not res.get("ok"):
        return "无法给出购买建议：%s" % res.get("reason", "未知原因")

    L = ["【%s · 提前购票分析】" % res["route"], ""]
    L.append("出行日期：%s（还有 %d 天）" % (res["travel_date"], res["days_until_travel"]))

    meta = res.get("_meta") or {}
    if meta:
        if meta.get("stopped_early"):
            stop = "提前停止：谷底已确认（连续 %d 天无更低价）" % meta.get("validate_after", 3)
        elif meta.get("trough_confirmed"):
            stop = "谷底已确认"
        else:
            stop = "扫满 %d 天上限，谷底未确认" % meta.get("max_window", 14)
        # 原文案是「窗口 7~14 天」，被读成「从第 7 天才开始扫」—— 实际是从明天
        # 就开始扫，7 和 14 管的是**什么时候允许停**。一个让人误以为漏查了
        # 前 6 天的措辞，比没有这行更糟。
        L.append("扫描范围：明天起逐日查（提前 1~%d 天），本次实扫 %d 天"
                 "，消耗 %d 次查询额度"
                 % (meta.get("scanned_days", 0), meta.get("scanned_days", 0),
                    meta.get("api_calls", 0)))
        L.append("停止规则：至少 %d 天、至多 %d 天｜%s"
                 % (meta.get("min_window", 7), meta.get("max_window", 14), stop))
    L.append("价格口径：%s" % res.get("basis_label", ""))
    L.append(METHOD_DISCLOSURE)
    L.append("")

    scan = res.get("_scan") or []
    if scan:
        has_panel = res.get("basis") == "panel_median"
        if has_panel:
            L.append("| 出发日 | 星期 | 提前天数 | 面板中位价 | 全量最低价（对照） |")
            L.append("|--------|------|----------|-----------|------------------|")
        else:
            L.append("| 出发日 | 星期 | 提前天数 | 当日最低价 |")
            L.append("|--------|------|----------|-----------|")
        best = res["raw"]["cheapest_scan_price"]
        for r in scan:
            price = _price(r, res.get("basis", "min_price"))
            mark = " ← 最低" if price <= best * (1 + TIE_TOLERANCE) else ""
            if has_panel:
                L.append("| %s | %s | 提前%d天 | ¥%s%s | ¥%s |"
                         % (r["date"].isoformat(), WEEKDAY_CN[r["weekday"]],
                            r["advance"], round(price, 1), mark, r["min_price"]))
            else:
                L.append("| %s | %s | 提前%d天 | ¥%s%s |"
                         % (r["date"].isoformat(), WEEKDAY_CN[r["weekday"]],
                            r["advance"], round(price, 1), mark))
        L.append("")

    primary = res.get("primary") or res["raw"]
    L.append("🎯 建议购票日：**%s**（提前 %d 天，依据：%s）"
             % (primary["recommended_buy_date"], primary["advance_days"],
                res.get("primary_method", "")))

    con = res.get("consensus")
    if con:
        price_text = ("，那天中位价 ¥%s" % round(con["cheapest_scan_price"], 1)) \
            if isinstance(con.get("cheapest_scan_price"), (int, float)) else ""
        L.append("   逐航班共识：%d/%d 班（%.0f%%）各自的最低点都落在 %s（提前 %d 天）%s"
                 % (con["agree"], con["total"], con["share"] * 100,
                    con["cheapest_scan_date"], con["advance_days"], price_text))
        if isinstance(con.get("median_saving"), (int, float)):
            L.append("   逐航班中位跌幅 %.0f%%（相对各自的窗口均价）" % con["median_saving"])

    raw = res["raw"]
    raw_name = "面板中位价法" if res.get("basis") == "panel_median" else "当日最低价法"
    ties = ("；并列 %d 天（提前 %s 天）" % (len(raw["tie_advances"]),
            "/".join(str(a) for a in raw["tie_advances"]))) \
        if len(raw["tie_advances"]) > 1 else ""
    L.append("📊 %s：提前 %d 天最便宜（¥%s）→ %s%s"
             % (raw_name, raw["advance_days"], raw["cheapest_scan_price"],
                raw["recommended_buy_date"], ties))

    env = res.get("envelope")
    if env:
        env_ties = ("；该口径下 %d 天并列（提前 %s 天）" % (len(env["tie_advances"]),
                    "/".join(str(a) for a in env["tie_advances"]))) \
            if len(env["tie_advances"]) > 1 else ""
        L.append("📉 全量最低价法（**仅对照**，每天在不同的航班集合上取 min，"
                 "一班只飞那天的中转特价就能定住它）：提前 %d 天（¥%s）→ %s%s"
                 % (env["advance_days"], env["cheapest_scan_price"],
                    env["recommended_buy_date"], env_ties))

    wd = res.get("weekday_aligned")
    if wd:
        L.append("🗓 同星期几对齐法（%d 个「%s」样本）：提前 %d 天最便宜（¥%s）→ %s"
                 % (wd["sample_size"], wd["aligned_weekday"], wd["advance_days"],
                    wd["cheapest_scan_price"], wd["recommended_buy_date"]))
    else:
        L.append("🗓 同星期几对齐法：本次样本不足，未输出（见下方注意事项）。")

    if res["warnings"]:
        L.append("")
        L.append("⚠️ 注意：")
        L.extend("- " + w for w in res["warnings"])

    L.append("")
    L.append("说明：本方法是基于「航线运力相对稳定」的近似启发式，非精确预测；"
             "价格为航司基准最低可售价，不含平台优惠券；实际以购票平台为准。")
    return "\n".join(L)


def render_schedule(flights, origin: str, destination: str, day: str) -> str:
    """只列航班时刻（数据源不含票价时用）。"""
    L = ["【%s → %s ｜ %s 航班时刻】共 %d 班" % (origin, destination, day, len(flights))]
    if not flights:
        L.append("（该数据源未返回航班：可能此航线当天无航班，或换个日期再试）")
        return "\n".join(L)
    for f in flights:
        dep_ap = getattr(f, "departure_airport", "") or ""
        arr_ap = getattr(f, "arrival_airport", "") or ""
        L.append("· %s %s ｜ %s %s → %s %s"
                 % (getattr(f, "airline", ""), getattr(f, "flight_no", ""),
                    dep_ap, getattr(f, "departure_time", ""),
                    arr_ap, getattr(f, "arrival_time", "")))
    L.append("")
    L.append("⚠️ 当前数据源不含票价，故「提前几天买最便宜」的分析暂无法给出；"
             "分析逻辑已就绪，接入带票价的数据源即自动生效。")
    return "\n".join(L)
