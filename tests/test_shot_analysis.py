"""Shot language of a keyframe: people, shot size, composition, level and depth of field."""

import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

import frame_analysis as fa
import shot_analysis as sa

# NASA portrait (public domain), 320×320, chest-up.
ASTRONAUT = cv2.imread(str(Path(__file__).parent / 'data' / 'astronaut.jpg'))


def canvas(bgr=(200, 160, 90), size=(720, 1280)):
    image = np.zeros((*size, 3), np.uint8)
    image[:] = bgr
    return image


def textured(size=(720, 1280), seed=1):
    rng = np.random.default_rng(seed)
    noise = rng.integers(40, 215, (size[0] // 8, size[1] // 8, 3), dtype=np.uint8)
    return cv2.resize(noise, (size[1], size[0]), interpolation=cv2.INTER_NEAREST)


class ShotSizeTests(unittest.TestCase):
    def test_face_height_to_shot_size(self):
        cases = [(0.9, '大特写'), (0.5, '特写'), (0.25, '近景'), (0.15, '中景'), (0.07, '全景'), (0.02, '远景')]
        for height, label in cases:
            self.assertEqual(sa.shot_from_face(height), label, height)

    def test_real_portrait_crops_and_placements(self):
        self.assertEqual(fa.analyze(ASTRONAUT)['shot']['label'], '近景')
        close = cv2.resize(ASTRONAUT[25:165, 75:215], (640, 640))
        self.assertEqual(fa.analyze(close)['shot']['label'], '特写')
        wide = canvas((150, 160, 140), (1080, 1920))
        wide[500:800, 1200:1500] = cv2.resize(ASTRONAUT, (300, 300))
        result = fa.analyze(wide)
        self.assertEqual(result['shot']['label'], '全景')
        self.assertEqual(result['shot']['confidence'], '较准')
        self.assertIn('人脸', result['shot']['basis'])
        # The face box is reported relative to the picture, for the overlay.
        x, y, w, h = result['people']['faces'][0]['box']
        self.assertTrue(0.6 < x < 0.75 and 0.45 < y < 0.6, (x, y))

    def test_no_person_no_guess(self):
        result = fa.analyze(textured())
        self.assertEqual(result['shot']['label'], '')
        self.assertIn('未检测到人物', result['shot']['basis'])
        self.assertNotIn('', result['tags'])

    def test_missing_models_leave_the_rest_working(self):
        with patch.object(sa, 'FACE_MODEL', Path('missing.onnx')), patch.object(sa, 'PERSON_MODEL', Path('no.onnx')):
            result = fa.analyze(ASTRONAUT)
        self.assertEqual(result['shot']['label'], '')
        self.assertIn('不可用', result['shot']['basis'])
        self.assertTrue(result['color']['palette'])


class CompositionTests(unittest.TestCase):
    def ball(self, x, y):
        image = canvas()
        cv2.circle(image, (x, y), 60, (30, 30, 200), -1)
        return fa.analyze(image)['composition']

    def test_subject_on_a_third_line(self):
        result = self.ball(427, 360)
        self.assertEqual(result['label'], '三分法构图')
        self.assertIn('水平左三分线', result['position'])
        self.assertEqual(result['space'], '右侧留白')
        self.assertIn('显著区域', result['basis'])

    def test_centred_and_symmetric(self):
        self.assertIn(self.ball(640, 360)['label'], ('对称构图', '中心构图'))
        self.assertEqual(self.ball(640, 360)['position'].split(' · ')[0], '水平居中')

    def test_off_to_the_side(self):
        self.assertEqual(self.ball(1150, 360)['label'], '偏侧构图')

    def test_uniform_texture_has_no_subject(self):
        result = fa.analyze(textured())['composition']
        self.assertEqual(result['position'], '')
        self.assertIn('没有明显主体', result['basis'])

    def test_a_face_is_the_subject(self):
        result = fa.analyze(ASTRONAUT)
        self.assertEqual(result['subject']['source'], '人脸')
        self.assertIn('人脸', result['composition']['basis'])


class LevelAndDepthTests(unittest.TestCase):
    def test_tilted_horizon(self):
        image = canvas()
        cv2.fillPoly(image, [np.array([[0, 380], [1280, 480], [1280, 720], [0, 720]], np.int32)], (60, 110, 60))
        result = fa.analyze(image)['level']
        self.assertAlmostEqual(result['angle'], 4.5, delta=0.5)
        self.assertTrue(result['label'].startswith('轻微倾斜'))
        self.assertIn('右低左高', result['label'])
        self.assertEqual(len(result['line']), 4)

    def test_level_horizon_and_no_lines(self):
        image = canvas()
        image[400:] = (60, 110, 60)
        self.assertEqual(fa.analyze(image)['level']['label'], '水平')
        self.assertEqual(fa.analyze(canvas())['level']['label'], '无法判断')

    def test_dutch_angle(self):
        image = canvas()
        cv2.fillPoly(image, [np.array([[0, 250], [1280, 550], [1280, 720], [0, 720]], np.int32)], (60, 110, 60))
        self.assertIn('荷兰角', fa.analyze(image)['level']['label'])

    def test_blurred_background_behind_a_sharp_subject(self):
        image = cv2.GaussianBlur(textured(), (0, 0), 8)
        image[200:520, 500:780] = textured((320, 280), seed=2)
        cv2.circle(image, (640, 360), 40, (20, 20, 220), -1)
        self.assertEqual(fa.analyze(image)['depth']['label'], '浅景深')

    def test_everything_sharp_or_everything_soft(self):
        image = textured()
        image[200:520, 500:780] = textured((320, 280), seed=2)
        cv2.circle(image, (640, 360), 40, (20, 20, 220), -1)
        self.assertNotEqual(fa.analyze(image)['depth']['label'], '浅景深')
        self.assertEqual(fa.analyze(cv2.GaussianBlur(image, (0, 0), 10))['depth']['label'], '整体偏糊')


class TagTests(unittest.TestCase):
    def test_shot_and_composition_come_first(self):
        tags = fa.analyze(ASTRONAUT)['tags']
        self.assertEqual(tags[0], '近景')
        self.assertLessEqual(len(tags), 4)


if __name__ == '__main__':
    unittest.main()


class CalibrationRoundOneTests(unittest.TestCase):
    """Cases found on real music-video frames: a dark casino room with two people."""

    def test_dark_vignette_is_not_a_letterbox(self):
        image = textured()
        # Edges fade to near-black with texture left in them, as in a dark room shot.
        fade = np.minimum.outer(
            np.minimum(np.arange(720), np.arange(720)[::-1]), np.minimum(np.arange(1280), np.arange(1280)[::-1])
        )
        weight = np.clip(fade / 140.0, 0.04, 1.0)[..., None]
        dark = (image.astype(np.float32) * weight * 0.35).astype(np.uint8)
        self.assertFalse(fa.analyze(dark)['frame']['letterbox'])

    def test_pillarbox_in_video_black_with_noise(self):
        image = np.full((1080, 1920, 3), 16, np.uint8)
        image[:, 240:1680] = textured((1080, 1440))
        noise = np.random.default_rng(0).integers(-2, 3, image.shape)
        image = np.clip(image.astype(int) + noise, 0, 255).astype(np.uint8)
        frame = fa.analyze(image)['frame']
        self.assertEqual(frame['aspect'], '4:3')
        self.assertGreater(frame['bars']['left'], 200)

    def two_people(self, xs, sizes):
        image = canvas((60, 70, 80), (1080, 1920))
        for x, size in zip(xs, sizes):
            image[900 - size : 900, x : x + size] = cv2.resize(ASTRONAUT, (size, size))
        return fa.analyze(image)

    def test_two_people_on_either_side_are_balanced(self):
        result = self.two_people([250, 1370], [300, 300])
        self.assertEqual(result['subject']['source'], '多人')
        self.assertEqual(result['composition']['label'], '左右平衡构图')
        self.assertEqual(result['composition']['space'], '')
        self.assertIn('两侧', result['composition']['basis'])

    def test_centre_person_with_people_either_side(self):
        result = self.two_people([150, 810, 1470], [300, 300, 300])
        self.assertEqual(result['composition']['label'], '对称构图')
        self.assertTrue(result['composition']['position'].startswith('3 人'))

    def test_dark_background_is_not_called_shallow(self):
        image = canvas((12, 12, 12))
        image[180:540, 480:800] = textured((360, 320), seed=3)
        depth = fa.analyze(image)['depth']
        self.assertNotEqual(depth['label'], '浅景深')

    def test_low_key_with_a_lamp_is_high_contrast(self):
        image = (textured() * 0.25).astype(np.uint8)
        cv2.circle(image, (640, 120), 40, (250, 250, 250), -1)
        tone = fa.analyze(image)['tone']
        self.assertEqual((tone['key'], tone['contrast']), ('低调', '高对比'))
        self.assertIn('强光', tone['note'])

    def test_small_green_table_in_a_brown_room_is_an_accent(self):
        image = canvas((40, 60, 95))  # warm brown
        image[500:580, 500:780] = (80, 105, 45)  # dark green, about 2.4% of the frame
        accent = fa.analyze(image)['color']['accent']
        self.assertIsNotNone(accent)
        self.assertEqual(accent['name'][-1], '绿')

    def test_colour_names_follow_lab_hues(self):
        def name(rgb):
            return fa.analyze(canvas(rgb[::-1]))['color']['palette'][0]['name']

        self.assertEqual(name((40, 80, 230)), '蓝')
        self.assertEqual(name((230, 40, 40)), '红')
        self.assertEqual(name((40, 180, 70)), '绿')


class CalibrationRoundTwoTests(unittest.TestCase):
    """Cases from five real music-video frames: close-ups, a small centred singer, neon at night."""

    def test_chest_up_is_a_close_shot_not_a_close_up(self):
        self.assertEqual(sa.shot_from_face(0.45), '近景')
        self.assertEqual(sa.shot_from_face(0.55), '特写')

    def test_small_faces_are_found_on_the_sharper_picture(self):
        image = canvas((60, 70, 80), (1080, 1920))
        image[600:720, 900:1020] = cv2.resize(ASTRONAUT, (120, 120))  # face about 25 px high on 640 px
        faces = fa.analyze(image)['people']['faces']
        self.assertEqual(len(faces), 1)
        self.assertAlmostEqual(faces[0]['box'][0] + faces[0]['box'][2] / 2, 0.5, delta=0.05)

    def test_black_night_does_not_dilute_saturation(self):
        image = canvas((8, 8, 8))
        image[200:360, 300:980] = (180, 40, 230)  # neon magenta, 12% of the frame
        self.assertNotIn(fa.analyze(image)['color']['saturation']['label'], ('低饱和', '接近黑白'))

    def test_warm_skin_in_a_cool_scene_is_a_warm_cool_contrast(self):
        image = canvas((90, 70, 20))  # dark teal
        image[250:420, 560:720] = (120, 150, 200)  # warm skin tone, about 3%
        result = fa.analyze(image)['color']
        self.assertEqual(result['temperature']['label'], '冷调')
        self.assertEqual(result['harmony']['label'], '冷暖对比')

    def test_blurred_bright_bokeh_behind_a_sharp_face_is_shallow(self):
        rng = np.random.default_rng(5)
        image = canvas((30, 20, 40))
        for _ in range(40):  # bright neon lights, then heavily blurred
            x, y = int(rng.integers(0, 1280)), int(rng.integers(0, 720))
            cv2.circle(image, (x, y), int(rng.integers(20, 60)), (int(rng.integers(150, 255)), 80, 230), -1)
        image = cv2.GaussianBlur(image, (0, 0), 12)
        image[150:650, 440:840] = cv2.resize(ASTRONAUT, (400, 500))
        result = fa.analyze(image)
        self.assertEqual(result['depth']['label'], '浅景深')
