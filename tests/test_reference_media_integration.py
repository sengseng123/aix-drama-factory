"""Opt-in real FFmpeg tests using synthetic, temporary media only."""
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = str(Path(__file__).resolve().parents[1])
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from reference_media import (ReferenceError, detect_shots, extract_frames,
                             make_proxy, probe_video, run_media)

FFMPEG = os.environ.get('AIX_TEST_FFMPEG')
FFPROBE = os.environ.get('AIX_TEST_FFPROBE')


@unittest.skipUnless(FFMPEG and FFPROBE, 'set AIX_TEST_FFMPEG and AIX_TEST_FFPROBE')
class RealMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def generate(self, name, args):
        path = self.root / name
        run_media([FFMPEG, '-nostdin', '-y', '-v', 'error', *args, str(path)])
        return path

    def color_video(self):
        return self.generate('colors.mp4', [
            '-f', 'lavfi', '-i', 'color=red:s=320x180:r=24:d=2',
            '-f', 'lavfi', '-i', 'color=blue:s=320x180:r=24:d=2',
            '-filter_complex', '[0:v][1:v]concat=n=2:v=1:a=0[v]',
            '-map', '[v]', '-c:v', 'libx264', '-pix_fmt', 'yuv420p'])

    def test_real_cut_proxy_frames_and_source_unchanged(self):
        source = self.color_video()
        original = source.read_bytes()
        meta = probe_video(source, FFPROBE, lambda: None)
        self.assertAlmostEqual(meta['duration'], 4, places=1)
        self.assertFalse(meta['has_audio'])
        proxy = self.root / 'preview.mp4'
        make_proxy(source, proxy, FFMPEG, lambda: None, meta['duration'])
        shots = detect_shots(proxy, 0, 4, FFMPEG, lambda: None)
        self.assertEqual(len(shots), 2)
        self.assertAlmostEqual(shots[0]['end'], 2, delta=.05)
        frames = extract_frames(proxy, shots[1], self.root, FFMPEG, lambda: None)
        self.assertEqual(len(frames), 3)
        for frame in frames:
            self.assertGreater((self.root / frame['file']).stat().st_size, 100)
            self.assertTrue(2 < frame['time'] < 4)
        self.assertEqual(source.read_bytes(), original)

    def test_rotated_mov_proxy_has_display_orientation(self):
        source = self.color_video()
        rotated = self.generate('rotated.mov', ['-display_rotation', '90', '-i', str(source), '-c', 'copy'])
        meta = probe_video(rotated, FFPROBE, lambda: None)
        self.assertEqual(abs(meta['rotation']), 90)
        proxy = self.root / 'portrait.mp4'
        make_proxy(rotated, proxy, FFMPEG, lambda: None, meta['duration'])
        output = probe_video(proxy, FFPROBE, lambda: None)
        self.assertEqual((output['width'], output['height']), (180, 320))

    def test_variable_frame_rate_uses_elapsed_time(self):
        source = self.generate('vfr.mp4', ['-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=30:d=4',
            '-vf', "select='if(lt(t,2),not(mod(n,3)),1)'", '-fps_mode', 'vfr', '-c:v', 'libx264'])
        meta = probe_video(source, FFPROBE, lambda: None)
        self.assertNotEqual(meta['avg_frame_rate'], meta['frame_rate'])
        proxy = self.root / 'cfr.mp4'
        make_proxy(source, proxy, FFMPEG, lambda: None, meta['duration'])
        output = probe_video(proxy, FFPROBE, lambda: None)
        self.assertAlmostEqual(output['duration'], meta['duration'], delta=.15)
        frames = extract_frames(proxy, dict(id='last', start=3, end=3.8), self.root, FFMPEG, lambda: None)
        self.assertTrue(all(3 < frame['time'] < 3.8 for frame in frames))

    def test_audio_is_not_copied_to_proxy(self):
        source = self.generate('sound.mp4', ['-f', 'lavfi', '-i', 'color=red:s=160x90:d=1',
            '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-c:a', 'aac', '-shortest'])
        self.assertTrue(probe_video(source, FFPROBE, lambda: None)['has_audio'])
        proxy = self.root / 'silent.mp4'
        make_proxy(source, proxy, FFMPEG, lambda: None, 1)
        self.assertFalse(probe_video(proxy, FFPROBE, lambda: None)['has_audio'])

    def test_corrupt_audio_only_and_duration_limit_fail_clearly(self):
        bad = self.root / 'bad.mp4'
        bad.write_bytes(b'not a video')
        audio = self.generate('audio.mp4', ['-f', 'lavfi', '-i', 'sine=duration=1', '-c:a', 'aac'])
        for source in (bad, audio):
            with self.assertRaises(ReferenceError):
                probe_video(source, FFPROBE, lambda: None)
        with self.assertRaises(ReferenceError):
            probe_video(self.color_video(), FFPROBE, lambda: None, max_seconds=1)


if __name__ == '__main__':
    unittest.main()
