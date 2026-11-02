#!/usr/bin/env python3
"""Общая работа с пирами AmneziaWG. Конфиг awg0.conf теперь читают и пишут два
процесса - админский бот в телеграме и клиентский в MAX, - поэтому любое
изменение идёт под flock: иначе один затрёт пира, добавленного другим."""
import contextlib
import fcntl
import os
import re
import shutil
import subprocess
import time

SRV_CONF = "/etc/amnezia/amneziawg/awg0.conf"
IFACE = "awg0"
POOL = "10.8.2."
PUBHOST = "203.0.113.10"  # <- адрес своего сервера, попадёт в Endpoint клиентов
TMPDIR = "/run/vpnbot"
# файл блокировки лежит вне TMPDIR: каталог создаётся через RuntimeDirectory и
# пережил бы не каждый перезапуск служб, а блокировка нужна обоим процессам
LOCK_PATH = "/run/vpnbot-awg.lock"
NAME_RE = r"[A-Za-z0-9_-]{1,24}"


def run(args, inp=None):
    p = subprocess.run(args, capture_output=True, text=True, input=inp, timeout=60)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


@contextlib.contextmanager
def lock():
    """Исключительная блокировка на время правки awg0.conf. Не реентерабельна:
    составные операции (reissue) берут её один раз на всё."""
    os.makedirs(TMPDIR, mode=0o700, exist_ok=True)
    f = open(LOCK_PATH, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def srv_params():
    p = {}
    for line in open(SRV_CONF):
        s = line.strip()
        if s.startswith("["):
            if s == "[Peer]":
                break
            continue
        if "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            p[k.strip()] = v.strip()
    return p


def peer_names():
    """pubkey -> имя из комментария '# имя' перед блоком [Peer]."""
    names, pending = {}, None
    for line in open(SRV_CONF):
        s = line.strip()
        if s.startswith("#") and not s.startswith("#!"):
            pending = s.lstrip("# ").strip()
        elif s.startswith("PublicKey"):
            key = s.split("=", 1)[1].strip()
            if pending:
                names[key] = pending
                pending = None
        elif s.startswith("[Interface]"):
            pending = None
    return names


def peer_rows():
    _, dump, _ = run(["awg", "show", IFACE, "dump"])
    names = peer_names()
    rows = []
    now = time.time()
    for line in dump.splitlines()[1:]:
        f = line.split("\t")
        if len(f) < 8:
            continue
        hs = int(f[4])
        rows.append({
            "pub": f[0], "ip": f[3].split("/")[0], "endpoint": f[2],
            "age": None if hs == 0 else int(now - hs),
            "rx": int(f[5]), "tx": int(f[6]),
            "name": names.get(f[0], "без имени"),
        })
    return rows


def peer_ip(name):
    """Адрес пира по имени из файла конфигурации (пир может ни разу не подключаться)."""
    cur = None
    for line in open(SRV_CONF):
        s = line.strip()
        if s.startswith("#") and not s.startswith("#!"):
            cur = s.lstrip("# ").strip()
        elif s.startswith("AllowedIPs") and cur == name:
            m = re.search(rf"{re.escape(POOL)}(\d+)", s)
            if m:
                return f"{POOL}{m.group(1)}"
    return None


def free_ip():
    used = {int(m.group(1)) for m in
            (re.search(rf"{re.escape(POOL)}(\d+)/32", l) for l in open(SRV_CONF)) if m}
    for i in range(2, 255):
        if i not in used:
            return f"{POOL}{i}"
    return None


def human_age(sec):
    if sec is None:
        return "не подключался"
    if sec < 90:
        return f"{sec} с назад"
    if sec < 5400:
        return f"{sec // 60} мин назад"
    if sec < 172800:
        return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"


def _make_client(name, ip=None):
    if not re.fullmatch(NAME_RE, name):
        return None, "имя только из латиницы, цифр, дефиса и подчёркивания, до 24 символов"
    if name in peer_names().values():
        return None, f"клиент с именем {name} уже есть"
    ip = ip or free_ip()
    if not ip:
        return None, "свободных адресов в пуле не осталось"

    _, priv, _ = run(["awg", "genkey"])
    _, pub, _ = run(["awg", "pubkey"], inp=priv + "\n")
    _, psk, _ = run(["awg", "genpsk"])

    os.makedirs(TMPDIR, mode=0o700, exist_ok=True)
    pskfile = os.path.join(TMPDIR, "psk")
    with open(pskfile, "w") as f:
        f.write(psk + "\n")
    rc, _, err = run(["awg", "set", IFACE, "peer", pub, "preshared-key", pskfile,
                      "allowed-ips", f"{ip}/32"])
    os.unlink(pskfile)
    if rc != 0:
        return None, f"awg set не отработал: {err}"

    shutil.copy2(SRV_CONF, SRV_CONF + ".bak")
    with open(SRV_CONF, "a") as f:
        f.write(f"\n# {name}\n[Peer]\nPublicKey = {pub}\nPresharedKey = {psk}\n"
                f"AllowedIPs = {ip}/32\n")

    p = srv_params()
    _, srvpub, _ = run(["awg", "show", IFACE, "public-key"])
    lines = [
        "[Interface]", f"Address = {ip}/32", "DNS = 1.1.1.1, 1.0.0.1",
        f"PrivateKey = {priv}", "MTU = 1280",
    ]
    for k in ("Jc", "Jmin", "Jmax", "S1", "S2", "S3", "S4", "H1", "H2", "H3", "H4"):
        if k in p:
            lines.append(f"{k} = {p[k]}")
    lines += [
        "", "[Peer]", f"PublicKey = {srvpub}", f"PresharedKey = {psk}",
        "AllowedIPs = 0.0.0.0/0, ::/0",
        f"Endpoint = {PUBHOST}:{p.get('ListenPort', '43881')}",
        "PersistentKeepalive = 25",
    ]
    path = os.path.join(TMPDIR, f"{name}-amneziawg.conf")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    os.chmod(path, 0o600)
    return (path, ip), None


def _revoke(name):
    names = peer_names()
    pubs = [k for k, v in names.items() if v == name]
    if not pubs:
        return f"клиента с именем {name} нет"
    pub = pubs[0]
    run(["awg", "set", IFACE, "peer", pub, "remove"])
    shutil.copy2(SRV_CONF, SRV_CONF + ".bak")
    text = open(SRV_CONF).read()
    pattern = re.compile(r"\n# " + re.escape(name) + r"\n\[Peer\].*?(?=\n#|\n\[|\Z)", re.S)
    new = pattern.sub("", text)
    with open(SRV_CONF, "w") as f:
        f.write(new)
    ok = pub not in new
    return f"клиент {name} отозван" + ("" if ok else " (из файла блок убрать не вышло, проверьте руками)")


def make_client(name, ip=None):
    with lock():
        return _make_client(name, ip)


def revoke(name):
    with lock():
        return _revoke(name)


def reissue(name):
    """Новый ключ тому же клиенту с сохранением адреса: старый конфиг перестаёт
    работать. Нужно, когда человек потерял файл или сменил устройство."""
    with lock():
        ip = peer_ip(name)
        if ip is None:
            return None, f"клиента с именем {name} нет"
        _revoke(name)
        return _make_client(name, ip)
