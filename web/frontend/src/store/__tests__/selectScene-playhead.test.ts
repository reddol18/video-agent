/**
 * #398 — `selectScene` 액션이 다른 씬으로 전환되는 순간 `previewCurrentTime` 을
 *         0 으로 리셋해 TimelineBar playhead 가 옛 씬의 currentTime 위치에 박히는
 *         race 를 차단하는지 검증.
 *
 *  배경(이슈 #398):
 *    PreviewPanel 은 key={scene_id-...} 로 씬 변경 시 재마운트되며, 새 <video> 가
 *    첫 timeupdate(=currentTime=0) 를 발화하기까지 수백 ms 지연이 있다. 그 동안 store
 *    의 `previewCurrentTime` 에는 이전 씬의 마지막 currentTime(예: 8.3s) 이 남아 있어
 *    TimelineBar 가 (새 씬의 누적 시작 sec) + 8.3 으로 playhead 를 그린다.
 *    사용자는 "16번 선택했는데 14번 가운데에 박힘" 처럼 인식한다.
 *
 *  수정: store 의 `selectScene` 액션이 (id 가 기존 selectedSceneId 와 다를 때)
 *        `previewCurrentTime` 을 0 으로 동시에 리셋한다.
 */
import { beforeEach, describe, expect, it } from "vitest";
import { useStore } from "../index";

describe("#398 selectScene — playhead race 차단 (previewCurrentTime 리셋)", () => {
  beforeEach(() => {
    // 각 테스트 시작 시 selectedSceneId/previewCurrentTime 초기화.
    useStore.setState({ selectedSceneId: null, previewCurrentTime: 0 });
  });

  it("다른 씬으로 전환되면 previewCurrentTime 을 0 으로 리셋한다", () => {
    // 시나리오: 14번 씬을 재생 중이라 store 에 currentTime 8.3s 가 누적된 상태.
    useStore.setState({ selectedSceneId: 14, previewCurrentTime: 8.3 });
    useStore.getState().selectScene(16);
    expect(useStore.getState().selectedSceneId).toBe(16);
    expect(useStore.getState().previewCurrentTime).toBe(0);
  });

  it("같은 씬을 다시 선택하면 previewCurrentTime 을 유지한다 (재생 중 점프 방지)", () => {
    useStore.setState({ selectedSceneId: 14, previewCurrentTime: 8.3 });
    useStore.getState().selectScene(14);
    expect(useStore.getState().selectedSceneId).toBe(14);
    expect(useStore.getState().previewCurrentTime).toBe(8.3);
  });

  it("null 로 선택 해제하면 previewCurrentTime 을 0 으로 리셋한다", () => {
    useStore.setState({ selectedSceneId: 14, previewCurrentTime: 8.3 });
    useStore.getState().selectScene(null);
    expect(useStore.getState().selectedSceneId).toBeNull();
    expect(useStore.getState().previewCurrentTime).toBe(0);
  });

  it("초기(null) → 첫 씬 선택 시 previewCurrentTime 은 0 그대로 유지된다", () => {
    // selectedSceneId=null, previewCurrentTime=0 인 초기 상태에서 1번 선택.
    useStore.getState().selectScene(1);
    expect(useStore.getState().selectedSceneId).toBe(1);
    expect(useStore.getState().previewCurrentTime).toBe(0);
  });

  it("연속 전환: 14→16→17 모두 previewCurrentTime 0 으로 리셋", () => {
    useStore.setState({ selectedSceneId: 14, previewCurrentTime: 5.0 });
    useStore.getState().selectScene(16);
    expect(useStore.getState().previewCurrentTime).toBe(0);

    // 16번 재생 중 currentTime 2.5s 누적.
    useStore.setState({ previewCurrentTime: 2.5 });
    useStore.getState().selectScene(17);
    expect(useStore.getState().selectedSceneId).toBe(17);
    expect(useStore.getState().previewCurrentTime).toBe(0);
  });
});
