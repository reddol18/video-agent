/**
 * MediaRecorder가 생성한 webm은 Segment Info의 Duration 필드가 비어 있어
 * 일부 플레이어에서 길이가 잘못 표시된다. (예: 30초 녹화가 19초로 표시)
 *
 * 이 유틸은 webm(Matroska/EBML) 컨테이너의 첫 Segment를 찾아
 * Info > Duration(0x4489) 요소를 실제 경과시간으로 채워 넣은 새 Blob을 반환한다.
 *
 * 구현은 의도적으로 최소한이다 — MediaRecorder 출력만 다루며, 일반 mkv는 지원하지 않는다.
 * 알려진 한계:
 *   - Segment Info에 빈 Duration 자리가 이미 예약되어 있어야 한다 (Chrome MediaRecorder는 8바이트 0으로 채워 둠 — 일반적)
 *   - 자리가 없으면 그대로 반환 (worst case: 변경 없음)
 */

const ID_SEGMENT = 0x18538067;
const ID_INFO = 0x1549a966;
const ID_TIMECODE_SCALE = 0x2ad7b1;
const ID_DURATION = 0x4489;

function readVInt(buf: Uint8Array, offset: number): { value: number; size: number } {
  const first = buf[offset];
  if (first === 0) throw new Error("invalid VINT");
  let mask = 0x80;
  let size = 1;
  while ((first & mask) === 0) {
    mask >>= 1;
    size++;
    if (size > 8) throw new Error("VINT too large");
  }
  let value = first & (mask - 1);
  for (let i = 1; i < size; i++) value = value * 256 + buf[offset + i];
  return { value, size };
}

function readId(buf: Uint8Array, offset: number): { id: number; size: number } {
  const first = buf[offset];
  if (first === 0) throw new Error("invalid ID");
  let mask = 0x80;
  let size = 1;
  while ((first & mask) === 0) {
    mask >>= 1;
    size++;
    if (size > 4) throw new Error("ID too large");
  }
  let id = 0;
  for (let i = 0; i < size; i++) id = id * 256 + buf[offset + i];
  return { id, size };
}

function findChild(buf: Uint8Array, start: number, end: number, targetId: number): { offset: number; dataOffset: number; dataSize: number } | null {
  let p = start;
  while (p < end) {
    const { id, size: idSize } = readId(buf, p);
    const { value: size, size: sizeSize } = readVInt(buf, p + idSize);
    const dataOffset = p + idSize + sizeSize;
    if (id === targetId) return { offset: p, dataOffset, dataSize: size };
    p = dataOffset + size;
  }
  return null;
}

function writeFloat64BE(view: DataView, offset: number, value: number) {
  view.setFloat64(offset, value, false);
}

export async function fixWebmDuration(blob: Blob, durationMs: number): Promise<Blob> {
  if (!isFinite(durationMs) || durationMs <= 0) return blob;
  try {
    const ab = await blob.arrayBuffer();
    const buf = new Uint8Array(ab);

    let p = 0;
    while (p < buf.length) {
      const { id, size: idSize } = readId(buf, p);
      const { value: size, size: sizeSize } = readVInt(buf, p + idSize);
      const dataOffset = p + idSize + sizeSize;
      if (id === ID_SEGMENT) {
        const segEnd = size === Number.MAX_SAFE_INTEGER ? buf.length : dataOffset + size;
        const info = findChild(buf, dataOffset, segEnd, ID_INFO);
        if (!info) return blob;
        const infoEnd = info.dataOffset + info.dataSize;
        const tcs = findChild(buf, info.dataOffset, infoEnd, ID_TIMECODE_SCALE);
        let timecodeScale = 1_000_000;
        if (tcs) {
          let v = 0;
          for (let i = 0; i < tcs.dataSize; i++) v = v * 256 + buf[tcs.dataOffset + i];
          timecodeScale = v;
        }
        const dur = findChild(buf, info.dataOffset, infoEnd, ID_DURATION);
        if (!dur || dur.dataSize !== 8) return blob;

        const durationNs = durationMs * 1_000_000;
        const durationScaled = durationNs / timecodeScale;

        const out = new Uint8Array(ab.slice(0));
        const view = new DataView(out.buffer, dur.dataOffset, 8);
        writeFloat64BE(view, 0, durationScaled);
        return new Blob([out], { type: blob.type });
      }
      p = dataOffset + size;
    }
    return blob;
  } catch {
    return blob;
  }
}
