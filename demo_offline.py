"""不启动 ComfyUI 的离线演示。"""
from pathlib import Path
from engine import Settings,run_batch
from make_demo import main as make_demo


def main():
    base=Path(__file__).resolve().parent
    make_demo()
    result=run_batch(base/'demo'/'input',base/'results'/'offline_demo',Settings(mode='interpolation',long_edge=960))
    return 1 if result['failed'] else 0


if __name__=='__main__':
    raise SystemExit(main())
