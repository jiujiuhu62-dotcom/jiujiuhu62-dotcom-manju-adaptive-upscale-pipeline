"""本机 stdio MCP 客户端。只传路径与 JSON，不传图片字节给 MCP 客户端。"""
from pathlib import Path
import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import asdict

BASE = Path(__file__).resolve().parent


class MCPClient:
    def __init__(self,read_roots=(),workspace=BASE,trace=None,notify=print):
        command = [sys.executable,str(BASE/'mcp_server.py'),'--workspace',str(workspace)]
        for root in read_roots:
            command += ['--read-root',str(Path(root).resolve())]
        self.process = subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True,encoding='utf-8',bufsize=1,
            env={**os.environ,'PYTHONIOENCODING':'utf-8'},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        self.responses = queue.Queue()
        self.sequence = 0
        self.trace = Path(trace) if trace else None
        self.notify = notify
        self.trace_lock = threading.Lock()
        self.readers = [threading.Thread(target=self.read_stdout,daemon=True),
                        threading.Thread(target=self.read_stderr,daemon=True)]
        for thread in self.readers:
            thread.start()
        try:
            initialize = self.request('initialize',{'protocolVersion':'2024-11-05','capabilities':{},
                                      'clientInfo':{'name':'manju-local-client','version':'1.0.0'}})
            if initialize.get('protocolVersion')!='2024-11-05':
                raise RuntimeError('MCP 协议版本协商失败')
            self.send({'jsonrpc':'2.0','method':'notifications/initialized'})
        except BaseException:
            self.close()
            raise

    def audit(self,direction,message):
        if self.trace:
            with self.trace_lock:
                self.trace.parent.mkdir(parents=True,exist_ok=True)
                with self.trace.open('a',encoding='utf-8') as file:
                    file.write(json.dumps({'time':time.time(),'direction':direction,'message':message},
                                          ensure_ascii=False)+'\n')

    def read_stdout(self):
        try:
            for line in self.process.stdout:
                message = json.loads(line)
                self.audit('server_to_client',message)
                self.responses.put(message)
        except Exception as exc:
            self.responses.put({'error':{'message':'MCP 读取失败：'+str(exc)}})

    def read_stderr(self):
        for line in self.process.stderr:
            self.notify(line.rstrip())

    def send(self,message):
        self.audit('client_to_server',message)
        self.process.stdin.write(json.dumps(message,ensure_ascii=False)+'\n')
        self.process.stdin.flush()

    def request(self,method,params=None,timeout=1500,stop=None):
        self.sequence += 1
        request_id = self.sequence
        self.send({'jsonrpc':'2.0','id':request_id,'method':method,'params':params or {}})
        deadline = time.monotonic()+timeout
        cancelled = False
        while time.monotonic()<deadline:
            if stop is not None and stop.is_set() and not cancelled:
                self.send({'jsonrpc':'2.0','method':'notifications/cancelled',
                           'params':{'requestId':request_id,'reason':'Stop after current image'}})
                cancelled = True
            try:
                response = self.responses.get(timeout=.1)
            except queue.Empty:
                if self.process.poll() is not None:
                    raise RuntimeError('本地 MCP 子进程退出')
                continue
            if response.get('error'):
                raise RuntimeError(response['error']['message'])
            if response.get('id')==request_id:
                return response['result']
        raise TimeoutError('MCP 调用超时；请检查本地 ComfyUI 队列再决定是否重试')

    def call(self,name,arguments,timeout=1500,stop=None):
        result = self.request('tools/call',{'name':name,'arguments':arguments},timeout,stop)
        if result.get('isError'):
            raise RuntimeError(result['content'][0]['text'])
        return json.loads(result['content'][0]['text'])

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        for thread in self.readers:
            thread.join(timeout=2)
        self.process.stdout.close()
        self.process.stderr.close()

    def __enter__(self):
        return self

    def __exit__(self,*args):
        self.close()


def dispatch_batch(input_dir,output_dir,settings,notify=print,stop=None):
    if settings.mode=='interpolation':
        from engine import run_batch
        return run_batch(input_dir,output_dir,settings,notify,stop)
    output = Path(output_dir).resolve()
    if not any(output.is_relative_to(BASE/name) for name in ['results','private_tests','demo']):
        raise ValueError('AI 输出目录需位于本项目 results、private_tests 或 demo 内')
    with MCPClient(read_roots=[input_dir],trace=output/'mcp_trace.jsonl',notify=notify) as client:
        return client.call('process_batch',{'input_dir':str(Path(input_dir).resolve()),
            'output_dir':str(output),'settings':asdict(settings)},stop=stop,
            timeout=max(1500,settings.timeout*max(1,len(list(Path(input_dir).rglob('*.png'))))+60))
