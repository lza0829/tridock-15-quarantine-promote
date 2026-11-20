# Tridock（到货三联台）

骨架：到货预报装载 → 实收入账 → 供应商回执装载 → 三方键对齐与差额处置 → **结案预检（硬闸）** → 到货日结案封印。

- 前端 http://127.0.0.1:5900
- API http://127.0.0.1:10900

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10900 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5900
```

测试：`cd backend && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/python -m unittest discover -s tests -v`

## 结案预检硬闸（全局唯一架构）

本项目只贯彻「预检硬闸」一种口径，**不存在 skipped 路径**：未通过有效预检的到货日一律不能封印，sealed 对账批上不可能出现伪造或空的 `precheck_id`。

### 数据模型

- `precheck_batches`：预检批。`precheck_no / dock_day / passed / status(valid|invalidated) /
  legs_fingerprint / scan_summary(JSON) / 失效时间与原因`。
- `precheck_findings`：扫描物化的清单项，三类：
  - `missing_leg`：缺腿扫描（供应商+货号键在任一腿缺失；全天无数据报三腿全缺）。
  - `pending_variance`：未决差额扫描（`variance_rows.fate='pending'`）。
  - `batch_binding`：对账批绑定检查（无 open 批、对账批未覆盖当前三腿键集合、数量快照过期）。
- `reconcile_batches` 增加 `precheck_id` 与 `precheck_summary`：封印快照冗余预检号、
  三腿指纹与扫描摘要，封印后不可变。
- `legs_fingerprint`：三腿全部行序列化后的 SHA-256，任何装载/变更都会改变指纹。

### 正确性口径

1. **结案必须携带真实有效的 `precheck_id`**（`POST /api/reconcile/seal` 必填，`>0`）。
   服务端逐条拒绝：id 不存在（伪造）、非本 `dock_day`（跨日复用）、`status=invalidated`
   （过期复用）、`passed=0`（清单未清零），以及封印前重算指纹不一致（预检后三腿偷装）。
2. **预检失效钩子**（与业务写入同一事务提交）：
   - 预报/实收/回执任一腿再装载 → 该日 valid 预检失效；
   - 重跑三联对账（对账批重建）→ 失效；
   - 差额命运变更（未决扫描快照过期）→ 失效。
3. **封印快照绑定**：校验通过后在单事务 `BEGIN IMMEDIATE` 内以条件更新
   `WHERE status='open'` 写 `precheck_id + precheck_summary`。并发结案在写锁上串行，
   只有一方能封，失败方整体回滚，杜绝半截绑定失效 id。
4. **已绑定 sealed 批的预检永久冻结**：迟到的作废操作不会追溯改写封印快照。
5. 已结案到货日的写闸不变：三腿禁止再写、不能重跑对账/预检、不能重复封印。

### 联动面

- 后端：预检服务 `app/services/precheck_service.py`、结案闸
  `app/services/reconcile_service.py`、三腿路由失效钩子 `app/routers/lines.py`、
  预检路由 `app/routers/precheck.py`、总览 `app/routers/dashboard.py`。
- 前端：新增「06 结案预检」页（跑检、扫描发现、历史、失效原因）；
  「三联对账」页展示预检闸门状态，结案按钮仅在预检 valid+passed 时可点并携带 id，
  sealed 后展示快照绑定；「总览」展示最近预检与封印绑定。

旧版数据库启动时自动迁移（补建预检两表、给 `reconcile_batches` 补列），历史 sealed 批
`precheck_id` 为 NULL，属迁移前数据，不参与新封印流程。
