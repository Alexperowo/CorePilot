#!/usr/bin/env python3
"""
build_apk.py — Сборщик Android APK для CorePilot Mobile.
Решает проблемы с синтаксисом .bat файлов при парсинге путей.
"""
import os
import sys
import subprocess
import glob

def main():
    print("================================================================")
    print("  CorePilot Mobile — сборка APK (Python-раннер)")
    print("================================================================")
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_dir)
    
    # 1. Настройка PATH: ищем Git и Flutter
    env = dict(os.environ)
    path_dirs = env.get("PATH", "").split(os.pathsep)
    
    # Ищем Git
    git_found = subprocess.run(["where", "git"], capture_output=True).returncode == 0
    if not git_found:
        git_candidates = [
            r"C:\Program Files\Git\cmd",
            r"C:\Program Files (x86)\Git\cmd",
            os.path.expandvars(r"%LocalAppData%\Programs\Git\cmd")
        ]
        for d in git_candidates:
            if os.path.isdir(d):
                path_dirs.insert(0, d)
                print(f"[OK] Git добавлен в PATH: {d}")
                break
    else:
        print("[OK] Git уже в PATH")

    # Ищем Flutter
    flutter_found = subprocess.run(["where", "flutter"], capture_output=True).returncode == 0
    if not flutter_found:
        flutter_candidates = [
            os.path.expandvars(r"%LocalAppData%\flet\flutter\bin"),
            os.path.expandvars(r"%UserProfile%\flutter\bin"),
            os.path.expandvars(r"%LocalAppData%\Pub\Cache\bin"),
            r"C:\flutter\bin"
        ]
        # Добавляем версионированные пути (например, %UserProfile%\flutter\3.38.7\bin)
        user_profile = os.path.expandvars("%UserProfile%")
        versioned_paths = glob.glob(os.path.join(user_profile, "flutter", "*", "bin"))
        flutter_candidates.extend(versioned_paths)
        
        for d in flutter_candidates:
            if os.path.isdir(d) and (os.path.exists(os.path.join(d, "flutter")) or os.path.exists(os.path.join(d, "flutter.bat"))):
                path_dirs.insert(0, d)
                print(f"[OK] Flutter добавлен в PATH: {d}")
                flutter_found = True
                break
    else:
        print("[OK] Flutter уже в PATH")
        
    env["PATH"] = os.pathsep.join(path_dirs)

    # 2. Проверяем установку Flet
    try:
        import flet
        print("[OK] Flet установлен в локальном окружении Python")
    except ImportError:
        print("[!] Flet не найден — устанавливаю flet==0.80.5...")
        subprocess.run([sys.executable, "-m", "pip", "install", "flet==0.80.5"], check=True)

    # 3. Проверяем движок llama-server
    so_path = os.path.join(script_dir, "engine", "arm64-v8a", "libllama-server.so")
    if os.path.isfile(so_path):
        # Проверяем, прописан ли он в pyproject.toml
        toml_path = os.path.join(script_dir, "pyproject.toml")
        with open(toml_path, "r", encoding="utf-8") as f:
            toml_content = f.read()
        if "[tool.flet.android.libs]" not in toml_content:
            print("[!] Движок найден, но не прописан в pyproject.toml. Прописываю...")
            # Запускаем патч
            import _patch_pyproject
            _patch_pyproject.patch_pyproject(os.path.dirname(so_path), toml_path)
        else:
            print("[OK] Движок llama-server прописан в pyproject.toml")
    else:
        print("[!] Движок llama-server НЕ найден в engine/arm64-v8a/")
        print("    Edge AI на телефоне работать не будет (только облако).")

    # 4. Авто-принятие лицензий Android SDK
    sdkmanager_candidates = [
        os.path.expandvars(r"%LocalAppData%\Android\Sdk\cmdline-tools\latest\bin\sdkmanager.bat"),
        os.path.expandvars(r"%LocalAppData%\flet\android-sdk\cmdline-tools\latest\bin\sdkmanager.bat"),
        os.path.expandvars(r"%Android_Home%\cmdline-tools\latest\bin\sdkmanager.bat")
    ]
    for s in sdkmanager_candidates:
        if os.path.isfile(s):
            print(f"[OK] Принимаю лицензии Android SDK с помощью {s}...")
            # Подаем 'y' на все лицензии
            p = subprocess.Popen([s, "--licenses"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, env=env)
            try:
                # Отправляем много 'y'
                p.communicate(input="y\ny\ny\ny\ny\ny\ny\ny\ny\ny\ny\ny\ny\ny\ny\n", timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
            break

    # 5. Запуск сборки APK
    print("\n[3/3] Сборка APK с помощью Flet CLI...")
    try:
        subprocess.run(["flet", "build", "apk", "--verbose"], env=env, check=True)
        print("\n================================================================")
        print("  ГОТОВО. APK успешно собран!")
        print(f"  Файл: {os.path.join(script_dir, 'build', 'flutter', 'build', 'app', 'outputs', 'flutter-apk', 'app-release.apk')}")
        print("================================================================")
    except subprocess.CalledProcessError as e:
        print(f"\n[ОШИБКА] Сборка завершилась ошибкой: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
