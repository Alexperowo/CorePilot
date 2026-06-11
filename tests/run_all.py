#!/usr/bin/env python3
"""Запуск всех unit-тестов ядра без pytest (фолбэк для окружений без него).

С установленным pytest предпочтительно:  pytest tests/
Без pytest:                              python tests/run_all.py
"""
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import conftest  # noqa: F401,E402  (ставит стабы и корень проекта в sys.path)

import importlib


def main() -> int:
    modules = [f[:-3] for f in sorted(os.listdir(_HERE))
               if f.startswith("test_") and f.endswith(".py")]
    total = passed = failed = 0
    failures = []
    for modname in modules:
        mod = importlib.import_module(modname)
        fns = [(k, v) for k, v in sorted(vars(mod).items())
               if k.startswith("test_") and callable(v)]
        print(f"\n=== {modname} ({len(fns)} тестов) ===")
        for name, fn in fns:
            total += 1
            try:
                fn()
                passed += 1
                print(f"  ok   {name}")
            except Exception as e:
                failed += 1
                failures.append((modname, name, e))
                print(f"  FAIL {name}: {type(e).__name__}: {e}")

    print("\n" + "=" * 56)
    print(f"  ИТОГО: {passed}/{total} пройдено, {failed} провалено")
    if failures:
        print("  Провалы:")
        for m, n, e in failures:
            print(f"    {m}::{n} — {e}")
    print("=" * 56)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
