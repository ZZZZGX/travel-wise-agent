# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""路由层：规则基线与 LLM 路由可互换、可对照。"""

from .base import RouteOutcome, Router, RuleRouter
from .llm_router import LLMRouter

__all__ = ["Router", "RouteOutcome", "RuleRouter", "LLMRouter"]
