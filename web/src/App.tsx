import { lazy, Suspense, useEffect, useState } from "react";
import { Breadcrumb, Button, ConfigProvider, Drawer, Dropdown, Grid, Layout, Menu, Spin, Tooltip } from "antd";
import type { MenuProps } from "antd";
import {
  Bot,
  ChevronLeft,
  ChevronRight,
  Database,
  LibraryBig,
  Menu as MenuIcon,
  MessageSquareText,
  Settings2,
  BookOpen,
  ChevronDown,
  UsersRound,
  Wrench,
} from "lucide-react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import Login from "./Login";
import { clearSession, getMe, hasSession, logout, type User } from "./api";

const Agents = lazy(() => import("./Agents"));
const ModelCatalog = lazy(() => import("./ModelCatalog"));
const Sessions = lazy(() => import("./Sessions"));
const Skills = lazy(() => import("./Skills"));
const SkillWorkspace = lazy(() => import("./SkillWorkspace"));
const Tools = lazy(() => import("./Tools"));
const Knowledge = lazy(() => import("./Knowledge"));

const { Header, Sider, Content } = Layout;
const { useBreakpoint } = Grid;

const menuItems: MenuProps["items"] = [
  {
    type: "group",
    label: "工作区",
    children: [
      { key: "chat", icon: <MessageSquareText size={18} />, label: "对话", disabled: true },
      { key: "agents", icon: <Bot size={18} />, label: "Agent" },
      { key: "skills", icon: <BookOpen size={18} />, label: "Skill" },
      { key: "tools", icon: <Wrench size={18} />, label: "Tool" },
      { key: "knowledge-bases", icon: <LibraryBig size={18} />, label: "知识库" },
    ],
  },
  {
    type: "group",
    label: "管理",
    children: [
      { key: "users", icon: <UsersRound size={18} />, label: "用户", disabled: true },
      { key: "models", icon: <Database size={18} />, label: "模型与供应商" },
    ],
  },
  {
    type: "group",
    label: "账户",
    children: [
      { key: "settings", icon: <Settings2 size={18} />, label: "个人设置", disabled: true },
    ],
  },
];

function readToken(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function Navigation({ collapsed, isAdmin, onSelect }: { collapsed: boolean; isAdmin: boolean; onSelect: () => void }) {
  const navigate = useNavigate();
  const location = useLocation();
  return (
    <div className="navigation">
      <Link to="/agents" aria-label="Y-Point 首页" className={`brand ${collapsed ? "brand-collapsed" : ""}`}>
        <img className="brand-icon" src="/brand-icon.png" alt="Y-Point" />
        {!collapsed && <img className="brand-wordmark" src="/brand-wordmark.png" alt="" />}
      </Link>
      <Menu className="navigation-menu" mode="inline" inlineCollapsed={collapsed} selectedKeys={[location.pathname.startsWith("/knowledge-bases") ? "knowledge-bases" : location.pathname.startsWith("/models") ? "models" : location.pathname.startsWith("/skills") ? "skills" : location.pathname.startsWith("/tools") ? "tools" : "agents"]} items={menuItems?.map((group) => group && group.type === "group" ? { ...group, children: group.children?.filter((item) => isAdmin || item?.key !== "models") } : group)} onClick={({ key }) => { navigate(`/${key}`); onSelect(); }} />
    </div>
  );
}

function Shell({ user, onSignOut }: { user: User; onSignOut: () => void }) {
  const screens = useBreakpoint();
  const location = useLocation();
  const currentPage = location.pathname.startsWith("/models") ? "模型与供应商" : location.pathname.startsWith("/skills") ? "Skill" : location.pathname.startsWith("/tools") ? "Tool" : location.pathname.startsWith("/knowledge-bases") ? "知识库" : "Agent";
  const isMobile = !screens.md;
  const [collapsed, setCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const displayName = user.nickname || user.username || `用户 #${user.id}`;

  return (
    <Layout className="app-layout">
      <a className="skip-link" href="#main-content">跳转到主内容</a>
      {!isMobile && (
        <Sider className="app-sider" width="var(--yp-nav-width)" collapsedWidth="var(--yp-nav-collapsed-width)" collapsed={collapsed} trigger={null}>
          <Navigation collapsed={collapsed} isAdmin={user.role === "admin"} onSelect={() => setDrawerOpen(false)} />
        </Sider>
      )}
      <Layout className="app-main">
        <Header className="app-header">
          <Tooltip title={isMobile ? "打开导航" : collapsed ? "展开导航" : "收起导航"}>
            <Button
              className="nav-toggle"
              type="text"
              icon={isMobile ? <MenuIcon size={19} /> : collapsed ? <ChevronRight size={19} /> : <ChevronLeft size={19} />}
              aria-label={isMobile ? "打开导航" : collapsed ? "展开导航" : "收起导航"}
              onClick={() => isMobile ? setDrawerOpen(true) : setCollapsed(!collapsed)}
            />
          </Tooltip>
          {isMobile && <img className="header-wordmark" src="/brand-wordmark.png" alt="Y-Point" />}
          {!isMobile && <Breadcrumb className="header-title" aria-label="当前位置" items={[{ title: currentPage === "模型与供应商" ? "管理" : "工作区" }, { title: currentPage }, ...(location.pathname.includes("/sessions") ? [{ title: "会话" }] : [])]} />}
          <Dropdown menu={{ items: [{ key: "logout", label: "退出登录" }], onClick: onSignOut }} placement="bottomRight">
            <Button className="account-button" type="text" icon={<img className="account-brand-icon" src="/brand-icon.png" alt="" />} aria-label={`账户：${displayName}`}>
              <span className="account-name">{displayName}</span><ChevronDown className="account-chevron" size={14} aria-hidden />
            </Button>
          </Dropdown>
        </Header>
        <Content className="app-content" id="main-content" tabIndex={-1}>
          <Suspense fallback={<div className="page-loading"><Spin /></div>}>
            <Routes>
              <Route path="/agents" element={<Agents />} />
              <Route path="/agents/:agentId/sessions" element={<Sessions />} />
              <Route path="/agents/:agentId/sessions/:sessionId" element={<Sessions />} />
              <Route path="/models" element={user.role === "admin" ? <ModelCatalog /> : <Navigate to="/agents" replace />} />
              <Route path="/knowledge-bases" element={<Knowledge user={user} />} />
              <Route path="/knowledge-bases/:knowledgeId" element={<Knowledge user={user} />} />
              <Route path="/skills" element={<Skills userId={user.id} />} />
              <Route path="/skills/:skillId" element={<SkillWorkspace />} />
              <Route path="/tools" element={<Tools userId={user.id} />} />
              <Route path="*" element={<Navigate to="/agents" replace />} />
            </Routes>
          </Suspense>
        </Content>
      </Layout>
      <Drawer
        className="mobile-navigation"
        placement="left"
        width="var(--yp-mobile-nav-width)"
        open={isMobile && drawerOpen}
        onClose={() => setDrawerOpen(false)}
        closable={false}
        styles={{ body: { padding: 0 } }}
      >
        <Navigation collapsed={false} isAdmin={user.role === "admin"} onSelect={() => setDrawerOpen(false)} />
      </Drawer>
    </Layout>
  );
}

function App() {
  const screens = useBreakpoint();
  const isMobile = !screens.md;
  const [user, setUser] = useState<User | null>(null);
  const [restoring, setRestoring] = useState(hasSession);
  const [reducedMotion, setReducedMotion] = useState(() => window.matchMedia("(prefers-reduced-motion: reduce)").matches);

  useEffect(() => {
    const preference = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(preference.matches);
    preference.addEventListener("change", update);
    return () => preference.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!hasSession()) return;
    let active = true;
    getMe()
      .then((current) => { if (active) setUser(current); })
      .catch(() => { clearSession(); })
      .finally(() => { if (active) setRestoring(false); });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    const onExpired = () => setUser(null);
    window.addEventListener("yp:session-expired", onExpired);
    return () => window.removeEventListener("yp:session-expired", onExpired);
  }, []);

  async function signOut() {
    try {
      await logout();
    } catch {
      clearSession();
    } finally {
      setUser(null);
    }
  }

  return (
    <ConfigProvider
      theme={{
        components: { Tag: { defaultBg: readToken("--yp-color-tag-bg"), defaultColor: readToken("--yp-color-tag-text") } },
        token: {
          motion: !reducedMotion,
          colorPrimary: readToken("--yp-color-brand"),
          colorSuccess: readToken("--yp-color-success"),
          colorWarning: readToken("--yp-color-warning"),
          colorError: readToken("--yp-color-error"),
          colorInfo: readToken("--yp-color-info"),
          colorSuccessBg: readToken("--yp-color-success-bg"),
          colorSuccessBorder: readToken("--yp-color-success-border"),
          colorSuccessText: readToken("--yp-color-success-text"),
          colorWarningBg: readToken("--yp-color-warning-bg"),
          colorWarningBorder: readToken("--yp-color-warning-border"),
          colorWarningText: readToken("--yp-color-warning-text"),
          colorErrorBg: readToken("--yp-color-error-bg"),
          colorErrorBorder: readToken("--yp-color-error-border"),
          colorErrorText: readToken("--yp-color-error-text"),
          colorInfoBg: readToken("--yp-color-info-bg"),
          colorInfoBorder: readToken("--yp-color-info-border"),
          colorInfoText: readToken("--yp-color-info-text"),
          colorBgLayout: readToken("--yp-color-page"),
          colorBgContainer: readToken("--yp-color-surface"),
          colorText: readToken("--yp-color-text"),
          colorTextSecondary: readToken("--yp-color-text-secondary"),
          colorBorder: readToken("--yp-color-border"),
          fontFamily: readToken("--yp-font-family"),
          fontSize: Number.parseInt(readToken(isMobile ? "--yp-font-size-lg" : "--yp-font-size-md"), 10),
          borderRadius: Number.parseInt(readToken("--yp-radius-md"), 10),
          controlHeight: Number.parseInt(readToken(isMobile ? "--yp-touch-target" : "--yp-control-height"), 10),
        },
      }}
    >
      {restoring ? <div className="app-restoring"><Spin /></div> : user ? (
        <BrowserRouter><Shell user={user} onSignOut={() => void signOut()} /></BrowserRouter>
      ) : <Login onSuccess={setUser} />}
    </ConfigProvider>
  );
}

export default App;
