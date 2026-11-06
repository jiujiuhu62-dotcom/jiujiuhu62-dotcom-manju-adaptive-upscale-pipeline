"""生成用于尺寸、线条和批处理检查的程序测试图，不代表真实漫剧画质验证。"""
from pathlib import Path
from PIL import Image, ImageDraw


def main():
    base = Path(__file__).resolve().parent / "demo" / "input"
    base.mkdir(parents=True, exist_ok=True)
    for name, size in [("01_landscape.png", (320, 180)), ("02_portrait.png", (360, 640)),
                       ("03_mid.png", (1280, 720)), ("04_large.png", (2200, 1238))]:
        image = Image.new("RGB", size, "#e8edf6")
        draw = ImageDraw.Draw(image)
        w, h = size
        for i in range(12):
            x = int(w * i / 12)
            draw.rectangle((x, int(h * .6), int(w * (i + 1) / 12), h), fill=(25 + 17*i, 70 + 9*i, 130))
        draw.ellipse((w*.25, h*.12, w*.65, h*.72), fill="#f5cfb4", outline="#26364b", width=max(1, w//160))
        draw.line((w*.34, h*.4, w*.4, h*.4), fill="#23334a", width=max(1, w//160))
        draw.line((w*.51, h*.4, w*.57, h*.4), fill="#23334a", width=max(1, w//160))
        for i in range(8):
            draw.line((w*.72, h*(.15+.045*i), w*.94, h*(.15+.045*i)), fill="#26364b", width=i+1)
        draw.text((10, 10), f"SYNTHETIC TEST / {w}x{h}", fill="#182337")
        image.save(base / name)
    print(base)


if __name__ == "__main__":
    main()
