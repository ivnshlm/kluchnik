#!/usr/bin/env python3
"""Админский бот сервера: состояние машины + управление клиентами
AmneziaWG."""
import json
import os
import re
import secrets
import shutil
import threading
import time
import urllib.error
import urllib.request

import awgcore
import clientsdb as cdb
from awgcore import PUBHOST, human_age, make_client, peer_names, peer_rows, revoke, run

CFG_PATH = "/etc/vpnbot/config.json"

# что показывать в /status и за чем следить: свои службы вписать сюда,
# "docker:имя" - это контейнер, остальное - юнит systemd
SERVICES = {
    "awg": "awg-quick@awg0",
    "vnstat": "vnstat",
    "ssh": "ssh",
}
RESTARTABLE = ("awg", "vnstat")

HELP = """<b>Состояние</b>
/status - нагрузка, память, диск, службы
/traffic - расход по vnstat (день и месяц)
/peers - клиенты VPN: handshake и объёмы
/journal - последние ошибки из журнала

<b>Клиенты</b>
/new имя - выпустить конфиг (файлом в чат)
/revoke имя - отозвать (спросит подтверждение)
/invite имя - код приглашения в бот MAX
/invite all - коды всем, кто ещё не привязан
/invites - выданные и ещё не использованные коды
/clients - кто привязан в MAX и с каких устройств

<b>Управление</b>
/restart awg|caddy|hk|vnstat
/help - эта справка"""


def cfg_load():
    with open(CFG_PATH) as f:
        return json.load(f)


def cfg_save(cfg):
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CFG_PATH)


def svc_state(unit):
    if unit.startswith("docker:"):
        rc, out, _ = run(["docker", "inspect", "-f", "{{.State.Status}}", unit.split(":", 1)[1]])
        if rc != 0:
            return "нет контейнера"
        return "active" if out == "running" else out
    _, state, _ = run(["systemctl", "is-active", unit])
    return state


def svc_restart(unit):
    if unit.startswith("docker:"):
        rc, _, err = run(["docker", "restart", unit.split(":", 1)[1]])
    else:
        rc, _, err = run(["systemctl", "restart", unit])
    return rc, err


# --- Telegram ---

def api(method, params=None, retries=2):
    token = cfg_load()["token"]
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except Exception as e:
            if attempt == retries:
                print(f"api {method} failed: {e}", flush=True)
                return {"ok": False}
            time.sleep(2)


def send(chat, text):
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)] or [""]:
        api("sendMessage", {"chat_id": chat, "text": chunk, "parse_mode": "HTML",
                            "disable_web_page_preview": True})


def send_file(chat, path, caption=""):
    token = cfg_load()["token"]
    boundary = "----vpnbot" + secrets.token_hex(8)
    name = os.path.basename(path)
    with open(path, "rb") as f:
        blob = f.read()
    parts = []
    for key, val in (("chat_id", str(chat)), ("caption", caption)):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"{name}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + blob + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendDocument",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except Exception as e:
        print(f"sendDocument failed: {e}", flush=True)
        return {"ok": False}


# --- сбор состояния ---

def cmd_status():
    load = open("/proc/loadavg").read().split()[:3]
    up = float(open("/proc/uptime").read().split()[0])
    days, rem = divmod(int(up), 86400)
    hours = rem // 3600
    mem = {}
    for line in open("/proc/meminfo"):
        k, v = line.split(":")
        mem[k] = int(v.split()[0]) // 1024
    used = mem["MemTotal"] - mem["MemAvailable"]
    swap_used = mem["SwapTotal"] - mem["SwapFree"]
    du = shutil.disk_usage("/")
    pct = du.used * 100 // du.total

    out = [f"<b>сервер</b> ({PUBHOST})",
           f"аптайм: {days} д {hours} ч, load: {' '.join(load)}",
           f"память: {used} / {mem['MemTotal']} МБ, swap: {swap_used} / {mem['SwapTotal']} МБ",
           f"диск: {du.used // 2**30} / {du.total // 2**30} ГБ ({pct}%), свободно {du.free // 2**30} ГБ",
           "", "<b>Службы</b>"]
    for alias, unit in SERVICES.items():
        state = svc_state(unit)
        mark = "OK" if state == "active" else "ЛЕЖИТ"
        out.append(f"{alias}: {state} - {mark}")
    if os.path.exists("/var/run/reboot-required"):
        out.append("\nтребуется перезагрузка (накатано новое ядро)")
    return "\n".join(out)


def cmd_traffic():
    _, day, _ = run(["vnstat", "-d", "--limit", "3"])
    _, mon, _ = run(["vnstat", "-m", "--limit", "2"])
    return f"<pre>{day}\n\n{mon}</pre>"


def cmd_peers():
    rows = peer_rows()
    if not rows:
        return "клиентов нет"
    rows.sort(key=lambda r: (r["age"] is None, r["age"] or 0))
    out = [f"<b>Клиенты awg0</b> (всего {len(rows)})"]
    for r in rows:
        out.append(
            f"\n<b>{r['name']}</b> - {r['ip']}\n"
            f"  {human_age(r['age'])}, принято {r['rx'] / 2**20:.1f} МБ, отдано {r['tx'] / 2**20:.1f} МБ"
        )
    return "\n".join(out)


def cmd_journal():
    _, log, _ = run(["journalctl", "-p", "err", "-n", "15", "--no-pager", "-o", "short"])
    return f"<pre>{log or 'ошибок нет'}</pre>"


# --- приглашения в клиентский бот MAX ---

def cmd_invite(arg):
    name = (arg.split() or [""])[0]
    if name == "all":
        bound = cdb.bound_peers()
        names = sorted(n for n in peer_names().values() if n not in bound)
        if not names:
            return "все пиры уже привязаны к MAX"
        out = ["<b>Коды для непривязанных клиентов</b>", ""]
        for n in names:
            out.append(f"{n}: <code>{cdb.fmt_code(cdb.invite_create(n))}</code>")
        out.append("\nКаждый код - на 7 дней и на один раз. Человек присылает его боту в MAX.")
        return "\n".join(out)
    if not re.fullmatch(awgcore.NAME_RE, name):
        return "как пользоваться: /invite имя (латиница, цифры, дефис) или /invite all"
    exists = name in peer_names().values()
    code = cdb.invite_create(name)
    tail = ("клиент уже есть, по коду ему перевыпустится ключ и придёт свежий конфиг"
            if exists else "такого клиента ещё нет, по коду он создастся")
    return (f"Код для <b>{name}</b>: <code>{cdb.fmt_code(code)}</code>\n{tail}.\n"
            "Действует 7 дней, один раз. Перешлите человеку: пусть пришлёт код боту в MAX.")


def cmd_invites():
    rows = cdb.invite_open()
    if not rows:
        return "открытых кодов нет"
    now = time.time()
    out = ["<b>Открытые коды</b>"]
    for code, peer, expires in rows:
        out.append(f"{peer}: <code>{cdb.fmt_code(code)}</code>, ещё {int((expires - now) // 86400)} дн")
    return "\n".join(out)


def cmd_clients():
    rows = {r["name"]: r for r in peer_rows()}
    people = cdb.people_all()
    out = [f"<b>Привязано в MAX: {len(people)}</b>"]
    for uid, label, joined, limit, blocked, last, chat in people:
        devs = cdb.devices_of(uid)
        parts = [f"{d} ({human_age(rows[d]['age']) if d in rows else 'нет пира'})" for d in devs]
        out.append(f"\n<b>{label}</b> - uid {uid}, устройств {len(devs)}/{limit}\n  "
                   + (", ".join(parts) or "нет"))
    free = [n for n in peer_names().values() if n not in cdb.bound_peers()]
    if free:
        out.append("\nБез привязки к MAX: " + ", ".join(sorted(free)))
    return "\n".join(out)


# --- присмотр за машиной ---

def watchdog():
    last = {}
    while True:
        time.sleep(600)
        try:
            cfg = cfg_load()
            admin = cfg.get("admin_id")
            if not admin:
                continue
            alerts = []
            du = shutil.disk_usage("/")
            pct = du.used * 100 // du.total
            if pct >= 85 and last.get("disk") != pct // 5:
                last["disk"] = pct // 5
                alerts.append(f"диск занят на {pct}%, свободно {du.free // 2**30} ГБ")
            for alias, unit in SERVICES.items():
                state = svc_state(unit)
                down = state != "active"
                if down != last.get("svc_" + alias, False):
                    last["svc_" + alias] = down
                    if down:
                        alerts.append(f"служба {alias} ({unit}) не работает: {state}")
                    else:
                        alerts.append(f"служба {alias} снова поднялась")
            if alerts:
                send(admin, "<b>Внимание</b>\n" + "\n".join(alerts))
        except Exception as e:
            print(f"watchdog: {e}", flush=True)


# --- разбор команд ---

def handle(msg):
    chat = msg["chat"]["id"]
    uid = msg.get("from", {}).get("id")
    text = (msg.get("text") or "").strip()
    cfg = cfg_load()
    admin = cfg.get("admin_id")

    if admin is None:
        if text.startswith("/claim "):
            if secrets.compare_digest(text.split(None, 1)[1].strip(), cfg["claim_code"]):
                cfg["admin_id"] = uid
                cfg.pop("claim_code", None)
                cfg_save(cfg)
                send(chat, f"Готово, бот привязан к вам (id {uid}). Остальные будут игнорироваться.\n\n" + HELP)
            else:
                send(chat, "код не подошёл")
        else:
            send(chat, "бот ещё не привязан к владельцу, нужна команда /claim с кодом")
        return

    if uid != admin:
        print(f"чужой запрос от {uid}: {text[:50]}", flush=True)
        return

    cmd, _, arg = text.partition(" ")
    cmd, arg = cmd.lower(), arg.strip()

    if cmd in ("/start", "/help"):
        send(chat, HELP)
    elif cmd == "/status":
        send(chat, cmd_status())
    elif cmd == "/traffic":
        send(chat, cmd_traffic())
    elif cmd == "/peers":
        send(chat, cmd_peers())
    elif cmd == "/journal":
        send(chat, cmd_journal())
    elif cmd == "/new":
        if not arg:
            send(chat, "как пользоваться: /new имя")
            return
        res, err = make_client(arg.split()[0])
        if err:
            send(chat, err)
            return
        path, ip = res
        send_file(chat, path, f"{arg.split()[0]}: {ip}, сервер {PUBHOST}. Открывать приложением AmneziaVPN.")
        os.unlink(path)
    elif cmd == "/revoke":
        parts = arg.split()
        if not parts:
            send(chat, "как пользоваться: /revoke имя, потом /revoke имя confirm")
        elif len(parts) > 1 and parts[1] == "confirm":
            send(chat, revoke(parts[0]))
        else:
            send(chat, f"отозвать клиента <b>{parts[0]}</b>? Тогда пришлите:\n<code>/revoke {parts[0]} confirm</code>")
    elif cmd == "/invite":
        send(chat, cmd_invite(arg))
    elif cmd == "/invites":
        send(chat, cmd_invites())
    elif cmd == "/clients":
        send(chat, cmd_clients())
    elif cmd == "/restart":
        if arg not in RESTARTABLE:
            send(chat, "можно перезапускать: " + ", ".join(RESTARTABLE))
            return
        rc, err = svc_restart(SERVICES[arg])
        state = svc_state(SERVICES[arg])
        send(chat, f"{arg}: перезапуск {'удался' if rc == 0 else 'не удался: ' + err}, сейчас {state}")
    else:
        send(chat, "не знаю такой команды, /help покажет список")


def main():
    offset = 0
    threading.Thread(target=watchdog, daemon=True).start()
    print("бот запущен", flush=True)
    while True:
        try:
            r = api("getUpdates", {"offset": offset, "timeout": 30}, retries=0)
            if not r or not r.get("ok"):
                time.sleep(5)
                continue
            for upd in r["result"]:
                offset = upd["update_id"] + 1
                if "message" in upd:
                    try:
                        handle(upd["message"])
                    except Exception as e:
                        print(f"обработка упала: {e}", flush=True)
        except Exception as e:
            print(f"цикл: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
