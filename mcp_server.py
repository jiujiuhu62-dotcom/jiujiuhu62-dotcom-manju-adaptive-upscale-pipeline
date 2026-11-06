"""本机 MCP stdio 服务，JSON-RPC 2.0，协议 2024-11-05。

所有 stdout 仅含 JSON 消息；日志走 stderr；不提供图片/资源读取工具。
仅暴露 get_status/process_batch/degrade_image。文件读写受启动参数的根目录约束。
"""
from dataclasses import asdict
from pathlib import Path
import argparse
import json
import sys
import threading

from engine import ComfyClient, Settings, VERSION, run_batch
from image_eval import degrade_image

BASE = Path(__file__).resolve().parent
PROTOCOL = '2024-11-05'


class Server:
    def __init__(self,workspace,read_roots):
        self.workspace = Path(workspace).resolve()
        self.read_roots = [self.workspace]+[Path(p).resolve() for p in read_roots]
        self.initialized = False
        self.ready = False
        self.write_lock = threading.Lock()
        self.busy = threading.Lock()
        self.tasks = {}
        self.threads = []

    def send(self,message):
        with self.write_lock:
            sys.stdout.write(json.dumps(message,ensure_ascii=False,allow_nan=False)+'\n')
            sys.stdout.flush()

    def resolve(self,path,write=False):
        resolved = Path(path).resolve()
        roots = ([self.workspace/'results',self.workspace/'private_tests',self.workspace/'demo']
                 if write else self.read_roots)
        if not any(resolved.is_relative_to(root.resolve()) for root in roots):
            raise ValueError('路径不在本机 MCP 授权范围内')
        return resolved

    def definitions(self):
        settings_schema = {'type':'object','properties':{
            'long_edge':{'type':'integer','minimum':64,'maximum':8192},
            'mode':{'enum':['adaptive','fixed_ai','interpolation']},
            'server':{'type':'string'},'model':{'type':'string'},'model_revision':{'type':'string'},
            'timeout':{'type':'number','exclusiveMinimum':0},'resume':{'type':'boolean'},
            'output_width':{'type':'integer','minimum':0,'maximum':8192},
            'output_height':{'type':'integer','minimum':0,'maximum':8192}},'additionalProperties':False}
        return [
            {'name':'get_status','description':'查询本机 ComfyUI 节点、模型与设备；不返回图片。',
             'inputSchema':{'type':'object','properties':{'server':{'type':'string'}},'additionalProperties':False}},
            {'name':'process_batch','description':'只在本地执行图片批处理，输出状态与本地路径；支持任务间取消。',
             'inputSchema':{'type':'object','properties':{'input_dir':{'type':'string'},
                 'output_dir':{'type':'string'},'settings':settings_schema},
                 'required':['input_dir','output_dir'],'additionalProperties':False}},
            {'name':'degrade_image','description':'本地降采样并模糊，生成低清图与参数；不返回图片内容。',
             'inputSchema':{'type':'object','properties':{'source':{'type':'string'},
                 'destination':{'type':'string'},'factor':{'type':'integer','enum':[2,3,4,8]},
                 'blur_radius':{'type':'number','minimum':0,'maximum':5}},
                 'required':['source','destination'],'additionalProperties':False}}
        ]

    def execute(self,name,args,stop):
        if name=='get_status':
            if set(args)-{'server'}:
                raise ValueError('未知参数')
            client = ComfyClient(args.get('server','http://127.0.0.1:8188'))
            models,version = client.inspect()
            return {'models':models,'comfyui_version':version,'system_stats':client.json('/system_stats')}
        if name=='process_batch':
            if set(args)-{'input_dir','output_dir','settings'}:
                raise ValueError('未知参数')
            source = self.resolve(args['input_dir'])
            output = self.resolve(args['output_dir'],write=True)
            return run_batch(source,output,Settings(**args.get('settings',{})),
                             notify=lambda s: print(s,file=sys.stderr,flush=True),stop=stop)
        if name=='degrade_image':
            if set(args)-{'source','destination','factor','blur_radius'}:
                raise ValueError('未知参数')
            return degrade_image(self.resolve(args['source']),self.resolve(args['destination'],write=True),
                                 args.get('factor',4),args.get('blur_radius',.6))
        raise ValueError('未知工具：'+name)

    def worker(self,request):
        request_id = request['id']
        stop = self.tasks[request_id]
        try:
            params = request.get('params',{})
            args = params.get('arguments',{})
            if not isinstance(args,dict):
                raise ValueError('arguments 必须是 object')
            value = self.execute(params['name'],args,stop)
            result = {'content':[{'type':'text','text':json.dumps(value,ensure_ascii=False,allow_nan=False)}],
                      'isError':False}
        except Exception as exc:
            result = {'content':[{'type':'text','text':str(exc)}],'isError':True}
        finally:
            self.tasks.pop(request_id,None)
            self.busy.release()
        self.send({'jsonrpc':'2.0','id':request_id,'result':result})

    def dispatch(self,request):
        if not isinstance(request,dict) or request.get('jsonrpc')!='2.0' or not isinstance(request.get('method'),str):
            self.send({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':'Invalid Request'}})
            return
        method = request['method']
        request_id = request.get('id')
        if method=='notifications/initialized':
            self.ready = self.initialized
            return
        if method=='notifications/cancelled':
            target = request.get('params',{}).get('requestId')
            if target in self.tasks:
                self.tasks[target].set()
            return
        if 'id' not in request:
            return
        if method=='initialize':
            self.initialized = True
            value = {'protocolVersion':PROTOCOL,'capabilities':{'tools':{'listChanged':False}},
                     'serverInfo':{'name':'manju-local-comfy','version':VERSION}}
        elif method=='ping':
            value = {}
        elif not self.ready:
            self.send({'jsonrpc':'2.0','id':request_id,'error':{'code':-32002,'message':'Not initialized'}})
            return
        elif method=='tools/list':
            value = {'tools':self.definitions()}
        elif method=='tools/call':
            if not self.busy.acquire(blocking=False):
                self.send({'jsonrpc':'2.0','id':request_id,'error':{'code':-32000,'message':'Local worker busy'}})
                return
            self.tasks[request_id] = threading.Event()
            worker = threading.Thread(target=self.worker,args=(request,),daemon=True)
            self.threads.append(worker)
            worker.start()
            return
        else:
            self.send({'jsonrpc':'2.0','id':request_id,'error':{'code':-32601,'message':'Method not found'}})
            return
        self.send({'jsonrpc':'2.0','id':request_id,'result':value})

    def serve(self):
        for line in sys.stdin:
            try:
                self.dispatch(json.loads(line))
            except json.JSONDecodeError:
                self.send({'jsonrpc':'2.0','id':None,'error':{'code':-32700,'message':'Parse error'}})
            except Exception as exc:
                self.send({'jsonrpc':'2.0','id':None,'error':{'code':-32600,'message':str(exc)}})
        for stop in list(self.tasks.values()):
            stop.set()
        for thread in self.threads:
            thread.join(timeout=10)


if __name__=='__main__':
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace',default=str(BASE))
    parser.add_argument('--read-root',action='append',default=[])
    args = parser.parse_args()
    Server(args.workspace,args.read_root).serve()
