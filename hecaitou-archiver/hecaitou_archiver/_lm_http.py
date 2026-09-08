"""One bounded Ark HTTPS request. The key is read from txt, never argv or output."""
import http.client
import json
import ssl
import sys
import urllib.error
import urllib.request
import certifi
try:
    from .credentials import read_api_key
except ImportError:  # Executed as the private subprocess script.
    from credentials import read_api_key


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    key = ""
    try:
        config = json.load(sys.stdin)
        if config["url"] != "https://ark.cn-beijing.volces.com/api/v3/chat/completions":
            raise ValueError("仅允许指定的火山方舟 HTTPS 接口")
        key = read_api_key(config["key_file"])
        body = config.get("body")
        request = urllib.request.Request(
            config["url"],
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json",
                     "Authorization": "Bearer " + key},
        )
        # Do not forward credentials to redirects. Certificate validation remains enabled.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())))
        with opener.open(request, timeout=config["timeout"]) as response:
            payload = response.read(2 * 1024 * 1024 + 1)
            if len(payload) > 2 * 1024 * 1024:
                raise ValueError("模型响应超过 2 MiB")
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != len(payload):
                raise http.client.IncompleteRead(payload)
        sys.stdout.buffer.write(payload)
        return 0
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        if key:
            detail = detail.replace(key, "[REDACTED]")
        print(json.dumps({"http_status": exc.code, "message": detail or str(exc)}), file=sys.stderr)
        return 1
    except (OSError, ValueError, http.client.HTTPException) as exc:
        message = f"{type(exc).__name__}: {exc}"
        if key:
            message = message.replace(key, "[REDACTED]")
        print(json.dumps({"http_status": None, "message": message}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
