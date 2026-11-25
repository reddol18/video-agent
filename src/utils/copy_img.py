from PIL import Image
import argparse
import os

def pixelate(img, pixel_size=16, palette_size=16):
    # 이미지 축소 후 확대 (픽셀화)
    small = img.resize(
        (img.width // pixel_size, img.height // pixel_size),
        resample=Image.NEAREST
    )
    result = small.resize(img.size, Image.NEAREST)
    # 색상 팔레트 제한
    result = result.convert('P', palette=Image.ADAPTIVE, colors=palette_size).convert('RGB')
    return result

def main():
    parser = argparse.ArgumentParser(description="이미지를 픽셀아트로 변환")
    parser.add_argument("--input", type=str, required=True, help="입력 이미지 파일명")
    parser.add_argument("--output", type=str, default="outputs", help="출력 폴더명")
    parser.add_argument("--pixel_size", type=int, default=2, help="픽셀 크기")
    parser.add_argument("--palette_size", type=int, default=16, help="색상 개수")
    args = parser.parse_args()

    base = os.path.basename(args.input)
    name, ext = os.path.splitext(base)
    output_name = f"{name}_pixel{ext}"

    img = Image.open(args.input).convert("RGB")
    pixel_img = pixelate(img, args.pixel_size, args.palette_size)

    os.makedirs(args.output, exist_ok=True)
    output_path = os.path.join(args.output, output_name)
    pixel_img.save(output_path)
    print(f"Saved to {output_path}")

if __name__ == "__main__":
    main()