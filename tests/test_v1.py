"""MCP、退化、参考指标与精确尺寸测试。临时目录限定在测试目录内。"""
from dataclasses import asdict
from pathlib import Path
import json
import shutil
import sys
import threading
import time
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from engine import ComfyClient,Settings,png_bytes,run_batch
from image_eval import degrade_image,reference_metrics
from mcp_client import MCPClient
from test_engine import FakeServer


class V1Tests(unittest.TestCase):
    def setUp(self):
        self.root=Path(__file__).resolve().parent/('case_'+uuid.uuid4().hex)
        self.root.mkdir()
        (self.root/'input').mkdir()
        self.image=self.root/'input'/'test.png'
        # 非常量样本，避免只有平色才能通过的指标测试。
        im=Image.new('RGB',(96,64))
        im.putdata([(x*2,y*3,(x+y)%256) for y in range(64) for x in range(96)])
        im.save(self.image)

    def tearDown(self):
        assert self.root.resolve().is_relative_to(Path(__file__).resolve().parent)
        shutil.rmtree(self.root)

    def test_degradation_reproducible_and_no_source_change(self):
        original=self.image.read_bytes()
        path=self.root/'private_tests'/'low.png'
        record=degrade_image(self.image,path)
        first=path.read_bytes()
        record2=degrade_image(self.image,path)
        self.assertEqual(first,path.read_bytes())
        self.assertEqual(original,self.image.read_bytes())
        self.assertEqual(record,record2)
        self.assertEqual(record['low_size'],[24,16])

    def test_degradation_rejects_overwrite(self):
        with self.assertRaises(ValueError):
            degrade_image(self.image,self.image)

    def test_metrics_identity(self):
        with Image.open(self.image) as im:
            values=reference_metrics(im,im)
        self.assertTrue(values['psnr_infinite'])
        self.assertIsNone(values['psnr_rgb_db'])
        self.assertAlmostEqual(values['ssim_y'],1,places=10)

    def test_metrics_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            reference_metrics(Image.new('RGB',(64,64)),Image.new('RGB',(65,64)))

    def test_psnr_known_constant_difference(self):
        a,b=Image.new('RGB',(64,64),(100,100,100)),Image.new('RGB',(64,64),(110,110,110))
        values=reference_metrics(a,b)
        self.assertAlmostEqual(values['mse_rgb'],100)
        self.assertAlmostEqual(values['psnr_rgb_db'],28.130803608679106,places=10)

    def test_ssim_matches_independent_skimage_when_available(self):
        try:
            import numpy as np
            from skimage.metrics import structural_similarity
        except ImportError:
            self.skipTest('独立交叉核验库未安装，非运行依赖')
        with Image.open(self.image) as im:
            altered=im.filter(__import__('PIL.ImageFilter',fromlist=['GaussianBlur']).GaussianBlur(1))
            ours=reference_metrics(im,altered,border=4)['ssim_y']
            a=np.asarray(im,dtype=float)[4:-4,4:-4]@np.array([.299,.587,.114])
            b=np.asarray(altered,dtype=float)[4:-4,4:-4]@np.array([.299,.587,.114])
            other=structural_similarity(a,b,data_range=255,gaussian_weights=True,sigma=1.5,
                                       use_sample_covariance=False)
        self.assertAlmostEqual(ours,other,places=8)

    def test_exact_reference_dimensions(self):
        result=run_batch(self.root/'input',self.root/'results',Settings(mode='interpolation',
            output_width=101,output_height=67),notify=lambda _:None)
        self.assertEqual(result['items'][0]['output_size'],[101,67])

    def test_dimension_types_and_pair(self):
        for settings in [Settings(long_edge=960.5),Settings(output_width=64),Settings(resume='true')]:
            with self.assertRaises(ValueError):
                settings.validate()

    def test_mcp_lifecycle_list_degrade_and_offline_batch(self):
        trace=self.root/'results'/'trace.jsonl'
        with MCPClient(workspace=self.root,trace=trace,notify=lambda _:None) as client:
            tools=client.request('tools/list')['tools']
            self.assertEqual({t['name'] for t in tools},{'get_status','process_batch','degrade_image'})
            value=client.call('degrade_image',{'source':str(self.image),
                'destination':str(self.root/'private_tests'/'low.png')})
            self.assertEqual(value['low_size'],[24,16])
            report=client.call('process_batch',{'input_dir':str(self.root/'input'),
                'output_dir':str(self.root/'results'/'batch'),
                'settings':asdict(Settings(mode='interpolation',long_edge=128))})
            self.assertEqual(report['success'],1)
        entries=[json.loads(line) for line in trace.read_text('utf-8').splitlines()]
        self.assertEqual(entries[0]['message']['method'],'initialize')
        self.assertNotIn('base64',trace.read_text('utf-8'))
        self.assertEqual(client.process.returncode,0)

    def test_mcp_rejects_unauthorized_output(self):
        with MCPClient(workspace=self.root,notify=lambda _:None) as client:
            with self.assertRaisesRegex(RuntimeError,'授权范围'):
                client.call('degrade_image',{'source':str(self.image),'destination':str(self.root/'escape.png')})
        self.assertFalse((self.root/'escape.png').exists())

    def test_mcp_ai_with_local_mock_comfy(self):
        with FakeServer() as server:
            with MCPClient(workspace=self.root,notify=lambda _:None) as client:
                result=client.call('process_batch',{'input_dir':str(self.root/'input'),
                    'output_dir':str(self.root/'results'/'batch'),
                    'settings':asdict(Settings(server=server.address,model='test.pth',long_edge=128))})
                self.assertEqual(result['success'],1)
                self.assertEqual(len(server.jobs),1)

    def test_unknown_mcp_tool_is_error(self):
        with MCPClient(workspace=self.root,notify=lambda _:None) as client:
            with self.assertRaisesRegex(RuntimeError,'未知工具'):
                client.call('upload_cloud',{})


if __name__=='__main__':
    unittest.main()
