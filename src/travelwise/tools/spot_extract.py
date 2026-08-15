# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""spot_extract.py —— 从搜索结果的标题 / 摘要里抽出**地点名**。

## 这是二层发现的第二层

第一层（providers/web_search.py）拿回来的是一堆标题和摘要，形如：

    "昆明这5个出片机位，第3个绝了！翠湖公园的红嘴鸥…"
    "昆明citywalk｜文林街到钱局街，一条街喝三家咖啡"

用户要的不是这些标题，是里面的**地名**：翠湖公园、文林街、钱局街。
抽出来之后按地名各给一个搜索入口，用户就不必自己一条条读帖子筛地方了。

## 为什么用规则而不是丢给 LLM

三个理由，按重要性排：

  1. **地名是可校验的，形容词不是。** 规则抽取的每个候选都能回答
     「它出现在哪几条结果里」，可审计；LLM 抽出来的名字无法区分
     「帖子里写的」和「模型自己想的」，而编造一个不存在的地名是这个功能
     最糟的失败模式——用户按名字搜不到，比不给还差。
  2. **成本。** 这一层每次要处理几十条文本，是最费 token 的位置。
     纯 Python 抽取 0 token，省下的额度留给真正需要推理的地方。
  3. **可测。** 规则的对错能用固定输入断言，不需要联网也不需要模型。

代价是召回不完整：没有后缀词的地名（"滇池"里的"池"不在词表就抽不到）会漏。
所以词表是显式常量、可增补，且**名录里的景区名直接全文匹配**作为补充召回。

## 判据

一个候选要成立，必须同时满足：

  - 以地点后缀词结尾（公园 / 街 / 寺 / 湖 / 咖啡馆 …），且前缀非空；
  - 不是「裸后缀」（"公园"本身不是地名）；
  - 不含攻略话术词（打卡 / 出片 / 攻略 / 宝藏 / 必去 …）——这些是标题修饰，
    "宝藏公园"不是一个能搜到的地方；
  - 被至少 `min_mentions` 条**不同的**搜索结果提到。单条结果提一次
    可能只是那篇作者的私人叫法，或者是抽错了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: 地点后缀词。按长度降序匹配，长的优先（"生态旅游风景区" 先于 "风景区" 先于 "区"）。
#: 增删这里就能调整召回范围，不用改逻辑。
SUFFIXES: tuple[str, ...] = (
    "生态旅游风景区", "国家森林公园", "风景名胜区", "湿地公园", "森林公园",
    "地质公园", "主题公园", "文创园区", "文化园区", "美术馆", "博物馆",
    "图书馆", "科技馆", "纪念馆", "艺术馆", "展览馆", "水族馆", "海洋馆",
    "天文台", "观景台", "观景平台", "咖啡馆", "咖啡店", "面包店", "书店",
    "菜市场", "步行街", "商业街", "美食街", "老街", "古镇", "古城", "古村",
    "风景区", "旅游区", "度假区", "游乐园", "滑雪场", "温泉", "营地",
    "牧场", "农场", "茶山", "梯田", "花海", "花田", "草原", "土林", "石林",
    "瀑布", "峡谷", "雪山", "湿地", "水库", "码头", "渡口", "栈道", "步道",
    "绿道", "沙滩", "海滩", "大桥", "广场", "夜市", "市集", "教堂", "清真寺",
    "影视城", "街区", "公园", "植物园", "动物园", "大学", "学院", "书院",
    "村寨", "山谷", "山庄", "大坝", "水坝", "湖畔", "书屋", "酒吧", "餐厅",
    "面馆", "寺院", "禅寺", "苗寨", "白族乡", "彝族乡", "民族村",
    "湖", "海", "江", "河", "溪", "潭", "池", "泉", "峰", "岭", "岩", "洞",
    "岛", "湾", "滩", "桥", "塔", "寺", "庙", "庵", "宫", "殿", "陵", "碑",
    "楼", "阁", "亭", "院", "府", "园", "村", "寨", "街", "巷", "山", "站",
)

#: 攻略话术 / 平台修饰词。出现在候选名里就直接否掉——
#: "宝藏公园"、"出片机位"不是能搜到的地方，是标题的形容词。
NOISE_WORDS: tuple[str, ...] = (
    "打卡", "出片", "攻略", "宝藏", "必去", "必玩", "必吃", "小众", "免费",
    "推荐", "合集", "盘点", "榜单", "路线", "行程", "地图", "门票", "避雷",
    "人少", "绝美", "绝了", "神仙", "隐藏", "秘境", "天堂", "拍照", "机位",
    "圣地", "旅游", "旅行", "游玩", "一日游", "周末", "特种兵", "美食",
    "好去处", "地方", "景点", "本地人", "第一", "超级", "巨美", "值得",
    "这个", "那个", "几个", "元", "块钱", "免门票", "vlog", "Vlog",
)

#: 只由后缀构成的裸词，以及地名里不该单独成立的通用词。
GENERIC_NAMES: frozenset[str] = frozenset(SUFFIXES) | frozenset({
    "市中心", "老城区", "新城区", "市区", "郊区", "县城", "城区", "景区",
    "公园们", "小公园", "大公园", "小山", "大山", "小湖", "大湖",
    "火车站", "高铁站", "汽车站", "地铁站", "长途汽车站", "机场",
})

#: **动词与虚词**：往前吃前缀时撞上这些字就停。
#:
#: 不加这一条会抽出"尽头是翠湖公园""旁边就是云南大学""经过钱局街"——
#: 地名前面粘着半句话，用户拿它去搜什么都搜不到。
#: 注意这里**不含** 大/小/新/老/多 等字：它们真实地出现在地名里
#: （大理古城、小西门、新迎小区），一并挡掉会误杀。
STOP_CHARS: frozenset[str] = frozenset(
    "的是在和与到去从往至或有就也都把被让使给对为及跟但又还再却而了过着"
    "看逛住吃喝拍玩走坐来带请荐比像等每另此该那这旁边侧附近周"
    "以可能要想会须经沿绕朝俯瞰观赏爬临靠面顺连挑选建议适合值")

#: 数量词开头：「一条街」「三家咖啡馆」不是地名，是数量短语。
_MEASURE_HEAD = re.compile(r"^[一二三四五六七八九十两几百千]{1,2}[条个家只座间张片段处道]")

#: 断句字符：候选名不能跨过它们往前吃。
_BOUNDARY = re.compile(r"[^\u4e00-\u9fa5A-Za-z0-9·]")

#: 候选名允许的字符（中文 / 字母 / 数字 / 间隔号）。
_NAME_CHARS = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9·]")

#: 前缀最多往前吃几个字。太长就不是地名而是整句话了。
MAX_PREFIX = 8


@dataclass
class Candidate:
    """一个候选地点。**证据随身携带**，随时能回答「凭什么说它是个地方」。"""

    name: str
    mentions: int = 0                        # 提到它的搜索结果条数（去重后）
    sources: list = field(default_factory=list)   # [(标题, url), ...]
    queries: list = field(default_factory=list)   # 从哪些查询词里出现
    aliases: list = field(default_factory=list)   # 合并掉的写法，如"翠湖"←"翠湖公园"
    in_catalog: bool = False                 # 是否已在官方 A 级景区名录里
    catalog_level: str = ""

    def to_dict(self) -> dict:
        return {"名称": self.name, "提及次数": self.mentions,
                "别名": self.aliases, "在名录内": self.in_catalog,
                "级别": self.catalog_level,
                "出处": [{"标题": t, "url": u} for t, u in self.sources[:5]]}


def _clean(text: str) -> str:
    """去掉 emoji 与零宽字符，其余原样保留（标点是断句依据，不能删）。"""
    out = []
    for ch in text or "":
        code = ord(ch)
        if 0x1F000 <= code <= 0x1FAFF or code in (0x200B, 0xFE0F, 0x2764):
            out.append(" ")
        elif 0x2600 <= code <= 0x27BF:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _has_noise(name: str) -> bool:
    return any(w in name for w in NOISE_WORDS)


def extract_from_text(text: str, city: str = "",
                      extra_names: tuple = ()) -> list[str]:
    """从一段文字里抽出候选地名。同一段里重复出现只算一次。

    `extra_names` 是全文直接匹配的补充词表（通常是名录里的景区名）——
    补规则抽取的召回缺口：没有后缀词的名字规则抽不到，
    但只要名录里有，就能整词命中。
    """
    text = _clean(text)
    found: list[str] = []
    seen: set[str] = set()

    for name in extra_names:
        if name and len(name) >= 2 and name in text and name not in seen:
            seen.add(name)
            found.append(name)

    consumed = [False] * len(text)
    for suffix in sorted(SUFFIXES, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(suffix, start)
            if i < 0:
                break
            start = i + 1
            end = i + len(suffix)
            if any(consumed[i:end]):
                continue          # 已被更长的后缀吃掉，别重复抽

            j = i
            steps = 0
            while j > 0 and steps < MAX_PREFIX:
                ch = text[j - 1]
                if _BOUNDARY.match(ch) or not _NAME_CHARS.match(ch):
                    break
                if ch in STOP_CHARS:
                    break
                if consumed[j - 1]:
                    break     # 撞上另一个已抽出的地名，别把两个名字连成一个
                j -= 1
                steps += 1
            name = text[j:end].strip("·")

            # 前缀里带数字（"这5个公园"）→ 从数字后面切
            m = list(re.finditer(r"[0-9A-Za-z]+", name[:-len(suffix)] or ""))
            if m:
                name = name[m[-1].end():]
            # 城市名前缀去掉："昆明翠湖公园"和"翠湖公园"是同一个地方，
            # 留着会多出一行，而且用户拿去搜也多余。
            if city and name.startswith(city) and len(name) - len(city) >= 2:
                name = name[len(city):]
            if len(name) < 2 or name == suffix:
                continue
            if name in GENERIC_NAMES or _has_noise(name):
                continue
            if _MEASURE_HEAD.match(name):
                continue
            if city and name == city + suffix:
                continue          # "昆明公园"不是一个地方
            # 整个候选名的跨度都标记为已用：否则"滇池海埂大坝"里的
            # 「海」会再次触发，抽出"滇池海"这种半截名字。
            for k in range(j, end):
                consumed[k] = True
            if name not in seen:
                seen.add(name)
                found.append(name)
    return found


def _merge(cands: dict) -> dict:
    """把同一个地方的不同写法并成一行。两种包含关系，取舍方向相反：

      - **短名是长名的前缀**（翠湖 ⊂ 翠湖公园）→ 保留**长**的：
        多出来的是地点类型，更具体、更好搜。
      - **短名是长名的后缀**（海埂大坝 ⊂ 滇池海埂大坝）→ 保留**提及多**的，
        并列时保留**短**的：多出来的那截通常是上下文（城市名、上位地名），
        不是名字的一部分。

    裸后缀（"公园" ⊂ "翠湖公园"）不会走到这里——早被 GENERIC_NAMES 挡掉了。
    """
    def absorb(host: Candidate, guest: Candidate) -> None:
        for src in guest.sources:
            if src not in host.sources:
                host.sources.append(src)
        host.mentions = len({u or t for t, u in host.sources})
        host.queries.extend(q for q in guest.queries if q not in host.queries)
        if guest.name not in host.aliases:
            host.aliases.append(guest.name)

    names = sorted(cands, key=len, reverse=True)
    dropped: set[str] = set()
    for long in names:
        if long in dropped:
            continue
        for short in names:
            if short == long or short in dropped or len(short) >= len(long):
                continue
            if long.startswith(short):
                if cands[long].mentions >= cands[short].mentions:
                    absorb(cands[long], cands[short])
                    dropped.add(short)
            elif long.endswith(short):
                if cands[short].mentions >= cands[long].mentions:
                    absorb(cands[short], cands[long])
                    dropped.add(long)
                    break
                absorb(cands[long], cands[short])
                dropped.add(short)
    return {k: v for k, v in cands.items() if k not in dropped}


def extract_candidates(hits, city: str = "", min_mentions: int = 2,
                       catalog: dict | None = None,
                       extra_names: tuple = ()) -> list[Candidate]:
    """主入口：一批搜索结果 → 排好序的候选地点。

    `catalog` 是 {景区名: 级别}，用来标注「这个地方名录里本来就有」——
    不是用来过滤：名录里没有恰恰是二层发现存在的理由（咖啡馆、天台、
    老街不会是 A 级景区）。标注出来只是让用户知道哪些是新东西。

    排序：提及次数降序 → 名字长度降序（长名更具体）→ 名称，保证结果稳定可测。
    """
    catalog = catalog or {}
    cands: dict[str, Candidate] = {}

    for hit in hits or []:
        title = getattr(hit, "title", "") or ""
        url = getattr(hit, "url", "") or ""
        query = getattr(hit, "source", "") or ""
        text = getattr(hit, "text", None) or "%s %s" % (title, getattr(hit, "snippet", ""))
        for name in extract_from_text(text, city=city, extra_names=extra_names):
            c = cands.setdefault(name, Candidate(name=name))
            if (title, url) not in c.sources:
                c.sources.append((title, url))
            if query and query not in c.queries:
                c.queries.append(query)

    for c in cands.values():
        # 提及次数按**不同结果**计数：同一篇里出现十次仍是一条证据
        c.mentions = len({u or t for t, u in c.sources})

    cands = _merge(cands)

    out = []
    for c in cands.values():
        if c.mentions < min_mentions:
            continue
        level = catalog.get(c.name) or ""
        c.in_catalog = bool(level)
        c.catalog_level = level
        out.append(c)

    out.sort(key=lambda c: (-c.mentions, -len(c.name), c.name))
    return out
