# 园区电表区间用电量 API

把网关补传的电表**累计读数**转换成**区间用电量**，供异常耗电排查使用。

技术栈：FastAPI + SQLite（WAL），Docker Compose 启动两个 API 进程共享同一个
数据库卷。

## 快速开始

```bash
docker compose up -d --build
```

| 地址 | 用途 |
| --- | --- |
| <http://localhost:8000/docs> | Swagger UI / api-1（可执行入仓、撤销、查询） |
| <http://localhost:8000/redoc> | ReDoc |
| <http://localhost:8000/openapi.json> | OpenAPI 规范 |
| <http://localhost:8002/docs> | api-2，第二个进程，同一份数据 |
| <http://localhost:8000/health> | 健康检查（含当前提交序号） |

两个端口背后是**两个独立的 API 进程，共用 `/data/energy.sqlite3`**；可以交替
向两个端口发送写入以验证跨进程一致性（`scripts/e2e_compose.py` 就是这样做的）。

数据保存在命名卷 `energy-data` 中：`docker compose down` 保留数据，
`docker compose down -v` 清空重来。想加载演示数据（含乱序补传、换表、
计数回退样例）时设置 `ENERGY_SEED_DEMO=1` 后启动 api 容器，例如：

```bash
ENERGY_SEED_DEMO=1 docker compose up -d --build
curl -s localhost:8000/api/v1/meters | python3 -m json.tool
```

本地（非 Docker）运行：

```bash
python3 -m pip install -r requirements-dev.txt
ENERGY_DB_PATH=/tmp/energy.sqlite3 python3 -m app.run     # http://127.0.0.1:8000/docs
python3 -m pytest                                        # 含双进程并发/崩溃测试
```

## API

### 入仓 — `POST /api/v1/readings`

一次提交一批读数，**整批原子提交**（要么全部生效，要么整批拒绝）。

```json
{
  "readings": [
    {
      "meter_id": "M-1001",
      "source_record_id": "gw-0001",
      "read_at": "2026-10-01T00:00:00Z",
      "reading": 1000,
      "meter_replaced": false
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `meter_id` | 电表号 |
| `source_record_id` | 源记录号，全局唯一，用于重试去重与撤销 |
| `read_at` | UTC 时刻（ISO-8601）。带时区会换算为 UTC；不带时区按 UTC 解释 |
| `reading` | 累计读数，非负整数（度）；接受整数、整值浮点、数字字符串 |
| `meter_replaced` | 换表标记，缺省 `false`；接受 `true/false` 或 `1/0` |

返回：

```json
{
  "commit_seq": 3,
  "changed": true,
  "counts": {"created": 2},
  "records": [{"source_record_id": "gw-0001", "status": "created"}, ...],
  "affected_meters": [{"meter_id": "M-1001", "reading_count": 2,
                       "interval_count": 1, "break_count": 0,
                       "usage_total": 120, "first_read_at": "...", "last_read_at": "..."}]
}
```

`records[].status` 为 `created`（新写入）、`unchanged`（同号同内容重试，
不重复写、不推进提交序号）。整份重试时 `changed=false`、`counts` 全为
`unchanged`。

### 撤销 — `POST /api/v1/readings/undo`

```json
{"source_record_ids": ["gw-0001", "gw-0002"]}
```

按源记录号删除读数并重算这些电表的**全部区间**；不存在的号码返回
`not_found`（不算失败）；撤销后相邻读数会自动连成新的区间。

### 纠错提案 — `POST /api/v1/corrections/propose`

从单表计数回退中定位疑似网关误读。输入该电表时序上**两个可信源记录号**
（闭区间），求区间内最少需要撤销哪些读数，使保留读数在每个换表段内非递减。

```json
{
  "meter_id": "M-1003",
  "start_source_record_id": "demo-m1003-0000",
  "end_source_record_id": "demo-m1003-0400"
}
```

规则：

* 两个端点与所有带 `meter_replaced=true` 的读数**必须保留**；换表读数开启
  新段，非递减约束不跨段（换表导致的计数变小是合法换表，不是误读）。
* 每段在“必留点必须入选”的约束下求最长非降子序列（LNDS）：保留最多即
  撤销最少。
* 必留读数之间无法在非递减条件下连通时 `status="no_solution"`，只给原因。
* 存在多个并列的最少撤销集合时 `status="not_unique"`，不自动选择，响应里
  给出两个同等长度的撤销集合供工程师判断。
* 仅当方案**唯一**时 `status="unique"`，返回待撤销源记录号
  `revoke_source_record_ids` 与提案所依据快照的 `commit_seq`。

```json
{
  "status": "unique",
  "reason": null,
  "meter_id": "M-1003",
  "start_source_record_id": "demo-m1003-0000",
  "end_source_record_id": "demo-m1003-0400",
  "commit_seq": 7,
  "minimum_revoke_count": 1,
  "revoke_source_record_ids": ["demo-m1003-0200"],
  "segments": [ {"segment_index": 0, "reading_count": 5, "mandatory_count": 2,
                  "revoke_source_record_ids": ["demo-m1003-0200"], "...": "..."} ]
}
```

提案是只读操作，不改变任何读数或区间。

### 纠错确认 — `POST /api/v1/corrections/confirm`

工程师在**同一个写事务**内核对快照序号与方案后才真正撤销：

```json
{
  "meter_id": "M-1003",
  "start_source_record_id": "demo-m1003-0000",
  "end_source_record_id": "demo-m1003-0400",
  "expected_commit_seq": 7,
  "revoke_source_record_ids": ["demo-m1003-0200"]
}
```

* 事务内先核对当前提交序号等于 `expected_commit_seq`，再重新求解，确认方案
  仍唯一且撤销集合与提交内容完全一致，然后删除读数并重建该表区间。
* 序号过期（期间有别的写入）返回 409 `correction_stale`；方案已变化、不再
  唯一或无解分别返回 `correction_plan_mismatch` / `correction_not_unique` /
  `correction_no_solution`。任一失败整体回滚，**读数与区间都不变**。
* 提案本身无需撤销（`minimum_revoke_count=0`）时确认不推进提交序号。

### 查询 — `GET /api/v1/meters/{meter_id}`

可选参数 `from` / `to`（ISO-8601，含端点）与 `include_intervals`。
响应来自**同一个数据库快照**：

```json
{
  "meter_id": "M-1001",
  "commit_seq": 5,
  "window": {"from": null, "to": null, "applied": false},
  "readings": [{"source_record_id": "...", "read_at": "...", "reading": 1000, ...}],
  "intervals": [{
      "from_source_record_id": "...", "to_source_record_id": "...",
      "from_read_at": "...", "to_read_at": "...",
      "from_reading": 1000, "to_reading": 1120,
      "usage": 120, "is_break": false, "break_reason": null, "commit_seq": 5 }],
  "breaks": [...],
  "usage_total": 120
}
```

`GET /api/v1/meters` 列出所有电表的汇总。无任何读数的电表返回 404。

### 错误

| 状态码 | 含义 |
| --- | --- |
| 409 `batch_rejected` | 整批拒绝，`conflicts[]` 说明每条冲突 |
| 409 `correction_stale` / `correction_plan_mismatch` / `correction_not_unique` / `correction_no_solution` | 纠错确认未通过事务内核对，未做任何修改 |
| 404 `correction_endpoint_not_found` | 纠错端点源记录号不存在 |
| 422 | 请求体/时间窗口参数不合法，或纠错端点不属同一电表、时序颠倒 |
| 404 `meter_not_found` | 该电表没有读数 |
| 503 `database_busy` | 等待写锁超时，可重试（带 `Retry-After`） |

## 规则与语义

* **区间 = 同表相邻两条读数之差。** 读数按 `(电表号, UTC 时刻)` 排序，
  无论到达顺序如何（乱序补传会把新读数插到中间并重算前后区间）。
* **断点**：以下两种情况记断点，该区间 `usage` 为 `null`，不推算用量：
  * 后一条读数带 `meter_replaced=true`（换表）；
  * 后一条读数小于前一条（计数回退）。
  断点区间的两端读数仍会被后续正常区间使用。
* **同号同内容重试**幂等：不写库、不推进提交序号、不重算。
* **同号异内容**、**同表同刻（不同源记录号）**：整批 409 拒绝，数据库保持
  原样（校验在写入之前完成）。
* 同一次请求里重复的源记录号：内容相同则合并；内容不同或同表同刻不同号则
  整批拒绝。
* 撤销后再提交同一源记录号视为全新记录。

### 纠错（疑似网关误读）语义

* **问题定义**：在两个可信端点划定的闭区间内，撤销最少的读数，使每个换表
  段内保留的累计读数非递减。两端点与换表读数必留，换表读数开启新段，非
  递减约束从不跨段。
* **求解**：换表标记把区间切成独立段；段内是“含必留点的最长非降子序列”。
  必留点不可达（两个必留读数递减且中间无可选点能修复）即**无解**；通过
  从同一张 DP 表分别构造两条不同的最优链来判定**唯一性**，两条链重合则
  方案唯一，否则返回两个并列最少撤销集合，不自动选择。
* **提案只读**：在一次 WAL 快照读事务内计算，响应携带该快照 `commit_seq`。
* **确认原子**：`BEGIN IMMEDIATE` 写事务内重新加载区间、重新求解，并核对
  快照序号与撤销集合；任何不一致都在提交前抛出并回滚，读数与区间保持
  原状。确认成功后该电表区间在同一事务内全部重建。

## 一致性、并发与故障安全

* 共享库、两个进程：所有写操作在 `BEGIN IMMEDIATE` 事务里执行，SQLite 的
  写锁把并发写串行化（`busy_timeout=30s`），不会出现 SQLite 层面互相覆盖。
  `(source_record_id)` 与 `(meter_id, read_at_us)` 的唯一约束在库内兜底。
* 读数与区间在**同一个事务**内写入；每次变更后该电表的区间全部重建，
  因此区间表始终是读数表的函数。
* 查询在显式读事务（WAL 快照）中执行，读数、区间、断点、`usage_total`
  同属一次已提交状态；响应里的 `commit_seq` 标识该状态。
* 失败/重启：整笔提交在一个事务内，未提交即回滚，进程被杀不会留下半笔
  结果；启动时校验 `user_version`，测试另跑 `PRAGMA integrity_check` 与
  `foreign_key_check`。
* 区间表有 `CHECK` 约束：断点区间 `usage` 必须为 `NULL`，正常区间必须有值。

## 测试

```bash
python3 -m pytest            # 53 个用例
```

* `tests/test_api.py`：区间/断点/乱序/幂等/冲突/撤销/窗口/校验。
* `tests/test_correction.py`：纠错提案的唯一/无解/不唯一、换表边界、必留点
  保护，以及确认的序号过期、方案失配、原子性与零撤销语义。
* `tests/test_concurrency.py`：启动两个真实进程共享同一个 SQLite 文件，
  并发写、并发重试、竞争冲突、持续读写一致性、进程崩溃（`os._exit`）、
  重启后数据延续、未提交事务回滚，以及纠错确认的跨进程串行化。
* `tests/test_durability.py`：区间重建中途异常时整批回滚、提交序号不前进、
  多进程同时初始化建库、schema 版本保护。

纠错算法 `app/correction.py` 不依赖数据库与 Web 框架，其唯一性/可行性判定
另由穷举小规模全配置与大规模随机暴力枚举对照验证（开发期脚本）。

验证 Compose 部署：

```bash
python3 scripts/e2e_compose.py http://localhost:8000 http://localhost:8002
```

## 目录结构

```
app/            FastAPI 应用（main.py 路由、service.py 领域逻辑、
                correction.py 纠错纯算法、db.py 存储）
tests/          功能与并发/故障测试
scripts/        Compose 部署冒烟脚本
Dockerfile, docker-compose.yml
```
