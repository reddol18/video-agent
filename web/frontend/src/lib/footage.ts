// footage 자산 → 씬 footage 객체 변환.
// 이미지 확장자(.png/.jpg/.jpeg/.gif/.webp)는 자동으로 type=kenburns + image 키,
// 그 외(영상)는 type=clip + file 키.
// Editor 드롭 핸들러와 동일 규칙 — 자동/수동 매칭 모두에서 일관되게 사용한다.

const IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp"];

export function isImageFilename(filename: string): boolean {
  const lower = filename.toLowerCase();
  return IMAGE_EXTS.some((ext) => lower.endsWith(ext));
}

export function makeFootageFromFilename(filename: string):
  | { type: "kenburns"; image: string }
  | { type: "clip"; file: string } {
  return isImageFilename(filename)
    ? { type: "kenburns", image: filename }
    : { type: "clip", file: filename };
}
