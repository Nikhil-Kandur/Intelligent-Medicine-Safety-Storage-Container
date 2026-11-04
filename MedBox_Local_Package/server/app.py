from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # launcher installs pyserial before this file is started
    serial = None
    list_ports = None

ROOT = Path(__file__).resolve().parent
DATA_FILE = Path(os.environ.get("DATA_FILE", ROOT / "data.json"))
HOST = os.environ.get("MEDBOX_HOST", "127.0.0.1")
PORT = int(os.environ.get("MEDBOX_PORT", "5000"))
DEVICE_KEY = os.environ.get("DEVICE_KEY", "medbox-demo-key")
USER_NAME = os.environ.get("USER_NAME", "customer")
USER_PASS = os.environ.get("USER_PASS", "medbox123")
ADMIN_NAME = os.environ.get("ADMIN_NAME", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "admin123")
ALLOW_DEMO_BUTTONS = os.environ.get("ALLOW_DEMO_BUTTONS", "true").lower() != "false"
TZ_MIN = int(os.environ.get("TZ_OFFSET_MIN", "330"))
SECRET = os.environ.get("SESSION_SECRET", "medbox-local-demo-secret-change-me")
SERIAL_PORT_SETTING = os.environ.get("MEDBOX_SERIAL_PORT", "AUTO").strip()
SERIAL_BAUD = int(os.environ.get("MEDBOX_SERIAL_BAUD", "115200"))
ONLINE_MS = 20_000
MAX_HISTORY = 10_080
MAX_SCHED = 16


def blank_shelf():
    return {"baseline": None, "baselineAt": None, "lastCheck": None}


def defaults():
    return {
        "nextId": 1,
        "medicines": [],
        "schedule": [],
        "shelves": {"1": blank_shelf(), "2": blank_shelf()},
        "settings": {"tolAbs": 2.0, "tolPct": 1.5, "stableG": 2.0, "dueWindowMin": 30},
        "history": [],
        "log": [],
    }


def load_db():
    base = defaults()
    try:
        raw = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            base.update(raw)
            base["shelves"] = {**defaults()["shelves"], **(raw.get("shelves") or {})}
            base["settings"] = {**defaults()["settings"], **(raw.get("settings") or {})}
    except (OSError, json.JSONDecodeError, TypeError):
        pass
    return base


db = load_db()
state_lock = threading.RLock()
fresh_condition = threading.Condition(state_lock)
serial_write_lock = threading.Lock()
current_device: dict[str, Any] | None = None
current_bin: dict[str, Any] | None = None
measure: dict[str, Any] | None = None
command_queue: list[str] = []
serial_status = {"connected": False, "port": None, "message": "Waiting for ESP32 USB serial connection"}
fails: dict[str, tuple[int, float]] = {}


def save_db():
    with state_lock:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = DATA_FILE.with_suffix(DATA_FILE.suffix + ".tmp")
        tmp.write_text(json.dumps(db, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, DATA_FILE)


def log_event(text: str):
    with state_lock:
        db["log"].insert(0, {"at": int(time.time() * 1000), "text": text})
        del db["log"][40:]
    save_db()


def next_id():
    with state_lock:
        i = int(db.get("nextId", 1))
        db["nextId"] = i + 1
        return i


def as_num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def rnd(v, digits=1):
    return None if v is None else round(float(v), digits)


def clean(value, limit):
    text = str(value if value is not None else "")
    text = text.replace("|", " ").replace("\r", " ").replace("\n", " ")
    text = "".join(ch if 32 <= ord(ch) <= 126 else "?" for ch in text)
    return text.strip()[:limit]


def local_datetime(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.fromtimestamp(ts, timezone.utc) + timedelta(minutes=TZ_MIN)


def local_date(ts=None):
    return local_datetime(ts).strftime("%Y-%m-%d")


def local_minutes(dt=None):
    dt = dt or local_datetime()
    return dt.hour * 60 + dt.minute


def hhmm_to_min(s):
    try:
        hh, mm = str(s or "").split(":", 1)
        h, m = int(hh), int(mm)
        return h * 60 + m if 0 <= h <= 23 and 0 <= m <= 59 else None
    except (ValueError, TypeError):
        return None


def push_history(ts, t, h):
    global current_bin
    bin_id = int(ts // 60_000)
    with state_lock:
        if current_bin and current_bin["bin"] != bin_id:
            if current_bin["nT"] > 0:
                db["history"].append({
                    "ts": current_bin["bin"] * 60_000,
                    "t": current_bin["sumT"] / current_bin["nT"],
                    "h": current_bin["sumH"] / current_bin["nH"] if current_bin["nH"] else None,
                })
                del db["history"][:-MAX_HISTORY]
                save_db()
            current_bin = None
        if current_bin is None:
            current_bin = {"bin": bin_id, "sumT": 0.0, "nT": 0, "sumH": 0.0, "nH": 0}
        if t is not None:
            current_bin["sumT"] += t
            current_bin["nT"] += 1
        if h is not None:
            current_bin["sumH"] += h
            current_bin["nH"] += 1


def mkt(temps):
    if not temps:
        return None
    k = 10000.0
    mean = sum(math.exp(-k / (c + 273.15)) for c in temps) / len(temps)
    return k / (-math.log(mean)) - 273.15


def window_temps(win):
    spans = {"1h": 3600_000, "24h": 86400_000, "7d": 604800_000, "all": float("inf")}
    cutoff = time.time() * 1000 - spans.get(win, 86400_000)
    out = []
    with state_lock:
        out.extend(s["t"] for s in db["history"] if s.get("ts", 0) >= cutoff and s.get("t") is not None)
        if current_bin and current_bin["nT"]:
            out.append(current_bin["sumT"] / current_bin["nT"])
    return out


def temp_status(t):
    if t is None:
        return {"level": "na", "text": "No reading"}
    if 15 <= t <= 25:
        return {"level": "ok", "text": "Within 15-25 °C"}
    if 25 < t <= 30 or 8 <= t < 15:
        return {"level": "caution", "text": "Above 25 °C (excursion limit 30 °C)" if t > 25 else "Below 15 °C"}
    return {"level": "alert", "text": "Above 30 °C" if t > 30 else "Below 8 °C"}


def hum_status(h):
    if h is None:
        return {"level": "na", "text": "No reading"}
    if h <= 60:
        return {"level": "ok", "text": "At or below 60 %RH"}
    if h <= 75:
        return {"level": "caution", "text": "Above 60 %RH"}
    return {"level": "alert", "text": "Above 75 %RH"}


def mkt_status(value):
    if value is None:
        return {"level": "na", "text": "No data yet"}
    return {"level": "ok", "text": "MKT at or below 25 °C"} if value <= 25 else {"level": "alert", "text": "MKT above 25 °C"}


def schedule_view(item, dt=None):
    dt = dt or local_datetime()
    dow = (dt.weekday() + 1) % 7  # Sunday=0, matching JavaScript getUTCDay() on shifted local time
    days = item.get("days") or []
    for_today = not days or dow in days
    taken = item.get("takenDate") == dt.strftime("%Y-%m-%d")
    minute = hhmm_to_min(item.get("time"))
    due_window = int(db["settings"].get("dueWindowMin", 30))
    due = bool(for_today and not taken and minute is not None and local_minutes(dt) >= minute and local_minutes(dt) < minute + due_window)
    return {**item, "forToday": for_today, "taken": taken, "due": due}


def issue_token(role, name):
    payload = {"role": role, "name": name, "exp": int(time.time() * 1000) + 12 * 3600_000}
    raw = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
    return raw + "." + sig


def verify_token(token):
    try:
        raw, sig = token.split(".", 1)
        want = base64.urlsafe_b64encode(hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(sig, want):
            return None
        payload = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        return payload if payload.get("exp", 0) > int(time.time() * 1000) else None
    except Exception:
        return None


def get_user():
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return None
    return verify_token(header[7:])


def require_auth(fn):
    """The redesigned app is a single-user local demo; no customer/admin login."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        request.user = {"role": "local", "name": "Local user"}
        return fn(*args, **kwargs)
    return wrapper


def require_admin(fn):
    """Local-only setup actions are available on the same screen as other controls."""
    return require_auth(fn)


def eval_shelves(shelves):
    with state_lock:
        dev = current_device.copy() if current_device else None
        settings = db["settings"].copy()
    if not dev or int(time.time() * 1000) - dev["lastSeen"] > ONLINE_MS:
        return {"ok": False, "msg": "The box is offline or the USB serial cable is disconnected"}
    weights = {}
    for shelf in shelves:
        if not dev.get("hx" + str(shelf)):
            return {"ok": False, "msg": f"Load cell {shelf} is not reading"}
        if not dev.get("c" + str(shelf)):
            return {"ok": False, "msg": f"Shelf {shelf} is not calibrated. Admin: tare the empty shelf, add a known weight and calibrate first."}
        noise = dev.get("n" + str(shelf))
        if noise is not None and noise > settings["stableG"]:
            return {"ok": False, "msg": f"Shelf {shelf} is not steady yet (noise {rnd(noise, 1)} g). Keep still."}
        weight = dev.get("w" + str(shelf))
        if weight is None:
            return {"ok": False, "msg": f"Shelf {shelf} has no calibrated weight reading"}
        weights[shelf] = weight
    return {"ok": True, "weights": weights}


def fresh_reading(shelves, timeout=14.0):
    start_ms = int(time.time() * 1000)
    deadline = time.monotonic() + timeout
    with fresh_condition:
        last = {"ok": False, "msg": "The box is offline (no data received over USB)"}
        while time.monotonic() < deadline:
            if current_device and current_device["lastSeen"] >= start_ms:
                last = eval_shelves(shelves)
                if last["ok"]:
                    return last
            remaining = deadline - time.monotonic()
            if remaining > 0:
                fresh_condition.wait(min(remaining, 1.0))
        return last


def solve(target, meds, tol, unlimited):
    solutions = []
    counts = [0] * len(meds)
    steps = 0

    def rec(i, total, items):
        nonlocal steps
        steps += 1
        if steps > 400_000:
            return
        if i == len(meds):
            if abs(total - target) <= tol:
                solutions.append({"counts": counts.copy(), "total": total, "err": abs(total - target), "n": items})
            return
        med = meds[i]
        max_count = math.floor((target + tol) / med["w"]) if unlimited else min(med["max"], math.floor((target + tol) / med["w"]))
        for count in range(max_count + 1):
            counts[i] = count
            rec(i + 1, total + count * med["w"], items + count)
        counts[i] = 0

    rec(0, 0.0, 0)
    solutions.sort(key=lambda x: (x["err"], x["n"]))
    return solutions[:5]


def check_shelf(shelf, current):
    sh = db["shelves"][str(shelf)]
    out = {"shelf": shelf, "baseline": sh["baseline"], "current": rnd(current, 1), "at": int(time.time() * 1000)}
    if sh["baseline"] is None:
        out.update(status="no_baseline", solutions=[])
        return out
    delta = sh["baseline"] - current
    tol = max(db["settings"]["tolAbs"], db["settings"]["tolPct"] / 100 * abs(delta))
    out.update(delta=rnd(delta, 1), tol=rnd(tol, 1))
    if abs(delta) <= tol:
        out.update(status="unchanged", solutions=[])
        return out
    removed = delta > 0
    # Each entry represents one complete medicine pack/container, not tablets.
    # A removal can only match packs currently recorded as present; an addition
    # can only match packs registered as pending on this shelf.
    meds = [
        {"id": m["id"], "name": m["name"], "w": m["unitWeight"], "max": 1}
        for m in db["medicines"]
        if m["shelf"] == shelf
        and (m.get("unitWeight") or 0) > 0
        and ((m.get("qty", 0) > 0) if removed else (m.get("qty", 0) <= 0))
    ]
    sols = solve(abs(delta), meds, tol, False)
    out["status"] = ("removed" if removed else "added") if sols else "unknown"
    out["solutions"] = [{"items": [{"medicineId": meds[i]["id"], "name": meds[i]["name"], "count": c} for i, c in enumerate(sol["counts"]) if c > 0], "total": rnd(sol["total"], 1), "err": rnd(sol["err"], 2)} for sol in sols]
    out["ambiguous"] = len(sols) > 1 and (sols[1]["err"] - sols[0]["err"]) < tol / 2
    return out


def add_command(command):
    with state_lock:
        command_queue.append(command)


def serial_response_lines():
    """Create the schedule/time/command response sent to the ESP32 on USB serial."""
    now = time.time()
    dt = local_datetime(now)
    epoch = int(now)
    lines = [f"SYNC:{epoch}:{TZ_MIN}:{int(db['settings']['dueWindowMin'])}"]
    with state_lock:
        pending = list(command_queue)
        for item in db["schedule"]:
            view = schedule_view(item, dt)
            if not view["forToday"] or hhmm_to_min(item.get("time")) is None:
                continue
            tm = str(item["time"]).zfill(5)
            lines.append(f"SCHED:{tm}|{clean(item.get('label'), 20)}|{clean(item.get('dose'), 16)}|{1 if view['taken'] else 0}")
    lines.extend("CMD:" + c for c in pending)
    lines.append("END")
    return "\n".join(lines) + "\n", pending


def choose_serial_port():
    if SERIAL_PORT_SETTING and SERIAL_PORT_SETTING.upper() != "AUTO":
        return SERIAL_PORT_SETTING
    if list_ports is None:
        return None
    ports = list(list_ports.comports())
    if not ports:
        return None
    preferred = []
    other = []
    for p in ports:
        if p.vid in (0x10C4, 0x1A86, 0x303A, 0x0403, 0x067B):
            preferred.append(p.device)
        elif "bluetooth" not in (p.description or "").lower():
            other.append(p.device)
    return (preferred or other or [None])[0]


def accept_telemetry(packet):
    global current_device
    now_ms = int(time.time() * 1000)
    dev = {
        "lastSeen": now_ms,
        "ip": "USB serial",
        "t": as_num(packet.get("t")), "h": as_num(packet.get("h")), "dht": as_num(packet.get("dht")),
        "w1": as_num(packet.get("w1")), "w2": as_num(packet.get("w2")),
        "n1": as_num(packet.get("n1")), "n2": as_num(packet.get("n2")),
        "hx1": bool(packet.get("hx1")), "hx2": bool(packet.get("hx2")),
        "cal1": as_num(packet.get("cal1")), "cal2": as_num(packet.get("cal2")),
        "c1": bool(packet.get("c1")), "c2": bool(packet.get("c2")),
        "bat": None, "rssi": None, "up": as_num(packet.get("up")),
        "fw": clean(packet.get("fw", ""), 12), "ack": clean(packet.get("ack", ""), 48),
        "aseq": as_num(packet.get("aseq")),
    }
    with fresh_condition:
        current_device = dev
        fresh_condition.notify_all()
    push_history(now_ms, dev["t"], dev["h"])


def serial_worker():
    """USB serial reader. ESP32 telemetry and PC replies use newline-delimited MBX protocol."""
    while True:
        port_name = choose_serial_port()
        if not port_name:
            serial_status.update(connected=False, port=None, message="No serial port found; connect ESP32 by USB")
            time.sleep(3)
            continue
        ser = None
        try:
            ser = serial.Serial(port_name, SERIAL_BAUD, timeout=0.25, write_timeout=2)
            time.sleep(2.0)  # ESP32 boards commonly reset when the COM port opens
            try:
                ser.reset_input_buffer()
            except Exception:
                pass
            serial_status.update(connected=True, port=port_name, message=f"Connected to ESP32 on {port_name}")
            print(f"[USB] Connected to ESP32 on {port_name} at {SERIAL_BAUD} baud")
            while True:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="ignore").strip()
                if not line.startswith("MBX:"):
                    continue
                try:
                    packet = json.loads(line[4:])
                except json.JSONDecodeError:
                    continue
                if packet.get("type") != "telemetry":
                    continue
                accept_telemetry(packet)
                response, sent_commands = serial_response_lines()
                with serial_write_lock:
                    ser.write(response.encode("ascii", errors="replace"))
                    ser.flush()
                if sent_commands:
                    with state_lock:
                        for command in sent_commands:
                            try:
                                command_queue.remove(command)
                            except ValueError:
                                pass
        except Exception as exc:
            serial_status.update(connected=False, port=None, message=f"USB serial disconnected: {exc}")
            print(f"[USB] {exc}; retrying serial connection")
            time.sleep(2)
        finally:
            try:
                if ser and ser.is_open:
                    ser.close()
            except Exception:
                pass
            serial_status.update(connected=False, port=None, message="Waiting for ESP32 USB serial connection")


app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024


@app.get("/")
def home():
    return send_from_directory(ROOT, "index.html")


@app.get("/api/health")
def health():
    return jsonify(ok=True, serial=serial_status)


@app.get("/api/config")
def config():
    return jsonify(demoButtons=ALLOW_DEMO_BUTTONS, tzMin=TZ_MIN)


@app.post("/api/login")
def login():
    ip = request.remote_addr or "local"
    count, last = fails.get(ip, (0, 0))
    if count >= 8 and time.time() - last < 60:
        return jsonify(error="Too many attempts. Wait a minute."), 429
    body = request.get_json(silent=True) or {}
    username, password = str(body.get("username") or ""), str(body.get("password") or "")
    if hmac.compare_digest(username, ADMIN_NAME) and hmac.compare_digest(password, ADMIN_PASS):
        fails.pop(ip, None)
        return jsonify(token=issue_token("admin", ADMIN_NAME), role="admin")
    if hmac.compare_digest(username, USER_NAME) and hmac.compare_digest(password, USER_PASS):
        fails.pop(ip, None)
        return jsonify(token=issue_token("customer", USER_NAME), role="customer")
    fails[ip] = (count + 1 if time.time() - last < 60 else 1, time.time())
    return jsonify(error="Wrong username or password"), 401


@app.post("/api/login/demo")
def demo_login():
    if not ALLOW_DEMO_BUTTONS:
        return jsonify(error="Demo login is disabled"), 403
    body = request.get_json(silent=True) or {}
    role = "admin" if body.get("role") == "admin" else "customer"
    return jsonify(token=issue_token(role, role), role=role)


@app.get("/api/state")
@require_auth
def api_state():
    now_ms = int(time.time() * 1000)
    dt = local_datetime()
    win = request.args.get("window", "24h")
    if win not in ("1h", "24h", "7d", "all"):
        win = "24h"
    temps = window_temps(win)
    mkt_value = rnd(mkt(temps), 2)
    with state_lock:
        dev = current_device.copy() if current_device else None
        online = bool(dev and now_ms - dev["lastSeen"] < ONLINE_MS)
        hist = [{"ts": x["ts"], "t": rnd(x.get("t"), 1), "h": rnd(x.get("h"), 1)} for x in db["history"][-180:]]
        if current_bin and current_bin["nT"]:
            hist.append({"ts": now_ms, "t": rnd(current_bin["sumT"] / current_bin["nT"], 1), "h": rnd(current_bin["sumH"] / current_bin["nH"], 1) if current_bin["nH"] else None})
        schedule = [schedule_view(item, dt) for item in db["schedule"]]
        medicines = list(db["medicines"])
        shelves = json.loads(json.dumps(db["shelves"]))
        settings = dict(db["settings"])
        logs = list(db["log"][:15])
        measure_data = {"medicineId": measure["medicineId"], "shelf": measure["shelf"], "before": measure["before"]} if measure else None
    if dev:
        dev.update(online=online, ageSec=round((now_ms - dev["lastSeen"]) / 1000))
    else:
        dev = {"online": False}
    cur = dev if online else None
    readings = {
        "temp": cur.get("t") if cur else None,
        "hum": cur.get("h") if cur else None,
        "bat": None,
        "w1": cur.get("w1") if cur else None,
        "w2": cur.get("w2") if cur else None,
        "ldr1": None,
        "ldr2": None,
    }
    return jsonify(
        role=request.user["role"],
        serverTime=dt.strftime("%Y-%m-%d %H:%M"), tzMin=TZ_MIN,
        device=dev, readings=readings,
        mkt={"value": mkt_value, "window": win, "samples": len(temps)},
        status={"temp": temp_status(readings["temp"]), "hum": hum_status(readings["hum"]), "mkt": mkt_status(mkt_value)},
        history=hist, medicines=medicines, schedule=schedule,
        due=[x for x in schedule if x["due"]], shelves=shelves, settings=settings,
        log=logs, measure=measure_data,
    )


def read_medicine(body, partial=False):
    out = {}
    if not partial or "name" in body:
        name = clean(body.get("name"), 40)
        if not name:
            raise ValueError("Name is required")
        out["name"] = name
    if not partial or "shelf" in body:
        try:
            shelf = int(body.get("shelf"))
        except (ValueError, TypeError):
            shelf = 0
        if shelf not in (1, 2):
            raise ValueError("Shelf must be 1 or 2")
        out["shelf"] = shelf
    if not partial or "qty" in body:
        try:
            qty = int(body.get("qty"))
        except (ValueError, TypeError):
            qty = -1
        if not 0 <= qty <= 9999:
            raise ValueError("Quantity must be 0 or more")
        out["qty"] = qty
    if not partial or "dose" in body:
        out["dose"] = clean(body.get("dose"), 40)
    if body.get("unitWeight") not in (None, ""):
        try:
            weight = float(body["unitWeight"])
        except (ValueError, TypeError):
            weight = 0
        if not 0 < weight < 20000:
            raise ValueError("Unit weight must be above 0")
        out["unitWeight"] = rnd(weight, 2)
    return out


@app.post("/api/medicines")
@require_auth
def add_medicine():
    try:
        body = request.get_json(silent=True) or {}
        out = read_medicine(body)
        med = {"id": next_id(), "unitWeight": None, "dose": "", **out}
        with state_lock:
            db["medicines"].append(med)
        log_event(f"Added medicine {med['name']} (shelf {med['shelf']})")
        save_db()
        return jsonify(med)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.put("/api/medicines/<int:medicine_id>")
@require_auth
def update_medicine(medicine_id):
    with state_lock:
        med = next((m for m in db["medicines"] if m["id"] == medicine_id), None)
    if not med:
        return jsonify(error="Not found"), 404
    try:
        med.update(read_medicine(request.get_json(silent=True) or {}, partial=True))
        save_db()
        return jsonify(med)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.delete("/api/medicines/<int:medicine_id>")
@require_auth
def delete_medicine(medicine_id):
    global measure
    with state_lock:
        db["medicines"] = [m for m in db["medicines"] if m["id"] != medicine_id]
        # Remove all associated ingestion times with the item.
        db["schedule"] = [item for item in db["schedule"] if item.get("medicineId") != medicine_id]
        if measure and measure["medicineId"] == medicine_id:
            measure = None
    save_db()
    return jsonify(ok=True)


@app.post("/api/measure/before")
@require_auth
def measure_before():
    global measure
    body = request.get_json(silent=True) or {}
    try:
        medicine_id = int(body.get("medicineId"))
    except (ValueError, TypeError):
        medicine_id = -1
    with state_lock:
        med = next((m for m in db["medicines"] if m["id"] == medicine_id), None)
    if not med:
        return jsonify(error="Choose a medicine first"), 404
    result = fresh_reading([med["shelf"]])
    if not result["ok"]:
        return jsonify(error=result["msg"]), 409
    measure = {"medicineId": med["id"], "shelf": med["shelf"], "before": result["weights"][med["shelf"]], "at": int(time.time() * 1000)}
    return jsonify(before=rnd(measure["before"], 1), shelf=med["shelf"])


@app.post("/api/measure/after")
@require_auth
def measure_after():
    global measure
    if not measure:
        return jsonify(error='Press "Weigh BEFORE" first'), 400
    body = request.get_json(silent=True) or {}
    try:
        units = max(1, int(body.get("units", 1)))
    except (ValueError, TypeError):
        units = 1
    with state_lock:
        m = next((x for x in db["medicines"] if x["id"] == measure["medicineId"]), None)
        mcopy = dict(measure)
    if not m:
        measure = None
        return jsonify(error="Medicine was deleted"), 404
    result = fresh_reading([mcopy["shelf"]])
    if not result["ok"]:
        return jsonify(error=result["msg"]), 409
    after = result["weights"][mcopy["shelf"]]
    diff = after - mcopy["before"]
    if diff < 0.8:
        return jsonify(error=f"Weight only changed by {rnd(diff, 1)} g. Place the item on shelf {mcopy['shelf']} first."), 422
    m["unitWeight"] = rnd(diff / units, 2)
    log_event(f"Measured {m['name']}: {m['unitWeight']} g per unit")
    measure = None
    save_db()
    return jsonify(before=rnd(mcopy["before"], 1), after=rnd(after, 1), unitWeight=m["unitWeight"])


@app.post("/api/measure/cancel")
@require_auth
def measure_cancel():
    global measure
    measure = None
    return jsonify(ok=True)


@app.post("/api/stock/baseline")
@require_auth
def stock_baseline():
    body = request.get_json(silent=True) or {}
    try:
        shelf = int(body.get("shelf"))
    except (ValueError, TypeError):
        shelf = 0
    if shelf not in (1, 2):
        return jsonify(error="Shelf must be 1 or 2"), 400
    result = fresh_reading([shelf])
    if not result["ok"]:
        return jsonify(error=result["msg"]), 409
    with state_lock:
        db["shelves"][str(shelf)] = {"baseline": rnd(result["weights"][shelf], 1), "baselineAt": int(time.time() * 1000), "lastCheck": None}
        sh = dict(db["shelves"][str(shelf)])
    log_event(f"Shelf {shelf} baseline set to {sh['baseline']} g")
    save_db()
    return jsonify(sh)


@app.post("/api/stock/check")
@require_auth
def stock_check():
    with state_lock:
        shelves = [s for s in (1, 2) if db["shelves"][str(s)]["baseline"] is not None]
    if not shelves:
        return jsonify(error='Set a baseline first (put the medicines on the shelf, then press "Set baseline")'), 400
    result = fresh_reading(shelves)
    if not result["ok"]:
        return jsonify(error=result["msg"]), 409
    checks = []
    with state_lock:
        for shelf in shelves:
            check = check_shelf(shelf, result["weights"][shelf])
            db["shelves"][str(shelf)]["lastCheck"] = check
            checks.append(check)
    save_db()
    return jsonify(results=checks)


@app.post("/api/stock/confirm")
@require_auth
def stock_confirm():
    body = request.get_json(silent=True) or {}
    try:
        shelf, pick = int(body.get("shelf")), int(body.get("solution"))
    except (ValueError, TypeError):
        return jsonify(error="Invalid shelf or solution"), 400
    with state_lock:
        sh = db["shelves"].get(str(shelf)) if shelf in (1, 2) else None
        lc = sh.get("lastCheck") if sh else None
        if not lc:
            return jsonify(error='Run "Check stock" first'), 400
        lines = []
        if pick >= 0:
            if pick >= len(lc.get("solutions", [])):
                return jsonify(error="Unknown option"), 400
            for item in lc["solutions"][pick]["items"]:
                med = next((m for m in db["medicines"] if m["id"] == item["medicineId"]), None)
                if not med:
                    continue
                if lc["status"] == "removed":
                    med["qty"] = max(0, med["qty"] - item["count"])
                else:
                    med["qty"] += item["count"]
                lines.append(f"{'-' if lc['status'] == 'removed' else '+'}{item['count']} {med['name']}")
        sh["baseline"] = lc["current"]
        sh["baselineAt"] = int(time.time() * 1000)
        sh["lastCheck"] = None
    log_event("Shelf %s stock updated%s" % (shelf, ": " + ", ".join(lines) if lines else " (weight accepted)"))
    save_db()
    return jsonify(ok=True)


def read_schedule(body):
    tm = str(body.get("time") or "")
    if hhmm_to_min(tm) is None:
        raise ValueError("Time must look like 08:30")
    label = clean(body.get("label"), 40)
    medicine_id = None
    if body.get("medicineId"):
        try:
            mid = int(body.get("medicineId"))
        except (ValueError, TypeError):
            mid = -1
        med = next((m for m in db["medicines"] if m["id"] == mid), None)
        if med:
            medicine_id = med["id"]
            if not label:
                label = med["name"]
    if not label:
        raise ValueError("Choose a medicine or type a name")
    days = [int(x) for x in body.get("days", []) if str(x).lstrip("-").isdigit() and 0 <= int(x) <= 6] if isinstance(body.get("days", []), list) else []
    if len(days) == 7:
        days = []
    return {"time": tm.zfill(5), "medicineId": medicine_id, "label": label, "dose": clean(body.get("dose"), 40), "days": days}


@app.post("/api/schedule")
@require_auth
def add_schedule():
    try:
        item = {"id": next_id(), "takenDate": None, **read_schedule(request.get_json(silent=True) or {})}
        with state_lock:
            db["schedule"].append(item)
        save_db()
        return jsonify(item)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.put("/api/schedule/<int:item_id>")
@require_auth
def update_schedule(item_id):
    with state_lock:
        item = next((x for x in db["schedule"] if x["id"] == item_id), None)
    if not item:
        return jsonify(error="Not found"), 404
    try:
        item.update(read_schedule(request.get_json(silent=True) or {}))
        save_db()
        return jsonify(item)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400


@app.delete("/api/schedule/<int:item_id>")
@require_auth
def delete_schedule(item_id):
    with state_lock:
        db["schedule"] = [x for x in db["schedule"] if x["id"] != item_id]
    save_db()
    return jsonify(ok=True)


@app.post("/api/schedule/<int:item_id>/taken")
@require_auth
def mark_taken(item_id):
    with state_lock:
        item = next((x for x in db["schedule"] if x["id"] == item_id), None)
        if not item:
            return jsonify(error="Not found"), 404
        item["takenDate"] = local_date()
        label, tm = item.get("label", ""), item.get("time", "")
    log_event(f"Marked taken: {tm} {label}")
    save_db()
    return jsonify(ok=True)


@app.post("/api/admin/tare")
@require_admin
def admin_tare():
    body = request.get_json(silent=True) or {}
    try:
        shelf = int(body.get("shelf"))
    except (ValueError, TypeError):
        shelf = 0
    if shelf not in (1, 2):
        return jsonify(error="Shelf must be 1 or 2"), 400
    command = f"TARE:{shelf}"
    add_command(command)
    return jsonify(queued=command)


@app.post("/api/admin/calibrate")
@require_admin
def admin_calibrate():
    body = request.get_json(silent=True) or {}
    try:
        shelf, grams = int(body.get("shelf")), float(body.get("grams"))
    except (ValueError, TypeError):
        shelf, grams = 0, 0
    if shelf not in (1, 2) or not 5 <= grams <= 20000:
        return jsonify(error="Shelf 1 or 2 and a known weight of 5 g or more"), 400
    command = f"CAL:{shelf}:{grams:g}"
    add_command(command)
    return jsonify(queued=command)


@app.post("/api/admin/settings")
@require_admin
def admin_settings():
    body = request.get_json(silent=True) or {}
    ranges = {"tolAbs": (0.2, 50), "tolPct": (0, 20), "stableG": (0.2, 50), "dueWindowMin": (5, 240)}
    with state_lock:
        settings = db["settings"]
        for key, (lo, hi) in ranges.items():
            if key in body:
                try:
                    value = float(body[key])
                    if math.isfinite(value) and lo <= value <= hi:
                        settings[key] = value
                except (ValueError, TypeError):
                    pass
        result = dict(settings)
    save_db()
    return jsonify(result)


@app.post("/api/admin/reset")
@require_admin
def admin_reset():
    global db, measure, current_bin
    with state_lock:
        db = defaults()
        measure = None
        current_bin = None
    save_db()
    return jsonify(ok=True)


# ============================================================================
# SIMPLIFIED SINGLE-PAGE LOCAL UI API
# ============================================================================

def parse_item_times(body):
    raw = body.get("times") if "times" in body else body.get("time")
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raw = [raw]
    times = []
    for value in raw:
        value = str(value or "").strip()
        if not value:
            continue
        if hhmm_to_min(value) is None:
            raise ValueError("Each ingestion time must be a valid time")
        value = value.zfill(5)
        if value not in times:
            times.append(value)
    if len(times) > MAX_SCHED:
        raise ValueError(f"The OLED supports up to {MAX_SCHED} reminders")
    return times


def replace_item_times(med, times):
    """Replace one item's reminders while preserving IDs/taken state for unchanged times."""
    others = [x for x in db["schedule"] if x.get("medicineId") != med["id"]]
    if len(others) + len(times) > MAX_SCHED:
        raise ValueError(f"The OLED supports up to {MAX_SCHED} reminders in total")
    previous = [x for x in db["schedule"] if x.get("medicineId") == med["id"]]
    used_ids = set()
    rebuilt = []
    for tm in times:
        old = next((x for x in previous if x.get("time") == tm and x.get("id") not in used_ids), None)
        if old:
            entry = dict(old)
            used_ids.add(old.get("id"))
            entry["label"] = med["name"]
        else:
            entry = {
                "id": next_id(),
                "takenDate": None,
                "time": tm,
                "medicineId": med["id"],
                "label": med["name"],
                "dose": "Take",
                "days": [],
            }
        rebuilt.append(entry)
    db["schedule"] = others + rebuilt


@app.post("/api/items")
def add_storage_item():
    body = request.get_json(silent=True) or {}
    name = clean(body.get("name"), 40)
    try:
        shelf = int(body.get("shelf"))
        weight = float(body.get("weight"))
        times = parse_item_times(body)
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc) or "Enter a shelf and a valid package weight in grams"), 400
    if not name:
        return jsonify(error="Enter an item name"), 400
    if shelf not in (1, 2):
        return jsonify(error="Choose Shelf 1 or Shelf 2"), 400
    if not math.isfinite(weight) or not 0.1 <= weight <= 20000:
        return jsonify(error="Package weight must be between 0.1 g and 20,000 g"), 400

    med = {
        "id": next_id(),
        "name": name,
        "shelf": shelf,
        "qty": 0,
        "unitWeight": rnd(weight, 2),
        "dose": "Take",
    }
    with state_lock:
        if len(db["schedule"]) + len(times) > MAX_SCHED:
            return jsonify(error=f"The OLED supports up to {MAX_SCHED} reminders in total"), 400
        db["medicines"].append(med)
        try:
            replace_item_times(med, times)
        except ValueError as exc:
            db["medicines"].remove(med)
            return jsonify(error=str(exc)), 400
    log_event(f"Added {name} ({weight:g} g) to Shelf {shelf}")
    save_db()
    return jsonify({**med, "weight": med["unitWeight"], "present": False}), 201


@app.put("/api/items/<int:item_id>")
def update_storage_item(item_id):
    body = request.get_json(silent=True) or {}
    name = clean(body.get("name"), 40)
    try:
        shelf = int(body.get("shelf"))
        weight = float(body.get("weight"))
        times = parse_item_times(body) if ("times" in body or "time" in body) else None
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc) or "Enter a shelf and a valid package weight in grams"), 400
    if not name:
        return jsonify(error="Enter an item name"), 400
    if shelf not in (1, 2):
        return jsonify(error="Choose Shelf 1 or Shelf 2"), 400
    if not math.isfinite(weight) or not 0.1 <= weight <= 20000:
        return jsonify(error="Package weight must be between 0.1 g and 20,000 g"), 400
    with state_lock:
        med = next((m for m in db["medicines"] if m["id"] == item_id), None)
        if not med:
            return jsonify(error="Item not found"), 404
        if times is not None:
            try:
                others = [x for x in db["schedule"] if x.get("medicineId") != item_id]
                if len(others) + len(times) > MAX_SCHED:
                    return jsonify(error=f"The OLED supports up to {MAX_SCHED} reminders in total"), 400
            except ValueError as exc:
                return jsonify(error=str(exc)), 400
        if shelf != med["shelf"]:
            med["qty"] = 0
        med.update(name=name, shelf=shelf, unitWeight=rnd(weight, 2))
        for item in db["schedule"]:
            if item.get("medicineId") == item_id:
                item["label"] = name
        if times is not None:
            replace_item_times(med, times)
    log_event(f"Updated {name}: {weight:g} g on Shelf {shelf}")
    save_db()
    return jsonify({**med, "weight": med["unitWeight"], "present": bool(med.get("qty", 0))})


@app.post("/api/items/<int:item_id>/reminders")
def set_item_reminders(item_id):
    body = request.get_json(silent=True) or {}
    try:
        times = parse_item_times(body)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with state_lock:
        med = next((m for m in db["medicines"] if m["id"] == item_id), None)
        if not med:
            return jsonify(error="Item not found"), 404
        if len([x for x in db["schedule"] if x.get("medicineId") != item_id]) + len(times) > MAX_SCHED:
            return jsonify(error=f"The OLED supports up to {MAX_SCHED} reminders in total"), 400
        replace_item_times(med, times)
    log_event(f"Updated ingestion times for {med['name']}")
    save_db()
    return jsonify(ok=True, times=times)


@app.post("/api/items/<int:item_id>/reminder")
def set_item_reminder(item_id):
    # Backward-compatible endpoint for older local UI versions.
    body = request.get_json(silent=True) or {}
    try:
        times = parse_item_times(body)
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with state_lock:
        med = next((m for m in db["medicines"] if m["id"] == item_id), None)
        if not med:
            return jsonify(error="Item not found"), 404
        try:
            replace_item_times(med, times)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
    log_event(f"Updated ingestion times for {med['name']}")
    save_db()
    return jsonify(ok=True, times=times)


@app.post("/api/storage/reload")
def storage_reload():
    """Read both scales, initialise first baseline, then infer complete packs added/removed."""
    result = fresh_reading([1, 2])
    if not result["ok"]:
        return jsonify(error=result["msg"]), 409

    outcomes = []
    events = []
    with state_lock:
        for shelf in (1, 2):
            current = rnd(result["weights"][shelf], 1)
            sh = db["shelves"][str(shelf)]

            if sh.get("baseline") is None:
                sh.update(baseline=current, baselineAt=int(time.time() * 1000), lastCheck=None)
                # On first setup, the user registers items already placed inside
                # the box, then Reload records that physical arrangement.
                for med in db["medicines"]:
                    if med.get("shelf") == shelf:
                        med["qty"] = 1
                outcomes.append({"shelf": shelf, "status": "initialised", "current": current, "message": "Starting storage weight recorded"})
                continue

            check = check_shelf(shelf, current)
            sh["lastCheck"] = check

            if check["status"] == "unchanged":
                sh.update(baseline=current, baselineAt=int(time.time() * 1000), lastCheck=None)
                outcomes.append({"shelf": shelf, "status": "unchanged", "current": current, "delta": check.get("delta", 0)})
                continue

            if check["status"] in ("removed", "added") and len(check.get("solutions", [])) == 1:
                solution = check["solutions"][0]
                changes = []
                for item in solution.get("items", []):
                    med = next((m for m in db["medicines"] if m["id"] == item["medicineId"]), None)
                    if not med:
                        continue
                    if check["status"] == "removed":
                        med["qty"] = 0
                        changes.append({"name": med["name"], "action": "removed", "weight": med["unitWeight"]})
                    else:
                        med["qty"] = 1
                        changes.append({"name": med["name"], "action": "added", "weight": med["unitWeight"]})
                sh.update(baseline=current, baselineAt=int(time.time() * 1000), lastCheck=None)
                events.extend(changes)
                outcomes.append({"shelf": shelf, "status": check["status"], "current": current, "delta": check.get("delta"), "changes": changes, "autoApplied": True})
                continue

            outcomes.append({
                "shelf": shelf,
                "status": check["status"],
                "current": current,
                "delta": check.get("delta"),
                "solutions": check.get("solutions", []),
                "ambiguous": check.get("ambiguous", False),
                "autoApplied": False,
            })

    if events:
        for event in events:
            log_event(f"{event['action'].title()}: {event['name']} ({event['weight']} g)")
    save_db()
    return jsonify(results=outcomes, events=events)


@app.post("/api/storage/resolve")
def storage_resolve():
    body = request.get_json(silent=True) or {}
    try:
        shelf = int(body.get("shelf"))
        solution = int(body.get("solution"))
    except (TypeError, ValueError):
        return jsonify(error="Choose a valid shelf and match"), 400
    with state_lock:
        sh = db["shelves"].get(str(shelf)) if shelf in (1, 2) else None
        check = sh.get("lastCheck") if sh else None
        if not check:
            return jsonify(error="There is no pending storage change to resolve"), 400
        if solution < 0:
            sh.update(baseline=check["current"], baselineAt=int(time.time() * 1000), lastCheck=None)
            msg = f"Shelf {shelf} weight accepted; item identity was not changed"
        else:
            solutions = check.get("solutions", [])
            if solution >= len(solutions):
                return jsonify(error="That match is no longer available"), 400
            changes = []
            for item in solutions[solution].get("items", []):
                med = next((m for m in db["medicines"] if m["id"] == item["medicineId"]), None)
                if not med:
                    continue
                if check["status"] == "removed":
                    med["qty"] = 0
                    action = "Removed"
                else:
                    med["qty"] = 1
                    action = "Added"
                changes.append(f"{action}: {med['name']}")
            sh.update(baseline=check["current"], baselineAt=int(time.time() * 1000), lastCheck=None)
            msg = ", ".join(changes) or "Storage updated"
    log_event(msg)
    save_db()
    return jsonify(ok=True, message=msg)


def main():
    if serial is None:
        raise SystemExit("pyserial is missing. Run start_medbox.bat so it installs automatically.")
    worker = threading.Thread(target=serial_worker, name="medbox-usb-reader", daemon=True)
    worker.start()
    url = f"http://{HOST}:{PORT}"
    print("\nSMART MEDICINE BOX — LOCAL DEMO")
    print(f"Website: {url}")
    print("Single-user local interface — no login required.")
    print("Connect ESP32 to this computer with USB. Close Arduino Serial Monitor first.")
    print("Press Ctrl+C to stop. Data is saved locally in server/data.json.\n")
    if os.environ.get("MEDBOX_NO_BROWSER", "0") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=HOST, port=PORT, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
