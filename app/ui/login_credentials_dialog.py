import tkinter as tk
from tkinter import messagebox, ttk

from app.models import LoginCredentials


def edit_login_credentials(parent, initial: LoginCredentials, save) -> None:
    dialog = tk.Toplevel(parent)
    dialog.title("账号登录凭据")
    dialog.transient(parent)
    dialog.resizable(False, False)
    body = ttk.Frame(dialog, padding=12)
    body.pack(fill="both", expand=True)
    variables = []
    for index, (label, value, masked) in enumerate((
        ("登录邮箱", initial.email, False),
        ("登录密码", initial.password, True),
        ("2FA 密钥（验证器原始密钥）", initial.totp_secret, True),
    )):
        ttk.Label(body, text=label).grid(row=index * 2, column=0, sticky="w")
        variable = tk.StringVar(value=value)
        variables.append(variable)
        ttk.Entry(body, textvariable=variable, width=52, show="*" if masked else "").grid(
            row=index * 2 + 1, column=0, sticky="ew", pady=(4, 10),
        )
    ttk.Label(body, text="凭据以明文保存在当前账号 JSON，仅供本机登录使用。\n未配置 2FA 密钥时，验证码需要手动输入。未配置邮箱或密码时使用手动登录。").grid(
        row=6, column=0, sticky="w", pady=(0, 10),
    )

    def commit(clear=False):
        credentials = LoginCredentials() if clear else LoginCredentials.from_dict(dict(zip(
            ("email", "password", "totp_secret"), (variable.get() for variable in variables),
        )))
        try:
            credentials.validate()
            ok, error = save(credentials)
            if not ok:
                raise ValueError(error)
        except ValueError as exc:
            messagebox.showerror("保存登录凭据失败", str(exc), parent=dialog)
            return
        dialog.destroy()

    actions = ttk.Frame(body)
    actions.grid(row=7, column=0, sticky="e")
    ttk.Button(actions, text="清除凭据", command=lambda: commit(True)).pack(side="left", padx=4)
    ttk.Button(actions, text="取消", command=dialog.destroy).pack(side="left", padx=4)
    ttk.Button(actions, text="保存", command=commit).pack(side="left", padx=4)
    dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
    dialog.grab_set()
    dialog.wait_window()
