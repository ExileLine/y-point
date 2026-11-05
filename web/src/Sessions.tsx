import { Fragment, memo, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Alert, Button, Dropdown, Empty, Grid, Input, Modal, Pagination, Select, Skeleton, Spin, Tabs, Tag, Tooltip, Typography, message } from "antd";
import { Archive, ArrowDown, ArrowLeft, Bot, MessageSquareText, MoreHorizontal, Pencil, Plus, RefreshCw, RotateCcw, Search, SendHorizontal, Square, Trash2 } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";
import {
  checkSessionAttachments, ApiError, cancelRun, createAgentSession, deleteAgentSession, getAgent, getAgentSession, getRun, getRunSnapshot, getSharedAgent,
  issueRunStreamTicket, listAgentSessions, listSessionMessages, listSessionModels, listSessionRuns, runStreamUrl, submitSessionRun, updateAgentSession,
  type Agent, type AgentSession, type AgentSessionPage, type SessionMessage, type SessionRun,
  type RunStreamEvent, type SessionRunDraft, type SharedAgent, type SessionModelOption,
} from "./api";
import { MessageContent } from "./components/MessageContent";
import MessageActions from "./components/MessageActions";
import MessageFiles from "./components/MessageFiles";
import ChatAttachments, { type ChatFile } from "./components/ChatAttachments";
import RunCalls, { ActiveRunCalls } from "./components/RunCalls";
import StreamingMessage from "./components/StreamingMessage";
import RunKnowledgeEvidence from "./knowledge/RunKnowledgeEvidence";
import "./styles/sessions.css";

const terminalStates = new Set(["completed", "failed", "cancelled", "timed_out"]);
const stateLabels: Record<string, string> = {
  queued: "等待回复", running: "运行中", waiting_for_approval: "等待审批", cancel_requested: "取消中",
  completed: "已完成", failed: "运行失败", cancelled: "已取消", timed_out: "已超时",
};
const failureLabels: Record<string, string> = {
  provider_timeout: "模型供应商响应超时，可稍后重新发送；持续超时请检查供应商超时设置或切换模型",
  execution_timeout: "本轮已达到执行时限，可重新发送或调整智能体的模型超时预算",
  model_error: "模型请求失败，可重新发送或切换模型；如持续失败请检查供应商连接",
  summary_error: "上下文摘要失败", checkpoint_mismatch: "会话状态不一致", attachment_unavailable: "附件不可用",
  artifact_required: "需要文件", context_window_exceeded: "超出模型上下文窗口",
};

function RunStateLabel({ state }: { state: string }) {
  if (state === "queued") return <span className="sessions-queue-loading" role="status" aria-label="等待回复">
    <span aria-hidden="true"><span>.</span><span>.</span><span>.</span></span>
  </span>;
  return <>{stateLabels[state] || state}</>;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : "操作失败";
}

function displayTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

const MessageHistory = memo(function MessageHistory({ items, pendingRunId }: { items: SessionMessage[]; pendingRunId: string | null }) {
  const answered = new Set(items.filter(item => item.role === "assistant").map(item => item.run_id));
  return <div className="sessions-messages">
    {items.map((item) => <Fragment key={item.id}><div className={`session-message session-message-${item.role}`} data-message-id={item.id}>
      <div className="session-message-avatar">{item.role === "assistant" ? <Bot size={18} /> : "我"}</div>
      <div className="session-message-main">
        <div className="session-message-meta"><strong>{item.role === "assistant" ? "Agent" : "我"}</strong>{item.role === "assistant" && item.model_id && <Tooltip title={[item.provider_name, item.model_name || `模型 #${item.model_id}`].filter(Boolean).join(" · ")}><span className="session-message-model">{item.model_name || `模型 #${item.model_id}`}</span></Tooltip>}<time dateTime={item.create_time}>{displayTime(item.create_time)}</time></div>
        {item.role === "assistant" && <RunCalls calls={item.calls || []} />}
        <div className="session-message-content">{item.role === "assistant" ? <MessageContent text={item.content} /> : item.content}</div>
        <MessageActions text={item.content} assistant={item.role === "assistant"} />
        {item.role === "assistant" && item.run_id && <RunKnowledgeEvidence runId={item.run_id} />}
        <MessageFiles files={item.attachments} />
        <MessageFiles files={item.artifacts} generated />
      </div>
    </div>
    {item.role === "user" && item.calls?.length && !answered.has(item.run_id) && item.run_id !== pendingRunId ? <div className="session-message session-message-assistant" data-call-run={item.run_id}>
      <div className="session-message-avatar"><Bot size={18} /></div>
      <div className="session-message-main"><div className="session-message-meta"><strong>Agent</strong></div><RunCalls calls={item.calls} /></div>
    </div> : null}
    </Fragment>)}
  </div>;
});

export default function Sessions() {
  const { agentId: agentIdParam, sessionId } = useParams();
  const agentId = Number(agentIdParam);
  const navigate = useNavigate();
  const screens = Grid.useBreakpoint();
  const isMobile = !screens.md;
  const basePath = `/agents/${agentId}/sessions`;
  const [agent, setAgent] = useState<Agent | SharedAgent | null>(null);
  const [agentError, setAgentError] = useState("");
  const [agentLoading, setAgentLoading] = useState(true);
  const [archived, setArchived] = useState(false);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(1);
  const [revision, setRevision] = useState(0);
  const [directory, setDirectory] = useState<AgentSessionPage>({ items: [], total: 0, page: 1, page_size: 20 });
  const [directoryLoading, setDirectoryLoading] = useState(true);
  const [directoryError, setDirectoryError] = useState("");
  const [selected, setSelected] = useState<AgentSession | null>(null);
  const [selectedLoading, setSelectedLoading] = useState(false);
  const [selectedError, setSelectedError] = useState("");
  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [messagesLoading, setMessagesLoading] = useState(false);
  const [messagesError, setMessagesError] = useState("");
  const [messageRevision, setMessageRevision] = useState(0);
  const [callsRevision, setCallsRevision] = useState(0);
  const [activeRun, setActiveRun] = useState<SessionRun | null>(null);
  const [lastRun, setLastRun] = useState<SessionRun | null>(null);
  const [runError, setRunError] = useState("");
  const [streamText, setStreamText] = useState<{ runId: string; text: string } | null>(null);
  const [streamFallback, setStreamFallback] = useState(false);
  const [composer, setComposer] = useState("");
  const [chatFiles, setChatFiles] = useState<Record<string, ChatFile[]>>({});
  const files = selected ? chatFiles[selected.id] || [] : [];
  const attachmentPending = files.some(item => item.state !== "ready");
  const [pendingDraft, setPendingDraft] = useState<SessionRunDraft | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [models, setModels] = useState<SessionModelOption[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [modelsError, setModelsError] = useState("");
  const [modelsRevision, setModelsRevision] = useState(0);
  const [modelTarget, setModelTarget] = useState<number | null>(null);
  const [switchingModel, setSwitchingModel] = useState(false);
  const [runsLoading, setRunsLoading] = useState(false);
  const [renameTarget, setRenameTarget] = useState<AgentSession | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<AgentSession | null>(null);
  const [newOpen, setNewOpen] = useState(false);
  const [newTitle, setNewTitle] = useState("");
  const historyRef = useRef<HTMLDivElement>(null);
  const historyContentRef = useRef<HTMLDivElement>(null);
  const followingRef = useRef(true);
  const [following, setFollowing] = useState(true);
  const followLatest = useCallback(() => {
    followingRef.current = true;
    setFollowing(true);
    const history = historyRef.current;
    if (history) history.scrollTop = history.scrollHeight;
  }, []);
  const followContent = useCallback(() => {
    const history = historyRef.current;
    if (history && followingRef.current) history.scrollTop = history.scrollHeight;
  }, []);
  function trackScroll() {
    const history = historyRef.current;
    if (!history) return;
    const nearBottom = history.scrollHeight - history.scrollTop - history.clientHeight < 80;
    followingRef.current = nearBottom;
    setFollowing(nearBottom);
  }
  const currentSessionId = useRef(sessionId);
  currentSessionId.current = sessionId;

  function invalidateAccess() {
    setAgent(null);
    setAgentError("Agent 不可访问或共享授权已撤销");
    setSelected(null);
    setMessages([]);
    setActiveRun(null);
    setDirectory({ items: [], total: 0, page: 1, page_size: 20 });
  }

  useEffect(() => {
    let active = true;
    setAgentLoading(true);
    setAgentError("");
    setAgent(null);
    if (!Number.isInteger(agentId) || agentId <= 0) {
      setAgentError("Agent 地址无效");
      setAgentLoading(false);
      return;
    }
    getAgent(agentId).catch((failure) => {
      if (failure instanceof ApiError && failure.status === 404) return getSharedAgent(agentId);
      throw failure;
    }).then((item) => { if (active) setAgent(item); }).catch((failure) => { if (active) setAgentError(errorText(failure)); }).finally(() => { if (active) setAgentLoading(false); });
    return () => { active = false; };
  }, [agentId]);

  useEffect(() => {
    const timer = window.setTimeout(() => { setSearch(searchInput.trim()); setPage(1); }, 300);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  useEffect(() => {
    if (!Number.isInteger(agentId) || agentId <= 0 || !agent) return;
    let active = true;
    setDirectoryLoading(true);
    setDirectoryError("");
    listAgentSessions(agentId, page, archived, search).then((result) => { if (active) setDirectory(result); }).catch((failure) => {
      if (!active) return;
      if (failure instanceof ApiError && failure.status === 404) invalidateAccess();
      else setDirectoryError(errorText(failure));
    }).finally(() => { if (active) setDirectoryLoading(false); });
    return () => { active = false; };
  }, [agentId, agent, page, archived, search, revision]);

  useEffect(() => {
    if (!sessionId || !agent) { setSelected(null); setSelectedError(""); return; }
    let active = true;
    setSelectedLoading(true);
    setSelectedError("");
    setSelected(null);
    setMessages([]);
    setActiveRun(null);
    setLastRun(null);
    setStreamText(null);
    setStreamFallback(false);
    setPendingDraft(null);
    setRunError("");
    setComposer("");
    setSubmitting(false);
    getAgentSession(agentId, sessionId).then((item) => {
      if (!active) return;
      setSelected(item);
      setArchived(item.archived);
    }).catch((failure) => { if (active) { setSelectedError(errorText(failure)); setMessages([]); setActiveRun(null); } }).finally(() => { if (active) setSelectedLoading(false); });
    return () => { active = false; };
  }, [agent, agentId, sessionId]);

  useEffect(() => {
    if (!selected || !sessionId) return;
    let active = true;
    setMessagesLoading(true);
    setMessagesError("");
    async function loadHistory() {
      let after = 0;
      const all: SessionMessage[] = [];
      for (;;) {
        const batch = await listSessionMessages(agentId, sessionId!, after);
        all.push(...batch);
        if (batch.length < 100) break;
        const next = batch.at(-1)!.id;
        if (next <= after) throw new Error("消息游标未前进");
        after = next;
      }
      if (active) setMessages(all);
    }
    void loadHistory().catch((failure) => { if (active) {
      setMessagesError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 404) { setMessages([]); setSelected(null); setSelectedError("会话不可访问，请返回列表刷新"); }
    } }).finally(() => { if (active) setMessagesLoading(false); });
    return () => { active = false; };
  }, [agentId, selected?.id, sessionId, messageRevision]);

  useEffect(() => {
    setModelTarget(null);
    setModels([]);
    setModelsError("");
    if (!selected?.id) return;
    const controller = new AbortController();
    setModelsLoading(true);
    listSessionModels(agentId, selected.id, controller.signal).then(items => { if (!controller.signal.aborted) setModels(items); }).catch((failure) => {
      if (!controller.signal.aborted) setModelsError(errorText(failure));
    }).finally(() => { if (!controller.signal.aborted) setModelsLoading(false); });
    return () => controller.abort();
  }, [agentId, selected?.id, modelsRevision]);

  useLayoutEffect(() => {
    followingRef.current = true;
    setFollowing(true);
  }, [sessionId]);

  useLayoutEffect(() => { followContent(); }, [messages, streamText, messagesLoading, activeRun?.state, followContent]);

  useEffect(() => {
    const content = historyContentRef.current;
    if (!content) return;
    const observer = new ResizeObserver(followContent);
    observer.observe(content);
    return () => observer.disconnect();
  }, [selected?.id, followContent]);

  useEffect(() => {
    if (!selected || !sessionId) return;
    let active = true;
    setRunsLoading(true);
    listSessionRuns(agentId, sessionId).then((result) => {
      if (!active) return;
      const running = result.items.find((item) => !terminalStates.has(item.state));
      setActiveRun(running || null);
      setLastRun(result.items[0] || null);
    }).catch((failure) => { if (active) {
      setRunError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 404) { setMessages([]); setSelected(null); setSelectedError("会话不可访问，请返回列表刷新"); }
    } }).finally(() => { if (active) setRunsLoading(false); });
    return () => { active = false; };
  }, [agentId, selected?.id, sessionId]);

  useEffect(() => {
    if (!activeRun || !sessionId || !streamFallback) return;
    const runId = activeRun.id;
    let active = true;
    let timer: number;
    async function tick() {
      try {
        const current = await getRun(runId);
        if (!active) return;
        setRunError("");
        setLastRun(current);
        if (terminalStates.has(current.state)) {
          setActiveRun(null);
          setMessageRevision((value) => value + 1);
          setRevision((value) => value + 1);
          return;
        }
        setActiveRun(current);
      } catch (failure) {
        if (!active) return;
        setRunError(errorText(failure));
        if (failure instanceof ApiError && failure.status === 404) {
          setMessages([]);
          setSelected(null);
          setSelectedError("会话不可访问，请返回列表刷新");
          setActiveRun(null);
          return;
        }
      }
      timer = window.setTimeout(() => void tick(), 2500);
    }
    timer = window.setTimeout(() => void tick(), 800);
    return () => { active = false; window.clearTimeout(timer); };
  }, [activeRun?.id, sessionId, streamFallback]);

  useEffect(() => {
    if (!activeRun || !sessionId) return;
    const subscribedRun = activeRun;
    const runId = subscribedRun.id;
    let active = true;
    let cursor = 0;
    let stream: EventSource | null = null;
    let retry: number | undefined;
    const fallbackTimer = window.setTimeout(() => { if (active) setStreamFallback(true); }, 5000);
    setStreamText(null);
    setStreamFallback(false);

    function applyEvent(payload: RunStreamEvent) {
      if (payload.sequence <= cursor) return;
      cursor = payload.sequence;
      if (payload.event_type === "text_delta" && payload.content) {
        setStreamText((current) => ({ runId, text: (current?.runId === runId ? current.text : "") + payload.content }));
      }
      if (["tool_started", "tool_completed", "tool_failed"].includes(payload.event_type)) setCallsRevision(value => value + 1);
      if (payload.event_type === "artifact_published") setMessageRevision((value) => value + 1);
      if (payload.event_type === "state" && payload.state && terminalStates.has(payload.state)) {
        setLastRun((current) => ({ ...(current?.id === runId ? current : subscribedRun), state: payload.state! }));
        setActiveRun(null);
        setMessageRevision((value) => value + 1);
        setRevision((value) => value + 1);
        stream?.close();
        stream = null;
      }
    }

    async function connect() {
      try {
        let snapshot;
        do {
          snapshot = await getRunSnapshot(runId, cursor);
          if (!active) return;
          snapshot.events.forEach(applyEvent);
          cursor = Math.max(cursor, snapshot.next_after);
        } while (snapshot.has_more);
        if (terminalStates.has(snapshot.run.state)) {
          setLastRun(snapshot.run);
          setActiveRun(null);
          setMessageRevision((value) => value + 1);
          setRevision((value) => value + 1);
          return;
        }
        await issueRunStreamTicket(runId);
        if (!active) return;
        stream = new EventSource(runStreamUrl(runId, cursor));
        stream.onopen = () => { window.clearTimeout(fallbackTimer); if (active) setStreamFallback(false); };
        stream.addEventListener("text_delta", (event) => {
          if (!active) return;
          applyEvent(JSON.parse((event as MessageEvent).data) as RunStreamEvent);
        });
        stream.addEventListener("state", (event) => {
          if (!active) return;
          applyEvent(JSON.parse((event as MessageEvent).data) as RunStreamEvent);
        });
        for (const type of ["tool_started", "tool_completed", "tool_failed"]) {
          stream.addEventListener(type, (event) => {
            if (active) applyEvent(JSON.parse((event as MessageEvent).data) as RunStreamEvent);
          });
        }
        stream.addEventListener("artifact_published", (event) => {
          if (active) applyEvent(JSON.parse((event as MessageEvent).data) as RunStreamEvent);
        });
        stream.onerror = () => {
          stream?.close();
          stream = null;
          if (active) {
            setStreamFallback(true);
            retry = window.setTimeout(() => void connect(), 1500);
          }
        };
      } catch (failure) {
        if (active && !(failure instanceof ApiError && failure.status === 404)) {
          setStreamFallback(true);
          retry = window.setTimeout(() => void connect(), 2500);
        }
      }
    }
    void connect();
    return () => { active = false; stream?.close(); window.clearTimeout(retry); window.clearTimeout(fallbackTimer); };
  }, [activeRun?.id, sessionId]);

  const currentRun = activeRun || lastRun;
  const preview = streamText && (activeRun?.id === streamText.runId || (lastRun?.id === streamText.runId && lastRun.state === "completed")) && !messages.some((item) => item.role === "assistant" && item.run_id === streamText.runId) ? streamText : null;
  const pendingRunId = preview?.runId || (activeRun && !messages.some(item => item.role === "assistant" && item.run_id === activeRun.id) ? activeRun.id : null);
  const canSend = Boolean(selected && !selected.archived && agent?.enabled && !activeRun && !submitting && !pendingDraft && !switchingModel && modelTarget === null && !runsLoading);

  function refresh() { setRevision((value) => value + 1); }

  async function createSession() {
    const title = newTitle.trim() || "新对话";
    setBusy(true);
    setDirectoryError("");
    try {
      const created = await createAgentSession(agentId, title);
      setNewOpen(false);
      setNewTitle("");
      setArchived(false);
      setPage(1);
      refresh();
      navigate(`${basePath}/${created.id}`);
    } catch (failure) { setDirectoryError(errorText(failure)); }
    finally { setBusy(false); }
  }

  async function renameSession() {
    if (!renameTarget || !renameTitle.trim()) return;
    setBusy(true);
    setDirectoryError("");
    try {
      const updated = await updateAgentSession(agentId, renameTarget, { title: renameTitle.trim() });
      if (updated.id === currentSessionId.current) setSelected(updated);
      setRenameTarget(null);
      refresh();
    } catch (failure) { setDirectoryError(`${errorText(failure)}；请刷新会话后重试`); refresh(); }
    finally { setBusy(false); }
  }

  async function toggleArchive(target: AgentSession) {
    setBusy(true);
    setDirectoryError("");
    try {
      const updated = await updateAgentSession(agentId, target, { archived: !target.archived });
      if (updated.id === currentSessionId.current) setSelected(updated);
      refresh();
      message.success(updated.archived ? "会话已归档" : "会话已恢复");
    } catch (failure) { setDirectoryError(`${errorText(failure)}；请刷新会话后重试`); refresh(); }
    finally { setBusy(false); }
  }

  async function removeSession() {
    if (!deleteTarget) return;
    setBusy(true);
    setDirectoryError("");
    try {
      await deleteAgentSession(agentId, deleteTarget.id);
      if (deleteTarget.id === sessionId) navigate(basePath);
      setDeleteTarget(null);
      refresh();
      message.success("会话已删除");
    } catch (failure) { setDirectoryError(errorText(failure)); }
    finally { setBusy(false); }
  }

  async function switchModel() {
    if (!selected || modelTarget === null || activeRun || pendingDraft || switchingModel || runsLoading) return;
    const targetSessionId = selected.id;
    setSwitchingModel(true);
    setRunError("");
    try {
      const updated = await updateAgentSession(agentId, selected, { model_id: modelTarget });
      if (currentSessionId.current !== targetSessionId) return;
      setSelected(updated);
      setModelTarget(null);
      refresh();
      message.success("模型已切换，从下一条消息开始使用");
    } catch (failure) {
      if (currentSessionId.current !== targetSessionId) return;
      setRunError(errorText(failure));
      setModelTarget(null);
      setModelsRevision(value => value + 1);
      await Promise.allSettled([
        getAgentSession(agentId, targetSessionId).then(current => { if (currentSessionId.current === targetSessionId) setSelected(current); }),
        listSessionRuns(agentId, targetSessionId).then(result => {
          if (currentSessionId.current !== targetSessionId) return;
          setActiveRun(result.items.find(item => !terminalStates.has(item.state)) || null);
          setLastRun(result.items[0] || null);
        }),
      ]);
    } finally { setSwitchingModel(false); }
  }

  async function send(draft?: SessionRunDraft) {
    if (!selected) return;
    const targetSessionId = selected.id;
    const next = draft || { input_text: composer.trim(), request_key: crypto.randomUUID(), publish_answer_artifact: false, artifact_ids: files.flatMap(item => item.artifact ? [item.artifact.id] : []), context_artifact_ids: files.flatMap(item => item.context && item.artifact ? [item.artifact.id] : []), model_id: selected.model_id ?? undefined, expected_session_version: selected.version };
    if (!next.input_text || (!draft && attachmentPending)) return;
    followLatest();
    setSubmitting(true);
    setRunError("");
    try {
      if (!draft && next.artifact_ids.length) await checkSessionAttachments(agentId, targetSessionId, next);
      if (currentSessionId.current !== targetSessionId) return;
      setPendingDraft(next);
      const run = await submitSessionRun(agentId, targetSessionId, next);
      if (currentSessionId.current !== targetSessionId) return;
      setPendingDraft(null);
      setComposer("");
      setChatFiles(current => ({ ...current, [targetSessionId]: [] }));
      setActiveRun(run);
      setLastRun(run);
      setMessageRevision((value) => value + 1);
      refresh();
      void getAgentSession(agentId, targetSessionId).then((current) => { if (currentSessionId.current === targetSessionId) setSelected(current); }).catch((failure) => { if (currentSessionId.current === targetSessionId) setRunError(`会话状态刷新失败：${errorText(failure)}`); });
    } catch (failure) {
      if (currentSessionId.current !== targetSessionId) return;
      setRunError(errorText(failure));
      if (failure instanceof ApiError && failure.status === 409) {
        setPendingDraft(null);
        listSessionRuns(agentId, targetSessionId).then((result) => {
          if (currentSessionId.current !== targetSessionId) return;
          setActiveRun(result.items.find((item) => !terminalStates.has(item.state)) || null);
          setLastRun(result.items[0] || null);
        }).catch(() => undefined);
        getAgentSession(agentId, targetSessionId).then((current) => { if (currentSessionId.current === targetSessionId) setSelected(current); }).catch(() => undefined);
      }
    } finally { if (currentSessionId.current === targetSessionId) setSubmitting(false); }
  }

  async function stopRun() {
    if (!activeRun) return;
    const targetSessionId = sessionId;
    setBusy(true);
    setRunError("");
    try { const updated = await cancelRun(activeRun.id); if (currentSessionId.current === targetSessionId) setActiveRun(updated); }
    catch (failure) { if (currentSessionId.current === targetSessionId) setRunError(errorText(failure)); }
    finally { setBusy(false); }
  }

  function sessionMenu(item: AgentSession) {
    return [
      { key: "rename", label: "重命名", icon: <Pencil size={15} />, onClick: () => { setRenameTarget(item); setRenameTitle(item.title); } },
      { key: "archive", label: item.archived ? "恢复" : "归档", icon: item.archived ? <RotateCcw size={15} /> : <Archive size={15} />, onClick: () => void toggleArchive(item) },
      { key: "delete", label: "删除", danger: true, icon: <Trash2 size={15} />, onClick: () => setDeleteTarget(item) },
    ];
  }

  return <div className="workspace sessions-page">
    <div className="sessions-page-heading">
      <Button type="text" icon={<ArrowLeft size={18} />} onClick={() => navigate("/agents")}>Agent</Button>
      <div><h1>{agent?.name || "会话"}</h1><Typography.Text type="secondary">{agentError || "会话"}</Typography.Text></div>
    </div>
    {agentLoading ? <div className="page-loading"><Spin /></div> : agentError ? <Alert type="error" showIcon message={agentError} action={<Button size="small" onClick={() => navigate("/agents")}>返回 Agent</Button>} /> : <div className={`sessions-surface ${isMobile && sessionId ? "sessions-has-selection" : ""}`}>
      <aside className="sessions-rail" aria-label="会话列表">
        <div className="sessions-rail-top"><strong>会话</strong>{directoryLoading && directory.items.length > 0 && <Spin size="small" aria-label="正在更新会话列表" />}<Tooltip title="新建会话"><Button type="primary" icon={<Plus size={17} />} aria-label="新建会话" disabled={!agent?.enabled} onClick={() => setNewOpen(true)} /></Tooltip></div>
        <Input className="sessions-search" aria-label="搜索会话" prefix={<Search size={16} />} placeholder="搜索标题" value={searchInput} onChange={(event) => setSearchInput(event.target.value)} maxLength={128} allowClear />
        <Tabs activeKey={archived ? "archived" : "active"} onChange={(key) => { setArchived(key === "archived"); setPage(1); }} items={[{ key: "active", label: "进行中" }, { key: "archived", label: "已归档" }]} />
        {directoryError && <Alert className="sessions-alert" type="error" showIcon message={directoryError} action={<Button size="small" onClick={refresh}>重试</Button>} />}
        {directoryLoading && !directory.items.length ? <Skeleton active paragraph={{ rows: 5 }} /> : directory.items.length === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={search ? "没有符合条件的会话" : archived ? "没有已归档的会话" : "还没有会话"} /> : <div className="sessions-list">
          {directory.items.map((item) => <div className={`sessions-list-row ${sessionId === item.id ? "sessions-list-row-active" : ""}`} key={item.id}>
            <button type="button" className="sessions-list-open" onClick={() => navigate(`${basePath}/${item.id}`)}><MessageSquareText size={17} aria-hidden /><span><strong>{item.title}</strong><small>{displayTime(item.update_time)}</small></span></button>
            <Dropdown menu={{ items: sessionMenu(item) }} trigger={["click"]}><Tooltip title="会话操作"><Button type="text" icon={<MoreHorizontal size={16} />} aria-label={`${item.title} 操作`} disabled={busy} /></Tooltip></Dropdown>
          </div>)}
        </div>}
        {directory.total > directory.page_size && <Pagination className="sessions-pagination" size="small" current={page} pageSize={directory.page_size} total={directory.total} onChange={setPage} showSizeChanger={false} />}
      </aside>
      <section className="sessions-main" aria-label="当前会话">
        {!sessionId ? <div className="sessions-placeholder"><Empty description="选择或新建会话" /></div> : selectedLoading ? <div className="page-loading"><Spin /></div> : selectedError ? <Alert type="error" showIcon message={selectedError} action={<Button size="small" onClick={() => navigate(basePath)}>返回列表</Button>} /> : selected && <>
          <div className="sessions-current-header">
            {isMobile && <Tooltip title="返回会话列表"><Button type="text" icon={<ArrowLeft size={18} />} aria-label="返回会话列表" onClick={() => navigate(basePath)} /></Tooltip>}
            <div className="sessions-current-title"><strong>{selected.title}</strong>{selected.archived && <Tag>已归档</Tag>}</div>
            <div className="sessions-current-actions">
              <Tooltip title="刷新会话"><Button type="text" icon={<RefreshCw size={17} />} aria-label="刷新会话" onClick={() => {
                const targetSessionId = selected.id;
                setMessageRevision((value) => value + 1);
                void getAgentSession(agentId, targetSessionId).then((current) => {
                  if (currentSessionId.current === targetSessionId) setSelected(current);
                }).catch((failure) => {
                  if (currentSessionId.current === targetSessionId) setRunError(errorText(failure));
                });
              }} /></Tooltip>
              <Dropdown menu={{ items: sessionMenu(selected) }} trigger={["click"]}><Tooltip title="会话操作"><Button type="text" icon={<MoreHorizontal size={17} />} aria-label="当前会话操作" disabled={busy} /></Tooltip></Dropdown>
            </div>
          </div>
          <div className="sessions-history" ref={historyRef} onScroll={trackScroll}>
            <div className="sessions-history-content" ref={historyContentRef}>
            {isMobile && directoryError && <Alert className="sessions-alert" type="error" showIcon message={directoryError} action={<Button size="small" onClick={refresh}>重试</Button>} />}
            {messagesError && <Alert className="sessions-alert" type="error" showIcon message={messagesError} action={<Button size="small" onClick={() => setMessageRevision((value) => value + 1)}>重试</Button>} />}
            {messagesLoading && !messages.length && !preview ? <Skeleton active paragraph={{ rows: 4 }} /> : messages.length ? <MessageHistory items={messages} pendingRunId={pendingRunId} /> : !preview && !activeRun ? <div className="sessions-history-empty"><Empty description={agent?.welcome_message || "暂无消息"} /></div> : null}
            {pendingRunId && <div className="sessions-messages"><div className="session-message session-message-assistant" data-stream-run={pendingRunId}><div className="session-message-avatar"><Bot size={18} /></div><div className="session-message-main"><div className="session-message-meta"><strong>Agent</strong>{(currentRun?.model_id ?? selected.model_id) && <span className="session-message-model">{models.find(item => item.id === (currentRun?.model_id ?? selected.model_id))?.display_name || `模型 #${currentRun?.model_id ?? selected.model_id}`}</span>}</div><ActiveRunCalls key={pendingRunId} runId={pendingRunId} refreshKey={callsRevision} active={activeRun?.id === pendingRunId} />{preview && <><div className="session-message-content">{activeRun?.id === preview.runId ? <StreamingMessage key={preview.runId} runId={preview.runId} text={preview.text} onProgress={followContent} /> : <MessageContent text={preview.text} />}</div><MessageActions text={preview.text} assistant copyLabel="复制当前回复" /></>}</div></div></div>}
            {currentRun && <div className="sessions-run-status"><Tag color={terminalStates.has(currentRun.state) ? currentRun.state === "completed" ? "success" : "error" : "processing"}><RunStateLabel state={currentRun.state} /></Tag>{currentRun.failure_code && <span>{failureLabels[currentRun.failure_code] || currentRun.failure_code}</span>}{activeRun && <Button size="small" icon={<Square size={13} />} loading={busy} onClick={() => void stopRun()}>取消</Button>}</div>}
            {runError && <Alert className="sessions-alert" type="error" showIcon message={runError} />}
            </div>
          </div>
          {!following && <div className="sessions-jump"><Button icon={<ArrowDown size={16} />} onClick={followLatest}>回到最新消息</Button></div>}
          <div className="sessions-composer">
            <div className="sessions-model-bar">
              <label htmlFor="session-model">当前模型</label>
              <Select id="session-model" aria-label="当前对话模型" className="sessions-model-select" showSearch optionFilterProp="searchText"
                loading={modelsLoading} value={selected.model_id ?? undefined} placeholder="选择模型" popupMatchSelectWidth={false}
                disabled={selected.archived || !agent?.enabled || Boolean(activeRun) || submitting || busy || switchingModel || Boolean(pendingDraft) || runsLoading}
                onChange={(value: number) => { if (value !== selected.model_id) setModelTarget(value); }}
                options={[
                  ...(selected.model_id && !models.some(item => item.id === selected.model_id) ? [{ value: selected.model_id, label: `模型 #${selected.model_id}（目录不可用）`, disabled: true, searchText: "" }] : []),
                  ...models.map(item => ({ value: item.id, disabled: !item.selectable, searchText: `${item.provider_name} ${item.display_name}`,
                    label: <Tooltip title={item.notice || `${item.provider_name} · ${item.verified_capabilities.join(" / ")}`}><span className="sessions-model-option"><span>{item.display_name}</span><small>{item.provider_name}{item.notice ? ` · ${item.notice}` : ""}</small></span></Tooltip> })),
                ]} />
              <span className="sessions-model-hint">{activeRun ? "本轮结束后可切换" : "切换从下一条消息生效"}</span>
              {modelsError && <Tooltip title={modelsError}><Button size="small" onClick={() => setModelsRevision(value => value + 1)}>重试模型目录</Button></Tooltip>}
            </div>
            <ChatAttachments key={selected.id} items={files} disabled={!canSend} onChange={update => {
              const target = selected.id; setChatFiles(current => ({ ...current, [target]: update(current[target] || []) }));
            }} />
            {pendingDraft && <div className="sessions-retry"><Button size="small" loading={submitting} onClick={() => void send(pendingDraft)}>重试提交</Button><Button size="small" onClick={() => { setPendingDraft(null); setRunError(""); }}>放弃重试</Button></div>}
            <Input.TextArea aria-label="消息内容" value={composer} onChange={(event) => setComposer(event.target.value)} placeholder={selected.archived ? "会话已归档" : "输入消息"} rows={3} maxLength={32768} disabled={!canSend} />
            <div className="sessions-composer-footer"><span>{selected.archived ? "已归档" : activeRun ? <RunStateLabel state={activeRun.state} /> : ""}</span><Button type="primary" icon={<SendHorizontal size={17} />} loading={submitting} disabled={!canSend || !composer.trim() || attachmentPending} onClick={() => void send()}>发送</Button></div>
          </div>
        </>}
      </section>
    </div>}
    <Modal title="切换当前对话模型" open={modelTarget !== null} onCancel={() => { if (!switchingModel) setModelTarget(null); }}
      onOk={() => void switchModel()} okText="切换模型" cancelText="取消" confirmLoading={switchingModel}
      okButtonProps={{ disabled: Boolean(activeRun) || runsLoading || switchingModel }} cancelButtonProps={{ disabled: switchingModel }} closable={!switchingModel} maskClosable={!switchingModel}>
      <p>后续消息将使用 <strong>{models.find(item => item.id === modelTarget)?.display_name || `模型 #${modelTarget}`}</strong>，此选择只影响当前对话。</p>
      <p>新模型将收到已完成轮次的文字历史。跨供应商切换时，历史文字会发送给目标供应商；较长历史会摘要或仅携带最近部分。</p>
      <p>历史附件正文、图片和工具内部状态不会自动迁移。需要文件内容时请重新附加，或让模型使用文件工具读取已有产物。</p>
    </Modal>
    <Modal title="新建会话" open={newOpen} onCancel={() => setNewOpen(false)} onOk={() => void createSession()} okText="创建" okButtonProps={{ loading: busy }} cancelText="取消"><Input aria-label="会话标题" placeholder="新对话" maxLength={128} value={newTitle} onChange={(event) => setNewTitle(event.target.value)} onPressEnter={() => void createSession()} /></Modal>
    <Modal title="重命名会话" open={Boolean(renameTarget)} onCancel={() => setRenameTarget(null)} onOk={() => void renameSession()} okText="保存" okButtonProps={{ loading: busy, disabled: !renameTitle.trim() }} cancelText="取消"><Input aria-label="新标题" maxLength={128} value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} onPressEnter={() => void renameSession()} /></Modal>
    <Modal title={`删除 ${deleteTarget?.title || "会话"}？`} open={Boolean(deleteTarget)} onCancel={() => setDeleteTarget(null)} onOk={() => void removeSession()} okText="删除" okButtonProps={{ danger: true, loading: busy }} cancelText="取消"><Typography.Text type="secondary">删除后会话不再显示，运行中的会话无法删除。</Typography.Text></Modal>
  </div>;
}
