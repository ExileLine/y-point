import { useState } from "react";
import { Alert, Button, Form, Input } from "antd";
import { LockKeyhole, UserRound } from "lucide-react";
import { login, type User } from "./api";

type Credentials = { identifier: string; password: string };

export default function Login({ onSuccess }: { onSuccess: (user: User) => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(values: Credentials) {
    setBusy(true);
    setError("");
    try {
      onSuccess(await login(values.identifier, values.password));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login-page">
      <div className="login-panel">
        <div className="login-brand"><img src="/brand-icon.png" alt="" className="login-icon" />
        <img src="/brand-wordmark.png" alt="Y-Point" className="login-wordmark" /></div>
        <h1>登录</h1>
        <p className="login-description">进入你的工作区，连接 Agent、知识与工具。</p>
        {error && <Alert type="error" showIcon message={error} className="login-error" />}
        <Form<Credentials> layout="vertical" onFinish={submit} requiredMark={false} disabled={busy}>
          <Form.Item name="identifier" label="用户名或邮箱" rules={[{ required: true, message: "请输入用户名或邮箱" }]}>
            <Input prefix={<UserRound size={17} />} autoComplete="username" />
          </Form.Item>
          <Form.Item name="password" label="密码" rules={[{ required: true, message: "请输入密码" }]}>
            <Input.Password prefix={<LockKeyhole size={17} />} autoComplete="current-password" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={busy}>登录</Button>
        </Form>
        <p className="login-caption">Y-Point · 让知识与行动连接起来</p>
      </div>
    </main>
  );
}
