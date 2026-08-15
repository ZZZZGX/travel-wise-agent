# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""
city_codes.py —— 中文城市名 → 城市三字码（IATA 城市码）解析

背景：付费机票接口 /flight/detail 的 depCode/arrCode 要的是【城市三字码】
（北京=BJS、上海=SHA、乌鲁木齐=URC），而用户说的是中文城市名。
本模块把名字转成码，数据来自 data/source/city_codes.csv（可自行增删行扩展）。

设计原则
- 只做名字→码的确定性映射，查不到就【明确报错】，绝不猜一个码（猜错会查错城市、白烧额度）。
- 若传入的本身已是 3 个大写字母（如 "URC"），视为已是三字码，原样返回。
- 处理常见后缀（市/地区/自治州/盟/省…）与别名（伊犁→YIN、香格里拉/迪庆→DIG…）。
"""

import os
import csv

from ..paths import DATA_DIR

_SRC = os.environ.get("TRAVELWISE_CITYCODE_SRC",
                      str(DATA_DIR / "source" / "city_codes.csv"))
_SRC_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk")

# 去掉这些行政后缀后再匹配（长的在前，先去长的）
_SUFFIXES = ("特别行政区", "自治州", "自治区", "地区", "盟", "市", "县", "区", "省")

_TABLE = None  # {名字或别名: 三字码}


def _read_rows(src=_SRC):
    last = None
    for enc in _SRC_ENCODINGS:
        try:
            with open(src, encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except UnicodeDecodeError as e:
            last = e
            continue
    raise UnicodeError("无法解码 %s（试过 %s）：%s"
                       % (src, "/".join(_SRC_ENCODINGS), last))


def _strip_suffix(name):
    for suf in _SUFFIXES:
        if len(name) > len(suf) and name.endswith(suf):
            return name[: -len(suf)]
    return name


def load_table(src=_SRC, force=False):
    """加载并缓存 {名字/别名 → 三字码}。名字与其去后缀形式都入表。"""
    global _TABLE
    if _TABLE is not None and not force:
        return _TABLE
    table = {}

    def _put(key, code):
        key = (key or "").strip()
        if key:
            table.setdefault(key, code)
            table.setdefault(_strip_suffix(key), code)

    for row in _read_rows(src):
        code = (row.get("三字码") or "").strip().upper()
        if not code:
            continue
        _put(row.get("城市名"), code)
        for alias in (row.get("别名") or "").split(";"):
            _put(alias, code)
    _TABLE = table
    return table


class CityCodeError(Exception):
    """无法把城市名解析成三字码。"""


def resolve(city, src=_SRC):
    """
    中文城市名 → 三字码。
    - 已是 3 个大写字母 → 原样返回（用户直接给了码）。
    - 否则查表：原名 → 去后缀 → 别名。查不到抛 CityCodeError（附建议），不猜。
    """
    city = (city or "").strip()
    if not city:
        raise CityCodeError("城市名为空")
    if len(city) == 3 and city.isalpha() and city.isupper():
        return city  # 传入的已是三字码

    table = load_table(src)
    for key in (city, _strip_suffix(city)):
        if key in table:
            return table[key]
    raise CityCodeError(
        "城市「%s」不在三字码表里，无法转成机票接口需要的三字码。"
        "请确认城市名，或在 data/source/city_codes.csv 增补该城市及其三字码后重试。" % city)


def try_resolve(city, src=_SRC):
    """解析成功返回三字码，失败返回 None（不抛错），供上层自行处理。"""
    try:
        return resolve(city, src)
    except CityCodeError:
        return None


if __name__ == "__main__":
    import sys
    tests = sys.argv[1:] or ["北京", "上海市", "乌鲁木齐", "伊犁", "香格里拉",
                             "库尔勒", "巴音郭楞", "URC", "火星"]
    for t in tests:
        try:
            print("%-10s -> %s" % (t, resolve(t)))
        except CityCodeError as e:
            print("%-10s -> 查不到（%s）" % (t, str(e)[:30]))
