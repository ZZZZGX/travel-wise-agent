# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""smoke_discovery.py —— 景区二层发现的分级冒烟测试。

## 为什么分级

这条链路上只有一处花钱：网页搜索接口（每个场景词 1 次调用）。
所以和 smoke_full_flow.py 一样按【花费从零到有】排，前一级不过就不往下走：

    阶段 0  配置体检        0 元   —— 搜索源配成了什么、名录读不读得到
    阶段 1  抽取规则自检    0 元   —— 固定输入 → 固定输出，只测规则本身
    阶段 2  回放全链路      0 元   —— 用 data/fixtures/scene_search.json 跑通二层
    阶段 3  降级行为        0 元   —— 没搜索源时**必须说未启用**，不能编清单
    阶段 4  真实搜索源      N 次   —— 只有这一级花钱，默认不跑

## 用法（项目根目录）

    python scripts\\smoke_discovery.py                        # 跑 0~3，一分钱不花
    python scripts\\smoke_discovery.py --place 大理           # 换城市（回放里有大理）
    python scripts\\smoke_discovery.py --stage 4 --place 昆明 # 打真实接口，会先报价确认
    python scripts\\smoke_discovery.py --stage 4 --yes        # 不确认直接打
    python scripts\\smoke_discovery.py --scenes 打卡,出片     # 只搜两个词 = 只花两次

阶段 4 需要先配好 `.env`：

    TRAVELWISE_SEARCH_PROVIDER=http
    TRAVELWISE_SEARCH_TOKEN=你的key
    TRAVELWISE_SEARCH_CONFIG=config\\web_search_api.json
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

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
        print("  （--yes 已开启，自动确认）")
        return True
    try:
        return input("  %s [y/N] " % question).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ------------------------------------------------------------ 阶段 0
def stage0(args) -> bool:
    head(0, "配置体检", "0 元")
    from travelwise.config import Settings, build_web_search_provider
    from travelwise.tools.spot_repository import load_city

    settings = Settings.from_env()
    provider = build_web_search_provider(settings)
    print("  搜索源模式 = %s → 实际拿到 %s（enabled=%s）"
          % (settings.search_provider, provider.name, provider.enabled))
    if not provider.enabled:
        print("  未启用原因：%s" % getattr(provider, "reason", "-"))
    print("  场景词      = %s" % (settings.scene_list() or "默认四个（打卡/出片/小众景点/citywalk）"))
    print("  提及门槛    = ≥%d 条结果" % settings.search_min_mentions)
    print("  回放文件    = %s" % settings.search_fixtures)

    fixture = Path(settings.search_fixtures)
    if not fixture.exists():
        return fail("回放文件存在", "找不到 %s，阶段 2 会跑不了" % fixture)
    ok("回放文件存在", str(fixture.name))

    try:
        official = load_city(args.place)
    except Exception as e:                                   # noqa: BLE001
        return fail("名录可读", str(e))
    ok("名录可读", "「%s」收录 %d 条 A 级景区" % (args.place, len(official)))
    if not official:
        print("     ↑ 名录没收录这个城市，二层仍然能跑，只是所有地点都会标成「名录外」。")
    return True


# ------------------------------------------------------------ 阶段 1
#: 固定输入 → 期望。每一条都对应一个**真实踩过的坑**，
#: 不是为了凑覆盖率，而是防止改词表时把已经修好的东西改回去。
EXTRACT_CASES = [
    ("昆明这5个出片机位，第3个绝了！翠湖公园的红嘴鸥太治愈",
     ["翠湖公园"], ["出片机位", "5个出片机位"], "序号 + 话术前缀不能粘进名字"),
    ("从文林街出发，经过钱局街，尽头是翠湖公园",
     ["文林街", "钱局街", "翠湖公园"], ["是翠湖公园", "过钱局街"], "动词不能粘进名字"),
    ("昆明citywalk｜文林街到钱局街，一条街喝三家咖啡",
     ["文林街", "钱局街"], ["一条街", "三家咖啡"], "数量短语不是地名"),
    ("西山森林公园可以俯瞰滇池",
     ["西山森林公园", "滇池"], ["林公园可以俯瞰滇池"], "相邻的两个地名不能连成一个"),
    ("昆明宝藏公园合集｜必去打卡机位",
     [], ["宝藏公园", "打卡机位"], "营销词不是地方"),
    ("推荐昆明翠湖公园",
     ["翠湖公园"], ["昆明翠湖公园"], "城市名前缀要剥掉"),
]


def stage1(args) -> bool:
    head(1, "抽取规则自检（纯函数，固定输入）", "0 元 0 token")
    from travelwise.tools import spot_extract

    all_good = True
    for text, want, reject, why in EXTRACT_CASES:
        got = spot_extract.extract_from_text(text, city="昆明")
        missing = [w for w in want if w not in got]
        leaked = [r for r in reject if r in got]
        label = "%s ｜ %s" % (text[:22], why)
        if missing or leaked:
            all_good = fail(label, "缺 %s ｜ 多 %s ｜ 实得 %s" % (missing, leaked, got))
        else:
            ok(label, "→ %s" % (got or "（空，符合预期）"))
    return all_good


# ------------------------------------------------------------ 阶段 2
def _run_discovery(place, provider, args, use_cache=True):
    from travelwise.tools import destination_search
    return destination_search.curate(
        place, scope="city", search_provider=provider,
        scenes=(args.scenes.replace("，", ",").split(",") if args.scenes else None),
        min_mentions=args.min_mentions, today=date.today())


def stage2(args) -> bool:
    head(2, "回放全链路（本地 JSON，不联网）", "0 元 0 token")
    from travelwise.config import Settings
    from travelwise.providers.web_search import FixtureWebSearchProvider
    from travelwise.tools import destination_search, scene_discovery

    settings = Settings.from_env()
    provider = FixtureWebSearchProvider(settings.search_fixtures)
    data = _run_discovery(args.place, provider, args)
    disc = data["discovery"]

    if not disc["enabled"]:
        return fail("二层启用", disc["reason"])
    ok("二层启用", "读到 %d 条搜索结果" % disc["hits"])

    if not disc["spots"]:
        return fail("抽出地点", "一个都没有——回放文件里可能没有「%s」这个城市的数据，"
                                "换 --place 或往 fixtures 里加" % args.place)
    ok("抽出地点", "%d 个（候选 %d 个，门槛 ≥%d 次提及）"
       % (len(disc["spots"]), disc["candidates_total"], disc["min_mentions"]))

    bad = [s["名称"] for s in disc["spots"]
           if not s["links"]["web"].startswith("https://") or "keyword=" not in s["links"]["web"]]
    if bad:
        return fail("每个地点都有可点入口", "这些没有：%s" % bad)
    ok("每个地点都有可点入口")

    evidenceless = [s["名称"] for s in disc["spots"] if not s["出处"]]
    if evidenceless:
        return fail("每个地点都带证据", "这些没出处：%s" % evidenceless)
    ok("每个地点都带证据", "可回答「凭什么说它是个地方」")

    print()
    print("  ---- 二层输出（就是用户会看到的） " + "-" * 26)
    for line in scene_discovery.render(disc):
        print("  " + line)
    print()
    print("  ---- 证据抽样（前 3 个地点的出处） " + "-" * 25)
    for s in disc["spots"][:3]:
        print("  %s ← %s" % (s["名称"], " ｜ ".join(e["标题"][:24] for e in s["出处"][:3])))

    text = destination_search.render(data)
    if text.index("按场景检索（一层") > text.index("场景发现（二层"):
        return fail("一层在前、二层在后", "顺序反了")
    return ok("整体报告拼接正常", "共 %d 行" % len(text.splitlines()))


# ------------------------------------------------------------ 阶段 3
def stage3(args) -> bool:
    head(3, "降级行为（没搜索源时不能假装做过）", "0 元")
    from travelwise.skills.destination import DestinationSkill

    res = DestinationSkill().run(args.place, scope="city", today=date.today())
    if not res["ok"]:
        return fail("无搜索源仍能出结果", res.get("error", ""))
    if res["discovery_enabled"]:
        return fail("二层如实标记未启用", "没配搜索源却报告已启用")
    ok("二层如实标记未启用")
    if res["discovered_count"]:
        return fail("未启用时不产出地点", "居然给了 %d 个地点" % res["discovered_count"])
    ok("未启用时不产出地点", "0 个——没有编造")
    if "未启用" not in res["text"]:
        return fail("用户可见处写明未启用", "报告里没有这句话")
    ok("用户可见处写明未启用")
    if not res["official_count"]:
        print("     注意：名录也是空的（这个城市没收录），所以这次只有关键词入口。")
    else:
        ok("一层照常工作", "名录 %d 条 + 关键词入口" % res["official_count"])

    for line in res["text"].splitlines():
        if "未启用" in line or "二层" in line:
            print("  > %s" % line)
    return True


# ------------------------------------------------------------ 阶段 4
def stage4(args) -> bool:
    from travelwise.config import Settings, build_web_search_provider
    from travelwise.tools import scene_discovery

    settings = Settings.from_env()
    scenes = (args.scenes.replace("，", ",").split(",") if args.scenes
              else list(scene_discovery.DEFAULT_SCENES))
    head(4, "真实搜索源", "%d 次搜索调用" % len(scenes))

    provider = build_web_search_provider(settings)
    if not provider.enabled:
        return fail("搜索源可用", getattr(provider, "reason", "未配置"))
    if provider.name == "fixture":
        print("  当前是回放源，不会真的联网——阶段 4 与阶段 2 等价。")

    print("  将对「%s」搜索 %d 个场景词：%s"
          % (args.place, len(scenes), "、".join(scenes)))
    print("  同一天重复跑会命中缓存，不再付费（缓存目录 data/cache/discover）。")
    if not confirm("确认发起真实搜索？", args.yes):
        print("  已跳过。")
        return True

    data = _run_discovery(args.place, provider, args)
    disc = data["discovery"]
    if not disc["enabled"]:
        return fail("二层启用", disc["reason"])
    for err in disc.get("errors") or []:
        print("  ⚠️ %s" % err)
    if not disc["hits"]:
        return fail("搜索有返回", "0 条结果——检查 response.list_path 与 field_map 是否对上")
    ok("搜索有返回", "%d 条（实际调用 %d 次，缓存=%s）"
       % (disc["hits"], disc["api_calls"], disc["cached"]))

    if not disc["spots"]:
        print("  ⚠️ 有结果但没抽出地点。两种可能：")
        print("     a) field_map 对错了，title/snippet 拿到的是空串；")
        print("     b) 真实标题的文风与回放数据差太远，SUFFIXES 覆盖不到。")
        print("     缓存里有原始标题，直接看：data/cache/discover/")
        return fail("抽出地点", "0 个")
    ok("抽出地点", "%d 个" % len(disc["spots"]))

    print()
    for line in scene_discovery.render(disc):
        print("  " + line)
    return True


STAGES = [stage0, stage1, stage2, stage3, stage4]


def main() -> int:
    ap = argparse.ArgumentParser(description="景区二层发现分级冒烟测试")
    ap.add_argument("--stage", type=int, default=3,
                    help="跑到第几级（默认 3，即完全不花钱）")
    ap.add_argument("--place", default="昆明", help="测试城市（默认昆明）")
    ap.add_argument("--scenes", default="", help="逗号分隔的场景词，直接决定花几次钱")
    ap.add_argument("--min-mentions", type=int, default=2,
                    help="至少被几条结果提到才算一个地点（默认 2）")
    ap.add_argument("--yes", action="store_true", help="付费确认自动通过")
    args = ap.parse_args()

    print("TravelWise 景区发现冒烟测试")
    print("城市=%s ｜ 跑到阶段 %d ｜ 提及门槛 ≥%d"
          % (args.place, args.stage, args.min_mentions))

    for i, stage in enumerate(STAGES):
        if i > args.stage:
            break
        try:
            passed = stage(args)
        except KeyboardInterrupt:
            print("\n已中断。")
            return 130
        except Exception:                                    # noqa: BLE001
            print("  [异常] 阶段 %d 抛出未预期异常：" % i)
            traceback.print_exc()
            passed = False
            _results.append(("阶段 %d" % i, False, "未预期异常"))
        if not passed:
            print()
            print("阶段 %d 未通过，**停止**——先修这一级再往下。" % i)
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
