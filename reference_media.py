"""Bounded reference-video inspection and editable shot/segment planning."""
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid


class ReferenceError(ValueError):
    pass


def number(value, label):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ReferenceError(f'{label}必须是数字') from None
    if isinstance(value, bool) or not math.isfinite(result):
        raise ReferenceError(f'{label}必须是有限数字')
    return result


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def run_media(args, check=lambda: None, timeout=120):
    """Poll only our own child; file-backed output avoids pipe deadlocks."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(args, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                check()
                if time.monotonic() > deadline:
                    raise ReferenceError('视频处理超时，请缩短片段或降低分辨率')
                time.sleep(.05)
            check()
            out.seek(0)
            err.seek(0)
            stdout, stderr = out.read(2_000_000), err.read(2_000_000)
            if process.returncode:
                raise ReferenceError('视频无法解码或处理失败，请检查视频及 FFmpeg 配置')
            return stdout.decode('utf-8', 'replace'), stderr.decode('utf-8', 'replace')
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def probe_video(path, ffprobe, check, max_seconds=60):
    raw, _ = run_media([ffprobe, '-v', 'error', '-protocol_whitelist', 'file,pipe',
                        '-show_streams', '-show_format', '-of', 'json', str(path)], check, 20)
    try:
        data = json.loads(raw)
        streams = data.get('streams', [])
        video = next(s for s in streams if s.get('codec_type') == 'video'
                     and not s.get('disposition', {}).get('attached_pic'))
    except (ValueError, StopIteration, TypeError):
        raise ReferenceError('文件中没有可用视频轨') from None
    duration = number(video.get('duration', data.get('format', {}).get('duration')), '视频时长')
    if not 0 < duration <= max_seconds:
        raise ReferenceError(f'视频时长必须在 0～{max_seconds:g} 秒以内')
    width, height = int(video.get('width', 0)), int(video.get('height', 0))
    if not 1 <= width <= 8192 or not 1 <= height <= 8192 or width * height > 34_000_000:
        raise ReferenceError('视频分辨率无效或过大')
    rotation = video.get('tags', {}).get('rotate', 0)
    for side in video.get('side_data_list', []):
        rotation = side.get('rotation', rotation)
    rotation = number(rotation, '视频旋转角度')
    return dict(duration=duration, width=width, height=height, rotation=rotation,
                time_base=video.get('time_base'), avg_frame_rate=video.get('avg_frame_rate'),
                frame_rate=video.get('r_frame_rate'), start_time=video.get('start_time', '0'),
                has_audio=any(s.get('codec_type') == 'audio' for s in streams))


def make_proxy(path, output, ffmpeg, check, duration):
    # Autorotation is applied by ffmpeg; timestamps are elapsed seconds from the
    # first decoded frame, not frame numbers (VFR input is therefore supported).
    run_media([ffmpeg, '-nostdin', '-y', '-v', 'error', '-protocol_whitelist', 'file,pipe',
               '-i', str(path), '-map', '0:v:0', '-an', '-t', str(duration),
               '-vf', "setpts=PTS-STARTPTS,scale=w='min(960,iw)':h='min(960,ih)':"
               'force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1',
               '-r', '24', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '24',
               '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(output)], check)


def detect_shots(proxy, start, end, ffmpeg, check):
    _, logs = run_media([ffmpeg, '-nostdin', '-v', 'info', '-i', str(proxy), '-an',
                         '-vf', f"select='gt(scene,0.32)',showinfo", '-f', 'null', '-'], check)
    cuts = sorted(set(float(t) for t in re.findall(r'pts_time:([\d.]+)', logs)))
    boundaries = [start]
    for t in cuts:
        if start < t < end and t - boundaries[-1] >= .25 and end - t >= .25:
            boundaries.append(t)
    boundaries.append(end)
    if len(boundaries) > 61:
        raise ReferenceError('切镜过多，请缩短选区（最多 60 个参考镜头）')
    return [dict(id=uuid.uuid4().hex[:12], start=round(a, 4), end=round(b, 4))
            for a, b in zip(boundaries, boundaries[1:])]


def extract_frames(proxy, shot, output_dir, ffmpeg, check):
    frames = []
    for index, fraction in enumerate((.15, .5, .85)):
        timestamp = shot['start'] + (shot['end'] - shot['start']) * fraction
        filename = f"{shot['id']}-{index}.jpg"
        run_media([ffmpeg, '-nostdin', '-y', '-v', 'error', '-ss', str(timestamp),
                   '-i', str(proxy), '-frames:v', '1', '-vf',
                   "scale=w='min(512,iw)':h='min(512,ih)':force_original_aspect_ratio=decrease",
                   str(Path(output_dir) / filename)], check, 20)
        if not (Path(output_dir) / filename).is_file():
            raise ReferenceError('参考镜头抽帧失败，请调整选区')
        frames.append(dict(file=filename, time=round(timestamp, 4)))
    return frames


def validate_shots(items, start, end):
    if not isinstance(items, list) or not 1 <= len(items) <= 60:
        raise ReferenceError('需要 1～60 个参考镜头')
    result, seen = [], set()
    fields = ('description', 'camera', 'palette', 'review', 'target_action', 'group')
    for item in items:
        if not isinstance(item, dict):
            raise ReferenceError('参考镜头格式错误')
        key = str(item.get('id', ''))
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', key) or key in seen:
            raise ReferenceError('镜头 ID 无效或重复')
        seen.add(key)
        a, b = number(item.get('start'), '开始时间'), number(item.get('end'), '结束时间')
        if not start <= a < b <= end + .001:
            raise ReferenceError('参考镜头时间越界或起止顺序错误')
        shot = dict(id=key, start=a, end=b)
        for field in fields:
            value = item.get(field, '')
            if not isinstance(value, str) or len(value) > 4000:
                raise ReferenceError(f'{field} 必须是长度不超过 4000 的文本')
            shot[field] = value.strip()
        cast = item.get('cast', [])
        if not isinstance(cast, list) or len(cast) > 12 or any(not isinstance(x, str) or len(x) > 80 for x in cast):
            raise ReferenceError('角色映射格式无效')
        shot['cast'] = list(dict.fromkeys(cast))
        options = item.get('reference', ['camera', 'rhythm'])
        if not isinstance(options, list) or any(x not in ('camera', 'rhythm', 'palette', 'action') for x in options):
            raise ReferenceError('参考项无效')
        shot['reference'] = list(dict.fromkeys(options))
        result.append(shot)
    return result


def build_plan(shots, characters, story, scene, frame_count):
    """Keep source cinematographic shots distinct from H3 production segments."""
    if not isinstance(story, str) or not story.strip() or len(story) > 10000:
        raise ReferenceError('请填写自己的故事/剧本（最多 10000 字）')
    if not isinstance(characters, list) or not 1 <= len(characters) <= 12:
        raise ReferenceError('请提供 1～12 个目标角色')
    cast = []
    for item in characters:
        if not isinstance(item, dict):
            raise ReferenceError('目标角色格式错误')
        name, appearance = item.get('name'), item.get('appearance')
        if not isinstance(name, str) or not re.fullmatch(r'[^/\\\x00-\x1f]{1,80}', name):
            raise ReferenceError('角色名称无效')
        if not isinstance(appearance, str) or not appearance.strip() or len(appearance) > 2000:
            raise ReferenceError('请填写目标角色外貌')
        if name in [c['name'] for c in cast]:
            raise ReferenceError('目标角色名称不能重复')
        cast.append(dict(name=name, appearance=appearance, visual_locked=True))
    if not isinstance(scene, str) or not scene.strip() or len(scene) > 2000:
        raise ReferenceError('请填写目标场景')
    groups = []
    for shot in shots:
        if any(name not in [c['name'] for c in cast] for name in shot['cast']):
            raise ReferenceError('镜头引用了不存在的目标角色')
        if not shot['target_action']:
            raise ReferenceError('每个镜头都需要目标动作/剧情，不能直接冒用原片人物')
        group = shot['group'] or shot['id']
        if not groups or groups[-1][0] != group:
            groups.append((group, []))
        groups[-1][1].append(shot)
    segments, bindings = [], []
    for _, group in groups:
        slices = []
        for shot in group:
            a = shot['start']
            while a < shot['end'] - .0001:
                b = min(a + 15, shot['end'])
                slices.append((shot, a, b))
                a = b
        packs = []
        for entry in slices:
            length = entry[2] - entry[1]
            if not packs or sum(b-a for _, a, b in packs[-1]) + length > 15.0001:
                packs.append([])
            packs[-1].append(entry)
        for pack in packs:
            source_duration = sum(b-a for _, a, b in pack)
            rhythm = all('rhythm' in sh['reference'] for sh, _, _ in pack)
            seconds = min(15, max(3, math.ceil(source_duration) if rhythm else 8))
            count = frame_count(seconds)
            index = len(segments) + 1
            camera, actions, names = [], [], []
            for sh, a, b in pack:
                actions.append(f"源 {a:.2f}–{b:.2f}s：{sh['target_action']}")
                if 'action' in sh['reference']:
                    actions.append('仅参考动作结构，使用目标角色：' + sh['description'])
                if 'camera' in sh['reference']:
                    camera.append(sh['camera'])
                if 'palette' in sh['reference']:
                    camera.append('参考色调：' + sh['palette'])
                names.extend(sh['cast'])
            segment_id = f'EP01-{index:02d}'
            segments.append(dict(index=index, segment_id=segment_id, duration=seconds,
                                 scene='目标场景', characters=list(dict.fromkeys(names)), props=[],
                                 action='目标剧情：' + story + '\n' + '\n'.join(actions),
                                 camera='；'.join(camera) or '根据目标剧情设计镜头',
                                 dialogue='', continue_from_previous=False))
            bindings.append(dict(segment_id=segment_id, mode='shot_reference',
                                 source_ranges=[dict(shot_id=sh['id'], start=a, end=b) for sh,a,b in pack],
                                 source_duration=round(source_duration, 4), planned_duration=seconds,
                                 frames=count, output_duration=count/24,
                                 note='按 H3 能力取整/对齐，不变速源视频' if abs(count/24-source_duration)>.05 else ''))
    if len(segments) > 60:
        raise ReferenceError('生产片段过多，请合并镜头或缩短范围')
    return dict(script=dict(title=story.strip()[:30], characters=cast,
                            scenes=[dict(name='目标场景', description=scene)], props=[], shots=segments),
                segment_bindings=bindings)
