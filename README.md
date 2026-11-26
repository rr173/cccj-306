# 校际微证书后端（FastAPI + SQLite）

一个只处理本地学校、学生、正式修读记录、微证书要求项、审核与颁证占用的后端。
不包含成绩录入、选课或电子签章。

## 核心规则

- 来源学校发布修读记录：全局唯一 `record_code`、学生、课程类别 `category`、整数 `points`。
- 已发布记录的来源学校、学生、类别、分值不可修改，也不能删除；来源学校只能撤销。
- 颁证学校定义证书及多个互不相同的要求项；每个要求项指定：
  - `accepted_categories`：接受的课程类别；
  - `min_points`：最低分值；
  - `points_mode`：
    - `whole_record`（默认）：必须有一条类别匹配且自身分值达到 `min_points` 的整条记录；
    - `sum_points`：允许同一要求项使用多条整条记录，按分值合计达标。
- 同一条记录可以符合多个要求项，但一次方案中最多分配给一个要求项，不拆分、不重复。
- 预审 `POST /api/reviews` 只读、不占用。
- 显式颁证 `POST /api/certificates/issue` 在同一个 `BEGIN IMMEDIATE` 写事务中重新检查学生、记录状态、占用和匹配方案，然后写入证书及逐项记录。
- 记录占用状态：
  - `active` 证书：占用；
  - `revocation_pending` 证书：对外立即无效，但其中尚未释放的正常记录继续占用；
  - `revocation_processed` 证书：颁证学校确认后释放全部逐项占用。
- 来源撤销后：
  - 相关有效证书立即变为 `revocation_pending`；
  - `GET /verify/{certificate_code}` 同时根据当前来源记录实时复核，依据已撤销时绝不返回 `valid=true`；
  - 原证书和逐项依据仍可查询，系统不提供换记录或静默改判接口；
  - 颁证学校确认后状态变为 `revocation_processed`，并释放该证书的记录占用。

SQLite 中还有最终数据库约束：

```sql
CREATE UNIQUE INDEX uq_active_record_occupancy
ON certificate_item_record(record_code)
WHERE released_at IS NULL;
```

因此即使两个颁证事务并发，同一条记录也不可能存在两条未释放占用。

## 运行

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 写入三校示例基础账
python -m app.seed --db microcred_demo.db --reset

# 启动 API
MICROCERT_DB=microcred_demo.db uvicorn app.main:app --reload
# 或：MICROCERT_DB=microcred_demo.db python main.py
```

打开：

- Swagger UI：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>
- 对外验证：`GET /verify/{certificate_code}`

## 三校小账例

数据由 `app/seed.py` 写入：

| ID | 学校/学生 | 说明 |
|---|---|---|
| `sch_xinghe` | 星河理工学院 | 来源学校 |
| `sch_yunsong` | 云杉数字学院 | 颁证学校 |
| `sch_linhai` | 临海商学院 | 另一所颁证学校 |
| `stu_lin` | 林知夏 | 学生 |

修读记录：

| 记录编号 | 类别 | 分值 | 场景 |
|---|---:|---:|---|
| `XH-2026-1001` | `MATH` | 4 | 同时符合云杉两个要求项，但只能使用一次；也是临海要求项的目标记录 |
| `XH-2026-1002` | `CS` | 3 | 可补云杉 `logic` 项，随后由星河撤销 |
| `XH-2026-1003` | `STAT` | 5 | 备用来源记录 |

云杉证书 `def_yunsong_digital`：

- `logic`：接受 `CS`、`MATH`，最低 3；
- `quant`：接受 `MATH`、`STAT`，最低 3。

临海证书 `def_linhai_biz_data`：

- `math_core`：接受 `MATH`，最低 4。

### 可运行的完整时间线

```bash
python examples/demo_three_schools.py --db /tmp/three_schools_demo.db
```

脚本会断言并打印以下差异：

1. 只审核 `XH-2026-1001`：它同时符合 `logic` 和 `quant`，但无法用一条记录同时覆盖两项，`feasible=false`。
2. 加入 `XH-2026-1002` 后：方案为 `logic -> XH-2026-1002`、`quant -> XH-2026-1001`。
3. 云杉颁证前，临海可以用 `XH-2026-1001` 审核通过。
4. 云杉颁证后，临海审核看到该记录 `status=occupied`，颁证返回 `409`。
5. 星河撤销 `XH-2026-1002` 后，云杉证书立即 `revocation_pending`，对外验证 `valid=false`。
6. 云杉确认撤销前，`XH-2026-1001` 仍被 pending 证书占用，临海仍不能颁证。
7. 云杉调用确认接口后，证书变为 `revocation_processed`，逐项行保留但 `released_at` 已写入。
8. 临海随后可以使用释放的 `XH-2026-1001` 成功颁证。

## 主要 HTTP 接口

### 本地基础账

- `POST /api/schools`
- `GET  /api/schools`
- `GET  /api/schools/{school_id}`
- `GET  /api/schools/{school_id}/ledger`
- `GET  /api/schools/{school_id}/certificates?status=active|revocation_pending|revocation_processed`
- `POST /api/students`
- `GET  /api/students/{student_id}`
- `GET  /api/students/{student_id}/certificates`
- `POST /api/records`
- `GET  /api/records?student_id=...&source_school_id=...`
- `GET  /api/records/{record_code}`
- `GET  /api/records/{record_code}/usage`
- `POST /api/records/{record_code}/revoke`

### 证书要求、审核、颁证

- `POST /api/credential-definitions`
- `GET  /api/credential-definitions`
- `GET  /api/credential-definitions/{definition_id}`
- `POST /api/reviews`
- `POST /api/certificates/issue`
- `GET  /api/certificates/{certificate_code}`
- `POST /api/certificates/{certificate_code}/confirm-revocation`
- `GET  /verify/{certificate_code}`

## Curl 走查

```bash
# 1. 只给一条同时符合两个要求项的记录：应 feasible=false
curl -sS -X POST http://127.0.0.1:8000/api/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "definition_id":"def_yunsong_digital",
    "student_id":"stu_lin",
    "record_codes":["XH-2026-1001"]
  }'

# 2. 两条记录审核
curl -sS -X POST http://127.0.0.1:8000/api/reviews \
  -H 'Content-Type: application/json' \
  -d '{
    "definition_id":"def_yunsong_digital",
    "student_id":"stu_lin",
    "record_codes":["XH-2026-1001","XH-2026-1002"]
  }'

# 3. 显式颁证；也可传 assignments 完全指定方案
curl -sS -X POST http://127.0.0.1:8000/api/certificates/issue \
  -H 'Content-Type: application/json' \
  -d '{
    "certificate_code":"CERT-YUNSONG-001",
    "definition_id":"def_yunsong_digital",
    "student_id":"stu_lin",
    "record_codes":["XH-2026-1001","XH-2026-1002"]
  }'

# 4. 来源学校撤销
curl -sS -X POST \
  http://127.0.0.1:8000/api/records/XH-2026-1002/revoke \
  -H 'Content-Type: application/json' \
  -d '{"source_school_id":"sch_xinghe","reason":"来源凭据复核撤销"}'

# 5. 对外验证：valid=false, status=revocation_pending
curl -sS http://127.0.0.1:8000/verify/CERT-YUNSONG-001

# 6. 颁证学校确认，释放其余记录
curl -sS -X POST \
  http://127.0.0.1:8000/api/certificates/CERT-YUNSONG-001/confirm-revocation \
  -H 'Content-Type: application/json' \
  -d '{"issuer_school_id":"sch_yunsong"}'
```

审核失败或颁证冲突时，`error.details.review.candidates` 会给出每条候选记录的状态：

- `available`
- `ineligible`
- `occupied`（含占用证书及该证书是 `active` 还是 `revocation_pending`）
- `revoked`
- `wrong_student`
- `not_found`

## 并发设计

- SQLite 启用 WAL、外键和 `busy_timeout=30s`。
- 所有撤销、确认、颁证事务使用 `BEGIN IMMEDIATE`，在读取最终候选状态前取得写锁。
- 颁证在该写事务内重新执行候选状态检查和匹配，不信任此前预审结果。
- 部分唯一索引 `uq_active_record_occupancy` 是数据库层的最终双用防护。
- 验证接口实时联查来源记录的 `revoked_at`；即使生命周期状态更新与读取交错，只要依据记录已提交撤销，有效判定也会变为 false。

## 测试

```bash
pip install -r requirements-dev.txt
pytest
```

测试覆盖：

- 一条记录符合两项但不能同时覆盖；
- 完整审核、颁证、占用、撤销、pending 保持占用、确认释放、他校再颁证；
- 已发布记录分值不可变；
- 累计分值模式；
- 12 个并发颁证竞争同一记录时恰好一个成功；
- 20 轮“撤销 vs 颁证”竞争后，已撤销依据不可能验证为有效。
