"""命令行入口，适合自动化或按模式分别运行对照实验。"""
import argparse
import json
from engine import Settings
from mcp_client import dispatch_batch as run_batch


def main():
    parser = argparse.ArgumentParser(description="漫剧分镜尺寸自适应超分批处理")
    parser.add_argument("input", help="输入目录")
    parser.add_argument("output", help="独立输出目录，不能位于输入目录内")
    parser.add_argument("--mode", choices=["adaptive", "fixed_ai", "interpolation"], default="adaptive")
    parser.add_argument("--long-edge", type=int, default=1920)
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--model", default="")
    parser.add_argument("--model-revision", default="")
    parser.add_argument("--timeout", type=float, default=1200)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument("--width",type=int,default=0,help="可选的精确参考宽度，需同时指定 height")
    parser.add_argument("--height",type=int,default=0,help="精确参考高度；普通使用建议设置长边")
    args = parser.parse_args()
    try:
        result = run_batch(args.input, args.output, Settings(long_edge=args.long_edge,
            mode=args.mode, server=args.server, model=args.model, model_revision=args.model_revision,
            timeout=args.timeout, resume=not args.no_resume,
            output_width=args.width,output_height=args.height))
        return 1 if result["failed"] else 0
    except KeyboardInterrupt:
        print("已终止本地等待。若已提交 AI 任务，请检查 ComfyUI 队列；不会自动全局中断。")
        return 130
    except Exception as exc:
        print("错误：", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
