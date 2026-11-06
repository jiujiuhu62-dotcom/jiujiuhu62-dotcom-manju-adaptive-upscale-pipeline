"""仅管理本次创建的本机 ComfyUI 子进程，使用独立目录及离线设置。"""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import time
from engine import ComfyClient

BASE = Path(__file__).resolve().parent


def load_project():
    return json.loads((BASE / 'project.json').read_text('utf-8'))


class LocalComfy:
    def __init__(self, announce=print):
        self.config = load_project()
        self.announce = announce
        self.process = None
        self.logs = []
        self.owns_service = False

    def start(self):
        config = self.config
        client = ComfyClient(config['server'])
        try:
            models, _ = client.inspect()
            if config['model'] not in models:
                raise RuntimeError('8188 服务已存在，但没有项目模型。请按 README 添加模型路径；不会重启已有服务。')
            self.announce('复用已有本机 8188 服务；不修改或终止该服务。')
            return client
        except OSError:
            pass
        comfy = Path(config['comfy_path'])
        python = Path(config['python'])
        if not (comfy / 'main.py').is_file() or not python.is_file():
            raise ValueError('请在 project.json 中设置正确的 ComfyUI 和 Python 路径')
        weight = BASE / 'models' / 'upscale_models' / config['model']
        if not weight.is_file():
            raise ValueError('缺少模型：' + str(weight))
        if hashlib.sha256(weight.read_bytes()).hexdigest() != config['model_sha256']:
            raise ValueError('模型 SHA256 不符，请核对 project.json 和实际权重')
        runtime = BASE / 'runtime'
        for name in ['input','output','temp','user']:
            (runtime / name).mkdir(parents=True, exist_ok=True)
        paths = runtime / 'model-paths.yaml'
        paths.write_text('manju_local:\n  base_path: ' + json.dumps(BASE.as_posix(), ensure_ascii=False)
                         + '\n  upscale_models: models/upscale_models/\n', encoding='utf-8')
        # 不加载自定义节点或云 API 节点。所有输入输出均留在项目 runtime 中。
        command = [str(python), '-s', str(comfy/'main.py'), '--listen','127.0.0.1','--port','8188',
                   '--disable-all-custom-nodes','--disable-api-nodes','--disable-auto-launch',
                   '--extra-model-paths-config',str(paths),'--database-url','sqlite:///:memory:']
        for name in ['input','output','temp','user']:
            command += ['--'+name+'-directory', str(runtime/name)]
        environment = {**os.environ,'PYTHONIOENCODING':'utf-8','HF_HUB_OFFLINE':'1',
                       'TRANSFORMERS_OFFLINE':'1','HF_HUB_DISABLE_TELEMETRY':'1','DO_NOT_TRACK':'1'}
        self.logs = [(runtime/('server.'+suffix+'.log')).open('w',encoding='utf-8') for suffix in ['stdout','stderr']]
        self.process = subprocess.Popen(command,cwd=comfy,env=environment,
            stdout=self.logs[0],stderr=self.logs[1],
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.owns_service = True
        self.announce('正在启动本机 ComfyUI：' + config['server'])
        deadline = time.monotonic()+55
        try:
            while time.monotonic()<deadline:
                if self.process.poll() is not None:
                    raise RuntimeError('ComfyUI 启动失败，请查看 runtime/server.stderr.log')
                try:
                    models, _ = client.inspect()
                    if config['model'] not in models:
                        raise RuntimeError('项目权重未被服务加载')
                    self.announce('本机 ComfyUI 已就绪')
                    return client
                except OSError:
                    time.sleep(.5)
            raise TimeoutError('本机服务启动超过 55 秒，请检查日志')
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for log in self.logs:
            log.close()
        self.logs = []

    def __enter__(self):
        self.start()
        return self

    def __exit__(self,*args):
        self.close()
