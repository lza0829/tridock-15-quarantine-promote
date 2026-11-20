"""硬闸架构（架构一）端到端核对：预检批、扫描物化、失效、结案写闸、sealed 快照、并发。

运行：cd backend && .venv/bin/python -m unittest tests.test_precheck_gate -v
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="tridock-test-")
os.environ["TRIDOCK_DATA"] = _tmp

from app import config  # noqa: E402
from app.db import connect  # noqa: E402
from app.seed import seed_if_empty  # noqa: E402
from app.services import precheck_service, reconcile_service  # noqa: E402

DAY = "2026-11-20"
DAY2 = "2026-11-21"
NOW = "2026-11-20T08:00:00Z"


class PrecheckGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        seed_if_empty()

    def setUp(self):
        c = connect()
        for t in (
            "precheck_findings", "precheck_batches", "variance_rows", "match_rows",
            "reconcile_batches", "expect_lines", "receive_lines", "confirm_lines",
        ):
            c.execute(f"DELETE FROM {t}")
        c.commit()
        c.close()

    def conn(self):
        return connect()

    # ---------- 夹具 ----------
    def _add_expect(self, c, doc, sup="SUP-A", sku="SKU-X", qty=10, day=DAY):
        c.execute(
            "INSERT INTO expect_lines(doc_no,supplier_code,sku,dock_day,qty,note,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (doc, sup, sku, day, qty, None, NOW),
        )

    def _add_receive(self, c, rno, sup="SUP-A", sku="SKU-X", qty=10, day=DAY):
        c.execute(
            "INSERT INTO receive_lines(receive_no,supplier_code,sku,dock_day,qty,occurred_at,note,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rno, sup, sku, day, qty, NOW, None, NOW),
        )

    def _add_confirm(self, c, cno, sup="SUP-A", sku="SKU-X", qty=10, day=DAY):
        c.execute(
            "INSERT INTO confirm_lines(confirm_no,supplier_code,sku,dock_day,qty,note,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (cno, sup, sku, day, qty, None, NOW),
        )

    def _aligned_day(self, day=DAY):
        """三腿齐全、数量对齐的一天，跑对账后应能预检通过。"""
        c = self.conn()
        tag = day.replace("-", "")
        self._add_expect(c, f"EX-{tag}", day=day)
        self._add_receive(c, f"RC-{tag}", day=day)
        self._add_confirm(c, f"CF-{tag}", day=day)
        reconcile_service.run_reconcile(c, day)
        c.commit()
        c.close()

    # ---------- 1. 无预检硬闸拒封 ----------
    def test_01_seal_without_precheck_rejected(self):
        self._aligned_day()
        c = self.conn()
        with self.assertRaises(ValueError) as cm:
            reconcile_service.seal_day(c, DAY, 999999)
        self.assertIn("不存在", str(cm.exception))
        row = c.execute(
            "SELECT status, precheck_id FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (DAY,),
        ).fetchone()
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["precheck_id"])  # 不得写入假 id
        c.close()

    # ---------- 2. 预检失败（缺腿/无对账批）不能封印 ----------
    def test_02_failed_precheck_blocks_seal(self):
        c = self.conn()
        self._add_expect(c, "EX-FAIL-1", day=DAY)
        pc = precheck_service.run_precheck(c, DAY)
        self.assertEqual(pc["passed"], 0)
        kinds = {f["kind"] for f in pc["findings"]}
        self.assertIn("missing_leg", kinds)
        self.assertIn("batch_binding", kinds)
        with self.assertRaises(ValueError) as cm:
            reconcile_service.seal_day(c, DAY, pc["id"])
        self.assertIn("未通过", str(cm.exception))
        c.close()

    # ---------- 3. 缺腿扫描物化 ----------
    def test_03_missing_leg_materialized(self):
        c = self.conn()
        self._add_expect(c, "EX-MISS-1", sku="SKU-M", day=DAY)
        self._add_receive(c, "RC-MISS-1", sku="SKU-M", day=DAY)  # 缺回执
        pc = precheck_service.run_precheck(c, DAY)
        finding = [f for f in pc["findings"] if f["kind"] == "missing_leg" and f["sku"] == "SKU-M"][0]
        self.assertEqual(json.loads(finding["detail"])["missing_legs"], ["confirm"])
        stored = c.execute(
            "SELECT COUNT(*) n FROM precheck_findings WHERE precheck_id=? AND kind='missing_leg'",
            (pc["id"],),
        ).fetchone()["n"]
        self.assertGreaterEqual(stored, 1)
        c.close()

    # ---------- 4. 未决差额扫描物化并阻塞 ----------
    def test_04_pending_variance_blocks(self):
        c = self.conn()
        tag = DAY.replace("-", "")
        self._add_expect(c, f"EX-{tag}-V", sku="SKU-V", qty=10)
        self._add_receive(c, f"RC-{tag}-V", sku="SKU-V", qty=8)
        self._add_confirm(c, f"CF-{tag}-V", sku="SKU-V", qty=8)
        reconcile_service.run_reconcile(c, DAY)
        pc = precheck_service.run_precheck(c, DAY)
        self.assertEqual(pc["scan_summary"]["pending_variance_count"], 1)
        self.assertEqual(pc["passed"], 0)
        with self.assertRaises(ValueError):
            reconcile_service.seal_day(c, DAY, pc["id"])
        # 处置差额 → 旧预检失效
        var = c.execute(
            "SELECT id FROM variance_rows WHERE dock_day=? AND fate='pending'", (DAY,)
        ).fetchone()
        reconcile_service.set_variance_fate(c, var["id"], "accept_short")
        stale = c.execute("SELECT status FROM precheck_batches WHERE id=?", (pc["id"],)).fetchone()
        self.assertEqual(stale["status"], "invalidated")
        with self.assertRaises(ValueError) as cm:
            reconcile_service.seal_day(c, DAY, pc["id"])
        self.assertIn("已失效", str(cm.exception))
        c.close()

    # ---------- 5. 完整 happy path：预检通过 + 封印快照绑定 ----------
    def test_05_happy_path_seal_snapshot(self):
        day = "2026-11-22"
        self._aligned_day(day)
        c = self.conn()
        pc = precheck_service.run_precheck(c, day)
        self.assertEqual(pc["passed"], 1)
        self.assertEqual(pc["status"], "valid")
        sealed = reconcile_service.seal_day(c, day, pc["id"])
        self.assertEqual(sealed["status"], "sealed")
        self.assertEqual(sealed["precheck_id"], pc["id"])
        snap = json.loads(sealed["precheck_summary"])
        self.assertEqual(snap["precheck_id"], pc["id"])
        self.assertEqual(snap["precheck_no"], pc["precheck_no"])
        self.assertEqual(snap["legs_fingerprint"], pc["legs_fingerprint"])
        self.assertIn("scan_summary", snap)
        c.close()

    # ---------- 6. 跨日（非本 dock_day）预检复用被拒 ----------
    def test_06_cross_day_precheck_rejected(self):
        day_a, day_b = "2026-11-23", "2026-11-24"
        self._aligned_day(day_a)
        self._aligned_day(day_b)
        c = self.conn()
        pc_a = precheck_service.run_precheck(c, day_a)
        pc_b = precheck_service.run_precheck(c, day_b)
        with self.assertRaises(ValueError) as cm:
            reconcile_service.seal_day(c, day_b, pc_a["id"])
        self.assertIn("不能用于", str(cm.exception))
        # day_b 自己的预检可正常封
        sealed = reconcile_service.seal_day(c, day_b, pc_b["id"])
        self.assertEqual(sealed["status"], "sealed")
        c.close()

    # ---------- 7. 预检后三腿再装载 → 旧预检失效，复用被拒 ----------
    def test_07_leg_reload_invalidates_precheck(self):
        day = "2026-11-25"
        self._aligned_day(day)
        c = self.conn()
        pc = precheck_service.run_precheck(c, day)
        self.assertEqual(pc["status"], "valid")
        # 预报腿再装载（三腿齐补，模拟迟到的完整一单）
        self._add_expect(c, "EX-RELOAD-1", sku="SKU-R", qty=3, day=day)
        self._add_receive(c, "RC-RELOAD-1", sku="SKU-R", qty=3, day=day)
        self._add_confirm(c, "CF-RELOAD-1", sku="SKU-R", qty=3, day=day)
        precheck_service.invalidate_day(c, day, "预报腿再装载")
        c.commit()
        row = c.execute("SELECT status, invalidate_reason FROM precheck_batches WHERE id=?", (pc["id"],)).fetchone()
        self.assertEqual(row["status"], "invalidated")
        self.assertIn("再装载", row["invalidate_reason"])
        with self.assertRaises(ValueError) as cm:
            reconcile_service.seal_day(c, day, pc["id"])
        self.assertIn("已失效", str(cm.exception))
        # 重新预检：对账批覆盖不全 → 不通过；重跑对账后再检 → 通过
        pc2_raw = precheck_service.run_precheck(c, day)
        self.assertEqual(pc2_raw["passed"], 0)
        reconcile_service.run_reconcile(c, day)
        pc2 = precheck_service.run_precheck(c, day)
        self.assertEqual(pc2["passed"], 1)
        sealed = reconcile_service.seal_day(c, day, pc2["id"])
        self.assertEqual(sealed["precheck_id"], pc2["id"])
        c.close()

    # ---------- 8. 重跑对账使旧预检失效 ----------
    def test_08_rerun_reconcile_invalidates(self):
        day = "2026-11-26"
        self._aligned_day(day)
        c = self.conn()
        pc = precheck_service.run_precheck(c, day)
        reconcile_service.run_reconcile(c, day)
        row = c.execute("SELECT status FROM precheck_batches WHERE id=?", (pc["id"],)).fetchone()
        self.assertEqual(row["status"], "invalidated")
        c.close()

    # ---------- 9. 伪造/非法 precheck_id 被拒，且不落库 ----------
    def test_09_forged_id_rejected_not_persisted(self):
        day = "2026-11-27"
        self._aligned_day(day)
        c = self.conn()
        for forged in (0, -1, 424242):
            with self.assertRaises(ValueError):
                reconcile_service.seal_day(c, day, forged)
        row = c.execute(
            "SELECT status, precheck_id, precheck_summary FROM reconcile_batches"
            " WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        self.assertEqual(row["status"], "open")
        self.assertIsNone(row["precheck_id"])
        self.assertIsNone(row["precheck_summary"])
        c.close()

    # ---------- 10. 指纹漂移直攻：绕过失效钩子直接插腿，封印时仍被硬闸拦下 ----------
    def test_10_fingerprint_drift_caught_at_seal(self):
        day = "2026-11-28"
        self._aligned_day(day)
        c = self.conn()
        pc = precheck_service.run_precheck(c, day)
        # 模拟任何未挂失效钩子的写入路径：直接 SQL 改三腿
        self._add_confirm(c, "CF-DRIFT-1", sku="SKU-DRIFT", qty=1, day=day)
        c.commit()
        with self.assertRaises(ValueError) as cm:
            reconcile_service.seal_day(c, day, pc["id"])
        self.assertIn("预检后三腿数据发生变化", str(cm.exception))
        # 自愈：漂移预检已被作废
        row = c.execute("SELECT status FROM precheck_batches WHERE id=?", (pc["id"],)).fetchone()
        self.assertEqual(row["status"], "invalidated")
        # 批次仍 open，未被半截绑定
        b = c.execute("SELECT status, precheck_id FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1", (day,)).fetchone()
        self.assertEqual(b["status"], "open")
        self.assertIsNone(b["precheck_id"])
        c.close()

    # ---------- 11. 并发预检与结案：不得出现两个 sealed / 半截绑定 ----------
    def test_11_concurrent_seal_only_one_wins(self):
        day = "2026-11-29"
        self._aligned_day(day)
        c = self.conn()
        pc = precheck_service.run_precheck(c, day)
        pc_id = pc["id"]
        c.close()

        errors: list[Exception] = []
        results: list[str] = []

        def worker():
            conn = connect()
            try:
                sealed = reconcile_service.seal_day(conn, day, pc_id)
                results.append(sealed["status"])
            except Exception as e:  # noqa: BLE001
                errors.append(e)
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        c = self.conn()
        sealed_n = c.execute(
            "SELECT COUNT(*) n FROM reconcile_batches WHERE dock_day=? AND status='sealed'", (day,)
        ).fetchone()["n"]
        sealed_rows = c.execute(
            "SELECT precheck_id, precheck_summary FROM reconcile_batches"
            " WHERE dock_day=? AND status='sealed'",
            (day,),
        ).fetchall()
        self.assertEqual(sealed_n, 1)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 3)
        for r in sealed_rows:
            self.assertEqual(r["precheck_id"], pc_id)
            self.assertIsNotNone(r["precheck_summary"])
        c.close()

    # ---------- 12. 并发：一边装载（作废预检）一边结案，结案方必须失败且无半截绑定 ----------
    def test_12_concurrent_load_vs_seal(self):
        day = "2026-11-30"
        self._aligned_day(day)
        c = self.conn()
        pc = precheck_service.run_precheck(c, day)
        pc_id = pc["id"]
        c.close()

        seal_err: list[Exception] = []

        def sealer():
            conn = connect()
            try:
                reconcile_service.seal_day(conn, day, pc_id)
            except Exception as e:  # noqa: BLE001
                seal_err.append(e)
            finally:
                conn.close()

        def loader():
            conn = connect()
            try:
                # 与封印事务竞争写锁；无论谁先拿到锁，结果只允许两种：
                # 装载先 → 预检 invalidated，封印拒；封印先 → 装载被 sealed 写闸拒。
                try:
                    precheck_service.invalidate_day(conn, day, "并发装载作废预检")
                    self._add_expect(conn, "EX-CONC-1", sku="SKU-C", qty=1, day=day)
                    conn.commit()
                except Exception:
                    conn.rollback()
            finally:
                conn.close()

        t1 = threading.Thread(target=sealer)
        t2 = threading.Thread(target=loader)
        t1.start(); t2.start()
        t1.join(timeout=30); t2.join(timeout=30)

        c = self.conn()
        pc_row = c.execute("SELECT status FROM precheck_batches WHERE id=?", (pc_id,)).fetchone()
        b = c.execute(
            "SELECT status, precheck_id FROM reconcile_batches WHERE dock_day=? ORDER BY id DESC LIMIT 1",
            (day,),
        ).fetchone()
        if b["status"] == "sealed":
            # 封印先成功：装载必须被写闸挡住，预检仍 valid，绑定完整
            self.assertEqual(pc_row["status"], "valid")
            self.assertEqual(b["precheck_id"], pc_id)
            self.assertEqual(seal_err, [])
        else:
            # 装载先生效：预检失效，封印被拒，批次保持 open 且无假绑定
            self.assertEqual(pc_row["status"], "invalidated")
            self.assertIsNone(b["precheck_id"])
            self.assertEqual(len(seal_err), 1)
        c.close()

    # ---------- 13. 结案后三腿写闸仍生效，且不能再跑预检/对账 ----------
    def test_13_post_seal_write_gate(self):
        day = "2026-12-01"
        self._aligned_day(day)
        c = self.conn()
        pc = precheck_service.run_precheck(c, day)
        reconcile_service.seal_day(c, day, pc["id"])
        c.close()

        c = self.conn()
        from app.services.gate import assert_writable
        with self.assertRaises(ValueError):
            assert_writable(c, day)
        with self.assertRaises(ValueError):
            precheck_service.run_precheck(c, day)
        with self.assertRaises(ValueError):
            reconcile_service.run_reconcile(c, day)
        c.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
