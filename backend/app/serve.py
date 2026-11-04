"""Listen on IPv4 health checks and Railway's IPv6 private network."""

import os
import socket

import uvicorn


def main():
    port = int(os.environ.get("PORT", "8000"))
    with socket.create_server(
        ("::", port), family=socket.AF_INET6, dualstack_ipv6=True
    ) as listener:
        uvicorn.run(
            "app.main:app",
            fd=listener.fileno(),
            workers=1,
            access_log=False,
            proxy_headers=False,
        )


if __name__ == "__main__":
    main()
