"""Camera movement from a shot's frames: push / pull, pan / tilt, roll, handheld shake, static and tracking shots.
The shots are synthetic: a moving, zooming or rotating window over a large textured picture."""

import math
import unittest
from pathlib import Path

import cv2
import numpy as np

import motion

ASTRONAUT = cv2.imread(str(Path(__file__).parent / 'data' / 'astronaut.jpg'))


def texture():
    """A large, varied picture: blocks and circles of many colours, so features can be tracked."""
    rng = np.random.default_rng(11)
    image = cv2.resize(
        rng.integers(30, 225, (50, 80, 3), dtype=np.uint8), (3200, 2000), interpolation=cv2.INTER_NEAREST
    )
    for _ in range(900):
        centre = (int(rng.integers(0, 3200)), int(rng.integers(0, 2000)))
        colour = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.circle(image, centre, int(rng.integers(6, 40)), colour, -1)
    return image


BIG = texture()
JITTER = np.random.default_rng(3).normal(0, 1, (60, 2))


def view(cx, cy, width, angle=0.0, size=(640, 360)):
    matrix = cv2.getRotationMatrix2D((cx, cy), angle, size[0] / width)
    matrix[0, 2] += size[0] / 2 - cx
    matrix[1, 2] += size[1] / 2 - cy
    return cv2.warpAffine(BIG, matrix, size, flags=cv2.INTER_AREA)


def run(frame_at, count=48, fps=24, people=None):
    frames = [frame_at(i / (count - 1), i) for i in range(count)]
    return motion.analyze([motion.small_gray(f) for f in frames], [i / fps for i in range(count)], people)


class CameraMovementTests(unittest.TestCase):
    def test_static(self):
        result = run(lambda t, i: view(1600, 1000, 1200))
        self.assertEqual((result['label'], result['text'], result['terms']), ('固定', '固定机位', ['static shot']))

    def test_pan_right_and_left(self):
        right = run(lambda t, i: view(1300 + t * 500, 1000, 1200))
        self.assertEqual(right['label'], '右摇/移')
        self.assertIn('向右摇（或横移）', right['text'])
        self.assertAlmostEqual(right['moves']['pan'], -500 / 1200, delta=0.03)
        left = run(lambda t, i: view(2100 - t * 1200, 1000, 1200))
        self.assertEqual(left['label'], '左摇/移')
        self.assertTrue(left['text'].startswith('快速'), left['text'])
        self.assertNotIn('手持', left['text'], 'a smooth fast pan is not shaky')

    def test_tilt_up_and_down(self):
        self.assertEqual(run(lambda t, i: view(1600, 1200 - t * 350, 1200))['label'], '上摇/升')
        self.assertEqual(run(lambda t, i: view(1600, 800 + t * 350, 1200))['label'], '下摇/降')

    def test_push_in_and_pull_out(self):
        push = run(lambda t, i: view(1600, 1000, 1400 - t * 400))
        self.assertEqual(push['label'], '推近')
        self.assertAlmostEqual(push['moves']['zoom'], 1.4, delta=0.05)
        self.assertEqual(push['terms'], ['steady push in'])
        self.assertEqual(run(lambda t, i: view(1600, 1000, 1000 + t * 400))['label'], '拉远')

    def test_slow_push_is_called_slow(self):
        push = run(lambda t, i: view(1600, 1000, 1200 - t * 100), count=96)
        self.assertTrue(push['text'].startswith('缓慢推近'), push['text'])

    def test_roll(self):
        result = run(lambda t, i: view(1600, 1000, 1200, angle=t * 8))
        self.assertEqual(result['label'], '旋转')
        self.assertAlmostEqual(abs(result['moves']['roll']), 8, delta=1)

    def test_handheld(self):
        shaky = run(lambda t, i: view(1600 + JITTER[i][0] * 6, 1000 + JITTER[i][1] * 6, 1200))
        self.assertEqual(shaky['label'], '手持')
        self.assertIn('晃动', shaky['text'])
        pan = run(lambda t, i: view(1300 + t * 500 + JITTER[i][0] * 6, 1000 + JITTER[i][1] * 6, 1200))
        self.assertEqual(pan['label'], '右摇/移')
        self.assertIn('手持', pan['text'])
        self.assertIn('handheld', pan['terms'])

    def test_push_while_panning(self):
        result = run(lambda t, i: view(1300 + t * 400, 1000, 1400 - t * 300))
        self.assertEqual(result['label'], '推近 · 右摇/移')

    def test_tracking_a_person(self):
        subject = cv2.resize(ASTRONAUT, (220, 220))

        def frame(t, i):
            image = view(1100 + t * 900, 1000, 1200)
            image[100:320, 210:430] = subject  # the person stays put while the background slides
            return image

        box = [210 / 640, 100 / 360, 220 / 640, 220 / 360]
        result = run(frame, people=(box, box))
        self.assertEqual(result['label'], '跟拍')
        self.assertIn('跟拍', result['text'])
        self.assertTrue(result['moves']['follow'])
        moved = [box[0] + 0.4, box[1], box[2], box[3]]
        self.assertNotEqual(run(frame, people=(box, moved))['label'], '跟拍')

    def test_person_walking_past_a_fixed_camera(self):
        subject = cv2.resize(ASTRONAUT, (200, 200))

        def frame(t, i):
            image = view(1600, 1000, 1200)
            x = int(20 + t * 400)
            image[120:320, x : x + 200] = subject
            return image

        first, last = [20 / 640, 120 / 360, 200 / 640, 200 / 360], [420 / 640, 120 / 360, 200 / 640, 200 / 360]
        self.assertEqual(run(frame, people=(first, last))['label'], '固定')

    def test_too_short_or_featureless(self):
        self.assertIn('太短', run(lambda t, i: view(1600, 1000, 1200), count=4)['text'])
        flat = motion.analyze([np.full((180, 320), 90, np.uint8)] * 10, [i / 24 for i in range(10)])
        self.assertEqual(flat['label'], '')
        self.assertIn('无法判断', flat['text'])

    def test_samples_are_spread_over_the_shot(self):
        self.assertEqual(motion.sample_frames(5, 5), [5])
        self.assertEqual(motion.sample_frames(0, 9), list(range(10)))
        frames = motion.sample_frames(100, 1099)
        self.assertLessEqual(len(frames), motion.MAX_SAMPLES + 1)
        self.assertEqual((frames[0], frames[-1]), (100, 1099))
        self.assertTrue(all(math.isfinite(f) for f in frames))


if __name__ == '__main__':
    unittest.main()
