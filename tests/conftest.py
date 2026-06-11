"""Общая подготовка для unit-тестов ядра CorePilot.

Тесты покрывают ДЕТЕРМИНИРОВАННЫЕ функции (path-safety, парсеры, DAG-логика,
нормализация ошибок, чекпойнты, diff) — то, что auto_qa как интеграционный
стресс-тест не проверяет на уровне отдельных функций.

Если pydantic не установлен (изолированное окружение), ставим минимальный стаб,
чтобы тесты ядра оставались запускаемыми. На машине с реальным pydantic он
используется как есть.
"""
import os
import sys
import types

# Корень проекта (на уровень выше tests/) — чтобы импортировать utils и пр.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _install_pydantic_stub():
    try:
        import pydantic  # noqa: F401
        return
    except Exception:
        pass

    pyd = types.ModuleType("pydantic")

    class _FI:
        __slots__ = ("default", "default_factory")
        def __init__(self, default=None, default_factory=None):
            self.default = default
            self.default_factory = default_factory

    def Field(default=None, default_factory=None, **_kw):
        return _FI(default, default_factory)

    def computed_field(fn=None, **_kw):
        return (lambda f: f) if fn is None else fn

    class ConfigDict(dict):
        def __init__(self, **kw): super().__init__(**kw)

    class _Meta(type):
        def __new__(mcs, name, bases, ns):
            cls = super().__new__(mcs, name, bases, ns)
            ann = {}
            for b in bases:
                ann.update(getattr(b, "__qa_ann__", {}))
            ann.update(getattr(cls, "__annotations__", {}))
            cls.__qa_ann__ = ann
            return cls

    class BaseModel(metaclass=_Meta):
        def __init__(self, **data):
            for f in type(self).__qa_ann__:
                if f in data:
                    setattr(self, f, data[f]); continue
                raw = getattr(type(self), f, None)
                if isinstance(raw, _FI):
                    setattr(self, f, raw.default_factory() if raw.default_factory else raw.default)
                else:
                    setattr(self, f, raw)
            for k, v in data.items():
                setattr(self, k, v)

        @classmethod
        def model_validate(cls, d):
            return cls(**{k: v for k, v in (d or {}).items() if k in cls.__qa_ann__})

        def model_dump(self):
            return {k: getattr(self, k, None) for k in type(self).__qa_ann__}

        def model_dump_json(self, **_kw):
            import json
            return json.dumps(self.model_dump(), ensure_ascii=False, default=str)

    pyd.BaseModel = BaseModel
    pyd.Field = Field
    pyd.computed_field = computed_field
    pyd.ConfigDict = ConfigDict
    sys.modules["pydantic"] = pyd


def _install_context_stub():
    if "context_manager" not in sys.modules:
        cm = types.ModuleType("context_manager")
        cm.DatabaseManager = object
        sys.modules["context_manager"] = cm


def _install_crewai_stub():
    """Стаб crewai — нужен модулям, которые его импортируют на уровне модуля
    (manager_agents, pipeline_agents). Тестируем чистые функции, не сам crewai."""
    if "crewai" not in sys.modules:
        crewai = types.ModuleType("crewai")
        class _Stub:
            def __init__(self, *a, **k): self.__dict__.update(k)
        crewai.Agent = _Stub
        crewai.Crew = _Stub
        crewai.Task = _Stub
        crewai.LLM = _Stub
        sys.modules["crewai"] = crewai
        tools = types.ModuleType("crewai.tools")
        tools.tool = lambda *a, **k: (lambda f: f)
        sys.modules["crewai.tools"] = tools


_install_pydantic_stub()
_install_context_stub()
_install_crewai_stub()
