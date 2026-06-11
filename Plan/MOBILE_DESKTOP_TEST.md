# MOBILE_DESKTOP_TEST.md — Тест мобильного + десктопа

## Этап 1: Подготовка (ВЫПОЛНЕНО ранее)
- [x] `llama_manager.py` поддерживает `lan_access=True` → биндит на `0.0.0.0`
- [x] Тумблер «Доступ с телефона (LAN)» есть в UI (вкладка Llama-сервер)
- [x] Токен авторизации хранится в `server_token.txt`, передаётся Bearer-заголовком
- [x] APK собран (144.6 МБ с движком llama.cpp для arm64)

## Этап 2: Поднять эмулятор (ТРЕБУЕТ ЖИВОЙ КОНСОЛИ)
```
avdmanager create avd -n CorePilot_Test -k "system-images;android-33;google_apis;x86_64"
emulator -avd CorePilot_Test -no-snapshot-load
adb wait-for-device
```
Проверить что устройство видно: `adb devices`

## Этап 3: Установить APK на эмулятор
```
install_apk.bat
# или вручную:
adb install "mobile\build\flutter\build\app\outputs\flutter-apk\app-release.apk"
```

## Этап 4: Тест прав хранилища на эмуляторе
- Запустить APK → вкладка Edge AI
- Убедиться что появляется кнопка «Выдать доступ к папке моделей»
- Нажать → открывается системный экран прав → выдать → список моделей появляется
- Лог: `adb logcat -d -s flutter`

## Этап 5: Тест подключения к ПК
На ПК:
1. Включить тумблер «Доступ с телефона (LAN)» → запустить Llama-сервер
2. Узнать IP ПК (`ipconfig`, адрес Wi-Fi)
3. Скопировать токен из `server_token.txt`

На мобильном (Настройки):
- Хост: `10.0.2.2:8080` (эмулятор) ИЛИ `192.168.x.x:8080` (реальный телефон)
- Вставить токен
- Вкладка «Чат» → выбрать «PC CorePilot» → отправить тестовое сообщение

## Этап 6: Диагностика при проблемах
```
adb logcat -d | Select-String "CorePilot|python|flet|Exception"
# просмотр последних строк краша:
adb logcat -d -s AndroidRuntime | tail -30
```

**Hot-patch без пересборки APK (если нужно быстро проверить правку main.py):**
```
adb root
adb push mobile\main.py /data/user/0/com.corepilot.corepilot_mobile/files/flet/app/main.py
adb shell am force-stop com.corepilot.corepilot_mobile
adb shell am start -n com.corepilot.corepilot_mobile/.MainActivity
```
