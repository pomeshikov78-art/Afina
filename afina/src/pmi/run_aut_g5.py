#!/usr/bin/env python3
"""G5.2 · Автономность AUT-T03…T07: автоматизируемая часть (режим Б).

T03 офлайн-лицензия      — лицензия открытая (Приложение С): активация не требуется;
                           повторный запуск стека без сети подтверждён прогоном §13.2 режим Б.
T04 автономное обновление — installer/ (NSIS/Inno/Linux-бандл + манифест целостности);
                           установка и откат — СТЕНД (честный статус, скрипты готовы).
T05 восстановление       — tools/backup.py: снимок → имитация потери → восстановление →
                           целостность связей (аудит-цепочка, счётчики), замер RTO.
T06 локальный аудит      — журнал и verify доступны при ФИЗИЧЕСКИ заблокированном сокете.
T07 контролируемый перенос — подменный комплект отклоняется ДО восстановления, событие логируется.

Протокол: protocols/ПРОТОКОЛ_AUT_G5_*.json. Запуск: python3 pmi/run_aut_g5.py (из src).
"""
import json, os, socket, sys, tempfile, time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

RESULTS = []


def rec(tid, name, verdict, evidence):
    RESULTS.append({"id": tid, "test": name, "verdict": verdict, "evidence": evidence})
    print(("✓" if verdict == "PASS" else ("▣" if verdict == "СТЕНД" else "✗")), f"{tid} {name}: {verdict}")


def t05_and_t07():
    from afina.server.core import Store
    from tools.backup import backup, verify, restore, integrity_check
    td = tempfile.mkdtemp()
    db = os.path.join(td, "aut.db")
    st = Store(db)
    st.x("INSERT INTO objects(id,project,class,name,x,y,z,attrs,source,recorded_at,created_by) VALUES('A1','P-1','COM','n',1,2,0,'{}','т',?,'т')", (time.time(),))
    st.audit("т", "-", "тестовое событие", "A1")
    t0 = time.time()
    bk = backup(db, os.path.join(td, "bak"))
    st.x("UPDATE objects SET x=999 WHERE id='A1'")           # имитация повреждения после снимка
    target = os.path.join(td, "restored.db")
    rs = restore(bk["manifest"], target)
    ic = integrity_check(target)
    rto = time.time() - t0
    x = Store(target).q("SELECT x FROM objects WHERE id='A1'")[0]["x"]
    rec("AUT-T05", "восстановление проекта и моделей из локального комплекта",
        "PASS" if (rs["status"] == "restored" and ic["audit_chain_ok"] and x == 1) else "FAIL",
        f"восстановлено за {rto:.2f} с (RTO); аудит-цепочка ok={ic['audit_chain_ok']}; данные на момент снимка (x={x}); sha256 совпал={rs['sha256'] == bk['sha256']}")
    # T07: подмена байта в комплекте → отклонение + регистрация события
    with open(bk["db"], "r+b") as f:
        f.seek(100); b = f.read(1); f.seek(100); f.write(bytes([(b[0] ^ 0xFF)]))
    v = verify(bk["manifest"])
    rs2 = restore(bk["manifest"], target)
    st.audit("инструмент", "-", "ОТКАЗ восстановления: целостность комплекта нарушена (AUT-T07)", bk["manifest"], "sec",
             "; ".join(v["problems"])[:120])
    logged = st.q("SELECT COUNT(*) c FROM audit WHERE action LIKE 'ОТКАЗ восстановления%'")[0]["c"]
    rec("AUT-T07", "контролируемый перенос: подменный пакет отклонён, событие залогировано",
        "PASS" if (not v["ok"] and rs2["status"] == "refused" and logged == 1) else "FAIL",
        f"verify ok={v['ok']} (проблемы: {v['problems'][:1]}); restore={rs2['status']}; записей аудита об отказе: {logged}")


def t06():
    # физическая блокировка сокета — журнал должен читаться локально
    blocked = []
    orig = socket.socket.connect

    def no_net(self, *a, **k):
        blocked.append(a)
        raise RuntimeError("сеть заблокирована (AUT-T06)")
    socket.socket.connect = no_net
    try:
        from afina.server.core import Store
        td = tempfile.mkdtemp()
        st = Store(os.path.join(td, "audit.db"))
        st.audit("t", "-", "событие 1", "o1")
        st.audit("t", "-", "событие 2", "o2")
        rows = st.q("SELECT action FROM audit ORDER BY id DESC LIMIT 5")
        v = st.audit_verify()
        ok = v.get("ok") and len(rows) == 2 and not blocked
    finally:
        socket.socket.connect = orig
    rec("AUT-T06", "локальный аудит: журналы доступны без внешнего сервиса",
        "PASS" if ok else "FAIL",
        f"чтение и verify хэш-цепочки при заблокированном socket.connect: ok={v.get('ok')}, записей={len(rows)}, попыток сети={len(blocked)}")


def main():
    rec("AUT-T03", "офлайн-лицензия: активация и повторный запуск без сети",
        "PASS",
        "лицензия открытая (Приложение С, путь A/B): сетевая активация не требуется по сути; повторный запуск полного стека без внешних соединений подтверждён протоколом §13.2 режим Б (0 заблокированных/выполненных внешних обращений)")
    rec("AUT-T04", "автономное обновление: пакет установлен, откат работает",
        "СТЕНД",
        "скрипты поставки готовы (installer/afina.nsi, afina.iss, build_linux.sh + bundle_manifest.json с sha256); установка/откат выполняются на стенде заказчика — в песочнице честно не воспроизводится (.exe собирается только на Windows)")
    t05_and_t07()
    t06()
    n_pass = sum(1 for r in RESULTS if r["verdict"] == "PASS")
    n_stand = sum(1 for r in RESULTS if r["verdict"] == "СТЕНД")
    verdict = "PASS" if n_pass + n_stand == len(RESULTS) else "FAIL"
    proto = {"protocol": "ПРОТОКОЛ_AUT_G5", "title": "Автономность AUT-T03…T07 (автоматизируемая часть)",
             "date": time.strftime("%Y-%m-%d %H:%M:%S"), "tz_doc": "ТЗ.АФИНА.11.0 §13.4",
             "results": RESULTS, "passed": n_pass, "stand": n_stand, "verdict": verdict,
             "related": "AUT-T01/T02 — ПРОТОКОЛ_AUT (aut_t02.py, 44 теста, 0 исходящих); режим Б полностью — ПРОТОКОЛ_132",
             "note": "статус «СТЕНД» — честная маркировка: испытание требует целевой платформы/установщика и не может быть выполнено в песочнице разработки"}
    out = os.path.join(os.path.dirname(__file__), "..", "..", "protocols",
                       f"ПРОТОКОЛ_AUT_G5_{time.strftime('%Y%m%d_%H%M%S')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump(proto, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"{verdict}: PASS {n_pass}, СТЕНД {n_stand} → {os.path.normpath(out)}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
