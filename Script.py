
import asyncio
import aiohttp
import ssl
import time
import random
import os
from collections import defaultdict
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional

# Try to import SOCKS support (optional)
try:
    from aiohttp_socks import ProxyConnector
    SOCKS_SUPPORT = True
except ImportError:
    SOCKS_SUPPORT = False
    ProxyConnector = None

# ── Configuration ─────────────────────────────────────────────────
BUTTONS: List[Dict[str, Any]] = [
    {"method": "GET", "url": "https://www.example.ai/"},
    {"method": "GET", "url": "https://www.example.ai/{random_query}"},
    {"method": "GET", "url": "https://www.example.ai/{random_user}"},
    {"method": "POST",
     "url": "https://www.example.ai/",
     "data": {"submit": "login", "username": "{random_user}", "password": "dummy"}},
    {"method": "POST",
     "url": "https://www.example.ai/",
     "data": "vote=yes&option={random_option}"},
    {"method": "POST",
     "url": "https://www.example.ai/",
     "data": {"action": "add_to_cart", "product_id": "{random_product}"}},
]

NUM_BOTS = 20
REQUESTS_PER_BOT = 10
MAX_CONCURRENT = 1
MIN_DELAY = 0.1
MAX_DELAY = 1

# ── Proxy rotation ────────────────────────────────────────────────
PROXY = None                 # Single proxy (e.g. "http://45.131.208.46:80")
PROXY_LIST_FILE = "http.txt"
MAX_PROXY_RETRIES = 3
TOO_MANY_FAILS = 5

# ── Proxy pre-testing ─────────────────────────────────────────────
ENABLE_PROXY_TEST = True    # Set to True to filter dead proxies first
TEST_PROXY_TIMEOUT = 0.0001
TEST_PROXY_URL = "https://api.ipify.org/"

# ── SSL / TLS ──────────────────────────────────────────────────────
VERIFY_SSL = True
CUSTOM_CA_FILE: Optional[str] = None
TIMEOUT = aiohttp.ClientTimeout(total=15, connect=5)

# ── Connection optimisation ────────────────────────────────────────
FORCE_CLOSE = False
CONNECTOR_LIMIT = 100
CONNECTOR_LIMIT_PER_HOST = 20

# ── User-Agent rotation ───────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
]
# ────────────────────────────────────────────────────────────────────

class ProxyManager:
    def __init__(self):
        self.proxies: List[str] = []
        self.single_proxy: Optional[str] = None
        self.fail_counts = defaultdict(int)

        if PROXY:
            self.single_proxy = PROXY
            print(f"[*] Using single proxy: {PROXY}")
        elif PROXY_LIST_FILE and os.path.isfile(PROXY_LIST_FILE):
            with open(PROXY_LIST_FILE, 'r') as f:
                self.proxies = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            if not self.proxies:
                print("[!] Proxy file is empty. Running without proxy.")
            else:
                print(f"[*] Loaded {len(self.proxies)} proxies from {PROXY_LIST_FILE}")
        else:
            print("[*] No proxy configured. Running with direct connection.")

    def get_proxy(self, exclude: Optional[List[str]] = None) -> Optional[str]:
        if self.single_proxy:
            return self.single_proxy
        if not self.proxies:
            return None
        available = self.proxies if exclude is None else [p for p in self.proxies if p not in exclude]
        if not available:
            return None
        return random.choice(available)

    def record_failure(self, proxy: str):
        self.fail_counts[proxy] += 1
        if self.fail_counts[proxy] >= TOO_MANY_FAILS:
            if proxy in self.proxies:
                self.proxies.remove(proxy)
                print(f"[!] Removed dead proxy after {TOO_MANY_FAILS} failures: {proxy}")

async def test_proxy(proxy: str) -> bool:
    """Test if a proxy works (any scheme)."""
    # If SOCKS support is missing, test only HTTP/HTTPS natively
    if not SOCKS_SUPPORT:
        if proxy.startswith(("http://", "https://")):
            try:
                timeout = aiohttp.ClientTimeout(total=TEST_PROXY_TIMEOUT)
                async with aiohttp.ClientSession(timeout=timeout) as sess:
                    async with sess.get(TEST_PROXY_URL, proxy=proxy) as resp:
                        return resp.status == 200
            except:
                return False
        else:
            return False  # SOCKS cannot be tested

    # With SOCKS support, use unified connector
    try:
        connector = ProxyConnector.from_url(proxy)
        timeout = aiohttp.ClientTimeout(total=TEST_PROXY_TIMEOUT)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as sess:
            async with sess.get(TEST_PROXY_URL) as resp:
                return resp.status == 200
    except:
        return False

class Bot:
    def __init__(self, bot_id: int, session: aiohttp.ClientSession,
                 semaphore: asyncio.Semaphore, proxy_mgr: ProxyManager):
        self.bot_id = bot_id
        self.session = session
        self.semaphore = semaphore
        self.proxy_mgr = proxy_mgr

    def _fill_placeholders(self, template: str) -> str:
        placeholders = {
            "{random_user}": f"user_{random.randint(1, 1000)}",
            "{random_query}": random.choice(["news", "weather", "sports", "tech"]),
            "{random_option}": random.choice(["a", "b", "c"]),
            "{random_product}": f"SKU-{random.randint(1000, 9999)}",
        }
        for placeholder, value in placeholders.items():
            template = template.replace(placeholder, value)
        return template

    async def single_request(self, url: str, method: str, call_args: dict):
        tried_proxies = []
        last_error = None

        for attempt in range(MAX_PROXY_RETRIES):
            proxy = self.proxy_mgr.get_proxy(exclude=tried_proxies)
            if proxy is not None:
                tried_proxies.append(proxy)

            try:
                async with self.semaphore:
                    status, body_snippet = await self._execute_request(url, method, call_args, proxy)
                    print(f"Bot {self.bot_id:03d} | {method:4s} {url} → {status} | {body_snippet}...")
                if proxy:
                    self.proxy_mgr.fail_counts[proxy] = 0   # reset counter
                return
            except asyncio.TimeoutError:
                last_error = f"TIMEOUT (proxy: {proxy})"
                print(f"Bot {self.bot_id:03d} | {method} {url} → {last_error}")
            except aiohttp.ClientError as e:
                last_error = f"ERROR (proxy: {proxy}): {e}"
                print(f"Bot {self.bot_id:03d} | {method} {url} → {last_error}")
            except Exception as e:
                last_error = f"UNKNOWN ERROR (proxy: {proxy}): {e}"
                print(f"Bot {self.bot_id:03d} | {method} {url} → {last_error}")

            if proxy:
                self.proxy_mgr.record_failure(proxy)

        print(f"Bot {self.bot_id:03d} | {method} {url} → FAILED after {MAX_PROXY_RETRIES} attempts. Last error: {last_error}")

    async def _execute_request(self, url: str, method: str, call_args: dict, proxy: Optional[str]):
        """Execute request with or without a proxy.
        - Uses native aiohttp proxy for HTTP/HTTPS if SOCKS library absent.
        - Uses ProxyConnector if SOCKS support is present (for all types) or if proxy is SOCKS.
        """
        if proxy:
            # Determine if the proxy is a SOCKS type
            proxy_lower = proxy.lower()
            is_socks = proxy_lower.startswith(("socks4://", "socks5://", "socks://"))

            if is_socks or SOCKS_SUPPORT:
                # Need the library for SOCKS; also used for unified handling if library installed
                if not SOCKS_SUPPORT:
                    raise RuntimeError(
                        f"SOCKS proxy requested but aiohttp_socks is not installed. "
                        f"Install it: pip install aiohttp-socks"
                    )
                connector = ProxyConnector.from_url(proxy)
                jar = self.session.cookie_jar
                timeout = aiohttp.ClientTimeout(total=TIMEOUT.total)
                async with aiohttp.ClientSession(
                    connector=connector,
                    cookie_jar=jar,
                    timeout=timeout
                ) as proxied_session:
                    return await self._do_request(proxied_session, url, method, call_args)
            else:
                # Native aiohttp proxy support for HTTP/HTTPS (no extra library)
                # Just pass the proxy parameter to the existing session's request
                return await self._do_request(self.session, url, method, call_args, proxy=proxy)
        else:
            # Direct connection
            return await self._do_request(self.session, url, method, call_args)

    async def _do_request(self, session: aiohttp.ClientSession, url: str,
                          method: str, call_args: dict, proxy: Optional[str] = None):
        """Core HTTP request, optionally with proxy parameter."""
        req_kwargs = {**call_args}
        if proxy:
            req_kwargs["proxy"] = proxy

        async with session.request(
            method, url,
            timeout=TIMEOUT,
            ssl=VERIFY_SSL if isinstance(VERIFY_SSL, bool) else True,
            **req_kwargs
        ) as response:
            status = response.status
            body = await response.text()
            snippet = body[:80].replace("\n", " ")
            return status, snippet

    async def run(self) -> None:
        for i in range(REQUESTS_PER_BOT):
            await asyncio.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

            button = random.choice(BUTTONS)
            url = button["url"]
            method = button.get("method", "GET").upper()
            url = self._fill_placeholders(url)

            call_args: Dict[str, Any] = {}
            if USER_AGENTS:
                call_args.setdefault("headers", {})
                call_args["headers"]["User-Agent"] = random.choice(USER_AGENTS)

            if method == "POST":
                data = button.get("data")
                headers = button.get("headers", {})
                if isinstance(data, dict):
                    processed = {k: self._fill_placeholders(str(v)) for k, v in data.items()}
                    call_args["json"] = processed
                elif isinstance(data, str):
                    call_args["data"] = self._fill_placeholders(data)
                if headers:
                    call_args.setdefault("headers", {}).update(headers)

            await self.single_request(url, method, call_args)

def make_ssl_context() -> Optional[ssl.SSLContext]:
    if not VERIFY_SSL:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    if CUSTOM_CA_FILE:
        return ssl.create_default_context(cafile=CUSTOM_CA_FILE)
    return None

async def main() -> None:
    proxy_mgr = ProxyManager()

    ssl_ctx = make_ssl_context()
    connector = aiohttp.TCPConnector(
        limit=CONNECTOR_LIMIT,
        limit_per_host=CONNECTOR_LIMIT_PER_HOST,
        force_close=FORCE_CLOSE,
        ssl=ssl_ctx,
        enable_cleanup_closed=True,
    )

    trust_env = (proxy_mgr.single_proxy is None and not proxy_mgr.proxies)
    async with aiohttp.ClientSession(
        connector=connector,
        trust_env=trust_env,
        cookie_jar=aiohttp.CookieJar(unsafe=False),
    ) as session:
        if ENABLE_PROXY_TEST and proxy_mgr.proxies:
            print(f"[*] Testing proxies against {TEST_PROXY_URL}...")
            alive = []
            for p in proxy_mgr.proxies:
                if await test_proxy(p):
                    alive.append(p)
                else:
                    print(f"    Dead proxy removed: {p}")
            proxy_mgr.proxies = alive
            print(f"[*] {len(alive)} proxies survived.")

        semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        bots = [Bot(i + 1, session, semaphore, proxy_mgr) for i in range(NUM_BOTS)]
        tasks = [b.run() for b in bots]

        start = time.time()
        await asyncio.gather(*tasks)
        elapsed = time.time() - start
        print(f"\n✅ All {NUM_BOTS} bots finished in {elapsed:.2f}s.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
