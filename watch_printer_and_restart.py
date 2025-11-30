import os
import time
import subprocess
import shutil
import logging
from logging.handlers import SysLogHandler

import requests

# ========== KONFIGURACJA Z ENV ==========

BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")   # np. https://truenas.local
API_KEY = os.environ.get("API_KEY", "")
APP_NAME = os.environ.get("APP_NAME", "p910nd")         # nazwa appki w SCALE (jeśli istnieje)
DOCKER_CONTAINER = os.environ.get("DOCKER_CONTAINER", "p910nd")

# Uniwersalne dopasowanie drukarki:
# Domyślnie: "usblp" (czyli dowolne urządzenie, które zarejestruje sterownik usblp – każda drukarka USB)
USB_EVENT_MATCH_ANY_OF = [
    token.strip() for token in os.environ.get(
        "USB_EVENT_MATCH_ANY_OF",
        "usblp,USB Bidirectional printer"
    ).split(",")
    if token.strip()
]

SSL_VERIFY = os.environ.get("SSL_VERIFY", "false").lower() == "true"
COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "10"))

# Logowanie
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_TO_FILE = os.environ.get("LOG_TO_FILE", "false").lower() == "true"
LOG_FILE_PATH = os.environ.get("LOG_FILE_PATH", "/var/log/usb-printer-watcher.log")
LOG_TO_SYSLOG = os.environ.get("LOG_TO_SYSLOG", "false").lower() == "true"
SYSLOG_ADDRESS = os.environ.get("SYSLOG_ADDRESS", "/dev/log")

HEADERS = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}


# ========== LOGOWANIE ==========

logger = logging.getLogger("usb-printer-watcher")
logger.setLevel(LOG_LEVEL)

fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

# stdout (zawsze) – widoczne w `docker logs` / logach aplikacji w TrueNAS
sh = logging.StreamHandler()
sh.setFormatter(fmt)
logger.addHandler(sh)

# log do pliku w kontenerze (opcjonalnie)
if LOG_TO_FILE:
    os.makedirs(os.path.dirname(LOG_FILE_PATH), exist_ok=True)
    fh = logging.FileHandler(LOG_FILE_PATH)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

# syslog (opcjonalnie; wymaga działającego sysloga pod `SYSLOG_ADDRESS`, zwykle /dev/log)
if LOG_TO_SYSLOG:
    try:
        syslog_handler = SysLogHandler(address=SYSLOG_ADDRESS)
        syslog_handler.setFormatter(logging.Formatter("usb-printer-watcher: %(levelname)s %(message)s"))
        logger.addHandler(syslog_handler)
    except Exception as e:
        logger.warning(f"Nie udało się podłączyć do sysloga ({SYSLOG_ADDRESS}): {e}")


# ========== TrueNAS API ==========

def truenas_app_exists(app_name: str) -> bool:
    if not BASE_URL or not API_KEY:
        return False
    url = f"{BASE_URL}/api/v2.0/chart/release"
    try:
        resp = requests.get(url, headers=HEADERS, verify=SSL_VERIFY, timeout=10)
        resp.raise_for_status()
        releases = resp.json()
        exists = any(r.get("name") == app_name for r in releases)
        logger.debug(f"Sprawdzam istnienie appki '{app_name}' w SCALE: {exists}")
        return exists
    except Exception as e:
        logger.warning(f"Nie udało się pobrać listy app z TrueNAS: {e}")
        return False


def restart_via_truenas(app_name: str) -> bool:
    """
    Zrestartuj appkę w SCALE.
    Dostosuj endpoint/payload do API Docs Twojego TrueNAS:
    zwykle: POST /api/v2.0/chart/release/restart {"release_name": "nazwa"}.
    """
    if not BASE_URL or not API_KEY:
        return False

    url = f"{BASE_URL}/api/v2.0/chart/release/restart"
    payload = {"release_name": app_name}
    try:
        resp = requests.post(url, json=payload, headers=HEADERS, verify=SSL_VERIFY, timeout=30)
        resp.raise_for_status()
        logger.info(f"Zrestartowano appkę TrueNAS '{app_name}' przez API.")
        return True
    except Exception as e:
        logger.error(f"Błąd przy resecie appki '{app_name}' przez API: {e}")
        return False


# ========== Docker (docker.sock) ==========

def resolve_container_name(pattern: str) -> str | None:
    """
    Szuka kontenera Dockera po nazwie.
    - najpierw próbuje dokładne dopasowanie (name == pattern),
    - jeśli brak, szuka nazw zawierających pattern jako substring.
    Zwraca nazwę kontenera albo None.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        logger.error(f"Nie udało się pobrać listy kontenerów Dockera: {e.stdout}")
        return None
    except Exception as e:
        logger.error(f"Wyjątek przy pobieraniu listy kontenerów Dockera: {e}")
        return None

    names = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not names:
        logger.error("Brak uruchomionych kontenerów Dockera.")
        return None

    # 1. dokładne dopasowanie
    if pattern in names:
        logger.info(f"Znaleziono dokładne dopasowanie kontenera Dockera: '{pattern}'.")
        return pattern

    # 2. dopasowanie przez 'pattern w nazwie'
    matches = [name for name in names if pattern in name]
    if len(matches) == 1:
        logger.info(f"Znaleziono jedno dopasowanie kontenera zawierające '{pattern}': '{matches[0]}'.")
        return matches[0]
    elif len(matches) == 0:
        logger.error(f"Nie znaleziono kontenera z nazwą zawierającą '{pattern}'. "
                     f"Dostępne kontenery: {names}")
        return None
    else:
        logger.error(f"Znaleziono wiele kontenerów zawierających '{pattern}': {matches}. "
                     f"Doprecyzuj zmienną DOCKER_CONTAINER.")
        return None


def restart_via_docker(container_pattern: str) -> bool:
    if not shutil.which("docker"):
        logger.warning("Brak binarki 'docker' w kontenerze – nie mogę zrestartować kontenera Dockera.")
        return False

    resolved_name = resolve_container_name(container_pattern)
    if not resolved_name:
        # komunikaty błędu już zostały zalogowane w resolve_container_name
        return False

    try:
        result = subprocess.run(
            ["docker", "restart", resolved_name],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        logger.info(
            f"Zrestartowano kontener Docker '{resolved_name}' (wzorzec: '{container_pattern}'). "
            f"Wyjście:\n{result.stdout.strip()}"
        )
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"Błąd przy 'docker restart {resolved_name}': {e.stdout}")
        return False
    except Exception as e:
        logger.error(f"Wyjątek przy restarcie Dockera '{resolved_name}': {e}")
        return False


# ========== GŁÓWNA LOGIKA RESTARTU ==========

def handle_printer_event():
    """
    1. Spróbuj zrestartować jako appkę SCALE.
    2. Jeśli nie istnieje lub się nie uda – spróbuj jako zwykły kontener Dockera.
    """
    logger.info("Wykryto zdarzenie drukarki USB – rozpoczynam procedurę restartu p910nd.")

    if APP_NAME and truenas_app_exists(APP_NAME):
        if restart_via_truenas(APP_NAME):
            return

    if restart_via_docker(DOCKER_CONTAINER):
        return

    logger.error("Nie udało się zrestartować p910nd ani jako appki SCALE, ani jako kontenera Dockera.")


# ========== NASŁUCH DMESG ==========

def line_matches_printer_event(line: str) -> bool:
    """
    Uniwersalne wykrywanie: jeśli jakikolwiek token z USB_EVENT_MATCH_ANY_OF
    występuje w linii dmesg – uznajemy, że to zdarzenie drukarki.
    Domyślnie: 'usblp' lub 'USB Bidirectional printer'.
    """
    for token in USB_EVENT_MATCH_ANY_OF:
        if token in line:
            return True
    return False


def follow_dmesg():
    """
    Nasłuchuje 'dmesg --follow --human' i reaguje, gdy pojawi się linia
    zawierająca którykolwiek z USB_EVENT_MATCH_ANY_OF.
    Wymaga dostępu do /dev/kmsg i odpowiednich uprawnień (privileged / CAP_SYSLOG).
    """
    logger.info(f"Start nasłuchu dmesg. Wzorce USB: {USB_EVENT_MATCH_ANY_OF}")

    if not shutil.which("dmesg"):
        logger.error("Brak 'dmesg' w kontenerze – zainstaluj kmod lub util-linux.")
        return

    proc = subprocess.Popen(
        ["dmesg", "--follow", "--human"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_trigger_time = 0.0

    try:
        for raw_line in proc.stdout:
            line = raw_line.strip()
            if not line:
                continue

            # Możesz odkomentować do debugowania:
            # logger.debug(f"[DMESG] {line}")

            if line_matches_printer_event(line):
                now = time.time()
                if now - last_trigger_time > COOLDOWN_SECONDS:
                    logger.info(f"Wykryto zdarzenie drukarki w linii: {line}")
                    handle_printer_event()
                    last_trigger_time = now
                else:
                    logger.info("Dodatkowe dopasowanie w czasie cooldownu – pomijam.")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    follow_dmesg()
