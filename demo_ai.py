"""使用已启动的本机 ComfyUI 执行程序测试图，所有数据保存在本地。"""
from pathlib import Path
import argparse
from engine import Settings
from make_demo import main as make_demo
from mcp_client import dispatch_batch

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',default='RealESRGAN_x4plus.safetensors')
    parser.add_argument('--model-revision',default='')
    parser.add_argument('--server',default='http://127.0.0.1:8188')
    args=parser.parse_args()
    make_demo()
    root=Path(__file__).resolve().parent
    result=dispatch_batch(root/'demo/input',root/'results/ai_demo',Settings(
        mode='fixed_ai',long_edge=960,server=args.server,model=args.model,model_revision=args.model_revision))
    return 1 if result['failed'] else 0

if __name__=='__main__':raise SystemExit(main())
