import { Button, Tooltip, message } from "antd";
import { RotateCcw, Share2, ThumbsDown, ThumbsUp } from "lucide-react";
import { CopyButton } from "./MessageContent";

const pendingActions = [
  { label: "重新生成", Icon: RotateCcw },
  { label: "喜欢", Icon: ThumbsUp },
  { label: "不喜欢", Icon: ThumbsDown },
  { label: "分享", Icon: Share2 },
];

export default function MessageActions({ text, assistant, copyLabel = "复制消息" }: { text: string; assistant: boolean; copyLabel?: string }) {
  return <div className="session-message-actions" role="group" aria-label="消息操作">
    <CopyButton text={text} label={copyLabel} />
    {assistant && pendingActions.map(({ label, Icon }) => <Tooltip key={label} title={label} trigger={["hover", "focus"]}>
      <Button type="text" size="small" className="message-action" icon={<Icon size={14} />} aria-label={label}
        onClick={() => message.info({ key: "message-action-pending", content: `${label}功能待接入` })} />
    </Tooltip>)}
  </div>;
}
