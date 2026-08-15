# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""router.py —— 意图路由 / 参数抽取 / 范围判断。

设计取舍（重要，别误读成"没做 LLM 所以低级"）：
这里是一个**确定性的规则路由**，理由有三：
  1. 它可以脱网、零成本、可复现地跑，因此**可以被评测**——路由准确率是本项目
     最核心的质量指标之一，需要一个能跑回归的实现；
  2. 它是 LLM 路由的 baseline：将来接入 function calling 后，用同一套评测集
     对比两者，才能证明"上 LLM 确实更好"，而不是凭感觉换技术；
  3. 中文出行请求的意图信号相当稳定（去/飞/机票/玩），规则已能覆盖大部分。

因此 LLM 路由是 Roadmap 的下一步，不是本文件的缺陷。接口已经留好：
`route()` 的输出结构与 LLM 版完全一致，替换时上层无感。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from .state import TaskStatus, TravelState
from .tools import city_codes
from .tools.spot_repository import is_province_name

# --- 意图信号 -------------------------------------------------------------
FLIGHT_WORDS = ["机票", "航班", "买票", "购票", "票价", "飞", "起飞", "航线", "几点的",
                "红眼", "直飞", "转机", "提前几天", "什么时候买"]
DEST_WORDS = ["好玩", "玩什么", "玩的", "适合玩", "有什么地方", "去处", "景点", "景区",
              "打卡", "出片", "拍照", "攻略", "去哪玩", "值得去", "推荐地方", "逛",
              "游玩", "小众", "一日游", "有什么可以", "有啥"]
# 「去/到/飞 + 地名」是强出行信号：即使没说"机票"也应兼顾两件事
STRONG_TRAVEL = re.compile(r"(去|到|飞|前往)\s*([\u4e00-\u9fff]{2,10})")

# --- 参数抽取 -------------------------------------------------------------
# 时间表述会粘在城市名前面（「月底上海去北京」→ 误抽出"月底上海"），
# 因此抽航线前先把时间词从文本里摘掉。日期抽取仍用原文，不受影响。
TIME_NOISE = re.compile(
    r"(\d{4}[-/年]\d{1,2}[-/月]\d{1,2}\s*[日号]?"
    r"|\d{1,2}\s*[-/月]\s*\d{1,2}\s*[日号]?"
    r"|\d{1,2}\s*[日号]"
    r"|今天|明天|后天|大后天|下下周|下个?周|这周|月底|月初|月中"
    r"|一周后|两周后|一个月后|周[一二三四五六日天]|礼拜[一二三四五六日天])")

# 「从A去B」「A飞B」「A到B」「A去B」
ROUTE_PATTERNS = [
    re.compile(r"从\s*([\u4e00-\u9fff]{2,8}?)\s*(?:出发)?\s*(?:去|到|飞往|飞|前往)\s*([\u4e00-\u9fff]{2,8})"),
    re.compile(r"([\u4e00-\u9fff]{2,8}?)\s*(?:飞往|飞|到|去|前往)\s*([\u4e00-\u9fff]{2,8})"),
]
DEST_ONLY = re.compile(r"(?:去|到|飞往|飞|前往)\s*([\u4e00-\u9fff]{2,8})")

# 绝对日期
ABS_DATE = [
    (re.compile(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})"), "ymd"),
    (re.compile(r"(\d{1,2})[-/月](\d{1,2})\s*[日号]?"), "md"),
]
# 相对日期
REL_DATE = {
    "今天": 0, "明天": 1, "后天": 2, "大后天": 3,
    "下周": 7, "下下周": 14, "一周后": 7, "两周后": 14, "一个月后": 30,
}

#: 「下周五」「这礼拜三」这类说法。必须**先于** REL_DATE 匹配：
#: 「下周五」里含有子串「下周」，直接走 REL_DATE 就变成 today+7，
#: 而 today+7 与今天是同一个星期几 —— 用户说周五，系统按周三查价，
#: 还查得理直气壮。这类错比查不到更危险：它有结果，而结果是错的。
WEEKDAY_DATE = re.compile(
    r"(下下|下|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])")
_WEEKDAY_INDEX = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
                  "日": 6, "天": 6}
_WEEK_OFFSET = {"下下": 2, "下": 1, "这": 0, "本": 0}

# 常见把「省」误当城市的词尾
_SEASON_HINT = {"春": 4, "夏": 7, "秋": 10, "冬": 1}
_NOISE_SUFFIX = ("玩", "去", "的", "了", "吗", "呢", "有", "看", "找", "话", "话说")


def _clean_place(name: str) -> str:
    """去掉粘连的动词/语气词尾巴，如「成都玩」→「成都」。"""
    name = (name or "").strip()
    while len(name) > 2 and name.endswith(_NOISE_SUFFIX):
        name = name[:-1]
    return name


# 会粘在地名前面的连接词/副词，抽地名时必须剥掉
_LEAD_NOISE = ("顺便", "看看", "再", "还有", "然后", "另外", "以及", "和", "跟",
               "想去", "打算", "计划", "帮我", "我", "请")

# 不是地名的通用词，抽到就跳过继续往前找
_PLACE_STOPWORDS = {"地方", "什么", "哪里", "哪儿", "景点", "好玩", "附近", "周边", "这里", "那里"}

# 粘在地名后的季节 / 时间尾巴：「成都秋天」→「成都」
_SEASON_TAIL = re.compile(r"(春天|夏天|秋天|冬天|春季|夏季|秋季|冬季|"
                          r"这边|那边|现在|最近|\d{1,2}月)$")


def _extract_place(text: str) -> str | None:
    """抽「想去玩的地方」。只取紧邻询问词前的那个地名，并剥掉连接词。

    「月底去成都，顺便看看四川有什么玩的」→ 四川（而不是"顺便看看四川"）
    """
    matches = list(re.finditer(
        r"([\u4e00-\u9fff]{2,12}?)\s*(?:有什么|有啥|好玩|玩什么|适合玩|景点|值得去|去处)", text))
    if not matches:
        return None

    # 从后往前找第一个"像地名"的候选：中文里限定语紧贴询问词，
    # 但「有什么地方适合玩」会让"地方"成为最后一个匹配，需要跳过这类通用词。
    for m in reversed(matches):
        raw = m.group(1)
        for _ in range(2):                     # 连接词可能叠加，剥两轮
            for noise in _LEAD_NOISE:
                if raw.startswith(noise) and len(raw) > len(noise):
                    raw = raw[len(noise):]
                    break
        raw = _SEASON_TAIL.sub("", raw)        # 「成都秋天」→「成都」
        raw = _clean_place(raw)
        if raw and len(raw) >= 2 and raw not in _PLACE_STOPWORDS:
            return raw
    return None


def looks_like_place(text: str) -> bool:
    """这段话是不是一个「光秃秃的地名」。

    这个谓词原先只住在 `slots.py` 里，也就是说**多轮补槽位时会校验城市名，
    首轮路由却不会**。于是「帮我看看这两天飞成都的票价走势，我从上海出发」
    会抽出 `origin="帮我看看这两天"`，而且 `missing` 是空的——
    系统认为参数齐了，转头拿这坨东西去查航班。

    这不是「抽得不够准」，是**编造了一个用户没说过的值**，
    和本项目写在 README 里的「空字段不冒充默认值」直接冲突。
    规则只留一份，放在两边都能 import 到的位置（slots 依赖 router，
    所以只能往下沉到这里）。

    判断分两层：城市码表认识的直接算数；认不出的也可能是对的
    （159 行的名录本来就不全），但要求它不含动词性噪声。
    宁可放过一个生僻地名，也不要把「帮我看看这两天」当成城市。
    """
    if not text or len(text) > 10:
        return False
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,10}", text):
        return False
    if city_codes.try_resolve(text):
        return True
    return not _NOT_A_PLACE.search(text)


#: 出现这些字样就一定不是地名。人称代词和动词是主力——
#: 「我打算」「帮我看看这两天」这类串能混进城市槽位，全是因为
#: 原来的黑名单只拦疑问词，不拦「说话的人」和「他在干什么」。
_NOT_A_PLACE = re.compile(
    r"(什么|怎么|哪|吗|呢|好玩|机票|航班|高铁|火车|不要|取消|票价|走势"
    r"|我|你|他|她|们|帮|想|打算|知道|看看|问下|求|谢谢|在吗|急)")


def narrow_to_city(text: str | None) -> str | None:
    """把抽出来的一段话收紧成一个真正的城市名，收不出来就返回 None。

    正则抓到的边界经常带渣：「成都的机票」「趟成都」「成都的票价走势」。
    以前这些渣原样进了槽位——mock 数据源不挑食，看起来一切正常；
    真接上 HTTP 数据源时才会在三字码解析那一步炸掉，而那时错误信息
    指向的是数据源，不是路由。

    所以这里的顺序是**先尽力救、再判缺失**：

      1. 整段就认识 → 直接用（「上海市」）
      2. 否则取其中最长的、城市码表认识的一段（「成都的机票」→「成都」）
      3. 都不认识：短且不含噪声的放过（名录只有 159 行，本来就不全），
         其余一律判为没抽到 —— 让上层去问，而不是拿它去查

    第 3 条是分寸所在。收得太紧，生僻城市会被逼着多问一轮（代价：啰嗦）；
    收得太松，「帮我看看这两天」会被当成出发地（代价：编造）。
    这个项目在这两者之间一向选前者。
    """
    if not text:
        return None
    if city_codes.try_resolve(text):
        return text
    # 最长可识别子串。8 个字以内穷举不到 36 种组合，便宜得很。
    best = ""
    for start in range(len(text)):
        for end in range(len(text), start + 1, -1):
            piece = text[start:end]
            if len(piece) > len(best) and city_codes.try_resolve(piece):
                best = piece
    if best:
        return best
    return text if len(text) <= 4 and looks_like_place(text) else None


def extract_date(text: str, today: date | None = None) -> str | None:
    """抽出行日期。抽不到返回 None——**不猜**，由上层去问用户。"""
    today = today or date.today()

    for pattern, kind in ABS_DATE:
        m = pattern.search(text)
        if m:
            try:
                if kind == "ymd":
                    return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
                month, day = int(m.group(1)), int(m.group(2))
                year = today.year + (1 if month < today.month else 0)
                return date(year, month, day).isoformat()
            except ValueError:
                return None

    m = WEEKDAY_DATE.search(text)
    if m:
        prefix, name = m.group(1), m.group(2)
        monday = today - timedelta(days=today.weekday())     # 本周一
        target = monday + timedelta(days=_WEEKDAY_INDEX[name])
        if prefix:
            target += timedelta(weeks=_WEEK_OFFSET[prefix])
        elif target < today:
            # 光说「周五」而本周五已经过了 —— 中文语境里指的是下一个周五。
            # 返回一个过去的日期毫无用处，而且会一路错到出票期分析里。
            target += timedelta(weeks=1)
        return target.isoformat()

    # 长键优先：不排序的话「下下周」会先被「下周」吃掉，差整整一周。
    for word in sorted(REL_DATE, key=len, reverse=True):
        if word in text:
            return (today + timedelta(days=REL_DATE[word])).isoformat()

    # 「月底」「月初」「月中」
    if "月底" in text:
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        return (nxt - timedelta(days=1)).isoformat()
    if "月初" in text:
        nxt = (today.replace(day=28) + timedelta(days=4)).replace(day=1)
        return nxt.isoformat()
    return None


def extract_month_hint(text: str) -> int | None:
    """从「秋天」「十月」这类表述取出用于季节标注的月份。"""
    m = re.search(r"(\d{1,2})\s*月", text)
    if m:
        month = int(m.group(1))
        if 1 <= month <= 12:
            return month
    for word, month in _SEASON_HINT.items():
        if word + "天" in text or word + "季" in text:
            return month
    return None


def detect_intents(text: str) -> list[str]:
    """仅按关键词判断意图。强出行信号的补充判定在 route() 里做，
    因为那需要知道是否抽到了完整航线（见 _apply_travel_signal）。"""
    intents = []
    if any(w in text for w in FLIGHT_WORDS):
        intents.append("flight")
    if any(w in text for w in DEST_WORDS):
        intents.append("destination")
    return intents


def _apply_travel_signal(intents: list[str], text: str,
                         origin: str | None, destination: str | None) -> list[str]:
    """「去 / 飞 / 到 + 地名」强出行信号的补充判定。

    关键区分（否则会把纯机票请求误判成"顺便还要玩"）：
      - 抽到【完整航线】(A→B)，如「月底上海去北京」——这是明确的机票请求，
        用户没提玩什么就不要自作主张塞目的地清单；
      - 只抽到【单个目的地】，如「我下周要去新疆」——诉求不明，
        此时才主动兼顾机票与目的地两件事，缺什么参数再问。
    """
    if not STRONG_TRAVEL.search(text):
        return intents
    has_full_route = bool(origin and destination)
    if not intents:
        return ["flight"] if has_full_route else ["flight", "destination"]
    if "flight" not in intents:
        intents.insert(0, "flight")
    return intents


def decide_scope(place: str, text: str) -> str:
    """判断检索范围。

    铁律：用户说城市就是城市，**绝不因为结果少而自动扩成省**。
    只有当地名本身就是省 / 自治区名时才是 province。
    """
    if not place:
        return "city"
    if is_province_name(place) and not place.endswith("市"):
        return "province"
    if re.search(r"(全省|整个省|省内|全区)", text):
        return "province"
    return "city"


def route(user_request: str, today: date | None = None) -> TravelState:
    """把一句自然语言解析成 TravelState。"""
    text = (user_request or "").strip()
    state = TravelState(user_request=text, current_step=TaskStatus.ROUTED)
    word_intents = detect_intents(text)

    # -- 航线 --（先摘掉时间词，避免"月底上海"这类粘连）
    route_text = TIME_NOISE.sub(" ", text)
    origin = destination = None
    for pattern in ROUTE_PATTERNS:
        m = pattern.search(route_text)
        if m:
            origin, destination = _clean_place(m.group(1)), _clean_place(m.group(2))
            break
    if destination is None:
        m = DEST_ONLY.search(route_text)
        if m:
            destination = _clean_place(m.group(1))
    # 出发地与目的地相同 = 抽错了，宁可判为缺失让上层去问，也不将错就错
    if origin and origin == destination:
        origin = None

    # 收紧成真正的城市名，收不出来就当没抽到。让一坨渣继续往下走，
    # 等于用一个用户没说过的值去查航班——那是编造，不是尽力而为。
    origin, destination = narrow_to_city(origin), narrow_to_city(destination)
    if origin and origin == destination:
        origin = None

    state.intents = _apply_travel_signal(word_intents, text, origin, destination)
    if not state.intents:
        state.current_step = TaskStatus.OUT_OF_SCOPE
        return state

    state.origin = origin
    state.destination = destination
    state.travel_date = extract_date(text, today)

    # -- 玩乐范围（可能与落地城市不同）--
    # 「飞乌鲁木齐，新疆有什么玩的」→ destination=乌鲁木齐，place=新疆
    if "destination" in state.intents:
        state.place = _extract_place(text) or destination
        if state.place:
            state.scope = decide_scope(state.place, text)

    # -- 缺失参数（只问真正需要的）--
    # 规则本身住在 TravelState.recompute_missing()：多轮续跑补完槽位后
    # 也要重算一次，两边必须用同一个定义，否则会出现「单轮对、多轮错」。
    state.recompute_missing()
    return state
