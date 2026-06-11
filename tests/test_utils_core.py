"""Unit-тесты ядровых утилит: чекпойнты (возобновление), unified diff, атомарная
запись, строгий парсер патчей."""
import os
import tempfile

import conftest  # noqa: F401
from utils import (PipelineCheckpoint, generate_unified_diff, atomic_write_text,
                   strict_parse_fixes, extract_agent_reasoning)


# --- Чекпойнты ---------------------------------------------------------------

def test_checkpoint_save_get_roundtrip():
    proj = tempfile.mkdtemp()
    ck = PipelineCheckpoint(proj, "задача про логирование")
    assert ck.get("gather") is None
    ck.save("gather", "<манифест>")
    # новый объект на тот же контент видит сохранённое (персистентность на диск)
    ck2 = PipelineCheckpoint(proj, "задача про логирование")
    assert ck2.get("gather") == "<манифест>"


def test_checkpoint_isolated_by_content():
    proj = tempfile.mkdtemp()
    PipelineCheckpoint(proj, "задача A").save("fix", "codeA")
    ckB = PipelineCheckpoint(proj, "задача B")
    assert ckB.get("fix") is None  # разный контент — разный чекпойнт


def test_checkpoint_clear():
    proj = tempfile.mkdtemp()
    ck = PipelineCheckpoint(proj, "x")
    ck.save("gather", "data")
    ck.clear()
    assert PipelineCheckpoint(proj, "x").get("gather") is None


def test_checkpoint_corrupt_file_no_crash():
    proj = tempfile.mkdtemp()
    ck = PipelineCheckpoint(proj, "x")
    ck.save("gather", "data")  # создаёт папку и файл
    # портим файл чекпойнта
    with open(ck.path, "w", encoding="utf-8") as f:
        f.write("{ битый json ,,,")
    ck2 = PipelineCheckpoint(proj, "x")  # не должно бросать
    assert ck2.get("gather") is None  # битый -> игнор, дефолт


# --- Unified diff ------------------------------------------------------------

def test_diff_shows_changes():
    diff = generate_unified_diff("a = 1\n", "a = 2\n", "foo.py")
    assert "-a = 1" in diff and "+a = 2" in diff


def test_diff_empty_for_identical():
    diff = generate_unified_diff("same\n", "same\n", "foo.py")
    assert "+same" not in diff and "-same" not in diff


# --- Атомарная запись --------------------------------------------------------

def test_atomic_write_creates_file():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "out.txt")
    atomic_write_text(p, "содержимое")
    with open(p, encoding="utf-8") as f:
        assert f.read() == "содержимое"


def test_atomic_write_no_tmp_leftover():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "out.txt")
    atomic_write_text(p, "данные")
    leftovers = [f for f in os.listdir(d) if f.endswith(".tmp")]
    assert not leftovers


# --- strict_parse_fixes / extract_agent_reasoning ---------------------------

def test_extract_reasoning_no_crash_on_garbage():
    for raw in ["", "просто текст", "<think>x</think>код"]:
        extract_agent_reasoning(raw)


def test_strict_parse_fixes_garbage_no_crash():
    for raw in ["", "не json", "{}", '{"patches":[]}']:
        strict_parse_fixes(raw)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\nUTILS-CORE: {len(fns)}/{len(fns)} тестов пройдено")
