#!/usr/bin/env python3
"""Synchronize qBittorrent with Proton's forwarded port and NAT-PMP address.

Proton's inbound address can differ from its outbound HTTP address. Gluetun
owns the port lease; this worker only queries the gateway's external address.
It must run in Gluetun's network namespace, alongside qBittorrent.
"""

import argparse
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import socket
import struct
import time
import urllib.parse
import urllib.request


SUCCESS_PATH = Path('/tmp/qb-port-sync.success')


def read_port(path):
    raw = path.read_text().strip()
    if not raw.isascii() or not raw.isdecimal() or not 1 <= int(raw) <= 65535:
        raise ValueError('Invalid forwarded port')
    return int(raw)


def parse_external_address(data):
    if len(data) != 12:
        raise ValueError('Invalid NAT-PMP response length')
    version, opcode, result, _, packed = struct.unpack('!BBHI4s', data)
    if (version, opcode, result) != (0, 128, 0):
        raise ValueError('NAT-PMP external-address query failed')
    address = ipaddress.IPv4Address(packed)
    if not address.is_global or address.is_multicast:
        raise ValueError('NAT-PMP did not return a public unicast address')
    return str(address)


def external_address(gateway):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(5)
        # A connected UDP socket accepts responses only from this gateway.
        sock.connect((gateway, 5351))
        sock.send(b'\x00\x00')
        return parse_external_address(sock.recv(64))


class QBittorrent:
    def __init__(self, base_url, username, password):
        self.base_url = base_url.rstrip('/')
        self.username = username
        self.password = password
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()),
        )

    def request(self, path, data=None):
        request = urllib.request.Request(
            self.base_url + '/api/v2/' + path,
            data=None if data is None else urllib.parse.urlencode(data).encode(),
            headers={'Referer': self.base_url + '/', 'Origin': self.base_url},
        )
        with self.opener.open(request, timeout=15) as response:
            return response.read().decode()

    def login(self):
        result = self.request('auth/login', {'username': self.username, 'password': self.password})
        # qBittorrent 5.2 returns an empty successful response when localhost
        # authentication is bypassed. The subsequent preferences GET still
        # verifies access before any settings or success marker are changed.
        if result.strip() not in ('Ok.', ''):
            raise RuntimeError('qBittorrent login failed')

    def preferences(self):
        return json.loads(self.request('app/preferences'))

    def set_preferences(self, preferences):
        self.request('app/setPreferences', {'json': json.dumps(preferences)})


def synchronize(qb, port_file, gateway):
    port = read_port(port_file)
    address = external_address(gateway)
    if read_port(port_file) != port:
        raise RuntimeError('Forwarded port changed during gateway query')
    expected = {'listen_port': port, 'announce_ip': address}
    current = qb.preferences()
    changed = any(current.get(key) != value for key, value in expected.items())
    if changed:
        qb.set_preferences(expected)
        current = qb.preferences()
    if any(current.get(key) != value for key, value in expected.items()):
        raise RuntimeError('qBittorrent did not retain forwarded endpoint settings')
    if read_port(port_file) != port:
        raise RuntimeError('Forwarded port changed during qBittorrent update')
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--health', action='store_true')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if args.health:
        try:
            age = time.time() - SUCCESS_PATH.stat().st_mtime
            return 0 if 0 <= age < 180 else 1
        except OSError:
            return 1

    port_file = Path(os.environ.get('PORT_FORWARDED', '/tmp/gluetun/forwarded_port'))
    gateway = os.environ.get('NAT_PMP_GATEWAY', '10.2.0.1')
    interval = max(5, int(os.environ.get('RECHECK_TIME', '60')))
    host = os.environ.get('QBITTORRENT_SERVER', '127.0.0.1')
    port = int(os.environ.get('QBITTORRENT_PORT', '8080'))
    qb = QBittorrent(f'http://{host}:{port}',
                     os.environ['QBITTORRENT_USERNAME'], os.environ['QBITTORRENT_PASSWORD'])
    verified = False
    while True:
        try:
            qb.login()
            changed = synchronize(qb, port_file, gateway)
            SUCCESS_PATH.touch()
            if changed or not verified:
                print('Forwarded port and NAT-PMP announce address verified'
                      + (' (updated)' if changed else ''), flush=True)
            verified = True
            if args.once:
                return 0
        except Exception as exc:
            # Never log HTTP bodies, URLs, credentials, or cookie contents.
            print(f'Forwarded endpoint synchronization failed: {type(exc).__name__}', flush=True)
            verified = False
            if args.once:
                return 1
        time.sleep(interval)


if __name__ == '__main__':
    raise SystemExit(main())
