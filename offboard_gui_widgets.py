"""Tk widget classes used by ``offboard_gui.py``.

Lives in its own module so the main GUI file can focus on wiring the
controller. The classes here all communicate with the controller through
methods (``controller.quarantine_selected_recommended`` etc.) instead of
re-implementing the same logic.

This module must not import :mod:`offboard_gui` at top level (it would
create a circular import — offboard_gui imports from here). The
``OffboardGui`` type appears only as a string annotation on
:class:`DetailPanel`.
"""

from __future__ import annotations

import datetime as dt
import enum
import json
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

import offboard_assistant as core


class StatusLevel(enum.Enum):
    """Severity for the status bar at the bottom of the GUI window.

    The indicator color and the text background change with the level so the
    user can distinguish "still working" from "failed" at a glance.
    Palette picks low-saturation colors that read on both light and dark
    Windows themes (chosen against the GitHub neutral palette).
    """

    INFO = ("info", "#1a7f37", "#ddf4ff", "#1a7f37")   # green dot, blue tint
    WARN = ("warn", "#9a6700", "#fff8c5", "#9a6700")   # amber dot, yellow tint
    ERROR = ("error", "#cf222e", "#ffebe9", "#cf222e")  # red dot, pink tint
    BUSY = ("busy", "#57606a", "#eaeef2", "#57606a")    # grey dot, neutral

    def __init__(self, tag: str, indicator_fg: str, text_bg: str, indicator_bg: str) -> None:
        self.tag = tag
        self.indicator_fg = indicator_fg
        self.text_bg = text_bg
        self.indicator_bg = indicator_bg


class FirstRunWizard(tk.Toplevel):
    """Three-step first-run dialog.

    Step 1: pick the baseline date (today is one click away).
    Step 2: enter company/personal email domains (one per line).
    Step 3: pick directories to scan for sensitive files.

    Writes <state_dir>/config.json + wizard.done on completion. The wizard
    can be skipped — no data is recorded in that case — but the next launch
    will surface it again until either ``wizard.done`` or a baseline exists.
    """

    def __init__(self, master: tk.Misc, state_dir: Path) -> None:
        super().__init__(master)
        self.state_dir = state_dir
        self.title("首次运行向导 / First-Run Wizard")
        self.geometry("640x520")
        self.transient(master)
        self.grab_set()
        self.scan_roots: list[str] = []
        self.company_domains: list[str] = []
        self.personal_domains: list[str] = []
        self.baseline_since = dt.date.today().isoformat()
        self._build()

    def show(self) -> None:
        """Center the dialog over its master and block until it is closed.

        ``grab_set`` was already issued in ``__init__``, so user input is
        funneled to the wizard until ``finish`` / ``skip`` runs ``destroy``.
        """
        self.update_idletasks()
        try:
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - self.winfo_width()) // 2
            y = m.winfo_rooty() + (m.winfo_height() - self.winfo_height()) // 2
            self.geometry(f"+{max(x, 0)}+{max(y, 0)}")
        except Exception:
            pass
        self.wait_window(self)

    def _build(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        header = ttk.Label(
            self,
            text="三步完成首次配置 / Complete setup in three steps",
            font=("TkDefaultFont", 12, "bold"),
        )
        header.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 4))

        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=12, pady=8)
        self._build_step1(notebook)
        self._build_step2(notebook)
        self._build_step3(notebook)

        button_frame = ttk.Frame(self)
        button_frame.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 12))
        ttk.Button(button_frame, text="跳过 / Skip", command=self._skip).pack(side="left")
        ttk.Button(button_frame, text="完成 / Finish", command=self._finish).pack(side="right")

    def _build_step1(self, notebook: ttk.Notebook) -> None:
        step = ttk.Frame(notebook, padding=10)
        notebook.add(step, text="1. 基线日期")
        ttk.Label(step, text="选择入职当天或更早的日期，作为清理起点。").pack(anchor="w")
        date_frame = ttk.Frame(step)
        date_frame.pack(fill="x", pady=8)
        self.date_var = tk.StringVar(value=dt.date.today().isoformat())
        ttk.Entry(date_frame, textvariable=self.date_var, width=14).pack(side="left", padx=(0, 6))
        ttk.Button(date_frame, text="今天", command=lambda: self.date_var.set(dt.date.today().isoformat())).pack(side="left")

    def _build_step2(self, notebook: ttk.Notebook) -> None:
        step = ttk.Frame(notebook, padding=10)
        notebook.add(step, text="2. 账号域名")
        ttk.Label(step, text="公司邮箱域名（一行一个，如 @corp.example.com）。\n用于推断浏览器登录、AI 配置的归属。").pack(anchor="w")
        self.company_text = tk.Text(step, height=4, width=60)
        self.company_text.pack(fill="x", pady=(4, 8))
        ttk.Label(step, text="个人邮箱域名（一行一个）。").pack(anchor="w")
        self.personal_text = tk.Text(step, height=4, width=60)
        self.personal_text.pack(fill="x", pady=(4, 0))

    def _build_step3(self, notebook: ttk.Notebook) -> None:
        step = ttk.Frame(notebook, padding=10)
        notebook.add(step, text="3. 扫描目录")
        ttk.Label(step, text="添加要扫描敏感文件（.env、SSH key 等）的目录。\n未添加时使用默认目录。").pack(anchor="w")
        list_frame = ttk.Frame(step)
        list_frame.pack(fill="both", expand=True, pady=(4, 8))
        self.scan_list = tk.Listbox(list_frame, height=6)
        self.scan_list.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(list_frame, orient="vertical", command=self.scan_list.yview)
        sb.pack(side="right", fill="y")
        self.scan_list.configure(yscrollcommand=sb.set)
        buttons = ttk.Frame(step)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="添加目录…", command=self._add_scan_root).pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="移除选中", command=self._remove_scan_root).pack(side="left")

    def _add_scan_root(self) -> None:
        path = filedialog.askdirectory(title="选择扫描目录", parent=self)
        if path:
            self.scan_list.insert("end", path)
            self.scan_roots.append(path)

    def _remove_scan_root(self) -> None:
        for index in reversed(self.scan_list.curselection()):
            value = self.scan_list.get(index)
            self.scan_list.delete(index)
            try:
                self.scan_roots.remove(value)
            except ValueError:
                pass

    def _skip(self) -> None:
        if messagebox.askyesno("跳过向导", "确定跳过？下次启动仍会询问。", parent=self):
            self.destroy()

    def _finish(self) -> None:
        baseline_since = self.date_var.get().strip()
        try:
            core.parse_since(baseline_since)
        except ValueError:
            messagebox.showerror("日期无效", "请使用 YYYY-MM-DD 格式。", parent=self)
            return
        company = [d.strip() for d in self.company_text.get("1.0", "end").splitlines() if d.strip()]
        personal = [d.strip() for d in self.personal_text.get("1.0", "end").splitlines() if d.strip()]
        config = core.load_local_config(self.state_dir)
        config["baseline_since"] = baseline_since
        config["scan_roots"] = list(self.scan_roots)
        config["company_email_domains"] = company
        config["personal_email_domains"] = personal
        core.save_local_config(self.state_dir, config)
        core.mark_wizard_done(self.state_dir)
        messagebox.showinfo("完成", "配置已保存。点击清理清单 → 建立基线 开始首次扫描。", parent=self)
        self.destroy()


class OverflowMenu(ttk.Menubutton):
    """A ttk.Menubutton wrapping a tearoff-less tk.Menu for secondary actions.

    Items are added via :meth:`add` (label + command) or :meth:`add_separator`.
    Callbacks retain their original closures; nothing is serialized.
    """

    def __init__(self, parent: tk.Misc, label: str = "更多") -> None:
        super().__init__(parent, text=label)
        self._menu = tk.Menu(self, tearoff=0)
        self.configure(menu=self._menu)

    def add(self, label: str, command) -> None:
        self._menu.add_command(label=label, command=command)

    def add_separator(self) -> None:
        self._menu.add_separator()


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
    if item_type == "ide_recent_project":
        return "IDE 最近项目"
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
    if item_type == "ide_recent_project":
        return "review_required"
    return "review_required"


class FilterSidebar(ttk.Frame):
    """Multi-select filter sidebar. Calls on_change() whenever any checkbox flips."""

    OWNER_OPTIONS = [
        ("company_account", "公司"),
        ("personal_account", "个人"),
        ("unknown", "未知"),
    ]

    def __init__(self, master: tk.Misc, on_change) -> None:
        super().__init__(master, padding=(6, 0))
        self.on_change = on_change
        self.owner_vars: dict[str, tk.BooleanVar] = {}
        self.recommendation_vars: dict[str, tk.BooleanVar] = {}
        self.category_vars: dict[str, tk.BooleanVar] = {}
        self.confidence_vars: dict[str, tk.BooleanVar] = {}

        ttk.Label(self, text="筛选 / Filter", font=("TkDefaultFont", 10, "bold")).pack(anchor="w", pady=(0, 6))
        self._build_section("归属", self.owner_vars, self.OWNER_OPTIONS)
        self._build_dynamic_sections()
        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=6)
        ttk.Button(self, text="重置", command=self._reset).pack(fill="x")

    def _build_section(self, label: str, var_map: dict, options: list[tuple[str, str]]) -> None:
        ttk.Label(self, text=label, font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(4, 2))
        for key, text in options:
            var = tk.BooleanVar(value=True)
            var_map[key] = var
            cb = ttk.Checkbutton(self, text=text, variable=var, command=self.on_change)
            cb.pack(anchor="w")

    def _build_dynamic_sections(self) -> None:
        # Categories & recommendations & confidence are populated after the
        # first snapshot is collected, via :meth:`refresh_counts`.
        self.dynamic_frame = ttk.Frame(self)
        self.dynamic_frame.pack(fill="x")

    def refresh_counts(self, candidates: list[dict[str, Any]]) -> None:
        if self.category_vars or self.recommendation_vars or self.confidence_vars:
            return  # already populated
        categories: dict[str, int] = {}
        recommendations: dict[str, int] = {}
        confidences: dict[str, int] = {}
        for item in candidates:
            cat = item.get("category_label") or default_category_label(item)
            categories[cat] = categories.get(cat, 0) + 1
            rec = item.get("recommendation") or default_recommendation(item)
            recommendations[rec] = recommendations.get(rec, 0) + 1
            conf = item.get("cleanup_confidence") or "low"
            confidences[conf] = confidences.get(conf, 0) + 1
        for widget in self.dynamic_frame.winfo_children():
            widget.destroy()
        self._populate_dynamic("分类", self.category_vars, sorted(categories))
        self._populate_dynamic("推荐", self.recommendation_vars, sorted(recommendations))
        self._populate_dynamic("置信度", self.confidence_vars, sorted(confidences))

    def _populate_dynamic(self, label: str, var_map: dict[str, tk.BooleanVar], entries: list[str]) -> None:
        if not entries:
            return
        ttk.Label(self.dynamic_frame, text=label, font=("TkDefaultFont", 9, "bold")).pack(anchor="w", pady=(4, 2))
        for key in entries:
            var = tk.BooleanVar(value=True)
            var_map[key] = var
            ttk.Checkbutton(self.dynamic_frame, text=key, variable=var, command=self.on_change).pack(anchor="w")

    def _reset(self) -> None:
        for var_map in (self.owner_vars, self.recommendation_vars, self.category_vars, self.confidence_vars):
            for var in var_map.values():
                var.set(True)
        self.on_change()

    def apply(self, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        result = []
        for item in candidates:
            owner = item.get("account_owner_hint") or "unknown"
            if not self.owner_vars.get(owner, tk.BooleanVar(value=True)).get():
                continue
            cat = item.get("category_label") or default_category_label(item)
            if not self.category_vars.get(cat, tk.BooleanVar(value=True)).get():
                continue
            rec = item.get("recommendation") or default_recommendation(item)
            if not self.recommendation_vars.get(rec, tk.BooleanVar(value=True)).get():
                continue
            conf = item.get("cleanup_confidence") or "low"
            if not self.confidence_vars.get(conf, tk.BooleanVar(value=True)).get():
                continue
            result.append(item)
        return result


class DetailPanel(ttk.Frame):
    """Right-hand detail panel showing the selected item's full metadata."""

    def __init__(self, master: tk.Misc, controller: "offboard_gui.OffboardGui") -> None:
        super().__init__(master, padding=(6, 0))
        self.controller = controller
        ttk.Label(self, text="详情 / Detail", font=("TkDefaultFont", 10, "bold")).pack(anchor="w")
        self.text = tk.Text(self, wrap="word", height=20, state="disabled")
        self.text.pack(fill="both", expand=True, pady=(4, 4))
        button_frame = ttk.Frame(self)
        button_frame.pack(fill="x")
        ttk.Button(button_frame, text="隔离此项", command=self._quarantine_single).pack(side="left", padx=(0, 4))
        ttk.Button(button_frame, text="标记已处理", command=self._mark_handled).pack(side="left", padx=(0, 4))
        ttk.Button(button_frame, text="复制路径", command=self._copy_path).pack(side="left")

    def show_item(self, item: dict[str, Any] | None) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        if item is None:
            self.text.insert("end", "（未选中任何候选项）")
            self.text.configure(state="disabled")
            self._current_item = None
            return
        self._current_item = item
        from offboard_gui import describe_item  # late import to avoid cycle
        title, detail, when = describe_item(item)
        self.text.insert("end", f"{title}\n\n", "title")
        self.text.insert("end", f"时间：{when or '—'}\n")
        self.text.insert("end", f"类型：{item.get('type', '—')}\n")
        owner = item.get("account_owner_hint") or "unknown"
        owner_label = {"company_account": "公司", "personal_account": "个人", "unknown": "未知"}.get(owner, owner)
        self.text.insert("end", f"归属：{owner_label}\n")
        self.text.insert("end", f"置信度：{item.get('cleanup_confidence', '—')}\n")
        self.text.insert("end", f"推荐：{item.get('recommendation') or default_recommendation(item)}\n")
        if item.get("category_label"):
            self.text.insert("end", f"分类：{item.get('category_label')}\n")
        if item.get("path"):
            self.text.insert("end", f"路径：{item.get('path')}\n")
        if item.get("origin"):
            self.text.insert("end", f"Origin：{item.get('origin')}\n")
        if item.get("app"):
            self.text.insert("end", f"应用：{item.get('app')}\n")
        if item.get("ide"):
            self.text.insert("end", f"IDE：{item.get('ide')}\n")
        self.text.insert("end", "\n说明\n")
        self.text.insert("end", detail)
        self.text.insert("end", "\n\n元数据\n")
        self.text.insert("end", json.dumps(item, ensure_ascii=False, indent=2))
        self.text.configure(state="disabled")

    def _current(self) -> dict[str, Any] | None:
        return getattr(self, "_current_item", None)

    def _quarantine_single(self) -> None:
        item = self._current()
        if not item:
            return
        target = core.recommended_cleanup_target(item)
        if not target:
            messagebox.showinfo("不支持隔离", "仅 recommend_cleanup 类型可自动隔离。", parent=self)
            return
        if not messagebox.askyesno("确认隔离", f"将移动：\n{target}", parent=self):
            return
        # Route through the controller's existing path so the move, the
        # manifest write, the state-bar update, and the post-move refresh
        # all use the same code as a multi-item quarantine. The only thing
        # that varies is the selection set.
        self.controller.selected_ids = {str(item.get("id"))}
        self.controller.quarantine_selected_recommended()

    def _mark_handled(self) -> None:
        item = self._current()
        if not item:
            return
        # Same idea as _quarantine_single: set the selection and reuse the
        # batch path so handled-items.json gets the same write semantics.
        self.controller.selected_ids = {str(item.get("id"))}
        self.controller.mark_selected_handled()

    def _copy_path(self) -> None:
        item = self._current()
        if not item:
            return
        path = item.get("path") or item.get("origin") or ""
        if not path:
            messagebox.showinfo("无可复制内容", "该项没有路径或 origin。", parent=self)
            return
        self.clipboard_clear()
        self.clipboard_append(path)
