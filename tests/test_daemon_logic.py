"""Unit-тесты детерминированной логики Демона: DAG-блокировки (когда задачу можно
брать) и нормализация ошибок (детектор застревания). Без LLM/сети/процессов."""
import sys
import types

import conftest  # noqa: F401


def _load_daemon():
    """Импортирует daemon с замоканными тяжёлыми зависимостями."""
    for m in ("litellm", "chainlit"):
        if m not in sys.modules:
            mod = types.ModuleType(m); 
            if m == "litellm": mod.success_callback = []
            sys.modules[m] = mod
    if "agents" not in sys.modules:
        ag = types.ModuleType("agents")
        for n in ("safe_kickoff", "init_api_keys", "install_secret_redaction",
                  "maybe_unload_between", "consult_oracle_titan"):
            setattr(ag, n, lambda *a, **k: None)
        ag._RedactSecretsFilter = type("F", (), {"filter": lambda s, r: True})
        sys.modules["agents"] = ag
    if "router" not in sys.modules:
        r = types.ModuleType("router"); r.route_task = lambda *a, **k: (0, 0, 0, 0)
        sys.modules["router"] = r
    # pipeline_parser НЕ стабим: реальный импортируется чисто и нужен другим тестам
    # (стаб здесь затёр бы его в sys.modules и сломал test_parsers при общем прогоне).
    import importlib.util, os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    spec = importlib.util.spec_from_file_location("daemon", os.path.join(root, "daemon.py"))
    d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
    return d


d = _load_daemon()


# --- DAG: _deps_satisfied ----------------------------------------------------

def test_deps_empty_allowed():
    ok, _ = d._deps_satisfied({"depends_on": []}, {})
    assert ok is True


def test_deps_all_done_allowed():
    ok, _ = d._deps_satisfied({"depends_on": ["A", "B"]}, {"A": "done", "B": "done"})
    assert ok is True


def test_deps_failed_parent_frozen():
    ok, why = d._deps_satisfied({"depends_on": ["A"]}, {"A": "failed"})
    assert ok is False and "failed" in why.lower() or "провал" in why.lower()


def test_deps_pending_parent_frozen():
    ok, _ = d._deps_satisfied({"depends_on": ["A"]}, {"A": "pending"})
    assert ok is False


def test_deps_missing_parent_frozen():
    ok, _ = d._deps_satisfied({"depends_on": ["GHOST"]}, {})
    assert ok is False


def test_deps_partial_done_frozen():
    # один родитель done, другой failed -> заморожено
    ok, _ = d._deps_satisfied({"depends_on": ["A", "B"]}, {"A": "done", "B": "failed"})
    assert ok is False


# --- Нормализация ошибок (детектор застревания) -----------------------------

def test_error_same_skeleton_different_numbers():
    e1 = d._normalize_error("NameError at line 42 in C:\\proj\\foo.py: x undefined")
    e2 = d._normalize_error("NameError at line 88 in C:\\proj\\bar.py: x undefined")
    assert e1 == e2  # одна ошибка — разные номера строк/пути


def test_error_different_errors_differ():
    e1 = d._normalize_error("NameError: x undefined")
    e2 = d._normalize_error("TypeError: cannot add int and str")
    assert e1 != e2


def test_error_empty():
    assert d._normalize_error("") == ""
    assert d._normalize_error(None) == ""


def test_stuck_threshold_is_two():
    assert d._STUCK_REPEAT_THRESHOLD == 2


def test_max_total_attempts_caps_alternating_errors():
    # Потолок должен быть конечным и больше порога повторов — иначе чередующиеся
    # ошибки крутились бы вечно.
    assert d._MAX_TOTAL_ATTEMPTS >= d._STUCK_REPEAT_THRESHOLD
    assert d._MAX_TOTAL_ATTEMPTS < 100  # разумный конечный предел


def test_titan_attempt_threshold_before_ceiling():
    # Титан должен пробоваться ДО исчерпания лимита (иначе задача провалится,
    # не успев получить переписывание).
    assert d._TITAN_ATTEMPT_THRESHOLD < d._MAX_TOTAL_ATTEMPTS


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\nDAEMON-LOGIC: {len(fns)}/{len(fns)} тестов пройдено")
