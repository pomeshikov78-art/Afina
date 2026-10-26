"""unified/bridge/master_chain_sync.py — сшивка трёх журналов (UNIF-17, шаг 9).

Три журнала единой программы:
  1. мастер-хэш-цепочка платформы «Афина» (линия A, /api/v1/audit + /audit/verify);
  2. NDJSON-журнал контура ENG6-PLAN (линия B, /api/v1/audit; sha256 входа без координат);
  3. единый леджер unified (audit_bridge.UnifiedAuditLedger) — связующее звено:
     run_id паспорта ↔ engine taskId ↔ inputSha256.

cross_anchor() сверяет все три и прикладывает к леджеру отпечатки:
  * platform.audit_verify()            → ok/entries (или broken_at);
  * sha256 канонического JSON записей  → отпечаток среза журнала платформы;
  * записи контура с тем же inputSha256 → matched (сквозная связка);
  * ledger.verify()                    → целостность собственной цепочки.

Итог — протокол «ТРИ ЖУРНАЛА» (JSON): вердикт ANCHORED только если все три
проверки прошли; иначе DEGRADED с причиной (честно, не подменяя результат).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from audit_bridge import UnifiedAuditLedger, _canon, _hash  # noqa: F401  (_canon — канонический JSON)
from backend_arc002 import stable_hash

VERSION = "1.0.0"


def cross_anchor(ledger: UnifiedAuditLedger, platform_client, engine_client,
                 run_id: str, engine_task_id: Optional[str], input_sha256: str,
                 actor: str = "afina-unified") -> Dict[str, Any]:
    """Сверка и якорение трёх журналов; запись результата в единый леджер."""
    # 1. мастер-цепочка платформы
    try:
        plat_verify = platform_client.audit_verify()
        plat_entries = platform_client.audit(limit=500)
        plat_fingerprint = stable_hash(plat_entries)
        plat_err = None
    except Exception as e:
        plat_verify, plat_entries, plat_fingerprint = None, [], None
        plat_err = f"{type(e).__name__}: {e}"

    # 2. журнал контура ENG6-PLAN: связка по inputSha256 (точный canonicalHash ядра)
    try:
        eng = engine_client.audit(limit=100)
        eng_items: List[Dict[str, Any]] = eng.get("items") or eng.get("entries") or []
        eng_matched = [a for a in eng_items if a.get("inputSha256") == input_sha256]
        eng_fingerprint = stable_hash(eng_matched)
        eng_err = None
    except Exception as e:
        eng_matched, eng_fingerprint, eng_err = [], None, f"{type(e).__name__}: {e}"

    # 3. собственный леджер
    led_verify = ledger.verify()

    ok = (plat_err is None and plat_verify and plat_verify.get("ok")
          and eng_err is None and len(eng_matched) >= 1
          and led_verify.get("ok"))
    verdict = "ANCHORED" if ok else "DEGRADED"
    reasons = []
    if plat_err:
        reasons.append(f"платформа недоступна: {plat_err}")
    elif not (plat_verify or {}).get("ok"):
        reasons.append(f"мастер-цепочка платформы повреждена: {plat_verify}")
    if eng_err:
        reasons.append(f"контур недоступен: {eng_err}")
    elif not eng_matched:
        reasons.append("в журнале контура нет записи с данным inputSha256")
    if not led_verify.get("ok"):
        reasons.append(f"единый леджер повреждён: {led_verify}")

    entry = ledger.record(
        "chains_cross_anchored", run_id=run_id, engine_task_id=engine_task_id,
        input_sha256=input_sha256, actor=actor, status=verdict,
        extra={"platform_verify": plat_verify,
               "platform_entries": len(plat_entries) if plat_entries else 0,
               "platform_audit_sha256": plat_fingerprint,
               "engine_matched": len(eng_matched),
               "engine_anchor_sha256": eng_fingerprint,
               "unified_verify": led_verify,
               "reasons": reasons})

    return {
        "document": "ПРОТОКОЛ сшивки трёх журналов (master-chain cross-anchor)",
        "protocol_version": VERSION,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "run_id": run_id,
        "engine_task_id": engine_task_id,
        "input_sha256": input_sha256,
        "verdict": verdict,
        "reasons": reasons,
        "platform": {"verify": plat_verify, "entries": len(plat_entries) if plat_entries else 0,
                     "audit_sha256": plat_fingerprint, "error": plat_err},
        "engine": {"matched": len(eng_matched), "anchor_sha256": eng_fingerprint, "error": eng_err},
        "unified_ledger": {"verify": led_verify, "anchor_seq": entry["seq"], "anchor_hash": entry["hash"]},
    }
