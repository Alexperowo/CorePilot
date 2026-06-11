import sqlite3
import time
import json
import threading
import logging

logger = logging.getLogger("CONTEXT")


class ContextManager:
    """Хранилище логов/задач/хартбитов на SQLite.

    Использует ОДИН долгоживущий коннект на экземпляр (а не открывает новый на
    каждый вызов) — это убирает оверхед переподключений и, главное, ошибку
    'database is locked' при частых хартбитах и параллельных воркерах Демона.
    Конкурентная запись из разных потоков (Демон + UI) сериализуется внутренним
    замком; WAL-режим разрешает параллельные чтения во время записи.
    """

    def __init__(self, db_path: str = "factory_data.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        # check_same_thread=False: коннект разделяется потоками, но любые операции
        # с ним мы оборачиваем в self._lock, поэтому это безопасно.
        self._conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")     # параллельные чтения + запись
        self._conn.execute("PRAGMA synchronous=NORMAL")   # быстрее, безопасно при WAL
        self._conn.execute("PRAGMA busy_timeout=30000")   # ждать снятия блокировки до 30с
        self._init_db()

    def _init_db(self):
        with self._lock:
            c = self._conn.cursor()
            c.execute('CREATE TABLE IF NOT EXISTS pipeline_logs (id INTEGER PRIMARY KEY, timestamp REAL, step_name TEXT, message TEXT, status TEXT, applied_fixes TEXT)')
            c.execute('CREATE TABLE IF NOT EXISTS cleaner_history (id INTEGER PRIMARY KEY, timestamp REAL, freed_mb REAL, items_removed INTEGER)')
            c.execute('CREATE TABLE IF NOT EXISTS manager_tasks (filename TEXT PRIMARY KEY, status TEXT, updated_at REAL)')
            c.execute('CREATE TABLE IF NOT EXISTS daemon_heartbeat (id INTEGER PRIMARY KEY, pid INTEGER, status TEXT, details TEXT, ts REAL)')
            self._conn.commit()

    def _execute(self, sql: str, params: tuple = ()):
        """Потокобезопасная запись с единым коннектом. Не роняет вызывающий код:
        сбой БД логируется, но Демон/UI продолжают работать (БД — вспомогательная)."""
        with self._lock:
            try:
                self._conn.execute(sql, params)
                self._conn.commit()
            except sqlite3.Error as e:
                logger.warning("SQLite ошибка при выполнении запроса: %s", e)

    def _query(self, sql: str, params: tuple = ()) -> list:
        with self._lock:
            try:
                return self._conn.execute(sql, params).fetchall()
            except sqlite3.Error as e:
                logger.warning("SQLite ошибка при чтении: %s", e)
                return []

    def add_manager_task(self, filename: str, status: str = 'queued') -> None:
        self._execute('INSERT OR IGNORE INTO manager_tasks (filename, status, updated_at) VALUES (?, ?, ?)',
                      (filename, status, time.time()))

    def update_manager_task_status(self, filename: str, status: str) -> None:
        self._execute('UPDATE manager_tasks SET status = ?, updated_at = ? WHERE filename = ?',
                      (status, time.time(), filename))

    def get_manager_stats(self) -> dict:
        rows = self._query('SELECT status, COUNT(*) FROM manager_tasks GROUP BY status')
        return {r[0]: r[1] for r in rows}

    def log_daemon_heartbeat(self, pid: int, status: str, details: str = '') -> None:
        with self._lock:
            try:
                self._conn.execute('INSERT INTO daemon_heartbeat (pid, status, details, ts) VALUES (?, ?, ?, ?)',
                                   (pid, status, details, time.time()))
                # Тримминг таблицы изредка (дорогой подзапрос не на каждый heartbeat).
                import random
                if random.random() < 0.02:
                    self._conn.execute('DELETE FROM daemon_heartbeat WHERE id NOT IN (SELECT id FROM daemon_heartbeat ORDER BY id DESC LIMIT 2000)')
                self._conn.commit()
            except sqlite3.Error as e:
                logger.warning("SQLite ошибка при записи heartbeat: %s", e)

    def log_cleaner_run(self, freed_mb: float, items_removed: int):
        self._execute('INSERT INTO cleaner_history (timestamp, freed_mb, items_removed) VALUES (?, ?, ?)',
                      (time.time(), freed_mb, items_removed))

    def close(self) -> None:
        """Закрывает коннект (например, при остановке Демона). Идемпотентно."""
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


DatabaseManager = ContextManager
