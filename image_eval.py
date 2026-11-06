"""确定性退化与有参考图评价。用户明确授权本地降采样，不调用生成服务。"""
from pathlib import Path
import math
import numpy as np
from PIL import Image, ImageFilter
from engine import atomic_bytes, atomic_json, digest, png_bytes, read_image


def degrade_image(source, destination, factor=4, blur_radius=.6):
    """先按 1/factor 用 Lanczos 降采样，再对低分辨率图做 GaussianBlur。

    blur_radius 的单位为低清图像像素。无随机数、无 JPEG、无额外噪声。
    参数是已知的人工退化，不等同于真实低清素材分布。
    """
    if factor not in {2,3,4,8} or not math.isfinite(blur_radius) or not 0 <= blur_radius <= 5:
        raise ValueError('factor 仅支持 2/3/4/8，blur_radius 在 0—5 内')
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if source == destination:
        raise ValueError('不能覆盖原图')
    raw = source.read_bytes()
    image = read_image(raw)
    size = (max(1,round(image.width/factor)),max(1,round(image.height/factor)))
    low = image.resize(size,Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(blur_radius))
    encoded = png_bytes(low)
    atomic_bytes(destination,encoded)
    record = {'source_sha256':digest(raw),'reference_size':list(image.size),'low_size':list(size),
              'factor':factor,'blur_radius_low_pixels':blur_radius,'order':['Lanczos downsample','GaussianBlur'],
              'low_sha256':digest(encoded),'source_mode':image.mode,'random_noise':False,
              'jpeg_compression':False}
    atomic_json(destination.with_suffix('.degradation.json'),record)
    return record


def _gaussian_valid(array):
    """11×11、sigma=1.5 的可分离高斯窗，只保留完整窗口位置。"""
    coordinates = np.arange(-5,6,dtype=np.float64)
    kernel = np.exp(-(coordinates**2)/(2*1.5**2))
    kernel /= kernel.sum()
    horizontal = np.zeros((array.shape[0],array.shape[1]-10),dtype=np.float64)
    for offset,weight in enumerate(kernel):
        horizontal += weight*array[:,offset:offset+horizontal.shape[1]]
    vertical = np.zeros((array.shape[0]-10,horizontal.shape[1]),dtype=np.float64)
    for offset,weight in enumerate(kernel):
        vertical += weight*horizontal[offset:offset+vertical.shape[0],:]
    return vertical


def reference_metrics(reference, result, border=4):
    """PSNR-RGB 与 SSIM-Y；均与同尺寸高清参考比较，不能代替主观验收。

    SSIM: Y=0.299R+0.587G+0.114B，0—255；11×11 高斯窗 sigma1.5，
    population covariance，C1=(.01*255)^2、C2=(.03*255)^2；无 padding。
    边缘先裁 border 像素，再做有效窗统计。该定义完整记录于报告。
    """
    if reference.size != result.size:
        raise ValueError('评价前必须对齐同源参考图与结果的尺寸')
    if border < 0 or min(reference.size) <= 2*border+10:
        raise ValueError('图片过小或裁边参数不合理')
    a = np.asarray(reference.convert('RGB'),dtype=np.float64)
    b = np.asarray(result.convert('RGB'),dtype=np.float64)
    if border:
        a,b = a[border:-border,border:-border],b[border:-border,border:-border]
    mse = float(np.mean((a-b)**2))
    psnr = None if mse == 0 else float(10*math.log10(255**2/mse))
    weights = np.array([.299,.587,.114])
    x,y = a@weights,b@weights
    mu_x,mu_y = _gaussian_valid(x),_gaussian_valid(y)
    variance_x = np.maximum(0,_gaussian_valid(x*x)-mu_x*mu_x)
    variance_y = np.maximum(0,_gaussian_valid(y*y)-mu_y*mu_y)
    covariance = _gaussian_valid(x*y)-mu_x*mu_y
    c1,c2 = (.01*255)**2,(.03*255)**2
    ssim = ((2*mu_x*mu_y+c1)*(2*covariance+c2) /
            ((mu_x**2+mu_y**2+c1)*(variance_x+variance_y+c2)))
    return {'psnr_rgb_db':psnr,'psnr_infinite':mse==0,'ssim_y':float(ssim.mean()),
            'mse_rgb':mse,'border':border,'ssim_window':'11x11 Gaussian sigma=1.5 valid population',
            'luminance':'0.299R+0.587G+0.114B (0..255)'}
