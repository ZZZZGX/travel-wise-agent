# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""TravelWise —— 平台无关的出行决策 Agent。"""

from ._version import STAGE, VERSION
from .orchestrator import TravelWiseAgent
from .state import TravelState, TaskStatus

#: 唯一真相源在 `_version.py`。这里**不要**再写字面量——
#: 上一版就是三处各写各的，最后 pyproject 说 0.1.0、README 说 0.3.0。
__version__ = VERSION
__stage__ = STAGE

__all__ = ["TravelWiseAgent", "TravelState", "TaskStatus", "__version__", "__stage__"]
