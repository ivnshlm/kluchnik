#!/usr/bin/env python3
"""Клиентский бот VPN в мессенджере MAX. Человек приходит с кодом приглашения,
получает конфиг AmneziaWG файлом и QR-кодом, смотрит своё состояние и сам
перевыпускает ключ. Админу - /claim, /who, /broadcast.

Правка пиров общая с телеграмным ботом и идёт через awgcore (под flock).
Транспорт - long polling на botapi.max.ru: вебхук потребовал бы отдельное имя
в Caddy, а platform-api2 просит доверять корневому сертификату Минцифры."""
import json
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

import awgcore
import clientsdb as cdb
from awgcore import PUBHOST

CFG_PATH = "/etc/vpnbot/config.json"
API = "https://botapi.max.ru"
REISSUE_COOLDOWN = 24 * 3600
APP_LINKS = ("Android: https://play.google.com/store/apps/details?id=org.amnezia.vpn\n"
             "iPhone: https://apps.apple.com/app/amneziavpn/id1600480083\n"
             "Windows и Mac: https://amnezia.org/ru/downloads")

HOWTO = {
    "and": ("Android\n\n"
            "1. Поставь AmneziaVPN из Google Play.\n"
            "2. Вернись сюда, нажми «Мой конфиг» и сохрани файл.\n"
            "3. В приложении: + внизу, «У меня есть данные для подключения», "
            "«Файл с ключом», выбери скачанный файл.\n"
            "4. Большая кнопка подключения - и всё.\n\n"
            "Проще быстрее: в приложении выбрать QR-код и навести на картинку, "
            "которую я присылаю вместе с файлом."),
    "ios": ("iPhone\n\n"
            "1. Поставь AmneziaVPN из App Store.\n"
            "2. В приложении: + , «У меня есть данные для подключения», «QR-код».\n"
            "3. Наведи камеру на QR-картинку, которую я прислал. Файл на айфоне "
            "импортировать сложнее, поэтому QR - основной путь.\n"
            "4. Разреши добавить конфигурацию VPN, когда система спросит."),
    "win": ("Windows\n\n"
            "1. Скачай AmneziaVPN с https://amnezia.org/ru/downloads и установи.\n"
            "2. Сохрани из этого чата файл .conf.\n"
            "3. В приложении: «У меня есть данные для подключения» - «Файл с ключом» - "
            "укажи скачанный файл.\n"
            "4. Подключайся. Если ругается на права - запусти от администратора."),
    "mac": ("macOS\n\n"
            "1. Скачай AmneziaVPN с https://amnezia.org/ru/downloads.\n"
            "2. Сохрани из этого чата файл .conf.\n"
            "3. В приложении: «У меня есть данные для подключения» - «Файл с ключом».\n"
            "4. Разреши добавить конфигурацию VPN в системном диалоге."),
}


def cfg_load():
    with open(CFG_PATH) as f:
        return json.load(f)


def cfg_save(cfg):
    tmp = CFG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, CFG_PATH)


# --- MAX API ---

def mapi(method, path, query=None, body=None, timeout=70):
    url = API + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": cfg_load()["max_token"],
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        print(f"max {method} {path}: {e.code} {detail}", flush=True)
    except Exception as e:
        print(f"max {method} {path}: {e}", flush=True)
    return None


def kb(rows):
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def btn(text, payload):
    return {"type": "callback", "text": text, "payload": payload}


def send(chat, text, buttons=None, attachments=None):
    att = list(attachments or [])
    if buttons:
        att.append(kb(buttons))
    body = {"text": text[:3900]}
    if att:
        body["attachments"] = att
    # вложение после загрузки доходит до получателя не мгновенно: пока оно
    # не готово, /messages отвечает ошибкой, так что пробуем ещё пару раз
    for attempt in range(4):
        r = mapi("POST", "/messages", {"chat_id": chat}, body)
        if r is not None:
            return r
        if not att:
            break
        time.sleep(2)
    return None


# короткая всплывашка в ответ на нажатие: пустое тело MAX не принимает
# (`message` or `notification` required), а выдача конфига идёт не мгновенно,
# так что человеку полезно видеть, что нажатие дошло
ACK = {
    "cfg": "Открываю", "how": "Открываю", "menu": "Готово", "st": "Смотрю",
    "srv": "Проверяю сервер", "dev": "Готовлю новое устройство",
    "re": "Секунду", "re!": "Перевыпускаю ключ", "msg": "Жду сообщение",
    "aw": "Собираю список",
}


def answer(callback_id, payload=""):
    text = ACK.get(payload.split(":", 1)[0], "Готово")
    mapi("POST", "/answers", {"callback_id": callback_id}, {"notification": text})


def upload(kind, path):
    """Загрузка файла или картинки: сначала берём адрес, потом кладём туда
    multipart, в ответе - токен для вложения."""
    slot = mapi("POST", "/uploads", {"type": kind})
    if not slot or "url" not in slot:
        return None
    with open(path, "rb") as f:
        blob = f.read()
    boundary = "----maxbot" + secrets.token_hex(8)
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"data\"; "
            f"filename=\"{os.path.basename(path)}\"\r\n"
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body = head + blob + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(slot["url"], data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            res = json.load(r)
    except Exception as e:
        print(f"upload {kind} failed: {e}", flush=True)
        return None
    if "token" in res:
        return res["token"]
    photos = res.get("photos") or {}
    for v in photos.values():
        if v.get("token"):
            return v["token"]
    print(f"upload {kind}: непонятный ответ {str(res)[:200]}", flush=True)
    return None


def notify_tg(text):
    """Уведомления админу уходят в телеграм: там его основная консоль."""
    cfg = cfg_load()
    token, admin = cfg.get("token"), cfg.get("admin_id")
    if not token or not admin:
        return
    data = json.dumps({"chat_id": admin, "text": text[:3800],
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage",
                                 data=data, headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"notify_tg: {e}", flush=True)


# --- меню ---

def main_menu(uid):
    rows = [
        [btn("Мой конфиг", "cfg")],
        [btn("Как подключиться", "how")],
        [btn("Моё состояние", "st"), btn("Сервер работает?", "srv")],
        [btn("Ещё устройство", "dev")],
        [btn("Написать админу", "msg")],
    ]
    if is_admin(uid):
        rows.append([btn("Кто подключён", "aw")])
    return rows


def is_admin(uid):
    return uid is not None and uid == cfg_load().get("max_admin_id")


def greet(uid, label):
    return (f"Привет, {label}. Это бот доступа к VPN.\n\n"
            "Тут можно забрать конфиг, посмотреть, подключён ли ты, "
            "проверить сервер и перевыпустить ключ, если потерял файл.")


# --- выдача конфига ---

def deliver(chat, path, peer, ip, lead):
    """Файл .conf плюс QR-картинка тем же сообщением нельзя: файл в MAX не
    совмещается с другими вложениями, поэтому двумя."""
    ftoken = upload("file", path)
    if not ftoken:
        send(chat, "Не вышло отправить файл, попробуй ещё раз через минуту или напиши админу.")
        return False
    send(chat, f"{lead}\n\nУстройство: {peer}\nАдрес в сети: {ip}\nСервер: {PUBHOST}",
         attachments=[{"type": "file", "payload": {"token": ftoken}}])

    qr = os.path.join(awgcore.TMPDIR, f"{peer}-qr.png")
    text = open(path).read()
    rc, _, err = awgcore.run(["qrencode", "-o", qr, "-s", "6", "-m", "2"], inp=text)
    if rc == 0 and os.path.exists(qr):
        itoken = upload("image", qr)
        if itoken:
            send(chat, "Для телефона: в AmneziaVPN выбери QR-код и наведи камеру на эту картинку.",
                 buttons=[[btn("Как подключиться", "how")], [btn("В начало", "menu")]],
                 attachments=[{"type": "image", "payload": {"token": itoken}}])
        os.unlink(qr)
    else:
        print(f"qrencode: {err}", flush=True)
        send(chat, "QR-кода не будет, подключайся файлом.",
             buttons=[[btn("Как подключиться", "how")], [btn("В начало", "menu")]])
    return True


def issue(chat, uid, peer, lead, existing):
    res, err = awgcore.reissue(peer) if existing else awgcore.make_client(peer)
    if err:
        send(chat, f"Не получилось: {err}\nНапиши админу, он разберётся.")
        notify_tg(f"MAX-бот: не смог выдать конфиг {peer} (uid {uid}): {err}")
        return False
    path, ip = res
    ok = deliver(chat, path, peer, ip, lead)
    try:
        os.unlink(path)
    except OSError:
        pass
    return ok


# --- обработка кода приглашения ---

def redeem(chat, uid, code, sender_name):
    inv = cdb.invite_find(code)
    if not inv:
        send(chat, "Такого кода нет. Проверь, что переписал его целиком, "
                   "или попроси новый у того, кто дал тебе доступ.")
        return
    _, peer, _, expires, used_by = inv
    if used_by:
        send(chat, "Этот код уже использован. Нужен новый - напиши тому, кто давал доступ.")
        return
    if expires < time.time():
        send(chat, "Код просрочен. Попроси новый.")
        return

    cdb.invite_use(code, uid)
    cdb.person_add(uid, peer, chat)
    cdb.device_add(peer, uid)
    cdb.logit(uid, "redeem", f"{peer} ({sender_name})")
    existing = peer in awgcore.peer_names().values()
    lead = ("Готово, доступ есть. Вот твой конфиг." if not existing else
            "Готово, доступ есть. Вот свежий конфиг - старый файл, если он у тебя "
            "был, больше не работает.")
    send(chat, greet(uid, peer), buttons=main_menu(uid))
    issue(chat, uid, peer, lead, existing)
    notify_tg(f"MAX-бот: {sender_name} (uid {uid}) забрал доступ по коду, клиент {peer}")


# --- кнопки ---

def show_config(chat, uid):
    devs = cdb.devices_of(uid)
    if not devs:
        send(chat, "За тобой не записано ни одного устройства. Напиши админу.",
             buttons=[[btn("Написать админу", "msg")]])
        return
    rows = [[btn(f"Перевыпустить: {d}", "re:" + d)] for d in devs]
    rows.append([btn("В начало", "menu")])
    send(chat, "Конфиг с ключом выдаётся один раз - на сервере он не хранится, "
               "так что переслать старый я не могу.\n\n"
               "Если файл потерялся или не работает, перевыпусти ключ: придёт новый "
               "файл, а старый перестанет подключаться. Адрес в сети останется тот же.",
         buttons=rows)


def do_reissue(chat, uid, peer, confirmed):
    if cdb.device_owner(peer) != uid:
        return
    if not confirmed:
        send(chat, f"Перевыпустить ключ для «{peer}»? Старый файл сразу перестанет работать.",
             buttons=[[btn("Да, перевыпустить", "re!:" + peer)], [btn("Не надо", "menu")]])
        return
    p = cdb.person(uid)
    left = REISSUE_COOLDOWN - (int(time.time()) - (p[5] or 0))
    if not is_admin(uid) and left > 0:
        send(chat, f"Перевыпускать можно раз в сутки. Попробуй через {left // 3600 + 1} ч "
                   "или напиши админу, если срочно.",
             buttons=[[btn("Написать админу", "msg")], [btn("В начало", "menu")]])
        return
    cdb.reissue_mark(uid)
    cdb.logit(uid, "reissue", peer)
    issue(chat, uid, peer, "Новый конфиг готов.", True)
    notify_tg(f"MAX-бот: {peer} (uid {uid}) перевыпустил себе ключ")


def add_device(chat, uid):
    p = cdb.person(uid)
    devs = cdb.devices_of(uid)
    limit = p[3] or cdb.DEV_LIMIT
    if len(devs) >= limit and not is_admin(uid):
        send(chat, f"У тебя уже {len(devs)} устройства, больше без админа нельзя.",
             buttons=[[btn("Написать админу", "msg")], [btn("В начало", "menu")]])
        return
    base, taken = p[1], set(awgcore.peer_names().values())
    peer = None
    for i in range(2, limit + 6):
        cand = f"{base}-{i}"
        if cand not in taken:
            peer = cand
            break
    if not peer:
        send(chat, "Не смог придумать имя новому устройству, напиши админу.")
        return
    cdb.device_add(peer, uid)
    cdb.logit(uid, "device", peer)
    issue(chat, uid, peer, "Конфиг для нового устройства.", False)
    notify_tg(f"MAX-бот: {base} (uid {uid}) добавил устройство {peer}")


def my_status(chat, uid):
    devs = cdb.devices_of(uid)
    rows = {r["name"]: r for r in awgcore.peer_rows()}
    out = ["Твои устройства:"]
    for d in devs:
        r = rows.get(d)
        if not r:
            out.append(f"\n{d}: на сервере не найдено, напиши админу")
            continue
        vol = (r["rx"] + r["tx"]) / 2 ** 30
        out.append(f"\n{d} ({r['ip']})\n  последний раз: {awgcore.human_age(r['age'])}"
                   f"\n  трафик: {vol:.1f} ГБ")
    out.append("\nТрафик считается с последнего перезапуска сервера, не за всю жизнь.")
    send(chat, "\n".join(out), buttons=[[btn("Сервер работает?", "srv")], [btn("В начало", "menu")]])


# Узлы check-host.net в Москве. С самого сервера не узнать, доходит ли до него
# запрос из России, - нужен взгляд снаружи. Пинг проверяет только маршрут до
# адреса, но не порт VPN, поэтому рядом в ответе идёт число живых подключений:
# оно и доказывает, что порт не режут.
RU_NODES = ("ru1.node.check-host.net", "ru2.node.check-host.net")
_ru_cache = (0.0, None)


def chget(path, query=None):
    url = "https://check-host.net" + path
    if query:
        url += "?" + urllib.parse.urlencode(query, doseq=True)
    # без User-Agent check-host.net отвечает 403: заголовок urllib по умолчанию
    # ему не нравится
    req = urllib.request.Request(url, headers={
        "Accept": "application/json", "User-Agent": "vpnbot/1.0"})
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.load(r)


def ru_probe():
    """Отзывается ли сервер на пинг из Москвы: (узлов ответило, узлов спросили,
    средний пинг в мс) или None, если недоступен сам сервис проверки. Ответ
    держим минуту: кнопку могут нажать разом несколько человек, а узлы просят
    не частить."""
    global _ru_cache
    ts, val = _ru_cache
    if time.time() - ts < 60:
        return val
    val = None
    try:
        q = [("host", PUBHOST)] + [("node", n) for n in RU_NODES]
        rid = chget("/check-ping", q)["request_id"]
        res = {}
        for _ in range(5):
            time.sleep(1.5)
            res = chget("/check-result/" + rid)
            if all(res.get(n) for n in RU_NODES):
                break
        live, pings = 0, []
        for node in RU_NODES:
            got = res.get(node)
            tries = got[0] if isinstance(got, list) and got and isinstance(got[0], list) else []
            ok = [t for t in tries if isinstance(t, list) and t[:1] == ["OK"]]
            if ok:
                live += 1
                pings += [t[1] for t in ok if len(t) > 1 and isinstance(t[1], (int, float))]
        val = (live, len(RU_NODES), int(1000 * sum(pings) / len(pings)) if pings else 0)
    except Exception as e:
        print(f"проверка доступности из РФ не удалась: {e}", flush=True)
    _ru_cache = (time.time(), val)
    return val


def server_health(chat):
    _, state, _ = awgcore.run(["systemctl", "is-active", "awg-quick@awg0"])
    up = float(open("/proc/uptime").read().split()[0])
    days, rem = divmod(int(up), 86400)
    out = []
    if state == "active":
        out.append("VPN на сервере работает.")
    else:
        out.append(f"VPN на сервере лежит (состояние: {state}). Админу уже видно, подождите.")
    net = "есть"
    try:
        socket.create_connection(("1.1.1.1", 443), timeout=4).close()
    except OSError:
        net = "НЕТ, это проблема на сервере"
    online = sum(1 for r in awgcore.peer_rows() if r["age"] is not None and r["age"] < 180)
    out.append(f"Интернет с сервера: {net}.")
    probe = ru_probe()
    if probe is None:
        out.append("Из России: проверить сейчас не получилось, сервис проверки молчит.")
    elif probe[0] == 0:
        out.append("Из России: сервер не отзывается с московских узлов - похоже, адрес "
                   "режут у провайдеров. Админу это тоже видно.")
    elif probe[0] < probe[1]:
        out.append(f"Из России: отзывается с {probe[0]} из {probe[1]} московских узлов, "
                   f"{probe[2]} мс. У части провайдеров может не пускать.")
    else:
        out.append(f"Из России: сервер отзывается с московских узлов, {probe[2]} мс.")
    if online:
        out.append(f"Прямо сейчас через сервер ходят устройств: {online} - "
                   "значит порт VPN не заблокирован.")
    else:
        out.append("Прямо сейчас подключённых устройств нет - само по себе это не поломка.")
    out.append(f"Сервер не перезагружался {days} д {rem // 3600} ч.")
    if state == "active" and net == "есть" and (probe is None or probe[0]):
        out.append("\nЕсли у тебя не подключается, а тут всё хорошо - попробуй выключить "
                   "и включить мобильный интернет или Wi-Fi, потом переподключиться в "
                   "приложении. Не помогло - перевыпусти ключ или напиши админу.")
    send(chat, "\n".join(out),
         buttons=[[btn("Мой конфиг", "cfg")], [btn("Написать админу", "msg")], [btn("В начало", "menu")]])


def who(chat):
    rows = {r["name"]: r for r in awgcore.peer_rows()}
    bound = cdb.bound_peers()
    out = [f"Привязано людей: {len(cdb.people_all())}"]
    for uid, label, joined, limit, blocked, last, pchat in cdb.people_all():
        devs = cdb.devices_of(uid)
        parts = []
        for d in devs:
            r = rows.get(d)
            parts.append(f"{d} ({awgcore.human_age(r['age']) if r else 'нет пира'})")
        out.append(f"\n{label} - uid {uid}\n  " + ", ".join(parts))
    free = [n for n in awgcore.peer_names().values() if n not in bound]
    if free:
        out.append("\nПиры без привязки к MAX: " + ", ".join(sorted(free)))
    codes = cdb.invite_open()
    if codes:
        out.append("\nОткрытые коды: " + ", ".join(f"{cdb.fmt_code(c)}={p}" for c, p, _ in codes))
    send(chat, "\n".join(out))


def broadcast(chat, text):
    sent, failed = 0, []
    for row in cdb.people_all():
        label, pchat = row[1], row[6]
        if not pchat:
            failed.append(label + " (не писал боту)")
            continue
        r = send(pchat, text, buttons=[[btn("Мой конфиг", "cfg")], [btn("В начало", "menu")]])
        if r is None:
            failed.append(label)
        else:
            sent += 1
        time.sleep(0.6)  # /messages ограничен по частоте
    msg = f"Отправлено: {sent}."
    if failed:
        msg += " Не дошло: " + ", ".join(failed)
    send(chat, msg)


# --- разбор входящего ---

def on_text(chat, uid, text, sender_name):
    p = cdb.person(uid)
    if p:
        cdb.chat_set(uid, chat)
    cmd = text.split()[0].lower() if text.split() else ""
    cfg = cfg_load()

    if cfg.get("max_admin_id") is None and cmd == "/claim":
        code = text.partition(" ")[2].strip()
        if code and secrets.compare_digest(code, cfg.get("max_claim_code", "")):
            cfg["max_admin_id"] = uid
            cfg.pop("max_claim_code", None)
            cfg_save(cfg)
            send(chat, f"Бот привязан к вам как админ (uid {uid}).\n\n"
                       "/who - кто подключён\n/broadcast текст - написать всем\n"
                       "Коды приглашений выдаются в телеграмном боте: /invite имя",
                 buttons=main_menu(uid))
        else:
            send(chat, "код не подошёл")
        return

    if is_admin(uid):
        if cmd == "/who":
            who(chat)
            return
        if cmd == "/broadcast":
            body = text.partition(" ")[2].strip()
            if not body:
                send(chat, "как пользоваться: /broadcast текст сообщения")
            else:
                broadcast(chat, body)
            return
        if cmd in ("/start", "/help") and p is None:
            send(chat, "Админ-режим.\n/who - кто подключён\n/broadcast текст - написать всем\n\n"
                       "Коды приглашений выдаёт телеграмный бот: /invite имя",
                 buttons=main_menu(uid))
            return

    if p is None:
        code = cdb.norm_code(text)
        if 6 <= len(code) <= 12 and " " not in text.strip():
            redeem(chat, uid, code, sender_name)
        elif cmd in ("/start", "/help"):
            send(chat, "Это бот доступа к VPN. Если тебе дали код приглашения - "
                       "пришли его сюда одним сообщением. Если кода нет, попроси "
                       "его у того, кто дал тебе этот бот.")
        else:
            print(f"чужой uid {uid} ({sender_name}): {text[:40]}", flush=True)
        return

    if cdb.pending_get(uid) == "msg":
        cdb.pending_clear(uid)
        notify_tg(f"VPN, сообщение от {p[1]} (MAX uid {uid}):\n\n{text}")
        send(chat, "Передал админу.", buttons=[[btn("В начало", "menu")]])
        return

    send(chat, greet(uid, p[1]), buttons=main_menu(uid))


def on_start(chat, uid, payload, sender_name):
    """Человек открыл бота кнопкой «начать» или по ссылке
    https://max.ru/<бот>?start=КОД - тогда код приходит в payload и вводить
    его руками не нужно."""
    p = cdb.person(uid)
    if p:
        cdb.chat_set(uid, chat)
        send(chat, greet(uid, p[1]), buttons=main_menu(uid))
        return
    code = cdb.norm_code(payload)
    if 6 <= len(code) <= 12:
        redeem(chat, uid, code, sender_name)
        return
    if is_admin(uid):
        send(chat, "Админ-режим.\n/who - кто подключён\n/broadcast текст - написать всем",
             buttons=main_menu(uid))
        return
    send(chat, "Это бот доступа к VPN. Если тебе дали код приглашения - "
               "пришли его сюда одним сообщением.")


def on_callback(chat, uid, payload):
    p = cdb.person(uid)
    if payload == "menu" and p:
        send(chat, greet(uid, p[1]), buttons=main_menu(uid))
        return
    if payload == "how":
        send(chat, "Какое устройство?\n\n" + APP_LINKS,
             buttons=[[btn("Android", "how:and"), btn("iPhone", "how:ios")],
                      [btn("Windows", "how:win"), btn("Mac", "how:mac")],
                      [btn("В начало", "menu")]])
        return
    if payload.startswith("how:"):
        send(chat, HOWTO.get(payload[4:], "нет такой инструкции"),
             buttons=[[btn("Мой конфиг", "cfg")], [btn("В начало", "menu")]])
        return
    if payload == "srv":
        server_health(chat)
        return
    if not p:
        return
    if payload == "cfg":
        show_config(chat, uid)
    elif payload.startswith("re!:"):
        do_reissue(chat, uid, payload[4:], True)
    elif payload.startswith("re:"):
        do_reissue(chat, uid, payload[3:], False)
    elif payload == "dev":
        add_device(chat, uid)
    elif payload == "st":
        my_status(chat, uid)
    elif payload == "msg":
        cdb.pending_set(uid, "msg")
        send(chat, "Напиши следующим сообщением, что передать админу.")
    elif payload == "aw" and is_admin(uid):
        who(chat)


def handle_update(upd):
    kind = upd.get("update_type")
    if kind == "message_created":
        m = upd.get("message") or {}
        rec, snd = m.get("recipient") or {}, m.get("sender") or {}
        if rec.get("chat_type") != "dialog":
            return  # групповые чаты бот не обслуживает
        chat, uid = rec.get("chat_id"), snd.get("user_id")
        text = ((m.get("body") or {}).get("text") or "").strip()
        if not chat or not uid or not text:
            return
        on_text(chat, uid, text, snd.get("name") or str(uid))
    elif kind == "bot_started":
        u = upd.get("user") or {}
        chat = upd.get("chat_id") or ((upd.get("message") or {}).get("recipient") or {}).get("chat_id")
        uid = u.get("user_id")
        if chat and uid:
            on_start(chat, uid, (upd.get("payload") or "").strip(), u.get("name") or str(uid))
    elif kind == "message_callback":
        c = upd.get("callback") or {}
        m = upd.get("message") or {}
        chat = (m.get("recipient") or {}).get("chat_id")
        uid = (c.get("user") or {}).get("user_id")
        if c.get("callback_id"):
            answer(c["callback_id"], c.get("payload") or "")
        if chat and uid and c.get("payload"):
            on_callback(chat, uid, c["payload"])


def main():
    cfg = cfg_load()
    if not cfg.get("max_token"):
        raise SystemExit("в /etc/vpnbot/config.json нет max_token")
    cdb.db()
    marker = None
    print("клиентский бот MAX запущен", flush=True)
    while True:
        try:
            q = {"limit": 50, "timeout": 30}
            if marker:
                q["marker"] = marker
            r = mapi("GET", "/updates", q)
            if r is None:
                time.sleep(5)
                continue
            for upd in r.get("updates") or []:
                try:
                    handle_update(upd)
                except Exception as e:
                    print(f"обработка упала: {e}", flush=True)
            marker = r.get("marker") or marker
        except Exception as e:
            print(f"цикл: {e}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
