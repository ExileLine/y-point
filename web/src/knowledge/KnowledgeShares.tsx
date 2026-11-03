import { useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, List, Popconfirm, Select, Tag, message } from "antd";
import { UserPlus, UserRoundX } from "lucide-react";
import { ApiError, searchShareTargets, type AgentShare } from "../api";
import { grantKnowledgeShare, listKnowledgeShares, revokeKnowledgeShare } from "../knowledge-api";

export default function KnowledgeShares({ baseId, onAccessCheck }: { baseId: number; onAccessCheck: () => Promise<void> }) {
  const [users, setUsers] = useState<number[]>([]);
  const [options, setOptions] = useState<AgentShare[]>([]);
  const [selected, setSelected] = useState<number>();
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [searchError, setSearchError] = useState("");
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; void reload(); return () => { mounted.current = false; }; }, [baseId]);
  useEffect(() => {
    let active = true;
    setOptions([]); setSearchError("");
    if (query.trim().length < 2) { setSearching(false); return; }
    setSearching(true);
    const timer = window.setTimeout(() => {
      searchShareTargets(query.trim()).then((rows) => { if (active) setOptions(rows); })
        .catch((failure: unknown) => { if (active) setSearchError(failure instanceof Error ? failure.message : "用户搜索失败"); })
        .finally(() => { if (active) setSearching(false); });
    }, 250);
    return () => { active = false; window.clearTimeout(timer); };
  }, [query]);

  async function reload() {
    setLoading(true);
    try { const response = await listKnowledgeShares(baseId); if (mounted.current) { setUsers(response.user_ids); setError(""); } }
    catch (failure) { if (mounted.current) setError(failure instanceof Error ? failure.message : "授权名单加载失败"); }
    finally { if (mounted.current) setLoading(false); }
  }
  async function change(userId: number, revoke = false) {
    setBusy(true); setError("");
    try {
      if (revoke) await revokeKnowledgeShare(baseId, userId); else await grantKnowledgeShare(baseId, userId);
      if (!mounted.current) return;
      message.success(revoke ? "已撤销只读权限，旧引用也将重新鉴权" : "已授予只读查询权限");
      setSelected(undefined); setQuery(""); await reload();
    } catch (failure) {
      if (!mounted.current) return;
      setError(failure instanceof Error ? failure.message : "授权操作失败");
      if (failure instanceof ApiError && failure.status === 404) await onAccessCheck();
    } finally { if (mounted.current) setBusy(false); }
  }

  return <div className="kb-stack">
    <div><h2 className="kb-section-title">指定用户只读共享</h2><p className="kb-muted">接收方可以查看文档目录、检索和读取授权引用。配置、上传、任务与共享名单仅向所有者开放。</p></div>
    <Alert type="info" showIcon message="共享 Agent 不会同时共享知识库" description="需要让其他用户通过 Agent 使用这里的资料时，请同时授予该用户知识库只读权限。" />
    {error && <Alert type="error" showIcon message={error} action={<Button size="small" onClick={() => void reload()}>重试</Button>} />}
    {searchError && <Alert type="error" showIcon message={searchError} />}
    <div className="kb-share-form"><Select showSearch filterOption={false} aria-label="搜索共享用户" placeholder="输入至少 2 个字符搜索用户名" value={selected} loading={searching} onSearch={setQuery} onChange={setSelected}
      disabled={busy || loading} notFoundContent={searching ? "正在搜索…" : query ? "没有匹配用户" : "请输入用户名"}
      options={options.filter((item) => !users.includes(item.user_id)).map((item) => ({ value: item.user_id, label: `${item.username} · #${item.user_id}` }))} />
      <Button type="primary" icon={<UserPlus size={16} />} loading={busy} disabled={!selected || loading} onClick={() => selected && void change(selected)}>授权</Button></div>
    <List loading={loading} dataSource={error ? [] : users} locale={{ emptyText: error ? "授权名单暂不可用" : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未共享，只有自己可以访问" /> }} renderItem={(userId) => <List.Item actions={[
      <Popconfirm key="revoke" title={`撤销用户 #${userId} 的只读权限？`} description="该用户的新检索和旧引用访问将被阻断。" okText="撤销" cancelText="保留" onConfirm={() => change(userId, true)} disabled={busy}>
        <Button danger type="text" icon={<UserRoundX size={16} />} disabled={busy} aria-label={`撤销用户 ${userId} 的授权`}>撤销</Button>
      </Popconfirm>,
    ]}><span>用户 #{userId}</span><Tag>只读</Tag></List.Item>} />
  </div>;
}
