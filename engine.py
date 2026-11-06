"""漫剧分镜批处理核心。仅连接本机 ComfyUI，不依赖第三方自定义节点。

尺寸路由与视觉验收分离：success 只表示文件生成成功，不表示画质合格。
"""
from __future__ import annotations

import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass

from PIL import Image, ImageCms, ImageOps

VERSION = "1.0.0"
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_bytes(path: Path, data: bytes):
    """先写同目录临时文件再替换，避免中断留下半张图。只替换本工具的输出。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_bytes(data)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, value):
    atomic_bytes(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def read_image(data: bytes) -> Image.Image:
    """统一 EXIF 朝向与 sRGB；首版仅接收静态、8 位常见 RGB/灰度图。

    不静默截取动画第一帧，也不把 16 位和 CMYK 的转换冒充无损处理。
    返回副本，避免图像依赖已关闭的文件句柄。
    """
    with Image.open(io.BytesIO(data)) as source:
        if getattr(source, "n_frames", 1) != 1:
            raise ValueError("首版只支持静态单帧图，不支持动画或多页 TIFF")
        if source.mode not in {"RGB", "RGBA", "L", "LA", "P"}:
            raise ValueError(f"暂不支持 {source.mode}，请先转换为 8 位 sRGB")
        source.load()
        image = ImageOps.exif_transpose(source)
        has_alpha = "A" in image.getbands() or "transparency" in image.info
        alpha = image.convert("RGBA").getchannel("A") if has_alpha else None
        rgb = image.convert("RGB")
        profile = source.info.get("icc_profile")
        if profile:
            try:
                # RGB 配置文件可以转换；其他色彩空间要求用户先显式转换。
                rgb = ImageCms.profileToProfile(
                    rgb, ImageCms.ImageCmsProfile(io.BytesIO(profile)),
                    ImageCms.createProfile("sRGB"), outputMode="RGB")
            except Exception as exc:
                raise ValueError("ICC 色彩转换失败，请先导出为 sRGB") from exc
        if alpha is not None:
            rgb.putalpha(alpha)
        return rgb


def target_size(width: int, height: int, long_edge: int) -> tuple[int, int]:
    """长边精确匹配目标；短边四舍五入，保持比例且不裁切、不补边。"""
    factor = long_edge / max(width, height)
    return max(1, round(width * factor)), max(1, round(height * factor))


@dataclass(frozen=True)
class Settings:
    long_edge: int = 1920
    mode: str = "adaptive"  # adaptive / fixed_ai / interpolation
    server: str = "http://127.0.0.1:8188"
    model: str = ""
    model_revision: str = ""  # 换权重后必须更新；留空时 AI 任务禁用复用
    timeout: float = 1200
    resume: bool = True
    output_width: int = 0
    output_height: int = 0

    def validate(self):
        for value in [self.long_edge,self.output_width,self.output_height]:
            if type(value) is not int:
                raise ValueError('尺寸参数必须是整数')
        if type(self.resume) is not bool or not isinstance(self.model_revision,str):
            raise ValueError('resume 必须是布尔值，模型版本标记必须是字符串')
        if not 64 <= self.long_edge <= 8192:
            raise ValueError("目标长边需在 64—8192 像素之间")
        if self.mode not in {"adaptive", "fixed_ai", "interpolation"}:
            raise ValueError("未知处理模式")
        if bool(self.output_width) != bool(self.output_height):
            raise ValueError("参考图精确尺寸必须同时指定宽与高")
        if self.output_width and not (1 <= self.output_width <= 8192 and 1 <= self.output_height <= 8192):
            raise ValueError("参考输出宽高需在 1—8192 像素内")
        if not math.isfinite(self.timeout) or self.timeout <= 0:
            raise ValueError("超时必须是正数")
        if self.mode != "interpolation":
            ComfyClient(self.server)
            if not self.model.strip():
                raise ValueError("请选择 ComfyUI 中可用的超分模型")


def choose_route(size: tuple[int, int], settings: Settings) -> str:
    if settings.mode == "interpolation":
        return "interpolation"
    edge = max(settings.output_width, settings.output_height) or settings.long_edge
    if settings.mode == "fixed_ai" or max(size) < edge:
        return "ai"
    return "resize_only"  # 只按尺寸路由；不等价于自动判断清晰度


def build_workflow(upload: str, model: str, width: int, height: int, prefix: str):
    """API 格式图；只使用本机 /object_info 中可核对的 ComfyUI 内置节点。

    模型按自身固有倍率推理，然后由 ImageScale 缩放至交付规格。
    不将固定 4x 模型包装为任意倍率推理，也不做扩散重绘和人脸替换。
    """
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": upload}},
        "2": {"class_type": "UpscaleModelLoader", "inputs": {"model_name": model}},
        "3": {"class_type": "ImageUpscaleWithModel", "inputs": {
            "upscale_model": ["2", 0], "image": ["1", 0]}},
        "4": {"class_type": "ImageScale", "inputs": {
            "image": ["3", 0], "upscale_method": "lanczos",
            "width": width, "height": height, "crop": "disabled"}},
        "5": {"class_type": "SaveImage", "inputs": {
            "images": ["4", 0], "filename_prefix": prefix}},
    }


class ComfyClient:
    def __init__(self, server: str):
        parts = urllib.parse.urlsplit(server.strip())
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path not in {"", "/"}):
            raise ValueError("首版仅允许本机 HTTP 地址，例如 http://127.0.0.1:8188")
        self.server = server.strip().rstrip("/")
        # 本机请求不经过系统 HTTP 代理，不把选中的图片转发到外部代理。
        class LocalOnlyRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                raise RuntimeError("拒绝 ComfyUI 重定向，图片请求只能到指定本机服务")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), LocalOnlyRedirect())

    def request(self, endpoint: str, payload=None, content_type="application/json", timeout=20):
        if isinstance(payload, dict):
            payload = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(self.server + endpoint, data=payload)
        if payload is not None:
            req.add_header("Content-Type", content_type)
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            raise RuntimeError(f"ComfyUI HTTP {exc.code}: {detail}") from exc

    def json(self, endpoint, payload=None):
        return json.loads(self.request(endpoint, payload))

    def inspect(self):
        info = self.json("/object_info")
        required = {"LoadImage", "UpscaleModelLoader", "ImageUpscaleWithModel", "ImageScale", "SaveImage"}
        missing = required - set(info)
        if missing:
            raise ValueError("ComfyUI 缺少节点：" + ", ".join(sorted(missing)))
        specification = info["UpscaleModelLoader"]["input"]["required"]["model_name"]
        # 旧版 API 直接返回 [[模型名...]]；新版 V3 返回 ["COMBO", {"options": [...]}]。
        # 不能把字符串 "COMBO" 当成模型列表，否则 UI 与实际任务均无法选中权重。
        if isinstance(specification[0], list):
            models = specification[0]
        elif specification[0] == "COMBO" and len(specification) > 1:
            models = specification[1].get("options", [])
        else:
            raise ValueError("无法识别 ComfyUI 的模型列表格式，请核对 /object_info")
        if not isinstance(models, list) or not all(isinstance(name, str) for name in models):
            raise ValueError("ComfyUI 返回的模型列表不是字符串数组")
        stats = self.json("/system_stats")
        return models, stats.get("system", {}).get("comfyui_version", "unknown")

    def upload(self, image: Image.Image):
        boundary = "----Manju" + uuid.uuid4().hex
        name = "manju_" + uuid.uuid4().hex + ".png"
        # 只上传规范化后的当前图片；不携带原图路径、档案和原始元数据。
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"'
                '\r\nContent-Type: image/png\r\n\r\n').encode() + png_bytes(image)
        body += f"\r\n--{boundary}--\r\n".encode()
        result = json.loads(self.request("/upload/image", body, "multipart/form-data; boundary=" + boundary))
        return "/".join(x for x in (result.get("subfolder", ""), result["name"]) if x)

    def upscale(self, image, model, size, timeout, audit, announce):
        name = self.upload(image)
        graph = build_workflow(name, model, *size, "manju/" + uuid.uuid4().hex)
        atomic_json(audit, graph)
        # 提交不自动重试：网络超时也可能意味着任务已被服务器接收。
        try:
            result = self.json("/prompt", {"prompt": graph, "client_id": uuid.uuid4().hex})
        except (TimeoutError, OSError) as exc:
            raise RuntimeError("提交结果未知，请先检查 ComfyUI 队列；程序未自动重复提交") from exc
        if result.get("node_errors") or not result.get("prompt_id"):
            raise RuntimeError("工作流校验失败：" + json.dumps(result, ensure_ascii=False))
        prompt_id = result["prompt_id"]
        announce("ComfyUI 任务：" + prompt_id)
        atomic_json(audit.with_suffix(".task.json"), {"prompt_id": prompt_id, "server": self.server})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            history = self.json("/history/" + urllib.parse.quote(prompt_id, safe=""))
            item = history.get(prompt_id)
            if item:
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError("ComfyUI 执行失败：" + json.dumps(status.get("messages", []), ensure_ascii=False))
                images = item.get("outputs", {}).get("5", {}).get("images", [])
                if images:
                    entry = images[0]
                    raw = self.request("/view?" + urllib.parse.urlencode({
                        "filename": entry["filename"], "subfolder": entry.get("subfolder", ""),
                        "type": entry.get("type", "output")}))
                    output = read_image(raw)
                    if output.size != size:
                        raise ValueError(f"服务器输出尺寸 {output.size} 与目标 {size} 不符")
                    # 旧版 ComfyUI 可能丢弃 alpha；独立按原图 alpha 缩放以保持透明度。
                    if "A" in image.getbands():
                        output = output.convert("RGB")
                        output.putalpha(image.getchannel("A").resize(size, Image.Resampling.LANCZOS))
                    return output
                if status.get("completed"):
                    raise RuntimeError("任务已完成但 SaveImage 节点没有返回图片")
            time.sleep(0.5)
        raise TimeoutError(f"任务等待超时（{prompt_id}）；服务器可能仍在执行，请先检查队列")


def validate_paths(input_dir: Path, output_dir: Path):
    if not input_dir.is_dir():
        raise ValueError("输入目录不存在")
    if (input_dir == output_dir or input_dir in output_dir.parents
            or output_dir in input_dir.parents):
        raise ValueError("输入与输出目录必须相互独立，不能相同或互相包含")


def scan_files(directory: Path):
    # 不跟随符号链接，不读取输出目录；中文路径及子目录均保留。
    result = []
    for root, dirs, files in os.walk(directory, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(root) / d).is_symlink())
        for name in sorted(files):
            path = Path(root) / name
            if path.suffix.lower() in EXTENSIONS and not path.is_symlink():
                result.append(path)
    return result


def write_report(output: Path, records, settings, server_version, stopped):
    summary = {"version": VERSION, "settings": asdict(settings), "server_version": server_version,
               "stopped": stopped, "total": len(records),
               "success": sum(r["status"] == "success" for r in records),
               "reused": sum(r["status"] == "reused" for r in records),
               "failed": sum(r["status"] == "failed" for r in records), "items": records}
    atomic_json(output / "report.json", summary)
    cards = []
    for row in records:
        escape = lambda v: html.escape(str(v), quote=True)
        preview = row.get("preview")
        image = f'<img src="{urllib.parse.quote(preview)}" loading="lazy" alt="左：插值基线；右：处理结果">' if preview else ""
        link = ""
        if row.get("output"):
            link = f'<a href="{urllib.parse.quote(row["output"])}">打开完整输出</a>'
        cards.append(f'<article><h2>{escape(row["input"])}</h2><p>{escape(row["status"])} · '
                     f'{escape(row.get("route", ""))} · {escape(row.get("elapsed_seconds", ""))} 秒</p>'
                     f'{image}<p>{escape(row.get("error", "待人工验收：人物、线条、风格、伪影"))}</p>{link}</article>')
    document = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>漫剧超分处理报告</title>
<style>body{font:16px/1.7 "Microsoft YaHei",sans-serif;background:#f4f6fa;color:#182337;
max-width:1120px;margin:40px auto;padding:0 24px}h1{font-size:30px}article{background:white;
padding:24px;margin:20px 0;border:1px solid #dce2ec;border-radius:12px}h2{font-size:18px;
overflow-wrap:anywhere}img{display:block;width:100%;height:auto}a{color:#2458cb}small{color:#536176}</style>
<h1>漫剧分镜超分处理报告</h1><p>左侧是普通插值基线，右侧是本次处理结果。缩略图只用于浏览，
请打开原尺寸文件检查。执行成功不等于画质合格，本报告不自动判断人物和风格一致性。</p>'''
    document += (f'<p>成功 {summary["success"]} · 复用 {summary["reused"]} · 失败 {summary["failed"]}'
                 f' · 模式 {html.escape(settings.mode)} · 已停止 {stopped}</p>')
    document += "".join(cards) + "</html>"
    atomic_bytes(output / "report.html", document.encode("utf-8"))
    return summary


def run_batch(input_dir, output_dir, settings: Settings, notify=print, stop=None):
    settings.validate()
    source = Path(input_dir).resolve()
    output = Path(output_dir).resolve()
    validate_paths(source, output)
    files = scan_files(source)
    if not files:
        raise ValueError("输入目录没有支持的图片")
    stop = stop or threading.Event()
    output.mkdir(parents=True, exist_ok=True)
    lock = output / ".run.lock"
    try:
        lock_handle = lock.open("x", encoding="utf-8")
    except FileExistsError as exc:
        raise RuntimeError("输出目录已锁定。确认没有处理任务后，可手动删除 .run.lock") from exc
    records = []
    server_version = "not_used"
    try:
        with lock_handle:
            lock_handle.write(str(os.getpid()))
        client = None
        if settings.mode != "interpolation":
            client = ComfyClient(settings.server)
            models, server_version = client.inspect()
            if settings.model not in models:
                raise ValueError("所选模型未出现在服务器列表，请刷新模型")
            if settings.resume and not settings.model_revision:
                notify("未填写模型版本标记：AI 结果本次不会复用，避免误用同名新权重的旧结果。")
        signature_settings = asdict(settings)
        for key in ("resume", "timeout"):
            signature_settings.pop(key)
        for index, path in enumerate(files, 1):
            if stop.is_set():
                break
            relative = path.relative_to(source).as_posix()
            key = digest(relative.encode("utf-8"))
            record_path = output / "records" / (key + ".json")
            destination = output / "images" / (relative + ".png")
            row = {"input": relative, "status": "failed", "visual_review": "pending"}
            started = time.monotonic()
            notify(f"[{index}/{len(files)}] {relative}")
            try:
                raw = path.read_bytes()
                image = read_image(raw)
                size = ((settings.output_width, settings.output_height) if settings.output_width
                        else target_size(*image.size, settings.long_edge))
                route = choose_route(image.size, settings)
                row.update(input_size=list(image.size), output_size=list(size), route=route)
                # 指纹包含内容、输入根目录、参数、程序版本及 ComfyUI 版本。
                fingerprint = digest(json.dumps({"input_sha256": digest(raw), "root": str(source),
                    "settings": signature_settings, "version": VERSION, "server_version": server_version},
                    sort_keys=True).encode("utf-8"))
                can_resume = settings.resume and (route != "ai" or bool(settings.model_revision))
                previous = None
                if can_resume and record_path.exists() and destination.exists():
                    try:
                        previous = json.loads(record_path.read_text("utf-8"))
                    except (ValueError, OSError):
                        previous = None
                if (previous and previous.get("fingerprint") == fingerprint
                        and previous.get("output_sha256") == digest(destination.read_bytes())):
                    row = {**previous, "status": "reused", "elapsed_seconds": 0,
                           "visual_review": "pending"}
                    notify("  指纹与输出校验通过，复用已有结果")
                else:
                    baseline = image.resize(size, Image.Resampling.LANCZOS)
                    if route == "ai":
                        audit = output / "workflows" / (key + ".json")
                        result = client.upscale(image, settings.model, size, settings.timeout, audit, notify)
                    else:
                        result = baseline.copy()
                    encoded = png_bytes(result)
                    atomic_bytes(destination, encoded)
                    # 报告采用相同显示尺寸的左右对比；不计算伪造的质量评分。
                    preview_size = target_size(*size, min(640, max(size)))
                    left = baseline.convert("RGBA").resize(preview_size, Image.Resampling.LANCZOS)
                    right = result.convert("RGBA").resize(preview_size, Image.Resampling.LANCZOS)
                    preview = Image.new("RGBA", (preview_size[0] * 2, preview_size[1]), "white")
                    preview.paste(left, (0, 0), left)
                    preview.paste(right, (preview_size[0], 0), right)
                    preview_path = output / "previews" / (key + ".png")
                    atomic_bytes(preview_path, png_bytes(preview.convert("RGB")))
                    row.update(status="success", fingerprint=fingerprint, output_sha256=digest(encoded),
                               output=destination.relative_to(output).as_posix(),
                               preview=preview_path.relative_to(output).as_posix(),
                               model=settings.model if route == "ai" else "none",
                               model_revision=settings.model_revision if route == "ai" else "",
                               elapsed_seconds=round(time.monotonic() - started, 3))
                    atomic_json(record_path, row)
                    notify(f"  完成：{route} → {size[0]}×{size[1]}")
            except Exception as exc:
                row.update(error=str(exc), elapsed_seconds=round(time.monotonic() - started, 3))
                notify("  失败：" + str(exc))
            records.append(row)
            # 逐张持久化，正常停止后可以续跑；日志只追加，不覆盖历史。
            with (output / "events.jsonl").open("a", encoding="utf-8") as journal:
                journal.write(json.dumps({"time": time.time(), **row}, ensure_ascii=False) + "\n")
            write_report(output, records, settings, server_version, stop.is_set())
        summary = write_report(output, records, settings, server_version, stop.is_set())
        notify(f"本次结束：成功 {summary['success']}，复用 {summary['reused']}，失败 {summary['failed']}")
        return summary
    finally:
        lock.unlink(missing_ok=True)
