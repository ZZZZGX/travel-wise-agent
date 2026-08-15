# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""smoke_full_flow.py —— 全流程分级冒烟测试。

## 为什么分级

这条链路上有两种东西会花钱：航班接口（¥0.2 / 次）和 LLM（token）。
一次性全跑，出问题时你分不清是配置错了、接口挂了、还是模型不听话，
而每重试一次都在付费。所以按【花费从零到有】排序，前面的过不了就不往下走：

    阶段 0  环境体检          0 元 0 token   —— 变量、配置、城市码表
    阶段 1  离线全流程        0 元 0 token   —— mock 数据跑通编排 + 矩阵 + 导出
    阶段 2  LLM 连通          ~50 token      —— 一句话确认 Key / 地址 / 模型名
    阶段 3  航班接口连通      2 次 ≈ ¥0.4    —— 每家 1 次，验凭证与字段映射
    阶段 4  真实价格矩阵      N 次 ≈ ¥0.2N   —— 默认 7 天
    阶段 5  Agent 全闭环      0 次（命中缓存）+ token

阶段 3 之后每一步都会先报价并要你确认（`--yes` 跳过确认，`--stage N` 只跑到第 N 级）。

用法（在项目根目录）：

    python scripts\\smoke_full_flow.py                 # 只跑 0~2，不花接口钱
    python scripts\\smoke_full_flow.py --stage 5       # 跑完整链路，逐级确认
    python scripts\\smoke_full_flow.py --stage 5 --yes # 不确认，直接跑
    python scripts\\smoke_full_flow.py --route 杭州 武汉 --days 7
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

UNIT_PRICE = 0.2          # 元 / 次，按 20 元 100 次算

_results: list[tuple[str, bool, str]] = []


def head(stage: int, title: str, cost: str) -> None:
    print()
    print("=" * 66)
    print("阶段 %d ｜ %s ｜ 预计花费：%s" % (stage, title, cost))
    print("=" * 66)


def ok(name: str, detail: str = "") -> bool:
    print("  [ OK ] %s%s" % (name, ("  " + detail) if detail else ""))
    _results.append((name, True, detail))
    return True


def fail(name: str, detail: str = "") -> bool:
    print("  [FAIL] %s%s" % (name, ("  " + detail) if detail else ""))
    _results.append((name, False, detail))
    return False


def confirm(question: str, auto_yes: bool) -> bool:
    if auto_yes:
        print("  (--yes 已自动确认)")
        return True
    try:
        answer = input("  %s [y/N] " % question).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


# ==========================================================================
# 阶段 0：环境体检
# ==========================================================================

def stage0(args) -> bool:
    head(0, "环境体检", "0 元 0 token")
    from travelwise.config import Settings, diagnose_dotenv

    # 先把「到底读了哪个文件、解析出哪些键」摆出来。
    # 「我明明设了却读不到」几乎总是文件的问题，不是代码的问题。
    report = diagnose_dotenv()
    print("  .env 路径：%s" % report["path"])
    if report["exists"]:
        keys = report["keys"]
        print("  已解析 %d 个键（%d 字节）：" % (len(keys), report["size"]))
        for item in keys:
            print("      第%-3d行  %-34s %s"
                  % (item["line"], item["key"],
                     "（空值）" if item["empty"] else "长度 %d" % item["length"]))
    else:
        print("  .env 不存在")
    for problem in report["problems"]:
        print("  [!] %s" % problem)

    settings = Settings.from_env()
    good = True

    print("  数据源=%s ｜ 天数=%d ｜ 缓存=%s ｜ 间隔=%.1fs"
          % (settings.flight_provider, settings.matrix_days,
             "开" if settings.flight_cache else "关", settings.request_interval))
    print("  LLM=%s ｜ 模型=%s ｜ 地址=%s"
          % (settings.llm_provider, settings.llm_model or "(默认)",
             settings.llm_base_url or "(默认)"))
    # 目的地二层发现是**可选**能力：没配不算失败，但必须让人看见它没开，
    # 否则会误以为"这城市就这么点地方"，其实是根本没搜。
    from travelwise.config import build_web_search_provider
    search = build_web_search_provider(settings)
    print("  搜索源=%s（二层发现%s）｜ 场景词=%s"
          % (settings.search_provider, "已启用" if search.enabled else "未启用",
             settings.scene_list() or "默认四个"))
    if not search.enabled:
        print("    [提示] %s" % getattr(search, "reason", ""))
        print("    [提示] 想离线验证二层：TRAVELWISE_SEARCH_PROVIDER=fixture")

    if settings.flight_provider != "http" and settings.flight_token:
        print("  [!] 已配 AppCode，但 TRAVELWISE_FLIGHT_PROVIDER=%s —— "
              "阶段 3/4/5 会跑**假数据**，不会调用真实接口。"
              "要打真接口请改成 http。" % settings.flight_provider)

    # 凭证只看有没有，绝不打印内容
    if settings.flight_token:
        ok("AppCode 已设置", "长度 %d" % len(settings.flight_token))
    else:
        good = fail("AppCode 未设置", "在 .env 里填 TRAVELWISE_FLIGHT_TOKEN")

    backup = os.environ.get("TRAVELWISE_FLIGHT_TOKEN_BACKUP", "")
    if backup:
        ok("备用 AppCode 已设置",
           "与主 AppCode 相同" if backup == settings.flight_token else "与主 AppCode 不同")
    else:
        print("  [提示] 未设 TRAVELWISE_FLIGHT_TOKEN_BACKUP，第二家会回落用主 AppCode")

    if settings.llm_api_key:
        ok("LLM API Key 已设置", "长度 %d" % len(settings.llm_api_key))
    else:
        good = fail("LLM API Key 未设置", "在 .env 里填 TRAVELWISE_LLM_API_KEY")

    if settings.llm_base_url.rstrip("/").endswith("/v1"):
        print("  [提示] BASE_URL 结尾带 /v1，客户端会自动去掉，避免拼成 /v1/v1")

    # 航班接口配置
    cfg = settings.load_flight_api_config()
    providers = cfg.get("providers") or ([cfg] if cfg.get("endpoint") else [])
    if providers:
        ok("航班接口配置已加载", "%d 个数据源：%s"
           % (len(providers), "、".join(p.get("name", "?") for p in providers)))
        for p in providers:
            resp = p.get("response") or {}
            if not resp.get("list_path"):
                print("    [提示] %s 未指定 list_path，将靠自动探测" % p.get("name"))
            if not resp.get("field_map"):
                print("    [提示] %s 未指定 field_map，将靠别名表猜字段" % p.get("name"))
    else:
        good = fail("航班接口配置为空", "检查 config/flight_api.json")

    # 城市三字码
    from travelwise.tools import city_codes, price_analysis
    try:
        table = city_codes.load_table()
        missing = [c for c in args.route if not city_codes.try_resolve(c)]
        if missing:
            good = fail("城市码解析", "查不到：%s（需在 data/source/city_codes.csv 增补）"
                        % "、".join(missing))
        else:
            ok("城市码解析", "%s → %s ｜ 表内 %d 个键"
               % ("、".join(args.route),
                  "、".join(city_codes.resolve(c) for c in args.route), len(table)))
    except Exception as e:                       # noqa: BLE001
        good = fail("城市码表读取失败", str(e))

    return good


# ==========================================================================
# 阶段 1：离线全流程（mock）
# ==========================================================================

def stage1(args) -> bool:
    head(1, "离线全流程（mock 数据）", "0 元 0 token")
    from travelwise.orchestrator import TravelWiseAgent
    from travelwise.providers.mock_flight import MockFlightProvider
    from travelwise.tools import matrix_export, price_analysis

    from travelwise.config import Settings, build_web_search_provider
    from travelwise.skills.destination import DestinationSkill

    today = date.today()
    travel = (today + timedelta(days=args.days + 10)).isoformat()
    settings = Settings.from_env()
    search = build_web_search_provider(settings)
    agent = TravelWiseAgent(MockFlightProvider(today=today), today=today,
                            matrix_days=args.days,
                            destination_skill=DestinationSkill(
                                search, scenes=settings.scene_list(),
                                min_mentions=settings.search_min_mentions))
    state = agent.handle("%s从%s飞%s，%s有什么好玩的"
                         % (travel, args.route[0], args.route[1], args.route[1]))

    result = state.flight_result or {}
    if not result.get("ok"):
        return fail("mock 机票流程", str(result.get("error"))[:80])
    matrix = result.get("matrix")
    if matrix is None or not matrix.rows:
        return fail("价格矩阵", "没有构建出任何航班行")
    ok("价格矩阵", "%d 个航班 × %d 天" % (len(matrix.rows), len(matrix.columns)))

    if result.get("analysis", {}).get("ok"):
        ok("提前量分析", "建议购票日 %s"
           % (price_analysis.pick_recommendation(result["analysis"]) or {})
           .get("recommended_buy_date"))

    out_dir = ROOT / "data" / "cache" / "exports"
    for fmt in ("csv", "xlsx", "html"):
        try:
            path = matrix_export.export(matrix, fmt, out_dir / ("smoke-mock." + fmt))
            ok("导出 %s" % fmt, "%s（%d 字节）" % (path.name, path.stat().st_size))
        except Exception as e:                   # noqa: BLE001
            fail("导出 %s" % fmt, str(e))

    dest = state.destination_result
    if dest:
        ok("目的地检索（一层 · 名录）", "%s 命中 %d 条"
           % (dest.get("place"), dest.get("official_count", 0)))
        if dest.get("discovery_enabled"):
            ok("场景发现（二层）", "抽出 %d 个地点" % dest.get("discovered_count", 0))
        else:
            # 不当成失败：没配搜索源是合法状态。但**必须说出来**。
            print("  [提示] 二层发现未启用：%s" % (dest.get("discovery_reason") or ""))
    return True


# ==========================================================================
# 阶段 2：LLM 连通
# ==========================================================================

def stage2(args) -> bool:
    head(2, "LLM 连通性", "约 50 token")
    from travelwise.config import Settings, build_llm_client
    from travelwise.llm.base import LLMError
    from travelwise.llm.messages import UserMessage

    settings = Settings.from_env()
    if settings.llm_provider == "scripted":
        print("  当前是离线回放模式（scripted），跳过真实调用。")
        print("  要测真模型：.env 里设 TRAVELWISE_LLM_PROVIDER=openai（DeepSeek 兼容）")
        return True

    client = build_llm_client(settings)
    print("  客户端=%s ｜ 模型=%s ｜ 地址=%s"
          % (client.name, getattr(client, "model", "?"), getattr(client, "base_url", "?")))
    try:
        response = client.complete([UserMessage(text="只回复两个字：收到")],
                                   system="你是一个测试探针。", max_tokens=16)
    except LLMError as e:
        return fail("LLM 调用", str(e)[:200])
    except Exception as e:                       # noqa: BLE001
        return fail("LLM 调用", "%s: %s" % (type(e).__name__, str(e)[:160]))

    ok("LLM 调用", "回复=%r ｜ token=%d ｜ 模型=%s"
       % (response.text.strip()[:20], response.usage.total, response.model))
    return True


# ==========================================================================
# 阶段 3：航班接口连通（付费）
# ==========================================================================

def stage3(args) -> bool:
    from travelwise.config import Settings, build_flight_provider
    from travelwise.providers.base import ProviderError

    settings = Settings.from_env()
    settings.flight_provider = "http"
    settings.flight_cache = False                # 探测必须真打，不能读缓存
    provider = build_flight_provider(settings)
    targets = list(getattr(provider, "providers", [provider]))

    head(3, "航班接口连通（每家 1 次）",
         "%d 次 ≈ ¥%.1f" % (len(targets), len(targets) * UNIT_PRICE))
    if not confirm("确认调用真实付费接口？", args.yes):
        print("  已跳过。")
        return True

    day = (date.today() + timedelta(days=1)).isoformat()
    good = True
    for p in targets:
        try:
            flights = p.search_flights(args.route[0], args.route[1], day)
        except ProviderError as e:
            good = fail("%s 调用" % p.name, str(e)[:180])
            continue

        if not flights:
            fail("%s 返回" % p.name, "连上了但当天 0 条航班，换个日期或热门航线再试")
            continue

        f = flights[0]
        ok("%s 调用" % p.name, "%d 条航班" % len(flights))
        print("      首条：%s %s ｜ %s %s → %s %s ｜ ¥%s ｜ %s"
              % (f.airline, f.flight_no, f.departure_airport, f.departure_time,
                 f.arrival_airport, f.arrival_time, f.price, f.departure_date))

        # 字段体检：这几项错了，矩阵会静默失真
        if not f.flight_no:
            good = fail("  航班号", "取不到 → 矩阵行主键失效，检查 field_map.flight_no")
        prices = [x.price for x in flights if x.price is not None]
        if not prices:
            good = fail("  票价", "一条都没取到 → 检查 response.price_field")
        elif len(set(prices)) == 1 and len(prices) > 2:
            good = fail("  票价", "所有航班同价 ¥%s → 很可能取到了全价而非最低可售价"
                        % prices[0])
        if f.departure_date and f.departure_date != day:
            good = fail("  日期", "请求 %s 返回 %s → 接口可能忽略了 depDate"
                        % (day, f.departure_date))
    return good


# ==========================================================================
# 阶段 4：真实价格矩阵（付费）
# ==========================================================================

def stage4(args) -> bool:
    from travelwise.config import Settings, build_flight_provider
    from travelwise.skills.flight import FlightSkill
    from travelwise.tools import matrix_export

    settings = Settings.from_env()
    settings.flight_provider = "http"
    today = date.today()

    head(4, "真实价格矩阵（%d 天）" % args.days,
         "最多 %d 次 ≈ ¥%.1f（缓存命中的部分不计费）"
         % (args.days, args.days * UNIT_PRICE))
    if not confirm("确认扫描 %d 天？" % args.days, args.yes):
        print("  已跳过。")
        return True

    provider = build_flight_provider(settings, today=today)
    skill = FlightSkill(provider)
    travel = (today + timedelta(days=args.days + 10)).isoformat()
    result = skill.run(args.route[0], args.route[1], travel, today=today,
                       matrix_days=args.days,
                       sleep_between=settings.request_interval)

    if not result.get("ok"):
        return fail("矩阵构建", str(result.get("error"))[:200])

    matrix = result["matrix"]
    ok("矩阵构建", "%d 个航班 × %d 天（有效 %d 天 / 失败 %d 天）"
       % (len(matrix.rows), len(matrix.columns),
          len(matrix.valid_columns), len(matrix.failed_days)))
    print()
    print(result["matrix_text"])

    path = matrix_export.export(matrix, args.export,
                                ROOT / "data" / "cache" / "exports" /
                                ("real-%s-%s.%s" % (args.route[0], args.route[1], args.export)))
    ok("导出", str(path))

    _cost(provider)
    return not matrix.warnings or all("失败" not in w for w in matrix.warnings)


# ==========================================================================
# 阶段 5：Agent 全闭环
# ==========================================================================

def stage5(args) -> bool:
    from travelwise.agent_loop import ToolCallingAgent
    from travelwise.config import (Settings, build_flight_provider,
                                   build_llm_client)
    from travelwise.skills.destination import DestinationSkill
    from travelwise.skills.flight import FlightSkill
    from travelwise.tools.registry import build_registry

    settings = Settings.from_env()
    settings.flight_provider = "http"
    today = date.today()

    head(5, "Agent 全闭环（模型自主选工具）",
         "接口 0~%d 次（阶段 4 的缓存多半命中）+ token" % args.days)
    if not confirm("确认运行？", args.yes):
        print("  已跳过。")
        return True

    provider = build_flight_provider(settings, today=today)
    registry = build_registry(FlightSkill(provider), DestinationSkill(), today=today,
                              default_days=args.days,
                              request_interval=settings.request_interval)
    client = build_llm_client(settings)
    if getattr(client, "is_synthetic", False):
        print("  [提示] 当前是离线回放客户端，结果不代表真实模型能力。")

    travel = (today + timedelta(days=args.days + 10)).isoformat()
    request = ("%s从%s飞%s，帮我看看未来这几天各个航班的价格怎么变，"
               "另外%s有什么好玩的"
               % (travel, args.route[0], args.route[1], args.route[1]))
    print("  用户请求：%s" % request)
    print("-" * 66)

    result = ToolCallingAgent(client, registry, today=today, max_tokens=2048).run(request)
    print(result.answer)
    print("-" * 66)
    print("  工具链：%s" % (" → ".join(result.tool_names) or "无"))
    print("  轮次 %d ｜ token %d ｜ 耗时 %.0fms"
          % (len(result.turns), result.usage.total, result.latency_ms))

    if result.table_stats:
        stats = result.table_stats
        if stats.missing:
            fail("表格记号", "模型漏写了 %s（用户侧已兜底附上，但这是模型的问题）"
                 % "、".join(stats.missing))
        elif stats.unknown:
            fail("表格记号", "模型编造了 %s" % "、".join(stats.unknown))
        else:
            ok("表格记号", "%d 个全部写出" % stats.total)

    if result.link_stats:
        stats = result.link_stats
        if stats.missing_primary:
            fail("链接记号", "漏了 %d 条主链接" % len(stats.missing_primary))
        else:
            ok("链接记号", "%d 条主链接全给" % stats.total_primary)

    _cost(provider)
    return bool(result.ok)


def _cost(provider) -> None:
    if hasattr(provider, "stats"):
        print("  [缓存] " + provider.stats())
        provider = getattr(provider, "inner", None)
    if hasattr(provider, "cost_report"):
        print("  [额度] " + provider.cost_report(UNIT_PRICE))


# ==========================================================================

STAGES = [stage0, stage1, stage2, stage3, stage4, stage5]


def main() -> int:
    ap = argparse.ArgumentParser(description="TravelWise 全流程分级冒烟测试")
    ap.add_argument("--stage", type=int, default=2,
                    help="跑到第几级（0~5）。默认 2，即只跑不花接口钱的部分")
    ap.add_argument("--route", nargs=2, default=["杭州", "武汉"],
                    metavar=("出发地", "目的地"))
    ap.add_argument("--days", type=int, default=0,
                    help="矩阵天数，默认读 .env 的 TRAVELWISE_MATRIX_DAYS")
    ap.add_argument("--export", choices=["csv", "xlsx", "html"], default="xlsx")
    ap.add_argument("--yes", action="store_true", help="所有付费确认自动通过")
    args = ap.parse_args()

    if not args.days:
        from travelwise.config import Settings
        args.days = Settings.from_env().matrix_days

    print("TravelWise 全流程冒烟测试")
    print("航线=%s→%s ｜ 天数=%d ｜ 跑到阶段 %d"
          % (args.route[0], args.route[1], args.days, args.stage))

    for i, stage in enumerate(STAGES):
        if i > args.stage:
            break
        try:
            passed = stage(args)
        except KeyboardInterrupt:
            print("\n已中断。")
            return 130
        except Exception:                        # noqa: BLE001
            print("  [异常] 阶段 %d 抛出未预期异常：" % i)
            traceback.print_exc()
            passed = False
            _results.append(("阶段 %d" % i, False, "未预期异常"))
        if not passed:
            print()
            print("阶段 %d 未通过，**停止**——后面的阶段要花钱，"
                  "先修这一级再继续。" % i)
            break

    print()
    print("=" * 66)
    failed = [name for name, good, _ in _results if not good]
    print("检查项 %d 个，失败 %d 个" % (len(_results), len(failed)))
    for name in failed:
        print("  ✗ %s" % name)
    print("=" * 66)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
