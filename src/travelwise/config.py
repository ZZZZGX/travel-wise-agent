# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""config.py —— 运行配置。

两条原则：
  1. 凭证只从环境变量读，绝不写进仓库；
  2. 什么都不配也要能跑（默认 mock），保证 clone 即用。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from .paths import CONFIG_DIR, DATA_DIR, PROJECT_ROOT


def _parse_dotenv_line(line: str) -> tuple[str, str] | None:
    """解析 .env 的一行。返回 (键, 值)，不是赋值行则返回 None。

    这里踩过的坑都是 Windows 上的真实情况，每一个都会让变量**静默丢失**——
    程序照跑，只是当成你没配：

      - **BOM**：记事本默认存 UTF-8 with BOM，于是文件第一个键叫
        "\ufeffTRAVELWISE_..."，永远匹配不上；
      - **行尾注释**：`KEY=value  # 说明` 会把注释一起当成值，
        API Key 后面粘一串中文，鉴权必然失败；
      - **export 前缀**：从 Linux 文档复制来的写法，键名会变成 "export KEY"。
    """
    line = line.strip().lstrip("\ufeff")
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):].lstrip()

    key, value = line.split("=", 1)
    key = key.strip().lstrip("\ufeff")
    value = value.strip()

    if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
        return key, value[1:-1]          # 引号内原样保留，包括 #

    # 未加引号时，「空白 + #」之后的内容视为注释
    for sep in ("  #", "\t#", " #"):
        if sep in value:
            value = value.split(sep, 1)[0]
            break
    return key, value.strip()


def load_dotenv(path: str | None = None) -> None:
    """极简 .env 读取（不引入 python-dotenv，保持零依赖）。

    两条规则，都和 python-dotenv / shell `source` 一致：

      1. **已存在的环境变量优先** —— 命令行 `set XXX=` 显式设置的，
         不该被文件里的值覆盖。
      2. **文件内同名键，后面**有值的**那个覆盖前面的** —— 一个 .env 被追加
         过内容（旧配置在上、新模板在下）时非常常见，而且两边总有一处是空的。
         原来是"前面的赢"，于是上面留空的 KEY 把下面填好的挡掉了，
         表现就是"我明明填了却说没配"。
         但单纯改成"后面的赢"同样会翻车：下面模板里那行空的 KEY
         会反过来把上面填好的值抹掉。所以规则是**空值不覆盖已有值**——
         在这类文件里，空赋值几乎从来不是"我要清空"的意思。

    重复键仍然是配置错误，diagnose_dotenv() 会把它明确报出来。
    """
    path = path or str(PROJECT_ROOT / ".env")
    if not os.path.exists(path):
        return
    parsed_all: dict[str, str] = {}
    try:
        # utf-8-sig：自动吃掉记事本写入的 BOM
        with open(path, encoding="utf-8-sig") as f:
            for line in f:
                parsed = _parse_dotenv_line(line)
                if parsed:
                    key, value = parsed
                    if value or key not in parsed_all:
                        parsed_all[key] = value           # 空值不覆盖已有值
    except OSError:
        return
    for key, value in parsed_all.items():
        os.environ.setdefault(key, value)


def diagnose_dotenv(path: str | None = None) -> dict:
    """体检 .env：到底读了哪个文件、解析出哪些键。**不返回任何值内容。**

    "我明明设了却读不到" 这类问题，靠猜没有意义——把事实摆出来最快。
    """
    root = PROJECT_ROOT
    path = path or str(root / ".env")
    report: dict = {"path": path, "exists": os.path.exists(path),
                    "keys": [], "problems": [], "size": 0}

    # 记事本"另存为"会偷偷加 .txt，文件看着叫 .env，实际是 .env.txt
    for stray in (".env.txt", ".env.example", ".env.local"):
        if os.path.exists(root / stray):
            if stray == ".env.txt":
                report["problems"].append(
                    "发现 .env.txt —— 记事本另存为时自动加了 .txt 后缀，"
                    "程序读的是 .env，这个文件根本没被加载。改名成 .env（去掉 .txt）。")
            elif stray == ".env.example" and not report["exists"]:
                report["problems"].append(
                    "只有 .env.example，没有 .env —— 模板不会被读取。"
                    "先 copy .env.example .env 再把值填进 .env。")

    if not report["exists"]:
        report["problems"].append("%s 不存在，所有配置只能来自系统环境变量。" % path)
        return report

    try:
        raw = open(path, "rb").read()
    except OSError as e:
        report["problems"].append("读取失败：%s" % e)
        return report

    report["size"] = len(raw)
    if raw.startswith(b"\xef\xbb\xbf"):
        report["problems"].append(
            "文件带 UTF-8 BOM（记事本默认行为）。当前代码已能处理，"
            "但建议用 VS Code 存成「UTF-8」而非「UTF-8 with BOM」。")
    if b"\x00" in raw[:200]:
        report["problems"].append(
            "文件疑似 UTF-16 编码（记事本的『Unicode』选项）——整个文件都读不出来。"
            "请另存为 UTF-8。")

    text = raw.decode("utf-8-sig", "replace")
    seen: dict[str, list] = {}
    for lineno, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        parsed = _parse_dotenv_line(line)
        if not parsed:
            report["problems"].append("第 %d 行不是合法的 KEY=VALUE：%s"
                                      % (lineno, stripped[:40]))
            continue
        key, value = parsed
        report["keys"].append({"key": key, "line": lineno,
                               "empty": not value, "length": len(value)})
        seen.setdefault(key, []).append((lineno, bool(value)))
        if "#" in line.split("=", 1)[1] and "#" not in value:
            report["problems"].append("第 %d 行的行尾注释已被正确剥离（%s）" % (lineno, key))
        if value and value != value.strip():
            report["problems"].append("第 %d 行的值首尾有空白（%s）" % (lineno, key))

    # 重复键：文件被追加过内容时最常见，而且几乎总是有一个是空的
    for key, hits in seen.items():
        if len(hits) < 2:
            continue
        lines = "、".join("第%d行%s" % (n, "" if has else "（空值）") for n, has in hits)
        report["problems"].append(
            "键 %s 出现了 %d 次（%s）。生效的是**最后一次**赋值；"
            "请删掉多余的行，只留一行。" % (key, len(hits), lines))

    return report


def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _float_env(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


@dataclass
class Settings:
    flight_provider: str = "mock"          # mock | http
    reminder_provider: str = "console"     # console | ics | json | mcp
    flight_token: str = ""
    flight_config_path: str = ""
    verify_ssl: bool = True
    strict_field_mapping: bool = False     # True 时禁止 HTTP Provider 自动猜字段
    # -- 价格矩阵 / 额度控制 --
    matrix_days: int = 7                   # 向后查几天（每天 1 次额度，直接决定花多少钱）
    flight_cache: bool = True              # 当日缓存：同一天重复查询不再付费
    request_interval: float = 0.0          # 串行请求间隔秒数，防 QPS 限流
    # -- 目的地二层发现 --
    search_provider: str = "none"          # none | fixture | http
    search_config_path: str = ""
    search_fixtures: str = ""
    search_token: str = ""
    search_scenes: str = ""                # 逗号分隔，空则用默认四个场景词
    search_min_mentions: int = 2           # 至少被几条结果提到才算一个地点
    # -- LLM（Phase 1）--
    router: str = "rule"                   # rule | llm
    llm_provider: str = "scripted"         # scripted | anthropic | openai
    llm_model: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_fixtures: str = ""                 # scripted 模式的回放文件

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        return cls(
            flight_provider=os.environ.get("TRAVELWISE_FLIGHT_PROVIDER", "mock").lower(),
            reminder_provider=os.environ.get("TRAVELWISE_REMINDER_PROVIDER", "console").lower(),
            flight_token=os.environ.get("TRAVELWISE_FLIGHT_TOKEN", ""),
            flight_config_path=os.environ.get(
                "TRAVELWISE_FLIGHT_CONFIG", str(CONFIG_DIR / "flight_api.json")),
            verify_ssl=os.environ.get("TRAVELWISE_VERIFY_SSL", "1") != "0",
            strict_field_mapping=os.environ.get("TRAVELWISE_STRICT_FIELDS", "0") == "1",
            matrix_days=_int_env("TRAVELWISE_MATRIX_DAYS", 7),
            flight_cache=os.environ.get("TRAVELWISE_FLIGHT_CACHE", "1") != "0",
            request_interval=_float_env("TRAVELWISE_REQUEST_INTERVAL", 0.0),
            search_provider=os.environ.get("TRAVELWISE_SEARCH_PROVIDER", "none").lower(),
            search_config_path=os.environ.get(
                "TRAVELWISE_SEARCH_CONFIG", str(CONFIG_DIR / "web_search_api.json")),
            search_fixtures=os.environ.get(
                "TRAVELWISE_SEARCH_FIXTURES",
                str(DATA_DIR / "fixtures" / "scene_search.json")),
            search_token=os.environ.get("TRAVELWISE_SEARCH_TOKEN", ""),
            search_scenes=os.environ.get("TRAVELWISE_SEARCH_SCENES", ""),
            search_min_mentions=_int_env("TRAVELWISE_SEARCH_MIN_MENTIONS", 2),
            router=os.environ.get("TRAVELWISE_ROUTER", "rule").lower(),
            llm_provider=os.environ.get("TRAVELWISE_LLM_PROVIDER", "scripted").lower(),
            llm_model=os.environ.get("TRAVELWISE_LLM_MODEL", ""),
            llm_api_key=os.environ.get("TRAVELWISE_LLM_API_KEY", ""),
            llm_base_url=os.environ.get("TRAVELWISE_LLM_BASE_URL", ""),
            llm_fixtures=os.environ.get(
                "TRAVELWISE_LLM_FIXTURES",
                str(PROJECT_ROOT / "evals" / "fixtures" / "llm_responses.json")),
        )

    def scene_list(self):
        """场景词。用户在 .env 里写 `打卡,出片` 就只搜两个，直接决定花几次钱。"""
        items = [x.strip() for x in (self.search_scenes or "").replace("，", ",").split(",")]
        return [x for x in items if x] or None

    def load_search_api_config(self) -> dict:
        try:
            with open(self.search_config_path, encoding="utf-8") as f:
                return json.load(f) or {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def load_flight_api_config(self) -> dict:
        try:
            with open(self.flight_config_path, encoding="utf-8") as f:
                return json.load(f) or {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}


def _token_for(entry: dict, settings: Settings) -> str:
    """每个数据源可以有自己的凭证环境变量。

    多源容错时两家接口的 Key 不可能相同，所以凭证跟着 auth.value_env 走；
    没写就回落到全局的 TRAVELWISE_FLIGHT_TOKEN。凭证仍然只从环境变量读，
    JSON 里永远只出现变量名，不出现值。
    """
    env_key = ((entry.get("auth") or {}).get("value_env")
               or entry.get("token_env") or "")
    if env_key:
        return os.environ.get(env_key, "") or settings.flight_token
    return settings.flight_token


def build_web_search_provider(settings: Settings):
    """按配置造网页搜索源。默认 none —— **不静默降级成假数据**。

    返回的 NullWebSearchProvider 带着 reason，一路传到最终输出里，
    用户看到的是「二层发现未启用，原因是…」，而不是一份不知从哪来的地点清单。
    """
    from .providers.base import ProviderError
    from .providers.web_search import (FixtureWebSearchProvider,
                                       HttpWebSearchProvider,
                                       NullWebSearchProvider)

    mode = (settings.search_provider or "none").lower()
    if mode == "fixture":
        try:
            return FixtureWebSearchProvider(settings.search_fixtures)
        except ProviderError as e:
            return NullWebSearchProvider("回放搜索源不可用：%s" % e)
    if mode == "http":
        cfg = settings.load_search_api_config()
        if not cfg.get("endpoint"):
            return NullWebSearchProvider(
                "TRAVELWISE_SEARCH_PROVIDER=http，但 %s 里没有 endpoint，"
                "二层发现未启用。" % settings.search_config_path)
        token = settings.search_token or os.environ.get(
            ((cfg.get("auth") or {}).get("value_env") or ""), "")
        try:
            return HttpWebSearchProvider(cfg, token=token,
                                         verify_ssl=settings.verify_ssl)
        except ProviderError as e:
            return NullWebSearchProvider(str(e))
    return NullWebSearchProvider()


def build_flight_provider(settings: Settings, today=None):
    """按配置造航班 Provider。默认 mock —— 无 Key 也能完整演示。

    配置里写了 providers 数组时组成**容错链**：第一家失败自动换第二家。
    最外层再套一层当日缓存 —— 0.2 元一次的接口，重复查询不该重复付费。
    """
    if settings.flight_provider == "http":
        from .providers.http_flight import HttpFlightProvider
        cfg = settings.load_flight_api_config()
        entries = cfg.get("providers")
        if not isinstance(entries, list) or not entries:
            entries = [cfg]                       # 单源写法照旧可用，向后兼容

        built = []
        for entry in entries:
            entry = dict(entry or {})
            if entry.get("enabled") is False:
                continue
            if settings.strict_field_mapping:
                entry["strict_field_mapping"] = True
            built.append(HttpFlightProvider(
                entry, token=_token_for(entry, settings),
                verify_ssl=settings.verify_ssl))

        if not built:
            # 一个都没配 —— 交给 HttpFlightProvider 在调用时给出那句明确的报错，
            # 而不是在这里静默回落到 mock（那会让用户以为自己查的是真数据）。
            built = [HttpFlightProvider({}, token=settings.flight_token,
                                        verify_ssl=settings.verify_ssl)]
        if len(built) == 1:
            provider = built[0]
        else:
            from .providers.fallback_flight import FallbackFlightProvider
            provider = FallbackFlightProvider(built)
    else:
        from .providers.mock_flight import MockFlightProvider
        provider = MockFlightProvider(today=today) if today else MockFlightProvider()

    if settings.flight_cache and settings.flight_provider == "http":
        from .providers.cached_flight import CachedFlightProvider
        provider = CachedFlightProvider(provider, today=today)
    return provider


def build_reminder_provider(settings: Settings):
    from .providers.reminders import resolve_reminder_provider
    kind = settings.reminder_provider
    if kind == "ics":
        return resolve_reminder_provider("ics", output_dir=str(DATA_DIR / "cache" / "reminders"))
    if kind in ("json", "jsonfile", "file"):
        return resolve_reminder_provider(
            "json", path=str(DATA_DIR / "cache" / "reminders" / "reminders.json"))
    return resolve_reminder_provider("console")


# --------------------------------------------------------------------------
# LLM / Router 工厂（Phase 1）
# --------------------------------------------------------------------------

_DEFAULT_MODELS = {"anthropic": "claude-sonnet-4-6", "openai": "gpt-4o-mini",
                   "deepseek": "deepseek-chat"}

#: OpenAI 兼容端点的默认地址。注意只写到域名——客户端会自己拼 /v1/chat/completions，
#: 这里再写一次 /v1 就会变成 /v1/v1，是接这类端点最常见的一个 404。
_DEFAULT_BASE_URLS = {"deepseek": "https://api.deepseek.com"}


def build_llm_client(settings: Settings):
    """按配置造 LLM 客户端。

    默认 scripted（离线回放）—— 保证无 Key 也能跑通整条 Tool Calling 链路。
    注意：scripted 产出的是合成数据，不能用来衡量模型质量，只能验证管道。
    """
    provider = settings.llm_provider
    model = settings.llm_model or _DEFAULT_MODELS.get(provider, "")

    if provider == "anthropic":
        from .llm.http_clients import AnthropicClient
        kwargs = {"api_key": settings.llm_api_key, "model": model}
        if settings.llm_base_url:
            kwargs["base_url"] = settings.llm_base_url
        return AnthropicClient(**kwargs)

    # DeepSeek 是 OpenAI 兼容端点，直接复用 OpenAIClient，只是换默认地址与模型。
    if provider in ("openai", "deepseek"):
        from .llm.http_clients import OpenAIClient
        kwargs = {"api_key": settings.llm_api_key, "model": model}
        base = settings.llm_base_url or _DEFAULT_BASE_URLS.get(provider, "")
        if base:
            kwargs["base_url"] = base.rstrip("/")
            if kwargs["base_url"].endswith("/v1"):
                kwargs["base_url"] = kwargs["base_url"][:-3]
        return OpenAIClient(**kwargs)

    from .llm.scripted import ScriptedLLMClient
    from .llm.base import LLMUnavailable
    try:
        return ScriptedLLMClient(fixtures_path=settings.llm_fixtures)
    except LLMUnavailable:
        return ScriptedLLMClient()          # 回放文件缺失时给个空库，调用时会明确报错


def build_router(settings: Settings, client=None):
    """按配置造路由器。llm 模式下失败自动降级到规则路由，且会标记 fell_back。"""
    from .routing.base import RuleRouter
    if settings.router != "llm":
        return RuleRouter()
    from .routing.llm_router import LLMRouter
    return LLMRouter(client or build_llm_client(settings), fallback=RuleRouter())
