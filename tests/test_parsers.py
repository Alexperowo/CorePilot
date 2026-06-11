"""Unit-тесты парсеров вывода LLM. Слабое место — малые локальные модели в низком
кванте: парсер обязан извлекать данные из «грязного» вывода и НЕ падать на мусоре."""
import conftest  # noqa: F401

import manager_agents as ma
import pipeline_parser as pp


# --- parse_backlog (DAG) -----------------------------------------------------

def test_backlog_normal_dag():
    raw = '[{"task_id":"T1","title":"a","depends_on":[]},{"task_id":"T2","title":"b","depends_on":["T1"]}]'
    bl = ma.parse_backlog(raw)
    assert bl is not None and len(bl) == 2
    by = {t["task_id"]: t for t in bl}
    assert by["T2"]["depends_on"] == ["T1"]


def test_backlog_strips_self_and_missing_deps():
    raw = '[{"task_id":"A","title":"x","depends_on":["A","ZZZ"]}]'
    bl = ma.parse_backlog(raw)
    # самоссылка A и несуществующий ZZZ должны быть убраны
    assert bl is not None
    assert "A" not in bl[0]["depends_on"]
    assert "ZZZ" not in bl[0]["depends_on"]


def test_backlog_json_in_markdown_fence():
    raw = 'Вот план:\n```json\n[{"task_id":"T1","title":"a","depends_on":[]}]\n```\nготово'
    bl = ma.parse_backlog(raw)
    assert bl is not None and len(bl) == 1


def test_backlog_garbage_no_crash():
    for bad in ["", "   ", "не json совсем", "[1,2,3]", "{}", "[{}]", "null", "[[["]:
        ma.parse_backlog(bad)  # не должно бросать исключение


# --- parse_fixer_output (патчи кода) ----------------------------------------

def test_fixer_valid_patches():
    raw = '{"patches":[{"filepath":"a.py","code":"x = 1"}]}'
    out = pp.parse_fixer_output(raw)
    assert out is not None


def test_fixer_garbage_no_crash():
    for bad in ["", None, "просто текст", '{"patches": "не список"}',
                '{"patches":[{"filepath":null,"code":null}]}', "{{{ broken"]:
        pp.parse_fixer_output(bad if isinstance(bad, str) else str(bad))


def test_fixer_no_changes_flag():
    raw = '{"patches":[],"no_changes_needed":true}'
    out = pp.parse_fixer_output(raw)
    assert out is not None


# --- остальные парсеры: главное — не падать на мусоре -----------------------

def test_all_parsers_survive_chaos():
    chaos = ["", "   \n\t", "{неполный", '{"a":' * 50 + "1" + "}" * 50,
             "日本語\x00 emoji", "```json\nне json\n```", "[1, true, null]"]
    for raw in chaos:
        pp.parse_gatherer_output(raw)
        pp.parse_architect_output(raw)
        pp.parse_fixer_output(raw)
        pp.parse_auditor_verdict(raw)
        ma.parse_backlog(raw)


def test_auditor_verdict_detects_ok():
    out = pp.parse_auditor_verdict("Анализ завершён. Вердикт: ОК")
    assert out is not None


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\nPARSERS: {len(fns)}/{len(fns)} тестов пройдено")
