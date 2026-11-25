/**
 * 템플릿 선택 모달 (#407).
 *
 * 홈 화면 '템플릿으로 만들기' 버튼 클릭 시 노출. 백엔드 GET /api/templates 로 받은
 * 목록을 카드 형태로 보여주고 사용자가 하나를 선택하면 onPick(template_id) 콜백을 호출.
 *
 * 부모(App.tsx)가 createProjectFromTemplate 호출/에러 처리/네비게이션을 담당한다.
 * 이 컴포넌트는 표시·선택까지만 책임진다.
 */
import { useEffect, useState } from "react";
import { listTemplates, deleteTemplate, type TemplateMeta } from "../api/client";
import { ui } from "../store/ui";

export interface TemplatePickerModalProps {
  open: boolean;
  onClose: () => void;
  onPick: (templateId: string, defaultTitle: string) => void;
}

export default function TemplatePickerModal({ open, onClose, onPick }: TemplatePickerModalProps) {
  const [templates, setTemplates] = useState<TemplateMeta[] | null>(null);
  const [error, setError] = useState<string>("");

  async function reload() {
    setError("");
    try {
      const items = await listTemplates();
      setTemplates(items);
    } catch (e) {
      setError(`템플릿 목록 불러오기 실패: ${(e as Error).message}`);
      setTemplates([]);
    }
  }

  useEffect(() => {
    if (!open) return;
    reload();
  }, [open]);

  async function handleDelete(t: TemplateMeta) {
    const ok = await ui.confirm({
      title: "템플릿 삭제",
      message: `템플릿 "${t.title}" 을 삭제합니다. 이 작업은 되돌릴 수 없습니다.`,
      variant: "danger",
      confirmLabel: "삭제",
    });
    if (!ok) return;
    try {
      await deleteTemplate(t.template_id);
      ui.success("템플릿이 삭제되었습니다");
      await reload();
    } catch (e) {
      ui.error(`템플릿 삭제 실패: ${(e as Error).message}`);
    }
  }

  if (!open) return null;

  return (
    <div style={styles.backdrop} onClick={onClose}>
      <div style={styles.modal} onClick={(e) => e.stopPropagation()}>
        <div style={styles.header}>
          <div style={styles.title}>템플릿으로 만들기</div>
          <button style={styles.closeBtn} onClick={onClose} title="닫기">×</button>
        </div>
        <div style={styles.subtitle}>
          저장된 템플릿을 출발점으로 새 프로젝트를 만듭니다.
          씬 구조 · 자막 · 오버레이 · BGM 은 그대로 가져오고, 영상은 씬별로 새로 업로드합니다.
        </div>

        {templates === null && <div style={styles.loading}>불러오는 중...</div>}
        {error && <div style={styles.error}>{error}</div>}

        {templates !== null && templates.length === 0 && (
          <div style={styles.empty}>
            저장된 템플릿이 없습니다.<br />
            편집기에서 "📐 템플릿으로 지정" 버튼을 눌러 현재 영상 구조를 저장하면 여기에 나타납니다.
          </div>
        )}

        {templates !== null && templates.length > 0 && (
          <div style={styles.list}>
            {templates.map((t) => (
              <div key={t.template_id} style={styles.card}>
                <div style={styles.cardLeft}>
                  <div style={styles.cardTitle}>{t.title}</div>
                  {t.description && <div style={styles.cardDesc}>{t.description}</div>}
                  <div style={styles.cardMeta}>
                    씬 {t.scene_count}개
                    {t.created_at && ` · ${new Date(t.created_at).toLocaleString("ko-KR")}`}
                  </div>
                </div>
                <div style={styles.cardActions}>
                  <button
                    style={styles.pickBtn}
                    onClick={() => onPick(t.template_id, t.title)}
                    title="이 템플릿으로 새 프로젝트 만들기"
                  >
                    선택 →
                  </button>
                  <button
                    style={styles.deleteBtn}
                    onClick={() => handleDelete(t)}
                    title="템플릿 삭제"
                  >
                    삭제
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

const styles: Record<string, React.CSSProperties> = {
  backdrop: {
    position: "fixed",
    inset: 0,
    background: "rgba(0,0,0,0.6)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    zIndex: 1000,
  },
  modal: {
    background: "#1a1a1a",
    border: "1px solid #2a2a2a",
    borderRadius: 12,
    padding: "20px 24px",
    width: "min(640px, 92vw)",
    maxHeight: "80vh",
    overflowY: "auto",
    color: "#e4e4e4",
  },
  header: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    marginBottom: 6,
  },
  title: {
    fontSize: 18,
    fontWeight: 600,
    color: "#fff",
  },
  closeBtn: {
    background: "transparent",
    border: "none",
    color: "#999",
    fontSize: 22,
    cursor: "pointer",
    padding: 0,
    lineHeight: 1,
  },
  subtitle: {
    fontSize: 12,
    color: "#888",
    marginBottom: 16,
    lineHeight: 1.5,
  },
  loading: {
    padding: 24,
    textAlign: "center" as const,
    color: "#999",
  },
  error: {
    padding: 12,
    background: "#3b1f1f",
    border: "1px solid #5a2a2a",
    borderRadius: 6,
    color: "#f87171",
    fontSize: 13,
    marginBottom: 12,
  },
  empty: {
    padding: 32,
    textAlign: "center" as const,
    color: "#999",
    fontSize: 13,
    lineHeight: 1.7,
  },
  list: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 10,
  },
  card: {
    display: "flex",
    alignItems: "center",
    justifyContent: "space-between",
    padding: "12px 14px",
    background: "#222",
    border: "1px solid #2a2a2a",
    borderRadius: 8,
  },
  cardLeft: {
    display: "flex",
    flexDirection: "column" as const,
    gap: 4,
    flex: 1,
    minWidth: 0,
  },
  cardTitle: {
    fontSize: 14,
    fontWeight: 600,
    color: "#fff",
  },
  cardDesc: {
    fontSize: 12,
    color: "#bbb",
  },
  cardMeta: {
    fontSize: 11,
    color: "#777",
  },
  cardActions: {
    display: "flex",
    gap: 8,
    marginLeft: 12,
  },
  pickBtn: {
    padding: "8px 14px",
    background: "#2563eb",
    color: "#fff",
    border: "none",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 13,
    fontWeight: 600,
  },
  deleteBtn: {
    padding: "8px 12px",
    background: "transparent",
    color: "#f87171",
    border: "1px solid #5a2a2a",
    borderRadius: 6,
    cursor: "pointer",
    fontSize: 12,
  },
};
