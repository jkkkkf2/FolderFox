import os
import pathlib
import shutil
import textwrap
import threading
import time
from dataclasses import dataclass
from typing import List, Dict, Any

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from deepseek_client import DeepSeekClient


# 判定“大文件”和“近期文件”的简单阈值（可按需要调整）
LARGE_FILE_THRESHOLD_MB = 100  # 大于该值视为大文件
RECENT_FILE_THRESHOLD_DAYS = 7  # 最近 N 天视为“近期文件”


SUPPORTED_TEXT_EXTS = {
    ".txt",
    ".md",
    ".py",
    ".json",
    ".csv",
    ".log",
    ".ini",
    ".cfg",
}

FILE_ICONS: Dict[str, str] = {
    # 文档
    ".pdf": "📄",
    ".doc": "📄",
    ".docx": "📄",
    ".xls": "📊",
    ".xlsx": "📊",
    ".ppt": "📊",
    ".pptx": "📊",
    ".txt": "📃",
    ".md": "📃",
    ".csv": "📊",
    ".html": "🌐",
    ".htm": "🌐",
    ".xml": "📃",
    # 代码 / 配置
    ".py": "🐍",
    ".json": "🧾",
    ".js": "🧾",
    ".ts": "🧾",
    ".tsx": "🧾",
    ".jsx": "🧾",
    ".ini": "⚙️",
    ".cfg": "⚙️",
    ".yaml": "⚙️",
    ".yml": "⚙️",
    # 图片
    ".jpg": "🖼️",
    ".jpeg": "🖼️",
    ".png": "🖼️",
    ".gif": "🖼️",
    ".bmp": "🖼️",
    ".webp": "🖼️",
    # 视频
    ".mp4": "🎬",
    ".mov": "🎬",
    ".avi": "🎬",
    ".mkv": "🎬",
    # 压缩包
    ".zip": "🗜️",
    ".rar": "🗜️",
    ".7z": "🗜️",
    ".tar": "🗜️",
    ".gz": "🗜️",
    # 音频
    ".mp3": "🎵",
    ".wav": "🎵",
    ".flac": "🎵",
    ".aac": "🎵",
    # 默认
    "default": "📁",
}


@dataclass
class FileSuggestion:
    name: str
    rel_path: str
    suggested_subfolder: str
    reason: str
    # 额外风险标记：用于高亮/筛选大文件和近期文件
    is_large: bool = False
    is_recent: bool = False


@dataclass
class MoveRecord:
    src: pathlib.Path
    dst: pathlib.Path


class FileOrganizerApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("智能文件夹整理 Agent (DeepSeek)")
        self.root.geometry("1080x660")
        self.root.configure(bg="#f5f5f7")  # 浅色背景

        # 全局样式：浅色简约风，蓝色轻点缀
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass

        style.configure(
            "Main.TFrame",
            background="#f5f5f7",
        )
        style.configure(
            "Top.TFrame",
            background="#ffffff",
        )
        style.configure(
            "Section.TLabel",
            background="#f5f5f7",
            foreground="#333333",
        )
        style.configure(
            "Title.TLabel",
            background="#f5f5f7",
            foreground="#111111",
            font=("Segoe UI", 12, "bold"),
        )

        # 按钮：深色扁平 + 蓝色高亮
        style.configure(
            "TButton",
            padding=(10, 4),
            background="#e5ecfb",
            foreground="#1f2933",
            borderwidth=0,
        )
        style.map(
            "TButton",
            background=[("active", "#2563eb")],
            foreground=[("active", "#ffffff")],
        )

        # Treeview：暗背景 + 条纹行
        style.configure(
            "Treeview",
            background="#ffffff",
            fieldbackground="#ffffff",
            foreground="#111827",
            font=("Segoe UI", 9),
            rowheight=22,
        )
        style.configure(
            "Treeview.Heading",
            background="#e5e7eb",
            foreground="#374151",
            font=("Segoe UI", 9, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", "#bfdbfe")],
            foreground=[("selected", "#111827")],
        )

        self.folder_path_var = tk.StringVar()
        self.status_var = tk.StringVar(value="")

        # 语言 & 模式
        # - lang_var: 仅控制“生成的分类文件夹名称语言”（传给 DeepSeek）
        # - ui_lang_var: 控制 UI 界面/弹窗语言
        self.lang_var = tk.StringVar(value="zh")
        self.ui_lang_var = tk.StringVar(value="zh")
        self.mode_var = tk.StringVar(value="standard")

        # 视图
        self.before_tree: ttk.Treeview | None = None
        self.group_tree: ttk.Treeview | None = None
        self.detail_tree: ttk.Treeview | None = None  # 已不再使用，保留属性避免后续引用报错
        self.before_spinner: ttk.Progressbar | None = None
        self.after_spinner: ttk.Progressbar | None = None
        self.custom_text: tk.Text | None = None
        self.skip_ext_var = tk.StringVar(value="")
        self.skip_exts: set[str] = set()
        self.last_files_info: List[Dict[str, Any]] = []

        # 右侧 Treeview 拖拽与编辑相关
        self.group_dir_nodes: Dict[str, str] = {}  # path -> node_id
        self.group_dir_node_to_path: Dict[str, str] = {}  # node_id -> path
        self.group_file_node_to_index: Dict[str, int] = {}  # file node_id -> suggestion index
        self.group_drag_item_id: str | None = None
        self.group_drag_is_dir: bool = False
        self.group_hover_target_id: str | None = None

        # 高风险文件筛选（大文件 / 近期文件）
        self.filter_risky_only_var = tk.BooleanVar(value=False)

        # 重复文件检测结果
        self.duplicate_groups: list[list[Dict[str, Any]]] = []

        # 左侧 Treeview 节点到路径映射（相对根目录）
        self.before_dir_node_to_path: Dict[str, str] = {}
        self.before_file_node_to_path: Dict[str, str] = {}

        self.suggestions: List[FileSuggestion] = []

        self.analyze_btn: ttk.Button | None = None
        self.execute_btn: ttk.Button | None = None
        self.undo_btn: ttk.Button | None = None

        self.is_busy: bool = False
        self.last_moves: List[MoveRecord] = []

        self._build_ui()

    # UI 构建
    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, style="Main.TFrame", padding="10 10 10 10")
        outer.pack(fill=tk.BOTH, expand=True)

        # 顶部标题
        self.title_label = ttk.Label(
            outer,
            text="智能文件夹整理 Agent",
            style="Title.TLabel",
        )
        self.title_label.pack(anchor="w", pady=(0, 4))

        top_frame = ttk.Frame(outer, style="Top.TFrame", padding="8 8 8 8")
        top_frame.pack(fill=tk.X)

        self.path_label = ttk.Label(top_frame, text="目标文件夹:", style="Section.TLabel")
        self.path_label.pack(side=tk.LEFT)

        path_entry = ttk.Entry(top_frame, textvariable=self.folder_path_var)
        path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=4)

        self.choose_btn = ttk.Button(top_frame, text="选择文件夹", command=self.choose_folder)
        self.choose_btn.pack(side=tk.LEFT, padx=4)

        self.analyze_btn = ttk.Button(
            top_frame,
            text="分析并生成整理方案",
            command=self.analyze_folder,
        )
        self.analyze_btn.pack(side=tk.LEFT, padx=4)

        # 语言 & 模式 & 个性化需求
        options_frame = ttk.Frame(outer, style="Main.TFrame", padding="4 4 4 6")
        options_frame.pack(fill=tk.X)

        # 分类文件夹语言选择（仅影响 DeepSeek 返回的 suggested_subfolder 语言）
        self.lang_label = ttk.Label(
            options_frame,
            text="分类文件夹语言：",
            style="Section.TLabel",
        )
        self.lang_label.grid(row=0, column=0, sticky="w")
        lang_cn = ttk.Radiobutton(
            options_frame,
            text="中文",
            variable=self.lang_var,
            value="zh",
        )
        lang_en = ttk.Radiobutton(
            options_frame,
            text="English",
            variable=self.lang_var,
            value="en",
        )
        lang_cn.grid(row=0, column=1, sticky="w")
        lang_en.grid(row=0, column=2, sticky="w", padx=(4, 0))

        # UI 语言（新增下拉框，控制界面所有文案与弹窗）
        self.ui_lang_label = ttk.Label(
            options_frame,
            text="界面语言：",
            style="Section.TLabel",
        )
        self.ui_lang_label.grid(row=0, column=3, sticky="w", padx=(16, 0))

        self.ui_lang_combo = ttk.Combobox(
            options_frame,
            state="readonly",
            values=("中文", "English"),
            width=10,
        )
        self.ui_lang_combo.grid(row=0, column=4, sticky="w")
        self.ui_lang_combo.set("中文" if self.ui_lang_var.get() == "zh" else "English")
        self.ui_lang_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_ui_lang_changed())

        # 整理模式
        self.mode_label = ttk.Label(
            options_frame,
            text="整理模式：",
            style="Section.TLabel",
        )
        self.mode_label.grid(row=0, column=5, sticky="w", padx=(16, 0))
        self.mode_standard = ttk.Radiobutton(
            options_frame,
            text="智能（默认）",
            variable=self.mode_var,
            value="standard",
        )
        self.mode_by_type = ttk.Radiobutton(
            options_frame,
            text="按类型",
            variable=self.mode_var,
            value="by_type",
        )
        self.mode_by_time = ttk.Radiobutton(
            options_frame,
            text="按文件名前缀",
            variable=self.mode_var,
            value="by_prefix",
        )
        self.mode_standard.grid(row=0, column=6, sticky="w")
        self.mode_by_type.grid(row=0, column=7, sticky="w", padx=(4, 0))
        self.mode_by_time.grid(row=0, column=8, sticky="w", padx=(4, 0))

        # 个性化需求
        self.custom_label = ttk.Label(
            options_frame,
            text="个性化整理需求（可选）：",
            style="Section.TLabel",
        )
        self.custom_label.grid(row=1, column=0, sticky="nw", pady=(4, 0))
        custom_frame = ttk.Frame(options_frame)
        custom_frame.grid(row=1, column=1, columnspan=6, sticky="nsew", pady=(4, 0))

        self.custom_text = tk.Text(
            custom_frame,
            height=3,
            wrap="word",
            bg="#ffffff",
            fg="#111827",
            insertbackground="#111827",
            relief="flat",
            borderwidth=1,
            highlightthickness=1,
            highlightbackground="#d1d5db",
        )
        custom_vsb = ttk.Scrollbar(
            custom_frame, orient="vertical", command=self.custom_text.yview
        )
        self.custom_text.configure(yscrollcommand=custom_vsb.set)
        self.custom_text.pack(side=tk.LEFT, fill=tk.X, expand=True)
        custom_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        options_frame.columnconfigure(1, weight=1)
        options_frame.columnconfigure(2, weight=0)
        options_frame.columnconfigure(3, weight=0)
        options_frame.columnconfigure(4, weight=0)
        options_frame.columnconfigure(5, weight=0)
        options_frame.columnconfigure(6, weight=0)
        options_frame.columnconfigure(7, weight=0)
        options_frame.columnconfigure(8, weight=0)

        # 不整理的文件类型设置
        self.skip_label = ttk.Label(
            options_frame,
            text="不整理的文件类型（如 .pdf,.jpg）：",
            style="Section.TLabel",
        )
        self.skip_label.grid(row=2, column=0, sticky="w", pady=(6, 0))
        skip_entry = ttk.Entry(options_frame, textvariable=self.skip_ext_var)
        skip_entry.grid(row=2, column=1, columnspan=6, sticky="we", pady=(6, 0))

        # 高风险文件筛选开关
        self.risky_check = ttk.Checkbutton(
            options_frame,
            text="只显示大文件 / 近期文件（高风险）",
            variable=self.filter_risky_only_var,
            command=lambda: self._refresh_tree(self.last_files_info)
            if self.last_files_info
            else None,
        )
        self.risky_check.grid(row=3, column=0, columnspan=7, sticky="w", pady=(6, 0))

        # 中间区域：卡片式，两侧树状结构
        paned = ttk.Panedwindow(outer, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True, padx=2, pady=4)

        # 左侧：当前结构（整理前）卡片
        left_card = ttk.Frame(paned, padding="8 8 8 8")
        left_card.configure(style="Top.TFrame")
        paned.add(left_card, weight=1)

        left_header = ttk.Frame(left_card, style="Top.TFrame")
        left_header.pack(fill=tk.X)

        self.before_label = ttk.Label(
            left_header,
            text="当前文件结构（整理前）",
            style="Section.TLabel",
        )
        self.before_label.pack(side=tk.LEFT)

        content_sep = ttk.Separator(left_card, orient=tk.HORIZONTAL)
        content_sep.pack(fill=tk.X, pady=(4, 4))

        left_frame = ttk.Frame(left_card)
        left_frame.pack(fill=tk.BOTH, expand=True)

        self.before_spinner = ttk.Progressbar(
            left_frame, mode="indeterminate", length=120
        )
        self.before_spinner.pack(anchor="w", pady=(0, 2))

        before_tree = ttk.Treeview(left_frame, show="tree")
        before_vsb = ttk.Scrollbar(
            left_frame, orient="vertical", command=before_tree.yview
        )
        before_tree.configure(yscrollcommand=before_vsb.set)

        before_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        before_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.before_tree = before_tree

        # 绑定左侧 Treeview 的双击打开事件
        self.before_tree.bind("<Double-1>", self._on_before_double_click)

        # 右侧：整理后结构预览卡片
        right_card = ttk.Frame(paned, padding="8 8 8 8")
        right_card.configure(style="Top.TFrame")
        paned.add(right_card, weight=1)

        right_header = ttk.Frame(right_card, style="Top.TFrame")
        right_header.pack(fill=tk.X)

        self.group_label = ttk.Label(
            right_header,
            text="整理后结构预览",
            style="Section.TLabel",
        )
        self.group_label.pack(side=tk.LEFT)

        right_sep = ttk.Separator(right_card, orient=tk.HORIZONTAL)
        right_sep.pack(fill=tk.X, pady=(4, 4))

        right_frame = ttk.Frame(right_card)
        right_frame.pack(fill=tk.BOTH, expand=True)

        self.after_spinner = ttk.Progressbar(
            right_frame, mode="indeterminate", length=120
        )
        self.after_spinner.pack(anchor="w", pady=(0, 2))

        group_tree = ttk.Treeview(right_frame, show="tree")
        group_vsb = ttk.Scrollbar(
            right_frame, orient="vertical", command=group_tree.yview
        )
        group_tree.configure(yscrollcommand=group_vsb.set)

        group_tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        group_vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.group_tree = group_tree

        # 绑定右侧 Treeview 的拖拽和右键菜单事件
        self.group_tree.bind("<ButtonPress-1>", self._on_group_button_press)
        self.group_tree.bind("<ButtonRelease-1>", self._on_group_button_release)
        self.group_tree.bind("<B1-Motion>", self._on_group_motion)
        self.group_tree.bind("<Button-3>", self._on_group_right_click)
        self.group_tree.bind("<Double-1>", self._on_group_double_click)

        self.group_context_menu = tk.Menu(self.root, tearoff=0)
        self.group_context_menu.add_command(
            label="新建子文件夹并移动选中文件", command=self._create_folder_and_move
        )
        self.group_context_menu.add_separator()
        self.group_context_menu.add_command(
            label="从整理方案中移除选中项", command=self._remove_selected_from_plan
        )

        # 底部操作 & 状态栏
        bottom_frame = ttk.Frame(outer, style="Main.TFrame", padding="4 4 4 0")
        bottom_frame.pack(fill=tk.X, pady=(4, 0))

        self.execute_btn = ttk.Button(
            bottom_frame,
            text="执行整理 (移动文件)",
            command=self.execute_organize,
        )
        self.execute_btn.pack(side=tk.LEFT)

        self.undo_btn = ttk.Button(
            bottom_frame,
            text="撤销上次整理",
            command=self.undo_last_organize,
            state=tk.DISABLED,
        )
        self.undo_btn.pack(side=tk.LEFT, padx=(8, 0))

        status_label = ttk.Label(
            bottom_frame, textvariable=self.status_var, anchor=tk.W
        )
        status_label.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=8)

        # 根据当前语言初始化 UI 文案
        self._set_ui_language(initial=True)

    def _get_ui_lang(self) -> str:
        """当前 UI 语言：'zh' 或 'en'。"""
        return "en" if self.ui_lang_var.get() == "en" else "zh"

    def _on_ui_lang_changed(self) -> None:
        """UI 语言下拉框变更时，刷新所有 UI 文案（不打断当前状态提示）。"""
        selected = (self.ui_lang_combo.get() or "").strip()
        self.ui_lang_var.set("en" if selected.lower().startswith("english") else "zh")
        self._set_ui_language(initial=False)

    def _set_ui_language(self, initial: bool = False) -> None:
        """根据当前语言设置所有 UI 文案。"""
        lang = self._get_ui_lang()
        current_status = (self.status_var.get() or "").strip()

        if lang == "en":
            self.root.title("Smart Folder Organizer (DeepSeek)")
            self.title_label.config(text="Smart Folder Organizer Agent")
            self.path_label.config(text="Target folder:")
            self.choose_btn.config(text="Choose folder")
            self.analyze_btn.config(text="Analyze and generate plan")

            self.lang_label.config(text="Category folder language:")
            # 单选文本本身保留“中文 / English”，便于区分

            self.ui_lang_label.config(text="UI language:")
            self.ui_lang_combo.set("English")

            self.mode_label.config(text="Organize mode:")
            self.mode_standard.config(text="Smart (default)")
            self.mode_by_type.config(text="By type")
            self.mode_by_time.config(text="By filename prefix")

            self.custom_label.config(text="Custom organizing preferences (optional):")
            self.skip_label.config(
                text="File types to skip (e.g. .pdf,.jpg):"
            )
            self.risky_check.config(
                text="Show only large / recent files (high risk)"
            )

            self.before_label.config(text="Current structure (before organizing)")
            self.group_label.config(text="Preview after organizing")

            self.execute_btn.config(text="Apply organizing (move files)")
            self.undo_btn.config(text="Undo last organizing")

            # 右键菜单
            self.group_context_menu.entryconfig(
                0, label="Create subfolder and move selected files"
            )
            self.group_context_menu.entryconfig(
                2, label="Remove selected items from plan"
            )

            default_en = "Please select a folder to organize."
            default_zh = "请选择一个需要整理的文件夹。"
            if initial or current_status in {"", default_zh, default_en}:
                self.status_var.set(default_en)
        else:
            self.root.title("智能文件夹整理 Agent (DeepSeek)")
            self.title_label.config(text="智能文件夹整理 Agent")
            self.path_label.config(text="目标文件夹:")
            self.choose_btn.config(text="选择文件夹")
            self.analyze_btn.config(text="分析并生成整理方案")

            self.lang_label.config(text="分类文件夹语言：")

            self.ui_lang_label.config(text="界面语言：")
            self.ui_lang_combo.set("中文")

            self.mode_label.config(text="整理模式：")
            self.mode_standard.config(text="智能（默认）")
            self.mode_by_type.config(text="按类型")
            self.mode_by_time.config(text="按文件名前缀")

            self.custom_label.config(text="个性化整理需求（可选）：")
            self.skip_label.config(text="不整理的文件类型（如 .pdf,.jpg）：")
            self.risky_check.config(text="只显示大文件 / 近期文件（高风险）")

            self.before_label.config(text="当前文件结构（整理前）")
            self.group_label.config(text="整理后结构预览")

            self.execute_btn.config(text="执行整理 (移动文件)")
            self.undo_btn.config(text="撤销上次整理")

            self.group_context_menu.entryconfig(
                0, label="新建子文件夹并移动选中文件"
            )
            self.group_context_menu.entryconfig(
                2, label="从整理方案中移除选中项"
            )

            default_en = "Please select a folder to organize."
            default_zh = "请选择一个需要整理的文件夹。"
            if initial or current_status in {"", default_zh, default_en}:
                self.status_var.set(default_zh)

    # 事件逻辑
    def choose_folder(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.folder_path_var.set(folder)
            if self._get_ui_lang() == "en":
                self.status_var.set(f"Selected folder: {folder}")
            else:
                self.status_var.set(f"已选择文件夹：{folder}")
            self._clear_suggestions()
            self._update_undo_state()

    def analyze_folder(self) -> None:
        if self.is_busy:
            return

        folder = self.folder_path_var.get().strip()
        if not folder:
            if self._get_ui_lang() == "en":
                messagebox.showwarning("Warning", "Please choose a folder first.")
            else:
                messagebox.showwarning("提示", "请先选择一个文件夹。")
            return

        folder_path = pathlib.Path(folder)
        if not folder_path.exists() or not folder_path.is_dir():
            if self._get_ui_lang() == "en":
                messagebox.showerror("Error", "Target path does not exist or is not a folder.")
            else:
                messagebox.showerror("错误", "目标路径不存在或不是文件夹。")
            return

        # 读取当前语言、模式和个性化需求（在主线程中完成）
        language = self.lang_var.get()
        mode = self.mode_var.get()
        custom_instruction = ""
        if self.custom_text is not None:
            custom_instruction = self.custom_text.get("1.0", "end").strip()

        # 解析不整理的文件类型
        self.skip_exts = set()
        raw_skip = self.skip_ext_var.get() or ""
        for part in raw_skip.replace("；", ",").replace("，", ",").split(","):
            ext = part.strip()
            if not ext:
                continue
            if not ext.startswith("."):
                ext = "." + ext
            self.skip_exts.add(ext.lower())

        self._set_busy(True)
        if self._get_ui_lang() == "en":
            self.status_var.set("Scanning original folder structure, please wait...")
        else:
            self.status_var.set("正在扫描原始文件结构，请稍候...")
        self._clear_suggestions()
        self._set_before_loading(True)
        self._set_after_loading(False)

        threading.Thread(
            target=self._scan_files_worker,
            args=(folder_path, language, mode, custom_instruction),
            daemon=True,
        ).start()

    def execute_organize(self) -> None:
        if self.is_busy:
            return

        folder = self.folder_path_var.get().strip()
        if not folder:
            if self._get_ui_lang() == "en":
                messagebox.showwarning(
                    "Warning",
                    "Please choose a folder and generate an organizing plan first.",
                )
            else:
                messagebox.showwarning("提示", "请先选择一个文件夹并生成整理方案。")
            return

        if not self.suggestions:
            if self._get_ui_lang() == "en":
                messagebox.showwarning(
                    "Warning",
                    "There is no organizing plan to apply. Please click "
                    "\"Analyze and generate plan\" first.",
                )
            else:
                messagebox.showwarning(
                    "提示", "当前没有可执行的整理方案，请先点击“分析并生成整理方案”。"
                )
            return

        if self._get_ui_lang() == "en":
            confirm = messagebox.askyesno(
                "Confirm organizing",
                "According to the current plan, subfolders will be created in the "
                "target folder and files will be moved into them.\n"
                "Do you want to continue?",
            )
        else:
            confirm = messagebox.askyesno(
                "确认整理",
                "将根据当前方案在目标文件夹下创建子文件夹，并移动其中的文件。\n"
                "你确认要继续吗？",
            )
        if not confirm:
            return

        folder_path = pathlib.Path(folder)
        self._set_busy(True)

        threading.Thread(
            target=self._execute_organize_worker, args=(folder_path,), daemon=True
        ).start()

    def undo_last_organize(self) -> None:
        if self.is_busy:
            return

        if not self.last_moves:
            if self._get_ui_lang() == "en":
                messagebox.showinfo("Info", "There is no organizing operation to undo.")
            else:
                messagebox.showinfo("提示", "当前没有可撤销的整理操作。")
            return

        if self._get_ui_lang() == "en":
            confirm = messagebox.askyesno(
                "Confirm undo",
                "The app will try to move files from the last organizing operation "
                "back to their original locations.\n"
                "Do you want to continue?",
            )
        else:
            confirm = messagebox.askyesno(
                "确认撤销",
                "将尝试把上次整理移动的文件移回原位置。\n你确认要继续吗？",
            )
        if not confirm:
            return

        self._set_busy(True)
        threading.Thread(target=self._undo_organize_worker, daemon=True).start()

    # 辅助方法
    def _clear_suggestions(self) -> None:
        self.suggestions = []
        # 清空左侧当前结构 & 整理后结构预览
        if self.before_tree is not None:
            for item in self.before_tree.get_children():
                self.before_tree.delete(item)
        if self.group_tree is not None:
            for item in self.group_tree.get_children():
                self.group_tree.delete(item)
        # 不清空 last_moves，这样仍然可以撤销上一次已经完成的整理

    def _refresh_tree(self, files_info: List[Dict[str, Any]]) -> None:
        # 刷新左侧“当前结构（整理前）”
        if self.before_tree is not None:
            for item in self.before_tree.get_children():
                self.before_tree.delete(item)

            before_nodes: Dict[str, str] = {}
            before_counts: Dict[str, int] = {}
            self.before_dir_node_to_path = {}
            self.before_file_node_to_path = {}

            for info in files_info:
                rel = str(info.get("rel_path") or info.get("name"))
                rel = rel.strip().replace("\\", "/")
                parts = [p for p in rel.split("/") if p]
                if not parts:
                    continue

                *dir_parts, file_name = parts
                parent_id = ""
                curr_path = ""

                for part in dir_parts:
                    curr_path = f"{curr_path}/{part}" if curr_path else part
                    if curr_path not in before_nodes:
                        node_id = self.before_tree.insert(parent_id, tk.END, text=part)
                        before_nodes[curr_path] = node_id
                        self.before_dir_node_to_path[node_id] = curr_path
                    parent_id = before_nodes[curr_path]
                    before_counts[curr_path] = before_counts.get(curr_path, 0) + 1

                # 文件作为叶子挂在最终目录节点下（或根节点）
                ext = os.path.splitext(file_name)[1].lower()
                icon = FILE_ICONS.get(ext, FILE_ICONS["default"])
                file_rel_path = rel  # rel 已是相对根目录的完整路径
                file_id = self.before_tree.insert(
                    parent_id, tk.END, text=f"{icon} {file_name}"
                )
                self.before_file_node_to_path[file_id] = file_rel_path

            # 为每个目录节点追加文件数量统计
            for path, node_id in before_nodes.items():
                name = path.split("/")[-1] if path else ""
                count = before_counts.get(path, 0)
                label = f"{name} ({count})" if count > 0 else name
                self.before_tree.item(node_id, text=label)

        # 刷新右侧“整理后结构”树状预览
        if self.group_tree is not None:
            # 记录当前展开的目录路径和选中的文件索引，用于刷新后恢复状态
            open_paths: set[str] = set()
            selected_index: int | None = None
            try:
                for path, node_id in self.group_dir_nodes.items():
                    if bool(self.group_tree.item(node_id, "open")):
                        open_paths.add(path)
                sel = self.group_tree.selection()
                if sel:
                    sel_id = sel[0]
                    if sel_id in self.group_file_node_to_index:
                        selected_index = self.group_file_node_to_index[sel_id]
            except Exception:  # noqa: BLE001
                open_paths = set()
                selected_index = None

            for item in self.group_tree.get_children():
                self.group_tree.delete(item)

            # 重置节点映射
            self.group_dir_nodes = {}
            self.group_dir_node_to_path = {}
            self.group_file_node_to_index = {}

            # nodes 用于缓存“路径 -> tree item id”
            nodes: Dict[str, str] = {}
            # counts 统计每个目录节点下包含的文件数量（含子孙）
            counts: Dict[str, int] = {}

            for idx, s in enumerate(self.suggestions):
                # 如果开启“只显示高风险”，则仅展示大文件或近期文件
                if self.filter_risky_only_var.get() and not (
                    getattr(s, "is_large", False) or getattr(s, "is_recent", False)
                ):
                    continue
                # 将建议子文件夹路径拆分为多级目录
                folder = s.suggested_subfolder.strip().strip("/\\")
                folder = folder.replace("\\", "/")
                parts = [p for p in folder.split("/") if p] if folder else []

                parent_id = ""
                curr_path = ""

                for part in parts:
                    curr_path = f"{curr_path}/{part}" if curr_path else part
                    if curr_path not in nodes:
                        # 在当前父节点下创建新的目录节点
                        node_id = self.group_tree.insert(parent_id, tk.END, text=part)
                        nodes[curr_path] = node_id
                        self.group_dir_nodes[curr_path] = node_id
                        self.group_dir_node_to_path[node_id] = curr_path
                    parent_id = nodes[curr_path]
                    # 该目录（及其父级）下文件计数 +1
                    counts[curr_path] = counts.get(curr_path, 0) + 1

                # 在最终目录节点下添加文件名叶子节点
                ext = os.path.splitext(s.name)[1].lower()
                icon = FILE_ICONS.get(ext, FILE_ICONS["default"])

                # 高风险文件前面加上 ⚠ 提醒
                is_risky = getattr(s, "is_large", False) or getattr(s, "is_recent", False)
                label = f"{icon} {s.name}"
                if is_risky:
                    label = f"⚠ {label}"

                file_id = self.group_tree.insert(parent_id, tk.END, text=label)
                self.group_file_node_to_index[file_id] = idx

            # 为每个“整理后”目录节点追加文件数量统计
            for path, node_id in nodes.items():
                name = path.split("/")[-1] if path else ""
                count = counts.get(path, 0)
                label = f"{name} ({count})" if count > 0 else name
                self.group_tree.item(node_id, text=label)
                # 恢复展开状态
                if path in open_paths:
                    self.group_tree.item(node_id, open=True)

            # 恢复选中项（如果还能找到对应索引）
            if selected_index is not None:
                for node_id, idx in self.group_file_node_to_index.items():
                    if idx == selected_index:
                        try:
                            self.group_tree.selection_set(node_id)
                        except Exception:  # noqa: BLE001
                            pass
                        break

    def _set_busy(self, busy: bool) -> None:
        self.is_busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        if self.analyze_btn is not None:
            self.analyze_btn.config(state=state)
        if self.execute_btn is not None:
            self.execute_btn.config(state=state)
        # 撤销按钮只有在有记录时才可用
        self._update_undo_state()
        self.root.config(cursor="watch" if busy else "")
        self.root.update_idletasks()

    def _set_before_loading(self, loading: bool) -> None:
        if self.before_spinner is None:
            return
        if loading:
            self.before_spinner.start(80)
        else:
            self.before_spinner.stop()
            self.before_spinner["value"] = 0

    def _set_after_loading(self, loading: bool) -> None:
        if self.after_spinner is None:
            return
        if loading:
            self.after_spinner.start(80)
        else:
            self.after_spinner.stop()
            self.after_spinner["value"] = 0

    # 右侧整理后结构的交互：拖拽与右键菜单
    def _on_group_button_press(self, event: tk.Event) -> None:  # type: ignore[override]
        if self.group_tree is None:
            return
        item = self.group_tree.identify_row(event.y)
        self.group_drag_item_id = item
        self.group_drag_is_dir = bool(
            item and item in self.group_dir_node_to_path and item not in self.group_file_node_to_index
        )
        self.group_hover_target_id = None

    def _on_group_button_release(self, event: tk.Event) -> None:  # type: ignore[override]
        if self.group_tree is None or not self.group_drag_item_id:
            return

        src_id = self.group_drag_item_id
        self.group_drag_item_id = None
        self.group_hover_target_id = None

        dest_id = self.group_tree.identify_row(event.y)
        if not dest_id:
            return

        # 如果落在文件节点上，则移动到其父目录
        if dest_id in self.group_file_node_to_index:
            dest_id = self.group_tree.parent(dest_id)

        # 只能拖到目录节点或根
        new_folder_path = ""
        if dest_id:
            if dest_id not in self.group_dir_node_to_path:
                return
            new_folder_path = self.group_dir_node_to_path[dest_id]

        # 拖拽的是目录：整体重定向该目录及其子目录下所有文件
        if self.group_drag_is_dir and src_id in self.group_dir_node_to_path:
            src_path = self.group_dir_node_to_path[src_id]
            # 不允许拖到自己或自己的子目录
            if new_folder_path and (
                new_folder_path == src_path or new_folder_path.startswith(src_path + "/")
            ):
                return
            src_basename = src_path.split("/")[-1] if "/" in src_path else src_path
            # 目标根路径 = 目标目录下的 src 目录本身
            if new_folder_path:
                target_root = f"{new_folder_path}/{src_basename}"
            else:
                target_root = src_basename

            for s in self.suggestions:
                folder = (s.suggested_subfolder or "").replace("\\", "/")
                if folder == src_path or folder.startswith(src_path + "/"):
                    suffix = folder[len(src_path) :].lstrip("/")
                    s.suggested_subfolder = (
                        f"{target_root}/{suffix}" if suffix else target_root
                    )
        else:
            # 拖拽的是文件：只更新该文件的目标子目录
            idx = self.group_file_node_to_index.get(src_id)
            if idx is None or idx < 0 or idx >= len(self.suggestions):
                return
            self.suggestions[idx].suggested_subfolder = new_folder_path
        # 刷新预览（使用上一次的 files_info）
        if self.last_files_info:
            self._refresh_tree(self.last_files_info)

    def _on_group_motion(self, event: tk.Event) -> None:  # type: ignore[override]
        if self.group_tree is None or not self.group_drag_item_id:
            return
        dest_id = self.group_tree.identify_row(event.y)
        if not dest_id:
            return
        # 文件则高亮其父目录
        if dest_id in self.group_file_node_to_index:
            dest_id = self.group_tree.parent(dest_id)
        if dest_id == self.group_hover_target_id:
            return
        self.group_hover_target_id = dest_id
        try:
            if dest_id:
                self.group_tree.selection_set(dest_id)
                label = self.group_tree.item(dest_id, "text")
                if self._get_ui_lang() == "en":
                    self.status_var.set(f"Dragging to: {label}")
                else:
                    self.status_var.set(f"拖动到目标：{label}")
        except Exception:  # noqa: BLE001
            pass

    # 双击打开：在系统中打开文件或目录
    def _on_before_double_click(self, event: tk.Event) -> None:  # type: ignore[override]
        if self.before_tree is None:
            return
        item = self.before_tree.identify_row(event.y)
        if not item:
            return
        base = pathlib.Path(self.folder_path_var.get().strip())
        if not base:
            return
        # 文件
        if item in self.before_file_node_to_path:
            rel = self.before_file_node_to_path[item]
            target = base / rel
        # 目录
        elif item in self.before_dir_node_to_path:
            rel_dir = self.before_dir_node_to_path[item]
            target = base / rel_dir
        else:
            return
        try:
            if target.exists():
                os.startfile(str(target))
        except Exception:  # noqa: BLE001
            pass

    def _on_group_double_click(self, event: tk.Event) -> None:  # type: ignore[override]
        if self.group_tree is None:
            return
        item = self.group_tree.identify_row(event.y)
        if not item:
            return
        base = pathlib.Path(self.folder_path_var.get().strip())
        if not base:
            return
        # 双击文件：打开当前真实位置的源文件
        if item in self.group_file_node_to_index:
            idx = self.group_file_node_to_index[item]
            if 0 <= idx < len(self.suggestions):
                rel = self.suggestions[idx].rel_path
                target = base / rel
            else:
                return
        # 双击目录：尝试打开目标目录（可能尚未创建）
        elif item in self.group_dir_node_to_path:
            folder_rel = self.group_dir_node_to_path[item]
            target = base / folder_rel
        else:
            return
        try:
            if target.exists():
                os.startfile(str(target))
        except Exception:  # noqa: BLE001
            pass

    def _on_group_right_click(self, event: tk.Event) -> None:  # type: ignore[override]
        if self.group_tree is None:
            return
        item = self.group_tree.identify_row(event.y)
        if item:
            self.group_tree.selection_set(item)
        try:
            self.group_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.group_context_menu.grab_release()

    def _create_folder_and_move(self) -> None:
        if self.group_tree is None or not self.last_files_info:
            return
        selection = self.group_tree.selection()
        if not selection:
            return

        # 找到一个参考目录路径（以第一个选中项为准）
        ref_id = selection[0]
        if ref_id in self.group_file_node_to_index:
            parent_id = self.group_tree.parent(ref_id)
        else:
            parent_id = ref_id

        base_path = ""
        if parent_id and parent_id in self.group_dir_node_to_path:
            base_path = self.group_dir_node_to_path[parent_id]

        # 简单对话框获取子文件夹名称
        from tkinter import simpledialog

        if self._get_ui_lang() == "en":
            folder_name = simpledialog.askstring(
                "Create subfolder",
                "Please enter the new subfolder name:",
                parent=self.root,
            )
        else:
            folder_name = simpledialog.askstring(
                "新建子文件夹",
                "请输入新子文件夹名称：",
                parent=self.root,
            )
        if not folder_name:
            return
        folder_name = folder_name.strip().strip("/\\")
        if not folder_name:
            return

        # 新路径 = base_path / folder_name
        if base_path:
            new_folder = f"{base_path}/{folder_name}"
        else:
            new_folder = folder_name

        # 将选中的文件移动到该新子文件夹
        for node_id in selection:
            idx = self.group_file_node_to_index.get(node_id)
            if idx is None:
                continue
            if 0 <= idx < len(self.suggestions):
                self.suggestions[idx].suggested_subfolder = new_folder

        self._refresh_tree(self.last_files_info)

    def _remove_selected_from_plan(self) -> None:
        if self.group_tree is None or not self.last_files_info:
            return
        selection = list(self.group_tree.selection())
        if not selection:
            return

        # 收集要移除的 suggestion 索引
        to_remove: set[int] = set()
        for node_id in selection:
            if node_id in self.group_file_node_to_index:
                idx = self.group_file_node_to_index[node_id]
                to_remove.add(idx)
            elif node_id in self.group_dir_node_to_path:
                path = self.group_dir_node_to_path[node_id]
                # 删除该目录及其子目录下的所有文件计划
                for i, s in enumerate(self.suggestions):
                    folder = (s.suggested_subfolder or "").replace("\\", "/")
                    if folder == path or folder.startswith(path + "/"):
                        to_remove.add(i)

        if not to_remove:
            return

        # 重新构建 suggestions，排除这些索引
        self.suggestions = [
            s for i, s in enumerate(self.suggestions) if i not in to_remove
        ]
        self._refresh_tree(self.last_files_info)

    def _update_undo_state(self) -> None:
        if self.undo_btn is None:
            return
        if self.is_busy:
            self.undo_btn.config(state=tk.DISABLED)
        else:
            self.undo_btn.config(
                state=tk.NORMAL if self.last_moves else tk.DISABLED
            )

    def _collect_files_info(self, folder_path: pathlib.Path) -> List[Dict[str, Any]]:
        files_info: List[Dict[str, Any]] = []
        for root_dir, _dirs, files in os.walk(folder_path):
            root_path = pathlib.Path(root_dir)
            for filename in files:
                f = root_path / filename
                rel_path = f.relative_to(folder_path).as_posix()
                info: Dict[str, Any] = {
                    "name": f.name,
                    "ext": f.suffix.lower(),
                    "size": f.stat().st_size,
                    "mtime": f.stat().st_mtime,
                    "rel_path": rel_path,
                }
                if f.suffix.lower() in SUPPORTED_TEXT_EXTS:
                    try:
                        with f.open("r", encoding="utf-8", errors="ignore") as fp:
                            sample = fp.read(800)
                            info["sample"] = sample
                    except OSError:
                        pass
                files_info.append(info)
        return files_info

    def _scan_files_worker(
        self,
        folder_path: pathlib.Path,
        language: str,
        mode: str,
        custom_instruction: str,
    ) -> None:
        try:
            files_info = self._collect_files_info(folder_path)
        except OSError as e:
            self.root.after(
                0,
                self._on_scan_error,
                f"读取文件夹失败：{e}",
            )
            return

        if not files_info:
            self.root.after(0, self._on_analyze_no_files)
            return

        self.root.after(
            0,
            self._on_scan_success,
            folder_path,
            files_info,
            language,
            mode,
            custom_instruction,
        )

    def _deepseek_worker(
        self,
        folder_path: pathlib.Path,
        files_info: List[Dict[str, Any]],
        language: str,
        mode: str,
        custom_instruction: str,
    ) -> None:
        # 过滤掉用户选择“不整理”的文件类型
        movable_files = [
            info for info in files_info if info.get("ext") not in self.skip_exts
        ]
        if not movable_files:
            self.root.after(
                0,
                self._on_analyze_no_files,
            )
            return

        try:
            client = DeepSeekClient()
            raw_results = client.organize_files(
                str(folder_path),
                movable_files,
                language=language,
                mode=mode,
                custom_instruction=custom_instruction or None,
            )
        except Exception as e:  # noqa: BLE001
            self.root.after(
                0,
                self._on_analyze_error,
                f"调用 DeepSeek 失败：{e}",
            )
            return

        self.root.after(
            0,
            self._on_analyze_success,
            movable_files,
            raw_results,
        )

    def _on_analyze_no_files(self) -> None:
        self._set_busy(False)
        self._set_before_loading(False)
        self._set_after_loading(False)
        if self._get_ui_lang() == "en":
            messagebox.showinfo(
                "Info",
                "There are no files to organize in this folder or its subfolders.",
            )
            self.status_var.set(
                "There are no files to organize in this folder."
            )
        else:
            messagebox.showinfo("提示", "该文件夹及其子文件夹中没有可整理的文件。")
            self.status_var.set("该文件夹中没有可整理的文件。")

    def _on_analyze_error(self, msg: str) -> None:
        self._set_busy(False)
        self._set_before_loading(False)
        self._set_after_loading(False)
        if self._get_ui_lang() == "en":
            messagebox.showerror("Error", msg)
        else:
            messagebox.showerror("错误", msg)
        self.status_var.set(msg)

    def _on_scan_error(self, msg: str) -> None:
        self._set_busy(False)
        self._set_before_loading(False)
        self._set_after_loading(False)
        if self._get_ui_lang() == "en":
            messagebox.showerror("Error", msg)
        else:
            messagebox.showerror("错误", msg)
        self.status_var.set(msg)

    def _on_scan_success(
        self,
        folder_path: pathlib.Path,
        files_info: List[Dict[str, Any]],
        language: str,
        mode: str,
        custom_instruction: str,
    ) -> None:
        # 扫描完成，展示原始结构
        self._set_before_loading(False)
        self._refresh_tree(files_info)
        if self._get_ui_lang() == "en":
            self.status_var.set(
                "Original structure loaded. Calling DeepSeek to generate plan..."
            )
        else:
            self.status_var.set("原始结构已加载，正在调用 DeepSeek 生成整理方案...")
        # 调用 DeepSeek 阶段
        self._set_after_loading(True)
        threading.Thread(
            target=self._deepseek_worker,
            args=(folder_path, files_info, language, mode, custom_instruction),
            daemon=True,
        ).start()

    def _on_analyze_success(
        self,
        files_info: List[Dict[str, Any]],
        raw_results: List[Dict[str, Any]],
    ) -> None:
        self._set_after_loading(False)
        self._set_busy(False)

        if not raw_results:
            if self._get_ui_lang() == "en":
                messagebox.showwarning(
                    "Warning",
                    "No organizing plan was returned from DeepSeek.",
                )
                self.status_var.set("No organizing plan was returned from DeepSeek.")
            else:
                messagebox.showwarning("提示", "未从 DeepSeek 获得整理方案。")
                self.status_var.set("未从 DeepSeek 获得整理方案。")
            return

        self.suggestions = []
        self.last_files_info = files_info

        # 计算判定“大文件/近期文件”的辅助值
        now_ts = time.time()
        large_bytes = LARGE_FILE_THRESHOLD_MB * 1024 * 1024
        recent_seconds = RECENT_FILE_THRESHOLD_DAYS * 24 * 3600

        # 要求模型保持顺序，一一对应；若长度不一致，取较短长度
        count = min(len(files_info), len(raw_results))
        for i in range(count):
            info = files_info[i]
            item = raw_results[i] or {}

            name = item.get("name") or info.get("name")
            if not name:
                continue
            suggested_subfolder = item.get("suggested_subfolder") or "其他"
            reason = item.get("reason") or ""
            rel_path = info.get("rel_path") or name

            size = info.get("size") or 0
            mtime = info.get("mtime") or 0
            is_large = size >= large_bytes
            is_recent = (now_ts - mtime) <= recent_seconds if mtime else False

            self.suggestions.append(
                FileSuggestion(
                    name=name,
                    rel_path=rel_path,
                    suggested_subfolder=suggested_subfolder,
                    reason=reason,
                    is_large=is_large,
                    is_recent=is_recent,
                )
            )

        self._refresh_tree(files_info)
        if self._get_ui_lang() == "en":
            self.status_var.set(
                "Organizing plan generated. Review it, then click "
                "\"Apply organizing (move files)\"."
            )
        else:
            self.status_var.set("整理方案已生成，请确认无误后点击“执行整理”。")

    def _execute_organize_worker(self, folder_path: pathlib.Path) -> None:
        base = folder_path
        moved_count = 0
        errors: List[str] = []
        moves: List[MoveRecord] = []

        for suggestion in self.suggestions:
            src = base / suggestion.rel_path
            if not src.exists() or not src.is_file():
                continue

            # 规范化子目录路径，避免绝对路径注入
            relative = pathlib.Path(
                suggestion.suggested_subfolder.strip().lstrip("/\\")
            )
            target_dir = base / relative

            try:
                target_dir.mkdir(parents=True, exist_ok=True)
                dst = target_dir / src.name
                # 如果目标已存在，优先判断是否同一文件或内容相同，避免生成过多后缀
                if dst.exists():
                    try:
                        # 已经在目标位置了，跳过
                        if dst.samefile(src):
                            continue
                    except OSError:
                        pass
                    try:
                        # 尺寸相同，视为重复文件，跳过
                        if src.stat().st_size == dst.stat().st_size:
                            continue
                    except OSError:
                        pass
                    # 否则再使用 _1、_2 这种后缀避免覆盖
                    stem = dst.stem
                    suffix = dst.suffix
                    i = 1
                    while True:
                        candidate = target_dir / f"{stem}_{i}{suffix}"
                        if not candidate.exists():
                            dst = candidate
                            break
                        i += 1
                shutil.move(str(src), str(dst))
                moved_count += 1
                moves.append(MoveRecord(src=src, dst=dst))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{src}: {e}")

        self.root.after(
            0,
            self._on_execute_done,
            moved_count,
            errors,
            moves,
        )

    def _on_execute_done(
        self,
        moved_count: int,
        errors: List[str],
        moves: List[MoveRecord],
    ) -> None:
        self._set_busy(False)

        self.last_moves = moves
        self._update_undo_state()

        if errors:
            msg = "\n".join(
                textwrap.shorten(err, width=120, placeholder="...")
                for err in errors[:10]
            )
            if self._get_ui_lang() == "en":
                full_msg = "Some files failed to move:\n" + msg
                messagebox.showwarning("Completed (with errors)", full_msg)
            else:
                full_msg = "部分文件移动失败：\n" + msg
                messagebox.showwarning("完成（部分失败）", full_msg)
        else:
            if self._get_ui_lang() == "en":
                messagebox.showinfo(
                    "Completed",
                    f"Organizing finished. {moved_count} files were moved.",
                )
            else:
                messagebox.showinfo("完成", f"文件整理完成，共移动 {moved_count} 个文件。")

        if self._get_ui_lang() == "en":
            self.status_var.set(
                f"Organizing finished. {moved_count} files were moved."
            )
        else:
            self.status_var.set(f"整理完成，共移动 {moved_count} 个文件。")
        self._clear_suggestions()

    def _undo_organize_worker(self) -> None:
        undone = 0
        errors: List[str] = []

        # 反向移动，尽量减少路径冲突
        for record in reversed(self.last_moves):
            if not record.dst.exists():
                continue
            try:
                record.src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(record.dst), str(record.src))
                undone += 1
            except Exception as e:  # noqa: BLE001
                errors.append(f"{record.dst} -> {record.src}: {e}")

        self.root.after(
            0,
            self._on_undo_done,
            undone,
            errors,
        )

    def _on_undo_done(self, undone: int, errors: List[str]) -> None:
        self._set_busy(False)

        if errors:
            msg = "\n".join(
                textwrap.shorten(err, width=120, placeholder="...")
                for err in errors[:10]
            )
            if self._get_ui_lang() == "en":
                full_msg = "Some files could not be moved back:\n" + msg
                messagebox.showwarning("Undo completed (with errors)", full_msg)
            else:
                full_msg = "部分文件回退失败：\n" + msg
                messagebox.showwarning("撤销完成（部分失败）", full_msg)
        else:
            if self._get_ui_lang() == "en":
                messagebox.showinfo(
                    "Undo completed",
                    f"{undone} files were moved back to their original locations.",
                )
            else:
                messagebox.showinfo("撤销完成", f"已将 {undone} 个文件移回原位置。")

        if self._get_ui_lang() == "en":
            self.status_var.set(
                f"Undo completed. {undone} files were moved back."
            )
        else:
            self.status_var.set(f"撤销完成，已回退 {undone} 个文件。")
        # 撤销完成后清空记录
        self.last_moves = []
        self._update_undo_state()


def main() -> None:
    root = tk.Tk()
    app = FileOrganizerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()

