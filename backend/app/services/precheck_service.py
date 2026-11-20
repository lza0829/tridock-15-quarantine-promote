from __future__ import annotations
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

# 全局唯一架构：预检硬闸。不存在 skipped 路径，sealed 批次必须绑定真实有效的预检批。

LEG_TABLES = (
    ("expect", "expect_lines"),
    ("receive", "receive_lines"),
    ("confirm", "confirm_lines"),
)
LEG_LABELS = {"expect": "预报", "receive": "实收", "confirm": "回执"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _leg_rows(conn: sqlite3.Connection, dock_day: str) -> dict[str, list[dict]]:
    legs: dict[str, list[dict]] = {}
    for leg, table in LEG_TABLES:
        rows = conn.execute(
            f"SELECT * FROM {table} WHERE dock_day=? ORDER BY id", (dock_day,)
        ).fetchall()
        legs[leg] = [dict(r) for r in rows]
    return legs


def legs_fingerprint(conn: sqlite3.Connection, dock_day: str) -> tuple[str, dict]:
    """三腿内容指纹：任一二进制层面的装载/变更都会改变指纹，使旧预检失效。"""
    legs = _leg_rows(conn, dock_day)
    payload = json.dumps(legs, ensure_ascii=False, sort_keys=True, separators=(",", ""), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), legs


def invalidate_day(conn: sqlite3.Connection, dock_day: str, reason: str) -> int:
    """把某到货日所有 valid 预检置为 invalidated。由调用方在同一事务内提交。

    已绑定到 sealed 对账批的预检永久冻结：封印快照不可变，迟到的作废不得追溯改写。
    """
    cur = conn.execute(
        """UPDATE precheck_batches
           SET status='invalidated', invalidated_at=?, invalidate_reason=?
           WHERE dock_day=? AND status='valid'
             AND id NOT IN (
               SELECT precheck_id FROM reconcile_batches
               WHERE status='sealed' AND precheck_id IS NOT NULL
             )""",
        (_now(), reason, dock_day),
    )
    return cur.rowcount


def _missing_leg_findings(legs: dict[str, list[dict]]) -> list[dict]:
    key_sets = {leg: {(r["supplier_code"], r["sku"]) for r in rows} for leg, rows in legs.items()}
    union_keys = set().union(*key_sets.values()) if key_sets else set()
    findings: list[dict] = []
    if not union_keys:
        findings.append(
            {
                "kind": "missing_leg",
                "supplier_code": None,
                "sku": None,
                "detail": json.dumps({"missing_legs": ["expect", "receive", "confirm"],
                                      "reason": "到货日没有任何三腿数据"}, ensure_ascii=False),
            }
        )
        return findings
    for supplier_code, sku in sorted(union_keys):
        missing = [leg for leg, ks in key_sets.items() if (supplier_code, sku) not in ks]
        if missing:
            findings.append(
                {
                    "kind": "missing_leg",
                    "supplier_code": supplier_code,
                    "sku": sku,
                    "detail": json.dumps(
                        {"missing_legs": missing,
                         "missing_labels": [LEG_LABELS[m] for m in missing]},
                        ensure_ascii=False,
                    ),
                }
            )
    return findings


def _batch_binding_findings(conn: sqlite3.Connection, dock_day: str, legs: dict[str, list[dict]]) -> list[dict]:
    batch = conn.execute(
        "SELECT * FROM reconcile_batches WHERE dock_day=? AND status='open' ORDER BY id DESC LIMIT 1",
        (dock_day,),
    ).fetchone()
    if not batch:
        return [
            {
                "kind": "batch_binding",
                "supplier_code": None,
                "sku": None,
                "detail": json.dumps({"reason": "no_open_batch", "message": "没有 open 对账批，请先跑三联对账"},
                                     ensure_ascii=False),
            }
        ]
    findings: list[dict] = []
    match_rows = conn.execute(
        "SELECT * FROM match_rows WHERE batch_id=? ORDER BY supplier_code, sku", (batch["id"],)
    ).fetchall()
    matched_map = {(r["supplier_code"], r["sku"]): dict(r) for r in match_rows}
    current_keys = set()
    for rows in legs.values():
        current_keys |= {(r["supplier_code"], r["sku"]) for r in rows}
    stale_keys = sorted(current_keys ^ set(matched_map))
    for supplier_code, sku in stale_keys:
        findings.append(
            {
                "kind": "batch_binding",
                "supplier_code": supplier_code,
                "sku": sku,
                "detail": json.dumps(
                    {"reason": "coverage_mismatch", "batch_id": batch["id"],
                     "message": "对账批未覆盖当前三腿键集合（已过期或漏绑）"},
                    ensure_ascii=False,
                ),
            }
        )
    qty_cols = {"expect": "expect_qty", "receive": "receive_qty", "confirm": "confirm_qty"}
    for key in sorted(current_keys & set(matched_map)):
        m = matched_map[key]
        for leg, rows in legs.items():
            qty = sum(r["qty"] for r in rows if (r["supplier_code"], r["sku"]) == key)
            if float(m[qty_cols[leg]]) != float(qty):
                findings.append(
                    {
                        "kind": "batch_binding",
                        "supplier_code": key[0],
                        "sku": key[1],
                        "detail": json.dumps(
                            {"reason": "qty_stale", "batch_id": batch["id"], "leg": leg,
                             "batch_qty": m[qty_cols[leg]], "current_qty": qty,
                             "message": "对账批数量与当前三腿不一致，需重跑对账"},
                            ensure_ascii=False,
                        ),
                    }
                )
    return findings


def _pending_variance_findings(conn: sqlite3.Connection, dock_day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM variance_rows WHERE dock_day=? AND fate='pending' ORDER BY id",
        (dock_day,),
    ).fetchall()
    return [
        {
            "kind": "pending_variance",
            "supplier_code": r["supplier_code"],
            "sku": r["sku"],
            "detail": json.dumps(
                {"variance_id": r["id"], "batch_id": r["batch_id"], "fate": r["fate"],
                 "qty": [r["expect_qty"], r["receive_qty"], r["confirm_qty"]],
                 "message": "差额仍为 pending，必须先处置"},
                ensure_ascii=False,
            ),
        }
        for r in rows
    ]


def run_precheck(conn: sqlite3.Connection, dock_day: str) -> dict:
    sealed = conn.execute(
        "SELECT 1 FROM reconcile_batches WHERE dock_day=? AND status='sealed' LIMIT 1",
        (dock_day,),
    ).fetchone()
    if sealed:
        raise ValueError(f"到货日 {dock_day} 已结案，无需再预检")

    fingerprint, legs = legs_fingerprint(conn, dock_day)
    findings = []
    findings += _missing_leg_findings(legs)
    findings += _batch_binding_findings(conn, dock_day, legs)
    findings += _pending_variance_findings(conn, dock_day)

    n = conn.execute("SELECT COUNT(*) c FROM precheck_batches").fetchone()["c"] + 1
    precheck_no = f"PC-{dock_day.replace('-', '')}-{n:03d}"
    now = _now()
    summary = {
        "expect_rows": len(legs["expect"]),
        "receive_rows": len(legs["receive"]),
        "confirm_rows": len(legs["confirm"]),
        "union_keys": len(
            set().union(*[{(r["supplier_code"], r["sku"]) for r in rows} for rows in legs.values()])
        ),
        "missing_leg_count": sum(1 for f in findings if f["kind"] == "missing_leg"),
        "batch_binding_issue_count": sum(1 for f in findings if f["kind"] == "batch_binding"),
        "pending_variance_count": sum(1 for f in findings if f["kind"] == "pending_variance"),
        "legs_fingerprint": fingerprint,
    }
    cur = conn.execute(
        """INSERT INTO precheck_batches
           (precheck_no, dock_day, passed, status, legs_fingerprint, scan_summary, created_at,
            invalidated_at, invalidate_reason)
           VALUES (?,?,?, 'valid', ?, ?, ?, NULL, NULL)""",
        (precheck_no, dock_day, 1 if not findings else 0, fingerprint,
         json.dumps(summary, ensure_ascii=False), now),
    )
    precheck_id = int(cur.lastrowid)
    for f in findings:
        conn.execute(
            "INSERT INTO precheck_findings(precheck_id, dock_day, kind, supplier_code, sku, detail)"
            " VALUES (?,?,?,?,?,?)",
            (precheck_id, dock_day, f["kind"], f["supplier_code"], f["sku"], f["detail"]),
        )
    conn.commit()
    return get_precheck(conn, precheck_id)


def get_valid_precheck(conn: sqlite3.Connection, precheck_id: int, dock_day: str) -> sqlite3.Row:
    """结案硬闸：逐条拒绝伪造/跨日/失效/未通过的预检批。"""
    if not isinstance(precheck_id, int) or precheck_id <= 0:
        raise ValueError("结案必须携带有效预检批 precheck_id（硬闸：未预检不能封印）")
    pc = conn.execute("SELECT * FROM precheck_batches WHERE id=?", (precheck_id,)).fetchone()
    if not pc:
        raise ValueError(f"precheck_id={precheck_id} 不存在，拒绝伪造预检绑定，不能结案")
    if pc["dock_day"] != dock_day:
        raise ValueError(
            f"预检批 {pc['precheck_no']} 属于到货日 {pc['dock_day']}，不能用于 {dock_day} 的结案"
        )
    if pc["status"] != "valid":
        reason = pc["invalidate_reason"] or "未知原因"
        raise ValueError(f"预检批 {pc['precheck_no']} 已失效（{reason}），请重新预检后再结案")
    if not pc["passed"]:
        raise ValueError(f"预检批 {pc['precheck_no']} 未通过清单项，不能结案")
    # 防御纵深：封印前重算指纹，杜绝失效状态未被钩子覆盖的任何路径。
    # 这里只做只读校验、不写库；调用方事务回滚后由其自愈钩子把漂移预检作废。
    fingerprint, _ = legs_fingerprint(conn, dock_day)
    if fingerprint != pc["legs_fingerprint"]:
        raise ValueError("预检后三腿数据发生变化，预检已失效，请重新预检后再结案")
    return pc


def list_prechecks(conn: sqlite3.Connection, dock_day: str | None = None) -> list[dict]:
    if dock_day:
        rows = conn.execute(
            "SELECT * FROM precheck_batches WHERE dock_day=? ORDER BY id DESC", (dock_day,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM precheck_batches ORDER BY id DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["scan_summary"] = json.loads(d["scan_summary"]) if d["scan_summary"] else None
        out.append(d)
    return out


def get_precheck(conn: sqlite3.Connection, precheck_id: int) -> dict:
    pc = conn.execute("SELECT * FROM precheck_batches WHERE id=?", (precheck_id,)).fetchone()
    if not pc:
        raise ValueError("预检批不存在")
    d = dict(pc)
    d["scan_summary"] = json.loads(d["scan_summary"]) if d["scan_summary"] else None
    d["findings"] = [
        dict(r) for r in conn.execute(
            "SELECT * FROM precheck_findings WHERE precheck_id=? ORDER BY id", (precheck_id,)
        ).fetchall()
    ]
    return d


def latest_for_day(conn: sqlite3.Connection, dock_day: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM precheck_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1", (dock_day,)
    ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["scan_summary"] = json.loads(d["scan_summary"]) if d["scan_summary"] else None
    return d
