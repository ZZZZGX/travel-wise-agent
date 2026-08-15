# 付费接口配置速查

只有三处需要动，其余全是默认值。

## 1. 查几天 —— 直接决定花多少钱

**位置：`.env`（仓库根目录，没有就新建）**

```ini
TRAVELWISE_MATRIX_DAYS=7
```

一天 1 次调用。7 天 = 7 次 ≈ ¥1.4（按 20 元 / 100 次算）。

优先级从高到低：

| 方式 | 写法 | 说明 |
|---|---|---|
| 命令行 | `--days 14` | 临时覆盖，只影响这一次 |
| 环境变量 | `TRAVELWISE_MATRIX_DAYS=7` | 默认值，写在 `.env` 里 |
| 代码默认 | `Settings.matrix_days = 7` | `src/travelwise/config.py` 第 61 行 |

`--days 0` 关闭矩阵，退回原来的单日查询 + 提前量分析。

## 2. 两个接口的容错链

**位置：`config/flight_api.json`** —— 聚美智数 / 怜花数科 这两家已经按接口文档填好了（endpoint、POST、depCode/arrCode/depDate、APPCODE 头、三字码转换）。你只要在 `.env` 里填 AppCode。

通用模板见 `config/flight_api.multi.example.json`。结构就是把单个配置塞进 `providers` 数组：

```json
{
  "providers": [
    { "name": "aliyun-primary", "endpoint": "...", "auth": { "value_env": "TRAVELWISE_FLIGHT_TOKEN" }, ... },
    { "name": "backup-api",     "endpoint": "...", "auth": { "value_env": "TRAVELWISE_FLIGHT_TOKEN_BACKUP" }, ... }
  ]
}
```

凭证仍然只进 `.env`，JSON 里永远只出现变量名：

```ini
TRAVELWISE_FLIGHT_PROVIDER=http
TRAVELWISE_FLIGHT_TOKEN=主接口的凭证
TRAVELWISE_FLIGHT_TOKEN_BACKUP=备接口的凭证
```

切换规则：

- 主源**抛错**（超时 / 401 / 429 / 返回格式变了）→ 自动换备源；
- 主源**返回空列表** → 视为「那天确实没航班」，**不换**。空结果是有效答案，再打一次备源只是白花 0.2 元。

单源写法完全向后兼容，不写 `providers` 数组就跟以前一样。

## 3. 省钱的三个开关

```ini
TRAVELWISE_FLIGHT_CACHE=1        # 当日缓存，默认开
TRAVELWISE_REQUEST_INTERVAL=0.5  # 每次请求间隔秒数，防 QPS 限流
TRAVELWISE_MATRIX_DAYS=7
```

**缓存**的键是 `(查询日, 出发地, 目的地, 出发日)`。同一天里重跑、改 prompt、演示、被 Ctrl-C 打断后重来 —— 全部 0 元。跨天自动失效，绝不拿昨天的价格冒充今天的。失败不缓存，否则一次 429 会把那天钉死。落盘在 `data/cache/flight_cache.db`。

**间隔**：串行连打 7~30 次很容易撞限流，那会变成一整片 `×` 列 —— 比慢几秒糟得多。不确定就先设 0.5。

跑完会自动打印对账：

```
[缓存] 缓存命中 5 / 7（省下约 ¥1.0），实际调用 2 次
[额度] 调用 2 次（失败 0 次）≈ ¥0.4
  · aliyun-primary：2 次
```

## 3.5 全流程冒烟测试

一条命令按【花费从零到有】逐级验证，前面过不了就不往下走：

```bat
smoke.bat        :: 阶段 0~2：环境体检 + 离线全流程 + LLM 连通（不花接口钱）
smoke.bat 5      :: 完整链路，每个付费阶段先问你
smoke.bat 5 yes  :: 完整链路，不逐级确认
```

| 阶段 | 内容 | 花费 |
|---|---|---|
| 0 | 变量、配置、城市码表 | 0 |
| 1 | mock 数据跑通编排 + 矩阵 + 三种导出 | 0 |
| 2 | LLM 一句话连通（Key / 地址 / 模型名） | ~50 token |
| 3 | 每家接口各 1 次，验凭证与字段映射 | 2 次 ≈ ¥0.4 |
| 4 | 真实 7 天价格矩阵 + 导出 | 7 次 ≈ ¥1.4 |
| 5 | Agent 全闭环（模型自主选工具） | 0 次（命中阶段 4 缓存）+ token |

换解释器：`set PY=E:\你的\python.exe` 后再运行。

## 3.6 「我明明设了 Key 却读不到」

跑 `smoke.bat`，阶段 0 会打印**到底读了哪个文件、解析出哪些键**（只报键名和长度，不打印值）：

```
  .env 路径：E:\...\travelwise-agent\.env
  已解析 5 个键（214 字节）：
      第1  行  TRAVELWISE_FLIGHT_PROVIDER         长度 4
      第4  行  TRAVELWISE_LLM_API_KEY             长度 9
  [!] 文件带 UTF-8 BOM（记事本默认行为）
```

键没出现在这个列表里 = 那一行根本没被解析。四个常见原因：

| 现象 | 原因 | 怎么修 |
|---|---|---|
| 键完全没出现 | 文件其实叫 `.env.txt`（记事本另存为偷偷加的） | 改名成 `.env` |
| 所有键都没出现 | 存成了 UTF-16（记事本的「Unicode」） | 另存为 UTF-8 |
| 第一个键没出现 | UTF-8 BOM | 已自动处理；建议存成不带 BOM 的 UTF-8 |
| 键在、但鉴权失败 | 行尾写了注释，注释被当成值的一部分 | 已自动剥离；仍建议把注释单独一行 |
| 键出现了两次 | `.env` 被追加过内容（旧配置在上、新模板在下） | 删掉多余的行，只留一行 |

同名键出现多次时，规则是**后面有值的那个生效，空值不覆盖已有值**——两个方向都不会翻车。但诊断仍会把重复报出来，因为这属于配置错误，早晚会咬人。

写法：值直接跟在 `=` 后面，同一行不要写注释，不要加引号。

## 4. 第一次跑之前

先花 **1 次**额度验字段，别直接烧 7 次：

```bash
python scripts\probe_flight_api.py 上海 成都
```

它会查四件事：航班号取到没有、票价取到没有、所有航班价格是否完全相同（那说明取的是全价而非最低可售价，整张矩阵会是常数）、返回日期是否等于请求日期（不等说明接口忽略了日期参数，每一列都会一样）。

## 5. 导出

```bash
python -m travelwise "上海到成都 9月5号" --days 7 --export xlsx
```

`csv` / `xlsx` / `html` 三选一，默认写到 `data/cache/exports/`，可用 `--export-path` 指定。全部由代码直接生成，**不经过任何模型，零 token**。
