#!/usr/bin/env python3
import socket
import sys

target = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
server = sys.argv[2] if len(sys.argv) > 2 else "192.168.1.10"
port = int(sys.argv[3]) if len(sys.argv) > 3 else 18899
message = f"set>server={server}:{port};".encode("ascii")

with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    sock.settimeout(5)
    sock.sendto(message, (target, 58899))
    print("sent", message.decode(), "to", f"{target}:58899")
    try:
        data, peer = sock.recvfrom(2048)
        print("reply", peer, data.decode("ascii", errors="replace"), data.hex())
    except TimeoutError:
        print("no UDP acknowledgement")

