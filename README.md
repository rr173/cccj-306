# 校际微证书后端（FastAPI + SQLite）

一个只处理本地学校、学生、正式修读记录、微证书要求项、审核与颁证占用，以及进阶实训报名与联合换班的后端。
不包含成绩录入、选课或电子签章；不做缴费、候补或自动寻找交换对象。

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
- 实训报名、退出、换班单创建与确认同样在 `BEGIN IMMEDIATE` 内决策；部分唯一索引
  `uq_active_enrollment`（每期每生一个在册名额）与 `uq_pending_swap_student`（每生至多一张待确认单）
  是数据库层的最终防护；来源撤销在同一事务内联动释放报名名额并终止相关换班单，
  因此名册、单据与剩余名额始终可以用同一个先后顺序解释。
- 器材的预约、改期、取消、领出、归还、停用与解除、标签变更都在 `BEGIN IMMEDIATE` 内完成：
  优先级与整套判定只在该事务内计算一次，部分唯一索引 `uq_equipment_active_handover`
  （`picked_up_at IS NOT NULL AND returned_at IS NULL` 时每件实物至多一条）是数据库层的
  重复借出最终防护；预约的 `seq` 与领出的实际编号持久化在库内，重启后可继续沿原顺位判断。

## 进阶实训：报名与联合换班

### 领域规则

- 主办学校创建培训期（`training_term`），登记该期接受的微证书定义（可多选）和各班（`training_class`）容量。
- 学生用**本人一张当前有效**的合格证书报名：
  - 证书必须 `active` 且其未释放依据中没有已撤销的来源记录；证书类型必须被该期接受；
  - 同一期只能持有一个正式班次名额（部分唯一索引 `uq_active_enrollment` 兜底）；
  - 可以主动退出（`withdrawn`），退出后可再次报名，历史行保留。
- 证书一旦因来源撤销而失效，对应报名**立即**变为 `disqualified` 并释放名额：
  - 不等原颁证学校处理撤销，也不会自动换用另一张证书；
  - 证书在颁证侧原有的记录占用规则不受影响（`revocation_pending` 仍占用、确认后才释放）。
- 主办方提交换班单（`swap_order`），指定同一期的参与学生、每人的目标班次和确认截止时间：
  - 参与者必须已有正式报名、目标班必须不同于原班、同一学生不能同时出现在两张待确认单中
    （部分唯一索引 `uq_pending_swap_student` 兜底）；
  - 换班单按**整体**判断能否容纳：本单转出的名额只供本单转入使用，不算作其他单或新报名的空位；
    同班净增人数必须从当前可用名额中留出；
  - 待确认期间名册仍显示原班次，但预留名额对其他报名/换班不可用。
- 学生只能确认或拒绝自己在单中的变动；重复或迟到的响应返回 `409 already_responded/swap_order_closed`。
- 全部确认后在一个写事务内一次变更所有人的正式班次（`swapped`），不会只换成一部分。
- 出现以下任一情况，整张单结束并释放预留，其他人的原班不变：
  有人拒绝（`rejected`）、有人退出报名（`withdrawn`）、有人证书失效失去资格（`disqualified`）、
  到达截止时间仍未全部确认（`expired`）。
- 过期单在每个服务入口的写事务内惰性清扫：服务重启后未到期的单可继续确认，过期单不再阻挡任何名额。

### 名额核算

每个班次返回：

| 字段 | 含义 |
|---|---|
| `capacity` | 主办方设定的容量 |
| `enrolled` | 当前正式在册人数 |
| `held_pending_in` / `held_pending_out` | 待确认单转入/转出人数 |
| `net_pending_reservation` | 净预留 = 转入 − 转出（本单转出只对冲本单转入） |
| `available` | 对新报名和其他单据仍可用的名额 = `capacity − enrolled − net_pending_reservation` |

### HTTP 接口

- 期与班：
  - `POST /api/training/terms`、`GET /api/training/terms`、`GET /api/training/terms/{term_id}`
  - `POST /api/training/classes`、`GET /api/training/terms/{term_id}/classes`
  - `GET /api/training/terms/{term_id}/classes/{class_id}/roster?organizer_school_id=...`（主办方名册与名额）
- 报名与退出：
  - `POST /api/training/enrollments`
  - `POST /api/training/withdrawals`
  - `GET /api/training/terms/{term_id}/students/{student_id}`（学生查看正式班次、报名历史和自己的换班单进度）
- 联合换班：
  - `POST /api/training/swap-orders`
  - `GET  /api/training/swap-orders`、`GET /api/training/swap-orders/{order_id}`
  - `POST /api/training/swap-orders/{order_id}/responses`（`confirmed` / `rejected`）

### 可复跑示例

```bash
python examples/demo_training_swap.py --db /tmp/training_demo.db
```

脚本断言并打印两条时间线：

1. **满班互换**：两个容量 1 的满班，单人调班无空位可容纳；联合换班单成立后两班净增均为 0，
   双方确认后一次性互换，外部学生始终拿不到名额。
2. **换班中途失去资格**：换班单待确认期间来源学校撤销某参与者证书所依据的记录，
   该报名立即失效释放名额、整单变为 `disqualified`、其他人原班不动，释放名额可被新报名使用，
   迟到的确认无法再次调班。

## 共享器材室：按班次整套预约与交接

几所学校在同一实训场地共用一间器材室。器材按**实物编号**登记，由所属学校维护能力标签
和停用时段；主办学校只能为自己名下的班次预约整套器材，现场一次领齐。

### 领域规则

- **实物与标签**
  - 每件实物有全局唯一 `item_id`、所属学校 `owner_school_id` 和一组能力标签；
  - 只有所属学校能改标签、加停用时段、提前解除停用；其他学校只能读取和预约；
  - 停用时段为半开区间 `[start_at, end_at)`，可提前解除（`lifted_at`），解除后该段不再阻挡预约；
  - 停用**不抹去**正在进行的借用：已领出的整套照常持有，直到实际归还。
- **班次预约**
  - 主办学校必须是该实训期 `training_term` 的 `organizer_school_id`，并为该期的一个班
    `class_id` 提交开始、结束时间和一组需求；
  - 每项需求写明所需**全部**标签及件数；一件实物必须同时带有该项的全部标签才算匹配；
  - 同一次预约的各件必须是**不同实物**；从领出到约定结束，同一件不能靠中途更换来满足需求；
  - 时间段含起点、不含终点。尚未领出的预约只考虑剩余时间 `[max(start, now), end)`，
    不追补已经过去的用时。
- **优先级与缺套**
  - 各预约按首次受理的先后（`seq`）确定优先级；
  - 当前不能全保时，第一个有完整可行安排的预约保留；随后每个预约只有在与已保留预约
    **共同可行**时才保留。判断时可以为这些尚未领出的预约重排器材，不能因为此前恰好占用了
    某一件就判缺；
  - 新单无法整套保留则不受理（`409 reservation_not_feasible`），不写任何预约；
  - 已受理但暂时无法保留的单状态仍为 `accepted`，对外显示 `feasible=false / short=true`（缺套），
    但**不占器材**（`planned_item_ids=[]`），并给出 `shortage` 原因；
  - 条件恢复（归还、解除停用、取消、改时间）后按原顺位自动恢复，不用重新下单。
- **交接**
  - 只有预约时间内（`start_at <= now < end_at`）且整套可保留时才能领出；
  - 领出时一次登记实际交接的全部编号：数量须与需求总件数一致、编号互不相同、
    每件都带对应需求项的全部标签且未处于停用段；
  - 不能领出仍在别人手里的实物（`409 equipment_item_in_use`）；
  - 领出后不再自动换件，按约定结束时间预计释放；到时仍未归还，
    `expected_release_at=null`、`overdue=true`，不再为这件器材预计可用时间，直到实际归还；
  - 已领出的一次整套归还，不能只归还一部分来维持借用。
- **生命周期**
  - 主办校可取消未领出的预约；未领且已到结束时间的预约在每次服务入口惰性过期；
  - 取消、停用或解除、归还、改时间都会在同一事务内重新评估受影响预约。

### HTTP 接口

- 器材维护（所属学校）：
  - `POST /api/equipment/items`
  - `GET  /api/equipment/items?owner_school_id=...&tag=...`
  - `GET  /api/equipment/items/{item_id}`
  - `POST /api/equipment/items/{item_id}/tags`
  - `POST /api/equipment/items/{item_id}/suspensions`
  - `POST /api/equipment/items/{item_id}/suspensions/{suspension_id}/lift`
- 预约与交接（主办学校）：
  - `POST /api/equipment/reservations`
  - `GET  /api/equipment/reservations?organizer_school_id=...&term_id=...&status=...`
  - `GET  /api/equipment/reservations/{reservation_id}`
  - `POST /api/equipment/reservations/{reservation_id}/reschedule`
  - `POST /api/equipment/reservations/{reservation_id}/cancel`
  - `POST /api/equipment/reservations/{reservation_id}/pickup`
  - `POST /api/equipment/reservations/{reservation_id}/return`
  - `GET  /api/equipment/schools/{school_id}/board`（本校单据状态与当前已领编号）

预约状态：`accepted`（含缺套）、`picked_up`、`returned`、`cancelled`、`expired`。

### 可复跑示例

```bash
python examples/demo_equipment_room.py --db /tmp/equipment_demo.db
```

脚本断言并打印两条时间线：

1. **停用后重排仍能领齐**：属校停用一台投影仪（覆盖班次时段），主办校的整套预约
   重排到同标签的另一台投影仪与独立 HDMI 线，进窗后一次登记全部编号领齐。
2. **逾期导致另一单暂缺、归还后恢复**：前一班领走最后一台相机后逾期未还，
   后一班不再预计释放时间且暂缺、不占器材；新单因无法整套保留而不受理；
   实际归还后后一班按原顺位恢复完整计划并在剩余时间内领出。

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
- 20 轮“撤销 vs 颁证”竞争后，已撤销依据不可能验证为有效；
- 实训报名校验（本人有效证书、接受类型、每期一个名额、退出再报、满班拒绝）；
- 证书失效立即释放报名名额并终止相关待确认换班单；
- 满班两人互换、三人联合轮换的整体预留与一次性换班；
- 拒绝、退出、撤销、超时四种结束方式均释放预留且不部分换班；
- 迟到/重复确认不能再次调班；重启后未到期单可继续确认、过期单不再阻名额；
- 并发报名不超额、并发换班单不能让同一学生同时占两张单；
- 器材：每项需求需全部标签且一次预约不重复用同一实物；停用后整套重排仍可领齐；
  解除停用按原顺位恢复；逾期未还不预计释放时间且后单暂缺，归还后恢复；
  取消/过期/改时间反映到受影响预约；并发交接同一实物恰好成功一次；
  重启后已受理顺位与实际交接编号不丢。
