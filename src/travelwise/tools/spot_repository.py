# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""
spot_cache_manager.py —— 景区名录加载与本地缓存（城市级 / 省·大区级）

数据源：全国 A 级景区名录（scenic_spots.csv，含级别/所在地区/适宜季节/门票/开放时间
等字段）。提供两种【互相独立、由调用方显式选择】的检索粒度：

  1) 城市级 load_city(city)     —— 只返回该城市的景区。查不到就如实返回空，
                                   绝不因数量少而自动扩大范围。
  2) 省/大区级 load_province(p) —— 返回整个省/自治区的景区，按州/地级市分组。

⚠️ 关键设计约束：这两个函数【不会】互相触发、也【不会】根据命中数量自动升级。
   「用户要在某个城市玩」还是「要在整个省玩」是一个业务意图判断，由总管
   （travelwise-orchestrator）依据用户明说的诉求决定调哪个，不在数据层猜。
   例：用户说"去沈阳玩"→ 只调 load_city('沈阳')，哪怕只有 1 个也不扩到辽宁；
       用户说"去辽宁玩"/"新疆有什么好玩的"→ 才调 load_province。

- 源文件默认路径：data/source/scenic_spots.csv
- 缓存目录：data/cache/scenic_spots/
    城市级：{城市}_spots.txt / .json
    省级：  {省}_province.txt / .json
- 城市匹配：优先命中"所在地区"，其次命中详细地址中的"{城市}市/区"，
  规避"大连"误命中"五大连池"这类子串陷阱。
- 省级匹配：省名藏在"详细地址"第一段（如"新疆 伊犁哈萨克自治州 …"），解析
  归一后前缀匹配，不需要额外维护城市→省映射表。
"""

import os
import csv
import json
import re
from collections import OrderedDict

from ..paths import DATA_DIR

_SRC = os.environ.get("TRAVELWISE_SPOT_SRC",
                      str(DATA_DIR / "source" / "scenic_spots.csv"))
_CACHE_DIR = os.environ.get("TRAVELWISE_SPOT_CACHE",
                            str(DATA_DIR / "cache" / "scenic_spots"))

# A 级排序权重
_LEVEL_RANK = {"5A": 5, "4A": 4, "3A": 3, "2A": 2, "1A": 1, "A": 0}

# 省 / 自治区 / 直辖市核心名（用于把用户输入或数据字段归一到同一个 key）
_PROVINCE_CORES = [
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "广西", "海南", "四川", "贵州", "云南", "西藏", "陕西", "甘肃",
    "青海", "宁夏", "新疆", "内蒙古", "香港", "澳门",
]


def _level_rank(level):
    return _LEVEL_RANK.get((level or "").strip().upper(), -1)


# 名录文件可能是 UTF-8(带/不带 BOM) 或 GBK/GB18030（Excel 中文默认另存常见）。
# 按顺序试探，取第一个能解码的，避免因编码不符整模块崩溃。
_SRC_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")


def load_source(src=_SRC):
    last_err = None
    for enc in _SRC_ENCODINGS:
        try:
            with open(src, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError as e:
            last_err = e
            continue
    # 所有候选编码都失败：如实抛出，附排查提示，绝不静默返回空。
    raise UnicodeError(
        "无法解码名录文件 %s（已尝试 %s）。请确认其为 UTF-8 或 GBK 编码。原始错误：%s"
        % (src, "/".join(_SRC_ENCODINGS), last_err)
    )


# ---------------------------------------------------------------------------
# 城市级检索（原有逻辑，保持不变）
# ---------------------------------------------------------------------------

def _region_hit(city, region):
    """
    城市名与'所在地区'字段是否匹配。用【前缀】而非【包含】，规避子串陷阱：
      - 地级市/州/盟/地区/自治州 的核心名恒在字段开头（'大连市''兴安盟''迪庆藏族自治州'）。
      - 前缀匹配可正确命中'鞍山'→'鞍山市'，同时排除'鞍山'误命中'马鞍山市'；
        '兴安'→'兴安盟' 命中，而'兴安'误命中'大兴安岭地区' 被排除。
    """
    region = (region or "").strip()
    return region.startswith(city)


def _addr_hit(city, addr):
    """
    详细地址里出现'{城市}市'或'{城市}区'才算命中，且要求其左侧不是别的汉字，
    避免'鞍山市'被'马鞍山市'这种更长地名裹挟造成误命中。
    """
    for token in (city + "市", city + "区"):
        idx = addr.find(token)
        while idx != -1:
            prev = addr[idx - 1] if idx > 0 else ""
            # 左侧为空/空白/标点即视为词首（真正的'{城市}市'），否则跳过继续找
            if not ("\u4e00" <= prev <= "\u9fff"):
                return True
            idx = addr.find(token, idx + 1)
    return False


def match_city(rows, city):
    """返回该城市的景区行，按级别从高到低排序。严格匹配，不做任何范围扩大。"""
    city = (city or "").strip()
    if not city:
        return []
    hits = []
    for x in rows:
        region = x.get("所在地区", "") or ""
        addr = " ".join(filter(None, [x.get("详细地址", ""), x.get("地址", "")]))
        if _region_hit(city, region) or _addr_hit(city, addr):
            hits.append(x)
    hits.sort(key=lambda r: _level_rank(r.get("级别")), reverse=True)
    return hits


# ---------------------------------------------------------------------------
# 省 / 大区级检索（新增）
# ---------------------------------------------------------------------------

def _province_key(text):
    """
    把一段地址或一个省名归一成省核心 key。
    - "四川省 甘孜藏族自治州 …" -> "四川"
    - "新疆 伊犁哈萨克自治州 …" -> "新疆"
    - "辽宁省" -> "辽宁"；"北京市 海淀区 …" -> "北京"
    - "内蒙古自治区 …" -> "内蒙古"
    识别不出返回 ""。
    """
    if not text:
        return ""
    first = re.split(r"[ \u3000]", text.strip(), maxsplit=1)[0]
    for core in _PROVINCE_CORES:
        if first.startswith(core) or text.strip().startswith(core):
            return core
    return ""


def _row_province(row):
    """取一行数据所属省核心 key（详细地址优先，其次地址列）。"""
    return _province_key(row.get("详细地址", "") or row.get("地址", "") or "")


def _prefecture(row):
    """州 / 地级市（用于省级结果分组）；去掉'等'、留空则归'其他'。"""
    return (row.get("所在地区", "") or "").replace("等", "").strip() or "其他"


def is_province_name(text):
    """判断一个词是否是省/自治区/直辖市名（供总管校验用户给的是省还是市）。"""
    return _province_key(text) != ""


def match_province(rows, province):
    """
    返回该省/自治区所有景区行，按级别从高到低排序。
    province 可传"辽宁""辽宁省""新疆""新疆维吾尔自治区"等，内部归一。
    """
    key = _province_key(province)
    if not key:
        return []
    hits = [x for x in rows if _row_province(x) == key]
    hits.sort(key=lambda r: _level_rank(r.get("级别")), reverse=True)
    return hits


def _slim(row):
    """只保留策展需要的字段。"""
    return {
        "名称": row.get("名称"),
        "级别": row.get("级别"),
        "所在地区": row.get("所在地区"),
        "适宜季节": (row.get("适宜季节") or "").strip(),
        "大门票参考": (row.get("大门票参考") or "").strip(),
        "开放时间": (row.get("开放时间") or "").strip(),
        "建议游玩时间": (row.get("建议游玩时间") or "").strip(),
        "详细地址": (row.get("详细地址") or row.get("地址") or "").strip(),
    }


# ---------------------------------------------------------------------------
# 城市级缓存 / 加载
# ---------------------------------------------------------------------------

def build_cache(city, src=_SRC, cache_dir=_CACHE_DIR):
    """按城市生成缓存（txt + json），返回精简后的景区列表。
    缓存写入失败（如目录无写权限）不影响返回——数据照常给出，只是不落盘。"""
    rows = match_city(load_source(src), city)
    slim = [_slim(r) for r in rows]
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "%s_spots.json" % city), "w", encoding="utf-8") as f:
            json.dump(slim, f, ensure_ascii=False, indent=2)
        with open(os.path.join(cache_dir, "%s_spots.txt" % city), "w", encoding="utf-8") as f:
            f.write("【%s · 官方 A 级景区名录】共 %d 个\n\n" % (city, len(slim)))
            for s in slim:
                f.write("%s（%s）\n" % (s["名称"], s["级别"]))
                if s["适宜季节"]:
                    f.write("  适宜季节：%s\n" % s["适宜季节"])
                if s["大门票参考"]:
                    f.write("  门票参考：%s\n" % s["大门票参考"])
                if s["开放时间"]:
                    f.write("  开放时间：%s\n" % s["开放时间"])
                f.write("\n")
    except OSError:
        pass  # 无写权限/只读文件系统等：静默跳过缓存，不阻断主流程
    return slim


def load_city(city, src=_SRC, cache_dir=_CACHE_DIR, rebuild=False):
    """
    城市级加载：优先读缓存；无缓存或 rebuild=True 时重建。
    返回 slim 列表（可能为空——名录未收录该城市时，如实为空，不扩范围）。
    """
    cache_json = os.path.join(cache_dir, "%s_spots.json" % city)
    if not rebuild and os.path.exists(cache_json):
        with open(cache_json, encoding="utf-8") as f:
            return json.load(f)
    return build_cache(city, src=src, cache_dir=cache_dir)


# ---------------------------------------------------------------------------
# 省级缓存 / 加载
# ---------------------------------------------------------------------------

def build_province_cache(province, src=_SRC, cache_dir=_CACHE_DIR):
    """按省生成缓存（txt + json），返回 {province, groups, flat} 结构。"""
    key = _province_key(province)
    rows = match_province(load_source(src), province)
    flat = [_slim(r) for r in rows]

    groups = OrderedDict()
    for r in rows:
        groups.setdefault(_prefecture(r), []).append(_slim(r))

    payload = {"province": key, "groups": groups, "flat": flat}
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "%s_province.json" % key), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(os.path.join(cache_dir, "%s_province.txt" % key), "w", encoding="utf-8") as f:
            f.write("【%s · 全省 A 级景区名录】共 %d 个，按州/地区分组\n\n" % (key, len(flat)))
            for pref, items in groups.items():
                f.write("〔%s〕\n" % pref)
                for s in items:
                    f.write("  %s（%s）" % (s["名称"], s["级别"]))
                    f.write("　适宜季节：%s\n" % (s["适宜季节"] or "未标注"))
                f.write("\n")
    except OSError:
        pass  # 无写权限等：静默跳过缓存，不阻断主流程
    return payload


def load_province(province, src=_SRC, cache_dir=_CACHE_DIR, rebuild=False):
    """
    省/大区级加载：优先读缓存；无缓存或 rebuild=True 时重建。
    返回 {"province": 归一省名, "groups": OrderedDict{州/地区:[slim...]}, "flat":[slim...]}。
    flat 为空表示名录未收录该省（或传入的不是省名）。
    """
    key = _province_key(province)
    if not key:
        return {"province": "", "groups": OrderedDict(), "flat": []}
    cache_json = os.path.join(cache_dir, "%s_province.json" % key)
    if not rebuild and os.path.exists(cache_json):
        with open(cache_json, encoding="utf-8") as f:
            data = json.load(f)
        data["groups"] = OrderedDict(data.get("groups", {}))
        return data
    return build_province_cache(province, src=src, cache_dir=cache_dir)


if __name__ == "__main__":
    import sys
    scope = sys.argv[1] if len(sys.argv) > 1 else "city"
    query = sys.argv[2] if len(sys.argv) > 2 else "大连"
    if scope == "province":
        data = load_province(query, rebuild=True)
        print("【省级】%s 命中 %d 个，按 %d 个州/地区分组：" %
              (data["province"] or query, len(data["flat"]), len(data["groups"])))
        for pref, items in data["groups"].items():
            print("  [%s] %s" % (pref, "、".join(s["名称"] for s in items)))
    else:
        spots = load_city(query, rebuild=True)
        print("【城市】%s 命中 %d 个官方景区：" % (query, len(spots)))
        for s in spots[:10]:
            print(" -", s["名称"], s["级别"], "｜适宜季节:", s["适宜季节"] or "未标注")
