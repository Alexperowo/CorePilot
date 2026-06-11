# CorePilot — запрос на ревью для Gemini 3.1 Pro (баг профилей в DearPyGui)

Десктоп-приложение: Python 3.12, DearPyGui 2.3.1, CrewAI 1.14.6, litellm 1.87.1,
Windows 10. Архитектура: UI (ui_tabs.py) общается ТОЛЬКО с service_layer.py;
агенты/LLM в agents.py; фоновый исполнитель daemon.py.

ГЛАВНЫЙ вопрос — БАГ ПРОФИЛЕЙ (не решён за 6 итераций). Прошу свежий взгляд.

## БАГ: применение профиля настроек НЕ меняет интерфейс

Вкладка «Настройки»: 5 ролей (Сборщик/Архитектор/Кодер/Аудитор/Оракул). У каждой
роли 3 выпадающих списка (combo) с ФИКСИРОВАННЫМИ строковыми тегами:
- источник: set_backend_<role>  (значения: lmstudio/ollama/llamacpp/lemonade/cloud)
- провайдер: set_provider_<role> (gemini/groq/openrouter/...)
- модель: set_model_<role>       (свободный список, тянется с сервера/провайдера)

Блок «Профили»: combo выбора профиля + кнопки Сохранить/Применить/Удалить.

ЧТО РАБОТАЕТ:
- Профили КОРРЕКТНО пишутся в JSON. Проверено — содержимое верное (профиль Local
  имеет backend=lmstudio + локальные модели; Cloud имеет backend=cloud + облачные).
- Сохранение настроек в сессию (.ai_session.json) работает.

СИМПТОМ (подтверждён скриншотами и пользователем):
Пользователь выбирает профиль в combo, жмёт «Применить» — В ИНТЕРФЕЙСЕ НИЧЕГО НЕ
МЕНЯЕТСЯ. Combo ролей остаются со старыми значениями. Перезапуск программы, повторное
«Применить + Сохранить» — НЕ помогает. Воспроизводится стабильно.

## ЧТО УЖЕ ПРОБОВАЛИ (НЕ предлагать повторно — НЕ помогло):

1. _reload(): dpg.set_value(tag, value) по каждому виджету из словаря _widgets.
2. _reload + dpg.configure_item(tag, items=..., default_value=val), затем set_value.
3. Пересборка строк ролей: контейнер role_rows_container, метод _rebuild_role_rows
   делал dpg.delete_item(container, children_only=True) и заново add_combo.
4. Перед пересозданием — ЯВНО dpg.delete_item(tag) каждого combo (освободить алиас),
   проверяя dpg.does_item_exist(tag), потом строить заново. ТОЖЕ НЕ ПОМОГЛО.

## ТЕКУЩИЙ КОД (после всех попыток):

```python
class SettingsTab:
    def __init__(self, notify):
        self._notify = notify
        self._values = {}
        self._widgets = {}            # ключ настройки -> тег виджета
        self._model_options = []

    def build(self):
        with dpg.tab(label="[*] Настройки"):
            self._values = svc.load_settings()
            # ... блок профилей (combo profile_select, кнопки) ...
            with dpg.collapsing_header(label="Модели по ролям (5 агентов)"):
                # ... пояснительный текст ...
                with dpg.group(tag="role_rows_container"):
                    self._build_role_rows()
            # ... остальные настройки (чекбоксы и т.п.) ...

    def _build_role_rows(self):
        for role in svc.ROLES:
            with dpg.group(horizontal=True, parent="role_rows_container"):
                dpg.add_text(f"{svc.ROLE_LABELS[role]:<12}", color=(120,200,255))
                bk_tag = f"set_backend_{role}"
                self._widgets[f"backend_{role}"] = bk_tag
                saved_backend = self._values.get(f"backend_{role}")
                if saved_backend in svc.SOURCES:      src_default = saved_backend
                elif self._values.get(f"mode_{role}")=="cloud": src_default = "cloud"
                else: src_default = "lmstudio"
                dpg.add_combo(svc.SOURCES, tag=bk_tag, width=140,
                              default_value=src_default, user_data=role,
                              callback=self._on_source_change)
                pr_tag = f"set_provider_{role}"
                self._widgets[f"provider_{role}"] = pr_tag
                dpg.add_combo(svc.PROVIDERS, tag=pr_tag, width=130,
                              default_value=self._values.get(f"provider_{role}","gemini"))
                md_tag = f"set_model_{role}"
                self._widgets[f"model_{role}"] = md_tag
                cur_model = self._values.get(f"model_{role}","")
                init_items = list(self._model_options) if self._model_options else []
                if cur_model and cur_model not in init_items:
                    init_items = [cur_model] + init_items
                dpg.add_combo(init_items, tag=md_tag, width=260, default_value=cur_model)

    def _rebuild_role_rows(self):
        for role in svc.ROLES:
            for tag in (f"set_backend_{role}", f"set_provider_{role}", f"set_model_{role}"):
                if dpg.does_item_exist(tag):
                    dpg.delete_item(tag)
        dpg.delete_item("role_rows_container", children_only=True)
        self._build_role_rows()

    def _apply_profile(self):
        name = dpg.get_value("profile_select")
        if not name: return
        ok, msg = svc.apply_profile(name)     # пишет настройки профиля в сессию (диск)
        if ok:
            self._values = svc.load_settings()  # читает применённые настройки
            self._rebuild_role_rows()           # пересоздаёт combo ролей
            self._reload()                      # обновляет общие поля (чекбоксы и пр.)

    def _reload(self):
        self._values = svc.load_settings()
        for key, tag in self._widgets.items():
            if key not in self._values: continue
            val = self._values[key]
            try:
                info = dpg.get_item_configuration(tag)
                if "items" in info:
                    items = list(info.get("items") or [])
                    if val and val not in items:
                        if key.startswith("backend_"):   items = list(svc.SOURCES)
                        elif key.startswith("provider_"): items = list(svc.PROVIDERS)
                        else: items = [val] + items
                    dpg.configure_item(tag, items=items, default_value=val)
                dpg.set_value(tag, val)
            except Exception:
                try: dpg.set_value(tag, val)
                except Exception: pass

    def _on_source_change(self, sender, app_data, user_data):
        # при смене источника очищаем модель (локальное имя невалидно для облака)
        role = user_data
        md_tag = self._widgets.get(f"model_{role}")
        if md_tag:
            dpg.configure_item(md_tag, items=[], default_value="")
            dpg.set_value(md_tag, "")
```

service_layer:
```python
def apply_profile(name):
    profiles = _load_profiles_raw()
    if name not in profiles: return False, "не найден"
    return save_settings(profiles[name])   # setattr на SessionState + save_session

def save_settings(values):
    st = load_session() or SessionState()
    for k,v in values.items():
        if hasattr(st,k): setattr(st,k,v)
    save_session(st); return True, "..."
```

## КЛЮЧЕВЫЕ ВОПРОСЫ К РЕВЬЮ:
1. В DearPyGui 2.x — корректно ли ВООБЩЕ менять дерево виджетов (delete_item +
   add_combo) ВНУТРИ callback-а кнопки (_apply_profile вызывается как callback
   кнопки «Применить»)? Не нужно ли отложить через dpg.set_frame_callback /
   split_frame, т.к. изменение UI во время обработки события конфликтует с
   рендер-циклом? ЭТО ОСНОВНАЯ ГИПОТЕЗА.
2. Если пересоздание в callback недопустимо — какой ТОЧНЫЙ паттерн для отложенного
   пересоздания combo в DPG 2.x?
3. Если пересоздание не нужно, а нужно обновление — какая ТОЧНАЯ последовательность
   configure_item/set_value заставляет mvCombo визуально показать новое значение?
   Влияет ли default_value (применяется только при создании)?
4. Может ли быть, что combo внутри collapsing_header + вложенных group имеют
   проблему с обновлением, если заголовок свёрнут/развёрнут?

## ОТДЕЛЬНЫЙ баг (его я УЖЕ исправил, на ревью не нужен — для контекста):
Тест показал "Сохранённых профилей: 0", хотя профили были. Причина: AUTO_DIR
(папка профилей/очереди) определялся как "./auto_tasks" ОТНОСИТЕЛЬНО рабочей папки.
При запуске из разных cwd (ярлык/.bat/тест) — разные файлы. ИСПРАВЛЕНО: привязал
AUTO_DIR к os.path.dirname(os.path.abspath(__file__)) в service_layer.py и daemon.py.

Стек точно: DearPyGui 2.3.1, Python 3.12.10, Windows 10.
