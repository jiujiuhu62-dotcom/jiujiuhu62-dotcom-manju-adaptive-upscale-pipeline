"""功能与协议回归测试；模拟服务器只验证接入，不证明真实 AI 画质。"""
import io
import json
from pathlib import Path
import sys
import tempfile
import shutil
import uuid
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image
from engine import (ComfyClient, Settings, atomic_json, build_workflow, choose_route,
                    png_bytes, read_image, run_batch, target_size, validate_paths)


class FakeServer:
    def __enter__(self):
        owner = self
        self.jobs = {}
        self.uploads = []
        self.fail = False
        self.hang = False
        self.malformed = False
        self.new_schema = False
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, obj):
                data = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/object_info":
                    self.reply({"LoadImage": {}, "ImageUpscaleWithModel": {}, "ImageScale": {},
                        "SaveImage": {}, "UpscaleModelLoader": {"input": {"required": {
                            "model_name": ["COMBO", {"options": ["test.pth"]}] if owner.new_schema else [["test.pth"]]}}}})
                elif self.path == "/system_stats":
                    self.reply({"system": {"comfyui_version": "mock-1"}})
                elif self.path.startswith("/history/"):
                    job_id = self.path.rsplit("/", 1)[1]
                    if owner.hang:
                        self.reply({})
                    elif owner.fail:
                        self.reply({job_id: {"status": {"status_str": "error", "messages": ["test OOM"]}}})
                    else:
                        self.reply({job_id: {"status": {"completed": True, "status_str": "success"},
                            "outputs": {"5": {"images": [{"filename": job_id + ".png", "type": "output"}]}}}})
                elif self.path.startswith("/view?"):
                    graph = list(owner.jobs.values())[-1]
                    params = graph["4"]["inputs"]
                    size = (params["width"], params["height"])
                    if owner.malformed:
                        size = (1, 1)
                    self.reply(png_bytes(Image.new("RGB", size, "navy")))
                else:
                    self.send_error(404)

            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                if self.path == "/upload/image":
                    owner.uploads.append(data)
                    self.reply({"name": "test.png", "subfolder": "", "type": "input"})
                elif self.path == "/prompt":
                    graph = json.loads(data)["prompt"]
                    job_id = str(len(owner.jobs) + 1)
                    owner.jobs[job_id] = graph
                    self.reply({"prompt_id": job_id, "node_errors": {}})
                else:
                    self.send_error(404)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"http://127.0.0.1:{self.server.server_port}"
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join()


class EngineTests(unittest.TestCase):
    def setUp(self):
        # Windows 沙箱下，tempfile 的 0o700 ACL 可能阻止受限令牌访问；
        # 使用父目录继承权限，在测试目录内建立唯一的临时目录。
        self.root = Path(__file__).resolve().parent / ("case_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.source = self.root / "输入"
        self.output = self.root / "输出"
        self.source.mkdir()

    def tearDown(self):
        assert self.root.resolve().is_relative_to(Path(__file__).resolve().parent)
        shutil.rmtree(self.root)

    def image(self, relative="角色.png", size=(32, 16), color="red", mode="RGB"):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new(mode, size, color).save(path)
        return path

    def run_batch(self, **options):
        return run_batch(self.source, self.output, Settings(long_edge=64, **options), notify=lambda _: None)

    def test_proportions_and_no_zero_side(self):
        self.assertEqual(target_size(320, 180, 1920), (1920, 1080))
        self.assertEqual(target_size(1, 100000, 64), (1, 64))

    def test_route_does_not_claim_visual_quality(self):
        settings = Settings(long_edge=64)
        self.assertEqual(choose_route((128, 64), settings), "resize_only")
        self.assertEqual(choose_route((32, 16), settings), "ai")

    def test_reject_overlapping_directories(self):
        for path in [self.source, self.source / "nested", self.root]:
            with self.assertRaises(ValueError):
                validate_paths(self.source, path)

    def test_interpolation_batch_corrupt_and_same_stem(self):
        self.image("子目录/a.png")
        self.image("子目录/a.jpg")
        (self.source / "broken.png").write_bytes(b"broken")
        result = self.run_batch(mode="interpolation")
        self.assertEqual((result["success"], result["failed"]), (2, 1))
        self.assertTrue((self.output / "images/子目录/a.png.png").exists())
        self.assertTrue((self.output / "images/子目录/a.jpg.png").exists())
        self.assertTrue((self.output / "report.html").exists())
        self.assertFalse((self.output / ".run.lock").exists())

    def test_resume_and_changed_content(self):
        path = self.image()
        self.run_batch(mode="interpolation")
        self.assertEqual(self.run_batch(mode="interpolation")["reused"], 1)
        Image.new("RGB", (32, 16), "blue").save(path)
        self.assertEqual(self.run_batch(mode="interpolation")["success"], 1)

    def test_output_tamper_invalidates_resume(self):
        self.image()
        self.run_batch(mode="interpolation")
        (self.output / "images/角色.png.png").write_bytes(b"damaged")
        self.assertEqual(self.run_batch(mode="interpolation")["success"], 1)

    def test_parameter_change_invalidates_resume(self):
        self.image()
        self.run_batch(mode="interpolation")
        result = run_batch(self.source, self.output, Settings(mode="interpolation", long_edge=96), notify=lambda _: None)
        self.assertEqual(result["success"], 1)
        with Image.open(self.output / "images/角色.png.png") as output:
            self.assertEqual(output.size, (96, 48))

    def test_preserves_alpha(self):
        self.image(color=(100, 30, 90, 77), mode="RGBA")
        self.run_batch(mode="interpolation")
        with Image.open(self.output / "images/角色.png.png") as result:
            self.assertEqual(result.mode, "RGBA")
            self.assertEqual(result.getpixel((0, 0))[3], 77)

    def test_exif_orientation(self):
        image = Image.new("RGB", (10, 20))
        exif = image.getexif()
        exif[274] = 6
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", exif=exif)
        self.assertEqual(read_image(buffer.getvalue()).size, (20, 10))

    def test_animation_is_rejected(self):
        buffer = io.BytesIO()
        Image.new("RGB", (10, 10), "red").save(buffer, "WEBP", save_all=True,
            append_images=[Image.new("RGB", (10, 10), "blue")], duration=100)
        with self.assertRaises(ValueError):
            read_image(buffer.getvalue())

    def test_stop_between_images(self):
        self.image("a.png")
        self.image("b.png")
        stop = threading.Event()
        def notify(message):
            if "完成：" in message:
                stop.set()
        result = run_batch(self.source, self.output, Settings(mode="interpolation", long_edge=64), notify, stop)
        self.assertEqual(result["success"], 1)
        self.assertTrue(result["stopped"])

    def test_existing_lock_is_not_deleted(self):
        self.image()
        self.output.mkdir()
        lock = self.output / ".run.lock"
        lock.write_text("other task")
        with self.assertRaises(RuntimeError):
            self.run_batch(mode="interpolation")
        self.assertTrue(lock.exists())

    def test_local_server_only(self):
        for address in ["https://example.com", "http://127.0.0.1.evil.test:8188", "http://user@localhost:8188", "http://localhost:8188/path"]:
            with self.assertRaises(ValueError):
                ComfyClient(address)

    def test_real_http_protocol_and_resume(self):
        self.image(color=(100, 40, 20, 80), mode="RGBA")
        with FakeServer() as server:
            options = dict(server=server.address, model="test.pth", model_revision="test-v1")
            result = self.run_batch(**options)
            self.assertEqual(result["success"], 1)
            self.assertEqual(len(server.uploads), 1)
            self.assertIn(b"Content-Type: image/png", server.uploads[0])
            self.assertEqual(server.jobs["1"]["3"]["class_type"], "ImageUpscaleWithModel")
            self.assertEqual(self.run_batch(**options)["reused"], 1)
            self.assertEqual(len(server.jobs), 1)
            self.assertEqual(self.run_batch(**{**options, "model_revision": "test-v2"})["success"], 1)
            with Image.open(self.output / "images/角色.png.png") as image:
                self.assertEqual(image.getpixel((0, 0))[3], 80)

    def test_new_v3_model_list_schema(self):
        with FakeServer() as server:
            server.new_schema = True
            models, _ = ComfyClient(server.address).inspect()
            self.assertEqual(models, ["test.pth"])

    def test_no_revision_does_not_reuse_ai(self):
        self.image()
        with FakeServer() as server:
            options = dict(server=server.address, model="test.pth")
            self.run_batch(**options)
            self.assertEqual(self.run_batch(**options)["success"], 1)
            self.assertEqual(len(server.jobs), 2)

    def test_ai_failure_is_never_silent_interpolation(self):
        self.image()
        with FakeServer() as server:
            server.fail = True
            result = self.run_batch(server=server.address, model="test.pth")
            self.assertEqual(result["failed"], 1)
            self.assertFalse((self.output / "images/角色.png.png").exists())

    def test_adaptive_large_image_never_submits_ai(self):
        self.image(size=(128, 64))
        with FakeServer() as server:
            result = self.run_batch(server=server.address, model="test.pth")
            self.assertEqual(result["success"], 1)
            self.assertEqual(result["items"][0]["route"], "resize_only")
            self.assertEqual(len(server.jobs), 0)
            self.assertEqual(len(server.uploads), 0)

    def test_invalid_server_dimensions_rejected(self):
        self.image()
        with FakeServer() as server:
            server.malformed = True
            result = self.run_batch(server=server.address, model="test.pth")
            self.assertEqual(result["failed"], 1)

    def test_timeout_records_failure(self):
        self.image()
        with FakeServer() as server:
            server.hang = True
            result = self.run_batch(server=server.address, model="test.pth", timeout=0.1)
            self.assertEqual(result["failed"], 1)
            self.assertIn("超时", result["items"][0]["error"])


if __name__ == "__main__":
    unittest.main()
