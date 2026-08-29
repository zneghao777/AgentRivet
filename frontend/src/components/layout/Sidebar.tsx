import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type FocusEvent,
  type PointerEvent,
} from "react";
import { flushSync } from "react-dom";
import { Popconfirm, message } from "antd";
import { useLocation, useNavigate } from "react-router-dom";
import { gsap } from "gsap";
import { Flip } from "gsap/Flip";
import { useGSAP } from "@gsap/react";
import { deleteData, postData } from "@/api/http";
import type { SessionItem } from "@/types/api";
import { useSessionStore } from "@/stores/sessionStore";
import { summarize } from "@/utils/format";
import "@/styles/sidebar.css";

gsap.registerPlugin(useGSAP, Flip);

const SIDEBAR_STATE_KEY = "harness.sidebar.collapsed";
const PINNED_SESSIONS_KEY = "harness.sidebar.pinnedSessions";

const FEATURES = [
  { label: "观测台", icon: "monitoring", path: "/observability" },
  { label: "插件", icon: "extension", path: "/skills" },
  { label: "Agent 管理", icon: "smart_toy", path: "/agents" },
  { label: "知识库", icon: "database", path: "/knowledge" },
  { label: "记忆", icon: "memory", path: "/memory" },
  { label: "设置", icon: "settings", path: "/settings" },
];

type SessionGroupKey = "pinned" | "today" | "yesterday" | "week" | "earlier";

const SESSION_GROUPS: Array<{ key: SessionGroupKey; label: string }> = [
  { key: "pinned", label: "已置顶" },
  { key: "today", label: "今天" },
  { key: "yesterday", label: "昨天" },
  { key: "week", label: "最近 7 天" },
  { key: "earlier", label: "更早" },
];

function readStoredBoolean(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "true";
  } catch {
    return false;
  }
}

function readStoredIds(key: string): string[] {
  try {
    const value = JSON.parse(window.localStorage.getItem(key) ?? "[]");
    return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
  } catch {
    return [];
  }
}

function getSessionDate(session: SessionItem): Date | null {
  const value = session.updated_at || session.created_at;
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function getDayDifference(date: Date): number {
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const target = new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  return Math.max(0, Math.round((today - target) / 86_400_000));
}

function getSessionGroup(session: SessionItem): Exclude<SessionGroupKey, "pinned"> {
  const date = getSessionDate(session);
  if (!date) return "earlier";
  const difference = getDayDifference(date);
  if (difference === 0) return "today";
  if (difference === 1) return "yesterday";
  if (difference < 7) return "week";
  return "earlier";
}

function formatSessionTime(session: SessionItem): string {
  const date = getSessionDate(session);
  if (!date) return "";
  const difference = getDayDifference(date);
  if (difference <= 1) {
    return new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(date);
  }
  if (difference < 7) return `${difference} 天前`;
  return `${date.getMonth() + 1}月${date.getDate()}日`;
}

function isFeatureActive(pathname: string, path: string): boolean {
  if (pathname === path) return true;
  if (path === "/observability") return pathname.startsWith("/observability");
  return pathname.startsWith(`${path}/`);
}

export default function Sidebar() {
  const navigate = useNavigate();
  const location = useLocation();
  const {
    currentSessionId,
    setSessions,
    setCurrentSession,
    sessions,
    streaming,
    fetchSessions,
  } = useSessionStore();
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [collapsed, setCollapsed] = useState(() => readStoredBoolean(SIDEBAR_STATE_KEY));
  const [motion, setMotion] = useState<"splitting" | "merging" | null>(null);
  const [pinnedIds, setPinnedIds] = useState<string[]>(() => readStoredIds(PINNED_SESSIONS_KEY));

  const shellRef = useRef<HTMLElement>(null);
  const railRef = useRef<HTMLElement>(null);
  const railGlideRef = useRef<HTMLSpanElement>(null);
  const splitSeamRef = useRef<HTMLSpanElement>(null);
  const drawerRef = useRef<HTMLElement>(null);
  const recentListRef = useRef<HTMLDivElement>(null);
  const listGlideRef = useRef<HTMLSpanElement>(null);
  const activeMarkerRef = useRef<HTMLSpanElement>(null);
  const transitionRunningRef = useRef(false);

  useEffect(() => {
    void fetchSessions();
  }, [fetchSessions]);

  const pinnedSet = useMemo(() => new Set(pinnedIds), [pinnedIds]);
  const groupedSessions = useMemo(() => {
    const normalizedQuery = query.trim().toLocaleLowerCase("zh-CN");
    const visible = normalizedQuery
      ? sessions.filter((session) => (session.title || "新对话").toLocaleLowerCase("zh-CN").includes(normalizedQuery))
      : sessions;
    const groups = new Map<SessionGroupKey, SessionItem[]>(SESSION_GROUPS.map((group) => [group.key, []]));
    for (const session of visible) {
      const key: SessionGroupKey = pinnedSet.has(session.session_id) ? "pinned" : getSessionGroup(session);
      groups.get(key)?.push(session);
    }
    return SESSION_GROUPS.map((group) => ({ ...group, sessions: groups.get(group.key) ?? [] }))
      .filter((group) => group.sessions.length > 0);
  }, [pinnedSet, query, sessions]);

  const { contextSafe } = useGSAP(() => undefined, { scope: shellRef });

  const moveGlide = contextSafe((
    root: HTMLElement | null,
    highlight: HTMLElement | null,
    target: EventTarget | null,
  ) => {
    if (!root || !highlight || !(target instanceof Element)) return;
    const row = target.closest<HTMLElement>("[data-glide-row]");
    if (!row || !root.contains(row)) return;
    const rootRect = root.getBoundingClientRect();
    const rowRect = row.getBoundingClientRect();
    gsap.to(highlight, {
      x: rowRect.left - rootRect.left - root.clientLeft + root.scrollLeft,
      y: rowRect.top - rootRect.top - root.clientTop + root.scrollTop,
      width: rowRect.width,
      height: rowRect.height,
      autoAlpha: 1,
      duration: 0.18,
      ease: "power3.out",
      overwrite: "auto",
    });
  });

  const hideGlide = contextSafe((highlight: HTMLElement | null) => {
    if (!highlight) return;
    gsap.to(highlight, {
      autoAlpha: 0,
      duration: 0.12,
      ease: "power1.out",
      overwrite: "auto",
    });
  });

  useGSAP(() => {
    const marker = activeMarkerRef.current;
    const list = recentListRef.current;
    const activeRow = list?.querySelector<HTMLElement>(`[data-session-id="${currentSessionId ?? ""}"]`);
    if (!marker || !activeRow) {
      if (marker) gsap.set(marker, { autoAlpha: 0 });
      return;
    }
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    gsap.to(marker, {
      y: activeRow.offsetTop,
      height: activeRow.offsetHeight,
      autoAlpha: 1,
      duration: reducedMotion ? 0 : 0.26,
      ease: "power3.out",
      overwrite: "auto",
    });
  }, {
    dependencies: [collapsed, currentSessionId, groupedSessions],
    scope: shellRef,
  });

  const setCollapsedWithMotion = contextSafe((nextCollapsed: boolean) => {
    const shell = shellRef.current;
    const rail = railRef.current;
    const drawer = drawerRef.current;
    const seam = splitSeamRef.current;
    if (!shell || !rail || !drawer || !seam || transitionRunningRef.current || nextCollapsed === collapsed) return;
    const drawerCopy = drawer.querySelectorAll<HTMLElement>("[data-drawer-copy]");
    const sibling = shell.nextElementSibling;
    const targets: Element[] = sibling ? [shell, sibling] : [shell];
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const canMorph = window.matchMedia("(min-width: 901px)").matches && !reducedMotion;

    const persist = () => {
      try {
        window.localStorage.setItem(SIDEBAR_STATE_KEY, String(nextCollapsed));
      } catch {
        // 本地存储不可用时仍保留当前会话内状态。
      }
    };

    const commitWithoutMotion = () => {
      flushSync(() => {
        setCollapsed(nextCollapsed);
        setMotion(null);
      });
      transitionRunningRef.current = false;
    };

    transitionRunningRef.current = true;
    persist();
    gsap.killTweensOf([drawer, drawerCopy, seam, rail, railGlideRef.current]);
    gsap.set(railGlideRef.current, { autoAlpha: 0 });

    if (!canMorph) {
      commitWithoutMotion();
      return;
    }

    const layoutState = Flip.getState(targets);
    flushSync(() => {
      setMotion(nextCollapsed ? "merging" : "splitting");
      setCollapsed(nextCollapsed);
    });

    if (!nextCollapsed) {
      gsap.set(drawer, {
        x: -68,
        scaleX: 0.08,
        scaleY: 0.965,
        autoAlpha: 0.36,
        transformOrigin: "left center",
      });
      gsap.set(drawerCopy, { x: -12, autoAlpha: 0 });
    }

    gsap.set(seam, {
      autoAlpha: 0,
      scaleX: nextCollapsed ? 0.55 : 0.3,
      transformOrigin: "left center",
    });

    Flip.from(layoutState, {
      duration: 0.62,
      ease: "power3.inOut",
      nested: true,
      scale: false,
      simple: true,
    });

    const finishMorph = () => {
      flushSync(() => setMotion(null));
      gsap.set(drawer, { clearProps: "transform,opacity,visibility,willChange" });
      gsap.set(drawerCopy, { clearProps: "transform,opacity,visibility" });
      gsap.set(seam, { clearProps: "transform,opacity,visibility,willChange" });
      gsap.set(rail, { clearProps: "transform,willChange" });
      transitionRunningRef.current = false;
    };

    const timeline = gsap.timeline({ onComplete: finishMorph });
    timeline.call(() => undefined, [], 0.62);

    if (nextCollapsed) {
      timeline
        .set([drawer, seam], { willChange: "transform, opacity" }, 0)
        .set(rail, { willChange: "transform" }, 0)
        .to(drawerCopy, {
          x: -12,
          autoAlpha: 0,
          duration: 0.2,
          ease: "power2.in",
          stagger: 0.006,
        }, 0)
        .to(seam, {
          autoAlpha: 0.9,
          scaleX: 1.35,
          duration: 0.2,
          ease: "power2.out",
        }, 0.06)
        .to(drawer, {
          x: -68,
          scaleX: 0.08,
          scaleY: 0.965,
          autoAlpha: 0.36,
          duration: 0.56,
          ease: "power3.inOut",
        }, 0.04)
        .to(rail, {
          scaleX: 1.018,
          transformOrigin: "right center",
          duration: 0.24,
          ease: "power2.inOut",
          yoyo: true,
          repeat: 1,
        }, 0.1)
        .to(seam, {
          autoAlpha: 0,
          scaleX: 0.22,
          duration: 0.22,
          ease: "power3.in",
        }, 0.34)
        .set(drawer, { autoAlpha: 0 }, 0.55);
      return;
    }

    timeline
      .set([drawer, seam], { willChange: "transform, opacity" }, 0)
      .set(rail, { willChange: "transform" }, 0)
      .to(seam, {
        autoAlpha: 0.9,
        scaleX: 1.45,
        duration: 0.2,
        ease: "power2.out",
      }, 0)
      .to(drawer, {
        x: 0,
        scaleX: 1,
        scaleY: 1,
        autoAlpha: 1,
        duration: 0.6,
        ease: "power3.inOut",
      }, 0.02)
      .to(rail, {
        scaleX: 0.982,
        transformOrigin: "right center",
        duration: 0.22,
        ease: "power2.inOut",
        yoyo: true,
        repeat: 1,
      }, 0.04)
      .to(seam, {
        autoAlpha: 0,
        scaleX: 0.26,
        duration: 0.24,
        ease: "power3.in",
      }, 0.34)
      .to(drawerCopy, {
        x: 0,
        autoAlpha: 1,
        duration: 0.24,
        ease: "power2.out",
        stagger: 0.012,
      }, 0.26);
  });

  const createNew = async () => {
    if (streaming) return;
    setCreating(true);
    try {
      const session = await postData<SessionItem>("/sessions", { title: "新对话" });
      setSessions([session, ...sessions.filter((item) => item.session_id !== session.session_id)]);
      setCurrentSession(session.session_id);
      navigate("/");
      if (collapsed) setCollapsedWithMotion(false);
    } finally {
      setCreating(false);
    }
  };

  const removeSession = async (sessionId: string) => {
    if (streaming) return;
    setDeletingId(sessionId);
    try {
      await deleteData(`/sessions/${sessionId}`);
      const remaining = sessions.filter((item) => item.session_id !== sessionId);
      setSessions(remaining);
      setPinnedIds((current) => {
        const next = current.filter((id) => id !== sessionId);
        try {
          window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify(next));
        } catch {
          // 本地存储不可用时仍保留当前会话内状态。
        }
        return next;
      });
      if (currentSessionId === sessionId) {
        setCurrentSession(remaining[0]?.session_id ?? null);
        navigate("/");
      }
      message.success("会话已删除");
    } catch (error) {
      message.error(error instanceof Error ? error.message : "删除会话失败");
    } finally {
      setDeletingId(null);
    }
  };

  const togglePinned = (sessionId: string) => {
    setPinnedIds((current) => {
      const next = current.includes(sessionId)
        ? current.filter((id) => id !== sessionId)
        : [sessionId, ...current];
      try {
        window.localStorage.setItem(PINNED_SESSIONS_KEY, JSON.stringify(next));
      } catch {
        // 本地存储不可用时仍保留当前会话内状态。
      }
      return next;
    });
  };

  const openHistory = () => {
    navigate("/");
    if (collapsed) setCollapsedWithMotion(false);
  };

  const handleRailPointerMove = (event: PointerEvent<HTMLElement>) => {
    moveGlide(railRef.current, railGlideRef.current, event.target);
  };

  const handleRailFocus = (event: FocusEvent<HTMLElement>) => {
    moveGlide(railRef.current, railGlideRef.current, event.target);
  };

  const handleListPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    moveGlide(recentListRef.current, listGlideRef.current, event.target);
  };

  const handleListFocus = (event: FocusEvent<HTMLDivElement>) => {
    moveGlide(recentListRef.current, listGlideRef.current, event.target);
  };

  return (
    <aside
      ref={shellRef}
      className={`side-panel sidebar-shell${collapsed ? " is-collapsed" : ""}${motion ? " is-morphing" : ""}`}
      data-collapsed={collapsed}
      data-motion={motion ?? undefined}
      aria-label="Harness 导航与历史会话"
    >
      <nav
        ref={railRef}
        className="sidebar-rail"
        aria-label="功能区"
        onPointerMove={handleRailPointerMove}
        onPointerLeave={() => hideGlide(railGlideRef.current)}
        onFocusCapture={handleRailFocus}
        onBlurCapture={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) hideGlide(railGlideRef.current);
        }}
      >
        <span ref={railGlideRef} className="sidebar-glide-highlight sidebar-rail-glide" aria-hidden="true" />

        <button
          type="button"
          className="sidebar-rail-brand"
          aria-label={collapsed ? "展开历史会话" : "返回对话"}
          data-tooltip={collapsed ? "展开历史会话" : "Harness"}
          onClick={collapsed ? () => setCollapsedWithMotion(false) : () => navigate("/")}
        >
          <span className="material-symbols-rounded">explore</span>
        </button>

        <div className="sidebar-rail-primary">
          <button
            type="button"
            className="sidebar-rail-button is-primary"
            data-glide-row
            data-tooltip={creating ? "创建中…" : "新建会话"}
            aria-label={creating ? "正在创建会话" : "新建会话"}
            onClick={() => void createNew()}
            disabled={creating || streaming}
          >
            <span className={`material-symbols-rounded${creating ? " is-spinning" : ""}`}>
              {creating ? "progress_activity" : "edit_square"}
            </span>
          </button>
          <button
            type="button"
            className={`sidebar-rail-button${location.pathname === "/" ? " is-active" : ""}`}
            data-glide-row
            data-tooltip="历史会话"
            aria-label="打开历史会话"
            aria-expanded={!collapsed}
            onClick={openHistory}
          >
            <span className="material-symbols-rounded">forum</span>
          </button>
        </div>

        <div className="sidebar-rail-features" aria-label="工作区功能">
          {FEATURES.map((item) => {
            const active = isFeatureActive(location.pathname, item.path);
            return (
              <button
                type="button"
                key={item.path}
                className={`sidebar-rail-button${active ? " is-active" : ""}`}
                data-glide-row
                data-tooltip={item.label}
                aria-label={item.label}
                aria-current={active ? "page" : undefined}
                onClick={() => navigate(item.path)}
              >
                <span className="material-symbols-rounded">{item.icon}</span>
              </button>
            );
          })}
        </div>

        <button
          type="button"
          className="sidebar-rail-profile"
          data-glide-row
          data-tooltip="Admin User"
          aria-label="账户：Admin User"
        >
          <span className="material-symbols-rounded">person</span>
        </button>
      </nav>

      <span ref={splitSeamRef} className="sidebar-split-seam" aria-hidden="true" />

      <section ref={drawerRef} className="sidebar-drawer" aria-hidden={collapsed}>
        <header className="sidebar-drawer-header" data-drawer-copy>
          <div className="sidebar-drawer-title">
            <strong>Harness</strong>
            <small>最近会话</small>
          </div>
          <button
            type="button"
            className="sidebar-collapse-button"
            aria-label="收起历史会话"
            onClick={() => setCollapsedWithMotion(true)}
          >
            <span className="material-symbols-rounded">keyboard_double_arrow_left</span>
          </button>
        </header>

        <label className="sidebar-search" data-drawer-copy>
          <span className="material-symbols-rounded">search</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索会话"
            aria-label="搜索历史会话"
          />
          {query ? (
            <button type="button" aria-label="清空搜索" onClick={() => setQuery("")}>
              <span className="material-symbols-rounded">close</span>
            </button>
          ) : null}
        </label>

        <div
          ref={recentListRef}
          className="sidebar-recent-list"
          data-drawer-copy
          onPointerMove={handleListPointerMove}
          onPointerLeave={() => hideGlide(listGlideRef.current)}
          onFocusCapture={handleListFocus}
          onBlurCapture={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget)) hideGlide(listGlideRef.current);
          }}
          onScroll={() => hideGlide(listGlideRef.current)}
        >
          <span ref={listGlideRef} className="sidebar-glide-highlight sidebar-list-glide" aria-hidden="true" />
          <span ref={activeMarkerRef} className="sidebar-session-active-marker" aria-hidden="true" />

          {groupedSessions.length === 0 ? (
            <div className="sidebar-empty-state">
              <span className="material-symbols-rounded">chat_bubble</span>
              <strong>{query ? "没有匹配的会话" : "暂无会话"}</strong>
              <small>{query ? "试试其他关键词" : "创建新会话后会显示在这里"}</small>
            </div>
          ) : null}

          {groupedSessions.map((group) => (
            <section className="sidebar-session-group" key={group.key} aria-labelledby={`session-group-${group.key}`}>
              <div className="sidebar-session-group-label" id={`session-group-${group.key}`}>
                <span>{group.label}</span>
                <small>{group.sessions.length}</small>
              </div>
              {group.sessions.map((session) => {
                const active = currentSessionId === session.session_id;
                const pinned = pinnedSet.has(session.session_id);
                return (
                  <div
                    className={`sidebar-session-row${active ? " is-active" : ""}`}
                    key={session.session_id}
                    data-glide-row
                    data-session-id={session.session_id}
                  >
                    <button
                      type="button"
                      className="sidebar-session-select"
                      onClick={() => {
                        setCurrentSession(session.session_id);
                        navigate("/");
                      }}
                      disabled={streaming && !active}
                      aria-current={active ? "page" : undefined}
                    >
                      <span className="material-symbols-rounded sidebar-session-icon">chat_bubble</span>
                      <span className="sidebar-session-copy">
                        <strong>{summarize(session.title || "新对话", 18)}</strong>
                        <small className={streaming && active ? "is-streaming" : ""}>
                          {streaming && active ? <i aria-hidden="true" /> : null}
                          {streaming && active ? "正在回复…" : formatSessionTime(session)}
                        </small>
                      </span>
                    </button>
                    <span className="sidebar-session-actions">
                      <button
                        type="button"
                        className={pinned ? "is-pinned" : ""}
                        title={pinned ? "取消置顶" : "置顶会话"}
                        aria-label={`${pinned ? "取消置顶" : "置顶会话"}：${session.title || "新对话"}`}
                        onClick={() => togglePinned(session.session_id)}
                      >
                        <span className="material-symbols-rounded">push_pin</span>
                      </button>
                      <Popconfirm
                        title="删除这条会话？"
                        description="删除后将无法恢复。"
                        okText="删除"
                        cancelText="取消"
                        okButtonProps={{ danger: true }}
                        onConfirm={() => void removeSession(session.session_id)}
                      >
                        <button
                          type="button"
                          title="删除会话"
                          aria-label={`删除会话：${session.title || "新对话"}`}
                          disabled={streaming || deletingId === session.session_id}
                        >
                          <span className={`material-symbols-rounded${deletingId === session.session_id ? " is-spinning" : ""}`}>
                            {deletingId === session.session_id ? "progress_activity" : "delete"}
                          </span>
                        </button>
                      </Popconfirm>
                    </span>
                  </div>
                );
              })}
            </section>
          ))}
        </div>
      </section>
    </aside>
  );
}
