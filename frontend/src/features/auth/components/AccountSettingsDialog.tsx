import { useState } from "react";
import type { FormEvent } from "react";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/shared/ui/dialog";
import { Button } from "@/shared/ui/button";

type RoleSettings = {
  api_key: string;
  base_url: string;
  model_name: string;
};

type ModelSettingsPayload = {
  generation?: RoleSettings;
};

type AccountSettingsDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: "account" | "model";
  userLabel: string;
  initialNickname?: string | null;
  initialModelSettings?: Record<string, unknown> | null;
  isSubmitting?: boolean;
  isTestingModel?: boolean;
  statusMessage?: string;
  onSaveNickname: (nickname: string) => Promise<void>;
  onChangePassword: (payload: { current_password: string; new_password: string }) => Promise<void>;
  onSaveModelSettings: (payload: ModelSettingsPayload) => Promise<void>;
  onTestModelSettings: (generation: RoleSettings) => Promise<void>;
  onClearModelSettings: () => Promise<void>;
};

function parseRoleSettings(raw: unknown): RoleSettings {
  const source = raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {};
  return {
    api_key: String(source.api_key ?? ""),
    base_url: String(source.base_url ?? ""),
    model_name: String(source.model_name ?? ""),
  };
}

export function AccountSettingsDialog({
  open,
  onOpenChange,
  mode,
  userLabel,
  initialNickname,
  initialModelSettings,
  isSubmitting,
  isTestingModel,
  statusMessage,
  onSaveNickname,
  onChangePassword,
  onSaveModelSettings,
  onTestModelSettings,
  onClearModelSettings,
}: AccountSettingsDialogProps) {
  const initialSettings = (initialModelSettings ?? {}) as Record<string, unknown>;
  const [nickname, setNickname] = useState(() => String(initialNickname ?? ""));
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [generation, setGeneration] = useState<RoleSettings>(() => {
    const parsed = parseRoleSettings(initialSettings.generation);
    return {
      ...parsed,
      base_url: parsed.base_url || "https://api.deepseek.com/v1",
      model_name: parsed.model_name || "deepseek-chat",
    };
  });
  const [localError, setLocalError] = useState("");

  const handleNicknameSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLocalError("");
    await onSaveNickname(nickname.trim());
  };

  const handlePasswordSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLocalError("");
    if (!currentPassword || !newPassword || !confirmPassword) {
      setLocalError("请完整填写密码字段。");
      return;
    }
    if (newPassword !== confirmPassword) {
      setLocalError("两次新密码输入不一致。");
      return;
    }
    if (currentPassword === newPassword) {
      setLocalError("新密码不能与旧密码相同。");
      return;
    }
    await onChangePassword({
      current_password: currentPassword,
      new_password: newPassword,
    });
    setCurrentPassword("");
    setNewPassword("");
    setConfirmPassword("");
  };

  const handleModelSettingsSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setLocalError("");
    await onSaveModelSettings({
      generation,
    });
  };

  const dialogTitle = mode === "model" ? "模型设置" : "账号设置";
  const dialogDescription = mode === "model" ? "当前用户模型参数配置" : `当前用户：${userLabel}`;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="account-dialog">
        <DialogHeader>
          <DialogTitle>{dialogTitle}</DialogTitle>
          <DialogDescription>{dialogDescription}</DialogDescription>
        </DialogHeader>

        {mode === "account" ? (
          <>
            <form className="account-form-section" onSubmit={handleNicknameSubmit}>
              <h4>昵称</h4>
              <input
                className="auth-input"
                value={nickname}
                onChange={(event) => setNickname(event.target.value)}
                placeholder="留空可清除昵称"
                disabled={isSubmitting}
              />
              <div className="auth-actions">
                <Button type="submit" size="sm" disabled={isSubmitting}>
                  保存昵称
                </Button>
              </div>
            </form>

            <form className="account-form-section" onSubmit={handlePasswordSubmit}>
              <h4>修改密码</h4>
              <input
                className="auth-input"
                type="password"
                placeholder="当前密码"
                value={currentPassword}
                onChange={(event) => setCurrentPassword(event.target.value)}
                disabled={isSubmitting}
              />
              <input
                className="auth-input"
                type="password"
                placeholder="新密码"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
                disabled={isSubmitting}
              />
              <input
                className="auth-input"
                type="password"
                placeholder="重复新密码"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                disabled={isSubmitting}
              />
              <div className="auth-actions">
                <Button type="submit" size="sm" disabled={isSubmitting}>
                  更新密码
                </Button>
              </div>
            </form>
          </>
        ) : null}

        {mode === "model" ? (
          <form className="account-form-section" onSubmit={handleModelSettingsSubmit}>
            <h4>生成模型</h4>
            <p className="account-model-note">管理员可随时替换 OpenAI 兼容模型；保存后下一次对话立即生效。</p>
            <div className="account-grid">
              <label className="auth-label">
                <span>API Key</span>
                <input
                  className="auth-input"
                  type="password"
                  autoComplete="new-password"
                  value={generation.api_key}
                  onChange={(event) =>
                    setGeneration((previous) => ({ ...previous, api_key: event.target.value }))
                  }
                  disabled={isSubmitting}
                />
              </label>
              <label className="auth-label">
                <span>Base URL</span>
                <input
                  className="auth-input"
                  value={generation.base_url}
                  onChange={(event) =>
                    setGeneration((previous) => ({ ...previous, base_url: event.target.value }))
                  }
                  disabled={isSubmitting}
                />
              </label>
              <label className="auth-label">
                <span>模型名称</span>
                <input
                  className="auth-input"
                  value={generation.model_name}
                  onChange={(event) =>
                    setGeneration((previous) => ({ ...previous, model_name: event.target.value }))
                  }
                  disabled={isSubmitting}
                />
              </label>
            </div>
            <div className="auth-actions">
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={isSubmitting}
                onClick={() => void onClearModelSettings()}
              >
                清空设置
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                disabled={isSubmitting || isTestingModel}
                onClick={() => void onTestModelSettings(generation)}
              >
                {isTestingModel ? "正在测试…" : "测试连接"}
              </Button>
              <Button type="submit" size="sm" disabled={isSubmitting}>
                保存模型设置
              </Button>
            </div>
          </form>
        ) : null}

        {localError ? <p className="auth-error">{localError}</p> : null}
        {statusMessage ? <p className="auth-status">{statusMessage}</p> : null}
      </DialogContent>
    </Dialog>
  );
}
