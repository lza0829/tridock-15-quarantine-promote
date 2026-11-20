from __future__ import annotations
from datetime import datetime, timezone
import json
import sqlite3

from app.services import precheck_service


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sum_map(conn: sqlite3.Connection, table: str, dock_day: str) -> dict[tuple[str, str], float]:
    rows = conn.execute(
        f"SELECT supplier_code, sku, SUM(qty) q FROM {table} WHERE dock_day=? GROUP BY supplier_code, sku",
        (dock_day,),
    ).fetchall()
    return {(r["supplier_code"], r["sku"]): float(r["q"]) for r in rows}


def run_reconcile(conn: sqlite3.Connection, dock_day: str) -> dict:
    sealed = conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='sealed'",
        (dock_day,),
    ).fetchone()
    if sealed:
        raise ValueError(f"到货日 {dock_day} 已结案，不能重跑对账")

    # 对账批重建后，旧预检的批绑定清单项必然过期，同事务内作废旧预检。
    precheck_service.invalidate_day(conn, dock_day, "重跑三联对账，对账批已重建")

    # drop previous open batch for same day
    old = conn.execute(
        "SELECT id FROM reconcile_batches WHERE dock_day=? AND status='open'",
        (dock_day,),
    ).fetchall()
    for r in old:
        conn.execute("DELETE FROM match_rows WHERE batch_id=?", (r["id"],))
        conn.execute("DELETE FROM variance_rows WHERE batch_id=?", (r["id"],))
        conn.execute("DELETE FROM reconcile_batches WHERE id=?", (r["id"],))

    expect_m = _sum_map(conn, "expect_lines", dock_day)
    receive_m = _sum_map(conn, "receive_lines", dock_day)
    confirm_m = _sum_map(conn, "confirm_lines", dock_day)
    keys = set(expect_m) | set(receive_m) | set(confirm_m)

    n = conn.execute("SELECT COUNT(*) c FROM reconcile_batches").fetchone()["c"] + 1
    batch_no = f"RB-{dock_day.replace('-', '')}-{n:03d}"
    cur = conn.execute(
        '''INSERT INTO reconcile_batches(batch_no, dock_day, matched_count, variance_count, status, sealed_at, note)
           VALUES (?,?,0,0,'open',NULL,?)''',
        (batch_no, dock_day, "三联对齐"),
    )
    batch_id = int(cur.lastrowid)

    matched = 0
    variance = 0
    now = _now()
    for supplier_code, sku in sorted(keys):
        eq = expect_m.get((supplier_code, sku), 0.0)
        rq = receive_m.get((supplier_code, sku), 0.0)
        cq = confirm_m.get((supplier_code, sku), 0.0)
        aligned = 1 if eq == rq == cq else 0
        conn.execute(
            '''INSERT INTO match_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, aligned)
               VALUES (?,?,?,?,?,?,?,?)''',
            (batch_id, dock_day, supplier_code, sku, eq, rq, cq, aligned),
        )
        if aligned:
            matched += 1
        else:
            variance += 1
            conn.execute(
                '''INSERT INTO variance_rows(batch_id, dock_day, supplier_code, sku, expect_qty, receive_qty, confirm_qty, fate, created_at)
                   VALUES (?,?,?,?,?,?,?,'pending',?)''',
                (batch_id, dock_day, supplier_code, sku, eq, rq, cq, now),
            )

    conn.execute(
        "UPDATE reconcile_batches SET matched_count=?, variance_count=? WHERE id=?",
        (matched, variance, batch_id),
    )
    conn.commit()
    return {
        "batch_id": batch_id,
        "batch_no": batch_no,
        "dock_day": dock_day,
        "matched_count": matched,
        "variance_count": variance,
        "status": "open",
    }


def _sweep_stale_precheck(conn: sqlite3.Connection, dock_day: str) -> None:
    """回滚后的自愈：若 valid 预检的三腿指纹已漂移，另起事务将其作废。"""
    try:
        row = conn.execute(
            "SELECT * FROM precheck_batches WHERE dock_day=? AND status='valid' ORDER BY id DESC LIMIT 1",
            (dock_day,),
        ).fetchone()
        if not row:
            return
        fingerprint, _ = precheck_service.legs_fingerprint(conn, dock_day)
        if fingerprint != row["legs_fingerprint"]:
            precheck_service.invalidate_day(conn, dock_day, "三腿指纹与预检快照不一致，封印时自动作废")
            conn.commit()
    except sqlite3.Error:
        pass


def seal_day(conn: sqlite3.Connection, dock_day: str, precheck_id: int) -> dict:
    # 单事务 + BEGIN IMMEDIATE：预检校验与封印写入原子化，
    # 并发结案在写锁上串行，条件更新保证只有一方能封，杜绝半截绑定失效 id。
    conn.execute("BEGIN IMMEDIATE")
    try:
        sealed = conn.execute(
            "SELECT 1 FROM reconcile_batches WHERE dock_day=? AND status='sealed' LIMIT 1",
            (dock_day,),
        ).fetchone()
        if sealed:
            raise ValueError(f"到货日 {dock_day} 已结案，不能重复封印")

        # 硬闸优先：伪造 / 跨日 / 失效 / 未通过 / 指纹漂移的 precheck_id 全部拒绝。
        pc = precheck_service.get_valid_precheck(conn, precheck_id, dock_day)

        batch = conn.execute(
            "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' ORDER BY id DESC LIMIT 1",
            (dock_day,),
        ).fetchone()
        if not batch:
            raise ValueError("没有可结案的对账批次，请先跑三联对账")

        pending = conn.execute(
            "SELECT COUNT(*) c FROM variance_rows WHERE batch_id=? AND fate='pending'",
            (batch["id"],),
        ).fetchone()["c"]
        if pending:
            raise ValueError(f"仍有 {pending} 条差额未处置，不能结案")

        now = _now()
        snapshot = {
            "precheck_id": precheck_id,
            "precheck_no": pc["precheck_no"],
            "sealed_at": now,
            "legs_fingerprint": pc["legs_fingerprint"],
            "scan_summary": json.loads(pc["scan_summary"]),
        }
        cur = conn.execute(
            """UPDATE reconcile_batches
               SET status='sealed', sealed_at=?, precheck_id=?, precheck_summary=?
               WHERE id=? AND status='open'""",
            (now, precheck_id, json.dumps(snapshot, ensure_ascii=False), batch["id"]),
        )
        if cur.rowcount != 1:
            raise ValueError("对账批状态已被并发改动，结案失败，请刷新后重试")
        conn.commit()
    except Exception:
        conn.rollback()
        _sweep_stale_precheck(conn, dock_day)
        raise
    return dict(conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (batch["id"],)).fetchone())


def set_variance_fate(conn: sqlite3.Connection, variance_id: int, fate: str) -> dict:
    allowed = {"pending", "accept_short", "accept_over", "quarantine"}
    if fate not in allowed:
        raise ValueError("非法差额命运")
    row = conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone()
    if not row:
        raise ValueError("差额行不存在")
    batch = conn.execute("SELECT * FROM reconcile_batches WHERE id=?", (row["batch_id"],)).fetchone()
    if batch and batch["status"] == "sealed":
        raise ValueError("已结案批次不可改差额命运")
    # 未决差额状态变化使旧预检的 pending 扫描快照过期。
    precheck_service.invalidate_day(conn, row["dock_day"], f"差额行 {variance_id} 命运变更为 {fate}")
    conn.execute("UPDATE variance_rows SET fate=? WHERE id=?", (fate, variance_id))
    conn.commit()
    return dict(conn.execute("SELECT * FROM variance_rows WHERE id=?", (variance_id,)).fetchone())
