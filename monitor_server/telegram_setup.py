"""Мастер подключения Telegram: токен бота и чат вводятся здесь, а не в чат и не в репозиторий.

Запуск: telegram_setup.bat. Шаги: ввести токен бота (ввод скрыт; Enter — оставить прежний) → открыть бота в Telegram и нажать Start →
мастер сам находит чат, сохраняет блок "telegram" в data\\server.json (вне git) и шлёт «Telegram подключён».
Потом перезапусти сервер: значок в трее → «Перезапустить сервер».
Сменить токен (например, после /revoke в @BotFather): запустить мастер ещё раз.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from monitor_telegram import API_BASE, TEST_TEXT, TelegramClient  # noqa: E402


def private_chats(updates: dict) -> dict:
    """Из ответа getUpdates: личные чаты {chat_id: подпись}. Группы и каналы не берём — тревоги заводов не для них."""
    out: dict = {}
    for u in (updates or {}).get("result", []):
        m = u.get("message") or {}
        c = m.get("chat") or {}
        if c.get("type") == "private" and c.get("id") is not None:
            name = " ".join(x for x in (c.get("first_name"), c.get("last_name")) if x) or "без имени"
            out.setdefault(c["id"], name + (" (@%s)" % c["username"] if c.get("username") else ""))
    return out


def save_telegram(server_json: Path, token: str, chat_ids: list) -> dict:
    """Записать блок telegram в server.json, не трогая остальное (пароль панели, адрес)."""
    try:
        cfg = json.loads(server_json.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    old = cfg.get("telegram") if isinstance(cfg.get("telegram"), dict) else {}
    cfg["telegram"] = {**old, "token": token, "chat_ids": list(chat_ids)}
    server_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = server_json.with_name(server_json.name + ".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, server_json)
    return cfg


def main(data: Path = HERE / "data", api_base: str = API_BASE) -> int:
    server_json = data / "server.json"
    try:
        old = json.loads(server_json.read_text(encoding="utf-8")).get("telegram", {})
    except (OSError, ValueError, AttributeError):
        old = {}
    token = getpass.getpass("Токен бота (ввод скрыт; Enter — оставить прежний): ").strip() or str(old.get("token") or "")
    if not token:
        print("Токена нет. Создай бота в @BotFather (/newbot) и запусти мастер снова.")
        return 1
    client = TelegramClient(token, api_base)
    ok, body, _, err = client.call("getMe")
    if not ok:
        print("Бот не отвечает: %s" % err)
        return 1
    username = (body.get("result") or {}).get("username", "?")
    print("Бот найден: @%s" % username)
    input("Открой @%s в Telegram, нажми Start (или напиши боту любое сообщение) и нажми Enter здесь… " % username)
    ok, body, _, err = client.call("getUpdates", {"timeout": 0, "allowed_updates": ["message"]})
    chats = private_chats(body) if ok else {}
    if not chats:
        print("Личных сообщений боту не нашёл%s. Напиши боту и запусти мастер снова." % (": " + err if err else ""))
        return 1
    ids = list(chats)
    if len(ids) > 1:
        for i, cid in enumerate(ids, 1):
            print("  %d) %s" % (i, chats[cid]))
        pick = input("Кому слать? Номер (Enter — первому): ").strip()
        ids = [ids[int(pick) - 1]] if pick.isdigit() and 1 <= int(pick) <= len(ids) else ids[:1]
    save_telegram(server_json, token, ids)
    print("Сохранено в %s: чат %s" % (server_json, chats[ids[0]]))
    ok, _, _, err = client.call("sendMessage", {"chat_id": ids[0], "text": TEST_TEXT})
    print("Тестовое сообщение отправлено." if ok else "Тест не ушёл: %s" % err)
    print("Теперь перезапусти сервер: значок в трее → «Перезапустить сервер».")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
