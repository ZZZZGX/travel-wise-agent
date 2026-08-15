# 景区二层发现 · 测试教程

> 目标：在**不花一分钱**的前提下把整条链路验到底，只有最后一步才碰真实搜索接口。

## 0. 这一层到底在测什么

二层发现只有三种糟糕的失败方式，测试全部围绕它们展开：

| 失败方式 | 后果 | 挡在哪 |
|---|---|---|
| 抽出不存在的地方（「宝藏公园」「尽头是翠湖公园」） | 用户按名字搜不到，比不给还差 | 阶段 1 + `TestExtractRules` |
| 假装做了发现（没搜索源却给清单） | 那份清单只能是编的 | 阶段 3 + `TestDegradation` |
| 重复付费（同城同日反复调接口） | 花冤枉钱 | `TestCache` |

「抽得全不全」反而是次要的——漏一个地名，用户少一个入口；编一个地名，用户白跑一趟。

## 1. 五分钟跑通（0 元）

在项目根目录：

```bat
cd /d D:\你的路径\travelwise-agent
set PY=E:\1comfyui\ComfyUI-aki-v3\ComfyUI-aki-v3\python\python.exe

REM ① 单元测试：228 个，全离线
"%PY%" -m unittest discover -s tests -p "test_*.py"

REM ② 只跑景区这一组，看每条规则的名字
"%PY%" -m unittest tests.test_scene_discovery -v

REM ③ 分级冒烟：阶段 0~3，一分钱不花
"%PY%" scripts\smoke_discovery.py
```

第 ③ 步的输出会依次给你：配置体检 → 抽取规则逐条自检 → 回放全链路（含用户真正会看到的那段输出）→ 降级行为验证。

## 2. `smoke_discovery.py` 的五个阶段

和 `smoke_full_flow.py` 一样按**花费从零到有**排，前一级不过就不往下走。

| 阶段 | 内容 | 花费 |
|---|---|---|
| 0 | 配置体检：搜索源配成了什么、名录读不读得到、回放文件在不在 | 0 元 |
| 1 | 抽取规则自检：6 组固定输入 → 期望输出 | 0 元 |
| 2 | 回放全链路：读本地 JSON 跑通「搜索 → 抽取 → 给入口」 | 0 元 |
| 3 | 降级行为：没搜索源时**必须**说未启用，且不产出任何地点 | 0 元 |
| 4 | 真实搜索源：默认不跑，会先报价并要你确认 | N 次调用 |

常用参数：

```bat
"%PY%" scripts\smoke_discovery.py                          REM 默认跑到阶段 3
"%PY%" scripts\smoke_discovery.py --place 大理             REM 换城市（回放里有大理）
"%PY%" scripts\smoke_discovery.py --min-mentions 1         REM 放宽门槛，看被过滤掉了什么
"%PY%" scripts\smoke_discovery.py --stage 4 --scenes 打卡,出片   REM 真实接口，只花 2 次
```

`--min-mentions 1` 很值得跑一次：它把被门槛砍掉的候选放出来，你能直观看到「门槛调低会混进什么噪声」，从而判断 2 这个默认值合不合适。

## 3. 不花钱地验证「换城市」

回放源 `data/fixtures/scene_search.json` 就是可编辑的测试数据。加一个城市只要加几组键：

```json
{
  "成都 打卡": [
    {"title": "成都打卡｜宽窄巷子和人民公园的盖碗茶",
     "snippet": "人民公园的鹤鸣茶社很出名，宽窄巷子晚上人多。",
     "url": "https://example.com/cd/1"}
  ],
  "成都 出片": [ ... ]
}
```

键是查询词，支持子串匹配（查询 `成都 打卡` 会命中键 `成都 打卡`）。

**把 title / snippet 直接从真实搜索结果里抄过来最有价值**——抽取规则的对错只有在真实标题的文风上才测得准。造得太干净的假数据会让规则显得比实际好用。

然后：

```bat
"%PY%" scripts\smoke_discovery.py --place 成都 --stage 2
```

## 4. 全流程测试

二层已经接进了原有的 `smoke_full_flow.py`：阶段 0 会报告搜索源状态，阶段 1 的离线全流程会顺带验证二层。

```bat
REM 不启用二层（默认，与改造前行为一致）
"%PY%" scripts\smoke_full_flow.py --stage 1

REM 启用回放源，全流程含二层，仍然 0 元
set TRAVELWISE_SEARCH_PROVIDER=fixture
"%PY%" scripts\smoke_full_flow.py --stage 1 --route 上海 昆明
```

第二条会多出一行 `[ OK ] 场景发现（二层）  抽出 10 个地点`。

再看 CLI 的真实输出（这是用户实际会读到的东西）：

```bat
set TRAVELWISE_SEARCH_PROVIDER=fixture
"%PY%" -m travelwise "昆明有什么好玩的"
"%PY%" -m travelwise "8月28号从上海飞昆明，机票什么时候买，昆明有什么好玩的"
```

Agent 闭环模式（模型自己选工具）：

```bat
"%PY%" -m travelwise --agent-loop "昆明有什么好玩的"
```

工具返回值里多了 `discovery_enabled` 和 `discovered_count`，模型据此能说清「这些地点是搜出来的还是名录里的」，二层没启用时也不会假装做过。

## 5. 接真实搜索接口（阶段 4）

### 5.1 配置

```bat
copy config\web_search_api.example.json config\web_search_api.json
```

example 文件里给了三种常见形态（博查 / Serper / key 走 query string），照你买的那家留一份，把字段搬到文件末尾那份**生效配置**里。凭证不要写进 JSON，只写环境变量名。

`.env`：

```
TRAVELWISE_SEARCH_PROVIDER=http
TRAVELWISE_SEARCH_TOKEN=你的key
TRAVELWISE_SEARCH_CONFIG=config/web_search_api.json
TRAVELWISE_SEARCH_SCENES=打卡,出片
TRAVELWISE_SEARCH_MIN_MENTIONS=2
```

`TRAVELWISE_SEARCH_SCENES` 直接决定花多少钱：**一个场景词 = 一次调用**。第一次接接口建议只填两个词。

### 5.2 跑

```bat
"%PY%" scripts\smoke_discovery.py --stage 4 --place 昆明
```

它会先打印将要搜的词和次数，等你确认再发请求。同一天同一城市重复跑会命中 `data/cache/discover` 的缓存，不再付费。

### 5.3 接不通时怎么定位

| 现象 | 多半是 |
|---|---|
| `搜索源可用` 就失败 | `.env` 没读到，或 `endpoint` 为空。先跑 `smoke_full_flow.py --stage 0` 看 .env 体检 |
| `搜索有返回` 失败（0 条） | `response.list_path` 路径写错。把返回体打出来对一遍层级 |
| 有结果但 `抽出地点` 为 0 | `field_map` 对错了（title/snippet 拿到空串），或标题里确实没有地名后缀词 |
| 抽出一堆奇怪的名字 | 真实标题文风和回放数据差太远。把那几条标题抄进 fixtures，回到阶段 1 调词表 |

## 6. 调词表的正确姿势

抽取规则集中在 `src/travelwise/tools/spot_extract.py` 顶部四个常量：

| 常量 | 作用 | 调它的时机 |
|---|---|---|
| `SUFFIXES` | 地点后缀词 | 漏抽了某类地方（比如「××书局」「××市集」） |
| `NOISE_WORDS` | 攻略话术词 | 抽出了「宝藏公园」这类形容词 |
| `STOP_CHARS` | 往前吃前缀时的截断字 | 抽出了「尽头是翠湖公园」这类粘着半句话的名字 |
| `GENERIC_NAMES` | 裸后缀与通用词 | 抽出了「公园」「市中心」这类不是具体地方的词 |

**改之前先把触发问题的那条真实标题加进 `EXTRACT_CASES`（`scripts/smoke_discovery.py` 里）或 `tests/test_scene_discovery.py`**，否则下次改词表很容易把已经修好的东西改回去。

`STOP_CHARS` 尤其要克制：它里面**不能**放「大 / 小 / 新 / 老」，这些字真实地出现在地名里（大理古城、小西门、新迎小区），一并挡掉会误杀。

## 7. 一份「改完之后过一遍」的清单

```bat
"%PY%" -m unittest discover -s tests -p "test_*.py"    REM 228 全绿
"%PY%" scripts\smoke_discovery.py                       REM 阶段 0~3 全 OK
set TRAVELWISE_SEARCH_PROVIDER=fixture
"%PY%" scripts\smoke_full_flow.py --stage 1 --route 上海 昆明
"%PY%" -m travelwise "昆明有什么好玩的"                 REM 肉眼看输出是否像人话
set TRAVELWISE_SEARCH_PROVIDER=none
"%PY%" -m travelwise "昆明有什么好玩的"                 REM 确认降级时明写「未启用」
```

最后两条是配套的：**同一个问题，配了搜索源和没配，都必须给出诚实且可用的回答**。
