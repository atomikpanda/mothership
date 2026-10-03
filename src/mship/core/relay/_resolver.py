"""One native address lookup, isolated so its caller can cancel and reap it.

Private stdin/stdout exchange; no relay credentials, HTTP or DNS wire handling.
Invoked as a script to avoid importing the application during resolver startup.
"""

import json
import socket
import sys


def main():
    args = json.load(sys.stdin)
    try:
        reply = {"addresses": socket.getaddrinfo(*args)}
    except socket.gaierror as error:
        reply = {"error": [error.errno, error.strerror]}
    json.dump(reply, sys.stdout)


if __name__ == "__main__":
    main()
