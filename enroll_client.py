"""enroll_client — приложение само просит токен у приёмника мониторинга (monitor_server), без ручного копирования.

Как это идёт:
  1. В сборке лежит ключ регистрации (enroll_key.txt рядом с VERSION; build.bat берёт его из monitor_server\\data\\server.json).
     Нет ключа (запуск из исходников, чужая сборка) — молчим, на приёмник не стучимся.
  2. Токена нет — раз в минуту шлём заявку POST <адрес>/enroll {key, machine, name, version, secret}. Адреса пробуем по очереди:
     рабочий с прошлого раза, адрес из настроек, зашитые (сеть ZeroTier «pc len», старая сеть, ретранслятор Буи).
  3. Макс жмёт «Принять» в панели (или кнопку в Telegram) — приёмник кладёт токен; на очередную заявку отвечает им ОДИН раз.
     secret — наш одноразовый пароль заявки: токен получит только тот, кто её подал.
  4. Токен, адрес и «Отправлять» сохраняются в monitor.json (sysmon.save_config) — дальше всё как при ручной настройке.
Канал закрытый (ZeroTier / локалка), поэтому по http и мимо системного прокси; шифровать отдельно не нужно (решение Макса 03.11).
Адрес в интернете (https://…, туннель или свой сервер) тоже годится: он идёт через системный прокси, как пульс.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import time
import urllib.error
import urllib.request
import uuid
from typing import Callable, Optional

from logger import log_event, mask_secrets
from paths import BUNDLE_DIR, DATA_DIR, read_version

# адреса приёмника по умолчанию: сеть ZeroTier «pc len» (ПК Макса), прежняя сеть angry-banan на время переезда, ретранслятор сервера Буи
DEFAULT_SERVERS = ("http://172.29.134.237:8769", "http://11.11.11.12:8769", "http://192.168.2.198:8769")
KEY_FILE = BUNDLE_DIR / "enroll_key.txt"
STATE_FILE = DATA_DIR / "enroll.json"        # секрет заявки; вне каталога обновления
PERIOD_S = 60                                # заявка раз в минуту, пока не решат
BACKOFF_MAX_S = 300                          # нет связи ни с одним адресом — пауза растёт до 5 минут
REJECTED_WAIT_S = 6 * 3600                   # отклонили — ещё раз спросим не раньше чем через 6 часов
HTTP_TIMEOUT_S = 6


def read_key() -> str:
    """Ключ регистрации из сборки (или переменная WEBMVS_ENROLL_KEY — для проверок). Нет — пусто."""
    env = os.environ.get("WEBMVS_ENROLL_KEY", "").strip()
    if env:
        return env
    try:
        return KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def machine_id() -> str:
    """Постоянный номер этого ПК: хеш MachineGuid Windows (не меняется от переустановки приложения), запасной — имя и MAC."""
    raw = ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0, winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
            raw = str(winreg.QueryValueEx(k, "MachineGuid")[0])
    except Exception:
        raw = ""
    if not raw:
        raw = "%s-%012x" % (socket.gethostname(), uuid.getnode())
    return hashlib.sha256(("web_MVS:" + raw).encode("utf-8")).hexdigest()[:32]


def post_json(url: str, body: dict, timeout: float = HTTP_TIMEOUT_S) -> tuple[Optional[int], Optional[dict], str]:
    """POST JSON. http:// — закрытая сеть (ZeroTier, локалка): мимо системного прокси; https:// — адрес в интернете: через системный прокси,
    как и пульс. (HTTP-код или None, ответ-словарь или None, текст ошибки по-русски)."""
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": "web_MVS-enroll/" + read_version()})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({} if url.lower().startswith("http://") else None))
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, json.loads(r.read(65536).decode("utf-8")), ""
    except urllib.error.HTTPError as e:
        try:
            data = json.loads(e.read(65536).decode("utf-8"))
        except Exception:
            data = None
        return e.code, data if isinstance(data, dict) else None, "Сервер ответил ошибкой HTTP %d" % e.code
    except urllib.error.URLError as e:
        return None, None, mask_secrets("Нет связи: %s" % (e.reason,))
    except Exception as e:
        return None, None, mask_secrets("Ошибка: %s" % e)


class Enroller:
    """Один шаг заявки за раз (step). Ничего не пишет сам: когда токен получен, отдаёт патч настроек для sysmon.save_config."""

    def __init__(self, key_fn: Callable[[], str] = read_key, machine_fn: Callable[[], str] = machine_id, post: Callable = post_json,
                 state_path=STATE_FILE, clock: Callable[[], float] = time.time, host_fn: Callable[[], str] = socket.gethostname):
        self.key_fn, self.machine_fn, self.post, self.state_path, self.clock, self.host_fn = key_fn, machine_fn, post, state_path, clock, host_fn
        self.status = {"state": "idle", "error": "", "url": "", "t": None}      # idle | no_key | searching | pending | rejected | done
        self._good = ""                    # адрес, ответивший в прошлый раз
        self._next = 0.0
        self._fails = 0

    # ---- секрет заявки на диске: перезапуск приложения не должен делать заявку «новой»
    def _secret(self, reset: bool = False) -> str:
        sec = ""
        try:
            sec = str(json.loads(self.state_path.read_text(encoding="utf-8")).get("secret") or "")
        except (OSError, ValueError, AttributeError):
            pass
        if reset or len(sec) < 16:
            sec = secrets.token_urlsafe(24)
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(json.dumps({"secret": sec}), encoding="utf-8")
            except OSError:
                pass
        return sec

    def _forget(self) -> None:
        try:
            self.state_path.unlink()
        except OSError:
            pass

    def hurry(self) -> None:
        """Кнопка «Отправить заявку»: снять паузы (обычную, растущую при обрыве связи и 6 часов после отказа) — следующий шаг пойдёт сразу."""
        self._next = 0.0
        self._fails = 0

    def _urls(self, cfg: dict) -> list[str]:
        out: list[str] = []
        for u in (self._good, str(cfg.get("url") or "").strip().rstrip("/")) + DEFAULT_SERVERS:
            if u and u not in out:
                out.append(u)
        return out

    def step(self, cfg: dict, now: Optional[float] = None) -> Optional[dict]:
        """Если пора — одна заявка. Возвращает патч настроек, когда токен получен, иначе None."""
        now = self.clock() if now is None else now
        key = self.key_fn()
        if not key:
            self.status.update(state="no_key", error="")
            return None
        if now < self._next:
            return None
        machine = self.machine_fn()
        body = {"key": key, "machine": machine, "name": (cfg.get("plant") or self.host_fn() or "").strip()[:40],
                "version": read_version(), "secret": self._secret()}
        last_err = ""
        for url in self._urls(cfg):
            code, res, err = self.post(url + "/enroll", body)
            if code == 200 and isinstance(res, dict) and res.get("ok"):
                return self._answer(url, res, cfg, now)
            if code == 403:
                last_err = "приёмник %s не принял ключ регистрации (сборка и приёмник от разных ключей)" % url
            elif code is not None:
                last_err = (res or {}).get("error") or err
            else:
                last_err = last_err or err
        self._fails += 1
        self._next = now + min(BACKOFF_MAX_S, PERIOD_S * self._fails)
        self.status.update(state="searching", error=last_err or "приёмник не отвечает", t=now)
        return None

    def _answer(self, url: str, res: dict, cfg: dict, now: float) -> Optional[dict]:
        self._good, self._fails = url, 0
        state = res.get("status")
        self.status.update(state="pending", error="", url=url, t=now)
        self._next = now + PERIOD_S
        if state == "approved" and res.get("token"):
            self._forget()
            self.status.update(state="done")
            log_event("sysmon", "Токен получен автоматически с %s: регистрация завершена" % url, "success")
            return {"token": str(res["token"]), "url": url, "allow_http": True, "enabled": True,
                    "plant": cfg.get("plant") or str(res.get("name") or "")[:40]}
        if state == "approved":                    # токен уже выдан, а у нас его нет (сбой при сохранении): новый секрет = новая заявка
            self._secret(reset=True)
            self._next = now + 5
        elif state == "rejected":
            self.status.update(state="rejected")
            self._next = now + REJECTED_WAIT_S
        return None
