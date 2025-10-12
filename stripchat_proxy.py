import base64
import hashlib
import json
import logging
import os
import re
import typing as t
from argparse import ArgumentParser
from functools import cached_property
from itertools import cycle
from random import randint

from aiohttp import (ClientConnectionError, ClientProxyConnectionError,
                     ClientResponseError, ClientSession, web)
from aiohttp_socks import ProxyConnectionError, ProxyConnector
from multidict import CIMultiDictProxy
from yarl import URL


class KeyStorage:
    def __init__(self) -> None:
        self._key: str | None = None
    @property
    def key(self) -> str | None:
        return self._key
    
    @key.setter
    def key(self, value: str | None) -> None:
        self._key = value

APP_DECODE_KEY = web.AppKey("decode_key", KeyStorage)
APP_SESSION_KEY = web.AppKey("session", ClientSession)
APP_SOCKS_SESSION_KEY = web.AppKey("socks_session", ClientSession)
APP_PROXY_URL = web.AppKey("http_proxy_url", URL)
APP_STRIPCHAT_HOST_KEY = web.AppKey("mirror", str)

DEFAULT_STRIPCHAt_HOST = "stripchat.com"
REQUEST_TIMEOUT = 5
API_ENDPOINT_MODEL = "https://{}/api/front/v2/models/username/{}/cam"
API_CONFIG_URL = "https://{}/api/front/v3/config/static"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/88.0.4324.182 Safari/537.36"
)
FORWARD_HEADERS = {
    "Referer": f"https://{DEFAULT_STRIPCHAt_HOST}/",
    "Origin": f"https://{DEFAULT_STRIPCHAt_HOST}",
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Keep-Alive": "timeout=30, max=1000",
    "DNT": "1",
}
M3U8_BASE_URL = (
    "https://edge-hls.doppiocdn.com/hls/{stream_name}/master/{stream_name}_auto.m3u8"
)
S_HEADERS = (
    "Range",
    "User-Agent",
    "Accept",
    "Accept-Encoding",
    "Referer",
    "Origin",
    "If-None-Match",
    "If-Modified-Since",
    "Cookie",
)
URI_TAGS = (
    "#EXT-X-MEDIA",
    "#EXT-X-I-FRAME-STREAM-INF",
    "#EXT-X-MAP",
    "#EXT-X-KEY",
    "#EXT-X-PART",
    "#EXT-X-PRELOAD-HINT",
    "#EXT-X-RENDITION-REPORT",
)

URI_REGEX_SEARCH = re.compile(r'URI=(?:"([^"]+)"|([^,]+))', flags=re.IGNORECASE)
URI_REGEX_SUB = re.compile(r'URI=(?:"[^"]+"|[^,]+)', flags=re.IGNORECASE)
CHUNK_SIZE = 1048576

MP4_REGEX = re.compile(r"^https://.*\.mp4$")
logger = logging.getLogger(os.path.basename(__file__))
logging.basicConfig(level=logging.INFO)


async def setup_session(app: web.Application) -> t.AsyncGenerator:
    session = ClientSession(raise_for_status=True)
    app[APP_SESSION_KEY] = session
    logger.info("Client session created")
    yield
    await session.close()
    logger.info("Client session closed")


class BaseView(web.View):
    @property
    def client_session(self) -> ClientSession:
        return self.request.config_dict[APP_SESSION_KEY]

    @property
    def forward_headers(self) -> dict:
        headers = dict(FORWARD_HEADERS)
        base_url = f"https://{self.stripchat_host}"
        headers.update({"Referer": base_url, "Origin": base_url})
        return headers

    @property
    def stripchat_host(self) -> str:
        return self.request.config_dict[APP_STRIPCHAT_HOST_KEY]

    @t.overload
    async def fetch(
        self, url, decode_json: t.Literal[False] = ..., **kwargs
    ) -> str: ...

    @t.overload
    async def fetch(self, url, decode_json: t.Literal[True], **kwargs) -> dict: ...

    @property
    def upstream_headers(self) -> dict:
        upstream_headers = self.forward_headers
        for k in S_HEADERS:
            if v := self.request.headers.get(k):
                upstream_headers[k] = v
        return upstream_headers

    @staticmethod
    def if_proxy_error(e: Exception) -> bool:
        if isinstance(e, ProxyConnectionError):
            return True
        if isinstance(e.__cause__, ProxyConnectionError):
            return True
        return False

    async def fetch(
        self,
        url: str | URL,
        decode_json: bool = False,
        proxy: URL | None = None,
        **kwargs,
    ) -> str | dict:
        if proxy and (session := self.request.config_dict.get(APP_SOCKS_SESSION_KEY)):
            logger.info(f"Try to connect via {proxy}")
            proxy = None
            try:
                async with session.get(url, **kwargs) as resp:
                    if decode_json:
                        return await resp.json()
                    return await resp.text()
            except Exception as e:
                if self.if_proxy_error(e):
                    logger.error(f"Cannot connect to proxy server {proxy}")

        try:
            async with self.client_session.get(url, proxy=proxy, **kwargs) as resp:
                if decode_json:
                    return await resp.json()
                return await resp.text()
        except ClientProxyConnectionError:
            raise web.HTTPBadRequest(reason=f"Cannot connect to proxy server: {proxy}")
        except ClientConnectionError:
            raise web.HTTPBadRequest(reason=f"Cannot connect to host: {url}")

    def match_info(self, k: str) -> str:
        return self.request.match_info[k]

    def raise_client_response_error(
        self, e: ClientResponseError, msg: str | None = None
    ):
        for cls in web.HTTPClientError.__subclasses__():
            if cls.status_code == e.status:
                raise cls(reason=msg or e.message)
        raise web.HTTPBadRequest(reason=msg or str(e))

    def url_for(self, route_name: str, **kwargs) -> URL:
        return self.request.app.router[route_name].url_for(**kwargs)

    @staticmethod
    def extract_psch_pkey(s: str) -> tuple[str, str]:
        if s.upper().startswith("#EXT-X-MOUFLON:PSCH"):
            parts = s.rsplit(":", 2)
            if len(parts) == 3:
                return parts[-2], parts[-1]
        return "", ""


class ModelView(BaseView):

    @cached_property
    def model_name(self):
        return self.match_info("model_name")

    async def fetch_stream_url(self) -> URL:
        api_url = API_ENDPOINT_MODEL.format(self.stripchat_host, self.model_name)
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"https://{self.stripchat_host}/{self.model_name}",
            "User-Agent": FORWARD_HEADERS["User-Agent"],
        }
        try:
            data = await self.fetch(
                api_url,
                decode_json=True,
                headers=headers,
                proxy=self.request.config_dict.get(APP_PROXY_URL),
            )

        except ClientResponseError as e:
            msg = f"Unable to load model data: ({e.message})"
            logger.error(msg)
            self.raise_client_response_error(e, msg)
        except json.JSONDecodeError:
            msg = "Unable to load model data: Invalid JSON response"
            logger.error(msg)
            raise web.HTTPBadRequest(reason=msg)

        if not data or not "cam" in data or not "user" in data:
            msg = "Invalid API response or stream offline"
            logger.error(msg)
            raise web.HTTPNotFound(reason=msg)
        user_data = data["user"]["user"]
        if not user_data["isLive"] or user_data["status"] != "public":
            msg = (
                "Stream offline or private"
                f"(status: {user_data['status'] or 'Unknown'})"
            )
            logger.error(msg)
            raise web.HTTPBadRequest(reason=msg)
        stream_name = data["cam"]["streamName"]
        m3u8_url = M3U8_BASE_URL.format(stream_name=stream_name)
        return URL(m3u8_url)

    async def get(self) -> web.StreamResponse:
        logger.info(f"New connection request for username: {self.model_name}")
        stream_url = await self.fetch_stream_url()
        logger.debug(
            "Incoming request for: %s -> orig: %s" % (self.model_name, stream_url)
        )
        try:
            m3u8_text = await self.fetch(stream_url, headers=self.upstream_headers)
        except ClientResponseError as e:
            logger.error(f"Unable to load stream list: ({e})")
            self.raise_client_response_error(e)
        m3u8_text = self.update_stream_urls(m3u8_text)
        content_type = "application/vnd.apple.mpegurl"
        return web.Response(text=m3u8_text, content_type=content_type)

    def update_stream_urls(self, m3u8_text: str) -> str:
        out = []
        psch = ""
        pkey = ""
        for line in m3u8_text.splitlines():
            if line.startswith("#EXT-X-MOUFLON"):
                psch, pkey = self.extract_psch_pkey(line)
            if not line.startswith("#"):
                remote_url = URL(line)
                assert remote_url.host
                line = self.url_for(
                    "playlist", path=remote_url.path.lstrip("/")
                ).update_query(psch=psch, pkey=pkey, host=remote_url.host)
                line = str(line)
            out.append(line)
        return "\n".join(out)


class PlaylistView(BaseView):
    def _make_absolute(self, base: str | URL, url: str | URL) -> URL:
        return URL(base).join(URL(url))

    async def _fetch_key(self) -> str:
        logger.info("Fetching decode key")
        data = await self.fetch(
            API_CONFIG_URL.format(self.stripchat_host),
            decode_json=True,
            headers=self.forward_headers,
            timeout=REQUEST_TIMEOUT,
        )
        config_data = data["static"]
        origin = config_data["features"]["MMPExternalSourceOrigin"]
        version = config_data["featuresV2"]["playerModuleExternalLoading"]["mmpVersion"]
        if not origin or not version:
            msg = "Failed to extract URLs from config API."
            logger.warning(msg)
            raise web.HTTPBadRequest(reason=msg)

        main_js_url = f"{origin}/v{version}/main.js"
        main_js_text = await self.fetch(
            main_js_url, headers=self.forward_headers, timeout=REQUEST_TIMEOUT
        )

        match = re.search(r'require\("./(Doppio[^"]*\.js)"\)', main_js_text)
        if not match:
            msg = "Doppio JS name not found in main.js"
            logger.error(msg)
            raise web.HTTPBadRequest(reason=msg)
        doppio_name = match.group(1)
        doppio_url = f"{origin}/v{version}/{doppio_name}"
        doppio_js_text = await self.fetch(
            doppio_url, headers=self.forward_headers, timeout=REQUEST_TIMEOUT
        )
        # Extract decode key
        match = re.search(rf'{re.escape(self.pkey)}:([^"]+)', doppio_js_text)
        if not match:
            msg = f"pkey {self.pkey} not found in Doppio JS"
            logger.error(msg)
            raise web.HTTPBadRequest(reason=msg)
        decode_key = match.group(1)
        logger.info(f"Found the following decode key: {decode_key}")
        return decode_key

    async def get_key(self) -> str:
        key_storage = self.request.config_dict[APP_DECODE_KEY]
        if not key_storage.key:
            try:
                key_storage.key = await self._fetch_key()
            except Exception as e:
                raise web.HTTPBadRequest(reason=f"Cannot fetch decode key: {e}")
        return key_storage.key

    @staticmethod
    def pad_base64(s: str) -> str:
        if not s:
            return s
        return s + ("=" * ((4 - len(s) % 4) % 4))

    def decrypt_base64(self, encrypted_base64: str, key: str) -> str:
        if not encrypted_base64:
            return encrypted_base64
        data = base64.b64decode(self.pad_base64(encrypted_base64))
        hash_bytes = hashlib.sha256(key.encode()).digest()
        out = bytearray()
        for b, h in zip(data, cycle(hash_bytes)):
            out.append(b ^ h)
        return out.decode("utf-8")

    async def decode_playlist(self, m3u8_text: str) -> str:
        if "#EXT-X-MOUFLON" not in m3u8_text:
            return m3u8_text
        try:
            key = await self.get_key()
        except ClientResponseError as e:
            self.raise_client_response_error(e)
        lines = m3u8_text.splitlines()
        # invalid_decryptions = 0
        decrypt_errors = False
        for idx, line in enumerate(lines):
            if line.startswith("#EXT-X-MOUFLON:FILE:"):
                enc = line.split(":", 2)[-1].strip()
                dec = self.decrypt_base64(enc, key)
                for j in range(idx + 1, min(len(lines), idx + 6)):
                    candidate = lines[j]
                    if candidate.strip() == "":
                        continue
                    if "media.mp4" in candidate:
                        new_candidate = candidate.replace("media.mp4", dec)
                        if not MP4_REGEX.match(new_candidate):
                            logger.warning(
                                "Invalid decrypted URL. Decode key may be wrong."
                            )
                            decrypt_errors = True
                            continue  # Skip replacing this one
                        if new_candidate != candidate:
                            lines[j] = new_candidate
                        break
        if decrypt_errors:
            logger.info("Got some decryption errors. Trying to get new key")
            await self.get_key()
        return "\n".join(lines)

    @property
    def origin(self) -> URL:
        query = dict(self.request.query)
        host = query.pop("host")
        return URL.build(
            scheme="https", host=host, path=f"/{self.match_info("path")}", query=query
        )

    @cached_property
    def psch(self) -> str:
        return self.request.query.get("psch", "")

    @cached_property
    def pkey(self) -> str:
        return self.request.query.get("pkey", "")

    def update_query(self, url: URL) -> URL:
        if self.psch and "psch" not in url.query:
            url = url.update_query(psch=self.psch)
        if self.pkey and "pkey" not in url.query:
            url = url.update_query(pkey=self.pkey)
        return url

    def _inject_and_proxy(self, url: URL) -> str:
        # Check url is absolute - host is not None
        assert url.host
        chunk_route = self.request.app.router["chunk_view"]
        chunk_url = (
            chunk_route.url_for(path=url.path.lstrip("/"))
            .with_query(url.query)
            .update_query(host=url.host, pkey=self.pkey, psch=self.psch)
        )
        return str(chunk_url)

    def _rewrite_uri_attr(self, s: str) -> str:
        # m = re.search(r'URI=(?:"([^"]+)"|([^,]+))', s, flags=re.IGNORECASE)
        m = URI_REGEX_SEARCH.search(s)
        if not m:
            return s
        uri = (m.group(1) or m.group(2) or "").strip()
        if not uri:
            return s
        abs_uri = self._make_absolute(self.origin, uri)
        proxy_url = self._inject_and_proxy(abs_uri)
        return URI_REGEX_SUB.sub(f'URI="{proxy_url}"', s)

    async def get(self) -> web.StreamResponse:
        try:
            m3u8_text = await self.fetch(self.origin)
        except ClientResponseError as e:
            logger.error(f"Unable to load .m3u8: {e.message}")
            self.raise_client_response_error(e)

        text = await self.decode_playlist(m3u8_text)

        out: list[str] = []
        for line in text.splitlines():
            if line.startswith("#EXT-X-MOUFLON"):
                psch, pkey = self.extract_psch_pkey(line)
            line = line.strip()
            if not line:
                continue

            # Rewrite all attribute-URI tags including audio renditions
            if any([line.upper().startswith(t) for t in URI_TAGS]):
                out.append(self._rewrite_uri_attr(line))
                continue

            # Rewrite plain URL lines (variants or segments)
            if not line.startswith("#"):
                absu = self._make_absolute(self.origin, line)
                out.append(self._inject_and_proxy(absu))
                continue
            out.append(line)
        body = "\n".join(out) + "\n"
        return web.Response(
            text=body, headers=({"Content-Type": "application/vnd.apple.mpegurl"})
        )


class ChunkView(BaseView):
    @property
    def upstream_headers(self) -> dict:
        headers = self.forward_headers
        for hdr in S_HEADERS:
            if v := self.request.headers.get(hdr):
                headers[hdr] = v
        return headers

    async def fetch_stream(
        self, retries: int = 3
    ) -> t.AsyncGenerator[t.Mapping | bytes, None]:
        query = dict(self.request.query)
        del query["host"]
        orig = URL.build(
            scheme="https",
            host=self.request.query["host"],
            path=f"/{self.match_info('path')}",
            query={
                "pkey": self.request.query.get("pkey", ""),
                "psch": self.request.query.get("psch", ""),
            },
        )
        for try_num in range(retries + 1):
            try:
                async with self.client_session.get(
                    orig, headers=self.upstream_headers
                ) as resp:
                    yield resp.headers
                    async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                        yield chunk
                    return
            except ClientResponseError as e:
                if try_num >= retries:
                    self.raise_client_response_error(e)
            except Exception as e:
                if try_num >= retries:
                    raise web.HTTPBadRequest(reason=str(e))

    def get_proxy_response_headers(self, cl_resp_headers: CIMultiDictProxy):
        headers = {}
        for h in (
            "Content-Type",
            "Content-Length",
            "Content-Range",
            "Accept-Ranges",
            "ETag",
            "Last-Modified",
            "Cache-Control",
        ):
            if v := cl_resp_headers.get(h):
                headers[h] = v
        if te := cl_resp_headers.get("Transfer-Encoding"):
            headers["Transfer-Encoding"] = te
        if ce := cl_resp_headers.get("Content-Encoding"):
            headers["Content-Encoding"] = ce
        headers["Connection"] = "keep-alive"
        return headers

    async def get(self):
        stream = self.fetch_stream()
        client_resp_headers = t.cast(CIMultiDictProxy, await anext(stream))
        proxy_resp_headers = self.get_proxy_response_headers(client_resp_headers)

        try:
            s_resp = web.StreamResponse(headers=proxy_resp_headers)
            await s_resp.prepare(self.request)
            # first = True
            async for chunk in stream:
                # if first:
                #     if b"ftyp" in chunk or b"moov" in chunk or b"sidx" in chunk:
                #         logger.debug("Atoms seen in first chunk from %s" % orig)
                #     first = False
                await s_resp.write(t.cast(bytes, chunk))
            return s_resp
        except Exception as e:
            raise web.HTTPBadRequest(reason=str(e))


def setup_proxy(proxy_url: str):
    async def _setup_proxy(app: web.Application):
        _proxy_url = URL(proxy_url)
        logger.info(f"Proxy URL obtained: {proxy_url}")
        app[APP_PROXY_URL] = _proxy_url
        session: ClientSession | None = None
        if _proxy_url.scheme.lower().startswith("socks"):
            connector = ProxyConnector.from_url(proxy_url)
            session = ClientSession(raise_for_status=True, connector=connector)
            app[APP_SOCKS_SESSION_KEY] = session
            logger.info("SOCKS proxy client session created.")
        yield
        if session:
            await session.close()
            logger.info("SOCKS proxy client session closed.")

    return _setup_proxy


async def create_app(
    geo_bypass_proxy: str | None = None,
    mirror: str | None = None,
) -> web.Application:
    app = web.Application(middlewares=[web.normalize_path_middleware()])
    app[APP_DECODE_KEY] = KeyStorage()
    app.cleanup_ctx.append(setup_session)
    app[APP_STRIPCHAT_HOST_KEY] = mirror or DEFAULT_STRIPCHAt_HOST

    if geo_bypass_proxy:
        app.cleanup_ctx.append(setup_proxy(geo_bypass_proxy))
    app.add_routes(
        [
            web.view("/chunk/{path:.+}/", ChunkView, name="chunk_view"),
            web.view("/chunklist/{path:.+}/", PlaylistView, name="playlist"),
            web.view("/{model_name:.*}/", ModelView, name="model"),
        ]
    )
    return app


def main():
    parser = ArgumentParser()
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=randint(20000, 45000),
        help="Port to run the proxy on (default: auto)",
    )
    parser.add_argument(
        "--geo-bypass-proxy",
        type=str,
        default=None,
        help="Proxy to bypass cuntry ban by GeoIP",
    )
    parser.add_argument(
        "--stripchat-host",
        type=str,
        default=DEFAULT_STRIPCHAt_HOST,
        help="Optional mirror domain for stripchat, like 'omg.adult'",
    )
    args = parser.parse_args()
    geo_bypass_proxy: str | None = args.geo_bypass_proxy

    host = args.host
    port = args.port
    web.run_app(
        create_app(geo_bypass_proxy=geo_bypass_proxy, mirror=args.stripchat_host),
        host=host,
        port=port,
        access_log=None,
    )


if __name__ == "__main__":
    main()
