#!/usr/bin/env python3
"""
_patch_pyproject.py — автоматическое обновление pyproject.toml.

Сканирует engine/arm64-v8a/, генерирует секцию [tool.flet.android.libs]
с записями для каждого .so файла. Если секция уже существует — обновляет её.
Закомментированный пример удаляется.

Вызывается из setup_engine.bat / update_engine.bat автоматически.
"""
from __future__ import annotations

import os
import re
import sys


def patch_pyproject(engine_dir: str, toml_path: str) -> bool:
    """Сканирует engine_dir, обновляет [tool.flet.android.libs] в toml_path."""

    # --- Собрать список .so файлов ---
    try:
        so_files = sorted(f for f in os.listdir(engine_dir) if f.endswith(".so"))
    except OSError as e:
        print(f"[ОШИБКА] Не удалось прочитать {engine_dir}: {e}")
        return False

    if not so_files:
        print("[ОШИБКА] В engine/arm64-v8a/ нет файлов .so")
        return False

    has_server = any("llama-server" in f or "llama_server" in f for f in so_files)
    if not has_server:
        print("[ПРЕДУПРЕЖДЕНИЕ] libllama-server.so не найден — движок может не запуститься")

    # --- Сгенерировать новую секцию ---
    section_lines = ["[tool.flet.android.libs]"]
    for f in so_files:
        rel_path = f"engine/arm64-v8a/{f}"
        section_lines.append(f'"arm64-v8a/{f}" = "{rel_path}"')
    new_section = "\n".join(section_lines) + "\n"

    # --- Прочитать pyproject.toml ---
    with open(toml_path, "r", encoding="utf-8") as fh:
        content = fh.read()

    # --- Удалить закомментированный пример [tool.flet.android.libs] ---
    # Паттерн: блок комментариев, содержащий [tool.flet.android.libs]
    # Удаляем строки от '#   [tool.flet.android.libs]' до первой непустой
    # строки без '#' (или до пустой строки после блока комментариев).
    content = re.sub(
        r"^#\s*\[tool\.flet\.android\.libs\].*?(?=\n(?:[^#\n]|\n))",
        "",
        content,
        flags=re.MULTILINE | re.DOTALL,
    )

    # --- Удалить существующую реальную секцию [tool.flet.android.libs] ---
    # От заголовка секции до следующего заголовка [... ] или конца файла.
    content = re.sub(
        r"\[tool\.flet\.android\.libs\]\s*\n(?:(?!\[).)*",
        "",
        content,
        flags=re.DOTALL,
    )

    # Убрать тройные+ пустые строки
    content = re.sub(r"\n{3,}", "\n\n", content)

    # --- Вставить новую секцию перед [tool.flet.app] ---
    if "[tool.flet.app]" in content:
        content = content.replace(
            "[tool.flet.app]",
            new_section + "\n[tool.flet.app]",
        )
    else:
        # Нет секции app — добавим в конец
        content = content.rstrip() + "\n\n" + new_section

    # --- Записать ---
    with open(toml_path, "w", encoding="utf-8") as fh:
        fh.write(content)

    print(f"[OK] pyproject.toml обновлён: {len(so_files)} библиотек прописано")
    for f in so_files:
        print(f"     + {f}")
    return True


if __name__ == "__main__":
    script_dir = os.path.dirname(os.path.abspath(__file__))
    engine_dir = os.path.join(script_dir, "engine", "arm64-v8a")
    toml_path = os.path.join(script_dir, "pyproject.toml")

    if not os.path.isdir(engine_dir):
        print(f"[ОШИБКА] Папка не найдена: {engine_dir}")
        print("         Сначала запустите setup_engine.bat для скачивания движка.")
        sys.exit(1)
    if not os.path.isfile(toml_path):
        print(f"[ОШИБКА] Файл не найден: {toml_path}")
        sys.exit(1)

    ok = patch_pyproject(engine_dir, toml_path)
    sys.exit(0 if ok else 1)
