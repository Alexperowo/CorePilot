"""Unit-тесты безопасности путей. Это критичный модуль: галлюцинированный или
вредоносный путь от LLM не должен вырваться за пределы проекта или уронить ядро."""
import os
import tempfile

import conftest  # noqa: F401  (ставит стабы, добавляет корень в sys.path)
from utils import safe_resolve_path


def _base():
    return tempfile.mkdtemp(prefix="cp_pathtest_")


def test_normal_relative_path_ok():
    base = _base()
    res = safe_resolve_path(base, "sub/dir/file.py")
    assert res.startswith(os.path.realpath(base))
    assert res.endswith("file.py")


def test_absolute_path_blocked():
    base = _base()
    # POSIX-абсолютный путь блокируется на любой ОС.
    try:
        safe_resolve_path(base, "/etc/passwd")
        assert False, "POSIX абсолютный путь не заблокирован"
    except ValueError:
        pass
    # Windows-путь: на Windows блокируется (is_absolute), на POSIX превращается в
    # безобидное имя ВНУТРИ base. Главный инвариант — никогда не вырывается наружу.
    try:
        res = safe_resolve_path(base, "C:\\Windows\\System32\\evil.dll")
        assert os.path.realpath(base) in os.path.realpath(res), "вырвался за base!"
    except ValueError:
        pass  # заблокирован — тоже ок


def test_traversal_blocked():
    base = _base()
    for p in ("../../etc/passwd", "../../../secret", "a/../../b"):
        try:
            safe_resolve_path(base, p)
            assert False, f"traversal не заблокирован: {p}"
        except ValueError:
            pass


def test_null_byte_blocked():
    base = _base()
    try:
        safe_resolve_path(base, "a\x00.py")
        assert False, "null-байт не заблокирован"
    except ValueError:
        pass


def test_pathological_path_no_crash():
    """Тысячи сегментов не должны ронять ядро RecursionError — только ValueError."""
    base = _base()
    huge = "a/" * 5000 + "f.py"
    try:
        safe_resolve_path(base, huge)
        # если не бросил — путь всё равно должен быть внутри base
    except ValueError:
        pass  # ожидаемо
    except RecursionError:
        assert False, "RecursionError не пойман — ядро уязвимо к галлюцинации пути"


def test_nested_valid_stays_inside():
    base = _base()
    res = safe_resolve_path(base, "x/y/z.py")
    assert os.path.realpath(base) in os.path.realpath(res)


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn(); print(f"  ok  {fn.__name__}")
    print(f"\nPATH-SAFETY: {len(fns)}/{len(fns)} тестов пройдено")
