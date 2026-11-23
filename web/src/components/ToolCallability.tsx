// -*- coding: utf-8 -*-
// @Time    : 2026-11-23
// @Author  : Codex
// @File    : ToolCallability.tsx

import { Alert, Tag, Tooltip, Typography } from "antd";
import type { ToolCallability } from "../api";

export const callabilityNotice = "这是配置与权限预检，不代表远端实时在线；提交任务及实际调用前仍会重新核对权限。";
const sources: Record<string, string> = { tool: "工具", connection: "连接", default: "默认", invalid_policy: "保守回退" };
const modes: Record<string, string> = { always: "每次审批", writes: "写入及未知操作审批", never: "不额外审批" };
const constraints: Record<string, string> = { non_idempotent_write: "非幂等写入", untrusted_read_declaration: "所有者只读确认需使用者审批", caller_requires_approval: "使用者要求审批", deployment_requires_approval: "部署要求审批" };

/** 统一生成工具目录与选择器的状态、修复动作及策略摘要。 */
export function callabilityText(value?: ToolCallability, includeMessage = true): string {
  if (!value) return "状态待核对";
  const policy = value.policy;
  return [includeMessage && value.message, value.action, policy && `策略：${sources[policy.source] || policy.source} · ${modes[policy.mode] || policy.mode}`,
    policy?.tool_policy_version != null && `工具策略 v${policy.tool_policy_version}`, policy?.connection_policy_version != null && `连接策略 v${policy.connection_policy_version}`,
    ...(policy?.constraints || []).map(item => constraints[item] || item)].filter(Boolean).join("；");
}

export function checkedTime(value?: string): string {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "尚未核对";
}

export function ToolCallabilityTag({ value, checkedAt }: { value?: ToolCallability; checkedAt?: string }) {
  return <Tooltip trigger="click" title={`${callabilityText(value)}；判断时间：${checkedTime(checkedAt)}；${callabilityNotice}`}>
    <Tag tabIndex={0} role="button" aria-label={`查看${value?.message || "工具状态"}详情`} onKeyDown={event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); event.currentTarget.click(); } }} color={!value ? "default" : value.status === "blocked" ? "error" : value.status === "approval_required" ? "warning" : "success"}>{value?.message || "状态待核对"}</Tag>
  </Tooltip>;
}

export function ToolCallabilityNotice({ value, version, connectionVersion, checkedAt }: {
  value?: ToolCallability; version: number; connectionVersion?: number | null; checkedAt?: string;
}) {
  return <Alert type={value?.status === "blocked" ? "warning" : "info"} showIcon
    message={`当前发布版本 v${version}：${value?.message || "状态待核对"}`}
    description={<><div>{callabilityText(value, false)}</div><div>工具 v{version}{connectionVersion != null ? ` · 固定连接配置 v${connectionVersion}` : ""} · 判断时间：{checkedTime(checkedAt)}</div>
      <Typography.Text type="secondary">{callabilityNotice} 此处展示已发布配置，尚未保存的修改将在保存后重新预检。</Typography.Text></>} />;
}
