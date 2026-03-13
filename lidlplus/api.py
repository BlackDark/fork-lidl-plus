"""
Lidl Plus api
"""

import base64
import html
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from lidlplus.exceptions import (
    WebBrowserException,
    LoginError,
    LegalTermsException,
    MissingLogin,
)

_AUTH_IMPORT_ERROR = None
try:
    from getuseragent import UserAgent
    from oic.oic import Client
    from oic.utils.authn.client import CLIENT_AUTHN_METHOD
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException as SeleniumTimeoutException
    from selenium.webdriver.chrome.service import Service
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support import expected_conditions
    from selenium.webdriver.support.ui import WebDriverWait
    from webdriver_manager.chrome import ChromeDriverManager
    from webdriver_manager.core.os_manager import ChromeType
except Exception as e:
    _AUTH_IMPORT_ERROR = e
    logging.debug(f"Auth packages not available: {e}")


class LidlPlusApi:
    """Lidl Plus api connector"""

    _CLIENT_ID = "LidlPlusNativeClient"
    _AUTH_API = "https://accounts.lidl.com"
    _TICKET_API = "https://www.lidl.de/mre/api/v1"
    _COUPONS_API = "https://coupons.lidlplus.com/app/api"
    _COUPONS_V1_API = "https://coupons.lidlplus.com/app/api/"
    _PROFILE_API = "https://profile.lidlplus.com/profile/api"
    _APP = "com.lidlplus.app"
    _OS = "iOs"
    _TIMEOUT = 60
    _BROWSER_TIMEOUT = 120

    def __init__(self, language, country, refresh_token="", token="", debug_log=None):
        self._login_url = ""
        self._code_verifier = ""
        self._refresh_token = refresh_token
        self._expires = None
        self._token = token
        self._country = country.upper()
        self._language = language.lower()
        self._perf_log = []  # accumulated CDP Network events for the current browser session
        self._debug_log = debug_log  # path to write raw CDP events, or None

    @property
    def refresh_token(self):
        """Lidl Plus api refresh token"""
        return self._refresh_token

    @property
    def token(self):
        """Current token to query api"""
        return self._token

    def _register_oauth_client(self):
        if self._login_url:
            return self._login_url
        client = Client(client_authn_method=CLIENT_AUTHN_METHOD, client_id=self._CLIENT_ID)
        client.provider_config(self._AUTH_API)
        code_challenge, self._code_verifier = client.add_code_challenge()
        args = {
            "client_id": client.client_id,
            "response_type": "code",
            "scope": ["openid profile offline_access lpprofile lpapis"],
            "redirect_uri": f"{self._APP}://callback",
            **code_challenge,
        }
        auth_req = client.construct_AuthorizationRequest(request_args=args)
        self._login_url = auth_req.request(client.authorization_endpoint)
        return self._login_url

    def _init_chrome(self, headless=True):
        user_agent = UserAgent(self._OS.lower()).Random()
        logging.getLogger("WDM").setLevel(logging.NOTSET)
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless")
        options.add_experimental_option("mobileEmulation", {"userAgent": user_agent})
        # Enable CDP network logging (replaces selenium-wire)
        options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
        for chrome_type in [ChromeType.GOOGLE, ChromeType.MSEDGE, ChromeType.CHROMIUM]:
            try:
                service = Service(ChromeDriverManager(chrome_type=chrome_type).install())
                driver = webdriver.Chrome(service=service, options=options)
                driver.execute_cdp_cmd("Network.enable", {})
                return driver
            except Exception:
                continue
        # Fall back to Selenium Manager (built into Selenium 4.6+)
        try:
            driver = webdriver.Chrome(options=options)
            driver.execute_cdp_cmd("Network.enable", {})
            return driver
        except Exception as exc:
            raise WebBrowserException("Unable to find a suitable Chrome driver") from exc

    def _init_firefox(self, headless=True):
        user_agent = UserAgent(self._OS.lower()).Random()
        logging.getLogger("WDM").setLevel(logging.NOTSET)
        options = webdriver.FirefoxOptions()
        options.set_preference("general.useragent.override", user_agent)
        try:
            return webdriver.Firefox(options=options)
        except Exception as exc:
            raise WebBrowserException("Unable to find a suitable Firefox driver") from exc

    def _get_browser(self, headless=True):
        if _AUTH_IMPORT_ERROR:
            raise WebBrowserException(
                f"Auth packages failed to import: {_AUTH_IMPORT_ERROR}\n"
                "Try using Python 3.12: uv run --python 3.12 execute.py get_token\n"
                "Or check: uv sync --extra auth"
            ) from _AUTH_IMPORT_ERROR
        self._perf_log = []
        try:
            return self._init_chrome(headless=headless)
        # pylint: disable=broad-except
        except Exception as exc1:
            try:
                return self._init_firefox(headless=headless)
            except Exception as exc2:
                raise WebBrowserException(f"Chrome failed: {exc1}; Firefox failed: {exc2}") from exc2

    # --- CDP performance log helpers ---

    def _flush_log(self, browser):
        """Drain Chrome's performance log into self._perf_log.

        Chrome's get_log("performance") is a consuming read — each call returns
        only new entries since the last call, clearing the buffer. We drain into
        self._perf_log so entries are never lost between method calls.
        """
        try:
            for raw in browser.get_log("performance"):
                try:
                    entry = json.loads(raw["message"])["message"]
                    self._perf_log.append(entry)
                    if self._debug_log and entry.get("method", "").startswith("Network."):
                        method = entry.get("method", "")
                        params = entry.get("params", {})
                        url = self._entry_url(entry)
                        status = params.get("response", {}).get("status", "")
                        location = self._entry_location(entry)
                        line = (
                            f"{method:<35} {str(status):<4} "
                            f"{url[:80]}"
                            + (f"  -> {location[:80]}" if location else "")
                            + "\n"
                        )
                        with open(self._debug_log, "a") as f:
                            f.write(line)
                except Exception:
                    pass
        except Exception:
            pass  # Firefox and other non-Chrome browsers silently skip this

    def _clear_log(self, browser):
        """Drain and discard the performance log (equivalent to del browser.requests)."""
        self._flush_log(browser)
        self._perf_log.clear()

    def _entry_url(self, entry):
        """Extract the primary URL from a CDP Network event."""
        params = entry.get("params", {})
        return (
            params.get("documentURL")
            or params.get("request", {}).get("url")
            or params.get("response", {}).get("url")
            or params.get("frame", {}).get("url")
            or ""
        )

    def _entry_location(self, entry):
        """Extract the Location redirect header from a Network.responseReceived entry.

        CDP returns all headers in lowercase.
        """
        if entry.get("method") != "Network.responseReceived":
            return ""
        headers = entry.get("params", {}).get("response", {}).get("headers", {})
        return headers.get("location") or headers.get("Location") or ""

    def _wait_for_response(self, browser, pattern, timeout=None):
        """Poll the performance log until a Network.responseReceived with a matching URL appears."""
        timeout = timeout or self._BROWSER_TIMEOUT
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._flush_log(browser)
            for entry in self._perf_log:
                if entry.get("method") != "Network.responseReceived":
                    continue
                if re.search(pattern, self._entry_url(entry)) or re.search(pattern, self._entry_location(entry)):
                    return
            time.sleep(0.2)
        raise SeleniumTimeoutException(f"Timed out waiting for response matching {pattern!r}")

    def _response_body_for(self, browser, url_pattern):
        """Return the decoded response body for the first response matching url_pattern."""
        for entry in self._perf_log:
            if entry.get("method") != "Network.responseReceived":
                continue
            if not re.search(url_pattern, self._entry_url(entry)):
                continue
            request_id = entry.get("params", {}).get("requestId")
            if not request_id:
                continue
            try:
                result = browser.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})
                body = result.get("body", "")
                if result.get("base64Encoded"):
                    body = base64.b64decode(body).decode("utf-8", errors="replace")
                return html.unescape(body)
            except Exception:
                pass
        return ""

    # --- Auth helpers ---

    def _auth(self, payload):
        default_secret = base64.b64encode(f"{self._CLIENT_ID}:secret".encode()).decode()
        headers = {
            "Authorization": f"Basic {default_secret}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        kwargs = {"headers": headers, "data": payload, "timeout": self._TIMEOUT}
        response = requests.post(f"{self._AUTH_API}/connect/token", **kwargs).json()
        self._expires = datetime.now(timezone.utc) + timedelta(seconds=response["expires_in"])
        self._token = response["access_token"]
        self._refresh_token = response["refresh_token"]

    def _renew_token(self):
        payload = {"refresh_token": self._refresh_token, "grant_type": "refresh_token"}
        return self._auth(payload)

    def _authorization_code(self, code):
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": f"{self._APP}://callback",
            "code_verifier": self._code_verifier,
        }
        return self._auth(payload)

    @property
    def _register_link(self):
        args = {
            "Country": self._country,
            "language": f"{self._language}-{self._country}",
        }
        params = "&".join([f"{key}={value}" for key, value in args.items()])
        return f"{self._register_oauth_client()}&{params}"

    @staticmethod
    def _accept_legal_terms(browser, wait, accept=True):
        wait.until(expected_conditions.visibility_of_element_located((By.ID, "checkbox_Accepted"))).click()
        if not accept:
            title = browser.find_element(By.TAG_NAME, "h2").text
            raise LegalTermsException(title)
        browser.find_element(By.TAG_NAME, "button").click()

    def _parse_code(self, browser, wait, accept_legal_terms=True):
        self._flush_log(browser)
        for entry in reversed(self._perf_log):
            method = entry.get("method", "")
            # Case 1: browser tried to navigate to the app callback URL directly
            if method == "Network.requestWillBeSent":
                url = self._entry_url(entry)
                if url.startswith(f"{self._APP}://callback"):
                    if code := re.findall(r"code=([0-9A-F]+)", url):
                        return code[0]
            # Case 2: auth server issued a redirect via Location header
            if method == "Network.responseReceived":
                url = self._entry_url(entry)
                if f"{self._AUTH_API}/connect" not in url:
                    continue
                location = self._entry_location(entry)
                if "legalTerms" in location:
                    self._accept_legal_terms(browser, wait, accept=accept_legal_terms)
                    return self._parse_code(browser, wait, False)
                if code := re.findall(r"code=([0-9A-F]+)", location):
                    return code[0]
        return ""

    def _click(self, browser, button, request=""):
        self._clear_log(browser)
        browser.find_element(*button).click()
        self._check_input_error(browser)
        if request:
            self._wait_for_response(browser, request)
            self._check_input_error(browser)

    @staticmethod
    def _check_input_error(browser):
        if errors := browser.find_elements(By.CLASS_NAME, "input-error-message"):
            for error in errors:
                if error.text:
                    raise LoginError(error.text)

    def _check_login_error(self, browser):
        # Match app-errors attribute without crossing into other attributes.
        # The attribute uses either single or double quotes as delimiter.
        m = re.search(r'app-errors=(?:\'([^\']*)\'|"([^"]*)")', browser.page_source)
        if not m:
            return
        raw = m.group(1) or m.group(2)
        try:
            err_data = json.loads(html.unescape(raw))
        except (json.JSONDecodeError, ValueError):
            return
        if err_data:
            raise LoginError(str(next(iter(err_data.values()))))

    def _check_2fa_auth(self, browser, wait, verify_mode="phone", verify_token_func=None):
        if verify_mode not in ["phone", "email"]:
            raise ValueError(f'Unknown 2fa-mode "{verify_mode}" - Only "phone" or "email" supported')
        method_button_id = "sso_2FAvalidation_emailbutton" if verify_mode == "email" else "sso_2FAvalidation_smsbutton"
        # Poll purely on DOM state — no network log checks to avoid false positives from
        # the initial /connect/authorize navigation that was loaded before clicking login.
        deadline = time.monotonic() + self._BROWSER_TIMEOUT
        state = None
        while time.monotonic() < deadline:
            if browser.find_elements(By.ID, method_button_id):
                state = "method_selection"
                break
            if browser.find_elements(By.NAME, "verificationCode"):
                state = "code_entry"
                break
            # Auth completed without 2FA: app callback URL appeared in the log
            self._flush_log(browser)
            if any(self._entry_location(e).startswith(self._APP) for e in self._perf_log):
                return
            time.sleep(0.3)
        if state == "method_selection":
            browser.find_element(By.ID, method_button_id).click()
            wait.until(expected_conditions.element_to_be_clickable((By.NAME, "verificationCode")))
            state = "code_entry"
        if state == "code_entry":
            verify_code = verify_token_func()  # type: ignore
            browser.find_element(By.NAME, "verificationCode").send_keys(verify_code)
            self._click(browser, (By.CSS_SELECTOR, '[data-testid="button-primary"]'))

    def login(self, login, password, method, **kwargs):
        """Simulate app auth"""
        browser = self._get_browser(headless=kwargs.get("headless", True))
        browser.get(self._register_link)
        wait = WebDriverWait(browser, self._BROWSER_TIMEOUT)
        wait.until(expected_conditions.visibility_of_element_located((By.XPATH, '//*[@id="duple-button-block"]/button[1]/span'))).click()
        if method == "p":  # Login with phone number
            wait.until(expected_conditions.element_to_be_clickable((By.CSS_SELECTOR, '[data-testid="button-login-switch"]'))).click()
            wait.until(expected_conditions.element_to_be_clickable((By.NAME, "input-phone"))).send_keys(login)
        else:  # Login with email
            wait.until(expected_conditions.element_to_be_clickable((By.NAME, "input-email"))).send_keys(login)
        wait.until(expected_conditions.element_to_be_clickable((By.NAME, "Password"))).send_keys(password)
        # Click login — don't block on a specific network URL since the login POST
        # endpoint may vary; _check_2fa_auth handles waiting for the next DOM state.
        self._clear_log(browser)
        browser.find_element(By.XPATH, '//*[@id="duple-button-block"]/button').click()
        self._check_input_error(browser)
        self._check_login_error(browser)
        self._check_2fa_auth(
            browser,
            wait,
            kwargs.get("verify_mode", "phone"),
            kwargs.get("verify_token_func"),
        )
        accept_legal = kwargs.get("accept_legal_terms", True)
        code = ""
        deadline = time.monotonic() + self._BROWSER_TIMEOUT
        while time.monotonic() < deadline:
            self._flush_log(browser)
            code = self._parse_code(browser, wait, accept_legal_terms=accept_legal)
            if code:
                break
            time.sleep(0.5)
        if not code:
            raise LoginError("Could not extract authorization code from callback URL")
        self._authorization_code(code)
        browser.close()

    def _default_headers(self):
        if not self._token and self._refresh_token:
            self._renew_token()
        if not self._token:
            raise MissingLogin("You need to login!")
        return {
            "Authorization": f"Bearer {self._token}",
            "App-Version": "999.99.9",
            "Operating-System": self._OS,
            "App": "com.lidl.eci.lidl.plus",
            "Accept-Language": self._language,
        }

    def _web_headers(self):
        """Browser-like headers required by www.lidl.de/mre API."""
        token = self._token
        if not token and self._refresh_token:
            self._renew_token()
            token = self._token
        return {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
            "Accept": "application/json",
            "Accept-Language": f"{self._language},{self._language}-{self._country};q=0.9",
            "content-type": "application/json",
            "Cookie": f"authToken={token}",
        }

    def tickets_in_range(self, from_ym, to_ym):
        """Fetch ticket summaries, stopping as soon as we pass from_ym (newest-first API)."""
        url = f"{self._TICKET_API}/tickets"
        kwargs = {"headers": self._web_headers(), "timeout": self._TIMEOUT}
        result = []
        page = 1
        while True:
            resp = requests.get(f"{url}?country={self._country}&page={page}", **kwargs).json()
            items = resp.get("items", resp.get("tickets", []))
            if not items:
                break
            done = False
            for t in items:
                date_str = t.get("date") or t.get("dateTime") or t.get("Date")
                if not date_str:
                    continue
                try:
                    dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    ym = (dt.year, dt.month)
                except (ValueError, AttributeError):
                    continue
                if ym > to_ym:
                    continue  # too recent, skip
                if ym < from_ym:
                    done = True  # older than range, stop
                    break
                result.append(t)
            if done or len(items) < resp.get("size", 10):
                break
            page += 1
        return result

    def tickets(self, only_favorite=False):
        """Get a list of all tickets."""
        url = f"{self._TICKET_API}/tickets"
        kwargs = {"headers": self._web_headers(), "timeout": self._TIMEOUT}
        params = f"country={self._country}&page=1"
        if only_favorite:
            params += "&onlyFavorite=true"
        first = requests.get(f"{url}?{params}", **kwargs).json()
        all_tickets = first.get("items", first.get("tickets", []))
        total = first.get("totalCount", 0)
        size = first.get("size", len(all_tickets)) or 1
        for i in range(2, int(total / size) + 2):
            page = requests.get(f"{url}?country={self._country}&page={i}", **kwargs).json()
            all_tickets += page.get("items", page.get("tickets", []))
        return all_tickets

    def ticket(self, ticket_id):
        """Get full data of single ticket by id"""
        kwargs = {"headers": self._web_headers(), "timeout": self._TIMEOUT}
        lang_code = f"{self._language}-{self._country}"
        url = f"{self._TICKET_API}/tickets/{ticket_id}?country={self._country}&languageCode={lang_code}"
        return requests.get(url, **kwargs).json()

    def coupon_promotions_v1(self):
        """Get list of all coupons API V1"""
        url = f"{self._COUPONS_V1_API}/v1/promotionslist"
        kwargs = {"headers": {**self._default_headers(), "Country": self._country}, "timeout": self._TIMEOUT}
        return requests.get(url, **kwargs).json()

    def activate_coupon_promotion_v1(self, promotion_id):
        """Activate single coupon by id API V1"""
        url = f"{self._COUPONS_API}/v1/promotions/{promotion_id}/activation"
        kwargs = {"headers": {**self._default_headers(), "Country": self._country}, "timeout": self._TIMEOUT}
        return requests.post(url, **kwargs).text

    def coupons(self):
        """Get list of all coupons"""
        url = f"{self._COUPONS_API}/v2/promotionsList"
        headers = {**self._default_headers(), "Country": self._country}
        kwargs = {"headers": headers, "timeout": self._TIMEOUT}
        return requests.get(url, **kwargs).json()

    def activate_coupon(self, coupon_id):
        """Activate single coupon by id"""
        url = f"{self._COUPONS_API}/v1/promotions/{coupon_id}/activation"
        kwargs = {"headers": {**self._default_headers(), "Country": self._country}, "timeout": self._TIMEOUT}
        return requests.post(url, **kwargs).text

    def deactivate_coupon(self, coupon_id):
        """Deactivate single coupon by id"""
        url = f"{self._COUPONS_API}/v1/{self._country}/{coupon_id}/activation"
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        return requests.delete(url, **kwargs).json()

    def loyalty_id(self):
        """Get your loyalty ID"""
        url = f"{self._PROFILE_API}/v1/{self._country}/loyalty"
        kwargs = {"headers": self._default_headers(), "timeout": self._TIMEOUT}
        response = requests.get(url, **kwargs)
        response.raise_for_status()
        return response.text
