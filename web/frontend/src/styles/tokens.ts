/**
 * 공용 다크 테마 디자인 토큰 (#300).
 *
 * 페이지마다 제각각이던 보더 색(#2a2a2a/#333/#444/#3a3a3a 혼재), hover/focus 표시,
 * disabled 상태, 링크 색을 한 곳으로 모아 일관성을 확보한다.
 *
 * - 인라인 스타일에서는 `tokens.color.border.default` 처럼 직접 참조한다.
 * - `:focus-visible` / `:hover` 같은 의사 클래스는 인라인 스타일로 표현할 수 없으므로
 *   `globalThemeCss` 문자열을 앱 루트에 한 번 `<style>` 로 주입하여 처리한다
 *   (App.tsx 에서 사용).
 */

export const tokens = {
  color: {
    border: {
      /** 카드/입력/구분선 기본 보더 */
      default: "#2a2a2a",
      /** hover 시 강조 보더 */
      hover: "#444",
      /** focus / 활성 강조색 (브랜드 보라) */
      focus: "#6366f1",
    },
    text: {
      primary: "#fff",
      secondary: "#aaa",
      muted: "#666",
      /** disabled 상태 텍스트 */
      disabled: "#555",
    },
    bg: {
      page: "#0f0f0f",
      surface: "#1a1a1a",
      input: "#111",
    },
    /** 링크 기본/hover 색 */
    link: {
      default: "#6366f1",
      hover: "#a5b4fc",
    },
    accent: "#6366f1",
  },
} as const;

/**
 * 전역 테마 CSS. App 루트에서 한 번만 주입한다.
 *
 * 인라인 스타일로 표현 불가능한 상호작용 상태를 처리한다:
 * 1. 모든 인터랙티브 요소(button/a/input/select/textarea/[tabindex])에
 *    `:focus-visible` 시 강조색 2px outline.  (#300 DoD 2)
 * 2. `button:disabled` / `[aria-disabled="true"]` 는 opacity 0.4 + not-allowed.  (#300 DoD 3)
 * 3. 링크(a)는 hover 시 underline + 살짝 밝아짐.  (#300 DoD 4)
 *
 * 키보드(Tab) 사용 시에만 outline 이 보이도록 `:focus-visible` 을 사용하여
 * 마우스 클릭 시의 불필요한 테두리를 피한다.
 */
export const globalThemeCss = `
  button:focus-visible,
  a:focus-visible,
  input:focus-visible,
  select:focus-visible,
  textarea:focus-visible,
  [tabindex]:focus-visible {
    outline: 2px solid ${tokens.color.border.focus};
    outline-offset: 2px;
    border-radius: 4px;
  }
  button:disabled,
  button[disabled],
  [aria-disabled="true"] {
    opacity: 0.4;
    cursor: not-allowed !important;
  }
  a {
    transition: color 0.12s ease;
  }
  a:hover {
    text-decoration: underline;
    filter: brightness(1.2);
  }
`;
