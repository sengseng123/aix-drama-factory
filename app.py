# -*- coding: utf-8 -*-
"""
AIX爱客 - 全自动短剧生成系统
流程：剧本解析 → 图片生成 → 片段视频生成 → 视频合成
图片：千问2512工作流 | 视频：MiniMax H3 r2v工作流 | LLM：内置Qwen3.5-4B（可换任意OpenAI兼容API）
支持托管运行时（默认整包即用）或连接用户已有的 ComfyUI / OpenAI 兼容 LLM。
"""
import os, sys, json, time, re, uuid, copy, random, shutil, subprocess, threading, webbrowser, shlex
from urllib.parse import urlparse
import requests
import websocket
from flask import Flask, request, jsonify, Response, send_file, send_from_directory

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS_DIR = os.path.join(BASE_DIR, 'workflows')
ASSETS_DIR = os.path.join(BASE_DIR, 'assets')
OUTPUTS_DIR = os.path.join(BASE_DIR, 'outputs')
PROJECTS_DIR = os.path.join(BASE_DIR, 'projects')
CONFIG_PATH = os.path.join(BASE_DIR, 'config.json')
LLM_BAT = os.path.join(BASE_DIR, 'llm', 'qwen4b', 'start_qwen4b.bat')
LLAMA_SERVER = os.path.join(BASE_DIR, 'llm', 'qwen4b', 'llama', 'llama-server.exe')
QWEN_MODEL_DIR = os.path.join(BASE_DIR, 'llm', 'qwen4b', 'models')
LOGS_DIR = os.path.join(BASE_DIR, 'logs')
COMFY_BAT = os.path.join(BASE_DIR, 'ComfyUI', 'run_nvidia_gpu_fast_fp16_accumulation.bat')
FFMPEG_LOCAL = os.path.join(BASE_DIR, 'tools', 'ffmpeg', 'ffmpeg.exe')

app = Flask(__name__, static_folder=None)

# ============================== 配置管理 ==============================
# 官方品牌信息由程序只读提供，不属于用户运行配置。
BRAND_PORTAL = {
    "brand_name": "AIX爱客",
    "tutorial_url": "https://space.bilibili.com/1304008552",
    "software_url": "https://my.feishu.cn/wiki/Fksqw9lvgi1RKkk306jckqfHnwg?from=from_copylink",
}

DEFAULT_CONFIG = {
    "llm_mode": "local",                      # local=内置Qwen3.5-4B | custom=自定义API
    "local_llm_url": "http://127.0.0.1:8085",
    "local_llm_model": "qwen3.5-4b",
    "custom_base_url": "http://127.0.0.1:9006/v1",
    "custom_api_key": "EMPTY",
    "custom_model": "qwen3.8-27b",
    "comfyui_url": "http://127.0.0.1:8190",
    "comfyui_runtime_mode": "managed",       # managed=由AIX启动/停止 | external=仅连接已有服务
    "comfyui_start_script": "ComfyUI/run_nvidia_gpu_fast_fp16_accumulation.bat",
    "local_llm_runtime_mode": "managed",     # managed=由AIX启动/停止 | external=仅连接已有服务
    "llama_server_path": "llm/qwen4b/llama/llama-server.exe",
    "qwen_model_path": "llm/qwen4b/models/Qwen3.5-4B-Q4_K_M.gguf",
    "qwen_mmproj_path": "llm/qwen4b/models/mmproj-BF16.gguf",
    "h3_fl2va_model": "",                  # 留空时按 h3_model_profile 自动匹配
    "h3_ref2va_model": "",                 # 留空时按 h3_model_profile 自动匹配
    "image_workflow": "t2i_qwen2512.json", # workflows 下的生图适配器
    "image_model": "",                     # 留空时使用生图工作流内置模型
    "ffmpeg_path": "tools/ffmpeg/ffmpeg.exe",
    "style": "电影写实",
    "scene_reference_mode": "auto",          # auto=按画风匹配 | photo=真实取景 | concept=概念设定
    "shot_duration": "auto",                  # 兼容字段：auto=LLM逐片自定(优先8~15秒) | 固定片段秒数
    "shot_count": "auto",                     # 兼容字段：auto=LLM按故事节奏定 | 用户指定片段数
    "generate_storyboards": False,             # 是否启用分镜图链路：建立/生成或上传，并供H3作为构图参考
    "h3_steps": 10,                           # MiniMax H3 采样步数(4~25)，步数越低越快
    "h3_model_profile": "pruned",             # pruned=12GB推荐 | full=完整模型
    "exclusive_mode": True,                   # 互斥模式：本地LLM与ComfyUI不同时运行（12GB显存默认开启）
    "llm_auto_recover": True,                 # 本地LLM连接中断时自动拉起服务并重试当前请求
    "llm_retries": 3,                         # 首次请求之外的最大重试次数
    "h3_oom_retry": True,                    # 显存不足时清理缓存并自动重试当前片段一次
    "aspect_ratio": "16:9 (Widescreen)",
    "megapixels": 0.92,                       # 0.92≈720p
    "asset_width": 1280,
    "asset_height": 720,
}

H3_MODEL_PROFILES = {
    "pruned": {
        "label": "精简版 INT8（12GB显卡推荐）",
        "fl2va": "minimax_h3_fl2va_pruned_int8_convrot.safetensors",
        "ref2va": "minimax_h3_ref2va_pruned_int8_convrot.safetensors",
    },
    "full": {
        "label": "完整版 INT8（质量优先）",
        "fl2va": "minimax_h3_fl2va_int8_convrot.safetensors",
        "ref2va": "minimax_h3_ref2va_int8_convrot.safetensors",
    },
}

H3_ASPECT_RATIO_ALIASES = {
    "9:16 (Portrait)": "9:16 (Portrait Widescreen)",
}

def load_config():
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"[配置] 读取失败，用默认值: {e}")
    # 兼容旧版本配置文件：曾经可编辑的官方链接不再参与运行配置。
    for key in BRAND_PORTAL:
        cfg.pop(key, None)
    cfg['aspect_ratio'] = H3_ASPECT_RATIO_ALIASES.get(cfg.get('aspect_ratio'), cfg.get('aspect_ratio'))
    return cfg

def public_config():
    result = {**CONFIG, **BRAND_PORTAL}
    result.pop('reference_cloud_api_key', None)
    return result

def save_config(cfg):
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write('\n')

CONFIG = load_config()
DESKTOP_API_VERSION = '1.0'
_LLM_START_LOCK = threading.Lock()
_PREP_ASSET_BATCH_LOCK = threading.Lock()
_PREP_ASSET_BATCH_THREADS = {}

def desktop_safe_mode():
    return os.environ.get('AIX_DESKTOP_MODE') == '1'

@app.before_request
def enforce_desktop_safe_mode():
    if not desktop_safe_mode():
        return None
    if request.path.startswith('/api/desktop/v1/'):
        return None
    if request.method == 'GET' and request.path.startswith('/file/'):
        return None
    return jsonify({'ok': False, 'msg': '桌面安全模式未开放此操作'}), 403
_PREP_ASSET_MANUAL_THREADS = {}
_PREP_ASSET_STATE_LOCK = threading.RLock()
_PROJECT_IO_LOCK = threading.RLock()
_PROJECT_JOB_LOCK = threading.RLock()
_PROJECT_JOB_HANDLES = {}
ACTIVE_PRODUCTION_STATUSES = {'queued', 'running', 'retrying', 'merging', 'stopping'}
TERMINAL_PRODUCTION_STATUSES = {'done', 'failed', 'stopped', 'partial'}

def _lock_project_job_file(pid):
    """Acquire a non-blocking per-project OS lock shared by local/cloud workers."""
    handle = None
    try:
        path = f"{project_path(pid)}.run.lock"
        handle = open(path, 'a+b')
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        print(f"[项目锁] {pid} 获取失败: {exc}")
        return None
    return handle

def claim_project_job(pid, job_id):
    """Claim the only production slot for a project, including across worker processes."""
    with _PROJECT_JOB_LOCK:
        if pid in _PROJECT_JOB_HANDLES:
            return False
        handle = _lock_project_job_file(pid)
        if handle is None:
            return False
        _PROJECT_JOB_HANDLES[pid] = (job_id, handle)
        return True

def release_project_job(pid, job_id=None):
    with _PROJECT_JOB_LOCK:
        current = _PROJECT_JOB_HANDLES.get(pid)
        if not current or (job_id is not None and current[0] != job_id):
            return
        _PROJECT_JOB_HANDLES.pop(pid, None)
        current[1].close()

def project_job_active(pid):
    """Probe the cross-process lock without trusting this worker's memory."""
    with _PROJECT_JOB_LOCK:
        if pid in _PROJECT_JOB_HANDLES:
            return True
        handle = _lock_project_job_file(pid)
        if handle is None:
            return True
        handle.close()
        return False

def resolve_runtime_path(value, fallback=None):
    """Resolve configured runtime paths. Relative paths are based on the AIX app directory."""
    raw = str(value or fallback or '').strip()
    if not raw:
        return ''
    raw = os.path.expandvars(os.path.expanduser(raw))
    if not os.path.isabs(raw):
        raw = os.path.join(BASE_DIR, raw)
    return os.path.normpath(raw)

def comfy_runtime_mode():
    return 'external' if CONFIG.get('comfyui_runtime_mode') == 'external' else 'managed'

def local_llm_runtime_mode():
    return 'external' if CONFIG.get('local_llm_runtime_mode') == 'external' else 'managed'

def comfy_start_script():
    return resolve_runtime_path(CONFIG.get('comfyui_start_script'), COMFY_BAT)

def llama_server_path():
    return resolve_runtime_path(CONFIG.get('llama_server_path'), LLAMA_SERVER)

def qwen_model_path():
    return resolve_runtime_path(CONFIG.get('qwen_model_path'),
                                os.path.join(QWEN_MODEL_DIR, 'Qwen3.5-4B-Q4_K_M.gguf'))

def qwen_mmproj_path():
    return resolve_runtime_path(CONFIG.get('qwen_mmproj_path'),
                                os.path.join(QWEN_MODEL_DIR, 'mmproj-BF16.gguf'))

def local_llm_models():
    """List local text GGUF files without exposing unrelated filesystem contents."""
    roots = {os.path.normpath(QWEN_MODEL_DIR), os.path.dirname(qwen_model_path())}
    models = []
    seen = set()
    for root in sorted(roots):
        if not root or not os.path.isdir(root):
            continue
        for current, dirs, names in os.walk(root):
            dirs[:] = sorted(dirs)[:50]
            for name in sorted(names):
                path = os.path.normpath(os.path.join(current, name))
                if (not name.lower().endswith('.gguf') or name.lower().startswith('mmproj')):
                    continue
                key = os.path.normcase(path)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    value = os.path.relpath(path, BASE_DIR)
                    if value.startswith('..' + os.sep):
                        value = path
                except ValueError:
                    value = path
                models.append({'label': name, 'path': value, 'size': os.path.getsize(path)})
    return models

def runtime_env_command(name):
    """Read a trusted service command from the host environment, never from the web UI."""
    raw = str(os.environ.get(name, '') or '').strip()
    if not raw:
        return []
    try:
        return shlex.split(raw, posix=(os.name != 'nt'))
    except ValueError as exc:
        print(f"[运行时] {name} 解析失败: {exc}")
        return []

def run_runtime_env_command(name, wait=True):
    command = runtime_env_command(name)
    if not command:
        return False, f"未配置主机运行命令 {name}"
    try:
        if wait:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            if result.returncode != 0:
                detail = (result.stderr or result.stdout or '').strip()[-500:]
                return False, f"{name} 执行失败({result.returncode}): {detail}"
        else:
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
            subprocess.Popen(command, cwd=BASE_DIR, creationflags=creationflags,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, None
    except Exception as exc:
        return False, f"{name} 执行异常: {exc}"

# ============================== 慢动作过滤 ==============================
SLOW_MO_PATTERNS = [
    r'慢动作', r'慢镜头', r'慢速镜头', r'慢放', r'慢速播放', r'减速播放',
    r'升格镜头', r'升格拍摄', r'升格', r'子弹时间',
    # 凝滞/慢节奏词：H3对"缓慢""定格"极敏感，会直接渲染成慢放/冻结帧
    r'缓慢地?', r'缓缓地?', r'慢速', r'慢节奏', r'定格', r'凝滞', r'静帧', r'静止画面',
    r'时间仿佛静止', r'画面仿佛静止', r'空气仿佛凝固',
    r'slow[- ]?motion', r'slowmotion', r'slow[- ]?mo', r'\bslo[- ]?mo\b',
    r'\bslomo\b', r'bullet time', r'super slow motion',
    r'\bslowly\b', r'\bgradually\b', r'\bgradual\b', r'freeze[- ]?frame', r'frozen frame',
    r'\b(120|240|480|960)\s?fps\b',
]
SLOW_MO_RE = re.compile('|'.join(SLOW_MO_PATTERNS), re.IGNORECASE)

def filter_slow_motion(text):
    if not text:
        return text
    cleaned = SLOW_MO_RE.sub('', text)
    cleaned = re.sub(r'[，,、]\s*[，,、]+', '，', cleaned)
    cleaned = re.sub(r'^\s*[，,、]+|[，,、]+\s*$', '', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
    return cleaned.strip()

# ============================== LLM 统一调用 ==============================
def get_llm_endpoint():
    if CONFIG.get('llm_mode') == 'custom':
        base = CONFIG.get('custom_base_url', '').rstrip('/')
        return base, CONFIG.get('custom_api_key', 'EMPTY'), CONFIG.get('custom_model', '')
    return CONFIG.get('local_llm_url', 'http://127.0.0.1:8085').rstrip('/'), 'EMPTY', CONFIG.get('local_llm_model', 'qwen3.5-4b')

def llm_chat(messages, max_tokens=4096, temperature=0.7, retries=None, stop_check=None):
    if retries is None:
        retries = max(0, int(CONFIG.get('llm_retries', 3)))
    base, key, model = get_llm_endpoint()
    payload = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False}
    }
    if 'deepseek' in (model or '').lower():
        payload["reasoning_effort"] = "none"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    last_err = None
    for attempt in range(retries + 1):
        if stop_check and stop_check():
            raise ProjectStopRequested('当前项目已收到停止请求')
        try:
            r = requests.post(f"{base}/chat/completions", json=payload, headers=headers, timeout=600)
            r.raise_for_status()
            data = r.json()
            content = data['choices'][0]['message'].get('content', '') or ''
            # 剥离思考块：部分服务端（如llama.cpp）即使关闭思考也会残留空<think></think>标签，防止污染提示词/JSON
            content = re.sub(r'<think>.*?</think>', '', content, flags=re.S).strip()
            if content.strip():
                return content.strip(), None
            last_err = "模型返回空内容"
        except Exception as e:
            last_err = str(e)
            print(f"[LLM] 第{attempt+1}次调用失败: {e}")
            if stop_check and stop_check():
                raise ProjectStopRequested('当前项目已收到停止请求')
            http_server_error = (isinstance(e, requests.exceptions.HTTPError) and
                                 getattr(e.response, 'status_code', 0) >= 500)
            retryable_service_error = isinstance(e, (requests.exceptions.ConnectionError,
                                                     requests.exceptions.Timeout)) or http_server_error
            can_recover = (attempt < retries and retryable_service_error and
                           CONFIG.get('llm_mode') == 'local' and
                           CONFIG.get('llm_auto_recover', True))
            if can_recover:
                if stop_check and stop_check():
                    raise ProjectStopRequested('当前项目已收到停止请求')
                print("[LLM] 检测到本地服务中断，正在自动恢复；恢复后重试当前请求...")
                ok, recover_err = ensure_local_llm()
                if not ok:
                    last_err = f"本地LLM自动恢复失败: {recover_err}"
                    print(f"[LLM] {last_err}")
        if stop_check and stop_check():
            raise ProjectStopRequested('当前项目已收到停止请求')
        if attempt < retries:
            time.sleep(2)
    return None, last_err

def _ensure_local_llm_unlocked():
    """Check local LLM; managed mode may start it, external mode never owns its process."""
    base, _, _ = get_llm_endpoint()
    if CONFIG.get('llm_mode') == 'custom':
        try:
            r = requests.get(f"{base}/models", timeout=5)
            return r.status_code == 200, None
        except Exception as e:
            return False, f"自定义LLM服务不可达: {e}"
    try:
        r = requests.get(f"{base}/models", timeout=3)
        if r.status_code == 200:
            return True, None
    except Exception:
        pass
    if local_llm_runtime_mode() == 'external':
        return False, f"外部Qwen服务不可达: {base}（外部模式不会自动启动或停止该服务）"
    if runtime_env_command('AIX_LLM_START_COMMAND'):
        ok, start_err = run_runtime_env_command('AIX_LLM_START_COMMAND', wait=False)
        if not ok:
            return False, start_err
        for _ in range(90):
            time.sleep(2)
            try:
                if requests.get(f"{base}/models", timeout=3).status_code == 200:
                    print("[LLM] 主机托管Qwen服务已就绪")
                    return True, None
            except Exception:
                continue
        return False, "主机托管Qwen启动超时(180秒)"
    server_path = llama_server_path()
    model_path = qwen_model_path()
    mmproj_path = qwen_mmproj_path()
    missing = [path for path in (server_path, model_path) if not os.path.exists(path)]
    if missing:
        return False, f"本地LLM文件不存在: {missing[0]}"
    print("[LLM] 启动本地Qwen3.5-4B服务...")
    creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    os.makedirs(LOGS_DIR, exist_ok=True)
    log_path = os.path.join(LOGS_DIR, 'qwen.autostart.log')
    log_file = open(log_path, 'ab', buffering=0)
    try:
        command = [
            server_path, '--model', model_path,
            '--host', urlparse(base).hostname or '127.0.0.1',
            '--port', str(urlparse(base).port or 8085), '--ctx-size', '16384',
            '--n-predict', '4096', '--n-gpu-layers', '-1', '--flash-attn', 'on',
            '--reasoning-budget', '0'
        ]
        if mmproj_path and os.path.isfile(mmproj_path):
            command[3:3] = ['--mmproj', mmproj_path]
        subprocess.Popen(command, cwd=os.path.dirname(server_path), creationflags=creationflags,
           stdout=log_file, stderr=log_file)
    finally:
        log_file.close()
    for i in range(90):
        time.sleep(2)
        try:
            r = requests.get(f"{base}/models", timeout=3)
            if r.status_code == 200:
                print("[LLM] 本地服务已就绪")
                return True, None
        except Exception:
            continue
    return False, "本地LLM启动超时(180秒)"

def ensure_local_llm():
    """串行检查/拉起开发版LLM，避免并发失败时重复启动多个8085服务。"""
    with _LLM_START_LOCK:
        return _ensure_local_llm_unlocked()

# ============================== 互斥调度（低显存模式） ==============================
def exclusive_on():
    """Only processes managed by AIX may participate in automatic GPU switching."""
    return (bool(CONFIG.get('exclusive_mode')) and CONFIG.get('llm_mode') == 'local'
            and local_llm_runtime_mode() == 'managed'
            and comfy_runtime_mode() == 'managed')

def kill_by_port(port):
    """Windows整合包兼容兜底。Linux必须使用受信任的主机服务脚本。"""
    if os.name != 'nt':
        return False
    try:
        raw = subprocess.run(['netstat', '-ano'], capture_output=True, timeout=15).stdout or b''
        # Windows 本地化命令通常使用系统 ANSI 代码页输出。桌面壳可能强制
        # Python 使用 UTF-8；让 subprocess 自动解码会令读取线程崩溃，并把
        # stdout 留成 None。显式按 Windows 代码页容错解码，避免切换服务失败。
        out = raw.decode('mbcs', errors='replace') if isinstance(raw, bytes) else str(raw)
    except Exception:
        return False
    pids = {ln.split()[-1] for ln in out.splitlines() if f':{port}' in ln and 'LISTENING' in ln}
    for p in pids:
        if p and p != '0':
            print(f"[互斥] taskkill PID={p} (端口{port})")
            subprocess.run(['taskkill', '/PID', p, '/F'], capture_output=True, timeout=15)
    return bool(pids)

def wait_http_offline(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            requests.get(url, timeout=2)
        except Exception:
            return True
        time.sleep(1)
    return False

def stop_local_llm():
    """互斥模式：关闭本地LLM释放显存，并等待端口真正释放"""
    if CONFIG.get('llm_mode') != 'local' or local_llm_runtime_mode() != 'managed':
        return True, None
    base = CONFIG.get('local_llm_url', 'http://127.0.0.1:8085').rstrip('/')
    port = urlparse(base).port or 8085
    print(f"[互斥] 关闭本地LLM(端口{port})...")
    command = runtime_env_command('AIX_LLM_STOP_COMMAND')
    if command:
        ok, err = run_runtime_env_command('AIX_LLM_STOP_COMMAND')
        if not ok:
            return False, err
    elif not kill_by_port(port):
        return False, "当前主机未配置Qwen停止命令，不能安全执行互斥切换"
    if wait_http_offline(f"{base}/models"):
        print("[互斥] LLM已停止")
        return True, None
    return False, f"Qwen端口{port}未释放，已停止本次切换"

def stop_comfyui():
    """互斥模式：关闭ComfyUI释放显存，并等待端口真正释放"""
    if comfy_runtime_mode() != 'managed':
        return True, None
    port = urlparse(comfy_url()).port or 8190
    print(f"[互斥] 关闭ComfyUI(端口{port})...")
    command = runtime_env_command('AIX_COMFY_STOP_COMMAND')
    if command:
        ok, err = run_runtime_env_command('AIX_COMFY_STOP_COMMAND')
        if not ok:
            return False, err
    elif not kill_by_port(port):
        return False, "当前主机未配置ComfyUI停止命令，不能安全执行互斥切换"
    if wait_http_offline(f"{comfy_url()}/system_stats"):
        print("[互斥] ComfyUI已停止")
        return True, None
    return False, f"ComfyUI端口{port}未释放，已停止本次切换"

# ============================== ComfyUI 客户端 ==============================
def comfy_url():
    return CONFIG.get('comfyui_url', 'http://127.0.0.1:8190').rstrip('/')

def comfy_check():
    try:
        r = requests.get(f"{comfy_url()}/system_stats", timeout=5)
        return r.status_code == 200
    except Exception:
        return False

def get_h3_model_profile():
    profile = str(CONFIG.get('h3_model_profile', 'pruned')).lower()
    return profile if profile in H3_MODEL_PROFILES else 'pruned'

def get_h3_model_name(mode):
    family = 'ref2va' if mode == 'r2v' else 'fl2va'
    configured = str(CONFIG.get(f'h3_{family}_model') or '').strip()
    return configured or H3_MODEL_PROFILES[get_h3_model_profile()][family]

def resolve_h3_model_name(mode, available=None):
    """Resolve a workflow-role model; explicit choices are never guessed or substituted."""
    requested = get_h3_model_name(mode)
    available = list(available if available is not None else comfy_unet_models())
    if not available or requested in available:
        return requested
    family = 'ref2va' if mode == 'r2v' else 'fl2va'
    if str(CONFIG.get(f'h3_{family}_model') or '').strip():
        raise ValueError(f'ComfyUI 未检测到所选 H3 模型: {requested}')
    family_matches = []
    for name in available:
        lower = name.lower().replace('-', '_')
        if mode == 'r2v':
            if 'ref2va' in lower or 'remix' in lower:
                family_matches.append(name)
        elif 'fl2va' in lower:
            family_matches.append(name)
    if not family_matches:
        family = 'REF2VA/Remix' if mode == 'r2v' else 'FL2VA'
        raise ValueError(f"未检测到可用的{family}模型；当前模型: {', '.join(available)}")
    profile = get_h3_model_profile()
    def rank(name):
        lower = name.lower()
        return (
            0 if profile == 'pruned' and 'pruned' in lower else 1,
            0 if 'int8' in lower else 1,
            0 if 'remix' in lower else 1,
            name.lower(),
        )
    selected = sorted(family_matches, key=rank)[0]
    print(f"[H3模型] 预设文件不存在，已匹配服务器实际模型: {requested} -> {selected}")
    return selected

def apply_h3_model(workflow, mode):
    """将各工作流用途槽位选中的模型写入对应工作流。"""
    model_name = resolve_h3_model_name(mode)
    loaders = [node for node in workflow.values()
               if isinstance(node, dict) and node.get('class_type') == 'UNETLoader']
    if not loaders:
        raise ValueError("H3工作流中未找到UNETLoader")
    for node in loaders:
        node.setdefault('inputs', {})['unet_name'] = model_name
    return model_name

def comfy_unet_models():
    # ComfyUI does not publish reliable architecture metadata for arbitrary
    # community UNET names. Return its complete loader enum and let users map
    # any candidate to either workflow role; runtime errors remain explicit.
    return comfy_loader_models('UNETLoader')

def comfy_loader_models(class_type):
    """Return exact model options exposed by one connected ComfyUI loader."""
    try:
        encoded = requests.utils.quote(class_type, safe='')
        r = requests.get(f"{comfy_url()}/object_info/{encoded}", timeout=5)
        r.raise_for_status()
        schema = r.json()[class_type]['input']['required']
        for input_name in ('unet_name', 'ckpt_name'):
            options = schema.get(input_name, [[]])[0]
            if isinstance(options, list):
                return sorted(name for name in options if isinstance(name, str))
    except Exception:
        pass
    return []

def ensure_comfyui(max_wait=300):
    """Ensure ComfyUI is online; external mode never starts or stops user services."""
    if comfy_check():
        return True
    if comfy_runtime_mode() == 'external':
        print(f"[ComfyUI] 外部服务不可达: {comfy_url()}（不会自动启动或停止）")
        return False
    if runtime_env_command('AIX_COMFY_START_COMMAND'):
        ok, err = run_runtime_env_command('AIX_COMFY_START_COMMAND', wait=False)
        if not ok:
            print(f"[ComfyUI] {err}")
            return False
    else:
        start_script = comfy_start_script()
        if not os.path.exists(start_script):
            return False
        if os.name != 'nt':
            print("[ComfyUI] Linux托管模式必须配置 AIX_COMFY_START_COMMAND")
            return False
        print("[ComfyUI] 启动内置整合包...")
        subprocess.Popen(['cmd', '/c', 'start', '', start_script], cwd=os.path.dirname(start_script))
    for _ in range(max_wait // 3):
        time.sleep(3)
        if comfy_check():
            print("[ComfyUI] 已就绪")
            return True
    return False

def comfy_queue_busy():
    """返回开发版ComfyUI是否有运行/等待任务；读取失败时按忙处理。"""
    try:
        data = requests.get(f"{comfy_url()}/queue", timeout=5).json()
        return bool(data.get('queue_running') or data.get('queue_pending')), None
    except Exception as e:
        return True, f"无法读取ComfyUI队列: {e}"

def _comfy_queue_prompt_ids(queue_data):
    prompt_ids = set()
    for key in ('queue_running', 'queue_pending'):
        for item in (queue_data or {}).get(key, []):
            if isinstance(item, (list, tuple)) and len(item) > 1:
                prompt_ids.add(str(item[1]))
            elif isinstance(item, dict) and item.get('prompt_id'):
                prompt_ids.add(str(item['prompt_id']))
    return prompt_ids

def _comfy_queue_prompt_state(queue_data, prompt_id):
    """Return the state of one prompt without making assumptions about other jobs."""
    target = str(prompt_id)
    for key, state in (('queue_running', 'running'), ('queue_pending', 'pending')):
        for item in (queue_data or {}).get(key, []):
            item_id = None
            if isinstance(item, (list, tuple)) and len(item) > 1:
                item_id = item[1]
            elif isinstance(item, dict):
                item_id = item.get('prompt_id')
            if item_id is not None and str(item_id) == target:
                return state
    return None

def comfy_cancel_prompt_safely(prompt_id):
    """Cancel only one owned prompt; never fall back to ComfyUI's global interrupt."""
    prompt_id = str(prompt_id or '').strip()
    if not prompt_id:
        return 'none', '当前项目没有已提交的 ComfyUI 任务'

    # New ComfyUI versions expose an atomic, idempotent per-job cancellation API.
    # A 404 means the route is unavailable on older installations, so fall back
    # only to deleting this exact prompt while it is still pending.
    try:
        encoded = requests.utils.quote(prompt_id, safe='')
        response = requests.post(
            f"{comfy_url()}/api/jobs/{encoded}/cancel", timeout=10)
        if response.status_code == 200:
            cancelled = bool((response.json() or {}).get('cancelled'))
            return ('cancelled' if cancelled else 'terminal'), (
                '已向 ComfyUI 发送当前任务的定向停止请求'
                if cancelled else '当前 ComfyUI 任务已结束或无需取消')
        if response.status_code not in (404, 405):
            return 'deferred', f'ComfyUI 定向停止返回 HTTP {response.status_code}，将在当前步骤结束后停止'
    except Exception as exc:
        print(f"[ComfyUI] 定向停止接口不可用，检查精确队列删除: {exc}")

    try:
        queue_response = requests.get(f"{comfy_url()}/queue", timeout=5)
        queue_response.raise_for_status()
        state = _comfy_queue_prompt_state(queue_response.json(), prompt_id)
        if state == 'pending':
            response = requests.post(
                f"{comfy_url()}/queue", json={'delete': [prompt_id]}, timeout=10)
            response.raise_for_status()
            verify = requests.get(f"{comfy_url()}/queue", timeout=5)
            verify.raise_for_status()
            remaining = _comfy_queue_prompt_state(verify.json(), prompt_id)
            if remaining is None:
                return 'cancelled', '已从 ComfyUI 队列删除当前项目的等待任务'
            if remaining == 'running':
                return 'deferred', '任务已开始运行；当前 ComfyUI 版本不支持安全定向中断，将在当前步骤结束后停止'
            return 'deferred', 'ComfyUI 尚未确认删除等待任务，将继续等待当前步骤结束'
        if state == 'running':
            return 'deferred', '当前 ComfyUI 版本不支持安全停止运行中任务，将在当前步骤结束后停止'
        return 'terminal', '当前 ComfyUI 任务已结束或已离开队列'
    except Exception as exc:
        return 'deferred', f'无法确认 ComfyUI 定向停止能力，将在当前步骤结束后停止：{exc}'

def comfy_wait_prompt_exit(prompt_id, timeout=30, interval=0.5):
    """Wait for one failed prompt to leave the queue without interrupting any work."""
    deadline = time.monotonic() + max(0, timeout)
    last_error = None
    while True:
        try:
            response = requests.get(f"{comfy_url()}/queue", timeout=5)
            response.raise_for_status()
            if str(prompt_id) not in _comfy_queue_prompt_ids(response.json()):
                return True, None
        except Exception as exc:
            last_error = str(exc)
        if time.monotonic() >= deadline:
            detail = f"；最后一次队列读取错误: {last_error}" if last_error else ''
            return False, f"失败任务仍未退出ComfyUI队列{detail}"
        time.sleep(interval)

def prepare_llm_stage():
    """V2文案阶段：安全切换到Qwen；绝不打断有队列的ComfyUI。"""
    if CONFIG.get('llm_mode') == 'custom':
        return ensure_local_llm()
    if exclusive_on() and comfy_check():
        busy, err = comfy_queue_busy()
        if busy:
            return False, err or "ComfyUI仍有任务，不能切换到Qwen"
        stopped, stop_err = stop_comfyui()
        if not stopped:
            return False, stop_err
    return ensure_local_llm()

def prepare_comfy_stage():
    """V2图片/视频阶段：互斥模式下先释放Qwen，再启动ComfyUI。"""
    if exclusive_on():
        base, _, _ = get_llm_endpoint()
        try:
            if requests.get(f"{base}/models", timeout=3).status_code == 200:
                stopped, stop_err = stop_local_llm()
                if not stopped:
                    print(f"[互斥] {stop_err}")
                    return False
        except Exception:
            pass
    return ensure_comfyui()

def find_ffmpeg():
    """Prefer configured ffmpeg, then bundled ffmpeg, then system PATH."""
    configured = resolve_runtime_path(CONFIG.get('ffmpeg_path'))
    if configured and os.path.isfile(configured):
        return configured
    if os.path.isfile(FFMPEG_LOCAL):
        return FFMPEG_LOCAL
    return shutil.which('ffmpeg')

def runtime_preflight():
    """Read-only environment report used by first-run and external-runtime setup."""
    base, _, model = get_llm_endpoint()
    try:
        llm_online = requests.get(f"{base}/models", timeout=3).status_code == 200
    except Exception:
        llm_online = False
    comfy_online = comfy_check()
    ffmpeg = find_ffmpeg()
    comfy_host_managed = bool(runtime_env_command('AIX_COMFY_START_COMMAND'))
    llm_host_managed = bool(runtime_env_command('AIX_LLM_START_COMMAND'))
    comfy_start_available = (comfy_host_managed or os.path.isfile(comfy_start_script()))
    checks = {
        'comfyui': {
            'mode': comfy_runtime_mode(), 'url': comfy_url(), 'online': comfy_online,
            'start_script': comfy_start_script() if comfy_runtime_mode() == 'managed' else '',
            'start_script_exists': (comfy_start_available
                                    if comfy_runtime_mode() == 'managed' else None),
            'host_managed': (comfy_host_managed
                             if comfy_runtime_mode() == 'managed' else False),
        },
        'llm': {
            'source': CONFIG.get('llm_mode', 'local'),
            'mode': (local_llm_runtime_mode() if CONFIG.get('llm_mode') == 'local' else 'external'),
            'url': base, 'model': model, 'online': llm_online,
        },
        'ffmpeg': {'path': ffmpeg or '', 'available': bool(ffmpeg)},
    }
    if (CONFIG.get('llm_mode') == 'local' and local_llm_runtime_mode() == 'managed'
            and not llm_host_managed):
        checks['llm']['files'] = {
            'server': os.path.isfile(llama_server_path()),
            'model': os.path.isfile(qwen_model_path()),
            'mmproj_optional': os.path.isfile(qwen_mmproj_path()),
        }
    checks['llm']['host_managed'] = (llm_host_managed
                                     if checks['llm']['mode'] == 'managed' else False)
    llm_files = checks['llm'].get('files', {})
    llm_files_ready = all(llm_files.get(key) for key in ('server', 'model')) if llm_files else True
    llm_start_available = (llm_host_managed or llm_files_ready)
    messages = []
    if not comfy_online:
        if comfy_runtime_mode() == 'external':
            messages.append('外部 ComfyUI 当前不可达；AIX 不会自动启动或停止它')
        elif not comfy_start_available:
            messages.append('未找到托管 ComfyUI 启动脚本')
    if not llm_online:
        if checks['llm']['mode'] == 'external':
            messages.append('外部 LLM 当前不可达；AIX 不会自动启动或停止它')
        elif not llm_start_available:
            messages.append('本地 Qwen 运行程序或模型文件不完整')
    if not ffmpeg:
        messages.append('未找到 FFmpeg；视频合成和尾帧提取不可用')
    return {
        'ok': True,
        'ready_for_story': llm_online or (checks['llm']['mode'] == 'managed' and llm_start_available),
        'ready_for_video': ((comfy_online or (comfy_runtime_mode() == 'managed' and comfy_start_available))
                            and bool(ffmpeg)),
        'checks': checks,
        'messages': messages,
    }

def acceleration_status():
    """Inspect acceleration that is actually active in the shipped H3 workflow."""
    result = {
        "fast_fp16_accumulation": False,
        "quantized_unet": False,
        "quantized_text_encoder": False,
        "sageattention_node": False,
        "te_speed_node": False,
        "teacache_node": False,
        "unet_model": "",
        "text_encoder": "",
        "h3_steps": get_h3_steps() if 'get_h3_steps' in globals() else CONFIG.get('h3_steps', 10),
        "h3_model_profile": get_h3_model_profile(),
    }
    try:
        with open(comfy_start_script(), 'r', encoding='utf-8', errors='ignore') as f:
            result["fast_fp16_accumulation"] = '--fast fp16_accumulation' in f.read().lower()
    except Exception:
        pass
    try:
        with open(os.path.join(WORKFLOWS_DIR, 'h3_r2v.json'), 'r', encoding='utf-8') as f:
            workflow = json.load(f)
        classes = [str(node.get('class_type', '')).lower() for node in workflow.values() if isinstance(node, dict)]
        result["sageattention_node"] = any('sage' in name and 'attention' in name for name in classes)
        result["te_speed_node"] = any('tespeedminimaxh3' in name or 'te-speed' in name for name in classes)
        result["teacache_node"] = any('teacache' in name or 'tea_cache' in name for name in classes)
        for node in workflow.values():
            if not isinstance(node, dict):
                continue
            inputs = node.get('inputs', {})
            if node.get('class_type') == 'UNETLoader':
                result["unet_model"] = str(inputs.get('unet_name', ''))
            elif node.get('class_type') == 'CLIPLoader':
                result["text_encoder"] = str(inputs.get('clip_name', ''))
        result["unet_model"] = get_h3_model_name('r2v')
        unet_lower = result["unet_model"].lower()
        clip_lower = result["text_encoder"].lower()
        result["quantized_unet"] = any(tag in unet_lower for tag in ('int8', 'fp8', 'nvfp4', 'gguf'))
        result["quantized_text_encoder"] = any(tag in clip_lower for tag in ('int8', 'fp8', 'nvfp4', 'awq', 'gguf'))
    except Exception as e:
        result["inspect_error"] = str(e)
    return result

def comfy_upload_image(file_path):
    with open(file_path, 'rb') as f:
        files = {'image': (os.path.basename(file_path), f, 'image/png')}
        r = requests.post(f"{comfy_url()}/upload/image", files=files, data={'overwrite': 'true'}, timeout=60)
    r.raise_for_status()
    return r.json().get('name', os.path.basename(file_path))

def _normalize_comfy_option(value):
    """Normalize only path separators for cross-platform ComfyUI enum matching."""
    return str(value or '').replace('\\', '/').strip('/')

def comfy_resolve_workflow_options(workflow):
    """Replace Windows/Linux path variants with the exact values exposed by ComfyUI.

    Resolution is intentionally conservative: only scalar path-like strings are
    considered, and a value is changed only when an enum option from the node's
    live ``/object_info`` schema has the same normalized full path.
    """
    resolved = []
    class_schemas = {}
    for node_id, node in workflow.items():
        if not isinstance(node, dict):
            continue
        class_type = str(node.get('class_type') or '')
        inputs = node.get('inputs')
        if not class_type or not isinstance(inputs, dict):
            continue
        candidates = {
            key: value for key, value in inputs.items()
            if (str(key).endswith('_name') and isinstance(value, str)
                and ('\\' in value or '/' in value))
        }
        if not candidates:
            continue
        if class_type not in class_schemas:
            try:
                encoded = requests.utils.quote(class_type, safe='')
                response = requests.get(f"{comfy_url()}/object_info/{encoded}", timeout=10)
                response.raise_for_status()
                class_schemas[class_type] = response.json().get(class_type, {}).get('input', {})
            except Exception:
                class_schemas[class_type] = {}
        schema = class_schemas[class_type]
        for input_name, current_value in candidates.items():
            definition = None
            for section in ('required', 'optional'):
                section_schema = schema.get(section, {})
                if input_name in section_schema:
                    definition = section_schema[input_name]
                    break
            if not isinstance(definition, (list, tuple)) or not definition:
                continue
            options = definition[0]
            if not isinstance(options, (list, tuple)):
                continue
            if current_value in options:
                continue
            normalized = _normalize_comfy_option(current_value)
            matches = [option for option in options
                       if isinstance(option, str) and _normalize_comfy_option(option) == normalized]
            if len(matches) != 1:
                continue
            inputs[input_name] = matches[0]
            resolved.append((str(node_id), input_name, current_value, matches[0]))
    return resolved

def comfy_submit(workflow, client_id=None):
    submitted_workflow = copy.deepcopy(workflow)
    for node_id, input_name, old_value, new_value in comfy_resolve_workflow_options(submitted_workflow):
        print(f"[ComfyUI] 跨平台模型路径已匹配: 节点{node_id}.{input_name}: {old_value} -> {new_value}")
    payload = {"prompt": submitted_workflow}
    if client_id:
        payload['client_id'] = client_id
    r = requests.post(f"{comfy_url()}/prompt", json=payload, timeout=30)
    if not r.ok:
        try:
            data = r.json()
            errors = []
            for node_id, node_error in data.get('node_errors', {}).items():
                for item in node_error.get('errors', []):
                    detail = item.get('details') or item.get('message') or item.get('type')
                    if detail:
                        errors.append(f"节点{node_id}: {detail}")
            message = '；'.join(errors) or data.get('error', {}).get('message') or r.text[:500]
        except (ValueError, AttributeError):
            message = r.text[:500]
        raise RuntimeError(f"ComfyUI拒绝工作流：{message}")
    return r.json()['prompt_id']

def comfy_open_ws(client_id):
    parsed = urlparse(comfy_url())
    scheme = 'wss' if parsed.scheme == 'https' else 'ws'
    ws_url = f"{scheme}://{parsed.netloc}/ws?clientId={client_id}"
    ws = websocket.create_connection(ws_url, timeout=5)
    ws.settimeout(1.0)
    return ws

def comfy_wait(prompt_id, timeout=1200, interval=3, progress_cb=None, ws=None,
               stop_check=None, stop_handler=None):
    start = time.time()
    last_poll = 0
    stop_deferred = False
    cancel_requested = False
    cancel_left_queue_at = None
    try:
        while time.time() - start < timeout:
            if stop_check and stop_check():
                if not stop_deferred:
                    action, message = (stop_handler(prompt_id) if stop_handler
                                       else ('deferred', '将在当前步骤结束后停止'))
                    if action == 'cancelled':
                        exited, _ = comfy_wait_prompt_exit(prompt_id, timeout=5)
                        stop_deferred = True
                        cancel_requested = True
                        if exited:
                            cancel_left_queue_at = time.monotonic()
                    elif action == 'none':
                        return None, f"__AIX_STOPPED__:{message}"
                    else:
                        stop_deferred = True
            if ws is not None:
                try:
                    raw = ws.recv()
                    if isinstance(raw, str):
                        msg = json.loads(raw)
                        event_type = msg.get('type')
                        data = msg.get('data', {})
                        event_prompt = data.get('prompt_id')
                        if event_prompt in (None, prompt_id):
                            if event_type == 'progress' and progress_cb:
                                value, maximum = data.get('value', 0), data.get('max', 0)
                                if maximum:
                                    progress_cb(value, maximum, data.get('node'))
                            elif event_type == 'execution_error':
                                if stop_deferred:
                                    return None, "__AIX_STOPPED__:当前步骤已结束，项目已停止"
                                return None, f"ComfyUI执行错误: {data.get('exception_message') or data.get('exception_type') or '未知错误'}"
                except websocket.WebSocketTimeoutException:
                    pass
                except Exception as e:
                    print(f"[ComfyUI] WebSocket进度异常，改用历史轮询: {e}")
                    try:
                        ws.close()
                    except Exception:
                        pass
                    ws = None
            now = time.time()
            if now - last_poll >= interval:
                last_poll = now
                try:
                    r = requests.get(f"{comfy_url()}/history/{prompt_id}", timeout=15)
                    if r.status_code == 200:
                        h = r.json()
                        if prompt_id in h and h[prompt_id].get('status', {}).get('completed'):
                            return h[prompt_id], None
                        if prompt_id in h:
                            st = h[prompt_id].get('status', {})
                            if st.get('status_str') == 'error':
                                if stop_deferred:
                                    return None, "__AIX_STOPPED__:当前步骤已结束，项目已停止"
                                return None, f"ComfyUI执行错误: {json.dumps(st.get('messages', []), ensure_ascii=False)[:300]}"
                    if cancel_requested:
                        queue_response = requests.get(f"{comfy_url()}/queue", timeout=5)
                        queue_response.raise_for_status()
                        if _comfy_queue_prompt_state(queue_response.json(), prompt_id) is None:
                            cancel_left_queue_at = cancel_left_queue_at or time.monotonic()
                            if time.monotonic() - cancel_left_queue_at >= 3:
                                return None, "__AIX_STOPPED__:ComfyUI 已结束当前项目任务"
                        else:
                            cancel_left_queue_at = None
                except Exception as e:
                    print(f"[ComfyUI] 轮询异常: {e}")
            if ws is None:
                time.sleep(min(interval, 1))
        if stop_deferred:
            return None, "__AIX_STOPPED__:停止请求已保存；当前 ComfyUI 步骤仍在收尾"
        return None, f"ComfyUI生成超时({timeout}秒)"
    finally:
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass

def comfy_download(file_info, save_path):
    params = {'filename': file_info['filename'], 'subfolder': file_info.get('subfolder', ''), 'type': file_info.get('type', 'output')}
    r = requests.get(f"{comfy_url()}/view", params=params, timeout=120)
    r.raise_for_status()
    with open(save_path, 'wb') as f:
        f.write(r.content)
    return save_path

def comfy_free_memory(after_prompt_id=None, wait_timeout=0):
    """Unload cached models only while ComfyUI has no running or pending work."""
    if after_prompt_id:
        exited, exit_err = comfy_wait_prompt_exit(
            after_prompt_id, timeout=wait_timeout or 30)
        if not exited:
            return False, exit_err
    busy, queue_err = comfy_queue_busy()
    if busy:
        return False, queue_err or "ComfyUI队列非空，不能安全释放缓存"
    try:
        response = requests.post(
            f"{comfy_url()}/free",
            json={"unload_models": True, "free_memory": True}, timeout=30)
        response.raise_for_status()
        time.sleep(2)
        return True, None
    except Exception as exc:
        return False, f"ComfyUI释放显存失败: {exc}"

def comfy_missing_node_types(workflow):
    """Return custom node classes that the connected ComfyUI does not expose."""
    missing = []
    classes = sorted({str(node.get('class_type') or '') for node in workflow.values()
                      if isinstance(node, dict) and node.get('class_type')})
    for class_type in classes:
        try:
            encoded = requests.utils.quote(class_type, safe='')
            response = requests.get(f"{comfy_url()}/object_info/{encoded}", timeout=10)
            if response.status_code != 200 or class_type not in response.json():
                missing.append(class_type)
        except Exception:
            missing.append(class_type)
    return missing

def image_workflow_preflight():
    workflow = load_t2i_workflow().get('json', {})
    missing = comfy_missing_node_types(workflow)
    if not missing:
        return True, None
    hint = "；请在ComfyUI安装并启用 ComfyUI-GGUF" if any('gguf' in x.lower() for x in missing) else ""
    return False, "资产生图缺少节点: " + ", ".join(missing) + hint

def is_comfy_oom(error):
    text = str(error or '').lower()
    return any(token in text for token in (
        'out of memory', 'allocation on device', 'would exceed allowed memory',
        'cuda error: out of memory', '显存不足'))

# ============================== 千问2512 文生图 ==============================
# 画幅 → 资产生图尺寸（竖屏短剧时角色/场景图也必须是竖版）
ASPECT_IMG_SIZE = {
    "16:9 (Widescreen)": (1280, 720),
    "9:16 (Portrait)":   (720, 1280),
    "9:16 (Portrait Widescreen)": (720, 1280),
    "1:1 (Square)":      (960, 960),
    "4:3 (Standard)":    (1088, 816),
}

def image_workflow_files():
    try:
        return sorted(name for name in os.listdir(WORKFLOWS_DIR)
                      if re.fullmatch(r't2i_[A-Za-z0-9_.-]+\.json', name)
                      and os.path.isfile(os.path.join(WORKFLOWS_DIR, name)))
    except OSError:
        return []

def get_image_workflow_name():
    selected = str(CONFIG.get('image_workflow') or 't2i_qwen2512.json').strip()
    return selected if selected in image_workflow_files() else 't2i_qwen2512.json'

def load_t2i_workflow(name=None):
    selected = name or get_image_workflow_name()
    if selected not in image_workflow_files():
        raise ValueError('未知的生图工作流')
    with open(os.path.join(WORKFLOWS_DIR, selected), 'r', encoding='utf-8') as f:
        workflow = json.load(f)
    if not isinstance(workflow.get('json'), dict) or not isinstance(workflow.get('map'), dict):
        raise ValueError(f'生图工作流格式无效: {selected}')
    return workflow

def image_model_loader(workflow):
    """Find the primary diffusion/checkpoint loader used by a T2I adapter."""
    for node_id, node in workflow.get('json', {}).items():
        if not isinstance(node, dict):
            continue
        inputs = node.get('inputs') or {}
        for input_name in ('unet_name', 'ckpt_name'):
            if isinstance(inputs.get(input_name), str):
                return node_id, node.get('class_type', ''), input_name, inputs[input_name]
    return None

def image_workflow_catalog():
    catalog = []
    for name in image_workflow_files():
        try:
            workflow = load_t2i_workflow(name)
            loader = image_model_loader(workflow)
            if not loader:
                continue
            _, class_type, input_name, default_model = loader
            meta = workflow.get('meta') or {}
            options = comfy_loader_models(class_type)
            prefix = str(meta.get('model_prefix') or '').replace('\\', '/').strip('/')
            if prefix:
                options = [item for item in options
                           if item.replace('\\', '/').startswith(prefix + '/')]
            catalog.append({
                'id': name,
                'label': str(meta.get('label') or os.path.splitext(name)[0]),
                'family': str(meta.get('family') or 'custom'),
                'loader': class_type,
                'input': input_name,
                'default_model': default_model,
                'models': options,
            })
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    return catalog

def apply_image_model(workflow):
    selected = str(CONFIG.get('image_model') or '').strip()
    loader = image_model_loader(workflow)
    if not selected:
        return loader[3] if loader else ''
    if not loader:
        raise ValueError('生图工作流中未找到模型加载器')
    node_id, class_type, input_name, default_model = loader
    available = comfy_loader_models(class_type)
    if available and selected not in available:
        raise ValueError(f'ComfyUI 未检测到所选生图模型: {selected}')
    workflow['json'][node_id].setdefault('inputs', {})[input_name] = selected
    return selected

def gen_image(prompt, width=None, height=None, seed=None, save_name=None, progress_cb=None,
              project_pid=None, run_id=None):
    """千问2512生成参考图，返回本地保存路径。尺寸默认跟随全局画幅设置"""
    t2i = load_t2i_workflow()
    wf = copy.deepcopy(t2i['json'])
    try:
        apply_image_model({'json': wf, 'map': t2i['map']})
    except ValueError as exc:
        return None, str(exc)
    missing = comfy_missing_node_types(wf)
    if missing:
        hint = "；请安装并启用 ComfyUI-GGUF" if any('gguf' in x.lower() for x in missing) else ""
        return None, "资产生图缺少节点: " + ", ".join(missing) + hint
    mp = t2i['map']
    wf[mp['prompt'][0]]['inputs'][mp['prompt'][1]] = prompt
    if 'negative' in mp:
        pass  # 负面词用工作流内置
    wf[mp['seed'][0]]['inputs'][mp['seed'][1]] = seed if seed is not None else random.randint(1, 2**62)
    if not width or not height:
        width, height = ASPECT_IMG_SIZE.get(CONFIG.get('aspect_ratio', '16:9 (Widescreen)'), (1280, 720))
    wf[mp['width'][0]]['inputs'][mp['width'][1]] = width
    wf[mp['height'][0]]['inputs'][mp['height'][1]] = height
    if project_pid and run_id:
        raise_if_project_stopped(project_pid, run_id)
    client_id = uuid.uuid4().hex
    ws = None
    try:
        ws = comfy_open_ws(client_id)
    except Exception as e:
        print(f"[ComfyUI] 图像进度通道连接失败，继续使用历史轮询: {e}")
    registered = False
    try:
        pid = comfy_submit(wf, client_id=client_id)
        if project_pid and run_id:
            registered = bool(register_project_prompt(project_pid, run_id, pid, 'image'))
            if not registered:
                comfy_cancel_prompt_safely(pid)
                raise ProjectStopRequested('任务归属已变化，旧图片任务已停止')
        history, err = comfy_wait(
            pid, timeout=1800, progress_cb=progress_cb, ws=ws,
            stop_check=(lambda: project_stop_requested(project_pid, run_id)) if project_pid and run_id else None,
            stop_handler=(lambda prompt_id: cancel_owned_project_prompt(
                project_pid, run_id, prompt_id)) if project_pid and run_id else None)
    finally:
        try:
            if registered:
                clear_project_prompt(project_pid, run_id, pid)
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
    stopped = production_stop_message(err)
    if stopped is not None:
        raise ProjectStopRequested(stopped)
    if err:
        return None, err
    images = []
    for nid, out in history.get('outputs', {}).items():
        for img in out.get('images', []):
            images.append(img)
    if not images:
        return None, "ComfyUI未返回图像"
    if save_name is None:
        save_name = f"img_{uuid.uuid4().hex[:8]}.png"
    save_path = os.path.join(ASSETS_DIR, save_name)
    comfy_download(images[0], save_path)
    return save_path, None

# ============================== MiniMax H3 r2v 视频 ==============================
def load_r2v_workflow():
    with open(os.path.join(WORKFLOWS_DIR, 'h3_r2v.json'), 'r', encoding='utf-8') as f:
        return json.load(f)

def get_h3_aspect_ratio():
    aspect = CONFIG.get('aspect_ratio', '16:9 (Widescreen)')
    return H3_ASPECT_RATIO_ALIASES.get(aspect, aspect)

def get_h3_steps():
    """采样步数 clamp 4~25"""
    try:
        return max(4, min(int(CONFIG.get('h3_steps', 10)), 25))
    except (TypeError, ValueError):
        return 10

def h3_frames_from_duration(seconds):
    """H3帧数对齐：17的倍数+5"""
    frames = max(5, round(seconds * 24))
    return min(362, frames + (5 - frames % 17) % 17)

def gen_video_r2v(prompt, ref_image_paths, duration=None, seed=None, save_name=None,
                  progress_cb=None, preserve_prompt=False, project_pid=None, run_id=None):
    """MiniMax H3 r2v生成视频。ref_image_paths[0]=Picture 1(角色), [1]=Picture 2(场景)..."""
    wf = copy.deepcopy(load_r2v_workflow())
    apply_h3_model(wf, 'r2v')
    if not duration:
        cfg_dur = CONFIG.get('shot_duration', 8)
        duration = cfg_dur if isinstance(cfg_dur, (int, float)) else 8
    duration = min(max(3, int(duration)), 15)
    # 主程序“完整H3提示词/时间轴自动拆片”模式来自用户原文，不能再做关键词清洗。
    if not preserve_prompt:
        prompt = filter_slow_motion(prompt)

    # 提示词
    wf['138']['inputs']['value'] = prompt
    # 种子
    wf['129']['inputs']['noise_seed'] = seed if seed is not None else random.randint(1, 2**62)
    # 时长（帧数对齐后写回132，让131表达式算出对齐值；直接写帧数更稳：删131链，直接写136.length）
    frames = h3_frames_from_duration(duration)
    wf['136']['inputs']['length'] = frames
    for nid in ['131', '132']:
        wf.pop(nid, None)
    # 分辨率
    if '115' in wf:
        wf['115']['inputs']['aspect_ratio'] = get_h3_aspect_ratio()
        wf['115']['inputs']['megapixels'] = CONFIG.get('megapixels', 0.92)
    # 采样步数
    if '124' in wf:
        wf['124']['inputs']['steps'] = get_h3_steps()
    # 参考图：先删原有137/139连接，统一重建（H3 r2v 官方支持最多9张）
    refs = [p for p in ref_image_paths if p and os.path.exists(p)][:9]
    if not refs:
        return None, "无有效参考图"
    inputs136 = wf['136']['inputs']
    for k in list(inputs136.keys()):
        if k.startswith('ref_images.ref_image_'):
            del inputs136[k]
    next_node_id = 300
    for i, path in enumerate(refs):
        comfy_name = comfy_upload_image(path)
        load_nid = str(next_node_id); next_node_id += 1
        wf[load_nid] = {"inputs": {"image": comfy_name}, "class_type": "LoadImage", "_meta": {"title": f"参考图{i+1}"}}
        inputs136[f'ref_images.ref_image_{i}'] = [load_nid, 0]
    # 旧的137/139节点如无引用可移除
    for old in ['137', '139']:
        wf.pop(old, None)

    # Qwen-Image或上一段H3可能仍占用缓存。队列为空时先释放，再提交本段。
    freed, free_err = comfy_free_memory()
    if not freed:
        print(f"[ComfyUI] 提交前未释放缓存: {free_err}")
    history = None
    err = None
    attempts = 2 if CONFIG.get('h3_oom_retry', True) else 1
    for attempt in range(attempts):
        if project_pid and run_id:
            raise_if_project_stopped(project_pid, run_id)
        client_id = uuid.uuid4().hex
        ws = None
        try:
            ws = comfy_open_ws(client_id)
        except Exception as e:
            print(f"[ComfyUI] 视频进度通道连接失败，继续使用历史轮询: {e}")
        registered = False
        try:
            try:
                pid = comfy_submit(wf, client_id=client_id)
            except Exception as exc:
                return None, str(exc)
            if project_pid and run_id:
                registered = bool(register_project_prompt(project_pid, run_id, pid, 'video'))
                if not registered:
                    comfy_cancel_prompt_safely(pid)
                    raise ProjectStopRequested('任务归属已变化，旧视频任务已停止')
            history, err = comfy_wait(
                pid, timeout=1800, interval=5, progress_cb=progress_cb, ws=ws,
                stop_check=(lambda: project_stop_requested(project_pid, run_id)) if project_pid and run_id else None,
                stop_handler=(lambda prompt_id: cancel_owned_project_prompt(
                    project_pid, run_id, prompt_id)) if project_pid and run_id else None)
        finally:
            try:
                if registered:
                    clear_project_prompt(project_pid, run_id, pid)
            finally:
                if ws is not None:
                    try:
                        ws.close()
                    except Exception:
                        pass
        stopped = production_stop_message(err)
        if stopped is not None:
            raise ProjectStopRequested(stopped)
        if not err:
            break
        if attempt + 1 >= attempts or not is_comfy_oom(err):
            return None, err
        print("[ComfyUI] 当前片段显存不足，正在释放模型缓存并自动重试一次...")
        freed, free_err = comfy_free_memory(after_prompt_id=pid, wait_timeout=30)
        if not freed:
            return None, f"{err}；自动释放显存失败: {free_err}"
    if err:
        return None, f"{err}；已自动释放显存并重试1次"
    videos = []
    for nid, out in history.get('outputs', {}).items():
        for key in ('videos', 'gifs', 'images'):
            for v in out.get(key, []):
                videos.append(v)
    if not videos:
        return None, "ComfyUI未返回视频"
    if save_name is None:
        save_name = f"shot_{uuid.uuid4().hex[:8]}.mp4"
    return videos[0], save_name  # 由调用方决定保存目录

# ============================== 单镜头生成器（r2v / i2v / t2v） ==============================
SINGLE_SHOT_PROMPT = """你是MiniMax H3视频生成模型的提示词专家。MiniMax H3是全模态视频生成模型：一次生成同时产出画面与原生音频（对白、音效、音乐同出）。用户会给你一段口语化的创作要求，你需要在内部连续完成两步工作，最终只输出第二步的产物——H3{mode_label}提示词。

【内部第一步：需求整理（思考过程，不要输出）】
把用户的口语要求整理成具象拍摄方案：
1. 用户用"图1/图2/第一张图"等任何方式引用参考图时，编号是关键锚点必须保留，统一规范为"图N"写法（与图片上传顺序一致），禁止概括成"一个男人/一个场景"等丢失编号的表达
2. 抽象词必须转成具体可拍摄方案：动作类抽象词→直接设计具体动作序列（谁、用什么武器/招式、如何进攻、对方如何格挡/倒地，写清肢体部位+运动方向+发力方式）；氛围类→具体视觉元素+光影；情绪类→具体面部/肢体表现。禁止输出"炫酷/华丽/震撼/激烈/唯美/霸气"等抽象形容词本身
3. 禁止原文照抄：即使用户输入已比较具体，也必须进一步拆解深化——每个招式/动作拆成"起势→发力→击中→收势"的物理步骤；补充物理反馈细节（衣物飘动/地面尘土/汗水飞溅/受力踉跄/冲击气浪）；把笼统的"镜头切换"落实为具体运镜安排（哪段动作配哪种景别）。整理后的信息量必须明显大于原文
4. 完整保留用户的所有实质性要求（动作、互动关系、场景、环境、台词），只去除口语化语气词

【内部第二步：生成H3提示词（唯一需要输出的内容）】
把整理好的方案写成严格符合H3官方训练格式的提示词，由以下三个字段组成，按此顺序、各起一段（字段名英文一字不差）：

integrated_multimodal_description: 开头一句定调整体风格与初始构图（如"写实电影感，暖色调浅景深"），随后沿时间轴把内容拆成 **2~4 个带标题时间码的镜头段**（[Shot 1] → [Shot 2] → ...），用不同景别/视角/机位呈现动作推进。时长{duration}秒内把切点均匀铺开（首镜头不晚于1秒，末镜头在最后1秒收束）。每个镜头段写清该段画面、主体动作、镜头运动、说话人与台词、同步音效。
overall_soundscape: 用1-3句中文概括全片环境音、物理动作音、非语言人声（风雨/脚步/布料摩擦/撞击/呼吸等）。对白已写在上个字段，此处禁止重复。
non_diegetic_music: 用1-2句中文描述只有观众能听到的配乐（乐器编制、速度、力度起伏）；开头禁止重锤鼓点；无配乐写 N/A。

【硬性规则】
1. 结构标记与固定术语用英文（字段名、[Shot N]、时间码、The camera 运镜句式、(S1)说话人ID、<d>台词标签、<Picture N>引用标签）；画面/动作/场景等描述性文字用中文
2. 🔴台词（最高优先级）：全部由角色用中文（标准普通话）说出，逐字放入 <d>[Chinese] ...</d>——这是H3唯一的对白触发标记，没用 <d>[Chinese] 包裹的台词模型不会发声，等于丢失台词；用户要求里的每句台词必须完整写入，一字不差，禁止遗漏/改写/翻译；原创台词自然口语化（10-25字）。凡开口的角色分配稳定ID (S1)、(S2)…，首次出现时给足身份描述（角色类型/年龄/性别/音色）；语气/情绪描述写在 <d> 之外。✅正确格式：(S1) 银发剑客，冷峻中年男性，声音低沉沙哑 <d>[Chinese] 这笔账改日再算</d>　❌禁止格式：老板娘说道："趁热吃"（缺 <d>[Chinese] 标记，台词将不发声）。无台词则不写<d>段
3. {pic_rule}
4. 🔄切镜（多镜头是H3的最大优势，必须用足）：在 {duration} 秒内策划 **2~4 次硬切**（hard cut），写法 "[Shot 2] At 00:03.000, the camera cuts to a close-up of ..."。切镜时机=有**新信息**出现时：景别跃迁（全景切特写）、视角换向（正对切侧打）、主体聚焦（两人同框切单人面部）、动作落点（起跳切落地）等。只在距离/角度微调时用连续运镜过渡，不要为微小变化切镜。每个镜头段标注起始时间码，首段时间码为 00:00.000 或省略，后续段时间码必须递增且不超出总时长{duration}秒。切镜不是慢放，切点仍保持动作连续流畅
5. 运镜用 The camera 英文句式自然写入各镜头段：Push In推近 / Pull Out拉远 / Pan Left/Right水平摇 / Truck Left/Right横移 / Tilt Up/Down俯仰 / Arc Shot环绕 / Tracking Shot跟拍 / Static Shot固定 等；每个镜头段一种主运镜，与切镜搭配
6. 动作写成连续流动的一条线：一个动作没结束就长出下一个，任何一帧都不"停住摆姿势"；【开场即运动】首帧必须是动作正在进行中的瞬间（手已抬起/身体已前倾/武器已挥出），严禁静止站姿开场。【节奏铁律】所有动作按现实世界正常速度或更快执行，干脆利落、有爆发力，禁止舒缓悠长的节奏；禁止"定格/凝滞/静帧/蓄势待发/屏息以待"等静止构图表述——即使要求写的是"准备、对峙、蓄力"这类静态时刻，也必须改写成正在进行中的动作，否则H3会渲染成冻结帧或慢放
7. 克制聚焦：每个镜头段聚焦该段核心动作/事件，每段2-4句写到位，禁止标签式堆砌细节；总时长内镜头段数量与信息量平衡
8. 节奏恰好适配{duration}秒时长：台词、切点、收尾画面铺满全片；结尾镜头有明确收束画面
9. 绝对禁止"慢动作/慢镜头/升格/子弹时间/slow motion/slo-mo"等一切慢放表述及120fps等高帧率描述
10. 🧩自包含（无上下文）：本镜是独立成片，H3看不见上一镜。凡"在人类看来顺理成章的延续信息"——角色外观/服装/手中物体/所处环境/光线氛围——均须每镜显式写全，不可省略。例：上一镜两人持枪，本镜即使只写"转身对轰"，也必须重申"两人各握黑色手枪"；否则换一镜枪就消失、环境就漂移。禁止用"如前/同上"式省略。只描述本镜画面，不出现"分镜""第N镜"等元语言（[Shot N]与时间码是官方格式标记，允许使用）
11. 直接输出三字段提示词正文，不要任何解释、标题、思考过程或markdown围栏

{refs_block}

【用户创作要求】
{idea}

直接输出提示词："""

def build_single_refs_block(mode, ref_count):
    """按模式构造参考素材说明与图片规则"""
    if mode == 'i2v':
        if ref_count >= 2:
            return ("【参考素材】2张关键帧：<Picture 1>=首帧（视频起始画面），<Picture 2>=尾帧（视频结束画面）",
                    "开头先声明：视频从<Picture 1>的画面开始，保持其构图、主体外貌与场景一致，按动作描述发展，结尾自然过渡到<Picture 2>的画面收束")
        return ("【参考素材】1张首帧图（即<Picture 1>，视频的起始画面）",
                "开头先声明：视频从<Picture 1>的画面开始，保持其构图、主体外貌与场景一致，随后按动作描述发展")
    if mode == 't2v':
        return ("【参考素材】无参考图，纯文本生成",
                "画面描述必须自包含：主体外貌（性别/年龄/发型/服装）、环境、光线全部写清")
    # r2v（最多9张）
    refs = "、".join(f"<Picture {i+1}>" for i in range(max(1, ref_count)))
    return (f"【参考素材】共{ref_count}张参考图：{refs}（图片已附在本消息中，按此顺序，先看图再写提示词）",
            f"开头先声明素材用途：使用<Picture 1>作为画面主体参考（其余按顺序作为场景/道具/其他角色参考），画面中人物外貌、服装、场景必须与参考图保持一致；用户要求中的『图1/图2/第一张图』等说法按上传顺序对应<Picture 1>/<Picture 2>…，禁止错位或丢失编号；提示词中引用参考图时只允许使用<Picture 1>~<Picture {ref_count}>编号")

def load_h3_workflow(name):
    with open(os.path.join(WORKFLOWS_DIR, f'h3_{name}.json'), 'r', encoding='utf-8') as f:
        return json.load(f)

def gen_video_single(mode, prompt, ref_paths, duration, seed=None):
    """单镜头三模式视频生成。返回(comfy视频元信息dict, err)"""
    prompt = filter_slow_motion(prompt)
    if seed is None:
        seed = random.randint(1, 2**62)
    duration = min(max(3, int(duration)), 15)
    frames = h3_frames_from_duration(duration)
    try:
        if mode == 'r2v':
            # 复用主管线的r2v实现（工作流结构一致）
            vinfo, r2v_err = gen_video_r2v(prompt, ref_paths, duration=duration, seed=seed)
            return (vinfo, None) if vinfo else (None, r2v_err or "r2v生成失败")
        wf = copy.deepcopy(load_h3_workflow(mode))
        apply_h3_model(wf, mode)
        if mode == 'i2v':
            if not ref_paths:
                return None, "i2v模式需要上传1张首帧图"
            wf["105:104"]["inputs"]["prompt"] = prompt
            wf["105:104"]["inputs"]["length"] = frames
            wf.pop("105:107", None); wf.pop("105:111", None)
            wf["105:15"]["inputs"]["noise_seed"] = seed
            comfy_name = comfy_upload_image(ref_paths[0])
            wf["114"]["inputs"]["image"] = comfy_name
            # 尾帧（可选）：ref_paths[1] 存在则接入 last_frame
            if len(ref_paths) > 1 and os.path.exists(ref_paths[1]):
                last_name = comfy_upload_image(ref_paths[1])
                wf["900"] = {"inputs": {"image": last_name}, "class_type": "LoadImage", "_meta": {"title": "尾帧"}}
                wf["105:104"]["inputs"]["last_frame"] = ["900", 0]
            # 输出尺寸由115分辨率选择器决定（与全局画幅一致）；119控制首帧缩放像素
            wf["115"]["inputs"]["aspect_ratio"] = get_h3_aspect_ratio()
            wf["115"]["inputs"]["megapixels"] = CONFIG.get('megapixels', 0.92)
            wf["119"]["inputs"]["megapixels"] = CONFIG.get('megapixels', 0.92)
            wf["105:9"]["inputs"]["steps"] = get_h3_steps()
        else:  # t2v
            wf["105:104"]["inputs"]["prompt"] = prompt
            wf["105:104"]["inputs"]["length"] = frames
            wf.pop("105:107", None); wf.pop("105:111", None)
            wf["105:15"]["inputs"]["noise_seed"] = seed
            wf["115"]["inputs"]["aspect_ratio"] = get_h3_aspect_ratio()
            wf["115"]["inputs"]["megapixels"] = CONFIG.get('megapixels', 0.92)
            wf["105:9"]["inputs"]["steps"] = get_h3_steps()
        pid = comfy_submit(wf)
        history, err = comfy_wait(pid, timeout=1800, interval=5)
        if err:
            return None, err
        videos = []
        for nid, out in history.get('outputs', {}).items():
            for key in ('videos', 'gifs', 'images'):
                for v in out.get(key, []):
                    videos.append(v)
        if not videos:
            return None, "ComfyUI未返回视频"
        return videos[0], None
    except Exception as e:
        return None, str(e)

SINGLE_TASKS = {}  # task_id -> {"status": running/done/error, "msg": str, "video_url": str}

def single_shot_worker(task_id, mode, prompt, ref_paths, duration):
    t = SINGLE_TASKS[task_id]
    try:
        if exclusive_on():
            t['msg'] = "互斥模式：关闭LLM释放显存..."
            stop_local_llm()
        t['msg'] = "检查ComfyUI服务..."
        if not ensure_comfyui():
            t['status'] = 'error'
            t['msg'] = "ComfyUI未运行且内置整合包启动失败"
            return
        t['msg'] = f"MiniMax H3 {mode} 渲染中（约{duration}秒视频，请耐心等待）..."
        vinfo, err = gen_video_single(mode, prompt, ref_paths, duration)
        if err:
            t['status'] = 'error'
            t['msg'] = err
            return
        out_dir = os.path.join(OUTPUTS_DIR, 'single')
        os.makedirs(out_dir, exist_ok=True)
        fname = f"single_{uuid.uuid4().hex[:8]}.mp4"
        save_path = os.path.join(out_dir, fname)
        comfy_download(vinfo, save_path)
        t['status'] = 'done'
        t['msg'] = "生成完成"
        t['video_url'] = f"/file/outputs/single/{fname}"
        t['local_path'] = os.path.join('outputs', 'single', fname)
    except Exception as e:
        t['status'] = 'error'
        t['msg'] = str(e)

# ============================== 剧本解析（LLM） ==============================
SCRIPT_PARSE_PROMPT = """你是一位顶级短剧编剧兼导演。用户会给你任意形式的创意输入（一句话、故事梗概、场景描述或完整剧本），你必须将其扩写/拆解为一份可直接交给 MiniMax H3 生产的结构化短剧剧本。

【生产单位】
- 最终生产单位是“剧情片段（segment）”，不是单个摄影镜头。每个片段只提交 H3 一次，生成一条约 8～15 秒、内部包含 3～6 个连续摄影镜头的短片。
- 为兼容现有程序，JSON 仍使用 shots 数组；但数组中的每一项代表 EP01-01、EP01-02 这样的“片段”。不要把一个近景、特写或单句反应拆成独立数组项。
- 一个片段应在同一主要场景中完成一个小型叙事闭环：建立位置 → 推进动作/对白 → 反应或转折 → 明确收尾。片段内部的景别变化和正反打由后续 H3 提示词编译器完成。

【铁律】
1. 动作必须具象化：绝不使用"炫酷、激烈、帅气、霸气"等抽象词，必须写成具体动作（肢体部位+运动方向+发力方式+物理反馈）。例如"打斗激烈"→"他侧身避开直拳，顺势抓住对方手腕反拧到背后，将其压在墙上，地面尘土被脚步带起"
2. 严禁慢放：剧本与描述中绝对禁止"慢动作/慢镜头/升格/子弹时间/slow motion"等任何慢放表述
3. 台词必须中文，口语化、短促有力，符合角色性格
%DURATION_RULE%
%COUNT_RULE%
6. 角色外貌描述必须详细具体（性别、年龄段、人种与面部特征、发型发色、脸型、服装、标志性配饰），后续要用于AI生成角色参考图，必须保证全剧描述一致。特别注意：人种必须根据故事背景明确写出（如中国故事写"中国北方男人，东亚面孔"，欧美故事写"欧美面孔"），不得省略——否则生图模型会默认生成错误人种
7. 场景必须细分到具体拍摄点：不要只给一个笼统大场景。若剧情在同一大环境下的不同位置发生（如"街道"与"街道-瓜摊角落"、"房间"与"房间-窗边"），必须拆成多个独立场景条目分别描述，每个场景的构图、光线、关键陈设要具体到可直接出图
8. 关键道具：剧情中反复出现或推动剧情的具体物件（如凶器、信物、食物、交通工具等），必须列入props数组并给出外观描述，后续要生成道具参考图
9. 跨片连续性：逐片判断下一片是否必须从上一片最后一帧无缝开始。只有同一地点、近乎连续的时间、相同角色/服装/道具状态且动作或对话明显未结束时，下一片填写continue_from_previous=true并说明continuity_reason；换场、时间跳跃、蒙太奇、独立建立镜头必须为false。不要为了“角色一致性”而滥用尾帧，人物身份由角色参考图负责。

【输出格式】严格输出一个JSON对象（不要输出其他任何文字、不要用markdown代码块包裹）：
{
  "title": "剧名",
  "synopsis": "一句话剧情简介",
  "characters": [
    {"name": "角色名", "appearance": "详细外貌服装描述（用于生成参考图，全剧统一）", "personality": "性格关键词"}
  ],
  "scenes": [
    {"name": "场景名", "description": "纯环境描述：具体位置、时间、光线、氛围、建筑风格、关键陈设与静物（用于生成空的场景参考图，只允许写环境元素，严禁出现任何人物及其活动、身影、人群等字眼）"}
  ],
  "props": [
    {"name": "道具名", "description": "外观详细描述：材质、颜色、大小、特征（用于生成道具参考图）；无关键道具则输出空数组[]"}
  ],
  "shots": [
    {
      "index": 1,
      "segment_id": "EP01-01",
      "scene": "场景名（必须与scenes中的name一致）",
      "characters": ["本片段所有出场角色名（出场几个写几个）"],
      "props": ["本片段实际使用/特写的道具名（必须与props中的name一致，无则[]）"],
      "camera": "片段镜头序列类型（如：对话正反打/紧张收紧/跟随动作/建立到特写）",
      "action": "本片段完整的动作、信息与情绪推进（具象描述开场位置、连续动作、反应、转折和收尾；供后续拆成3～6个内部镜头）",
      "dialogue": [{"speaker": "角色名", "line": "观众实际听到的逐字台词", "tone": "语气"}],
      "continue_from_previous": false,
      "continuity_reason": "仅在同一时空、动作或人物状态需要从上一片段最后一帧无缝延续时说明原因；换场、时间跳跃或独立段落必须为false",
      "duration": 10
    }
  ]
}

dialogue字段只能包含观众实际听到的逐字中文台词。没有对白时必须写空数组[]；咕噜声、喘息、笑声、哭声等非语言声音写入action，禁止用“无台词”或声音说明占位。

用户输入：
"""

def parse_json_from_text(text):
    """从LLM输出中提取JSON对象"""
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    start = text.find('{')
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == '{':
            depth += 1
        elif text[i] == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i+1])
                except Exception:
                    return None
    return None

H3_PROMPT_FORMAT_VERSION = "ep01-english-natural-dialogue-v1"
H3_PROMPT_SECTIONS = (
    "【Important】", "【Storyboard】", "【Character Voice】",
    "【Sound】", "【Forbidden】", "【Mandatory】"
)

H3_PROMPT_TEMPLATE = """You are the H3 Prompt Compiler for a live-action short-drama pipeline. Convert the supplied episode-segment data and ordered reference pictures into one executable MiniMax H3 ReferenceToVideo prompt. One output prompt creates one complete 8–15 second story segment containing multiple camera shots. Return only the prompt, without analysis, Markdown fences, or commentary.

This format was validated in production on EP01-06. Follow it literally.

REFERENCE MAP:
{refs_desc}

REQUIRED SECTION ORDER:
【Important】
【Storyboard】
【Character Voice】
【Sound】
【Forbidden】
【Mandatory】

PRODUCTION RULES:
1. Write every heading, action, camera instruction, voice description, sound instruction, and restriction in English. Chinese may appear only as the exact source dialogue that the character audibly speaks.
2. Start the prompt with one plain reference line per supplied picture, for example `Picture 1 as Character 1 reference.` or `Picture 3 as scene reference.` Use only supplied Picture indices.
3. In 【Important】 state: no subtitles, captions, dialogue text, text overlay, or readable written words; all dialogue exists only as natural human voice.
4. In 【Storyboard】 first establish character positions and reference continuity, then write {internal_shot_range} concise internal camera shots spanning exactly {duration} seconds. Use `Shot N (start-end s) — shot type`. Together the shots must form one coherent mini-scene with an opening state, development, reaction or turn, and end state. Every shot must contain observable action, camera behavior, and physical continuity. Do not explain plot themes or internal reasoning.
5. Dialogue uses only this natural instruction form:
   `Character N speaks naturally in Chinese:`
   `“exact source line”`
   The quoted line contains only words the audience should hear. Never use character-name-colon screenplay notation.
6. Never output `(S1)`, `(S2)`, `<S1>`, `<S2>`, `<d>`, `[Chinese]`, XML dialogue tags, or speaker tokens. Never attach a line of dialogue to a reusable character description.
7. Each source dialogue line appears exactly once in the entire prompt and only inside the relevant Storyboard shot. Do not repeat dialogue in Important, Character Voice, Sound, Forbidden, or Mandatory. If the source has no dialogue, invent no speech, narration, voiceover, lyrics, or quoted words.
8. Never write `pause`, `brief pause`, `停顿`, `停顿一拍`, or other stage-direction words beside dialogue. Show hesitation as visible behavior in a separate English sentence, such as looking away, tightening a grip, or taking a breath.
9. In 【Character Voice】 give one separate English voice profile per speaking character. Describe age range, pitch, texture, pace, emotion, and forbidden delivery styles. Voice profiles contain no dialogue and no plot instructions.
10. In 【Sound】 write `overall_soundscape:` followed only by ambience, physical sounds, and non-verbal human sounds. Write `non_diegetic_music: N/A` unless music is explicitly requested. Never mention dialogue content or repeat a character description here.
11. In 【Forbidden】 include subtitles, captions, dialogue text, text overlay, readable text, watermark, logo, extra characters, face swapping, character drift, changed clothing, and changed environment layout.
12. In 【Mandatory】 require natural spoken dialogue, no written dialogue, reference-consistent identity/clothing/environment, real-time natural movement, and the requested audio policy. Do not repeat any source dialogue.
13. Preserve reference identity, facial features, hairstyle, clothing, body proportions, environment layout, props, and assigned Character numbers across every shot. Do not add unrequested characters or events.
14. Keep the prompt compact. Do not output official six-field schema labels, `integrated_multimodal_description`, `<Subject N>`, explanations, examples, or any text outside the requested prompt.
"""

def load_image_parts(paths, limit=9):
    """读取本地图片构造多模态content parts（data URL），读取失败的跳过"""
    import base64
    parts = []
    for p in (paths or [])[:limit]:
        try:
            ap = p if os.path.isabs(p) else os.path.join(BASE_DIR, p)
            with open(ap, 'rb') as fp:
                b64 = base64.b64encode(fp.read()).decode()
            ext = os.path.splitext(ap)[1].lower().lstrip('.')
            mime = {'png': 'image/png', 'webp': 'image/webp'}.get(ext, 'image/jpeg')
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        except Exception as e:
            print(f"[H3提示词] 参考图读取失败 {p}: {e}")
    return parts

def repair_missing_dialogue(sys_text, user_content, content, missing):
    """单镜头生成器旧入口的台词兜底；主短剧流水线使用下方严格格式校验。"""
    fixed, _ = llm_chat([
        {"role": "system", "content": sys_text},
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": content},
        {"role": "user", "content": f"你漏掉了必须逐字保留的台词：{'、'.join(missing)}。请把每句台词逐字用 <d>[Chinese] 台词原文</d> 补入 integrated_multimodal_description 中对应说话人开口的时刻（说话人标注 (S1)/(S2)），其余内容保持不变，重新输出完整的三字段提示词。"}
    ], max_tokens=1800, temperature=0.3)
    if fixed and all(m in fixed for m in missing):
        return fixed.strip()
    print("[H3提示词] 台词修复后仍缺失，保留初稿")
    return content

def _strip_expected_dialogue(text, dialogue_lines):
    """从提示词副本中剔除合法中文对白，便于检测是否混入中文导演指令。"""
    cleaned = text
    for line in sorted(set(dialogue_lines), key=len, reverse=True):
        cleaned = cleaned.replace(line, '')
    return cleaned

def validate_h3_production_prompt(content, dialogue_lines, ref_count, include_warnings=False):
    """校验英文控制层+中文自然对白格式。

    默认只返回会阻止生产的硬错误； ``include_warnings`` 用于生成阶段的一次
    自动纠正。少量非对白中文通常来自中文角色名或资产名，不应在纠正后继续
    卡死整个项目，但仍会留下日志供后续优化提示词。
    """
    text = (content or '').strip()
    issues = []
    warnings = []
    if not text:
        return ["输出为空"]

    positions = [text.find(section) for section in H3_PROMPT_SECTIONS]
    missing_sections = [section for section, pos in zip(H3_PROMPT_SECTIONS, positions) if pos < 0]
    if missing_sections:
        issues.append("缺少分区: " + "、".join(missing_sections))
    elif positions != sorted(positions):
        issues.append("分区顺序错误")

    banned_patterns = {
        r'<\/?d\b[^>]*>': '<d>标签',
        r'<S\d+>': '<Sx>标签',
        r'\(S\d+\)': '(Sx)标签',
        r'\[Chinese\]': '[Chinese]标签',
        r'<Subject\s+\d+>': '<Subject N>标签',
        r'integrated_multimodal_description': '旧三字段格式',
        r'(?i)\bbrief\s+pause\b': 'Brief pause舞台词',
        r'停顿(?:一拍|片刻|几秒)?': '中文停顿舞台词',
    }
    for pattern, label in banned_patterns.items():
        if re.search(pattern, text):
            issues.append("包含禁用内容: " + label)

    picture_ids = [int(match.group(1)) for match in
                   re.finditer(r'(?:<Picture|Picture)\s+(\d+)>?', text, re.I)]
    if picture_ids and (min(picture_ids) < 1 or max(picture_ids) > max(ref_count, 1)):
        issues.append(f"引用了不存在的参考图（允许1-{ref_count}）")

    dialogue_lines = [line.strip() for line in dialogue_lines if line and line.strip()]
    for line in dialogue_lines:
        count = text.count(line)
        if count != 1:
            issues.append(f"台词应恰好出现1次，实际{count}次: {line[:28]}")

    quote_blocks = re.findall(r'[“\"]([^”\"\r\n]+)[”\"]', text)
    expected = set(dialogue_lines)
    invented = [q.strip() for q in quote_blocks if q.strip() and q.strip() not in expected]
    if invented:
        issues.append("出现未授权引号内容: " + "、".join(invented[:3]))
    if not dialogue_lines and re.search(r'(?i)\b(?:speaks?|says?|asks?|replies?)\b', text):
        issues.append("无台词镜头出现说话指令")

    without_dialogue = _strip_expected_dialogue(text, dialogue_lines)
    if re.search(r'[\u4e00-\u9fff]', without_dialogue):
        warnings.append("除真实台词外仍含中文控制文字")

    storyboard_pos = text.find("【Storyboard】")
    voice_pos = text.find("【Character Voice】")
    if storyboard_pos >= 0 and voice_pos > storyboard_pos:
        storyboard = text[storyboard_pos:voice_pos]
        for line in dialogue_lines:
            if line not in storyboard:
                issues.append(f"台词不在Storyboard分区: {line[:28]}")

    result = issues + (warnings if include_warnings else [])
    return list(dict.fromkeys(result))

def repair_h3_production_prompt(sys_text, user_content, content, issues,
                                dialogue_lines, ref_count, stop_check=None):
    """格式不合格时低温重编译一次；仍不合格则拒绝把污染提示词送入H3。"""
    issue_text = "\n".join(f"- {item}" for item in issues)
    repair_request = (
        "The draft failed production validation:\n" + issue_text +
        "\nRewrite the complete prompt from scratch. Preserve each supplied Chinese dialogue line "
        "exactly once, only inside Storyboard. Use English everywhere else. Remove every old "
        "speaker/XML tag, screenplay name-colon line, dialogue repetition, and pause direction. "
        "Return only the complete corrected prompt in the required six-section order."
    )
    if stop_check and stop_check():
        raise ProjectStopRequested('当前项目已收到停止请求')
    fixed, err = llm_chat([
        {"role": "system", "content": sys_text},
        {"role": "user", "content": user_content},
        {"role": "assistant", "content": content},
        {"role": "user", "content": repair_request}
    ], max_tokens=3000, temperature=0.2, stop_check=stop_check)
    if not fixed:
        return None, err or "提示词格式修复无返回"
    fixed = fixed.strip()
    remaining = validate_h3_production_prompt(
        fixed, dialogue_lines, ref_count, include_warnings=True)
    hard_errors = validate_h3_production_prompt(fixed, dialogue_lines, ref_count)
    if hard_errors:
        return None, "H3提示词格式校验失败: " + "；".join(hard_errors[:5])
    if remaining:
        print("[H3提示词] 软警告，自动纠正后仍有少量非对白中文，已放行: "
              + "；".join(remaining[:5]))
    return fixed, None

def assemble_shot_refs(shot, assets, use_storyboard=True,
                       continuity_path=None, reserve_continuity=False):
    """组装片段参考图；可选分镜图、上一片段尾帧依次放在末尾。"""
    idx = shot.get('index')
    board = assets.get(f"board_{idx}", {}) if idx is not None and use_storyboard else {}
    board_path = board.get('path') if board else None
    if board_path and not os.path.exists(board_path):
        board_path = None
    continuity_reserved = bool(continuity_path or reserve_continuity)
    reserved_slots = (1 if board_path else 0) + (1 if continuity_reserved else 0)
    base_limit = max(0, 9 - reserved_slots)
    manual_keys = shot.get('manual_ref_keys') or []
    if manual_keys:
        ref_paths = [assets[k]['path'] for k in manual_keys
                     if k in assets and assets[k].get('path') and os.path.exists(assets[k]['path'])
                     and assets[k]['path'] != board_path][:base_limit]
        board_index = None
        if board_path:
            ref_paths.append(board_path)
            board_index = len(ref_paths)
        continuity_index = len(ref_paths) + 1 if continuity_reserved else None
        if continuity_path:
            ref_paths.append(continuity_path)
        return ref_paths, board_index, continuity_index
    ref_paths = []
    for ch in shot.get('characters', []):
        path = assets.get(f"char_{ch}", {}).get('path')
        if path and os.path.exists(path) and len(ref_paths) < base_limit:
            ref_paths.append(path)
    scene_key = f"scene_{shot.get('scene', '')}"
    scene_path = assets.get(scene_key, {}).get('path')
    if scene_path and os.path.exists(scene_path) and len(ref_paths) < base_limit:
        ref_paths.append(scene_path)
    for pn in shot.get('props', []):
        path = assets.get(f"prop_{pn}", {}).get('path')
        if path and os.path.exists(path) and len(ref_paths) < base_limit:
            ref_paths.append(path)
    if board_path:
        ref_paths.append(board_path)
        board_index = len(ref_paths)
    else:
        board_index = None
    continuity_index = len(ref_paths) + 1 if continuity_reserved else None
    if continuity_path:
        ref_paths.append(continuity_path)
    return ref_paths, board_index, continuity_index

def assemble_shot_ref_paths(shot, assets, use_storyboard=True):
    return assemble_shot_refs(shot, assets, use_storyboard)[0]

def apply_storyboard_reference(prompt, picture_index):
    """在提交H3前声明可选分镜图用途；不改写项目中保存的基础提示词。"""
    if not prompt or not picture_index:
        return prompt
    marker = f"Picture {picture_index} as the storyboard composition reference for this shot."
    if marker in prompt:
        return prompt
    instruction = (
        f"{marker} Preserve its camera angle, framing, character positions and action state. "
        "Use the dedicated character reference pictures for exact identity, facial features, hair, clothing and body proportions, "
        "and use the scene reference picture for stable environment layout."
    )
    return instruction + "\n\n" + prompt

def apply_continuity_reference(prompt, picture_index):
    """声明上一片段尾帧仅作为当前片段0秒的连续性锚点。"""
    if not prompt or not picture_index:
        return prompt
    marker = f"Picture {picture_index} as the opening-frame continuity reference for this segment."
    if marker in prompt:
        return prompt
    instruction = (
        f"{marker} At 0.00 seconds, begin from its exact pose, subject positions, camera framing, "
        "lighting, prop state and environment state, then continue the action naturally. "
        "Use the dedicated character pictures for identity, face, hair, clothing and body proportions."
    )
    return instruction + "\n\n" + prompt

def segment_needs_previous_tail(segment, prompt=''):
    """结构化决定优先；完整H3原文可用明确的连续性措辞触发。"""
    if segment.get('continue_from_previous') is True:
        return True
    value = str(segment.get('continue_from_previous', '')).strip().lower()
    if value in ('1', 'true', 'yes', '是'):
        return True
    return bool(H3_CONTINUITY_CUE_RE.search(prompt or segment.get('action', '') or ''))

def segment_label(segment, fallback_index=None):
    index = segment.get('index', fallback_index or 1)
    return str(segment.get('segment_id') or f"EP01-{int(index):02d}")

def extract_video_tail_frame(video_path, image_path):
    """用ffmpeg提取上一片段最后一帧，返回(None)或可读错误。"""
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return "未检测到ffmpeg，无法提取上一片段尾帧"
    source = video_path if os.path.isabs(video_path or '') else os.path.join(BASE_DIR, video_path or '')
    if not os.path.exists(source):
        return f"上一片段视频不存在: {source}"
    try:
        proc = subprocess.run(
            [ffmpeg, '-y', '-sseof', '-0.08', '-i', source, '-frames:v', '1', image_path],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        if proc.returncode != 0 or not os.path.exists(image_path):
            return "尾帧提取失败: " + (proc.stderr[-240:] if proc.stderr else 'ffmpeg未生成图片')
    except Exception as exc:
        return f"尾帧提取异常: {exc}"
    return None

NONVERBAL_DIALOGUE_MARKER_RE = re.compile(
    r'^(?:无台词|无对白|不说话|不发言|没有台词|没有对白|'
    r'(?:仅|只)发出.+?(?:声|声音)|(?:咕噜|喘息|笑|哭|抽泣|呜咽|叹气)(?:声|声音)?)'
    r'[。.!！?？]*$', re.I)
FULL_STAGE_DIRECTION_RE = re.compile(
    r'^\s*[（(【\[]\s*(.+?)\s*[）)】\]]\s*[。.!！?？]*\s*$')
LEADING_STAGE_DIRECTION_RE = re.compile(
    r'^\s*[（(【\[]\s*(.+?)\s*[）)】\]]\s*(\S.*?)\s*$')

def split_dialogue_direction(line, speaker=''):
    """Return audible words and a non-verbal direction without guessing prose semantics."""
    text = str(line or '').strip()
    speaker = str(speaker or '').strip()
    if speaker:
        text = re.sub(r'^' + re.escape(speaker) + r'\s*[：:]\s*', '', text, count=1)
    full_direction = FULL_STAGE_DIRECTION_RE.fullmatch(text)
    if full_direction:
        return '', f"（{full_direction.group(1).strip()}）"
    leading_direction = LEADING_STAGE_DIRECTION_RE.match(text)
    if leading_direction:
        return leading_direction.group(2).strip(), f"（{leading_direction.group(1).strip()}）"
    plain = text.strip().strip('。.!！?？').strip()
    if NONVERBAL_DIALOGUE_MARKER_RE.fullmatch(plain):
        return '', text
    return text, ''

def is_nonverbal_dialogue_marker(line):
    spoken, direction = split_dialogue_direction(line)
    return not spoken and bool(direction)

def normalized_shot_dialogue(shot):
    rows = []
    for item in (shot or {}).get('dialogue', []):
        if not isinstance(item, dict):
            continue
        line, _direction = split_dialogue_direction(item.get('line'), item.get('speaker'))
        if not line:
            continue
        row = dict(item)
        row['line'] = line
        rows.append(row)
    return rows

def normalize_script_dialogue(script):
    """Repair old projects where non-verbal directions were stored as spoken dialogue."""
    changed = False
    for shot in (script or {}).get('shots', []):
        original = shot.get('dialogue', [])
        normalized = normalized_shot_dialogue(shot)
        if normalized != original:
            markers = []
            for item in original:
                if not isinstance(item, dict):
                    continue
                _spoken, direction = split_dialogue_direction(item.get('line'), item.get('speaker'))
                if direction:
                    markers.append(direction)
            if markers:
                action = str(shot.get('action') or '').rstrip()
                note = '；'.join(markers)
                if note and note not in action:
                    shot['action'] = (action + ('；' if action else '') + note).strip('；')
            shot['dialogue'] = normalized
            changed = True
    return changed

def shot_dialogue_lines(shot):
    return [item['line'] for item in normalized_shot_dialogue(shot)]

def repair_prompt_dialogue_placement(content, shot):
    """Deterministically keep each source line exactly once inside Storyboard."""
    text = str(content or '')
    storyboard_pos = text.find('【Storyboard】')
    voice_pos = text.find('【Character Voice】')
    if storyboard_pos < 0 or voice_pos <= storyboard_pos:
        return text
    storyboard = text[storyboard_pos:voice_pos]
    rows = normalized_shot_dialogue(shot)
    char_numbers = {name: index for index, name in enumerate(shot.get('characters', []), 1)}
    repairs = []
    for row in rows:
        line = row['line']
        if text.count(line) == 1 and line in storyboard:
            continue
        text = text.replace(line, '')
        speaker = row.get('speaker', '')
        number = char_numbers.get(speaker, 1)
        repairs.append(f"Character {number} speaks naturally in Chinese:\n“{line}”")
    if not repairs:
        return text
    voice_pos = text.find('【Character Voice】')
    insertion = "\n\n### Required source dialogue\n" + "\n\n".join(repairs) + "\n\n"
    return text[:voice_pos] + insertion + text[voice_pos:]

def character_profiles_by_name(script):
    return {str(item.get('name') or ''): item for item in (script or {}).get('characters', [])
            if str(item.get('name') or '').strip()}

def infer_character_gender(appearance):
    text = str(appearance or '')
    female = ('女性', '女孩', '女生', '少女', '女人', '女主', '校花', '女士')
    male = ('男性', '男孩', '男生', '少年', '男人', '男主', '先生')
    if any(word in text for word in female):
        return 'female'
    if any(word in text for word in male):
        return 'male'
    return 'auto'

def resolve_character_gender(profile, field='visual_gender'):
    """Resolve an explicit lock first; infer only when the user leaves it on auto."""
    value = str((profile or {}).get(field) or 'auto').lower()
    if value in ('female', 'male'):
        return value
    inferred = infer_character_gender((profile or {}).get('appearance'))
    return inferred if inferred in ('female', 'male') else 'auto'

def shot_character_lock_contracts(shot, character_profiles):
    """Return deterministic per-shot visual/voice contracts confirmed before prompt compilation."""
    profiles = character_profiles or {}
    lines = []
    for number, name in enumerate(shot.get('characters', []), 1):
        profile = profiles.get(name, {})
        if not profile:
            continue
        gender = resolve_character_gender(profile)
        voice = resolve_character_gender(profile, 'voice_gender')
        locked = profile.get('visual_locked', False)
        lines.append(
            f"Character {number} ({name}): visual_gender={gender}; voice_gender={voice}; "
            f"appearance={profile.get('appearance', '')}; visual_lock={'mandatory' if locked else 'guidance'}."
        )
    return lines

def apply_character_visual_locks(prompt, shot, character_profiles):
    """Prepend hard production locks without exposing abstract story identity concepts to H3."""
    if not prompt:
        return prompt
    contracts = []
    profiles = character_profiles or {}
    for number, name in enumerate(shot.get('characters', []), 1):
        profile = profiles.get(name, {})
        if not profile.get('visual_locked'):
            continue
        gender = resolve_character_gender(profile)
        voice = resolve_character_gender(profile, 'voice_gender')
        gender_rule = (
            f"must remain {gender} in face, body, hair and clothing"
            if gender in ('female', 'male') else
            "must preserve the exact face, body, hair and clothing shown in that reference"
        )
        voice_rule = (
            f"Use a consistent {voice} voice"
            if voice in ('female', 'male') else
            "Keep the same natural voice throughout"
        )
        contracts.append(
            f"Character {number} is locked to the supplied character reference and {gender_rule} throughout this shot. "
            f"{voice_rule}. Never change Character {number} into a different-looking person."
        )
    if not contracts:
        return prompt
    marker = "MANDATORY CHARACTER VISUAL LOCKS:"
    if marker in prompt:
        return prompt
    return marker + "\n" + "\n".join(contracts) + "\n\n" + prompt

def character_ref_paths_for_prompt(shot, assets):
    """Only show Qwen character images when every shot character has a correctly ordered asset."""
    paths = []
    for name in shot.get('characters', []):
        path = (assets or {}).get(f"char_{name}", {}).get('path')
        if not path or not os.path.exists(path):
            return []
        paths.append(path)
    return paths

def gen_shot_h3_prompt(shot, ref_paths=None, storyboard_ref_index=None, character_profiles=None,
                       continuity_ref_index=None, stop_check=None):
    """用LLM为一个剧情片段编写含多个内部Shot的H3提示词。返回(prompt, err)。
    Picture编号规则与实际渲染时的ref_paths组装顺序严格一致（每片上限9张）：
    全部出场角色（四宫格三视图）→ 场景 → 道具，按顺序编号"""
    refs_desc_lines = []
    shot_chars = shot.get('characters', [])
    reserve_count = (1 if storyboard_ref_index else 0) + (1 if continuity_ref_index else 0)
    base_limit = max(0, 9 - reserve_count)
    for char_index, ch in enumerate(shot_chars, 1):
        if len(refs_desc_lines) < base_limit:
            refs_desc_lines.append(
                f"Picture {len(refs_desc_lines)+1} = Character {char_index} visual reference "
                "(identity, face, hair, clothing and body proportions)")
    if shot.get('scene') and len(refs_desc_lines) < base_limit:
        refs_desc_lines.append(
            f"Picture {len(refs_desc_lines)+1} = scene reference "
            "(layout, lighting and spatial continuity)")
    for pn in shot.get('props', []):
        if len(refs_desc_lines) < base_limit:
            refs_desc_lines.append(
                f"Picture {len(refs_desc_lines)+1} = prop reference "
                "(exact shape, material and color)")
    if storyboard_ref_index:
        refs_desc_lines.append(
            f"Picture {storyboard_ref_index} = storyboard composition reference "
            "(camera angle, framing, character positions and action state; identity still comes from character references)")
    if segment_needs_previous_tail(shot) and not continuity_ref_index:
        continuity_ref_index = len(refs_desc_lines) + 1
    if continuity_ref_index:
        refs_desc_lines.append(
            f"Picture {continuity_ref_index} = previous segment final-frame continuity reference "
            "(the exact opening pose, positions, framing, lighting and object state at 0.00 seconds; identity still comes from character references)")
    dialogue_rows = []
    char_numbers = {name: index for index, name in enumerate(shot_chars, 1)}
    for d in normalized_shot_dialogue(shot):
        line = d.get('line', '').strip()
        if not line:
            continue
        speaker = d.get('speaker', '').strip()
        dialogue_rows.append(
            f"Character {char_numbers.get(speaker, 1)} | delivery={d.get('tone','natural')} | exact_audio_line={line}")
    dialogue_text = "\n".join(dialogue_rows) or "NO SPOKEN DIALOGUE"
    dialogue_lines = shot_dialogue_lines(shot)
    duration = shot.get('duration', 8)
    internal_shot_range = "2–4" if int(duration) < 8 else ("3–5" if int(duration) <= 11 else "4–6")
    lock_lines = shot_character_lock_contracts(shot, character_profiles)
    lock_text = "\n".join(lock_lines) or "NO CONFIRMED CHARACTER LOCKS"
    sys_text = (H3_PROMPT_TEMPLATE
                .replace('{refs_desc}', "\n".join(refs_desc_lines))
                .replace('{duration}', str(duration))
                .replace('{internal_shot_range}', internal_shot_range)) + (
                    "\n\nMANDATORY CHARACTER CONTRACTS:\n" + lock_text +
                    "\nFollow every mandatory visual lock literally. Never infer a conflicting gender, body, clothing, identity, or voice."
                )
    user_text = (f"EPISODE SEGMENT DATA\nSegment ID: {shot.get('segment_id', 'EP01-' + str(shot.get('index', 1)).zfill(2))}\n"
                 f"Scene asset name: {shot.get('scene', '')}\n"
                 f"Characters in order: {', '.join(shot_chars)}\n"
                 f"Requested shot-sequence pattern: {shot.get('camera', 'establishing to reaction coverage')}\n"
                 f"Complete segment action, information and emotional arc: {shot.get('action', '')}\n"
                 f"Continue from previous segment final frame: {'YES' if segment_needs_previous_tail(shot) else 'NO'}\n"
                 f"CONFIRMED CHARACTER CONTRACTS:\n{lock_text}\n"
                 f"SOURCE DIALOGUE (do not translate, repeat, or embellish):\n{dialogue_text}\n"
                 f"Total duration: {duration} seconds\n"
                 f"Format version: {H3_PROMPT_FORMAT_VERSION}\n\nCompile the production prompt now.")
    user_content = [{"type": "text", "text": user_text}] + load_image_parts(ref_paths)
    content, err = llm_chat([
        {"role": "system", "content": sys_text},
        {"role": "user", "content": user_content}
    ], max_tokens=3000, temperature=0.35, stop_check=stop_check)
    if not content:
        return None, err
    if stop_check and stop_check():
        raise ProjectStopRequested('当前项目已收到停止请求')
    content = repair_prompt_dialogue_placement(content.strip(), shot)
    issues = validate_h3_production_prompt(
        content, dialogue_lines, len(refs_desc_lines))
    all_findings = validate_h3_production_prompt(
        content, dialogue_lines, len(refs_desc_lines), include_warnings=True)
    warnings = [item for item in all_findings if item not in issues]
    if warnings:
        print("[H3提示词] 软警告已放行: " + "；".join(warnings[:5]))
    if issues:
        print(f"[H3提示词] 格式不合格，触发低温重编译: {issues}")
        content, repair_err = repair_h3_production_prompt(
            sys_text, user_content, content, issues, dialogue_lines, len(refs_desc_lines),
            stop_check=stop_check)
        if not content:
            return None, repair_err
    return content, None

# ============================== 项目管理 ==============================
RACE_KEYWORDS = ['中国', '东亚', '亚洲', '华人', '华裔', '欧美', '西方', '欧洲', '美洲', '美国',
                 '非洲', '黑人', '白人', '拉丁', '印度', '中东', '阿拉伯', '日本', '韩国', '日韩', '东南亚', '俄罗斯']

def ensure_race_desc(appearance):
    """生图模型对未指明人种的角色默认偏欧美面孔。
    外貌描述若未含任何人种关键词，兜底注入东亚面孔（本产品主要面向中文故事）"""
    if any(k in appearance for k in RACE_KEYWORDS):
        return appearance
    return f"东亚面孔（中国人），{appearance}"

# 场景描述人物词清理：T2I模型对"不要人物"中的"人物"反而敏感，需从描述中物理剔除人物相关短句
# 注意：不收"居民"(误伤居民楼)等强建筑相关词；"保安"会误伤保安亭但宁可删细节也不留人物
SCENE_HUMAN_RE = re.compile(
    r'[^，。；、]*?(?:人物|角色|行人|人群|路人|顾客|观众|男人|女人|老人|小孩|男孩|女孩|人们|身影|人影|游客'
    r'|男子|女子|男士|女士|少年|少女|青年|婴儿|孩童|大人|摊主|老板|伙计|司机|店员|工人|农民|医生|护士'
    r'|警察|老师|学生|商人|客人|主人|服务员|厨师|助手|推销员|小贩|骑手|保安)[^，。；、]*[，。；、]?')

def sanitize_scene_text(desc, char_names=None, keep_background_life=False):
    """移除主角姓名；概念设定额外剔除人物，真实取景保留自然背景生活。"""
    if not desc:
        return desc
    cleaned = desc
    for name in (char_names or []):
        if name:
            cleaned = cleaned.replace(name, '')
    if not keep_background_life:
        cleaned = SCENE_HUMAN_RE.sub('', cleaned)
    cleaned = re.sub(r'[，、]{2,}', '，', cleaned)
    cleaned = re.sub(r'[。]{2,}', '。', cleaned)
    return cleaned.strip('，。 ')

CONCEPT_SCENE_STYLE_HINTS = (
    '3d动漫', '二次元', '日漫', '动漫', '动画', '漫画', '漫剧', '卡通',
    '插画', '手绘', '水墨', '国风', '游戏', '概念设计', 'concept art'
)

def resolve_scene_reference_mode(style=None, requested=None):
    """解析场景参考模式；自动模式下仅明确的动画/插画画风走概念设定，其余默认真实取景。"""
    requested = str(requested or CONFIG.get('scene_reference_mode', 'auto')).lower()
    if requested in ('photo', 'concept'):
        return requested
    style_lower = str(style or CONFIG.get('style', '')).lower()
    return 'concept' if any(hint in style_lower for hint in CONCEPT_SCENE_STYLE_HINTS) else 'photo'

def build_scene_image_prompt(style, description, mode='auto'):
    """按项目用途生成场景参考提示词：写实视频用真实机位，漫剧/动画保留空间设定图。"""
    resolved = resolve_scene_reference_mode(style, mode)
    if resolved == 'concept':
        prompt = (f"{style}风格，空场景环境概念设计图。{description}。"
                  f"45度俯拍全景视角，完整展现整个空间的布局、结构与每个角落，视野开阔，构图大气，"
                  f"光影考究，环境细节丰富，静态陈设清晰完整。画面内容为纯粹的环境空间与静物。画质精美。")
    else:
        prompt = (f"{style}风格，真实实景取景参考照片。{description}。"
                  f"摄影机位于成年人视线高度，平视观察，使用真实手机或35mm纪实摄影镜头，空间尺度符合现实。"
                  f"自然可用光与现场混合光，曝光和白平衡具有真实相机质感，材质保留细微磨损、灰尘与日常使用痕迹。"
                  f"陈设和背景元素随机自然分布，随手取景般略有偏心构图，前后景存在真实遮挡与景深，"
                  f"描述中出现的背景行人、顾客或交通流以真实比例随机分布，"
                  f"呈现普通生活现场的纪录片现实主义与可拍摄机位，环境细节可信。")
    return prompt, resolved

def describe_uploaded_asset(img_path, kind, name):
    """用户上传自定义参考图后，用视觉LLM分析图片重写外观描述（后续提示词以图为准而非原文字设计）。
    返回(描述, err)；视觉调用失败时返回(None, err)，调用方保留原描述兜底。"""
    import base64
    try:
        with open(img_path, 'rb') as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        return None, f"读图失败: {e}"
    ext = img_path.rsplit('.', 1)[-1].lower()
    mime = {'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'webp': 'image/webp'}.get(ext, 'image/png')
    if kind in ('char', 'character'):
        ask = (f"这是短剧角色「{name}」的设定参考图。请详细描述图中人物的外貌：人种、年龄段、发型发色、"
               f"面部特征、服装款式与颜色、体型、气质。只输出描述文字本身，100字以内。")
    elif kind == 'scene':
        ask = (f"这是短剧场景「{name}」的参考图。请详细描述图中环境：建筑风格、年代感、光线、色调、"
               f"陈设布局、氛围。只描述环境本身，只输出描述文字本身，80字以内。")
    else:
        ask = (f"这是短剧道具「{name}」的参考图。请详细描述图中物体的外观：形状、颜色、材质、年代感、"
               f"显著细节。只输出描述文字本身，50字以内。")
    messages = [{"role": "user", "content": [
        {"type": "text", "text": ask},
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}
    ]}]
    desc, err = llm_chat(messages, max_tokens=300, temperature=0.3)
    if err:
        return None, err
    return desc, None

def project_path(pid):
    return os.path.join(PROJECTS_DIR, f"{pid}.json")

def save_project(proj, preserve_runtime=True):
    target = project_path(proj['id'])
    with _PROJECT_IO_LOCK:
        # The pipeline keeps a project object in memory for a long time. Preserve
        # a newer stop request written by the API instead of overwriting it with
        # that stale snapshot during a later progress save.
        incoming_job = proj.get('production_job')
        if preserve_runtime and os.path.exists(target):
            try:
                with open(target, 'r', encoding='utf-8') as stream:
                    stored_job = (json.load(stream) or {}).get('production_job')
                stored_updated = float((stored_job or {}).get('updated') or 0)
                incoming_updated = float((incoming_job or {}).get('updated') or 0)
                equal_terminal_wins = (
                    stored_updated == incoming_updated
                    and (stored_job or {}).get('status') in TERMINAL_PRODUCTION_STATUSES
                    and (incoming_job or {}).get('status') in ACTIVE_PRODUCTION_STATUSES)
                if (stored_job and (not incoming_job
                        or (stored_job.get('run_id') == incoming_job.get('run_id')
                            and stored_job.get('stop_requested') and not incoming_job.get('stop_requested'))
                        or stored_updated > incoming_updated
                        or equal_terminal_wins)):
                    proj['production_job'] = copy.deepcopy(stored_job)
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        last_error = None
        for attempt in range(5):
            temp = f"{target}.{uuid.uuid4().hex}.tmp"
            try:
                with open(temp, 'w', encoding='utf-8') as f:
                    json.dump(proj, f, ensure_ascii=False, indent=2)
                os.replace(temp, target)
                return
            except PermissionError as exc:
                last_error = exc
                try:
                    if os.path.exists(temp):
                        os.remove(temp)
                except OSError:
                    pass
                time.sleep(0.04 * (attempt + 1))
        raise last_error or OSError(f'无法保存项目 {proj.get("id", "")}')

def load_project(pid):
    p = project_path(pid)
    with _PROJECT_IO_LOCK:
        for attempt in range(5):
            if not os.path.exists(p):
                return None
            try:
                with open(p, 'r', encoding='utf-8') as f:
                    project = json.load(f)
                # 发布案例使用相对资产路径；旧项目搬家后也按文件名自动恢复。
                for asset in (project.get('assets') or {}).values():
                    raw_path = str(asset.get('path') or '').strip()
                    if not raw_path:
                        continue
                    candidate = raw_path if os.path.isabs(raw_path) else os.path.join(BASE_DIR, raw_path)
                    if not os.path.exists(candidate):
                        candidate = os.path.join(ASSETS_DIR, os.path.basename(raw_path))
                    asset['path'] = os.path.abspath(candidate)
                return project
            except (PermissionError, json.JSONDecodeError):
                if attempt == 4:
                    raise
                time.sleep(0.04 * (attempt + 1))
    return None

class ProjectStopRequested(RuntimeError):
    pass

class ProjectAlreadyRunning(RuntimeError):
    pass

def production_job_active(job):
    return str((job or {}).get('status') or '') in ACTIVE_PRODUCTION_STATUSES

def request_project_stop_state(pid, requested_run_id=None):
    """Compare-and-set one project run to stopping without racing its terminal write."""
    with _PROJECT_IO_LOCK:
        proj = load_project(pid)
        if not proj:
            return 'missing', None, None
        job = proj.get('production_job') or {}
        if requested_run_id and requested_run_id != str(job.get('run_id') or ''):
            return 'stale', copy.deepcopy(job), None
        status = str(job.get('status') or '')
        if status == 'stopping':
            return 'stopping', copy.deepcopy(job), job.get('current_prompt_id')
        if not production_job_active(job) or proj.get('final'):
            return 'terminal', copy.deepcopy(job), None
        now = time.time()
        job.update({
            'status': 'stopping', 'stop_requested': True,
            'message': '停止请求已保存，将保留已经完成的结果',
            'stop_requested_at': now, 'updated': now,
        })
        proj['production_job'] = job
        save_project(proj, preserve_runtime=False)
        return 'accepted', copy.deepcopy(job), job.get('current_prompt_id')

def update_stopping_cancel_result(pid, run_id, action, message):
    with _PROJECT_IO_LOCK:
        proj = load_project(pid)
        if not proj:
            return None
        job = proj.get('production_job') or {}
        if job.get('run_id') == run_id and job.get('status') == 'stopping':
            job.update({'cancel_result': action, 'message': message, 'updated': time.time()})
            proj['production_job'] = job
            save_project(proj, preserve_runtime=False)
        return copy.deepcopy(job)

def begin_production_job(pid, kind):
    run_id = uuid.uuid4().hex
    if not claim_project_job(pid, run_id):
        raise ProjectAlreadyRunning('当前项目已有生成任务正在运行')
    try:
        proj = load_project(pid)
        if not proj:
            release_project_job(pid, run_id)
            return None
        now = time.time()
        proj['production_job'] = {
            'run_id': run_id, 'kind': kind, 'status': 'running',
            'stop_requested': False, 'current_prompt_id': None,
            'current_prompt_kind': None, 'cancel_result': None,
            'message': '任务正在运行', 'started': now, 'updated': now,
        }
        save_project(proj, preserve_runtime=False)
        return run_id
    except Exception:
        release_project_job(pid, run_id)
        raise

def end_production_job(pid, run_id):
    release_project_job(pid, run_id)

def update_production_job(pid, run_id=None, **changes):
    """Atomically update persisted runtime ownership for one project run."""
    with _PROJECT_IO_LOCK:
        proj = load_project(pid)
        if not proj:
            return None
        job = proj.get('production_job') or {}
        if run_id and job.get('run_id') != run_id:
            return None
        job.update(changes)
        job['updated'] = time.time()
        proj['production_job'] = job
        save_project(proj, preserve_runtime=False)
        return copy.deepcopy(job)

def project_stop_requested(pid, run_id=None):
    proj = load_project(pid)
    job = (proj or {}).get('production_job') or {}
    if run_id and job.get('run_id') != run_id:
        return True
    return bool(job.get('stop_requested'))

def raise_if_project_stopped(pid, run_id):
    if project_stop_requested(pid, run_id):
        raise ProjectStopRequested('当前项目已收到停止请求')

def register_project_prompt(pid, run_id, prompt_id, kind):
    return update_production_job(
        pid, run_id, current_prompt_id=str(prompt_id), current_prompt_kind=kind,
        status='stopping' if project_stop_requested(pid, run_id) else 'running')

def clear_project_prompt(pid, run_id, prompt_id):
    proj = load_project(pid)
    job = (proj or {}).get('production_job') or {}
    if job.get('run_id') == run_id and str(job.get('current_prompt_id') or '') == str(prompt_id):
        update_production_job(pid, run_id, current_prompt_id=None, current_prompt_kind=None)

def production_stop_message(error):
    text = str(error or '')
    return text.split(':', 1)[1] if text.startswith('__AIX_STOPPED__:') else None

def cancel_owned_project_prompt(pid, run_id, prompt_id):
    proj = load_project(pid)
    job = (proj or {}).get('production_job') or {}
    if job.get('run_id') != run_id or str(job.get('current_prompt_id') or '') != str(prompt_id):
        return 'none', '当前提示已不属于这个项目任务'
    if job.get('cancel_result') == 'cancelled':
        return 'cancelled', job.get('message') or '当前项目任务已取消'
    action, message = comfy_cancel_prompt_safely(prompt_id)
    update_production_job(pid, run_id, cancel_result=action, message=message)
    return action, message

def finalize_project_stopped(pid, run_id, message):
    """Persist a stopped terminal state while leaving completed outputs untouched."""
    with _PROJECT_IO_LOCK:
        proj = load_project(pid)
        if not proj:
            return None
        job = proj.get('production_job') or {}
        if job.get('run_id') != run_id:
            return None
        active_states = {'queued', 'prompting', 'generating', 'retrying', 'running', 'stopping'}
        for group in (proj.get('item_states') or {}).values():
            if not isinstance(group, dict):
                continue
            for item in group.values():
                if isinstance(item, dict) and item.get('status') in active_states:
                    item.update({'status': 'waiting', 'message': '任务已停止，可从断点继续', 'updated': time.time()})
        prep = proj.get('preproduction') or {}
        for item in prep.get('asset_plan', []):
            if item.get('status') in active_states:
                item.update({'status': 'waiting', 'message': '任务已停止，可重新生成', 'updated': time.time()})
        batch = prep.get('asset_batch') or {}
        if batch.get('status') in ('starting', 'running', 'stopping'):
            batch.update({'status': 'stopped', 'current': None, 'message': message, 'updated': time.time()})
        job.update({
            'status': 'stopped', 'stop_requested': True,
            'current_prompt_id': None, 'current_prompt_kind': None,
            'message': message, 'stopped': time.time(), 'updated': time.time(),
        })
        proj['production_job'] = job
        save_project(proj, preserve_runtime=False)
        return copy.deepcopy(job)

def finish_preproduction_llm_job(pid, run_id, status, message):
    """Finish one synchronous preparation call without overwriting a pending stop."""
    with _PROJECT_IO_LOCK:
        proj = load_project(pid)
        if not proj:
            return None
        job = proj.get('production_job') or {}
        if job.get('run_id') != run_id:
            return copy.deepcopy(job)
        if status == 'stopped' or job.get('stop_requested'):
            return finalize_project_stopped(
                pid, run_id, message or '已停止，完成的结果已保留')
        return update_production_job(
            pid, run_id, status=status, message=message,
            current_prompt_id=None, current_prompt_kind=None,
            finished=time.time())

def app_version():
    version_path = os.path.join(BASE_DIR, 'VERSION')
    try:
        with open(version_path, 'r', encoding='utf-8') as f:
            return f.read().strip() or 'unknown'
    except OSError:
        return 'unknown'

def desktop_aspect_ratio(value):
    aliases = {
        '16:9': '16:9 (Widescreen)',
        '9:16': '9:16 (Portrait Widescreen)',
        '1:1': '1:1 (Square)',
        '4:3': '4:3 (Standard)',
    }
    text = str(value or CONFIG.get('aspect_ratio'))
    return H3_ASPECT_RATIO_ALIASES.get(aliases.get(text, text), aliases.get(text, text))

def desktop_project_summary(proj):
    script = proj.get('script') or {}
    preproduction = proj.get('preproduction') or {}
    planned_segments = script.get('shots') or []
    completed_segments = proj.get('shots') or []
    assets = proj.get('assets') or {}
    final_url = proj.get('final')
    step = 'complete' if final_url else (preproduction.get('step') or ('production' if completed_segments else 'draft'))
    total = len(planned_segments)
    completed = len(completed_segments)
    progress = 100 if final_url else max(0, min(100, round((completed / total) * 100))) if total else 0
    return {
        'id': proj.get('id'),
        'title': proj.get('title') or str(proj.get('idea') or '')[:20] or '未命名短剧',
        'created': proj.get('created', 0),
        'stage': step,
        'progress': progress,
        'counts': {
            'segments': total,
            'completed_segments': completed,
            'characters': len(script.get('characters') or []),
            'scenes': len(script.get('scenes') or []),
            'props': len(script.get('props') or []),
            'assets': len(assets),
        },
        'has_final': bool(final_url),
        'final_url': final_url,
    }

def desktop_project_detail(proj):
    assets_view = []
    for key, asset in (proj.get('assets') or {}).items():
        path = asset.get('path') or ''
        assets_view.append({
            'key': key,
            'kind': asset.get('kind'),
            'url': f"/file/assets/{os.path.basename(path)}" if path else None,
            'uploaded': bool(asset.get('uploaded')),
        })
    shots_view = []
    for shot in (proj.get('shots') or []):
        shots_view.append({key: shot.get(key) for key in (
            'index', 'segment_id', 'video_url', 'duration',
            'generation_started_at', 'generation_elapsed_seconds',
            'continuity_from_previous')})
    return {
        'summary': desktop_project_summary(proj),
        'idea': proj.get('idea', ''),
        'input_mode': proj.get('input_mode', 'story'),
        'manual_h3_prompt': proj.get('manual_h3_prompt'),
        'style': proj.get('style'),
        'aspect_ratio': proj.get('aspect_ratio', CONFIG.get('aspect_ratio')),
        'scene_reference_mode': proj.get('scene_reference_mode', 'auto'),
        'shot_duration': proj.get('shot_duration', 'auto'),
        'shot_count': proj.get('shot_count', 'auto'),
        'generate_storyboards': bool(proj.get('generate_storyboards', False)),
        'script': proj.get('script'),
        'assets': assets_view,
        'shots': shots_view,
        'final_url': proj.get('final'),
        'item_states': proj.get('item_states', {}),
        'production_job': proj.get('production_job', {}),
        'preproduction': desktop_preproduction_view(proj) if proj.get('preproduction') else None,
        'prompts': proj.get('prompts', {}),
    }

def desktop_preproduction_view(proj):
    prep = copy.deepcopy(proj.get('preproduction') or {})
    if prep.get('step') == 'character_review':
        prep.update({'step': 'prompts', 'script_confirmed': bool(proj.get('script')), 'character_confirmed': True})
    batch = prep.get('asset_batch') or {}
    if batch.get('status') in ('starting', 'running') and not prep_asset_batch_active(proj.get('id')):
        batch.update({'status': 'interrupted', 'current': None, 'message': '服务已重启，可重试缺失图片'})
        prep['asset_batch'] = batch
    for item in prep.get('asset_plan', []):
        item['url'] = prep_asset_url(item.get('path'))
        item.pop('path', None)
        item['progress'] = max(0, min(100, int(item.get('progress') or (100 if item.get('url') else 0))))
    return prep

def set_item_state(proj, group, key, status, progress=0, message='', kind=None, name=None, **extra):
    """Persist per-asset/per-shot state so refresh and history restore keep live progress."""
    groups = proj.setdefault('item_states', {})
    states = groups.setdefault(group, {})
    old = states.get(str(key), {})
    state = {
        **old,
        "status": status,
        "progress": max(0, min(100, int(progress or 0))),
        "message": message,
        "updated": time.time(),
    }
    if kind is not None:
        state['kind'] = kind
    if name is not None:
        state['name'] = name
    if status == 'failed' and message:
        state['error'] = message
        if group == 'shots' and not state.get('original_error'):
            state['original_error'] = message
    state.update(extra)
    states[str(key)] = state
    save_project(proj)
    return {"group": group, "key": str(key), **state}

def publish_item_state(proj, send, group, key, status, progress=0, message='', kind=None, name=None, **extra):
    payload = set_item_state(proj, group, key, status, progress, message, kind, name, **extra)
    send('item_status', payload)
    return payload

def make_progress_publisher(proj, send, group, key, kind, name, message, start=5, end=95):
    """Map ComfyUI node progress to a monotonic card percentage."""
    seen = {'progress': start - 1}
    def callback(value, maximum, node=None):
        if not maximum:
            return
        pct = start + round((end - start) * float(value) / float(maximum))
        pct = max(start, min(end, pct))
        if pct <= seen['progress']:
            return
        seen['progress'] = pct
        detail = f"{message} · {int(value)}/{int(maximum)}步"
        publish_item_state(proj, send, group, key, 'generating', pct, detail, kind, name)
    return callback

def initialize_item_states(proj, script):
    """Create stable waiting/completed entries for all cards, including older projects."""
    assets = proj.get('assets', {})
    states = proj.setdefault('item_states', {})
    asset_states = states.setdefault('assets', {})
    shot_states = states.setdefault('shots', {})
    for prefix, kind, rows in (
        ('char', 'character', script.get('characters', [])),
        ('scene', 'scene', script.get('scenes', [])),
        ('prop', 'prop', script.get('props', [])),
    ):
        for row in rows:
            name = row.get('name', '')
            key = f"{prefix}_{name}"
            ready = key in assets and os.path.exists(assets[key].get('path', ''))
            current = asset_states.get(key, {})
            if ready:
                asset_states[key] = {**current, "kind": kind, "name": name, "status": "done", "progress": 100, "message": "已完成", "updated": time.time()}
            elif current.get('status') not in ('generating', 'failed'):
                asset_states[key] = {**current, "kind": kind, "name": name, "status": "waiting", "progress": 0, "message": "等待生成", "updated": time.time()}
    completed_shots = {str(s.get('index')): s for s in proj.get('shots', []) if s.get('video_url')}
    for i, shot in enumerate(script.get('shots', [])):
        key = str(shot.get('index', i + 1))
        current = shot_states.get(key, {})
        if key in completed_shots:
            result = completed_shots[key]
            elapsed = result.get('generation_elapsed_seconds')
            shot_states[key] = {
                **current, "name": segment_label(shot, key), "status": "done", "progress": 100,
                "message": "片段已完成", "updated": time.time(),
                "generation_started_at": result.get('generation_started_at'),
                "generation_elapsed_seconds": elapsed,
            }
        elif current.get('status') not in ('retrying', 'prompting', 'generating', 'failed'):
            shot_states[key] = {**current, "name": segment_label(shot, key), "status": "waiting", "progress": 0, "message": "等待生成", "updated": time.time()}
    save_project(proj)
    return states

# ============================== SSE 工具 ==============================
SSE_HEARTBEAT_INTERVAL_SECONDS = 10.0


def sse(event, data):
    if event == 'error':
        event = 'pipeline_error'
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def sse_comment(comment='keep-alive'):
    """Return an SSE comment frame that keeps idle proxy connections open."""
    safe_comment = str(comment).replace('\r', ' ').replace('\n', ' ')
    return f": {safe_comment}\n\n"


def stream_sse_queue(worker, queue, poll_interval=0.3,
                     heartbeat_interval=SSE_HEARTBEAT_INTERVAL_SECONDS):
    """Yield worker events and periodic heartbeats during quiet generation phases."""
    last_emit = time.monotonic()
    while True:
        if queue:
            yield queue.pop(0)
            last_emit = time.monotonic()
        elif not worker.is_alive():
            break
        elif time.monotonic() - last_emit >= heartbeat_interval:
            yield sse_comment()
            last_emit = time.monotonic()
        else:
            time.sleep(poll_interval)
    while queue:
        yield queue.pop(0)

# ============================== 主管线 ==============================
def build_script_prompt(settings=None):
    """根据当前配置动态构造“片段级”剧本解析系统提示词。"""
    settings = settings or CONFIG
    dur = settings.get('shot_duration', 'auto')
    if isinstance(dur, str) and dur.lower() == 'auto':
        duration_rule = ("4. 每个剧情片段时长优先在8~15秒之间，由片段的动作复杂度与台词量决定；"
                         "每段必须足以编排3~6个内部摄影镜头并形成小型叙事闭环，"
                         "全片各段时长允许变化，不能把单一近景或一句反应单独作为一个片段")
    else:
        duration_rule = (f"4. 每个剧情片段固定{int(dur)}秒；在该时长内组织一个完整事件，"
                         "并预留3~6个内部摄影镜头的景别与反应变化")
    cnt = settings.get('shot_count', 'auto')
    if isinstance(cnt, str) and cnt.lower() == 'auto':
        count_rule = "5. 片段数量由故事节奏决定，一般3~8个片段；每段对应一次H3生成，绝不按单个摄影镜头硬拆"
    else:
        count_rule = (f"5. 严格生成{int(cnt)}个剧情片段，不多不少——这是硬性要求；"
                      f"请把故事节奏压缩或展开到恰好{int(cnt)}个片段，每段都必须有完整推进和收尾")
    return SCRIPT_PARSE_PROMPT.replace('%DURATION_RULE%', duration_rule).replace('%COUNT_RULE%', count_rule)

def get_shot_count_limit():
    """返回用户指定的片段数(int)，自动模式返回None（字段名为兼容旧配置保留）。"""
    cnt = CONFIG.get('shot_count', 'auto')
    if isinstance(cnt, str) and cnt.lower() == 'auto':
        return None
    try:
        return max(1, min(int(cnt), 20))
    except (TypeError, ValueError):
        return None

MANUAL_H3_ASSET_PROMPT = """你是影视资产拆解助手。用户输入的是 MiniMax H3 完整片段提示词，或由多个时间段组成的长提示词。
程序会识别顶层生产片段；片段内部的 Shot 1～Shot N 只是同一条视频中的摄影镜头，绝不能当作多个生产任务。你的任务只是在不改写、不润色原提示词的前提下，一次性提取所有片段生成参考图所需的资产，并返回严格 JSON。

规则：
1. 按提示词中 <Picture 1>、<Picture 2> 等实际编号顺序输出 reference_assets；picture 必须是对应数字。
2. kind 只能是 character、scene、prop。人物归 character，纯环境归 scene，关键物件归 prop。
3. description 必须足够详细，可直接用于生成一张稳定参考图；人物需含性别、年龄、面孔、发型、服装，场景需含地点、时间、光线与陈设，道具需含材质、颜色、尺寸与特征。
4. shots 是使用该资产的“生产片段序号”数组，不是片段内部 Shot 编号。若输入只有一个完整六分区H3片段，所有资产的shots均为[1]。
5. persistent_character：若原提示词要求“全程/每个镜头保持同一人、身份/五官/外貌一致”，该主角必须填 true，即使她只在某个时间段被明确提到也必须传入所有镜头。
6. 若提示词没有 Picture 标签，按角色→场景→道具的顺序自行分配 picture，确保至少一个参考资产。
7. 仅提取信息，严禁输出改写后的 H3 提示词或分镜提示词。
8. 对每个第2片及后续片段判断是否需要承接上一片尾帧：仅当地点、时间与动作连续，且当前片开场的姿态、站位、机位、光线或道具状态依赖上一片结尾时填 true；发生转场、时间跳跃、换地点、蒙太奇或可以独立开场时填 false。没有明确必要性时填 false，并给出简短 reason。

严格输出：
{
  "title": "简短项目名",
  "reference_assets": [
    {"picture": 1, "kind": "character", "name": "资产名", "description": "详细参考图描述", "shots": [1, 2], "persistent_character": true}
  ],
  "segment_continuity": [
    {"segment": 2, "continue_from_previous": true, "reason": "同一时空中的动作、姿态、站位或光线必须承接上一片段最后一帧"}
  ]
}
不要 markdown，不要解释。"""

H3_TIMECODE_RE = re.compile(
    r'(?<!\d)(\d{1,3}(?:\.\d+)?)\s*(?:-|–|—|~|～|至|到)\s*'
    r'(\d{1,3}(?:\.\d+)?)\s*(?:秒|s)(?:钟)?\s*[：:]?', re.IGNORECASE)
H3_GLOBAL_SUFFIX_RE = re.compile(
    r'(?:视觉风格|整体风格|全局要求|统一要求|负面提示|禁止项|约束条件)\s*[：:]', re.IGNORECASE)
H3_GLOBAL_IDENTITY_RE = re.compile(
    r'(?:(?:全程|整个视频|每个镜头|全片|始终|一直).{0,80}(?:同一个人|身份|五官|外貌|发型|身体比例|角色一致))'
    r'|(?:(?:同一个人|身份|五官|外貌|发型|身体比例|角色一致).{0,80}(?:全程|整个视频|每个镜头|全片|始终|一直))',
    re.IGNORECASE | re.S)
H3_EXPLICIT_PICTURE_RE = re.compile(r'<\s*picture\s*\d+\s*>|<\s*图片\s*\d+\s*>', re.IGNORECASE)
H3_EP_SEGMENT_RE = re.compile(r'(?im)^\s*(?:#{1,6}\s*)?(EP\d{1,3}-\d{1,3})\b[^\r\n]*$')
H3_CONTINUITY_CUE_RE = re.compile(
    r'(?:承接|紧接|延续|接续|无缝衔接|上一(?:段|片段|镜头)(?:的)?(?:结尾|尾帧|最后一帧))'
    r'|(?:continue(?:s|d)?\s+(?:directly|immediately|seamlessly)\s+from\s+the\s+previous\s+(?:segment|clip))'
    r'|(?:previous\s+(?:segment|clip)(?:\'s)?\s+(?:final|last)\s+frame)', re.IGNORECASE)

def _format_second(value):
    value = float(value)
    return str(int(value)) if value.is_integer() else f"{value:g}"

def _looks_like_complete_h3_segment(text):
    """识别已经写好的六分区片段；内部 Shot 时间码不再拆成多个视频。"""
    required = ("【Important】", "【Storyboard】", "【Sound】", "【Forbidden】", "【Mandatory】")
    return all(section in (text or '') for section in required) and bool(
        re.search(r'(?im)^\s*(?:#{1,6}\s*)?Shot\s+1\b', text or ''))

def _complete_h3_segment_duration(text):
    """从片段内部 Shot 时间范围取总时长。"""
    ranges = list(H3_TIMECODE_RE.finditer(text or ''))
    if not ranges:
        return None
    starts = [float(match.group(1)) for match in ranges]
    ends = [float(match.group(2)) for match in ranges]
    if any(end <= start for start, end in zip(starts, ends)):
        return None
    return max(ends)

def _split_complete_h3_segments(prompt):
    """按顶层 EPxx-xx 标题拆多个完整片段；没有标题时整段即一个片段。"""
    text = (prompt or '').strip()
    markers = list(H3_EP_SEGMENT_RE.finditer(text))
    if len(markers) < 2:
        return [(markers[0].group(1) if markers else 'EP01-01', text)]
    blocks = []
    for pos, marker in enumerate(markers):
        end = markers[pos + 1].start() if pos + 1 < len(markers) else len(text)
        blocks.append((marker.group(1), text[marker.start():end].strip()))
    return blocks

def parse_h3_timeline(prompt):
    """确定顶层生产片段；完整六分区提示词内部 Shot 原样保留。"""
    if _looks_like_complete_h3_segment(prompt):
        blocks = _split_complete_h3_segments(prompt)
        segments, prompts = [], {}
        for index, (segment_id, block) in enumerate(blocks, 1):
            if not _looks_like_complete_h3_segment(block):
                return None, f"{segment_id} 不是完整的H3片段格式"
            duration_value = _complete_h3_segment_duration(block)
            if duration_value is None:
                return None, f"{segment_id} 无法从内部 Shot 时间范围确定片段时长"
            if duration_value < 3 or duration_value > 15:
                return None, f"{segment_id} 时长为{_format_second(duration_value)}秒；单个H3片段必须在3～15秒之间"
            segment = {
                'index': index, 'segment_id': segment_id,
                'start': 0, 'end': duration_value, 'duration': int(round(duration_value)),
                'text': block, 'prompt': block, 'compiled_prompt': True,
                'continue_from_previous': index > 1 and bool(H3_CONTINUITY_CUE_RE.search(block))
            }
            segments.append(segment)
            prompts[str(index)] = block
        return {'prefix': '', 'suffix': '', 'segments': segments, 'prompts': prompts,
                'mode': 'complete_segments'}, None

    matches = list(H3_TIMECODE_RE.finditer(prompt or ''))
    if len(matches) < 2:
        return None, "至少需要两个顶层时间段（例如 0-10秒、10-20秒），或输入一个含Storyboard与内部Shot时间轴的完整H3片段"
    if len(matches) > 20:
        return None, "时间段超过20个，请精简后重试"
    prefix = (prompt[:matches[0].start()] or '').strip(' \t\r\n，,。;；')
    # 每段会用自己的局部时长，删除公共段里的“总时长XX秒”以免与H3单镜时长冲突。
    prefix = re.sub(r'(?:总时长|全片时长|视频时长)\s*(?:为|约|[:：])?\s*\d+(?:\.\d+)?\s*秒', '', prefix)
    prefix = re.sub(r'\s{2,}', ' ', prefix).strip(' \t\r\n，,。;；')
    raw_segments = []
    for i, match in enumerate(matches):
        start, end = float(match.group(1)), float(match.group(2))
        if end <= start:
            return None, f"时间段 {_format_second(start)}-{_format_second(end)}秒 无效：结束时间必须大于开始时间"
        duration_value = end - start
        if duration_value < 3 or duration_value > 15:
            return None, f"时间段 {_format_second(start)}-{_format_second(end)}秒 时长为{_format_second(duration_value)}秒；每段必须在3～15秒之间"
        next_pos = matches[i + 1].start() if i + 1 < len(matches) else len(prompt)
        text = prompt[match.end():next_pos].strip(' \t\r\n，,。;；')
        if not text:
            return None, f"时间段 {_format_second(start)}-{_format_second(end)}秒 没有画面内容"
        raw_segments.append({'start': start, 'end': end, 'duration': int(round(duration_value)), 'text': text})

    # 最后一段后常跟“视觉风格/禁止项”，将其提升为所有镜头共享的公共尾部。
    suffix = ''
    suffix_match = H3_GLOBAL_SUFFIX_RE.search(raw_segments[-1]['text'])
    if suffix_match:
        suffix = raw_segments[-1]['text'][suffix_match.start():].strip()
        raw_segments[-1]['text'] = raw_segments[-1]['text'][:suffix_match.start()].strip(' \t\r\n，,。;；')
    prompts = dict(proj.get('prompts') or {})
    prompt_errors = []
    for index, segment in enumerate(raw_segments, 1):
        local_end = _format_second(segment['duration'])
        original_range = f"{_format_second(segment['start'])}-{_format_second(segment['end'])}秒"
        parts = [prefix,
                 f"本片段时长{local_end}秒，对应完整时间轴的{original_range}。",
                 f"0-{local_end}秒：{segment['text']}", suffix]
        prompts[str(index)] = '\n\n'.join(p for p in parts if p)
        segment['index'] = index
        segment['prompt'] = prompts[str(index)]
        segment['segment_id'] = f"EP01-{index:02d}"
        segment['continue_from_previous'] = index > 1 and bool(H3_CONTINUITY_CUE_RE.search(segment['text']))
    return {'prefix': prefix, 'suffix': suffix, 'segments': raw_segments, 'prompts': prompts,
            'mode': 'timeline_segments'}, None

def build_manual_h3_script(prompt, extracted, timeline):
    """把完整H3输入转换为兼容的片段流水线结构（shots字段暂保留）。"""
    refs = extracted.get('reference_assets') or []
    refs = sorted([r for r in refs if isinstance(r, dict)],
                  key=lambda r: int(r.get('picture', 999) or 999))[:9]
    shot_count = len(timeline['segments'])
    characters, scenes, props, asset_defs = [], [], [], []
    used_keys = set()
    character_ref_count = sum(1 for ref in refs if str(ref.get('kind', '')).lower() == 'character')
    global_identity_requested = bool(H3_GLOBAL_IDENTITY_RE.search(prompt or ''))
    explicit_picture_order = bool(H3_EXPLICIT_PICTURE_RE.search(prompt or ''))
    for pos, ref in enumerate(refs, 1):
        kind = str(ref.get('kind', '')).lower()
        if kind not in ('character', 'scene', 'prop'):
            kind = 'scene'
        name = str(ref.get('name') or f"参考资产{pos}").strip()
        desc = str(ref.get('description') or name).strip()
        prefix = {'character': 'char', 'scene': 'scene', 'prop': 'prop'}[kind]
        base_name, key, suffix = name, f"{prefix}_{name}", 2
        while key in used_keys:
            name = f"{base_name}{suffix}"; key = f"{prefix}_{name}"; suffix += 1
        used_keys.add(key)
        persistent_value = ref.get('persistent_character', False)
        persistent_from_llm = persistent_value is True or str(persistent_value).strip().lower() in ('1', 'true', 'yes', '是')
        primary_name = bool(re.search(r'主角|女主|男主|主人公|主角', name))
        # 兜底：Qwen偶发把全片主角标成只在第1镜出现。全局身份约束下，唯一角色或明确主角必须锁定全镜。
        persistent_character = kind == 'character' and (
            persistent_from_llm or (global_identity_requested and (character_ref_count == 1 or primary_name)))
        raw_shots = ref.get('shots') or list(range(1, shot_count + 1))
        shot_indexes = []
        for value in raw_shots:
            try:
                idx = int(value)
                if 1 <= idx <= shot_count and idx not in shot_indexes:
                    shot_indexes.append(idx)
            except (TypeError, ValueError):
                pass
        if not shot_indexes:
            shot_indexes = list(range(1, shot_count + 1))
        if persistent_character:
            shot_indexes = list(range(1, shot_count + 1))
        asset_defs.append({'key': key, 'kind': kind, 'name': name, 'shots': shot_indexes,
                           'persistent_character': persistent_character, 'order': pos})
        if kind == 'character':
            characters.append({'name': name, 'appearance': desc, 'personality': ''})
        elif kind == 'scene':
            scenes.append({'name': name, 'description': desc})
        else:
            props.append({'name': name, 'description': desc})
    if not asset_defs:
        # 模型极端异常时仍提供可生成、可人工替换的基础场景卡。
        fallback = '主场景'
        scenes.append({'name': fallback, 'description': prompt[:800]})
        asset_defs.append({'key': f'scene_{fallback}', 'kind': 'scene', 'name': fallback,
                           'shots': list(range(1, shot_count + 1)),
                           'persistent_character': False, 'order': 1})
    continuity_map = {}
    for row in extracted.get('segment_continuity') or []:
        try:
            continuity_map[int(row.get('segment'))] = row
        except (TypeError, ValueError, AttributeError):
            continue
    shots = []
    for segment in timeline['segments']:
        idx = segment['index']
        used = [a for a in asset_defs if idx in a['shots']][:9]
        if not used:
            # 资产镜头映射偶发遗漏时至少保留贯穿角色或首个资产，避免R2V无参考图。
            used = ([a for a in asset_defs if a['kind'] == 'character'] or asset_defs)[:9]
        # 没有用户显式 <Picture N> 排序时，持续主角必须是Picture 1，最大化H3身份一致性。
        if not explicit_picture_order:
            used = sorted(used, key=lambda a: (
                0 if a.get('persistent_character') else (1 if a['kind'] == 'character' else 2), a['order']))
        shot_chars = [a['name'] for a in used if a['kind'] == 'character']
        shot_props = [a['name'] for a in used if a['kind'] == 'prop']
        shot_scenes = [a['name'] for a in used if a['kind'] == 'scene']
        continuity_row = continuity_map.get(idx, {})
        continuity_value = continuity_row.get('continue_from_previous')
        if continuity_row and 'continue_from_previous' in continuity_row:
            # Qwen 已阅读完整上下文时，以语义判断为准；正则只作为模型未给结论时的兜底。
            continue_from_previous = continuity_value is True or str(continuity_value).strip().lower() in (
                '1', 'true', 'yes', '是')
        else:
            continue_from_previous = bool(segment.get('continue_from_previous'))
        continue_from_previous = idx > 1 and continue_from_previous
        shots.append({
            'index': idx, 'segment_id': segment.get('segment_id', f"EP01-{idx:02d}"),
            'scene': shot_scenes[0] if shot_scenes else '',
            'characters': shot_chars, 'props': shot_props,
            'camera': '按该片段原始 H3 提示词执行', 'action': segment['text'],
            'dialogue': [], 'duration': segment['duration'],
            'continue_from_previous': bool(continue_from_previous),
            'continuity_reason': str(continuity_row.get('reason') or (
                '提示词包含明确的跨片连续性描述' if continue_from_previous else '')).strip(),
            'identity_locked_characters': [a['name'] for a in used if a.get('persistent_character')],
            'manual_ref_keys': [a['key'] for a in used]
        })
    script = {
        'title': str(extracted.get('title') or 'H3 时间轴项目').strip(),
        'synopsis': f"按用户完整 H3 提示词识别为{shot_count}个生产片段",
        'characters': characters, 'scenes': scenes, 'props': props,
        'shots': shots
    }
    return script, timeline['prompts']

def set_production_job_status(proj, status, message='', **extra):
    job = proj.setdefault('production_job', {})
    job.update({'status': status, 'message': message, 'updated': time.time(), **extra})
    return job

def project_missing_shot_indexes(proj):
    planned = (proj.get('script') or {}).get('shots') or []
    required = {int(shot.get('index', i + 1)) for i, shot in enumerate(planned)}
    completed = {
        int(result.get('index')) for result in (proj.get('shots') or [])
        if result.get('index') is not None and result.get('video_url')
    }
    return sorted(required - completed)

def generate_project_shot(proj, shot_position, send, run_id=None):
    """Generate one project segment from persisted script, prompt, assets and continuity."""
    script = proj.get('script') or {}
    shots = script.get('shots') or []
    shot = shots[shot_position]
    idx = int(shot.get('index', shot_position + 1))
    label = segment_label(shot, idx)
    pid = proj['id']
    out_dir = os.path.join(OUTPUTS_DIR, pid)
    os.makedirs(out_dir, exist_ok=True)
    shot_results = proj.get('shots') or []
    existing = next((result for result in shot_results
                     if result.get('index') == idx and result.get('video_url')), None)
    if existing:
        publish_item_state(
            proj, send, 'shots', idx, 'done', 100, '片段短片已完成', name=label,
            generation_started_at=existing.get('generation_started_at'),
            generation_elapsed_seconds=existing.get('generation_elapsed_seconds'))
        send('shot', {
            "index": idx, "segment_id": existing.get('segment_id', label),
            "video_url": existing['video_url'], "prompt": existing.get('prompt', ''),
            "refs": existing.get('refs', []), "duration": existing.get('duration', shot.get('duration', 8)),
            "generation_started_at": existing.get('generation_started_at'),
            "generation_elapsed_seconds": existing.get('generation_elapsed_seconds'),
            "cached": True,
        })
        return existing, None

    prompts_cache = proj.get('prompts', {})
    h3_prompt = prompts_cache.get(str(idx))
    continuity_needed = shot_position > 0 and segment_needs_previous_tail(shot, h3_prompt or '')
    continuity_path = None
    if continuity_needed:
        previous_idx = int(shots[shot_position - 1].get('index', shot_position))
        previous_result = next((result for result in shot_results if result.get('index') == previous_idx), None)
        if not previous_result or not previous_result.get('path'):
            message = f"{label}需要承接上一片段，但上一片段视频尚未保存"
            publish_item_state(proj, send, 'shots', idx, 'failed', 0, message, name=label)
            send('error', {"stage": 3, "msg": message})
            return None, message
        previous_path = previous_result['path']
        if not os.path.isabs(previous_path):
            previous_path = os.path.join(BASE_DIR, previous_path)
        continuity_path = os.path.join(out_dir, f"continuity_{previous_idx:02d}_to_{idx:02d}.png")
        publish_item_state(proj, send, 'shots', idx, 'prompting', 4, '正在提取上一片段尾帧', name=label)
        tail_error = extract_video_tail_frame(previous_path, continuity_path)
        if tail_error:
            message = f"{label}连续性首帧准备失败: {tail_error}"
            publish_item_state(proj, send, 'shots', idx, 'failed', 0, message, name=label)
            send('error', {"stage": 3, "msg": message})
            return None, message
        send('shot_status', {"index": idx, "status": "prompt", "msg": f"{label}：已提取上一片段尾帧作为首帧连续性参考"})

    ref_paths, board_ref_index, continuity_ref_index = assemble_shot_refs(
        shot, proj.get('assets', {}), proj.get('generate_storyboards', True),
        continuity_path=continuity_path)
    if not ref_paths:
        message = f"{label}无可用参考图"
        publish_item_state(proj, send, 'shots', idx, 'failed', 0, '无可用参考图', name=label)
        send('error', {"stage": 3, "msg": message})
        return None, message

    if h3_prompt:
        publish_item_state(proj, send, 'shots', idx, 'prompting', 5, '正在读取预生成片段提示词', name=label)
        send('shot_status', {"index": idx, "status": "prompt", "msg": f"{label}：使用预生成提示词", "prompt": h3_prompt})
    else:
        publish_item_state(proj, send, 'shots', idx, 'prompting', 3, 'AI正在编排片段提示词', name=label)
        send('shot_status', {"index": idx, "status": "prompt", "msg": f"{label}：AI正在编排内部镜头..."})
        h3_prompt, prompt_error = gen_shot_h3_prompt(
            shot, ref_paths, board_ref_index, character_profiles_by_name(script),
            continuity_ref_index=continuity_ref_index)
        if not h3_prompt:
            message = prompt_error or '提示词生成失败'
            publish_item_state(proj, send, 'shots', idx, 'failed', 0, message, name=label)
            send('error', {"stage": 3, "msg": f"{label}提示词生成失败: {message}"})
            return None, message
        h3_prompt = filter_slow_motion(h3_prompt)
        prompts_cache[str(idx)] = h3_prompt
        proj['prompts'] = prompts_cache
        save_project(proj)

    h3_prompt = apply_storyboard_reference(h3_prompt, board_ref_index)
    h3_prompt = apply_continuity_reference(h3_prompt, continuity_ref_index)
    h3_prompt = apply_character_visual_locks(h3_prompt, shot, character_profiles_by_name(script))
    generation_started_at = time.time()
    publish_item_state(
        proj, send, 'shots', idx, 'generating', 8, 'H3正在加载模型并渲染片段短片', name=label,
        generation_started_at=generation_started_at, generation_elapsed_seconds=None)
    send('shot_status', {
        "index": idx, "status": "render", "msg": f"{label}：H3正在渲染多镜头片段（耗时较长）...",
        "prompt": h3_prompt, "generation_started_at": generation_started_at})
    progress_cb = make_progress_publisher(
        proj, send, 'shots', idx, None, label, 'H3片段正在采样', start=10, end=95)
    raise_if_project_stopped(pid, run_id)
    video_info, video_error = gen_video_r2v(
        h3_prompt, ref_paths, duration=shot.get('duration', 8),
        save_name=f"shot_{idx:02d}.mp4", progress_cb=progress_cb,
        preserve_prompt=(proj.get('input_mode') == 'h3_prompt'),
        project_pid=pid, run_id=run_id)
    if video_info is None:
        elapsed = round(time.time() - generation_started_at, 1)
        message = video_error or '视频生成失败'
        publish_item_state(
            proj, send, 'shots', idx, 'failed', 0, message, name=label,
            generation_started_at=generation_started_at, generation_elapsed_seconds=elapsed)
        send('error', {"stage": 3, "msg": f"{label}视频生成失败: {message}"})
        return None, message

    video_path = os.path.join(out_dir, f"shot_{idx:02d}.mp4")
    comfy_download(video_info, video_path)
    elapsed = round(time.time() - generation_started_at, 1)
    video_url = f"/file/outputs/{pid}/shot_{idx:02d}.mp4"
    result = {
        "index": idx, "segment_id": label, "video_url": video_url,
        "prompt": h3_prompt, "path": video_path,
        "refs": [os.path.relpath(path, BASE_DIR) for path in ref_paths],
        "duration": shot.get('duration', 8),
        "generation_started_at": generation_started_at,
        "generation_elapsed_seconds": elapsed,
        "continuity_from_previous": continuity_needed,
        "continuity_frame": os.path.relpath(continuity_path, BASE_DIR) if continuity_path else None,
    }
    proj['shots'] = [row for row in shot_results if row.get('index') != idx] + [result]
    publish_item_state(
        proj, send, 'shots', idx, 'done', 100, '片段短片已完成', name=label,
        generation_started_at=generation_started_at, generation_elapsed_seconds=elapsed)
    send('shot', {key: result[key] for key in (
        'index', 'video_url', 'prompt', 'refs', 'duration',
        'generation_started_at', 'generation_elapsed_seconds')})
    return result, None

def finalize_project_video(proj, send, run_id=None):
    """Compose completed segments while keeping finalization errors separate from shot status."""
    missing = project_missing_shot_indexes(proj)
    if missing:
        return False, f"仍有{len(missing)}个片段待生成"
    raise_if_project_stopped(proj['id'], run_id)
    set_production_job_status(proj, 'merging', '正在合并所有片段', stage='finalizing')
    save_project(proj)
    send('stage', {"stage": 4, "name": "视频合成", "status": "running", "msg": "正在合并所有片段..."})
    try:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            message = "未检测到ffmpeg（整合包tools/ffmpeg与系统PATH均无）"
            set_production_job_status(proj, 'failed', message, stage='finalizing')
            save_project(proj)
            send('error', {"stage": 4, "msg": message})
            return False, message
        out_dir = os.path.join(OUTPUTS_DIR, proj['id'])
        os.makedirs(out_dir, exist_ok=True)
        ordered = sorted(proj.get('shots') or [], key=lambda result: result['index'])
        with open(os.path.join(out_dir, 'concat.txt'), 'w', encoding='utf-8') as stream:
            for result in ordered:
                stream.write(f"file '{os.path.basename(result['path'])}'\n")
        proc = subprocess.run(
            [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', 'concat.txt', '-c', 'copy', 'final.mp4'],
            cwd=out_dir, capture_output=True, text=True, timeout=300)
        raise_if_project_stopped(proj['id'], run_id)
        if proc.returncode != 0:
            proc = subprocess.run(
                [ffmpeg, '-y', '-f', 'concat', '-safe', '0', '-i', 'concat.txt', '-c:v', 'libx264', '-c:a', 'aac', 'final.mp4'],
                cwd=out_dir, capture_output=True, text=True, timeout=600)
            raise_if_project_stopped(proj['id'], run_id)
        if proc.returncode != 0:
            message = f"ffmpeg合并失败: {proc.stderr[-300:]}"
            set_production_job_status(proj, 'failed', message, stage='finalizing')
            save_project(proj)
            send('error', {"stage": 4, "msg": message})
            return False, message
    except ProjectStopRequested:
        raise
    except Exception as exc:
        message = f"ffmpeg执行异常: {exc}"
        set_production_job_status(proj, 'failed', message, stage='finalizing')
        save_project(proj)
        send('error', {"stage": 4, "msg": message})
        return False, message
    proj['final'] = f"/file/outputs/{proj['id']}/final.mp4"
    set_production_job_status(proj, 'done', '成片已生成', stage='complete')
    save_project(proj)
    send('stage', {"stage": 4, "name": "视频合成", "status": "done"})
    send('final', {"video_url": proj['final'], "title": proj.get('title', '短剧')})
    return True, None

def _run_pipeline(pid, idea, send, custom_assets=False, input_mode='story', run_id=None):
    """完整短剧生成管线，send(event, data)推送进度"""
    proj = load_project(pid)
    if proj is None:
        input_mode = 'h3_prompt' if input_mode == 'h3_prompt' else 'story'
        proj = {"id": pid, "idea": idea, "input_mode": input_mode, "title": "", "script": None, "assets": {}, "shots": [], "final": None, "created": time.time()}
        proj['scene_reference_mode'] = resolve_scene_reference_mode(CONFIG.get('style'), CONFIG.get('scene_reference_mode'))
        if input_mode == 'h3_prompt':
            proj['manual_h3_prompt'] = idea
    else:
        input_mode = proj.get('input_mode', input_mode)
        if input_mode == 'h3_prompt':
            idea = proj.get('manual_h3_prompt') or proj.get('idea') or idea
    if custom_assets:
        proj['custom_assets'] = True
    set_production_job_status(proj, 'running', '正在生成短剧', stage='production')
    save_project(proj)
    raise_if_project_stopped(pid, run_id)
    out_dir = os.path.join(OUTPUTS_DIR, pid)
    os.makedirs(out_dir, exist_ok=True)

    # ---------- 阶段1：剧本解析 ----------
    script = proj.get('script')
    if not script:
        manual_mode = input_mode == 'h3_prompt'
        timeline = None
        if manual_mode:
            timeline, timeline_err = parse_h3_timeline(idea)
            if timeline_err:
                send('error', {"stage": 1, "msg": f"H3时间轴解析失败: {timeline_err}"})
                return
        stage_msg = f"已识别{len(timeline['segments'])}个生产片段，正在一次性提取参考资产（不会改写片段提示词）..." if manual_mode else "AI编剧正在规划剧情片段与片段内镜头..."
        send('stage', {"stage": 1, "name": "H3 提示词解析" if input_mode == 'h3_prompt' else "剧本解析", "status": "running", "msg": stage_msg})
        if exclusive_on():
            send('stage', {"stage": 1, "name": "剧本解析", "status": "running", "msg": "互斥模式：关闭ComfyUI，集中显存运行LLM..."})
            stop_comfyui()
        ok, err = ensure_local_llm()
        if not ok:
            send('error', {"stage": 1, "msg": f"LLM服务不可用: {err}"})
            return
        sys_prompt = MANUAL_H3_ASSET_PROMPT if manual_mode else build_script_prompt()
        want_count = None if manual_mode else get_shot_count_limit()
        content, err = llm_chat([
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": idea}
        ], max_tokens=3000 if manual_mode else 8000, temperature=0.2 if manual_mode else 0.75)
        raise_if_project_stopped(pid, run_id)
        if not content:
            send('error', {"stage": 1, "msg": f"剧本生成失败: {err}"})
            return
        extracted = parse_json_from_text(content)
        manual_prompts = None
        if manual_mode and extracted:
            script, manual_prompts = build_manual_h3_script(idea, extracted, timeline)
        else:
            script = extracted
        # 指定片段数时：不足则重试一次（shots字段为历史兼容名）
        if want_count and script and script.get('shots') and len(script['shots']) < want_count:
            print(f"[剧本] 片段数不足({len(script['shots'])}/{want_count})，重试一次")
            content2, _ = llm_chat([
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": idea},
                {"role": "assistant", "content": content},
                {"role": "user", "content": f"片段数量不对：你给了{len(script['shots'])}个，必须恰好{want_count}个生产片段。请重新输出完整JSON（shots兼容数组中严格{want_count}项）。直接输出JSON，不要解释。"}
            ], max_tokens=8000, temperature=0.6)
            raise_if_project_stopped(pid, run_id)
            if content2:
                script2 = parse_json_from_text(content2)
                if script2 and script2.get('shots'):
                    script = script2
        if not script or not script.get('shots'):
            send('error', {"stage": 1, "msg": "剧本解析失败：模型输出格式异常，请重试", "raw": content[:500]})
            return
        # 指定片段数时：超出则截断并重排index
        if want_count and len(script['shots']) > want_count:
            script['shots'] = script['shots'][:want_count]
            for i, s in enumerate(script['shots']):
                s['index'] = i + 1
        # 清洗：过滤慢放词汇、限制时长3~15秒；无条件重排index（防止LLM漏输出index导致前端占位框错位）
        for i, shot in enumerate(script['shots']):
            shot['index'] = i + 1
            shot['segment_id'] = str(shot.get('segment_id') or f"EP01-{i + 1:02d}")
            continuity_value = shot.get('continue_from_previous', False)
            shot['continue_from_previous'] = i > 0 and (
                continuity_value is True or str(continuity_value).strip().lower() in ('1', 'true', 'yes', '是'))
            try:
                d = int(shot.get('duration', 8))
            except (TypeError, ValueError):
                d = 8
            shot['duration'] = max(3, min(d, 15))
            if not manual_mode:
                shot['action'] = filter_slow_motion(shot.get('action', ''))
                shot['camera'] = filter_slow_motion(shot.get('camera', ''))
        for c in script.get('characters', []):
            c['appearance'] = filter_slow_motion(c.get('appearance', ''))
        # 一致性补全：镜头引用的角色/场景/道具必须存在于资产清单，缺失则自动补条目（保证参考图编号不错位）
        chars = script.setdefault('characters', [])
        scenes_l = script.setdefault('scenes', [])
        props_l = script.setdefault('props', [])
        char_names = {c.get('name') for c in chars}
        scene_names = {s.get('name') for s in scenes_l}
        prop_names = {p.get('name') for p in props_l}
        for shot in script['shots']:
            shot_chars = [ch for ch in shot.get('characters', []) if ch]
            for ch in shot_chars:
                if ch not in char_names:
                    chars.append({"name": ch, "appearance": f"{ch}，形象与全剧其他角色风格统一", "personality": ""})
                    char_names.add(ch)
            shot['characters'] = shot_chars
            sc = shot.get('scene', '')
            if sc and sc not in scene_names:
                scenes_l.append({"name": sc, "description": f"{sc}，与全剧场景风格、时间、光线保持一致"})
                scene_names.add(sc)
            shot_props = [p for p in shot.get('props', []) if p]
            for pn in shot_props:
                if pn not in prop_names:
                    props_l.append({"name": pn, "description": f"{pn}，写实质感，与全剧年代风格一致"})
                    prop_names.add(pn)
            shot['props'] = shot_props
        proj['script'] = script
        proj['title'] = script.get('title', '未命名短剧')
        if manual_mode:
            proj['manual_h3_prompt'] = idea
            proj['timeline_segments'] = timeline['segments']
            proj['prompts'] = manual_prompts
        save_project(proj)
        send('stage', {"stage": 1, "name": "剧本解析", "status": "done", "script": script})
    else:
        send('stage', {"stage": 1, "name": "剧本解析", "status": "done", "script": script, "cached": True})

    if normalize_script_dialogue(script):
        proj['script'] = script
        save_project(proj)
    item_states = initialize_item_states(proj, script)
    send('item_states', item_states)

    # ---------- 自定义参考图模式：暂停等待用户上传/确认（须在批量提示词之前，保证提示词以上传图为准） ----------
    if proj.get('custom_assets') and not proj.get('assets_confirmed'):
        send('wait_assets', {"msg": "自定义参考图模式：请在资产卡片上传您准备的图片（可只传部分，空缺将由AI生成），完成后点击「继续生成」"})
        wait_start = time.time()
        while True:
            time.sleep(2)
            raise_if_project_stopped(pid, run_id)
            fresh = load_project(pid)
            if fresh and fresh.get('assets_confirmed'):
                proj = fresh
                script = proj.get('script', {})
                break
            if time.time() - wait_start > 3600:
                send('error', {"stage": 2, "msg": "等待参考图确认超时（1小时）"})
                return
        send('stage', {"stage": 2, "name": "资产生成", "status": "running", "msg": "参考图已确认，正在继续..."})

    style = CONFIG.get('style', '电影写实')
    characters = script.get('characters', [])
    scenes = script.get('scenes', [])
    shots = script.get('shots', [])

    # ---------- 互斥模式：LLM批量撰写全部片段提示词，完成后关闭LLM集中显存给ComfyUI ----------
    if exclusive_on():
        prompts_cache = proj.get('prompts', {})
        todo = []
        for i, shot in enumerate(shots):
            idx_key = str(shot.get('index', i + 1))
            cached = prompts_cache.get(idx_key)
            cached_valid = bool(cached)
            if cached_valid and input_mode != 'h3_prompt':
                _, board_index, continuity_index = assemble_shot_refs(
                    shot, proj.get('assets', {}), proj.get('generate_storyboards', True),
                    reserve_continuity=(i > 0 and segment_needs_previous_tail(shot, cached)))
                ref_count = max(board_index or 0, continuity_index or 0, len(assemble_shot_ref_paths(
                    shot, proj.get('assets', {}), proj.get('generate_storyboards', True))))
                cached_valid = not validate_h3_production_prompt(
                    cached, shot_dialogue_lines(shot), ref_count)
            if not cached_valid:
                todo.append((i, shot))
        if todo:
            send('stage', {"stage": 1, "name": "批量提示词", "status": "running", "msg": f"互斥模式：LLM正在批量撰写剩余{len(todo)}条片段提示词，写完即关闭..."})
            ok, err = prepare_llm_stage()
            if not ok:
                send('error', {"stage": 1, "msg": f"LLM服务不可用: {err}"})
                return
            prompt_errors = []
            for i, shot in todo:
                raise_if_project_stopped(pid, run_id)
                idx = shot.get('index', i + 1)
                label = segment_label(shot, idx)
                continuity_needed = i > 0 and segment_needs_previous_tail(shot)
                publish_item_state(proj, send, 'shots', idx, 'prompting', 3, 'AI正在预生成片段提示词', name=label)
                send('shot_status', {"index": idx, "status": "prompt", "msg": f"{label}：AI正在编排片段内镜头..."})
                prompt_refs, board_ref_index, continuity_ref_index = assemble_shot_refs(
                    shot, proj.get('assets', {}), proj.get('generate_storyboards', True),
                    reserve_continuity=continuity_needed)
                p, perr = gen_shot_h3_prompt(
                    shot, prompt_refs, board_ref_index, character_profiles_by_name(proj.get('script')),
                    continuity_ref_index=continuity_ref_index)
                if not p:
                    publish_item_state(proj, send, 'shots', idx, 'failed', 0, perr or '提示词生成失败', name=label)
                    send('error', {"stage": 1, "msg": f"{label}提示词生成失败: {perr}"})
                    prompt_errors.append(f"{label}: {perr or '提示词生成失败'}")
                    continue
                prompts_cache[str(idx)] = filter_slow_motion(p)
                proj['prompts'] = prompts_cache
                proj['prompt_format_version'] = H3_PROMPT_FORMAT_VERSION
                publish_item_state(proj, send, 'shots', idx, 'waiting', 5, '片段提示词已就绪，等待视频生成', name=label)
            if prompt_errors:
                stop_local_llm()
                send('stage', {"stage": 1, "name": "批量提示词", "status": "failed",
                               "msg": f"{len(prompt_errors)}条提示词未通过，其余已保存，可直接重试失败项"})
                send('error', {"stage": 1, "msg": "；".join(prompt_errors[:5])})
                return
            send('stage', {"stage": 1, "name": "批量提示词", "status": "done", "msg": f"互斥模式：{len(shots)}条提示词全部就绪"})
        stop_local_llm()

    # ---------- 阶段2：资产生成（角色/场景参考图） ----------
    send('stage', {"stage": 2, "name": "资产生成", "status": "running", "msg": ("互斥模式：LLM已关闭，正在启动ComfyUI生成参考图..." if exclusive_on() else "正在生成角色与场景参考图...")})
    if not prepare_comfy_stage():
        send('error', {"stage": 2, "msg": f"ComfyUI不可用或互斥切换失败: {comfy_url()}"})
        return
    assets = proj.get('assets', {})
    props = script.get('props', [])
    total_assets = len(characters) + len(scenes) + len(props)
    done_count = 0
    planned_asset_keys = ([f"char_{item['name']}" for item in characters] +
                          [f"scene_{item['name']}" for item in scenes] +
                          [f"prop_{item['name']}" for item in props])
    if any(key not in assets or not os.path.exists(assets[key].get('path', ''))
           for key in planned_asset_keys):
        ready, preflight_error = image_workflow_preflight()
        if not ready:
            send('error', {"stage": 2, "msg": preflight_error})
            return
    for c in characters:
        raise_if_project_stopped(pid, run_id)
        key = f"char_{c['name']}"
        if key in assets and os.path.exists(assets[key].get('path', '')):
            done_count += 1
            publish_item_state(proj, send, 'assets', key, 'done', 100, '参考图已完成', 'character', c['name'])
            send('asset', {"type": "character", "name": c['name'], "url": f"/file/assets/{os.path.basename(assets[key]['path'])}", "cached": True})
            continue
        # 四宫格三视图：横版4格 = 面部特写 + 全身正面/背面/侧面
        prompt = (f"{style}风格，角色设定参考图，四宫格三视图排版：一张横版图片从左到右均分为4个竖向分格——"
                  f"第1格：角色面部特写（五官清晰、表情中性、直视镜头）；"
                  f"第2格：同一角色全身正面站立照；第3格：同一角色全身背面站立照；第4格：同一角色全身侧面站立照。"
                  f"角色设定：{ensure_race_desc(c['appearance'])}。"
                  f"四个分格中角色的脸型、发型、服装、体型、肤色必须完全一致，纯色素净背景，站姿端正，画质精美，细节丰富。")
        publish_item_state(proj, send, 'assets', key, 'generating', 3, '正在加载模型并生成角色图', 'character', c['name'])
        progress_cb = make_progress_publisher(proj, send, 'assets', key, 'character', c['name'], '角色图正在采样')
        path, err = gen_image(prompt, progress_cb=progress_cb, project_pid=pid, run_id=run_id)
        if err:
            publish_item_state(proj, send, 'assets', key, 'failed', 0, err, 'character', c['name'])
            send('error', {"stage": 2, "msg": f"角色[{c['name']}]参考图生成失败: {err}"})
            return
        assets[key] = {"path": path, "kind": "character"}
        done_count += 1
        proj['assets'] = assets
        publish_item_state(proj, send, 'assets', key, 'done', 100, '角色参考图已完成', 'character', c['name'])
        send('asset', {"type": "character", "name": c['name'], "url": f"/file/assets/{os.path.basename(path)}"})
        send('progress', {"stage": 2, "done": done_count, "total": total_assets})
    for s in scenes:
        raise_if_project_stopped(pid, run_id)
        key = f"scene_{s['name']}"
        if key in assets and os.path.exists(assets[key].get('path', '')):
            done_count += 1
            publish_item_state(proj, send, 'assets', key, 'done', 100, '场景图已完成', 'scene', s['name'])
            send('asset', {"type": "scene", "name": s['name'], "url": f"/file/assets/{os.path.basename(assets[key]['path'])}", "cached": True})
            continue
        # 场景图：写实类使用真实拍摄机位；动漫/插画类保留作者原有的空间概念设定图。
        requested_scene_mode = proj.get('scene_reference_mode', 'auto')
        scene_mode = resolve_scene_reference_mode(style, requested_scene_mode)
        desc = sanitize_scene_text(s.get('description', ''), [c.get('name', '') for c in characters],
                                   keep_background_life=(scene_mode == 'photo'))
        prompt, scene_mode = build_scene_image_prompt(style, desc, scene_mode)
        mode_label = '真实取景' if scene_mode == 'photo' else '概念设定'
        publish_item_state(proj, send, 'assets', key, 'generating', 3, f'正在生成{mode_label}场景图', 'scene', s['name'])
        progress_cb = make_progress_publisher(proj, send, 'assets', key, 'scene', s['name'], f'{mode_label}场景图正在采样')
        path, err = gen_image(prompt, progress_cb=progress_cb, project_pid=pid, run_id=run_id)
        if err:
            publish_item_state(proj, send, 'assets', key, 'failed', 0, err, 'scene', s['name'])
            send('error', {"stage": 2, "msg": f"场景[{s['name']}]参考图生成失败: {err}"})
            return
        assets[key] = {"path": path, "kind": "scene"}
        done_count += 1
        proj['assets'] = assets
        publish_item_state(proj, send, 'assets', key, 'done', 100, '场景参考图已完成', 'scene', s['name'])
        send('asset', {"type": "scene", "name": s['name'], "url": f"/file/assets/{os.path.basename(path)}"})
        send('progress', {"stage": 2, "done": done_count, "total": total_assets})
    for p in props:
        raise_if_project_stopped(pid, run_id)
        key = f"prop_{p['name']}"
        if key in assets and os.path.exists(assets[key].get('path', '')):
            done_count += 1
            publish_item_state(proj, send, 'assets', key, 'done', 100, '道具图已完成', 'prop', p['name'])
            send('asset', {"type": "prop", "name": p['name'], "url": f"/file/assets/{os.path.basename(assets[key]['path'])}", "cached": True})
            continue
        prompt = f"{style}风格，关键道具特写参考图（无人物）。{p['description']}。纯色素净背景，道具居中完整展示，材质纹理细节清晰，画质精美。"
        publish_item_state(proj, send, 'assets', key, 'generating', 3, '正在加载模型并生成道具图', 'prop', p['name'])
        progress_cb = make_progress_publisher(proj, send, 'assets', key, 'prop', p['name'], '道具图正在采样')
        path, err = gen_image(prompt, progress_cb=progress_cb, project_pid=pid, run_id=run_id)
        if err:
            publish_item_state(proj, send, 'assets', key, 'failed', 0, err, 'prop', p['name'])
            send('error', {"stage": 2, "msg": f"道具[{p['name']}]参考图生成失败: {err}"})
            return
        assets[key] = {"path": path, "kind": "prop"}
        done_count += 1
        proj['assets'] = assets
        publish_item_state(proj, send, 'assets', key, 'done', 100, '道具参考图已完成', 'prop', p['name'])
        send('asset', {"type": "prop", "name": p['name'], "url": f"/file/assets/{os.path.basename(path)}"})
        send('progress', {"stage": 2, "done": done_count, "total": total_assets})
    send('stage', {"stage": 2, "name": "资产生成", "status": "done"})

    # ---------- 阶段3：片段视频生成 ----------
    send('stage', {"stage": 3, "name": "片段视频", "status": "running", "msg": "正在逐片生成视频..."})
    for position, _shot in enumerate(shots):
        raise_if_project_stopped(pid, run_id)
        _result, error = generate_project_shot(proj, position, send, run_id=run_id)
        if error:
            set_production_job_status(proj, 'failed', error, stage='production')
            save_project(proj)
            return
        send('progress', {
            "stage": 3,
            "done": len(proj.get('shots') or []),
            "total": len(shots),
        })
    send('stage', {"stage": 3, "name": "片段视频", "status": "done"})

    # ---------- 阶段4：视频合成 ----------
    raise_if_project_stopped(pid, run_id)
    finalize_project_video(proj, send, run_id=run_id)

def _run_pipeline_job(pid, idea, send, run_id, custom_assets=False, input_mode='story'):
    """Run one persisted project job and converge every exit to a terminal state."""
    send('job_status', {'run_id': run_id, 'status': 'running', 'message': '项目生成已开始'})
    try:
        _run_pipeline(pid, idea, send, custom_assets=custom_assets,
                      input_mode=input_mode, run_id=run_id)
    except ProjectStopRequested as exc:
        message = str(exc) or '已停止，完成的结果已保留'
        finalize_project_stopped(pid, run_id, message)
        send('job_status', {'status': 'stopped', 'message': message})
        end_production_job(pid, run_id)
        return
    except Exception:
        update_production_job(pid, run_id, status='failed', message='任务异常结束，可从断点继续', finished=time.time())
        end_production_job(pid, run_id)
        raise
    proj = load_project(pid) or {}
    job = proj.get('production_job') or {}
    if job.get('run_id') != run_id:
        end_production_job(pid, run_id)
        return
    if proj.get('final'):
        message = ('停止请求到达时项目已完成' if job.get('stop_requested') else '项目制作完成')
        update_production_job(pid, run_id, status='done', message=message, finished=time.time())
        send('job_status', {'status': 'done', 'message': message})
    elif job.get('stop_requested'):
        message = job.get('message') or '已停止，完成的结果已保留'
        finalize_project_stopped(pid, run_id, message)
        send('job_status', {'status': 'stopped', 'message': message})
    elif job.get('status') == 'failed':
        message = job.get('message') or '任务已结束，可从断点继续'
        update_production_job(pid, run_id, finished=time.time())
        send('job_status', {'status': 'failed', 'message': message})
    else:
        update_production_job(pid, run_id, status='failed', message='任务已结束，可从断点继续', finished=time.time())
        send('job_status', {'status': 'failed', 'message': '任务已结束，可从断点继续'})
    end_production_job(pid, run_id)

def run_pipeline(pid, idea, send, custom_assets=False, input_mode='story'):
    run_id = None
    try:
        run_id = begin_production_job(pid, 'pipeline')
        if not run_id:
            # New projects are created inside the legacy pipeline, so seed the shell first.
            save_project({
                'id': pid, 'idea': idea, 'input_mode': input_mode, 'title': '',
                'script': None, 'assets': {}, 'shots': [], 'final': None,
                'created': time.time(),
                'scene_reference_mode': resolve_scene_reference_mode(
                    CONFIG.get('style'), CONFIG.get('scene_reference_mode')),
                **({'manual_h3_prompt': idea} if input_mode == 'h3_prompt' else {}),
            })
            run_id = begin_production_job(pid, 'pipeline')
        return _run_pipeline_job(
            pid, idea, send, run_id, custom_assets=custom_assets, input_mode=input_mode)
    finally:
        if run_id:
            end_production_job(pid, run_id)

# ============================== 单镜头生成器 API ==============================
@app.route('/api/single_shot/upload', methods=['POST'])
def single_shot_upload():
    """上传参考图到 assets/uploads/，返回本地路径与预览URL"""
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({"error": "缺少文件"}), 400
    ext = os.path.splitext(f.filename)[1].lower() or '.png'
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        return jsonify({"error": "仅支持 png/jpg/webp 图片"}), 400
    up_dir = os.path.join(ASSETS_DIR, 'uploads')
    os.makedirs(up_dir, exist_ok=True)
    fname = f"up_{uuid.uuid4().hex[:8]}{ext}"
    f.save(os.path.join(up_dir, fname))
    rel = os.path.join('assets', 'uploads', fname)
    return jsonify({"path": rel, "url": f"/file/{rel}"})

@app.route('/api/single_shot/prompt', methods=['POST'])
def single_shot_prompt():
    """AI整理：把用户的粗略要求改写为H3提示词"""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'r2v')
    if mode not in ('r2v', 'i2v', 't2v'):
        mode = 'r2v'
    idea = (data.get('idea') or '').strip()
    if not idea:
        return jsonify({"error": "请先输入创作要求"}), 400
    try:
        duration = max(3, min(int(data.get('duration', 8)), 15))
    except (TypeError, ValueError):
        duration = 8
    ref_count = max(0, min(int(data.get('ref_count', 1) or 1), 9))
    # 读取已上传的参考图（r2v=各槽位图，i2v=首帧/尾帧），喂给多模态LLM直接看图
    images = load_image_parts(data.get('ref_paths') or []) if mode in ('r2v', 'i2v') else []
    if images:
        ref_count = len(images)
    if exclusive_on():
        stop_comfyui()
    if not ensure_local_llm():
        return jsonify({"error": "LLM服务未就绪"}), 503
    mode_label = {'r2v': '参考生视频(r2v)', 'i2v': '图生视频(i2v)', 't2v': '文生视频(t2v)'}[mode]
    refs_block, pic_rule = build_single_refs_block(mode, ref_count)
    prompt_text = (SINGLE_SHOT_PROMPT
                   .replace('{mode_label}', mode_label)
                   .replace('{refs_block}', refs_block)
                   .replace('{pic_rule}', pic_rule)
                   .replace('{duration}', str(duration))
                   .replace('{idea}', idea[:2000]))
    # 图片只能附加在user消息（system保持纯文本）
    user_content = [{"type": "text", "text": "请按系统要求生成H3提示词。"}] + images
    content, err = llm_chat([
        {"role": "system", "content": prompt_text},
        {"role": "user", "content": user_content}
    ], max_tokens=4096, temperature=0.7)
    if not content:
        return jsonify({"error": f"提示词生成失败: {err}"}), 500
    content = filter_slow_motion(content.strip())
    # 台词兜底校验：用户明确要求说出的台词必须逐字出现在提示词中，缺失则让LLM修复一次
    spoken = re.findall(r'说[：:]?\s*["\'「『]([^"\'」』\n]{2,60})["\'」』]', idea)
    spoken += re.findall(r'说[：:]\s*([^，。,.\n"\'「』]{2,60})', idea)
    missing = [s.strip() for s in spoken if s.strip() and s.strip() not in content]
    if missing:
        print(f"[单镜头] 台词缺失，触发修复: {missing}")
        content = filter_slow_motion(repair_missing_dialogue(prompt_text, user_content, content, missing))
    return jsonify({"prompt": content})

@app.route('/api/single_shot/generate', methods=['POST'])
def single_shot_generate():
    """启动单镜头视频生成任务（后台线程），返回task_id供轮询"""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'r2v')
    if mode not in ('r2v', 'i2v', 't2v'):
        mode = 'r2v'
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return jsonify({"error": "提示词为空"}), 400
    ref_paths = [p for p in (data.get('ref_paths') or []) if isinstance(p, str)]
    # 安全校验：只允许项目目录内的路径；统一转绝对路径（避免依赖启动时的CWD）
    max_refs = {'r2v': 9, 'i2v': 2, 't2v': 0}[mode]
    safe_paths = []
    for p in ref_paths[:max_refs]:
        ap = os.path.abspath(os.path.join(BASE_DIR, p))
        if ap.startswith(BASE_DIR) and os.path.exists(ap):
            safe_paths.append(ap)
    if mode in ('r2v', 'i2v') and not safe_paths:
        return jsonify({"error": f"{mode}模式需要至少1张参考图"}), 400
    try:
        duration = max(3, min(int(data.get('duration', 8)), 15))
    except (TypeError, ValueError):
        duration = 8
    task_id = uuid.uuid4().hex[:12]
    SINGLE_TASKS[task_id] = {"status": "running", "msg": "已加入队列", "video_url": "", "mode": mode}
    threading.Thread(target=single_shot_worker,
                     args=(task_id, mode, prompt, safe_paths, duration), daemon=True).start()
    return jsonify({"task_id": task_id})

@app.route('/api/single_shot/status/<task_id>')
def single_shot_status(task_id):
    t = SINGLE_TASKS.get(task_id)
    if not t:
        return jsonify({"status": "error", "msg": "任务不存在"}), 404
    return jsonify(t)

def recover_interrupted_shot_retries(proj):
    """Recover persisted production state only when no process still owns this project."""
    pid = proj.get('id')
    states = (proj.get('item_states') or {}).get('shots') or {}
    interrupted = [
        state for state in states.values()
        if state.get('retry_count') and state.get('status') in ('retrying', 'prompting', 'generating')
    ]
    production_job = proj.get('production_job') or {}
    if not interrupted and production_job.get('status') not in ACTIVE_PRODUCTION_STATUSES:
        return False
    recovery_id = f"recovery-{uuid.uuid4().hex}"
    if not pid or not claim_project_job(pid, recovery_id):
        return False
    try:
        current = load_project(pid)
        if not current:
            return False
        states = (current.get('item_states') or {}).get('shots') or {}
        interrupted = [
            state for state in states.values()
            if state.get('retry_count') and state.get('status') in ('retrying', 'prompting', 'generating')
        ]
        production_job = current.get('production_job') or {}
        job_status = production_job.get('status')
        if not interrupted and job_status not in ACTIVE_PRODUCTION_STATUSES:
            return False
        if job_status == 'stopping':
            finalize_project_stopped(
                pid, production_job.get('run_id'),
                production_job.get('message') or '项目已经安全停止，完成的结果已保留')
            return True
        message = ("服务重启导致视频合成中断，可从断点继续"
                   if job_status == 'merging'
                   else "服务重启导致生成任务中断，可继续生成")
        for state in states.values():
            if not state.get('retry_count') or state.get('status') not in ('retrying', 'prompting', 'generating'):
                continue
            message = "服务重启导致片段重试中断，可再次重新生成"
            state.update({
                'status': 'failed', 'progress': 0, 'message': message,
                'error': message, 'updated': time.time(),
            })
        stage = production_job.get('stage') or ('finalizing' if job_status == 'merging' else 'production')
        set_production_job_status(current, 'failed', message, stage=stage)
        save_project(current)
        return True
    finally:
        release_project_job(pid, recovery_id)

def run_project_shot_retry(pid, index, job_id):
    """Retry exactly one failed project segment and leave every completed segment untouched."""
    def capture(event, data):
        return None

    def fail_retry(project, planned_shot, message):
        state = (((project.get('item_states') or {}).get('shots') or {}).get(str(index), {}))
        set_item_state(
            project, 'shots', index, 'failed', 0, message,
            name=state.get('name') or segment_label(planned_shot, index),
        )
        set_production_job_status(project, 'failed', message, stage='production')
        save_project(project)

    try:
        proj = load_project(pid)
        if not proj:
            return
        current_job = proj.get('production_job') or {}
        if current_job.get('run_id') != job_id:
            now = time.time()
            proj['production_job'] = {
                'run_id': job_id, 'job_id': job_id, 'kind': 'shot_retry',
                'status': 'retrying', 'stop_requested': False,
                'current_prompt_id': None, 'current_prompt_kind': None,
                'cancel_result': None, 'message': f'片段{index}正在重试',
                'stage': 'production', 'started': now, 'updated': now,
            }
            save_project(proj, preserve_runtime=False)
        raise_if_project_stopped(pid, job_id)
        shots = (proj.get('script') or {}).get('shots') or []
        position = next((pos for pos, shot in enumerate(shots)
                         if int(shot.get('index', pos + 1)) == index), None)
        if position is None:
            set_production_job_status(proj, 'failed', f'片段{index}不存在', stage='production')
            save_project(proj)
            return
        planned = shots[position]

        if not (proj.get('prompts') or {}).get(str(index)):
            raise_if_project_stopped(pid, job_id)
            publish_item_state(
                proj, capture, 'shots', index, 'prompting', 2,
                '正在准备片段提示词', name=segment_label(planned, index))
            ready, service_error = prepare_llm_stage()
            if not ready:
                fail_retry(proj, planned, f"LLM服务不可用: {service_error}")
                return
            continuity_needed = position > 0 and segment_needs_previous_tail(planned)
            prompt_refs, board_ref_index, continuity_ref_index = assemble_shot_refs(
                planned, proj.get('assets', {}), proj.get('generate_storyboards', True),
                reserve_continuity=continuity_needed)
            if not prompt_refs:
                fail_retry(proj, planned, '无可用参考图')
                return
            prompt, prompt_error = gen_shot_h3_prompt(
                planned, prompt_refs, board_ref_index,
                character_profiles_by_name(proj.get('script')),
                continuity_ref_index=continuity_ref_index)
            if not prompt:
                fail_retry(proj, planned, prompt_error or '提示词生成失败')
                return
            proj.setdefault('prompts', {})[str(index)] = filter_slow_motion(prompt)
            proj['prompt_format_version'] = H3_PROMPT_FORMAT_VERSION
            save_project(proj)

        if not prepare_comfy_stage():
            fail_retry(proj, planned, f"ComfyUI不可用或互斥切换失败: {comfy_url()}")
            return
        raise_if_project_stopped(pid, job_id)
        result, error = generate_project_shot(proj, position, capture, run_id=job_id)
        if error or not result:
            set_production_job_status(proj, 'failed', error or '片段重试未完成', stage='production')
            save_project(proj)
            return
        missing = project_missing_shot_indexes(proj)
        if missing:
            message = f"片段{index}重试完成，仍有{len(missing)}个片段待生成"
            set_production_job_status(proj, 'partial', message, stage='production')
            save_project(proj)
            return
        raise_if_project_stopped(pid, job_id)
        finalize_project_video(proj, capture, run_id=job_id)
    except ProjectStopRequested as exc:
        message = str(exc) or '已停止，完成的结果已保留'
        finalize_project_stopped(pid, job_id, message)
    except Exception as exc:
        print(f"[片段重试] {pid}/{index} 异常: {exc}")
        proj = load_project(pid)
        if proj:
            completed = next((shot for shot in (proj.get('shots') or [])
                              if shot.get('index') == index and shot.get('video_url')), None)
            if not completed:
                state = (((proj.get('item_states') or {}).get('shots') or {}).get(str(index), {}))
                set_item_state(
                    proj, 'shots', index, 'failed', 0, '片段重试异常，请查看服务日志',
                    name=state.get('name'))
            set_production_job_status(proj, 'failed', '片段重试异常，请查看服务日志', stage='production')
            save_project(proj)
    finally:
        release_project_job(pid, job_id)

@app.route('/api/project/<pid>/shot/<int:index>/retry', methods=['POST'])
def project_shot_retry(pid, index):
    if not re.fullmatch(r'[0-9a-zA-Z_-]{1,64}', pid):
        return jsonify({"ok": False, "msg": "非法项目ID"}), 400
    proj = load_project(pid)
    if not proj:
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    if recover_interrupted_shot_retries(proj):
        proj = load_project(pid) or proj
    planned = next((shot for shot in (proj.get('script') or {}).get('shots', [])
                    if int(shot.get('index', 0) or 0) == index), None)
    if not planned:
        return jsonify({"ok": False, "msg": f"片段{index}不存在"}), 404
    completed = next((shot for shot in proj.get('shots', [])
                     if shot.get('index') == index and shot.get('video_url')), None)
    if completed:
        return jsonify({"ok": False, "msg": f"片段{index}已经生成完成，无需重试"}), 409

    state = (((proj.get('item_states') or {}).get('shots') or {}).get(str(index), {}))
    if state.get('status') != 'failed':
        if project_job_active(pid):
            return jsonify({"ok": False, "msg": "该项目已有生成任务，请等待当前任务结束"}), 409
        return jsonify({"ok": False, "msg": f"片段{index}当前不是失败状态"}), 409
    job_id = uuid.uuid4().hex
    if not claim_project_job(pid, job_id):
        return jsonify({"ok": False, "msg": "该项目已有生成任务，请等待当前任务结束"}), 409
    # Everything checked before the lock is only an optimistic fast path. A
    # previous retry may have completed between that read and this claim.
    try:
        proj = load_project(pid)
        if not proj:
            release_project_job(pid, job_id)
            return jsonify({"ok": False, "msg": "项目不存在"}), 404
        planned = next((shot for shot in (proj.get('script') or {}).get('shots', [])
                        if int(shot.get('index', 0) or 0) == index), None)
        completed = next((shot for shot in proj.get('shots', [])
                         if shot.get('index') == index and shot.get('video_url')), None)
        state = (((proj.get('item_states') or {}).get('shots') or {}).get(str(index), {}))
    except Exception as exc:
        print(f"[片段重试] {pid}/{index} 加锁后复核失败: {exc}")
        release_project_job(pid, job_id)
        return jsonify({"ok": False, "msg": "无法复核片段状态，请重试"}), 500
    if not planned or completed or state.get('status') != 'failed':
        release_project_job(pid, job_id)
        if not planned:
            return jsonify({"ok": False, "msg": f"片段{index}不存在"}), 404
        if completed:
            return jsonify({"ok": False, "msg": f"片段{index}已经生成完成，无需重试"}), 409
        return jsonify({"ok": False, "msg": f"片段{index}当前不是失败状态"}), 409
    original_error = state.get('original_error') or state.get('error') or state.get('message') or '视频生成失败'
    try:
        try:
            retry_count = max(0, int(state.get('retry_count') or 0)) + 1
        except (TypeError, ValueError):
            retry_count = 1
        queued = set_item_state(
            proj, 'shots', index, 'retrying', 0, f"第{retry_count}次重试已加入队列",
            name=state.get('name') or segment_label(planned, index),
            retry_count=retry_count, original_error=original_error,
        )
        set_production_job_status(
            proj, 'retrying', queued['message'], run_id=job_id, job_id=job_id,
            kind='shot_retry', shot_index=index, stage='production',
            stop_requested=False, current_prompt_id=None,
            current_prompt_kind=None, cancel_result=None)
        # The project lock makes this the authoritative start of a new run.
        # Do not let an equal-timestamp terminal state from the previous run win.
        save_project(proj, preserve_runtime=False)
        threading.Thread(
            target=run_project_shot_retry, args=(pid, index, job_id), daemon=True).start()
    except Exception as exc:
        print(f"[片段重试] {pid}/{index} 启动失败: {exc}")
        release_project_job(pid, job_id)
        try:
            failed = set_item_state(
                proj, 'shots', index, 'failed', 0, '无法启动片段重试，请查看服务日志',
                name=state.get('name') or segment_label(planned, index),
                retry_count=locals().get('retry_count', 1), original_error=original_error,
            )
            set_production_job_status(proj, 'failed', failed['message'], stage='production')
            save_project(proj)
        except Exception as save_error:
            print(f"[片段重试] {pid}/{index} 失败状态保存异常: {save_error}")
        return jsonify({"ok": False, "msg": "无法启动片段重试，请查看服务日志"}), 500
    return jsonify({"ok": True, "started": True, "item_state": queued}), 202

@app.route('/api/project/<pid>/shot/<int:index>/replace', methods=['POST'])
def project_shot_replace(pid, index):
    """用单镜头生成器重制项目中的某个分镜：复制新视频覆盖该镜记录"""
    proj = load_project(pid)
    if not proj:
        return jsonify({"error": "项目不存在"}), 404
    data = request.get_json(silent=True) or {}
    src_rel = data.get('src_path', '')  # outputs/single/xxx.mp4
    src_abs = os.path.abspath(os.path.join(BASE_DIR, src_rel))
    if not src_abs.startswith(BASE_DIR) or not os.path.exists(src_abs):
        return jsonify({"error": "源视频不存在"}), 400
    shots = proj.get('shots', [])
    target = next((s for s in shots if s.get('index') == index), None)
    if not target:
        return jsonify({"error": f"片段{index}不存在"}), 404
    out_dir = os.path.join(OUTPUTS_DIR, pid)
    os.makedirs(out_dir, exist_ok=True)
    new_name = f"shot_{index:02d}.mp4"
    shutil.copyfile(src_abs, os.path.join(out_dir, new_name))
    target['video_url'] = f"/file/outputs/{pid}/{new_name}?t={int(time.time())}"
    target['path'] = os.path.join('outputs', pid, new_name)
    if data.get('prompt'):
        target['prompt'] = data['prompt']
    # 重制后正片作废（需重新合成）
    proj.pop('final', None)
    save_project(proj)
    return jsonify({"ok": True, "shot": target})

# ============================== API ==============================
@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))

@app.route('/static/<path:filename>')
def serve_static(filename):
    return send_from_directory(os.path.join(BASE_DIR, 'static'), filename)

@app.route('/file/<folder>/<path:filename>')
def serve_file(folder, filename):
    if folder == 'assets':
        return send_from_directory(ASSETS_DIR, filename)
    if folder == 'outputs':
        return send_from_directory(OUTPUTS_DIR, filename)
    return "Not Found", 404

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    global CONFIG
    if request.method == 'POST':
        data = request.get_json(force=True)
        if not isinstance(data, dict):
            return jsonify({"error": "配置必须是JSON对象"}), 400
        if any(key.startswith('reference_') for key in data):
            return jsonify({"error": "请在视频参考页保存视觉设置"}), 400
        locked = sorted(set(data) & set(BRAND_PORTAL))
        if locked:
            return jsonify({
                "ok": False,
                "error": "AIX爱客品牌与官方入口链接为只读内容，不能在网页设置中修改",
                "locked_fields": locked,
            }), 403
        if 'h3_model_profile' in data and data['h3_model_profile'] not in H3_MODEL_PROFILES:
            return jsonify({"error": "未知的H3模型档位"}), 400
        if 'image_workflow' in data and data['image_workflow'] not in image_workflow_files():
            return jsonify({"error": "未知的生图工作流"}), 400
        for key in ('h3_fl2va_model', 'h3_ref2va_model', 'image_model'):
            if key in data and (not isinstance(data[key], str) or len(data[key]) > 500):
                return jsonify({"error": f"{key} 必须是有效的模型名称"}), 400
        for key in ('comfyui_runtime_mode', 'local_llm_runtime_mode'):
            if key in data and data[key] not in ('managed', 'external'):
                return jsonify({"error": f"{key} 只支持 managed 或 external"}), 400
        for key in ('comfyui_url', 'local_llm_url', 'custom_base_url'):
            if key in data:
                parsed = urlparse(str(data[key]).strip())
                if parsed.scheme not in ('http', 'https') or not parsed.hostname:
                    return jsonify({"error": f"{key} 必须是有效的 HTTP(S) 地址"}), 400
        allowed = set(DEFAULT_CONFIG)
        unknown = sorted(set(data) - allowed)
        if unknown:
            return jsonify({"error": f"未知配置项: {', '.join(unknown)}"}), 400
        CONFIG.update({key: value for key, value in data.items() if key in allowed})
        save_config(CONFIG)
        return jsonify({"ok": True, "config": public_config()})
    return jsonify(public_config())

@app.route('/api/runtime/preflight')
def api_runtime_preflight():
    return jsonify(runtime_preflight())

@app.route('/api/h3/models')
def api_h3_models():
    available = set(comfy_unet_models())
    profiles = []
    for profile_id, profile in H3_MODEL_PROFILES.items():
        missing = [profile[key] for key in ('fl2va', 'ref2va') if profile[key] not in available]
        profiles.append({
            "id": profile_id,
            "label": profile['label'],
            "available": not missing,
            "missing": missing,
            "fl2va": profile['fl2va'],
            "ref2va": profile['ref2va'],
        })
    return jsonify({
        "selected": get_h3_model_profile(), "profiles": profiles, "models": sorted(available),
        "selected_fl2va": CONFIG.get('h3_fl2va_model', ''),
        "selected_ref2va": CONFIG.get('h3_ref2va_model', ''),
    })

@app.route('/api/models')
def api_models():
    """Return model choices discovered from the local runtime and connected ComfyUI."""
    base, _, _ = get_llm_endpoint()
    service_models = []
    try:
        response = requests.get(f"{base}/models", timeout=5)
        response.raise_for_status()
        service_models = sorted({str(item.get('id')) for item in response.json().get('data', [])
                                 if isinstance(item, dict) and item.get('id')})
    except Exception:
        pass
    video_models = comfy_unet_models()
    return jsonify({
        'llm': {
            'local_files': local_llm_models(),
            'service_models': service_models,
            'selected_path': CONFIG.get('qwen_model_path', ''),
            'selected_name': (CONFIG.get('custom_model', '') if CONFIG.get('llm_mode') == 'custom'
                              else CONFIG.get('local_llm_model', '')),
        },
        'video': {
            'models': video_models,
            # Compatibility aliases for older pages. Both workflow roles now
            # intentionally receive the same unfiltered ComfyUI candidate set.
            'fl2va': video_models,
            'ref2va': video_models,
            'selected_fl2va': CONFIG.get('h3_fl2va_model', ''),
            'selected_ref2va': CONFIG.get('h3_ref2va_model', ''),
        },
        'image': {
            'workflows': image_workflow_catalog(),
            'selected_workflow': get_image_workflow_name(),
            'selected_model': CONFIG.get('image_model', ''),
        },
    })

@app.route('/api/status')
def api_status():
    base, _, model = get_llm_endpoint()
    try:
        r = requests.get(f"{base}/models", timeout=3)
        llm_ok = r.status_code == 200
    except Exception:
        llm_ok = False
    return jsonify({
        "version": app_version(),
        "llm_online": llm_ok, "llm_endpoint": base, "llm_model": model,
        "comfyui_online": comfy_check(), "comfyui_url": comfy_url(),
        "ffmpeg": bool(find_ffmpeg()), "acceleration": acceleration_status(),
        "runtime": {
            "comfyui_mode": comfy_runtime_mode(),
            "llm_mode": local_llm_runtime_mode() if CONFIG.get('llm_mode') == 'local' else 'external',
        }
    })

@app.route('/api/comfy/queue')
def api_comfy_queue():
    """Proxy queue state so browsers never need direct ComfyUI access."""
    try:
        response = requests.get(f"{comfy_url()}/queue", timeout=5)
        response.raise_for_status()
        data = response.json()
        return jsonify({
            "ok": True,
            "queue_running": data.get("queue_running") or [],
            "queue_pending": data.get("queue_pending") or [],
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "queue_running": [],
            "queue_pending": [],
            "error": str(exc),
        }), 503

@app.route('/api/pipeline/run')
def api_pipeline_run():
    idea = request.args.get('idea', '').strip()
    pid = request.args.get('pid') or uuid.uuid4().hex[:12]
    if not re.fullmatch(r'[0-9a-zA-Z_-]{1,64}', pid):
        return jsonify({"ok": False, "msg": "非法项目ID"}), 400
    input_mode = 'h3_prompt' if request.args.get('input_mode') == 'h3_prompt' else 'story'
    scene_mode_arg = request.args.get('scene_reference_mode', '').lower()
    if scene_mode_arg in ('auto', 'photo', 'concept'):
        CONFIG['scene_reference_mode'] = scene_mode_arg
    # 请求级参数（画风/每片时长/片段数/画幅）直接更新配置（参数名兼容旧版）
    if request.args.get('style'):
        CONFIG['style'] = request.args['style']
    if request.args.get('aspect_ratio') in ASPECT_IMG_SIZE:
        CONFIG['aspect_ratio'] = request.args['aspect_ratio']
    mp_arg = request.args.get('megapixels')
    if mp_arg:
        try:
            CONFIG['megapixels'] = max(0.1, min(float(mp_arg), 4.0))
        except ValueError:
            pass
    steps_arg = request.args.get('h3_steps')
    if steps_arg:
        try:
            CONFIG['h3_steps'] = max(4, min(int(steps_arg), 25))
        except ValueError:
            pass
    profile_arg = request.args.get('h3_model_profile', '').lower()
    if profile_arg in H3_MODEL_PROFILES:
        CONFIG['h3_model_profile'] = profile_arg
    dur_arg = request.args.get('shot_duration')
    if dur_arg and input_mode != 'h3_prompt':
        if dur_arg.lower() == 'auto':
            CONFIG['shot_duration'] = 'auto'
        else:
            try:
                CONFIG['shot_duration'] = max(3, min(int(dur_arg), 15))
            except ValueError:
                pass
    cnt_arg = request.args.get('shot_count')
    if cnt_arg and input_mode != 'h3_prompt':
        if cnt_arg.lower() == 'auto':
            CONFIG['shot_count'] = 'auto'
        else:
            try:
                CONFIG['shot_count'] = max(1, min(int(cnt_arg), 20))
            except ValueError:
                pass
    save_config(CONFIG)
    custom_assets = (request.args.get('custom_assets') == '1')
    if not idea and not load_project(pid):
        return jsonify({"ok": False, "msg": "请输入创作内容"}), 400
    def stream():
        queue = []
        def push(event, data):
            queue.append(sse(event, data))
        # 在线程中跑管线，主线程吐队列
        def worker():
            try:
                run_pipeline(pid, idea, push, custom_assets=custom_assets, input_mode=input_mode)
            except ProjectAlreadyRunning as exc:
                push('error', {"msg": str(exc)})
            except Exception as e:
                import traceback
                traceback.print_exc()
                push('error', {"msg": f"管线异常: {e}"})
            finally:
                push('end', {})
        t = threading.Thread(target=worker, daemon=True)
        try:
            t.start()
        except Exception:
            yield sse('error', {"msg": "无法启动生成任务，请查看服务日志"})
            yield sse('end', {})
            return
        yield sse('start', {"pid": pid})
        yield from stream_sse_queue(t, queue)
    return Response(stream(), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})

@app.route('/api/project/<pid>/stop', methods=['POST'])
def api_stop_project(pid):
    """Request a cooperative stop and cancel only the prompt owned by this project."""
    requested_run_id = str((request.get_json(silent=True) or {}).get('run_id') or '').strip()
    outcome, job, prompt_id = request_project_stop_state(pid, requested_run_id)
    if outcome == 'missing':
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    if outcome == 'stale':
        return jsonify({'ok': False, 'status': job.get('status') or 'idle',
                        'msg': '任务已经更新，请刷新页面后再操作'}), 409
    status = str(job.get('status') or '')
    if outcome == 'stopping':
        return jsonify({'ok': True, 'changed': False, 'status': 'stopping',
                        'msg': job.get('message') or '项目正在停止', 'production_job': job})
    if outcome == 'terminal' and status == 'stopped':
        return jsonify({'ok': True, 'changed': False, 'status': 'stopped', 'msg': job.get('message') or '项目已经停止'})
    if outcome == 'terminal' and status in ('done', 'failed'):
        return jsonify({'ok': True, 'changed': False, 'status': status or 'done', 'msg': '当前任务已经结束，无需停止'})
    if outcome == 'terminal':
        return jsonify({'ok': False, 'status': status or 'idle', 'msg': '当前项目没有正在生成的任务'}), 409

    run_id = job.get('run_id')
    message = job.get('message') or '停止请求已保存，将保留已经完成的结果'
    action = 'cooperative'
    if prompt_id:
        action, message = comfy_cancel_prompt_safely(prompt_id)
        if action == 'deferred':
            message = f'{message}；已完成结果会保留'
        else:
            message = f'{message}；项目正在收尾'
        job = update_stopping_cancel_result(pid, run_id, action, message) or job
    else:
        job = (load_project(pid) or {}).get('production_job') or job
    response_status = str(job.get('status') or 'stopping')
    response_message = job.get('message') or message
    return jsonify({
        'ok': True, 'status': response_status, 'action': action,
        'changed': True, 'msg': response_message, 'production_job': job,
    }), (202 if response_status == 'stopping' else 200)

# ============================== V2 分阶段筹备 ==============================
PREP_OUTLINE_PROMPT = """你是短剧策划。请根据用户提供的故事创意，写一份供编剧确认的剧情大纲。
要求：保留用户核心设定；明确开端、触发事件、升级、转折、高潮和结局；人物动机与因果完整；适合拆成3到10个短视频镜头；不要写H3提示词，不要输出JSON。中文输出，控制在500字内。"""

def prep_state(proj):
    return proj.setdefault('preproduction', {
        'version': 2, 'step': 'outline', 'story_confirmed': True,
        'outline': '', 'outline_confirmed': False,
        'script_confirmed': False, 'prompts_confirmed': False,
        'asset_plan': [], 'assets_confirmed': False,
        'asset_batch': {'status': 'waiting', 'total': 0, 'completed': 0, 'failed': 0},
        'updated': time.time()
    })

def prep_touch(proj, step=None):
    prep = prep_state(proj)
    if step:
        prep['step'] = step
    prep['updated'] = time.time()
    save_project(proj)
    return prep

def prep_asset_url(path):
    if not path:
        return None
    return f"/file/assets/{os.path.basename(path)}"

def build_preproduction_asset_plan(proj):
    """把已确认剧本整理为可独立上传、生成、改提示词和审核的图片清单。"""
    script = proj.get('script') or {}
    style = proj.get('style') or CONFIG.get('style', '电影写实')
    scene_mode = proj.get('scene_reference_mode') or CONFIG.get('scene_reference_mode', 'auto')
    existing = {item.get('key'): item for item in prep_state(proj).get('asset_plan', [])}
    plan = []

    def add(key, kind, name, prompt, index=None):
        old = existing.get(key, {})
        saved = proj.get('assets', {}).get(key, {})
        path = old.get('path') or saved.get('path')
        ready = bool(path and os.path.exists(path))
        plan.append({
            'key': key, 'kind': kind, 'name': name, 'index': index,
            'prompt': old.get('prompt') or prompt, 'path': path,
            'source': old.get('source') or saved.get('source') or ('upload' if saved.get('uploaded') else None),
            'confirmed': bool(old.get('confirmed')) and ready,
            'status': 'confirmed' if bool(old.get('confirmed')) and ready else ('ready' if ready else old.get('status', 'waiting')),
            'progress': 100 if ready else int(old.get('progress') or 0),
            'message': old.get('message') or ('已准备，等待审核' if ready else '等待批量生成'),
            'error': old.get('error'), 'updated': old.get('updated')
        })

    for char in script.get('characters', []):
        name = char.get('name', '未命名角色')
        appearance = ensure_race_desc(char.get('appearance', ''))
        resolved_gender = resolve_character_gender(char)
        gender_lock = {'female': '画面性别固定为女性。', 'male': '画面性别固定为男性。'}.get(resolved_gender, '')
        prompt = (f"{style}风格，真人角色资产设定图。角色：{name}。{gender_lock}{appearance}。"
                  "自然站姿，正面半身与全身信息清晰，真实皮肤和衣料材质，中性干净背景，严禁文字、标志和拼贴边框。")
        add(f"char_{name}", 'character', name, prompt)
    char_names = [c.get('name', '') for c in script.get('characters', [])]
    for scene in script.get('scenes', []):
        name = scene.get('name', '未命名场景')
        desc = sanitize_scene_text(scene.get('description', ''), char_names,
                                   keep_background_life=resolve_scene_reference_mode(style, scene_mode) == 'photo')
        prompt, _ = build_scene_image_prompt(style, desc, scene_mode)
        add(f"scene_{name}", 'scene', name, prompt)
    for prop in script.get('props', []):
        name = prop.get('name', '未命名道具')
        prompt = (f"{style}风格，关键道具资产图（无人物）。{prop.get('description', '')}。"
                  "素净背景，物体居中完整展示，真实材质纹理和使用痕迹，严禁文字和标志。")
        add(f"prop_{name}", 'prop', name, prompt)
    chars_by_name = {c.get('name'): c for c in script.get('characters', [])}
    scenes_by_name = {s.get('name'): s for s in script.get('scenes', [])}
    if proj.get('generate_storyboards', True):
        for pos, shot in enumerate(script.get('shots', []), 1):
            idx = int(shot.get('index') or pos)
            cast = '；'.join(f"{n}：{chars_by_name.get(n, {}).get('appearance', '')}" for n in shot.get('characters', []))
            scene = scenes_by_name.get(shot.get('scene'), {})
            prompt = (f"{style}风格，真人电影片段构图参考图，{segment_label(shot, idx)}。场景：{shot.get('scene', '')}，"
                      f"{scene.get('description', '')}。出场人物：{cast or '无明确人物'}。"
                      f"画面动作：{shot.get('action', '')}。摄影机：{shot.get('camera', '自然纪实机位')}。"
                      "严格表现该片段开场的瞬间、景别、人物站位和视线关系，真实摄影质感，不做45度俯拍概念图，不做多格拼贴，严禁文字和标志。")
            add(f"board_{idx}", 'storyboard', f"{segment_label(shot, idx)} 构图参考", prompt, idx)
    prep_state(proj)['asset_plan'] = plan
    prep = prep_state(proj)
    if not prep.get('asset_batch') or prep.get('asset_batch', {}).get('status') not in ('running', 'review'):
        prep['asset_batch'] = {
            'status': 'waiting', 'total': len(plan), 'completed': sum(bool(x.get('path')) for x in plan),
            'failed': 0, 'message': '等待批量生成', 'updated': time.time()
        }
    return plan

def preproduction_view(proj):
    # A Flask restart terminates the worker thread, while ComfyUI may already
    # have finished its last image.  Do not leave the recovered page polling a
    # nonexistent batch forever; the user can retry only the missing cards.
    state = prep_state(proj)
    if state.get('step') == 'character_review':
        state.update({'step': 'prompts', 'script_confirmed': bool(proj.get('script')),
                      'character_confirmed': True, 'updated': time.time()})
        save_project(proj)
    batch = state.get('asset_batch') or {}
    if batch.get('status') in ('starting', 'running') and not prep_asset_batch_active(proj.get('id')):
        batch.update({
            'status': 'failed', 'current': None,
            'message': '服务重启导致批量任务中断，可重试缺失图片',
            'updated': time.time()
        })
        prep_state(proj)['asset_batch'] = batch
        save_project(proj)
    prep = copy.deepcopy(prep_state(proj))
    for item in prep.get('asset_plan', []):
        item['url'] = prep_asset_url(item.get('path'))
        item['progress'] = max(0, min(100, int(item.get('progress') or (100 if item.get('url') else 0))))
    return prep

def finish_preproduction_llm_response(pid, run_id, payload, http_status, status, message):
    job = finish_preproduction_llm_job(pid, run_id, status, message) or {}
    if job.get('status') == 'stopped':
        current = load_project(pid) or {'id': pid}
        return jsonify({
            'ok': True, 'stopped': True, 'status': 'stopped',
            'msg': job.get('message') or message,
            'production_job': job,
            'preproduction': preproduction_view(current),
        }), 200
    response_payload = dict(payload)
    response_payload['production_job'] = job
    return jsonify(response_payload), http_status

def prep_asset_batch_active(pid):
    with _PREP_ASSET_BATCH_LOCK:
        thread = _PREP_ASSET_BATCH_THREADS.get(pid)
        return bool(thread and thread.is_alive())

def prep_asset_generation_active(pid):
    """Only one Qwen image task per V2 project, including a manual remake."""
    with _PREP_ASSET_BATCH_LOCK:
        batch = _PREP_ASSET_BATCH_THREADS.get(pid)
        manual = _PREP_ASSET_MANUAL_THREADS.get(pid)
        return bool((batch and batch.is_alive()) or (manual and manual.is_alive()))

def prep_update_asset_state(pid, key, status=None, progress=None, message=None, error=None, batch_update=None):
    """Persist one preparation card without overwriting WebSocket sampling updates."""
    with _PREP_ASSET_STATE_LOCK:
        proj = load_project(pid)
        if not proj:
            return None
        prep = prep_state(proj)
        item = next((x for x in prep.get('asset_plan', []) if x.get('key') == key), None)
        if not item:
            return None
        now = time.time()
        if status is not None:
            item['status'] = status
        if progress is not None:
            item['progress'] = max(0, min(100, int(progress)))
        if message is not None:
            item['message'] = message
        if error is not None:
            item['error'] = error
        item['updated'] = now
        if isinstance(batch_update, dict):
            prep.setdefault('asset_batch', {}).update(batch_update)
            prep['asset_batch']['updated'] = now
        save_project(proj)
        return item

def make_prep_asset_progress_publisher(pid, key, name, position=None, total=None):
    """Map ComfyUI sampling to persisted fifth-step card and batch percentages."""
    seen = {'progress': 4}
    def callback(value, maximum, node=None):
        if not maximum:
            return
        pct = max(5, min(95, 5 + round(90 * float(value) / float(maximum))))
        if pct <= seen['progress']:
            return
        seen['progress'] = pct
        batch = None
        if position is not None and total:
            overall = round(100 * ((position - 1) + pct / 100.0) / total)
            batch = {
                'current_key': key, 'current_name': name, 'current_progress': pct,
                'overall_progress': overall,
                'message': f'正在生成 {position}/{total}：{name} · {int(value)}/{int(maximum)}步'
            }
        prep_update_asset_state(pid, key, 'generating', pct,
                                f'ComfyUI 正在采样 · {int(value)}/{int(maximum)}步', batch_update=batch)
    return callback

@app.route('/api/preproduction/start', methods=['POST'])
def api_preproduction_start():
    d = request.get_json(force=True, silent=True) or {}
    content = str(d.get('content') or '').strip()
    mode = 'h3_prompt' if d.get('input_mode') == 'h3_prompt' else 'story'
    if not content:
        return jsonify({'ok': False, 'msg': '请输入故事或完整 H3 提示词'}), 400
    if mode == 'h3_prompt':
        _, err = parse_h3_timeline(content)
        if err:
            return jsonify({'ok': False, 'msg': err}), 400
    pid = uuid.uuid4().hex[:12]
    style = str(d.get('style') or CONFIG.get('style', '电影写实')).strip() or '电影写实'
    scene_mode = str(d.get('scene_reference_mode') or 'auto').lower()
    if scene_mode not in ('auto', 'photo', 'concept'):
        scene_mode = 'auto'
    try:
        shot_duration = 'auto' if str(d.get('shot_duration', 'auto')).lower() == 'auto' else max(3, min(int(d.get('shot_duration')), 15))
        shot_count = 'auto' if str(d.get('shot_count', 'auto')).lower() == 'auto' else max(1, min(int(d.get('shot_count')), 20))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'msg': '片段时长或片段数参数无效'}), 400
    generate_storyboards = bool(d.get('generate_storyboards', CONFIG.get('generate_storyboards', False)))
    CONFIG['generate_storyboards'] = generate_storyboards
    save_config(CONFIG)
    proj = {
        'id': pid, 'idea': content, 'input_mode': mode, 'title': '待确认项目',
        'style': style, 'scene_reference_mode': scene_mode,
        'shot_duration': shot_duration, 'shot_count': shot_count, 'script': None,
        'prompts': {}, 'assets': {}, 'shots': [], 'final': None, 'created': time.time(),
        'custom_assets': bool(d.get('custom_assets')),
        'generate_storyboards': generate_storyboards,
        'preproduction': {
            'version': 2, 'step': 'prompt_review' if mode == 'h3_prompt' else 'outline',
            'story_confirmed': mode == 'story', 'outline': '', 'outline_confirmed': False,
            'script_confirmed': False, 'prompts_confirmed': False,
            'asset_plan': [], 'assets_confirmed': False,
            'asset_batch': {'status': 'waiting', 'total': 0, 'completed': 0, 'failed': 0},
            'updated': time.time()
        }
    }
    if mode == 'h3_prompt':
        proj['manual_h3_prompt'] = content
    save_project(proj)
    return jsonify({'ok': True, 'pid': pid, 'project': {'id': pid, 'input_mode': mode, 'preproduction': preproduction_view(proj)}})

@app.route('/api/preproduction/<pid>/outline/generate', methods=['POST'])
def api_preproduction_outline(pid):
    proj = load_project(pid)
    if not proj or proj.get('input_mode') != 'story':
        return jsonify({'ok': False, 'msg': '项目不存在或入口模式不匹配'}), 404
    try:
        run_id = begin_production_job(pid, 'preproduction_outline')
    except ProjectAlreadyRunning as exc:
        return jsonify({'ok': False, 'msg': str(exc)}), 409
    try:
        update_production_job(
            pid, run_id, stage='preproduction', message='Qwen 正在生成剧情大纲')
        raise_if_project_stopped(pid, run_id)
        ready, ready_err = prepare_llm_stage()
        if not ready:
            return finish_preproduction_llm_response(
                pid, run_id, {'ok': False, 'msg': f'Qwen服务准备失败：{ready_err}'},
                503, 'failed', f'Qwen服务准备失败：{ready_err}')
        raise_if_project_stopped(pid, run_id)
        text, err = llm_chat([
            {'role': 'system', 'content': PREP_OUTLINE_PROMPT},
            {'role': 'user', 'content': proj.get('idea', '')}
        ], max_tokens=1000, temperature=0.6,
            stop_check=lambda: project_stop_requested(pid, run_id))
        raise_if_project_stopped(pid, run_id)
        if not text:
            message = f'大纲生成失败：{err}'
            return finish_preproduction_llm_response(
                pid, run_id, {'ok': False, 'msg': message}, 502, 'failed', message)
        with _PROJECT_IO_LOCK:
            raise_if_project_stopped(pid, run_id)
            proj = load_project(pid)
            prep = prep_state(proj)
            prep.update({'outline': text.strip(), 'outline_confirmed': False, 'step': 'outline_review'})
            prep['updated'] = time.time()
            job = set_production_job_status(
                proj, 'done', '剧情大纲生成完成', finished=time.time(),
                current_prompt_id=None, current_prompt_kind=None)
            save_project(proj, preserve_runtime=False)
        return jsonify({
            'ok': True, 'outline': text.strip(),
            'preproduction': preproduction_view(proj),
            'production_job': job,
        })
    except ProjectStopRequested as exc:
        message = str(exc) or '大纲生成已停止，原有内容已保留'
        return finish_preproduction_llm_response(
            pid, run_id, {'ok': True}, 200, 'stopped', message)
    except Exception:
        finish_preproduction_llm_job(pid, run_id, 'failed', '大纲生成异常结束，可重新生成')
        raise
    finally:
        end_production_job(pid, run_id)

@app.route('/api/preproduction/<pid>/outline/confirm', methods=['POST'])
def api_preproduction_confirm_outline(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    d = request.get_json(force=True, silent=True) or {}
    text = str(d.get('outline') or prep_state(proj).get('outline') or '').strip()
    if not text:
        return jsonify({'ok': False, 'msg': '剧情大纲不能为空'}), 400
    prep = prep_state(proj)
    prep.update({'outline': text, 'outline_confirmed': True, 'step': 'script'})
    prep_touch(proj)
    return jsonify({'ok': True, 'preproduction': preproduction_view(proj)})

@app.route('/api/preproduction/<pid>/script/generate', methods=['POST'])
def api_preproduction_script(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    prep = prep_state(proj)
    if not prep.get('outline_confirmed'):
        return jsonify({'ok': False, 'msg': '请先确认剧情大纲'}), 409
    try:
        run_id = begin_production_job(pid, 'preproduction_script')
    except ProjectAlreadyRunning as exc:
        return jsonify({'ok': False, 'msg': str(exc)}), 409
    try:
        update_production_job(
            pid, run_id, stage='preproduction', message='Qwen 正在生成结构化剧本')
        raise_if_project_stopped(pid, run_id)
        ready, ready_err = prepare_llm_stage()
        if not ready:
            return finish_preproduction_llm_response(
                pid, run_id, {'ok': False, 'msg': f'Qwen服务准备失败：{ready_err}'},
                503, 'failed', f'Qwen服务准备失败：{ready_err}')
        raise_if_project_stopped(pid, run_id)
        user_text = f"用户原始故事：\n{proj.get('idea', '')}\n\n已确认剧情大纲：\n{prep.get('outline', '')}"
        content, err = llm_chat([
            {'role': 'system', 'content': build_script_prompt(proj)},
            {'role': 'user', 'content': user_text}
        ], max_tokens=5000, temperature=0.65,
            stop_check=lambda: project_stop_requested(pid, run_id))
        raise_if_project_stopped(pid, run_id)
        script = parse_json_from_text(content or '')
        if not script or not script.get('shots'):
            message = f'剧本生成或解析失败：{err or "模型未返回有效 JSON"}'
            return finish_preproduction_llm_response(
                pid, run_id, {'ok': False, 'msg': message}, 502, 'failed', message)
        for pos, segment in enumerate(script.get('shots', []), 1):
            segment['index'] = pos
            segment['segment_id'] = str(segment.get('segment_id') or f"EP01-{pos:02d}")
            continuity_value = segment.get('continue_from_previous', False)
            segment['continue_from_previous'] = pos > 1 and (
                continuity_value is True or str(continuity_value).strip().lower() in ('1', 'true', 'yes', '是'))
            try:
                duration = int(segment.get('duration', 10))
            except (TypeError, ValueError):
                duration = 10
            segment['duration'] = max(3, min(duration, 15))
            segment['action'] = filter_slow_motion(segment.get('action', ''))
            segment['camera'] = filter_slow_motion(segment.get('camera', ''))
        with _PROJECT_IO_LOCK:
            raise_if_project_stopped(pid, run_id)
            proj = load_project(pid)
            proj['script'] = script
            proj['prompts'] = {}
            proj['title'] = script.get('title') or '未命名短剧'
            prep = prep_state(proj)
            prep.update({'script_confirmed': False, 'prompts_confirmed': False, 'step': 'script_review'})
            prep['updated'] = time.time()
            job = set_production_job_status(
                proj, 'done', '结构化剧本生成完成', finished=time.time(),
                current_prompt_id=None, current_prompt_kind=None)
            save_project(proj, preserve_runtime=False)
        return jsonify({
            'ok': True, 'script': script,
            'preproduction': preproduction_view(proj),
            'production_job': job,
        })
    except ProjectStopRequested as exc:
        message = str(exc) or '剧本生成已停止，原有内容已保留'
        return finish_preproduction_llm_response(
            pid, run_id, {'ok': True}, 200, 'stopped', message)
    except Exception:
        finish_preproduction_llm_job(pid, run_id, 'failed', '剧本生成异常结束，可重新生成')
        raise
    finally:
        end_production_job(pid, run_id)

@app.route('/api/preproduction/<pid>/script/confirm', methods=['POST'])
def api_preproduction_confirm_script(pid):
    proj = load_project(pid)
    if not proj or not proj.get('script'):
        return jsonify({'ok': False, 'msg': '还没有可确认的剧本'}), 404
    d = request.get_json(force=True, silent=True) or {}
    edited = d.get('script')
    if edited is not None:
        if not isinstance(edited, dict) or not edited.get('shots'):
            return jsonify({'ok': False, 'msg': '编辑后的剧本必须是含 shots 的 JSON 对象'}), 400
        for pos, segment in enumerate(edited.get('shots', []), 1):
            segment['index'] = pos
            segment['segment_id'] = str(segment.get('segment_id') or f"EP01-{pos:02d}")
            value = segment.get('continue_from_previous', False)
            segment['continue_from_previous'] = pos > 1 and (
                value is True or str(value).strip().lower() in ('1', 'true', 'yes', '是'))
            try:
                segment['duration'] = max(3, min(int(segment.get('duration', 10)), 15))
            except (TypeError, ValueError):
                segment['duration'] = 10
        proj['script'] = edited
        proj['title'] = edited.get('title') or proj.get('title')
    prep = prep_state(proj)
    characters = (proj.get('script') or {}).get('characters', [])
    for character in characters:
        character.setdefault('visual_gender', infer_character_gender(character.get('appearance')))
        character.setdefault('voice_gender', character.get('visual_gender') or 'auto')
        character.setdefault('visual_locked', True)
    prep.update({
        'script_confirmed': True,
        'character_confirmed': True,
        'step': 'prompts',
    })
    prep_touch(proj)
    return jsonify({'ok': True, 'preproduction': preproduction_view(proj)})

@app.route('/api/preproduction/<pid>/prompts/generate', methods=['POST'])
def api_preproduction_prompts(pid):
    proj = load_project(pid)
    if not proj or not proj.get('script'):
        return jsonify({'ok': False, 'msg': '请先生成并确认剧本'}), 404
    if not prep_state(proj).get('script_confirmed'):
        return jsonify({'ok': False, 'msg': '请先确认剧本'}), 409
    if normalize_script_dialogue(proj.get('script')):
        save_project(proj)
    try:
        run_id = begin_production_job(pid, 'preproduction_prompts')
    except ProjectAlreadyRunning as exc:
        return jsonify({'ok': False, 'msg': str(exc)}), 409
    llm_prepared = False
    try:
        update_production_job(
            pid, run_id, stage='preproduction', message='Qwen 正在逐片生成 H3 提示词')
        raise_if_project_stopped(pid, run_id)
        ready, ready_err = prepare_llm_stage()
        if not ready:
            return finish_preproduction_llm_response(
                pid, run_id, {'ok': False, 'msg': f'Qwen服务准备失败：{ready_err}'},
                503, 'failed', f'Qwen服务准备失败：{ready_err}')
        llm_prepared = True
        raise_if_project_stopped(pid, run_id)
        proj = load_project(pid)
        prompts = dict(proj.get('prompts') or {})
        prompt_errors = []
        profile_map = character_profiles_by_name(proj.get('script'))
        for pos, shot in enumerate(proj['script'].get('shots', []), 1):
            raise_if_project_stopped(pid, run_id)
            idx = int(shot.get('index') or pos)
            cached = prompts.get(str(idx))
            if cached and not validate_h3_production_prompt(cached, shot_dialogue_lines(shot), 9):
                continue
            update_production_job(
                pid, run_id, message=f'Qwen 正在生成 {segment_label(shot, idx)} 提示词')
            # Assets are intentionally generated after prompt review. Compile against the confirmed
            # text contract now; the complete ordered image set is attached only at H3 render time.
            text, err = gen_shot_h3_prompt(
                shot, [], None, profile_map,
                stop_check=lambda: project_stop_requested(pid, run_id))
            raise_if_project_stopped(pid, run_id)
            if not text:
                prompt_errors.append(f'{segment_label(shot, idx)}：{err or "提示词生成失败"}')
                continue
            generated_prompt = apply_character_visual_locks(
                filter_slow_motion(text), shot, profile_map)
            with _PROJECT_IO_LOCK:
                raise_if_project_stopped(pid, run_id)
                proj = load_project(pid)
                prompts = dict(proj.get('prompts') or {})
                prompts[str(idx)] = generated_prompt
                proj['prompts'] = prompts
                proj['prompt_format_version'] = H3_PROMPT_FORMAT_VERSION
                save_project(proj)
        with _PROJECT_IO_LOCK:
            raise_if_project_stopped(pid, run_id)
            proj = load_project(pid)
            prompts = dict(proj.get('prompts') or {})
            prep = prep_state(proj)
            if prompt_errors:
                prep.update({'prompts_confirmed': False, 'step': 'prompts'})
            else:
                proj['prompt_format_version'] = H3_PROMPT_FORMAT_VERSION
                prep.update({'prompts_confirmed': False, 'step': 'prompts_review'})
            prep['updated'] = time.time()
            message = ((f'{len(prompt_errors)}个片段提示词未通过，其余已保存，可再次点击只补失败项：'
                        + '；'.join(prompt_errors[:5])) if prompt_errors else 'H3 提示词生成完成')
            job = set_production_job_status(
                proj, 'failed' if prompt_errors else 'done', message,
                finished=time.time(), current_prompt_id=None, current_prompt_kind=None)
            save_project(proj, preserve_runtime=False)
        if prompt_errors:
            return jsonify({
                'ok': False, 'msg': message, 'prompts': prompts,
                'preproduction': preproduction_view(proj),
                'production_job': job,
            }), 502
        return jsonify({
            'ok': True, 'prompts': prompts,
            'preproduction': preproduction_view(proj),
            'production_job': job,
        })
    except ProjectStopRequested as exc:
        message = str(exc) or '提示词生成已停止，已完成的提示词已保留'
        return finish_preproduction_llm_response(
            pid, run_id, {'ok': True}, 200, 'stopped', message)
    except Exception:
        finish_preproduction_llm_job(pid, run_id, 'failed', '提示词生成异常结束，可重新生成')
        raise
    finally:
        try:
            if llm_prepared and exclusive_on():
                stop_local_llm()
        finally:
            end_production_job(pid, run_id)

@app.route('/api/preproduction/<pid>/prompts/confirm', methods=['POST'])
def api_preproduction_confirm_prompts(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    d = request.get_json(force=True, silent=True) or {}
    prep = prep_state(proj)
    if proj.get('input_mode') == 'h3_prompt' and not proj.get('script'):
        ready, ready_err = prepare_llm_stage()
        if not ready:
            return jsonify({'ok': False, 'msg': f'Qwen服务准备失败：{ready_err}'}), 503
        source = str(d.get('source_prompt') or proj.get('manual_h3_prompt') or proj.get('idea') or '').strip()
        timeline, terr = parse_h3_timeline(source)
        if terr:
            return jsonify({'ok': False, 'msg': terr}), 400
        content, err = llm_chat([
            {'role': 'system', 'content': MANUAL_H3_ASSET_PROMPT},
            {'role': 'user', 'content': source}
        ], max_tokens=2500, temperature=0.2)
        extracted = parse_json_from_text(content or '')
        if not extracted:
            return jsonify({'ok': False, 'msg': f'提示词资产解析失败：{err or "模型未返回有效 JSON"}'}), 502
        script, prompts = build_manual_h3_script(source, extracted, timeline)
        proj['manual_h3_prompt'] = source
        proj['idea'] = source
        proj['script'] = script
        proj['prompts'] = prompts
        proj['timeline_segments'] = timeline.get('segments', [])
        proj['title'] = script.get('title') or 'H3 时间轴项目'
        prep['script_confirmed'] = True
    else:
        incoming = d.get('prompts')
        if isinstance(incoming, dict):
            proj['prompts'] = {str(k): str(v).strip() for k, v in incoming.items() if str(v).strip()}
        expected = [str(s.get('index', i + 1)) for i, s in enumerate((proj.get('script') or {}).get('shots', []))]
        missing = [i for i in expected if not proj.get('prompts', {}).get(i)]
        if missing:
            return jsonify({'ok': False, 'msg': f'以下镜头缺少提示词：{", ".join(missing)}'}), 400
    prep.update({'prompts_confirmed': True, 'step': 'assets'})
    build_preproduction_asset_plan(proj)
    prep_touch(proj)
    return jsonify({'ok': True, 'script': proj.get('script'), 'preproduction': preproduction_view(proj)})

@app.route('/api/preproduction/<pid>/asset/generate', methods=['POST'])
def api_preproduction_generate_asset(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    d = request.get_json(force=True, silent=True) or {}
    key = str(d.get('key') or '')
    plan = prep_state(proj).get('asset_plan', [])
    item = next((x for x in plan if x.get('key') == key), None)
    if not item:
        return jsonify({'ok': False, 'msg': '资产项不存在'}), 404
    if prep_asset_generation_active(pid):
        return jsonify({'ok': False, 'msg': '已有图片生成任务进行中，请等待当前任务完成'}), 409
    prompt = str(d.get('prompt') or item.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'ok': False, 'msg': '生成提示词不能为空'}), 400
    try:
        run_id = begin_production_job(pid, 'asset')
    except ProjectAlreadyRunning as exc:
        return jsonify({'ok': False, 'msg': str(exc)}), 409
    item.update({'prompt': prompt, 'status': 'generating', 'progress': 3,
                 'message': '正在准备 ComfyUI', 'error': None, 'confirmed': False, 'updated': time.time()})
    prep_touch(proj)
    with _PREP_ASSET_BATCH_LOCK:
        worker = threading.Thread(target=run_preproduction_single_asset, args=(pid, key, prompt, run_id), daemon=True)
        _PREP_ASSET_MANUAL_THREADS[pid] = worker
        try:
            worker.start()
        except Exception as exc:
            _PREP_ASSET_MANUAL_THREADS.pop(pid, None)
            update_production_job(pid, run_id, status='failed', message=f'任务启动失败：{exc}')
            end_production_job(pid, run_id)
            return jsonify({'ok': False, 'msg': f'任务启动失败：{exc}'}), 500
    return jsonify({'ok': True, 'started': True, 'preproduction': preproduction_view(proj)}), 202

def run_preproduction_single_asset(pid, key, prompt, run_id):
    """Run one remake asynchronously so the card can render live sampling progress."""
    final_status = 'failed'
    final_message = '图片生成任务已结束，可重试'
    try:
        raise_if_project_stopped(pid, run_id)
        proj = load_project(pid)
        item = next((x for x in prep_state(proj).get('asset_plan', []) if x.get('key') == key), None) if proj else None
        if not item:
            return
        if not prepare_comfy_stage():
            prep_update_asset_state(pid, key, status='failed', progress=0, message='ComfyUI 启动失败', error='ComfyUI 启动失败')
            return
        ready, preflight_error = image_workflow_preflight()
        if not ready:
            prep_update_asset_state(pid, key, status='failed', progress=0,
                                    message=preflight_error, error=preflight_error)
            return
        safe = re.sub(r'[^\w一-鿿-]', '_', key)
        progress_cb = make_prep_asset_progress_publisher(pid, key, item.get('name', key))
        path, err = gen_image(
            prompt, save_name=f"prep_{pid}_{safe}_{int(time.time())}.png",
            progress_cb=progress_cb, project_pid=pid, run_id=run_id)
        if not path:
            prep_update_asset_state(pid, key, status='failed', progress=0, message=err or '生成失败', error=err or '生成失败')
            return
        proj = load_project(pid)
        item = next((x for x in prep_state(proj).get('asset_plan', []) if x.get('key') == key), None) if proj else None
        if item:
            item.update({'path': path, 'source': 'ai', 'status': 'ready', 'progress': 100,
                         'message': '生成完成，等待审核', 'confirmed': False, 'error': None, 'updated': time.time()})
            proj.setdefault('assets', {})[key] = {'path': path, 'kind': item.get('kind'), 'source': 'ai'}
            prep_touch(proj)
            final_status = 'done'
            final_message = '图片生成完成'
    except ProjectStopRequested as exc:
        final_status = 'stopped'
        final_message = str(exc) or '已停止，完成的结果已保留'
        prep_update_asset_state(pid, key, status='waiting', message=final_message, error=None)
    except Exception as exc:
        prep_update_asset_state(pid, key, status='failed', progress=0, message=str(exc), error=str(exc))
    finally:
        try:
            if final_status == 'stopped':
                finalize_project_stopped(pid, run_id, final_message)
            else:
                update_production_job(
                    pid, run_id, status=final_status, stop_requested=False,
                    current_prompt_id=None, current_prompt_kind=None,
                    message=final_message, finished=time.time())
        finally:
            end_production_job(pid, run_id)
            with _PREP_ASSET_BATCH_LOCK:
                _PREP_ASSET_MANUAL_THREADS.pop(pid, None)

def run_preproduction_asset_batch(pid, run_id):
    """Generate only missing preparation images, saving progress after every item."""
    errors = []
    final_status = 'failed'
    final_message = '批量图片任务已结束，可重试缺失图片'
    try:
        raise_if_project_stopped(pid, run_id)
        proj = load_project(pid)
        if not proj:
            return
        prep = prep_state(proj)
        plan = prep.get('asset_plan', [])
        missing = [x.get('key') for x in plan
                   if not x.get('path') or not os.path.exists(x.get('path', ''))]
        prep['asset_batch'] = {
            'status': 'running', 'total': len(missing), 'completed': 0, 'failed': 0,
            'succeeded': 0, 'current': None, 'current_key': None, 'current_progress': 0,
            'overall_progress': 0, 'message': '正在准备 ComfyUI', 'updated': time.time()
        }
        prep_touch(proj)
        if missing and not prepare_comfy_stage():
            raise RuntimeError('ComfyUI 未运行且自动启动失败')
        if missing:
            ready, preflight_error = image_workflow_preflight()
            if not ready:
                raise RuntimeError(preflight_error)
        for pos, key in enumerate(missing, 1):
            raise_if_project_stopped(pid, run_id)
            proj = load_project(pid)
            if not proj:
                raise RuntimeError('项目在批量生成期间不存在')
            prep = prep_state(proj)
            item = next((x for x in prep.get('asset_plan', []) if x.get('key') == key), None)
            if not item:
                continue
            if item.get('path') and os.path.exists(item['path']):
                prep['asset_batch'].update({'completed': pos, 'current': None, 'updated': time.time()})
                prep_touch(proj)
                continue
            prompt = str(item.get('prompt') or '').strip()
            item.update({'status': 'generating', 'progress': 3, 'message': '正在加载模型并提交任务',
                         'error': None, 'confirmed': False, 'updated': time.time()})
            prep['asset_batch'].update({
                'current': item.get('name'), 'current_key': key, 'current_name': item.get('name'),
                'current_progress': 3, 'overall_progress': round(100 * ((pos - 1) + .03) / len(missing)),
                'message': f'正在生成 {pos}/{len(missing)}：{item.get("name")}',
                'updated': time.time()
            })
            prep_touch(proj)
            safe = re.sub(r'[^\w一-鿿-]', '_', key)
            progress_cb = make_prep_asset_progress_publisher(pid, key, item.get('name', key), pos, len(missing))
            path, err = gen_image(
                prompt, save_name=f"prep_{pid}_{safe}_{int(time.time())}.png",
                progress_cb=progress_cb, project_pid=pid, run_id=run_id)
            proj = load_project(pid) or proj
            prep = prep_state(proj)
            item = next((x for x in prep.get('asset_plan', []) if x.get('key') == key), None)
            if not item:
                continue
            if path:
                item.update({'path': path, 'source': 'ai', 'status': 'ready', 'progress': 100,
                             'message': '生成完成，等待审核', 'confirmed': False, 'error': None, 'updated': time.time()})
                proj.setdefault('assets', {})[key] = {
                    'path': path, 'kind': item.get('kind'), 'source': 'ai'
                }
            else:
                errors.append(f"{item.get('name')}：{err or '生成失败'}")
                item.update({'status': 'failed', 'error': err or '生成失败', 'message': err or '生成失败', 'updated': time.time()})
            prep['asset_batch'].update({
                'completed': pos, 'succeeded': pos - len(errors), 'failed': len(errors), 'current': None,
                'current_key': None, 'current_progress': 0, 'overall_progress': round(100 * pos / len(missing)),
                'message': f'已处理 {pos}/{len(missing)}', 'updated': time.time()
            })
            prep_touch(proj)
        proj = load_project(pid)
        if proj:
            prep = prep_state(proj)
            plan = prep.get('asset_plan', [])
            ready = sum(bool(x.get('path') and os.path.exists(x.get('path', ''))) for x in plan)
            total = len(plan)
            prep['asset_batch'].update({
                'status': 'failed' if errors else 'review', 'current': None,
                'current_key': None, 'current_progress': 0, 'overall_progress': 100,
                'ready': ready, 'all_total': total, 'succeeded': ready, 'failed': len(errors),
                'message': ('；'.join(errors[:3]) if errors else '全部图片已准备好，请统一审核'),
                'updated': time.time()
            })
            prep_touch(proj)
        final_status = 'failed' if errors else 'done'
        final_message = ('部分图片生成失败，可重试缺失图片' if errors else '批量图片生成完成')
    except ProjectStopRequested as exc:
        final_status = 'stopped'
        final_message = str(exc) or '已停止，完成的图片已保留'
        proj = load_project(pid)
        if proj:
            batch = prep_state(proj).setdefault('asset_batch', {})
            batch.update({'status': 'stopped', 'current': None, 'message': final_message, 'updated': time.time()})
            prep_touch(proj)
    except Exception as exc:
        proj = load_project(pid)
        if proj:
            prep = prep_state(proj)
            batch = prep.setdefault('asset_batch', {})
            batch.update({'status': 'failed', 'current': None, 'message': str(exc), 'updated': time.time()})
            prep_touch(proj)
    finally:
        try:
            if final_status == 'stopped':
                finalize_project_stopped(pid, run_id, final_message)
            else:
                update_production_job(
                    pid, run_id, status=final_status, stop_requested=False,
                    current_prompt_id=None, current_prompt_kind=None,
                    message=final_message, finished=time.time())
        finally:
            end_production_job(pid, run_id)
            with _PREP_ASSET_BATCH_LOCK:
                _PREP_ASSET_BATCH_THREADS.pop(pid, None)

@app.route('/api/preproduction/<pid>/assets/generate-missing', methods=['POST'])
def api_preproduction_generate_missing_assets(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    prep = prep_state(proj)
    if prep.get('step') != 'assets':
        return jsonify({'ok': False, 'msg': '当前不在资产审核阶段'}), 409
    d = request.get_json(force=True, silent=True) or {}
    incoming = d.get('prompts') or {}
    if isinstance(incoming, dict):
        for item in prep.get('asset_plan', []):
            prompt = str(incoming.get(item.get('key')) or '').strip()
            if prompt:
                item['prompt'] = prompt
    with _PREP_ASSET_BATCH_LOCK:
        active = _PREP_ASSET_BATCH_THREADS.get(pid)
        manual = _PREP_ASSET_MANUAL_THREADS.get(pid)
        if (active and active.is_alive()) or (manual and manual.is_alive()):
            return jsonify({'ok': False, 'msg': '已有图片生成任务进行中'}), 409
        try:
            run_id = begin_production_job(pid, 'asset_batch')
        except ProjectAlreadyRunning as exc:
            return jsonify({'ok': False, 'msg': str(exc)}), 409
        thread = threading.Thread(target=run_preproduction_asset_batch, args=(pid, run_id), daemon=True)
        _PREP_ASSET_BATCH_THREADS[pid] = thread
        prep['asset_batch'] = {
            'status': 'starting', 'total': 0, 'completed': 0, 'failed': 0,
            'succeeded': 0, 'current_progress': 0, 'overall_progress': 0,
            'message': '正在启动批量生成', 'updated': time.time()
        }
        prep_touch(proj)
        try:
            thread.start()
        except Exception as exc:
            _PREP_ASSET_BATCH_THREADS.pop(pid, None)
            update_production_job(pid, run_id, status='failed', message=f'任务启动失败：{exc}')
            end_production_job(pid, run_id)
            return jsonify({'ok': False, 'msg': f'任务启动失败：{exc}'}), 500
    return jsonify({'ok': True, 'started': True, 'preproduction': preproduction_view(proj)}), 202

@app.route('/api/preproduction/<pid>/asset/upload', methods=['POST'])
def api_preproduction_upload_asset(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    key = request.form.get('key', '')
    f = request.files.get('file')
    item = next((x for x in prep_state(proj).get('asset_plan', []) if x.get('key') == key), None)
    if not item or not f:
        return jsonify({'ok': False, 'msg': '资产项或文件缺失'}), 400
    if prep_asset_generation_active(pid):
        return jsonify({'ok': False, 'msg': '图片生成进行中，请等待当前任务完成'}), 409
    ext = os.path.splitext(f.filename or '')[1].lower() or '.png'
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        return jsonify({'ok': False, 'msg': '仅支持 png/jpg/webp 图片'}), 400
    safe = re.sub(r'[^\w一-鿿-]', '_', key)
    path = os.path.join(ASSETS_DIR, f"prep_upload_{pid}_{safe}_{int(time.time())}{ext}")
    f.save(path)
    item.update({'path': path, 'source': 'upload', 'status': 'ready', 'progress': 100,
                 'message': '已上传，等待审核', 'confirmed': False, 'error': None, 'updated': time.time()})
    proj.setdefault('assets', {})[key] = {'path': path, 'kind': item.get('kind'), 'uploaded': True, 'source': 'upload'}
    prep_touch(proj)
    return jsonify({'ok': True, 'url': prep_asset_url(path), 'item': {**item, 'url': prep_asset_url(path)}})

@app.route('/api/preproduction/<pid>/asset/confirm', methods=['POST'])
def api_preproduction_confirm_asset(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    if prep_asset_generation_active(pid):
        return jsonify({'ok': False, 'msg': '图片生成进行中，请等待当前任务完成'}), 409
    d = request.get_json(force=True, silent=True) or {}
    key = str(d.get('key') or '')
    item = next((x for x in prep_state(proj).get('asset_plan', []) if x.get('key') == key), None)
    if not item or not item.get('path') or not os.path.exists(item['path']):
        return jsonify({'ok': False, 'msg': '请先上传或生成图片'}), 400
    item.update({'confirmed': True, 'status': 'confirmed', 'progress': 100, 'message': '已确认可用', 'updated': time.time()})
    prep_touch(proj)
    return jsonify({'ok': True, 'item': {**item, 'url': prep_asset_url(item.get('path'))}})

@app.route('/api/preproduction/<pid>/assets/confirm', methods=['POST'])
def api_preproduction_confirm_all_assets(pid):
    proj = load_project(pid)
    if not proj:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    if prep_asset_generation_active(pid):
        return jsonify({'ok': False, 'msg': '图片生成进行中，请等待当前任务完成'}), 409
    plan = prep_state(proj).get('asset_plan', [])
    pending = [x.get('name') for x in plan
               if not x.get('path') or not os.path.exists(x.get('path', ''))]
    if pending:
        return jsonify({'ok': False, 'msg': f'还有 {len(pending)} 项没有图片：{"、".join(pending[:5])}'}), 409
    now = time.time()
    for item in plan:
        if item.get('path') and os.path.exists(item.get('path', '')):
            item.update({'confirmed': True, 'status': 'confirmed', 'progress': 100, 'message': '已确认可用', 'updated': now})
    prep = prep_state(proj)
    prep.update({'assets_confirmed': True, 'step': 'preproduction_done'})
    prep_touch(proj)
    return jsonify({'ok': True, 'preproduction': preproduction_view(proj)})

@app.route('/api/desktop/v1/capabilities')
def api_desktop_capabilities():
    return jsonify({
        'ok': True,
        'safe_mode': desktop_safe_mode(),
        'api_version': DESKTOP_API_VERSION,
        'app_version': app_version(),
        'backend': 'v2',
        'features': {
            'project_list': True,
            'project_read': True,
            'project_create_draft': True,
            'environment_status': True,
            'production_start': False,
        },
    })

@app.route('/api/desktop/v1/status')
def api_desktop_status():
    base, _, model = get_llm_endpoint()
    try:
        response = requests.get(f"{base}/models", timeout=3)
        llm_ok = response.status_code == 200
    except Exception:
        llm_ok = False
    try:
        comfy_port = urlparse(comfy_url()).port or 8190
    except ValueError:
        comfy_port = 8190
    try:
        backend_port = urlparse(request.host_url).port or 7861
    except ValueError:
        backend_port = 7861
    return jsonify({
        'ok': True,
        'api_version': DESKTOP_API_VERSION,
        'app_version': app_version(),
        'services': {
            'backend': {'online': True, 'port': backend_port},
            'llm': {'online': llm_ok, 'model': model},
            'comfyui': {'online': comfy_check(), 'port': comfy_port},
            'ffmpeg': {'online': bool(find_ffmpeg())},
        },
        'acceleration': acceleration_status(),
    })

@app.route('/api/desktop/v1/projects')
def api_desktop_projects():
    items = []
    for fn in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        if not fn.endswith('.json'):
            continue
        try:
            with open(os.path.join(PROJECTS_DIR, fn), 'r', encoding='utf-8') as f:
                items.append(desktop_project_summary(json.load(f)))
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            continue
    return jsonify({'ok': True, 'projects': items})

@app.route('/api/desktop/v1/projects', methods=['POST'])
def api_desktop_create_project():
    if not request.is_json:
        return jsonify({'ok': False, 'msg': '请求必须使用 JSON'}), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'msg': 'JSON 请求体无效'}), 400
    title = str(data.get('name') or data.get('title') or '未命名短剧').strip()[:80] or '未命名短剧'
    content = str(data.get('content') or '').strip()
    if len(content) > 100000:
        return jsonify({'ok': False, 'msg': '故事或提示词内容过长'}), 400
    input_mode = 'h3_prompt' if data.get('input_mode') == 'h3_prompt' else 'story'
    if input_mode == 'h3_prompt' and content:
        _, error = parse_h3_timeline(content)
        if error:
            return jsonify({'ok': False, 'msg': error}), 400
    scene_mode = str(data.get('scene_reference_mode') or 'auto').lower()
    if scene_mode not in ('auto', 'photo', 'concept'):
        scene_mode = 'auto'
    try:
        shot_duration = 'auto' if str(data.get('shot_duration', 'auto')).lower() == 'auto' else max(3, min(int(data.get('shot_duration')), 15))
        shot_count = 'auto' if str(data.get('shot_count', 'auto')).lower() == 'auto' else max(1, min(int(data.get('shot_count')), 20))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'msg': '片段时长或片段数参数无效'}), 400
    pid = uuid.uuid4().hex[:12]
    has_content = bool(content)
    aspect_ratio = desktop_aspect_ratio(data.get('aspect_ratio'))
    if aspect_ratio not in ASPECT_IMG_SIZE:
        return jsonify({'ok': False, 'msg': '画面比例参数无效'}), 400
    project = {
        'id': pid,
        'title': title,
        'idea': content,
        'input_mode': input_mode,
        'style': str(data.get('style') or CONFIG.get('style', '电影写实')).strip() or '电影写实',
        'aspect_ratio': aspect_ratio,
        'scene_reference_mode': scene_mode,
        'shot_duration': shot_duration,
        'shot_count': shot_count,
        'script': None,
        'prompts': {},
        'assets': {},
        'shots': [],
        'final': None,
        'created': time.time(),
        'custom_assets': bool(data.get('custom_assets')),
        'generate_storyboards': bool(data.get('generate_storyboards', False)),
        'preproduction': {
            'version': 2,
            'step': ('prompt_review' if input_mode == 'h3_prompt' else 'outline') if has_content else 'input',
            'story_confirmed': has_content and input_mode == 'story',
            'outline': '',
            'outline_confirmed': False,
            'script_confirmed': False,
            'prompts_confirmed': False,
            'asset_plan': [],
            'assets_confirmed': False,
            'asset_batch': {'status': 'waiting', 'total': 0, 'completed': 0, 'failed': 0},
            'updated': time.time(),
        },
    }
    if input_mode == 'h3_prompt' and content:
        project['manual_h3_prompt'] = content
    save_project(project)
    return jsonify({'ok': True, 'project': desktop_project_detail(project)}), 201

@app.route('/api/desktop/v1/projects/<pid>')
def api_desktop_project(pid):
    if not re.fullmatch(r'[0-9a-zA-Z_-]{1,64}', pid):
        return jsonify({'ok': False, 'msg': '非法项目ID'}), 400
    project = load_project(pid)
    if not project:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    return jsonify({'ok': True, 'project': desktop_project_detail(project)})

@app.route('/api/desktop/v1/projects/<pid>', methods=['PATCH'])
def api_desktop_update_project(pid):
    if not re.fullmatch(r'[0-9a-zA-Z_-]{1,64}', pid):
        return jsonify({'ok': False, 'msg': '非法项目ID'}), 400
    if not request.is_json:
        return jsonify({'ok': False, 'msg': '请求必须使用 JSON'}), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({'ok': False, 'msg': 'JSON 请求体无效'}), 400
    project = load_project(pid)
    if not project:
        return jsonify({'ok': False, 'msg': '项目不存在'}), 404
    title_value = data.get('name', data.get('title'))
    if title_value is not None:
        project['title'] = str(title_value).strip()[:80] or '未命名短剧'
    editable_draft = not project.get('script') and not project.get('shots') and not project.get('final')
    if any(key in data for key in ('content', 'input_mode', 'style', 'aspect_ratio', 'scene_reference_mode')) and not editable_draft:
        return jsonify({'ok': False, 'msg': '项目已进入生产阶段，只允许修改名称'}), 409
    if editable_draft:
        input_mode = 'h3_prompt' if data.get('input_mode', project.get('input_mode')) == 'h3_prompt' else 'story'
        content = str(data.get('content', project.get('idea') or '')).strip()
        if len(content) > 100000:
            return jsonify({'ok': False, 'msg': '故事或提示词内容过长'}), 400
        if input_mode == 'h3_prompt' and content:
            _, error = parse_h3_timeline(content)
            if error:
                return jsonify({'ok': False, 'msg': error}), 400
        scene_mode = str(data.get('scene_reference_mode', project.get('scene_reference_mode', 'auto'))).lower()
        if scene_mode not in ('auto', 'photo', 'concept'):
            return jsonify({'ok': False, 'msg': '场景参考模式参数无效'}), 400
        aspect_ratio = desktop_aspect_ratio(data.get('aspect_ratio', project.get('aspect_ratio')))
        if aspect_ratio not in ASPECT_IMG_SIZE:
            return jsonify({'ok': False, 'msg': '画面比例参数无效'}), 400
        project.update({
            'idea': content,
            'input_mode': input_mode,
            'style': str(data.get('style', project.get('style') or CONFIG.get('style', '电影写实'))).strip() or '电影写实',
            'aspect_ratio': aspect_ratio,
            'scene_reference_mode': scene_mode,
        })
        if input_mode == 'h3_prompt' and content:
            project['manual_h3_prompt'] = content
        else:
            project.pop('manual_h3_prompt', None)
        prep = project.setdefault('preproduction', {})
        prep.update({
            'step': ('prompt_review' if input_mode == 'h3_prompt' else 'outline') if content else 'input',
            'story_confirmed': bool(content) and input_mode == 'story',
            'updated': time.time(),
        })
    save_project(project)
    return jsonify({'ok': True, 'project': desktop_project_detail(project)})

@app.route('/api/projects')
def api_projects():
    items = []
    for fn in sorted(os.listdir(PROJECTS_DIR), reverse=True):
        if fn.endswith('.json'):
            try:
                with open(os.path.join(PROJECTS_DIR, fn), 'r', encoding='utf-8') as f:
                    p = json.load(f)
                items.append({"id": p['id'], "title": p.get('title') or p.get('idea', '')[:20], "final": p.get('final'),
                              "created": p.get('created', 0),
                              "is_demo": bool(p.get('is_demo')),
                              "preproduction_step": (p.get('preproduction') or {}).get('step')})
            except Exception:
                continue
    return jsonify(items)

@app.route('/api/project/<pid>')
def api_project(pid):
    p = load_project(pid)
    if not p:
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    if recover_interrupted_shot_retries(p):
        p = load_project(pid) or p
    # 转换资产路径为URL
    assets_view = []
    for k, a in p.get('assets', {}).items():
        assets_view.append({"key": k, "kind": a.get('kind'), "url": f"/file/assets/{os.path.basename(a['path'])}"})
    return jsonify({"ok": True, "project": {
        "id": p['id'], "title": p.get('title'), "idea": p.get('idea'),
        "input_mode": p.get('input_mode', 'story'), "manual_h3_prompt": p.get('manual_h3_prompt'),
        "timeline_segments": p.get('timeline_segments', []),
        "reference_editor": bool(p.get('reference_creation', {}).get('editor')),
        "reference_origin": p.get('reference_origin'),
        "scene_reference_mode": p.get('scene_reference_mode', 'auto'),
        "script": p.get('script'), "assets": assets_view,
        "shots": p.get('shots', []), "final": p.get('final'),
        "custom_assets": p.get('custom_assets'), "assets_confirmed": p.get('assets_confirmed'),
        "generate_storyboards": p.get('generate_storyboards', True),
        "item_states": p.get('item_states', {}),
        "production_job": p.get('production_job', {}),
        "preproduction": preproduction_view(p) if p.get('preproduction') else None,
        "prompts": p.get('prompts', {}), "style": p.get('style')
    }})

@app.route('/api/project/<pid>/delete', methods=['POST'])
def api_delete_project(pid):
    """删除历史项目：存档 json + 该项目 outputs 视频目录"""
    if not re.fullmatch(r'[0-9a-zA-Z_-]{1,64}', pid):
        return jsonify({"ok": False, "msg": "非法项目ID"}), 400
    p = project_path(pid)
    if not os.path.exists(p):
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    project = load_project(pid)
    if project and project.get('is_demo'):
        return jsonify({"ok": False, "msg": "内置案例不可删除"}), 403
    os.remove(p)
    out_dir = os.path.join(OUTPUTS_DIR, pid)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir, ignore_errors=True)
    print(f"[项目] 已删除 {pid}")
    return jsonify({"ok": True})

@app.route('/api/upload_asset', methods=['POST'])
def api_upload_asset():
    """自定义参考图上传：保存到assets目录并登记进项目存档（kind自动从key前缀解析）"""
    pid = request.form.get('pid', '')
    key = request.form.get('key', '')
    f = request.files.get('file')
    if not pid or not key or not f:
        return jsonify({"ok": False, "msg": "参数缺失"}), 400
    if not re.fullmatch(r'[0-9a-zA-Z_-]{1,64}', pid):
        return jsonify({"ok": False, "msg": "非法项目ID"}), 400
    proj = load_project(pid)
    if not proj:
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    kind = key.split('_', 1)[0] if '_' in key else 'character'
    if kind not in ('char', 'scene', 'prop'):
        return jsonify({"ok": False, "msg": "非法资产类型"}), 400
    ext = os.path.splitext(f.filename or '')[1].lower() or '.png'
    if ext not in ('.png', '.jpg', '.jpeg', '.webp'):
        return jsonify({"ok": False, "msg": "仅支持 png/jpg/webp 图片"}), 400
    safe = re.sub(r'[^\w一-鿿-]', '_', key)
    save_path = os.path.join(ASSETS_DIR, f"upload_{pid}_{safe}{ext}")
    f.save(save_path)
    assets = proj.get('assets', {})
    assets[key] = {"path": save_path, "kind": kind, "uploaded": True}
    proj['assets'] = assets
    name = key.split('_', 1)[1] if '_' in key else key
    set_item_state(proj, 'assets', key, 'done', 100, '已上传参考图', kind, name)
    print(f"[上传] {pid}/{key} -> {os.path.basename(save_path)}")
    return jsonify({"ok": True, "url": f"/file/assets/{os.path.basename(save_path)}"})

@app.route('/api/confirm_assets', methods=['POST'])
def api_confirm_assets():
    """用户确认参考图就绪：对上传图用视觉LLM重写外观描述（以图为准），置确认标志让管线继续"""
    d = request.get_json(force=True, silent=True) or {}
    pid = d.get('pid', '')
    proj = load_project(pid)
    if not proj:
        return jsonify({"ok": False, "msg": "项目不存在"}), 404
    assets = proj.get('assets', {})
    script = proj.get('script') or {}
    rewritten, failed = 0, 0
    for key, a in assets.items():
        if not a.get('uploaded') or a.get('desc_done'):
            continue
        name = key.split('_', 1)[1] if '_' in key else key
        kind = a.get('kind', 'char')
        new_desc, derr = describe_uploaded_asset(a['path'], kind, name)
        if new_desc:
            pool_map = {'char': 'characters', 'character': 'characters', 'scene': 'scenes', 'prop': 'props'}
            pool = script.get(pool_map.get(kind, 'characters'), [])
            for item in pool:
                if item.get('name') == name:
                    if kind in ('char', 'character'):
                        item['appearance'] = new_desc
                    else:
                        item['description'] = new_desc
                    break
            a['desc_done'] = True
            rewritten += 1
        else:
            failed += 1
            print(f"[确认资产] {key} 视觉重写失败(保留原描述): {derr}")
    proj['script'] = script
    proj['assets'] = assets
    proj['assets_confirmed'] = True
    save_project(proj)
    print(f"[确认资产] {pid} 重写{rewritten}条 失败{failed}条")
    return jsonify({"ok": True, "rewritten": rewritten, "failed": failed})

from reference_api import register_reference_api
register_reference_api(sys.modules[__name__])

if __name__ == '__main__':
    web_host = os.environ.get('AIX_WEB_HOST', '127.0.0.1')
    try:
        web_port = int(os.environ.get('AIX_WEB_PORT', '7861'))
    except ValueError:
        web_port = 7861
    browser_host = '127.0.0.1' if web_host in ('0.0.0.0', '::') else web_host
    web_url = f"http://{browser_host}:{web_port}"
    print("=" * 50)
    print("  AIX爱客 · AI 短剧工厂 V2 实验版")
    print(f"  访问: {web_url}")
    print("=" * 50)
    if os.environ.get('AIX_NO_BROWSER') != '1':
        threading.Timer(1.5, lambda: webbrowser.open(web_url)).start()
    app.run(host=web_host, port=web_port, threaded=True, debug=False)
