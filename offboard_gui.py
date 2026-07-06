from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import offboard_assistant as core
import sync_bundle


class OffboardGui(tk.Tk):
    def __init__(self, state_base: Path | None) -> None:
        super().__init__()
        self.title("Offboard Assistant")
        self.geometry("1180x760")
        self.minsize(980, 620)
        self.state_base = state_base or core.default_state_base()
        self.state_dir = core.state_dir_from_arg(str(state_base) if state_base else None)
        self.candidates: list[dict[str, Any]] = []
        self.selected_ids: set[str] = set()

        self._build_layout()
        self._load_sync_config()
        self.refresh_data(rescan=False)

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        notebook = ttk.Notebook(self)
        notebook.grid(row=0, column=0, sticky="nsew")

        self.dashboard = ttk.Frame(notebook, padding=10)
        self.sync_tab = ttk.Frame(notebook, padding=10)
        self.background_tab = ttk.Frame(notebook, padding=10)
        self.guide_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.dashboard, text="清理清单")
        notebook.add(self.sync_tab, text="云同步/导入导出")
        notebook.add(self.background_tab, text="后台任务")
        notebook.add(self.guide_tab, text="说明")

        self._build_dashboard()
        self._build_sync_tab()
        self._build_background_tab()
        self._build_guide_tab()

        self.status_var = tk.StringVar(value="Ready")
        status = ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(8, 4))
        status.grid(row=1, column=0, sticky="ew")

    def _build_dashboard(self) -> None:
        self.dashboard.columnconfigure(0, weight=1)
        self.dashboard.rowconfigure(1, weight=1)

        toolbar = ttk.Frame(self.dashboard)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ttk.Button(toolbar, text="刷新", command=lambda: self.refresh_data(rescan=False)).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="重新扫描", command=lambda: self.refresh_data(rescan=True)).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="生成报告", command=self.generate_report).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="导出选中清理清单", command=self.export_selected_plan).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="导出 AI 审核包", command=self.export_ai_review_pack).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="隔离选中推荐项", command=self.quarantine_selected_recommended).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="标记选中已处理", command=self.mark_selected_handled).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="打开状态目录", command=self.open_state_dir).pack(side="left")

        columns = ("selected", "type", "confidence", "title", "time", "detail")
        self.tree = ttk.Treeview(self.dashboard, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("selected", text="选中")
        self.tree.heading("type", text="类型")
        self.tree.heading("confidence", text="置信度")
        self.tree.heading("title", text="对象")
        self.tree.heading("time", text="时间")
        self.tree.heading("detail", text="说明")
        self.tree.column("selected", width=55, anchor="center", stretch=False)
        self.tree.column("type", width=160, stretch=False)
        self.tree.column("confidence", width=210, stretch=False)
        self.tree.column("title", width=280)
        self.tree.column("time", width=180, stretch=False)
        self.tree.column("detail", width=360)
        self.tree.grid(row=1, column=0, sticky="nsew")
        self.tree.bind("<Double-1>", self.toggle_current_selection)
        self.tree.bind("<space>", self.toggle_current_selection)

        scrollbar = ttk.Scrollbar(self.dashboard, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.summary_var = tk.StringVar(value="")
        ttk.Label(self.dashboard, textvariable=self.summary_var, anchor="w").grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def _build_sync_tab(self) -> None:
        self.sync_tab.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(self.sync_tab, text="WebDAV 地址").grid(row=row, column=0, sticky="w", pady=4)
        self.webdav_url_var = tk.StringVar()
        ttk.Entry(self.sync_tab, textvariable=self.webdav_url_var).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(self.sync_tab, text="远程文件名").grid(row=row, column=0, sticky="w", pady=4)
        self.remote_name_var = tk.StringVar(value="offboard-assistant.enc")
        ttk.Entry(self.sync_tab, textvariable=self.remote_name_var).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(self.sync_tab, text="用户名").grid(row=row, column=0, sticky="w", pady=4)
        self.webdav_user_var = tk.StringVar()
        ttk.Entry(self.sync_tab, textvariable=self.webdav_user_var).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(self.sync_tab, text="WebDAV 密码").grid(row=row, column=0, sticky="w", pady=4)
        self.webdav_password_var = tk.StringVar()
        ttk.Entry(self.sync_tab, textvariable=self.webdav_password_var, show="*").grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(self.sync_tab, text="加密口令").grid(row=row, column=0, sticky="w", pady=4)
        self.passphrase_var = tk.StringVar()
        ttk.Entry(self.sync_tab, textvariable=self.passphrase_var, show="*").grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        actions = ttk.Frame(self.sync_tab)
        actions.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 4))
        ttk.Button(actions, text="保存配置", command=self.save_sync_config).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="导出加密包", command=self.export_bundle).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="导入加密包", command=self.import_bundle).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="上传到 WebDAV", command=self.upload_bundle).pack(side="left", padx=(0, 6))
        ttk.Button(actions, text="从 WebDAV 下载", command=self.download_bundle).pack(side="left")

        row += 1
        crypto_text = "可用" if sync_bundle.crypto_available() else "不可用：安装 cryptography 后启用"
        note = (
            f"加密模块状态：{crypto_text}\n\n"
            "不会保存 WebDAV 密码或加密口令。上传前会先生成 .enc 加密包，云端只保存密文。\n"
            "坚果云一般使用 WebDAV 地址，例如 https://dav.jianguoyun.com/dav/你的目录。"
        )
        ttk.Label(self.sync_tab, text=note, justify="left").grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 0))

    def _build_background_tab(self) -> None:
        self.background_tab.columnconfigure(0, weight=1)
        description = (
            "后台任务使用 Windows 计划任务，行为可见、可删除。\n"
            "安装监听任务：用户登录后启动，低频比较安装相关指纹。\n"
            "每日扫描任务：每天固定时间生成最新快照。\n"
            "这些任务不会读取明文密码、聊天正文、Cookie 或 token 值。"
        )
        ttk.Label(self.background_tab, text=description, justify="left").grid(row=0, column=0, sticky="w")

        settings = ttk.Frame(self.background_tab)
        settings.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        settings.columnconfigure(1, weight=1)
        ttk.Label(settings, text="安装监听间隔秒数").grid(row=0, column=0, sticky="w", pady=4)
        self.bg_interval_var = tk.StringVar(value="60")
        ttk.Entry(settings, textvariable=self.bg_interval_var, width=12).grid(row=0, column=1, sticky="w", pady=4)
        ttk.Label(settings, text="每日扫描时间 HH:MM").grid(row=1, column=0, sticky="w", pady=4)
        self.daily_time_var = tk.StringVar(value="09:00")
        ttk.Entry(settings, textvariable=self.daily_time_var, width=12).grid(row=1, column=1, sticky="w", pady=4)

        buttons = ttk.Frame(self.background_tab)
        buttons.grid(row=2, column=0, sticky="w", pady=(6, 8))
        ttk.Button(buttons, text="创建安装监听任务", command=self.create_watch_task).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="创建每日扫描任务", command=self.create_daily_scan_task).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="删除后台任务", command=self.delete_background_tasks).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="查看任务状态", command=self.query_background_tasks).pack(side="left")

        self.bg_output = tk.Text(self.background_tab, wrap="word", height=16)
        self.bg_output.grid(row=3, column=0, sticky="nsew")
        self.background_tab.rowconfigure(3, weight=1)

    def _build_guide_tab(self) -> None:
        text = tk.Text(self.guide_tab, wrap="word", height=20)
        text.pack(fill="both", expand=True)
        text.insert(
            "1.0",
            "安全边界\n"
            "- 不显示明文密码，不解密浏览器密码，不读取聊天正文。\n"
            "- 浏览器账号只显示域名、脱敏用户名、创建/修改/使用时间和数据库位置。\n"
            "- 聊天软件只显示数据目录位置，不展示聊天内容。\n"
            "- 勾选项默认用于生成清理清单或标记处理；自动删除应在确认策略后逐项开放。\n\n"
            "推荐流程\n"
            "1. 入职时先运行 CLI 的 init 建立基线。\n"
            "2. 日常用 watch-install 低频监听安装行为。\n"
            "3. 离职时在本窗口重新扫描，勾选候选项，导出清理清单或 AI 审核包。\n"
            "4. 加密导出后可上传到坚果云；家用电脑下载后导入继续查看。\n",
        )
        text.configure(state="disabled")

    def _load_sync_config(self) -> None:
        config = sync_bundle.load_sync_config(self.state_dir)
        self.webdav_url_var.set(config.get("webdav_url", ""))
        self.webdav_user_var.set(config.get("username", ""))
        self.remote_name_var.set(config.get("remote_name", "offboard-assistant.enc"))

    def save_sync_config(self) -> None:
        sync_bundle.save_sync_config(
            self.state_dir,
            {
                "webdav_url": self.webdav_url_var.get().strip(),
                "username": self.webdav_user_var.get().strip(),
                "remote_name": self.remote_name_var.get().strip() or "offboard-assistant.enc",
            },
        )
        self.set_status("同步配置已保存。密码和加密口令不会保存。")

    def refresh_data(self, rescan: bool) -> None:
        try:
            baseline_path = self.state_dir / core.BASELINE_FILE
            if not baseline_path.exists():
                self.candidates = []
                self.render_tree()
                self.set_status(f"未找到基线。请点击重新扫描前先建立基线。状态目录：{self.state_dir}")
                return
            baseline = core.read_json(baseline_path)
            snapshot_path = self.state_dir / core.SNAPSHOT_FILE
            if rescan or not snapshot_path.exists():
                snapshot = core.collect_snapshot(self.state_dir, core.default_scan_roots())
                core.write_json(snapshot_path, snapshot)
            else:
                snapshot = core.read_json(snapshot_path)
            since = core.parse_since(baseline.get("baseline_since") or baseline.get("generated_at"))
            self.candidates = core.diff_items(snapshot.get("items", []), baseline.get("items", []), since)
            for event in core.install_events_since(self.state_dir / core.INSTALL_EVENTS_FILE, since):
                item = dict(event)
                item["cleanup_confidence"] = "monitor_install_activity"
                item["exists_in_baseline"] = False
                item["after_since"] = True
                self.candidates.append(item)
            self.render_tree()
            self.set_status(f"已加载 {len(self.candidates)} 个候选项。状态目录：{self.state_dir}")
        except Exception as exc:
            messagebox.showerror("刷新失败", str(exc))

    def render_tree(self) -> None:
        for row in self.tree.get_children():
            self.tree.delete(row)
        for item in self.candidates:
            item_id = str(item.get("id"))
            title, detail, when = describe_item(item)
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    "✓" if item_id in self.selected_ids else "",
                    item.get("type", ""),
                    item.get("cleanup_confidence", ""),
                    title,
                    when,
                    detail,
                ),
            )
        counts: dict[str, int] = {}
        for item in self.candidates:
            counts[str(item.get("type", "unknown"))] = counts.get(str(item.get("type", "unknown")), 0) + 1
        summary = " | ".join(f"{key}: {value}" for key, value in sorted(counts.items())) or "无候选项"
        self.summary_var.set(summary)

    def toggle_current_selection(self, _event: object | None = None) -> None:
        focused = self.tree.focus()
        if not focused:
            return
        if focused in self.selected_ids:
            self.selected_ids.remove(focused)
        else:
            self.selected_ids.add(focused)
        self.render_tree()

    def selected_items(self) -> list[dict[str, Any]]:
        return [item for item in self.candidates if str(item.get("id")) in self.selected_ids]

    def generate_report(self) -> None:
        try:
            baseline = core.read_json(self.state_dir / core.BASELINE_FILE)
            snapshot = core.read_json(self.state_dir / core.SNAPSHOT_FILE)
            since = core.parse_since(baseline.get("baseline_since") or baseline.get("generated_at"))
            report = core.render_report(since, snapshot, self.candidates)
            path = self.state_dir / core.REPORT_FILE
            path.write_text(report, encoding="utf-8")
            self.set_status(f"报告已生成：{path}")
        except Exception as exc:
            messagebox.showerror("生成报告失败", str(exc))

    def export_selected_plan(self) -> None:
        items = self.selected_items()
        if not items:
            messagebox.showinfo("没有选中项", "请先双击候选项进行勾选。")
            return
        path = filedialog.asksaveasfilename(
            title="保存清理清单",
            defaultextension=".md",
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt")],
        )
        if not path:
            return
        actions = core.cleanup_actions_for_items(items)
        Path(path).write_text(core.render_cleanup_actions_markdown(actions), encoding="utf-8")
        self.set_status(f"选中清理清单已导出：{path}")

    def export_ai_review_pack(self) -> None:
        items = self.selected_items() or self.candidates
        if not items:
            messagebox.showinfo("没有候选项", "当前没有可导出的候选项。")
            return
        path = filedialog.asksaveasfilename(
            title="保存 AI 审核包",
            defaultextension=".json",
            initialfile="ai-review-payload.json",
            filetypes=[("JSON", "*.json")],
        )
        if not path:
            return
        payload = core.ai_review_payload_for_items(items)
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.set_status(f"AI 审核包已导出：{path}")

    def quarantine_selected_recommended(self) -> None:
        items = self.selected_items()
        if not items:
            messagebox.showinfo("没有选中项", "请先双击候选项进行勾选。")
            return
        targets: dict[str, dict[str, Any]] = {}
        skipped = 0
        for item in items:
            target = core.recommended_cleanup_target(item)
            if not target:
                skipped += 1
                continue
            targets[target] = item
        if not targets:
            messagebox.showinfo("没有可隔离项", "只有推荐清理的临时/缓存类项目支持直接隔离。API key、聊天目录、浏览器账号需要人工确认。")
            return
        message = (
            f"将移动 {len(targets)} 个文件/目录到隔离区，跳过 {skipped} 个不适合自动处理的项目。\n\n"
            "这不是永久删除，但会让原路径不可用。请先关闭相关程序。\n\n"
            "是否继续？"
        )
        if not messagebox.askyesno("确认隔离", message):
            return
        manifest_rows: list[dict[str, Any]] = []
        quarantine_root = self.state_dir / "quarantine" / dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        quarantine_root.mkdir(parents=True, exist_ok=True)
        errors: list[str] = []
        for index, (target_text, item) in enumerate(targets.items(), start=1):
            source = Path(target_text)
            if not source.exists():
                errors.append(f"不存在：{source}")
                continue
            destination = quarantine_root / f"{index:03d}-{source.name}"
            try:
                shutil.move(str(source), str(destination))
                manifest_rows.append(
                    {
                        "source": str(source),
                        "destination": str(destination),
                        "item_id": item.get("id"),
                        "category": item.get("category"),
                        "moved_at": core.utc_now(),
                    }
                )
            except OSError as exc:
                errors.append(f"{source}: {exc}")
        manifest_path = quarantine_root / "manifest.json"
        manifest_path.write_text(json.dumps({"items": manifest_rows, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
        if errors:
            messagebox.showwarning("部分隔离失败", "\n".join(errors[:10]))
        self.selected_ids.clear()
        self.refresh_data(rescan=True)
        self.set_status(f"已隔离 {len(manifest_rows)} 项到：{quarantine_root}")

    def mark_selected_handled(self) -> None:
        items = self.selected_items()
        if not items:
            messagebox.showinfo("没有选中项", "请先双击候选项进行勾选。")
            return
        path = self.state_dir / "handled-items.json"
        existing = core.read_json(path) if path.exists() else {"items": []}
        handled = existing.get("items", [])
        for item in items:
            handled.append({"id": item.get("id"), "type": item.get("type"), "title": describe_item(item)[0], "handled_at": core.utc_now()})
        core.write_json(path, {"items": handled})
        self.selected_ids.clear()
        self.render_tree()
        self.set_status(f"已标记 {len(items)} 项为已处理。")

    def export_bundle(self) -> Path | None:
        path_text = filedialog.asksaveasfilename(
            title="导出加密包",
            defaultextension=".enc",
            initialfile=self.remote_name_var.get() or "offboard-assistant.enc",
            filetypes=[("Encrypted bundle", "*.enc"), ("All files", "*.*")],
        )
        if not path_text:
            return None
        try:
            sync_bundle.export_encrypted_bundle(self.state_dir, Path(path_text), self.passphrase_var.get())
            self.set_status(f"加密包已导出：{path_text}")
            return Path(path_text)
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
            return None

    def import_bundle(self) -> None:
        path_text = filedialog.askopenfilename(title="导入加密包", filetypes=[("Encrypted bundle", "*.enc"), ("All files", "*.*")])
        if not path_text:
            return
        try:
            imported = sync_bundle.import_encrypted_bundle(Path(path_text), self.state_dir, self.passphrase_var.get())
            self.set_status(f"已导入：{', '.join(imported)}")
            self.refresh_data(rescan=False)
        except Exception as exc:
            messagebox.showerror("导入失败", str(exc))

    def upload_bundle(self) -> None:
        temp_path = self.state_dir / (self.remote_name_var.get().strip() or "offboard-assistant.enc")
        try:
            sync_bundle.export_encrypted_bundle(self.state_dir, temp_path, self.passphrase_var.get())
            sync_bundle.webdav_upload(
                self.webdav_url_var.get().strip(),
                self.remote_name_var.get().strip() or "offboard-assistant.enc",
                self.webdav_user_var.get().strip(),
                self.webdav_password_var.get(),
                temp_path,
            )
            self.save_sync_config()
            self.set_status("加密包已上传到 WebDAV。")
        except Exception as exc:
            messagebox.showerror("上传失败", str(exc))

    def download_bundle(self) -> None:
        temp_path = self.state_dir / (self.remote_name_var.get().strip() or "offboard-assistant.enc")
        try:
            sync_bundle.webdav_download(
                self.webdav_url_var.get().strip(),
                self.remote_name_var.get().strip() or "offboard-assistant.enc",
                self.webdav_user_var.get().strip(),
                self.webdav_password_var.get(),
                temp_path,
            )
            imported = sync_bundle.import_encrypted_bundle(temp_path, self.state_dir, self.passphrase_var.get())
            self.save_sync_config()
            self.set_status(f"已从 WebDAV 下载并导入：{', '.join(imported)}")
            self.refresh_data(rescan=False)
        except Exception as exc:
            messagebox.showerror("下载失败", str(exc))

    def open_state_dir(self) -> None:
        try:
            if sys.platform.startswith("win"):
                subprocess.Popen(["explorer", str(self.state_dir)])
            else:
                subprocess.Popen(["xdg-open", str(self.state_dir)])
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc))

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def app_invocation(self) -> list[str]:
        if getattr(sys, "frozen", False):
            return [sys.executable]
        return [sys.executable, str(Path(__file__).resolve())]

    def task_command(self, mode: str) -> str:
        args = self.app_invocation() + ["--state-dir", str(self.state_base.resolve()), mode]
        if mode == "--background-watch-install":
            args.extend(["--interval", self.bg_interval_var.get().strip() or "60", "--iterations", "720"])
        return subprocess.list2cmdline(args)

    def run_schtasks(self, args: list[str]) -> None:
        try:
            completed = subprocess.run(["schtasks"] + args, capture_output=True, text=True, timeout=30)
            output = (completed.stdout or "") + (completed.stderr or "")
            self.bg_output.insert("end", f"> schtasks {' '.join(args)}\n{output}\n")
            self.bg_output.see("end")
            if completed.returncode != 0:
                messagebox.showerror("计划任务失败", output or f"Exit code {completed.returncode}")
        except Exception as exc:
            messagebox.showerror("计划任务失败", str(exc))

    def create_watch_task(self) -> None:
        command = self.task_command("--background-watch-install")
        self.run_schtasks([
            "/Create",
            "/F",
            "/SC",
            "ONLOGON",
            "/TN",
            "OffboardAssistantInstallWatch",
            "/TR",
            command,
        ])

    def create_daily_scan_task(self) -> None:
        command = self.task_command("--background-scan")
        start_time = self.daily_time_var.get().strip() or "09:00"
        self.run_schtasks([
            "/Create",
            "/F",
            "/SC",
            "DAILY",
            "/ST",
            start_time,
            "/TN",
            "OffboardAssistantDailyScan",
            "/TR",
            command,
        ])

    def delete_background_tasks(self) -> None:
        self.run_schtasks(["/Delete", "/F", "/TN", "OffboardAssistantInstallWatch"])
        self.run_schtasks(["/Delete", "/F", "/TN", "OffboardAssistantDailyScan"])

    def query_background_tasks(self) -> None:
        self.run_schtasks(["/Query", "/TN", "OffboardAssistantInstallWatch"])
        self.run_schtasks(["/Query", "/TN", "OffboardAssistantDailyScan"])


def describe_item(item: dict[str, Any]) -> tuple[str, str, str]:
    item_type = str(item.get("type", ""))
    when = (
        item.get("created_at")
        or item.get("detected_at")
        or item.get("install_date")
        or item.get("password_modified_at")
        or item.get("modified_at")
        or ""
    )
    if item_type == "browser_login_metadata":
        return (
            f"{item.get('browser')} / {item.get('profile')} / {item.get('origin')}",
            f"username={item.get('username_masked') or 'not_recorded'}; db={item.get('database_path')}",
            str(when),
        )
    if item_type == "installed_app":
        return (
            str(item.get("name") or "unknown app"),
            f"version={item.get('version') or 'unknown'}; location={item.get('install_location') or 'unknown'}",
            str(when),
        )
    if item_type == "environment_variable":
        return (f"{item.get('scope')}:{item.get('name')}", "value_recorded=false", str(when))
    if item_type == "sensitive_file_location":
        findings = item.get("secret_findings") or []
        kinds = sorted({finding.get("kind") for finding in findings if finding.get("kind")})
        detail = f"contents_recorded=false; secret_findings={len(findings)}"
        if kinds:
            detail += f"; kinds={', '.join(kinds)}"
        return (str(item.get("path") or ""), detail, str(when))
    if item_type == "chat_data_location":
        return (f"{item.get('app')} - {item.get('path')}", "contents_recorded=false", str(when))
    if item_type == "install_activity_event":
        signals = item.get("signals", {})
        paths = item.get("install_paths") or []
        return (
            f"install activity at {item.get('detected_at')}",
            f"new_apps={len(signals.get('new_apps', []))}; paths={'; '.join(paths[:5]) if paths else 'unknown'}",
            str(when),
        )
    return (str(item.get("id") or "unknown"), json.dumps(item, ensure_ascii=False)[:240], str(when))


def main() -> int:
    parser = argparse.ArgumentParser(description="Offboard Assistant desktop GUI.")
    parser.add_argument("--state-dir", help="Directory containing .offboard-assistant. Default: %%APPDATA%%\\OffboardAssistant.")
    parser.add_argument("--background-watch-install", action="store_true", help="Run install monitor without opening GUI.")
    parser.add_argument("--background-scan", action="store_true", help="Run one snapshot scan without opening GUI.")
    parser.add_argument("--watch-dir", action="append", default=[], help="Directory to watch in background install mode.")
    parser.add_argument("--interval", type=int, default=60, help="Install monitor polling interval.")
    parser.add_argument("--iterations", type=int, help="Install monitor iterations.")
    args = parser.parse_args()
    if args.background_watch_install:
        args.once = False
        return core.command_watch_install(args)
    if args.background_scan:
        args.scan_root = []
        return core.command_scan(args)
    app = OffboardGui(Path(args.state_dir) if args.state_dir else None)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
