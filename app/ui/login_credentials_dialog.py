import tkinter as tk
from tkinter import messagebox, ttk

from app.models import LoginCredentials
from app.services.login_code_service import current_login_code


def show_login_credentials(parent, credentials: LoginCredentials) -> tk.Toplevel:
    dialog = tk.Toplevel(parent)
    dialog.title("登录信息 · 复制到浏览器")
    dialog.transient(parent)
    dialog.resizable(False, False)
    # 只在打开时提升一次，避免持续置顶导致复制按钮操作时主窗口抢焦点。
    dialog.lift()
    dialog.focus_force()
    body = ttk.Frame(dialog, padding=12)
    body.pack(fill="both", expand=True)
    code = tk.StringVar(master=dialog)
    countdown = tk.StringVar(master=dialog)
    notice = tk.StringVar(master=dialog)
    display_values: list[tk.StringVar] = []
    timer = None

    def refresh_code():
        if not credentials.totp_secret:
            code.set("")
            countdown.set("未配置 2FA 密钥，请使用自己的验证器。")
            return ""
        try:
            value, remaining = current_login_code(credentials.totp_secret)
        except (ValueError, TypeError):
            code.set("")
            countdown.set("2FA 密钥无效，请编辑登录凭据。")
            return ""
        code.set(value)
        countdown.set(f"{remaining} 秒后更新")
        return value

    def copy_value(label, value):
        # 点击时重新计算，避免复制到刚刚过期的验证码。
        text = refresh_code() if label == "验证码" else value
        if text:
            dialog.clipboard_clear()
            dialog.clipboard_append(text)
            notice.set(f"已复制{label}")

    for index, (label, value) in enumerate((
        ("账号", credentials.email), ("密码", credentials.password), ("验证码", code),
    )):
        ttk.Label(body, text=label).grid(row=index, column=0, sticky="w", padx=(0, 8), pady=5)
        if index == 2:
            options = {"textvariable": value}
        else:
            display_value = tk.StringVar(master=dialog, value=value)
            display_values.append(display_value)
            options = {"textvariable": display_value}
        ttk.Entry(body, width=40, state="readonly", **options).grid(row=index, column=1, pady=5)
        ttk.Button(body, text="复制", command=lambda name=label, text=value: copy_value(name, text),
                   state=tk.NORMAL if (credentials.totp_secret if index == 2 else value) else tk.DISABLED).grid(row=index, column=2, padx=(8, 0))
    ttk.Label(body, textvariable=countdown).grid(row=3, column=1, sticky="w")
    ttk.Label(body, textvariable=notice).grid(row=4, column=1, sticky="w", pady=5)
    ttk.Label(body, text="请在浏览器中粘贴登录；关闭此窗口不取消登录。").grid(row=5, column=0, columnspan=3, sticky="w")

    def tick():
        nonlocal timer
        refresh_code()
        timer = dialog.after(250, tick)

    def cleanup(event):
        if event.widget is dialog and timer is not None:
            dialog.after_cancel(timer)

    dialog.bind("<Destroy>", cleanup, add="+")
    tick()
    return dialog


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
    ttk.Label(body, text="凭据以明文保存在当前账号 JSON，仅供本机登录使用。\n登录时可复制账号、密码和动态验证码。未配置 2FA 密钥时请使用自己的验证器。").grid(
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
