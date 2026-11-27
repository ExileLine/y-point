import { useEffect, useId, useRef, useState } from "react";
import { Button, Popover, Switch } from "antd";
import { Shield, ShieldCheck, X } from "lucide-react";
import SessionApprovalPolicy from "./SessionApprovalPolicy";

export default function SessionApprovalOptions({ agentId, sessionId, revision, required, disabled, onChange }: {
  agentId: number; sessionId: string; revision: string; required: boolean; disabled: boolean; onChange: (value: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const frame = requestAnimationFrame(() => document.getElementById(panelId)?.focus());
    return () => cancelAnimationFrame(frame);
  }, [open, panelId]);

  function close() {
    setOpen(false);
    triggerRef.current?.focus();
  }

  return <Popover trigger="click" placement="top" open={open} onOpenChange={setOpen}
    rootClassName="sessions-approval-popover" content={
      <section id={panelId} className="sessions-approval-panel" role="dialog" aria-label="审批选项" tabIndex={-1}
        onKeyDown={event => { if (event.key === "Escape") { event.stopPropagation(); close(); } }}>
        <div className="sessions-approval-heading"><strong>工具审批</strong>
          <Button type="text" size="small" icon={<X size={16} />} aria-label="关闭审批选项" onClick={close} />
        </div>
        <label className="sessions-approval-toggle"><span><strong>本轮全部 MCP 调用需审批</strong><small>开启后，本轮每次 MCP 工具调用都需确认。</small></span>
          <Switch aria-label="本轮全部 MCP 调用需审批" checked={required} disabled={disabled} onChange={onChange} />
        </label>
        <SessionApprovalPolicy agentId={agentId} sessionId={sessionId} revision={revision} />
      </section>
    }>
    <Button ref={triggerRef} type="text" className={`sessions-approval-trigger${required ? " sessions-approval-trigger-active" : ""}`}
      icon={required ? <ShieldCheck size={16} /> : <Shield size={16} />} aria-label="审批选项" aria-haspopup="dialog"
      aria-expanded={open} aria-controls={open ? panelId : undefined}
      title={required ? "本轮需审批：全部 MCP 调用需确认" : "设置本轮审批与会话放行"}
      onKeyDown={event => { if (event.key === "Escape" && open) { event.stopPropagation(); close(); } }}>
      审批选项{required && <span className="sessions-approval-state">已开启</span>}
    </Button>
  </Popover>;
}
