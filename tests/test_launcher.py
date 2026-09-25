import errno
import unittest
from unittest.mock import patch

from run_server import available_port


class LauncherTests(unittest.TestCase):
    def test_uses_next_port_when_default_is_occupied(self):
        attempts = []

        class Probe:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def bind(self, address):
                attempts.append(address[1])
                if address[1] == 8000:
                    raise OSError(errno.EADDRINUSE, 'busy')

        with patch('run_server.socket.socket', return_value=Probe()):
            self.assertEqual(available_port(), 8001)
        self.assertEqual(attempts, [8000, 8001])


if __name__ == '__main__':
    unittest.main()
