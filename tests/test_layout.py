import unittest

from mini_tmux import Pane, choose_neighbor, compute_rects, leaf, pane_order, remove_from_layout, split_layout


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

    def test_pane_feed_keeps_crlf_lines(self):
        pane = Pane(1, -1, -1, "sh")

        pane.feed(b"echo hello\r\nhello\r\n")

        self.assertIn("echo hello", pane.lines)
        self.assertIn("hello", pane.lines)


if __name__ == "__main__":
    unittest.main()
