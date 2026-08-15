# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""_version.py —— 版本号的**唯一**真相源。

为什么单开一个文件：上一版 pyproject 写 0.1.0、README badge 写 v0.3.0 dev、
`__init__.__version__` 写 0.1.0，三处各说各话。人靠自觉同步三个地方，
迟早会漏——而"项目自称的数字和实际跑出来的对不上"这件事，
在一个通篇强调「诚实边界」的仓库里，比数字本身错得更难看。

所以规则改成：

  - 这里是唯一可以手写版本号的地方；
  - `pyproject.toml` 用 dynamic version 读它，`__init__` 从它 import；
  - README 里的 badge 由 `scripts/check_consistency.py` 校验，
    CI 里对不上就红——**漂移变成一个会失败的测试，而不是一次疏忽。**

同理，TESTS / EVALS 这几个数字也不再手写进 README，
而是由 check_consistency.py 实跑一遍再和 badge 比对。
"""

VERSION = "0.8.0"

#: 语义化的阶段标签，仅用于 README badge 与 CLI 自我介绍。
STAGE = "beta"

__all__ = ["VERSION", "STAGE"]
