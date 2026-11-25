#!/usr/bin/env python3
"""
AA Scope - LZ1RVD
Локална програма за антенния анализатор EU1KY (фърмуер r11) през ST-LINK на Mac.

Говори с анализатора само с командите на AA-600: ver, fq, sw, frx, off.
Те само мерят - нищо по уреда не се променя.
Анализаторът трябва да е в главното меню.

Пускане:  "AA Scope.app" (двоен клик) или python3 aa_scope.py
Страница: http://127.0.0.1:8765
"""
import glob
import json
import os
import select
import sys
import termios
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
HTTP_PORT = 8765
BAUD = termios.B38400          # COM_SPEED на анализатора (по подразбиране 38400)
LINE_TIMEOUT = 15.0            # секунди за една точка (на 1240 MHz е по-бавно)
HERE = os.path.dirname(os.path.abspath(__file__))


def find_port():
    """Портът на ST-LINK: /dev/cu.usbmodem*. Може да се зададе с AA_PORT=/dev/..."""
    env = os.environ.get("AA_PORT")
    if env:
        return env
    ports = sorted(glob.glob("/dev/cu.usbmodem*"))
    return ports[0] if ports else None


class SerialPort:
    """Минимален сериен порт без външни библиотеки (termios)."""

    def __init__(self, dev):
        self.fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        a = termios.tcgetattr(self.fd)
        a[0] = 0                                             # iflag
        a[1] = 0                                             # oflag
        a[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # cflag
        a[3] = 0                                             # lflag
        a[4] = BAUD
        a[5] = BAUD
        termios.tcsetattr(self.fd, termios.TCSANOW, a)
        termios.tcflush(self.fd, termios.TCIOFLUSH)
        self.buf = b""

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def write(self, text):
        os.write(self.fd, text.encode("ascii"))

    def drain(self, seconds=0.3):
        """Изчиства всичко, което идва в следващите `seconds` секунди."""
        end = time.time() + seconds
        while time.time() < end:
            r, _, _ = select.select([self.fd], [], [], 0.05)
            if r:
                try:
                    os.read(self.fd, 4096)
                except BlockingIOError:
                    pass
        self.buf = b""

    def readline(self, timeout):
        """Един ред (до \\r или \\n), без празните. None при изтекло време."""
        end = time.time() + timeout
        while True:
            # вадим готовите редове от буфера, празните се прескачат
            while True:
                seps = [i for i in (self.buf.find(b"\r"), self.buf.find(b"\n")) if i >= 0]
                if not seps:
                    break
                i = min(seps)
                line, self.buf = self.buf[:i].strip(), self.buf[i + 1:]
                if line:
                    return line.decode("ascii", "replace")
            left = end - time.time()
            if left <= 0:
                return None
            r, _, _ = select.select([self.fd], [], [], min(left, 0.2))
            if r:
                try:
                    self.buf += os.read(self.fd, 4096)
                except BlockingIOError:
                    pass

    def command(self, cmd, timeout=3.0):
        self.write(cmd + "\r")
        return self.readline(timeout)


# ---------------- състояние на измерването ----------------
lock = threading.Lock()         # пази `state`
port_lock = threading.Lock()    # само една заявка говори с анализатора в даден момент
state = {
    "running": False,
    "total": 0,
    "points": [],      # [[f_Hz, R, X], ...]
    "error": "",
    "port": None,
    "ver": "",
    "started": 0.0,
    "finished": 0.0,
    "fmax": None,      # BAND_FMAX на уреда в Hz (открива се с fq)
}

FMIN_HZ = 100000
FMAX_LIMIT_HZ = 1450000000


def probe_fmax(s):
    """Открива BAND_FMAX на уреда с командата fq (тя само приема честотата, не мери).
    Уредът отговаря ERROR над своя FMAX. FMAX е цяло число MHz между 150 и 1450."""
    if s.command("fq%d" % FMAX_LIMIT_HZ) == "OK":
        return FMAX_LIMIT_HZ
    lo, hi = 150, FMAX_LIMIT_HZ // 1000000      # MHz: lo е приет, hi е отказан
    if s.command("fq%d" % (lo * 1000000)) != "OK":
        return None
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if s.command("fq%d" % (mid * 1000000)) == "OK":
            lo = mid
        else:
            hi = mid
    return lo * 1000000


def check_version():
    dev = find_port()
    if not dev:
        return {"ok": False, "error": "Няма свързан ST-LINK (/dev/cu.usbmodem*)."}
    if not port_lock.acquire(timeout=5):
        return {"ok": False, "port": dev, "error": "Анализаторът е зает (тече измерване)."}
    try:
        with SerialPort(dev) as s:
            s.write("\r")          # изчиства недовършена команда (отговаря ERROR)
            s.drain(0.4)
            ver = s.command("ver", 2.0)
            fmax = probe_fmax(s) if ver else None
    except OSError as e:
        port_lock.release()
        return {"ok": False, "port": dev, "error": "Портът не може да се отвори: %s" % e}
    port_lock.release()
    if not ver:
        return {"ok": False, "port": dev,
                "error": "Анализаторът не отговаря. Включен ли е и в главното меню ли е?"}
    with lock:
        state["fmax"] = fmax
    return {"ok": True, "port": dev, "ver": ver, "fmax": fmax}


def sweep_worker(start, stop, points):
    dev = find_port()
    port_lock.acquire()
    try:
        if not dev:
            raise RuntimeError("Няма свързан ST-LINK (/dev/cu.usbmodem*).")
        steps = points - 1
        span = stop - start
        center = start + span // 2
        with SerialPort(dev) as s:
            s.write("\r")
            s.drain(0.4)
            # над BAND_FMAX уредът връща фалшиво 50+j0 Ohm - не допускаме такива точки
            with lock:
                fmax = state["fmax"]
            if fmax is None:
                fmax = probe_fmax(s)
                with lock:
                    state["fmax"] = fmax
            if fmax is not None and stop > fmax:
                raise RuntimeError("Краят %.3f MHz е над горната граница на уреда (%d MHz). "
                                   "Над нея уредът дава фалшиви 50 Ω." % (stop / 1e6, fmax // 1000000))
            r = s.command("fq%d" % center)
            if r != "OK":
                raise RuntimeError("Анализаторът отказа честотата %.3f MHz (%s). "
                                   "Извън обхвата на уреда ли е? Главното меню ли е отворено?"
                                   % (center / 1e6, r or "няма отговор"))
            r = s.command("sw%d" % span)
            if r != "OK":
                raise RuntimeError("Анализаторът отказа обхвата (%s)." % (r or "няма отговор"))
            s.write("frx%d\r" % steps)
            while True:
                line = s.readline(LINE_TIMEOUT)
                if line is None:
                    raise RuntimeError("Анализаторът спря да отговаря по време на измерването.")
                if line == "OK":
                    break
                if line == "ERROR":
                    raise RuntimeError("Анализаторът върна ERROR.")
                parts = line.split(",")
                if len(parts) != 3:
                    continue
                try:
                    f_mhz, rr, xx = float(parts[0]), float(parts[1]), float(parts[2])
                except ValueError:
                    continue
                with lock:
                    state["points"].append([round(f_mhz * 1e6), rr, xx])
            s.command("off")   # спира генератора след измерването
    except Exception as e:  # noqa: BLE001
        with lock:
            state["error"] = str(e)
    finally:
        port_lock.release()
        with lock:
            state["running"] = False
            state["finished"] = time.time()


def start_sweep(start, stop, points):
    if points < 2 or points > 1001:
        return {"ok": False, "error": "Точките трябва да са от 2 до 1001."}
    if start < FMIN_HZ or stop > FMAX_LIMIT_HZ or stop <= start:
        return {"ok": False, "error": "Честотите трябва да са между 0,1 и 1450 MHz, началото под края."}
    if (stop - start) < (points - 1):
        return {"ok": False, "error": "Обхватът е твърде тесен за толкова точки."}
    with lock:
        if state["running"]:
            return {"ok": False, "error": "Вече тече измерване."}
        fmax = state["fmax"]
        if fmax is not None and stop > fmax:
            return {"ok": False, "error": "Краят е над горната граница на уреда (%d MHz). "
                                          "Над нея уредът дава фалшиви 50 Ω." % (fmax // 1000000)}
        state.update(running=True, total=points, points=[], error="",
                     port=find_port(), started=time.time(), finished=0.0)
    threading.Thread(target=sweep_worker, args=(start, stop, points), daemon=True).start()
    return {"ok": True}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # без шум в терминала
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    ALLOWED_HOSTS = ("127.0.0.1:%d" % HTTP_PORT, "localhost:%d" % HTTP_PORT)

    def _trusted(self, post):
        """Само собствената страница: правилен Host (срещу DNS rebinding), а за POST -
        JSON и без чужд Origin (чужди сайтове не могат да пуснат измерване или спиране)."""
        if self.headers.get("Host", "") not in self.ALLOWED_HOSTS:
            return False
        if post:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in ("http://" + h for h in self.ALLOWED_HOSTS):
                return False
        return True

    def do_GET(self):
        if not self._trusted(post=False):
            self._send(403, "forbidden", "text/plain")
            return
        if self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path.startswith("/api/status"):
            with lock:
                self._json(dict(state))
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        if not self._trusted(post=True):
            self._send(403, "forbidden", "text/plain")
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            body = {}
        if self.path == "/api/ver":
            with lock:
                busy = state["running"]
            if busy:
                self._json({"ok": False, "busy": True, "error": "Анализаторът е зает - тече измерване."})
                return
            res = check_version()
            with lock:
                state["port"] = res.get("port")
                state["ver"] = res.get("ver", "")
            self._json(res)
        elif self.path == "/api/quit":
            with lock:
                busy = state["running"]
            if busy:
                self._json({"ok": False, "error": "Тече измерване - изчакай да свърши."})
                return
            self._json({"ok": True})
            # спира сървъра след като отговорът е изпратен
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        elif self.path == "/api/sweep":
            try:
                res = start_sweep(int(body["start"]), int(body["stop"]), int(body["points"]))
            except (KeyError, ValueError, TypeError):
                res = {"ok": False, "error": "Грешни параметри."}
            self._json(res)
        else:
            self._send(404, "not found", "text/plain")


def main():
    url = "http://%s:%d/" % (HOST, HTTP_PORT)
    try:
        srv = ThreadingHTTPServer((HOST, HTTP_PORT), Handler)
    except OSError:
        print("AA Scope вече работи - отварям страницата.")
        webbrowser.open(url)
        return
    print("AA Scope работи на %s" % url)
    print("Анализаторът трябва да е в главното меню. Спиране: Ctrl+C или затвори този прозорец.")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
