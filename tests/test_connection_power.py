"""Windows power API ownership/ABI; never changes the host power state."""
import ctypes
import unittest
from unittest.mock import Mock

from sharemouse.connection_power import ConnectionPower, ReasonContext


class ConnectionPowerTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.api = Mock()
        self.api.PowerCreateRequest.side_effect = iter(range(100, 200))
        self.api.PowerSetRequest.return_value = 1
        self.power = ConnectionPower(self.api, lambda: self.now)
        self.addCleanup(self.power.close)

    def test_display_may_turn_off_without_incrementing_requests_each_tick(self):
        for _ in range(100):
            self.power.set_active(True)
        self.assertEqual(self.api.PowerCreateRequest.call_count, 1)
        self.assertEqual([c.args for c in self.api.PowerSetRequest.call_args_list],
                         [(100, 1), (100, 3)])

    def test_disconnect_releases_each_owned_request_once(self):
        self.power.set_active(True)
        self.power.set_active(False)
        self.power.set_active(False)
        self.assertEqual([c.args for c in self.api.PowerClearRequest.call_args_list],
                         [(100, 1), (100, 3)])
        self.api.CloseHandle.assert_called_once_with(100)

    def test_resume_refresh_releases_old_handle_and_reasserts(self):
        self.power.set_active(True)
        self.now = 1000
        self.power.set_active(True)
        self.api.CloseHandle.assert_called_once_with(100)
        self.assertEqual(self.power.handle, 101)
        self.assertEqual(self.api.PowerSetRequest.call_count, 4)

    def test_partial_support_releases_only_successful_requests(self):
        self.api.PowerSetRequest.side_effect = [1, 0]
        self.power.set_active(True)
        self.power.close()
        self.api.PowerClearRequest.assert_called_once_with(100, 1)

    def test_failed_creation_does_not_busy_loop_or_leak(self):
        self.api.PowerCreateRequest.side_effect = None
        self.api.PowerCreateRequest.return_value = ctypes.c_void_p(-1).value
        for _ in range(100):
            self.power.set_active(True)
        self.assertIsNotNone(self.power.last_error)
        self.assertEqual(self.api.PowerCreateRequest.call_count, 1)
        self.api.CloseHandle.assert_not_called()

    def test_all_failed_requests_close_handle(self):
        self.api.PowerSetRequest.return_value = 0
        self.power.set_active(True)
        self.assertIsNone(self.power.handle)
        self.api.CloseHandle.assert_called_once_with(100)

    def test_reason_structure_has_windows_x64_layout(self):
        self.assertEqual(ReasonContext.reason.offset, 8)
        if ctypes.sizeof(ctypes.c_void_p) == 8:
            self.assertEqual(ctypes.sizeof(ReasonContext), 32)


if __name__ == '__main__':
    unittest.main()
