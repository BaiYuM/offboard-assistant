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

import ai_reviewer
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
        self.sort_column = "recommendation"
        self.sort_reverse = False

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
        self.ai_tab = ttk.Frame(notebook, padding=10)
        self.background_tab = ttk.Frame(notebook, padding=10)
        self.guide_tab = ttk.Frame(notebook, padding=10)
        notebook.add(self.dashboard, text="清理清单")
        notebook.add(self.sync_tab, text="云同步/导入导出")
        notebook.add(self.ai_tab, text="AI 审核")
        notebook.add(self.background_tab, text="后台任务")
        notebook.add(self.guide_tab, text="说明")

        self._build_dashboard()
        self._build_sync_tab()
        self._build_ai_tab()
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

        ttk.Label(toolbar, text="基线日期").pack(side="left", padx=(0, 4))
        self.baseline_since_var = tk.StringVar(value="2026-03-15")
        ttk.Entry(toolbar, textvariable=self.baseline_since_var, width=12).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="建立/覆盖基线", command=self.init_baseline_from_gui).pack(side="left", padx=(0, 10))
        ttk.Button(toolbar, text="刷新", command=lambda: self.refresh_data(rescan=False)).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="重新扫描", command=lambda: self.refresh_data(rescan=True)).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="生成报告", command=self.generate_report).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="导出选中清理清单", command=self.export_selected_plan).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="导出 AI 审核包", command=self.export_ai_review_pack).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="隔离选中推荐项", command=self.quarantine_selected_recommended).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="标记选中已处理", command=self.mark_selected_handled).pack(side="left", padx=(0, 6))
        ttk.Button(toolbar, text="打开状态目录", command=self.open_state_dir).pack(side="left")

        columns = ("selected", "category", "recommendation", "type", "confidence", "title", "time", "detail")
        self.tree = ttk.Treeview(self.dashboard, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("selected", text="选中", command=lambda: self.sort_by("selected"))
        self.tree.heading("category", text="分类", command=lambda: self.sort_by("category"))
        self.tree.heading("recommendation", text="推荐", command=lambda: self.sort_by("recommendation"))
        self.tree.heading("type", text="类型", command=lambda: self.sort_by("type"))
        self.tree.heading("confidence", text="置信度", command=lambda: self.sort_by("confidence"))
        self.tree.heading("title", text="对象", command=lambda: self.sort_by("title"))
        self.tree.heading("time", text="时间", command=lambda: self.sort_by("time"))
        self.tree.heading("detail", text="说明", command=lambda: self.sort_by("detail"))
        self.tree.column("selected", width=55, anchor="center", stretch=False)
        self.tree.column("category", width=150, stretch=False)
        self.tree.column("recommendation", width=160, stretch=False)
        self.tree.column("type", width=150, stretch=False)
        self.tree.column("confidence", width=190, stretch=False)
        self.tree.column("title", width=260)
        self.tree.column("time", width=180, stretch=False)
        self.tree.column("detail", width=320)
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

    def _build_ai_tab(self) -> None:
        self.ai_tab.columnconfigure(1, weight=1)
        row = 0
        ttk.Label(self.ai_tab, text="Base URL").grid(row=row, column=0, sticky="w", pady=4)
        self.ai_base_url_var = tk.StringVar(value=ai_reviewer.DEFAULT_BASE_URL)
        ttk.Entry(self.ai_tab, textvariable=self.ai_base_url_var).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(self.ai_tab, text="模型").grid(row=row, column=0, sticky="w", pady=4)
        self.ai_model_var = tk.StringVar(value=ai_reviewer.DEFAULT_MODEL)
        model_frame = ttk.Frame(self.ai_tab)
        model_frame.grid(row=row, column=1, sticky="ew", pady=4)
        model_frame.columnconfigure(0, weight=1)
        self.ai_model_combo = ttk.Combobox(model_frame, textvariable=self.ai_model_var)
        self.ai_model_combo.grid(row=0, column=0, sticky="ew")
        ttk.Button(model_frame, text="获取模型列表", command=self.fetch_ai_models).grid(row=0, column=1, padx=(6, 0))

        row += 1
        ttk.Label(self.ai_tab, text="API Key").grid(row=row, column=0, sticky="w", pady=4)
        self.ai_api_key_var = tk.StringVar()
        ttk.Entry(self.ai_tab, textvariable=self.ai_api_key_var, show="*").grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        ttk.Label(self.ai_tab, text="勾选策略").grid(row=row, column=0, sticky="w", pady=4)
        self.ai_selection_policy_var = tk.StringVar(value="离职模式：勾选 select + review")
        ttk.Combobox(
            self.ai_tab,
            textvariable=self.ai_selection_policy_var,
            values=[
                "保守模式：只勾选 select",
                "离职模式：勾选 select + review",
                "激进模式：勾选 select + review + 低风险 keep",
            ],
            state="readonly",
        ).grid(row=row, column=1, sticky="ew", pady=4)

        row += 1
        buttons = ttk.Frame(self.ai_tab)
        buttons.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 6))
        ttk.Button(buttons, text="审核全部候选项并自动勾选", command=lambda: self.run_ai_review(use_selected=False)).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(buttons, text="只审核已勾选项", command=lambda: self.run_ai_review(use_selected=True)).pack(
            side="left", padx=(0, 6)
        )
        ttk.Button(buttons, text="清空 AI 勾选", command=self.clear_selection).pack(side="left")

        row += 1
        note = (
            "AI 审核会把脱敏元数据发送到你配置的 API：路径、分类、密钥类型、脱敏摘要、时间等。\n"
            "不会发送明文 API key、密码、Cookie 或聊天正文。API Key 只在内存中使用，不保存。\n"
            "AI 只负责推荐勾选和总结，隔离/清理仍需要你手动确认。\n"
            "离职模式会把 review 项也勾上，适合统一处理离职残留。"
        )
        ttk.Label(self.ai_tab, text=note, justify="left").grid(row=row, column=0, columnspan=2, sticky="w")

        row += 1
        self.ai_output = tk.Text(self.ai_tab, wrap="word", height=18)
        self.ai_output.grid(row=row, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        self.ai_tab.rowconfigure(row, weight=1)

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
            if baseline.get("baseline_since"):
                self.baseline_since_var.set(str(baseline.get("baseline_since", ""))[:10])
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
        for item in self.sorted_candidates():
            item_id = str(item.get("id"))
            title, detail, when = describe_item(item)
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    "✓" if item_id in self.selected_ids else "",
                    item.get("category_label") or default_category_label(item),
                    item.get("recommendation") or default_recommendation(item),
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

    def sorted_candidates(self) -> list[dict[str, Any]]:
        def key(item: dict[str, Any]) -> tuple[int, str]:
            title, detail, when = describe_item(item)
            values = {
                "selected": "0" if str(item.get("id")) in self.selected_ids else "1",
                "category": item.get("category_label") or default_category_label(item),
                "recommendation": item.get("recommendation") or default_recommendation(item),
                "type": item.get("type", ""),
                "confidence": item.get("cleanup_confidence", ""),
                "title": title,
                "time": when,
                "detail": detail,
            }
            priority = recommendation_priority(str(values["recommendation"]))
            if self.sort_column == "recommendation":
                return (priority, str(values[self.sort_column]).lower())
            return (0, str(values.get(self.sort_column, "")).lower())

        return sorted(self.candidates, key=key, reverse=self.sort_reverse)

    def sort_by(self, column: str) -> None:
        if self.sort_column == column:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = column
            self.sort_reverse = False
        self.render_tree()

    def init_baseline_from_gui(self) -> None:
        since = self.baseline_since_var.get().strip()
        if not since:
            messagebox.showerror("缺少日期", "请输入基线日期，例如 2026-03-15。")
            return
        if not messagebox.askyesno("覆盖基线", f"将覆盖当前基线并使用日期 {since}。是否继续？"):
            return
        try:
            core.parse_since(since)
            snapshot = core.collect_snapshot(self.state_dir, core.default_scan_roots())
            snapshot["baseline_since"] = core.parse_since(since).isoformat()
            core.write_json(self.state_dir / core.BASELINE_FILE, snapshot)
            self.selected_ids.clear()
            self.refresh_data(rescan=True)
            self.set_status(f"基线已建立：{self.state_dir / core.BASELINE_FILE}")
        except Exception as exc:
            messagebox.showerror("建立基线失败", str(exc))

    def toggle_current_selection(self, _event: object | None = None) -> None:
        focused = self.tree.focus()
        if not focused:
            return
        if focused in self.selected_ids:
            self.selected_ids.remove(focused)
        else:
            self.selected_ids.add(focused)
        self.render_tree()

    def clear_selection(self) -> None:
        self.selected_ids.clear()
        self.render_tree()
        self.set_status("已清空勾选。")

    def selected_items(self) -> list[dict[str, Any]]:
        return [item for item in self.candidates if str(item.get("id")) in self.selected_ids]

    def run_ai_review(self, use_selected: bool) -> None:
        items = self.selected_items() if use_selected else self.candidates
        if not items:
            messagebox.showinfo("没有候选项", "当前没有可审核的候选项。")
            return
        payload = core.ai_review_payload_for_items(items)
        self.set_status("AI 审核中，请稍候...")
        self.update_idletasks()
        try:
            result = ai_reviewer.review_with_openai_compatible(
                api_key=self.ai_api_key_var.get(),
                base_url=self.ai_base_url_var.get().strip(),
                model=self.ai_model_var.get().strip(),
                payload=payload,
            )
        except Exception as exc:
            messagebox.showerror("AI 审核失败", str(exc))
            self.set_status("AI 审核失败。")
            return
        selected_by_ai = self.ai_selected_ids_for_policy(result)
        for item_id in selected_by_ai:
            self.selected_ids.add(str(item_id))
        self.render_tree()
        self.ai_output.delete("1.0", "end")
        self.ai_output.insert("end", f"摘要：{result.get('summary', '')}\n\n")
        warnings = result.get("warnings") or []
        if warnings:
            self.ai_output.insert("end", "警告：\n")
            for warning in warnings:
                self.ai_output.insert("end", f"- {warning}\n")
            self.ai_output.insert("end", "\n")
        self.ai_output.insert("end", "决策：\n")
        for decision in result.get("decisions", []):
            self.ai_output.insert(
                "end",
                f"- {decision.get('action')} / {decision.get('risk')} / {decision.get('id')}: {decision.get('reason')}\n",
            )
        self.set_status(f"AI 已按当前策略勾选 {len(selected_by_ai)} 项。请确认后再隔离或导出清单。")

    def ai_selected_ids_for_policy(self, result: dict[str, Any]) -> set[str]:
        policy = self.ai_selection_policy_var.get()
        selected = {str(item_id) for item_id in result.get("selected_ids", [])}
        for decision in result.get("decisions", []):
            item_id = str(decision.get("id", ""))
            action = str(decision.get("action", "")).lower()
            risk = str(decision.get("risk", "")).lower()
            if not item_id:
                continue
            if "保守" in policy:
                if action == "select":
                    selected.add(item_id)
            elif "离职" in policy:
                if action in {"select", "review"}:
                    selected.add(item_id)
            elif "激进" in policy:
                if action in {"select", "review"} or (action == "keep" and risk == "low"):
                    selected.add(item_id)
        return selected

    def fetch_ai_models(self) -> None:
        self.set_status("正在获取模型列表...")
        self.update_idletasks()
        try:
            models = ai_reviewer.list_openai_compatible_models(
                api_key=self.ai_api_key_var.get(),
                base_url=self.ai_base_url_var.get().strip(),
            )
        except Exception as exc:
            messagebox.showerror("获取模型列表失败", str(exc))
            self.set_status("获取模型列表失败。可继续手动输入模型名。")
            return
        self.ai_model_combo["values"] = models
        if models and self.ai_model_var.get() not in models:
            self.ai_model_var.set(models[0])
        self.set_status(f"已获取 {len(models)} 个模型。")

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


def default_category_label(item: dict[str, Any]) -> str:
    item_type = str(item.get("type", ""))
    if item_type == "browser_login_metadata":
        return "浏览器登录"
    if item_type == "chat_data_location":
        return "聊天数据位置"
    if item_type == "environment_variable":
        return "环境变量"
    if item_type == "installed_app":
        return "已安装软件"
    if item_type == "install_activity_event":
        return "安装行为"
    return "未分类"


def default_recommendation(item: dict[str, Any]) -> str:
    item_type = str(item.get("type", ""))
    if item_type == "browser_login_metadata":
        return "manual_review"
    if item_type == "chat_data_location":
        return "manual_review"
    if item_type == "environment_variable":
        return "review_required"
    if item_type == "installed_app":
        return "review_required"
    if item_type == "install_activity_event":
        return "review_required"
    return "review_required"


def recommendation_priority(value: str) -> int:
    order = {
        "recommend_cleanup": 0,
        "prioritize_revoke_then_clean": 1,
        "review_required": 2,
        "manual_review": 3,
        "keep": 4,
    }
    return order.get(value, 9)


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
