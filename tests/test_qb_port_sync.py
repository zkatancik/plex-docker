import importlib.util
from pathlib import Path
import socket
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('qb_port_sync', Path(__file__).parents[1] / 'scripts/qb-port-sync.py')
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class PortSyncTests(unittest.TestCase):
    def test_login_accepts_localhost_bypass_but_rejects_explicit_failure(self):
        qb = worker.QBittorrent('http://localhost:8080', 'user', 'password')
        with patch.object(qb, 'request', return_value=''):
            qb.login()
        with patch.object(qb, 'request', return_value='Fails.'):
            with self.assertRaises(RuntimeError):
                qb.login()

    def test_same_port_still_repairs_wrong_advertised_address(self):
        with tempfile.TemporaryDirectory() as directory:
            port = Path(directory) / 'port'
            port.write_text('63654\n')
            qb = Mock()
            qb.preferences.side_effect = [
                {'listen_port': 63654, 'announce_ip': ''},
                {'listen_port': 63654, 'announce_ip': '8.8.4.4'},
            ]
            with patch.object(worker, 'external_address', return_value='8.8.4.4'):
                self.assertTrue(worker.synchronize(qb, port, '10.2.0.1'))
            qb.set_preferences.assert_called_once_with({'listen_port': 63654, 'announce_ip': '8.8.4.4'})

    def test_verified_settings_are_not_rewritten_each_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            port = Path(directory) / 'port'
            port.write_text('63654')
            qb = Mock()
            qb.preferences.return_value = {'listen_port': 63654, 'announce_ip': '8.8.4.4'}
            with patch.object(worker, 'external_address', return_value='8.8.4.4'):
                self.assertFalse(worker.synchronize(qb, port, '10.2.0.1'))
            qb.set_preferences.assert_not_called()

    def test_ignored_api_update_is_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            port = Path(directory) / 'port'
            port.write_text('63654')
            qb = Mock()
            qb.preferences.return_value = {'listen_port': 1, 'announce_ip': ''}
            with patch.object(worker, 'external_address', return_value='8.8.4.4'):
                with self.assertRaises(RuntimeError):
                    worker.synchronize(qb, port, '10.2.0.1')

    def test_missing_gateway_response_never_changes_qbit(self):
        with tempfile.TemporaryDirectory() as directory:
            port = Path(directory) / 'port'
            port.write_text('63654')
            qb = Mock()
            with patch.object(worker, 'external_address', side_effect=TimeoutError):
                with self.assertRaises(TimeoutError):
                    worker.synchronize(qb, port, '10.2.0.1')
            qb.set_preferences.assert_not_called()

    def test_changing_forwarded_port_defers_update(self):
        with tempfile.TemporaryDirectory() as directory:
            port = Path(directory) / 'port'
            port.write_text('63654')
            qb = Mock()
            def lookup(_):
                port.write_text('63655')
                return '8.8.4.4'
            with patch.object(worker, 'external_address', side_effect=lookup):
                with self.assertRaises(RuntimeError):
                    worker.synchronize(qb, port, '10.2.0.1')
            qb.set_preferences.assert_not_called()

    def test_invalid_ports_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            port = Path(directory) / 'port'
            for raw in ['', '0', '65536', '-1', '123 456']:
                with self.subTest(raw=raw):
                    port.write_text(raw)
                    with self.assertRaises(ValueError):
                        worker.read_port(port)

    def test_natpmp_response_requires_successful_public_ipv4(self):
        valid = struct.pack('!BBHI4s', 0, 128, 0, 123, socket.inet_aton('8.8.4.4'))
        self.assertEqual(worker.parse_external_address(valid), '8.8.4.4')
        for invalid in [valid[:-1], valid + b'\0', bytes([1]) + valid[1:], valid[:1] + bytes([129]) + valid[2:],
                        struct.pack('!BBHI4s', 0, 128, 2, 123, socket.inet_aton('8.8.4.4')),
                        struct.pack('!BBHI4s', 0, 128, 0, 123, socket.inet_aton('192.168.1.1'))]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    worker.parse_external_address(invalid)

    def test_gateway_probe_only_queries_and_does_not_allocate_a_mapping(self):
        sock = Mock()
        sock.recv.return_value = struct.pack('!BBHI4s', 0, 128, 0, 123, socket.inet_aton('8.8.4.4'))
        with patch.object(worker.socket, 'socket') as factory:
            factory.return_value.__enter__.return_value = sock
            self.assertEqual(worker.external_address('10.2.0.1'), '8.8.4.4')
        sock.connect.assert_called_once_with(('10.2.0.1', 5351))
        sock.send.assert_called_once_with(b'\0\0')


if __name__ == '__main__':
    unittest.main()
