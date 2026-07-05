import unittest

from mini_tmux import Pane, choose_neighbor, compute_rects, leaf, pane_frame, pane_order, remove_from_layout, split_layout


class LayoutTests(unittest.TestCase):
    def test_split_layout_and_order(self):
        layout, changed = split_layout(leaf(1), 1, "h", 2)

        self.assertTrue(changed)
        self.assertEqual(pane_order(layout), [1, 2])

    def test_compute_horizontal_rects(self):
        layout, _ = split_layout(leaf(1), 1, "h", 2)

        rects = compute_rects(layout, 0, 0, 80, 24)

        self.assertEqual(rects[1], (0, 0, 40, 24))
        self.assertEqual(rects[2], (40, 0, 40, 24))

    def test_compute_vertical_rects(self):
        layout, _ = split_layout(leaf(1), 1, "v", 2)

        rects = compute_rects(layout, 0, 0, 80, 24)

        self.assertEqual(rects[1], (0, 0, 80, 12))
        self.assertEqual(rects[2], (0, 12, 80, 12))

    def test_remove_collapses_parent(self):
        layout, _ = split_layout(leaf(1), 1, "h", 2)
        layout, removed = remove_from_layout(layout, 1)

        self.assertTrue(removed)
        self.assertEqual(layout, leaf(2))

    def test_choose_neighbor(self):
        layout, _ = split_layout(leaf(1), 1, "h", 2)
        rects = compute_rects(layout, 0, 0, 80, 24)

        self.assertEqual(choose_neighbor(rects, 1, "right"), 2)
        self.assertEqual(choose_neighbor(rects, 2, "left"), 1)
        self.assertIsNone(choose_neighbor(rects, 1, "left"))

    def test_pane_frame_uses_internal_border_only(self):
        left = pane_frame((0, 0, 40, 24), 80, 24)
        right = pane_frame((40, 0, 40, 24), 80, 24)

        self.assertEqual(left[4:], (0, 0, 40, 24))
        self.assertEqual(right[4:], (41, 0, 39, 24))

    def test_single_pane_frame_uses_full_area_without_border(self):
        frame = pane_frame((0, 0, 80, 23), 80, 23, framed=False)

        self.assertEqual(frame, (0, 0, 79, 22, 0, 0, 80, 23))

    def test_pane_feed_keeps_crlf_lines(self):
        pane = Pane(1, -1, -1, "sh")

        pane.feed(b"echo hello\r\nhello\r\n")
        lines, _ = pane.view(24, 80)

        self.assertTrue(any("echo hello" in line for line in lines))
        self.assertTrue(any("hello" in line for line in lines))

    def test_pane_feed_handles_cursor_addressing(self):
        pane = Pane(1, -1, -1, "sh")

        pane.feed(b"\x1b[2J\x1b[3;5Hvim")
        lines, cursor = pane.view(10, 20)

        self.assertEqual(lines[2][4:7], "vim")
        self.assertEqual(cursor, (7, 2))

    def test_pane_feed_uses_alternate_screen(self):
        pane = Pane(1, -1, -1, "sh")

        pane.feed(b"shell\r\n\x1b[?1049hvim\x1b[?1049l")
        lines, _ = pane.view(10, 20)

        self.assertTrue(any("shell" in line for line in lines))
        self.assertFalse(any("vim" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
