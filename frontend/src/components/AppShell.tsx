// 提供统一工作台导航、页面定位和可关闭的全局错误提示。
import {
  AudioLines,
  BookOpen,
  ChevronRight,
  CircleAlert,
  FileText,
  Radio,
  ShieldCheck,
  X,
} from "lucide-react";
import type { ReactNode } from "react";

const pages = [
  { id: "studio", title: "语音演示", icon: Radio },
  { id: "products", title: "产品与知识", icon: BookOpen },
  { id: "reports", title: "会话报告", icon: FileText },
];

/** 渲染共享导航和页面容器，保持功能入口与当前页面一致。 */
export function AppShell({
  page,
  setPage,
  error,
  setError,
  children,
}: {
  page: string;
  setPage: (page: string) => void;
  error: string;
  setError: (message: string) => void;
  children: ReactNode;
}) {
  const current = pages.find((item) => item.id === page) || pages[0];
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#main-content"
          onClick={(event) => {
            event.preventDefault();
            setPage("studio");
          }}
        >
          <span className="brand-mark">
            <AudioLines size={23} />
          </span>
          <span className="brand-name">
            AI audio service<span className="brand-sub">语音对话工作台</span>
          </span>
        </a>
        <div className="nav-caption">工作空间</div>
        <nav aria-label="工作台导航">
          {pages.map(({ id, title, icon: Icon }) => (
            <button
              key={id}
              className={`nav-item ${page === id ? "active" : ""}`}
              aria-current={page === id ? "page" : undefined}
              onClick={() => setPage(id)}
            >
              <Icon size={18} />
              <span>{title}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <AudioLines size={22} />
            <p>
              产品知识，
              <br />
              在对话中发挥作用。
            </p>
          </div>
          <div className="local-tag">
            <ShieldCheck size={15} />
            数据保存在本机
          </div>
          <span className="version">AI audio service · v0.1.0</span>
        </div>
      </aside>
      <main id="main-content">
        <header className="topbar">
          <div className="breadcrumb">
            <span>工作空间</span>
            <ChevronRight size={14} />
            <strong>{current.title}</strong>
          </div>
          <div className="topbar-right">
            <span className="local-only">
              <span className="dot green" />
              本地工作台
            </span>
            <span className="avatar">AI</span>
          </div>
        </header>
        <div className="content">
          {error && (
            <div role="alert" className="error-banner">
              <CircleAlert size={18} />
              <span>{error}</span>
              <button aria-label="关闭提示" onClick={() => setError("")}>
                <X size={17} />
              </button>
            </div>
          )}
          {children}
          <footer className="page-footer">
            <span>AI audio service</span>
            <span>产品知识 · 实时语音 · 会话记录</span>
          </footer>
        </div>
      </main>
    </div>
  );
}
