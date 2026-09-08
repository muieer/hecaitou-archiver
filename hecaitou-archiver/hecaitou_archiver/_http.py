"""Private one-request worker. The parent process enforces a hard wall-clock timeout."""
import http.client
import ssl
import sys
import urllib.error
import urllib.request

import certifi


MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def main():
    try:
        url, timeout = sys.argv[1], float(sys.argv[2])
        request = urllib.request.Request(url, headers={
            "User-Agent": "HecaitouLocalArchiver/1.0 (+local personal archive)",
            "Accept": "application/json,text/html;q=0.9",
            "Cache-Control": "no-cache",
        })
        with urllib.request.urlopen(request, timeout=timeout,
                                    context=ssl.create_default_context(cafile=certifi.where())) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                print("响应超过 8 MiB，停止读取", file=sys.stderr)
                return 3
            length = response.headers.get("Content-Length")
            if length is not None and len(payload) != int(length):
                raise http.client.IncompleteRead(payload)
        sys.stdout.buffer.write(payload)
        return 0
    except (OSError, urllib.error.URLError, http.client.HTTPException, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
