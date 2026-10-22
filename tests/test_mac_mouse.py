import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(sys.platform == 'darwin', 'Mac native mouse regression')
class MacMouseTests(unittest.TestCase):
    def test_click_after_move_keeps_target_even_when_os_cursor_lags(self):
        import Quartz
        from pynput import mouse
        from sharemouse.mac_mouse import MouseController
        controller = MouseController()
        events = []
        with patch('sharemouse.mac_mouse.Quartz.CGEventPost', side_effect=lambda tap, event: events.append(event)), \
             patch('sharemouse.mac_mouse.Quartz.CGEventGetLocation', return_value=SimpleNamespace(x=10, y=20)):
            controller.position = (800, 400)
            controller.button_at(mouse.Button.left, True, (800, 400))
            controller.button_at(mouse.Button.left, False, (800, 400))
        self.assertEqual(len(events), 3)
        for event in events:
            point = Quartz.CGEventGetLocation(event)
            self.assertEqual((point.x, point.y), (800, 400))
        self.assertIsNone(controller._event_position)

    def test_native_read_and_display_bounds_use_same_coordinate_space(self):
        from sharemouse.mac_mouse import MouseController
        from sharemouse.display import primary_screen
        with patch('sharemouse.mac_mouse.Quartz.CGEventGetLocation', return_value=SimpleNamespace(x=500, y=250)), \
             patch('sharemouse.mac_mouse.Quartz.CGDisplayBounds', return_value=SimpleNamespace(size=SimpleNamespace(width=1920, height=1080))), \
             patch('sharemouse.mac_mouse.Quartz.CGDisplayPixelsHigh', return_value=2160):
            self.assertEqual(MouseController().position, (500, 250))
            self.assertEqual(primary_screen().height, 1080)
