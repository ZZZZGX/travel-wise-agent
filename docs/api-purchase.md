# 四个接口去哪买、怎么买

本项目用到四个外部接口。**全部是可选的**——一个都不配也能跑（`--provider mock` +
名录轨），只是拿不到真实数据。

| 接口 | 干什么 | 不配的后果 | 大致成本 |
|---|---|---|---|
| 航班查询（主） | 查票价 | 用合成数据，价格是假的 | ¥0.2/次 量级 |
| 航班查询（备） | 主接口挂了顶上 | 主接口一挂就整轮失败 | 同上 |
| 网页搜索 | 发现轨「先搜后抽」 | 只给搜索入口，不抽地点 | ¥0.02/次 量级 |
| LLM | 听懂自然语言 | 退回规则路由，只认固定写法 | ¥0.003/次路由 |

> 下面的价格是量级参考，不是报价。各家随时调价，**以你下单时看到的为准**。

---

## 一、航班查询接口（阿里云云市场）

### 买什么

云市场搜「**航班查询**」或「**机票价格查询**」。挑选时只看三件事：

1. **返回里必须有价格字段**。很多「航班动态」类商品只给起降时刻和延误信息，
   没有票价——那样本项目的核心功能（什么时候买划算）就没有数据支撑。
   商品详情页的「返回示例」里找 `price` / `adultPrice` / `salePrice` 之类的字段。
2. **入参接受城市三字码**（`SHA` / `URC`）或中文城市名，且日期格式明确。
3. **按次计费**，有小额套餐包可以先试。

本项目实测跑通过的两家（同一规格，可互为备份）：

- 聚美智数 · 飞机航班查询　`https://jmfjhb.market.alicloudapi.com/flight/detail`
- 怜花数科 · 飞机航班查询　`https://lhfjhbcx.market.alicloudapi.com/flight/detail`

两家的入参完全一致：`depCode` / `arrCode` / `depDate`，POST + form body。

### 怎么买

1. 登录阿里云 → 云市场 → 搜商品 → 选套餐规格 → 购买
2. 进「**已购买的服务**」页面，找到该商品，复制 **AppCode**
3. 如果 AppCode 那一栏是空的，检查右上角是不是 **RAM 用户**——
   RAM 用户看不到 AppCode，要用主账号登录

### 计费口径

云市场按 HTTP 状态码计费：**2xx 扣次数，4xx / 5xx 不扣**。
所以调参数、试字段映射的阶段不用心疼，参数填错是免费的。

### 配置

`.env`：

```
TRAVELWISE_FLIGHT_PROVIDER=http
TRAVELWISE_FLIGHT_CONFIG=config/flight_api.json
TRAVELWISE_FLIGHT_TOKEN=你的AppCode
TRAVELWISE_FLIGHT_TOKEN_BACKUP=备用接口的AppCode
```

AppCode 绑账号不绑商品——同一个云市场账号买的多个接口，AppCode 是同一个。
但仍然建议两行分开写：将来换供应商时只改一处。

`config/flight_api.json` 从 `config/flight_api.multi.example.json` 复制后改。
**必须打开 `resolve_city_code`**，否则会把「上海」两个汉字原样发过去，
接口多半返回空列表——而空列表在本项目里被解释成「当天确实没航班」，
你会拿到一份看起来正常、实际什么都没查的结果，还不会触发容错链。

### 买完先探一次

```bash
python scripts/probe_flight_api.py 上海 乌鲁木齐 --date 2026-09-01 --which 你的数据源名
```

它只打 1 次请求，把返回结构摊开给你看。**不加 `--which` 会逐个探测所有数据源，
配了两家就是 2 次。** 照着输出把 `list_path` / `price_field` / `field_map` 填准，
再在 `.env` 里加 `TRAVELWISE_STRICT_FIELDS=1`——字段对不上时直接报错，
而不是猜一个继续跑。

---

## 二、网页搜索接口

发现轨（二层：先搜后抽）需要一个能返回「标题 / 摘要 / URL」的搜索接口。

### 买什么

本项目的配置模板按 **博查（Bocha）** 写的，字段映射现成。两条购买路径：

| 路径 | 鉴权 | endpoint |
|---|---|---|
| 博查开放平台直连 | `Authorization: Bearer sk-xxx` | `https://api.bochaai.com/v1/web-search` |
| 阿里云云市场 | `Authorization: APPCODE xxx` | 商品自己的 `*.market.alicloudapi.com` 域名 |

**建议走直连**——模板就是照它写的，改都不用改。

Serper.dev（走 Google）和其它通用搜索接口也能用，
`config/web_search_api.example.json` 里给了三种形态的模板。

### 费用口径（重要）

**每个场景词 = 1 次调用。**

```
TRAVELWISE_SEARCH_SCENES=
```

留空 = 默认四个词（打卡 / 出片 / 小众景点 / citywalk）= **每查一个目的地 4 次**。
省钱就显式写少一点：

```
TRAVELWISE_SEARCH_SCENES=小众景点,citywalk
```

### 先零成本验证

不想立刻花钱，可以先用回放模式验证链路：

```
TRAVELWISE_SEARCH_PROVIDER=fixture
```

读 `data/fixtures/scene_search.json`，**0 元 0 token**。
注意里面只录了昆明和大理，查别的城市依然是空的（而且会明说「回放里没有这组键」）。

---

## 三、LLM

### 买什么

需要**支持 function calling 且非思考模式**的模型。本项目实测：

- ✅ `deepseek-chat`
- ❌ `deepseek-reasoner` / 任何开着 thinking 的模型 ——
  会报 `Thinking mode does not support this tool_choice`，
  因为路由靠强制工具调用实现，而思考模式不支持 `tool_choice` 参数

配置：

```
TRAVELWISE_LLM_PROVIDER=openai
TRAVELWISE_LLM_API_KEY=sk-...
TRAVELWISE_LLM_BASE_URL=https://api.deepseek.com
TRAVELWISE_LLM_MODEL=deepseek-chat
TRAVELWISE_ROUTER=llm
```

`PROVIDER=openai` 指的是**协议**不是厂商——DeepSeek 走 OpenAI 兼容协议。
换成智谱、通义、或任何中转站，只要兼容这个协议，改 `BASE_URL` 和 `MODEL` 即可。
**两者必须是同一家的**，配错了会报 Model Not Exist。

### 两种用法，成本差很多

| 模式 | 模型干什么 | 额度可控性 |
|---|---|---|
| `--router llm` | 只负责听懂你的话 | **扫描天数由 `--days` 锁死，模型碰不到** |
| `--agent-loop` | 自己决定调哪个工具、调几次 | `days` 是暴露给模型的参数，可能连调数次 |

**想控成本就用 `--router llm`。** 一次路由约 900 token ≈ ¥0.0025。

### 验证 LLM 真的参与了

```bash
python -m travelwise --router llm --provider mock --days 3 --trace "8月29号从上海飞乌鲁木齐"
python scripts/view_trace.py --latest --open
```

`--provider mock` 让航班接口完全不调用，只花一次路由的钱。看三处：

1. 输出底部**没有**「路由提示：LLM 路由失败已降级到规则路由」
2. trace 面板「模型调用」≥ 1、TOKEN 非 0、成本不是「离线回放」
3. 点开 `route` 那一行，`fell_back` 为 `false`

---

## 四、提醒（可选，不花钱）

`TRAVELWISE_REMINDER_PROVIDER=console` 就够用，输出到控制台。
接日历/推送需要自己实现 provider，见 `src/travelwise/providers/reminders.py`。

---

## 花钱总闸

一次典型的完整查询：

```
航班 7 次（--days 7）    ≈ ¥1.4
搜索 2 次（两个场景词）   ≈ ¥0.04
LLM 路由 1 次            ≈ ¥0.0025
```

**最大的开销是航班扫描，且它和 `--days` 严格成正比。**

三个止血开关：

```
TRAVELWISE_FLIGHT_CACHE=1     # 同一天重复查同一航线不再付费
TRAVELWISE_MATRIX_DAYS=7      # 默认扫描天数
TRAVELWISE_REQUEST_INTERVAL=0.5   # 防限流，别调到 0
```

**不要用 `--days 0`。** 它关掉的只是矩阵模式，代码会走另一条路
（窗口 7~14 天带提前停止），实际消耗 8~15 次，比开着矩阵更贵。
想少花就 `--days 3`。
