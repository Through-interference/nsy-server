"""A closed viewer's TCP TIME_WAIT must not block a valid immediate restart."""
import socket
import unittest
from nsy_server.ground_station import parser, load_drone, port_issues

class RestartTests(unittest.TestCase):
    def test_closed_http_connection_allows_immediate_restart(self):
        with socket.socket() as listener, socket.socket() as client:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
            listener.listen()
            client.connect(('127.0.0.1', port))
            connection, _ = listener.accept()
            connection.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(1), b'')
            connection.close()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            udp.bind(('127.0.0.1', 0))
            video = udp.getsockname()[1]
        args = parser().parse_args(['--mode', 'udp', '--http-port', str(port), '--video-port', str(video)])
        self.assertEqual(port_issues(args, load_drone(args)), [])

    def test_live_http_listener_still_rejected(self):
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', 0))
            listener.listen()
            args = parser().parse_args(['--mode', 'udp', '--http-port', str(listener.getsockname()[1])])
            self.assertTrue(any('HTTP' in issue for issue in port_issues(args, load_drone(args))))
