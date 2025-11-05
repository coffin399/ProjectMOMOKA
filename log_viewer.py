import tkinter as tk
from tkinter import scrolledtext, ttk
import logging
import queue
import threading
from datetime import datetime
import os
import json
import ctypes
import platform

def is_dark_mode():
    """OSのダークモード設定を検出"""
    try:
        if platform.system() == 'Windows':
            import darkdetect
            return darkdetect.isDark()
        return False
    except Exception as e:
        print(f"ダークモード検出エラー: {e}")
        return False

# ダークモードの色設定
DARK_BG = '#1e1e1e'
DARK_FG = '#e0e0e0'
DARK_SELECTION_BG = '#264f78'
DARK_SELECTION_FG = '#ffffff'
DARK_INSERT_BG = '#3c3c3c'
DARK_INSERT_FG = '#ffffff'
DARK_SCROLLBAR_BG = '#2d2d2d'
DARK_SCROLLBAR_TROUGH = '#1e1e1e'

# ライトモードの色設定
LIGHT_BG = '#f0f0f0'
LIGHT_FG = '#000000'
LIGHT_SELECTION_BG = '#cce8ff'
LIGHT_SELECTION_FG = '#000000'
LIGHT_INSERT_BG = '#ffffff'
LIGHT_INSERT_FG = '#000000'
LIGHT_SCROLLBAR_BG = '#e0e0e0'
LIGHT_SCROLLBAR_TROUGH = '#f0f0f0'

# 現在のテーマを決定
DARK_THEME = is_dark_mode()

def get_theme_colors():
    """現在のテーマに応じた色を返す"""
    if DARK_THEME:
        return {
            'bg': DARK_BG,
            'fg': DARK_FG,
            'select_bg': DARK_SELECTION_BG,
            'select_fg': DARK_SELECTION_FG,
            'insert_bg': DARK_INSERT_BG,
            'insert_fg': DARK_INSERT_FG,
            'scrollbar_bg': DARK_SCROLLBAR_BG,
            'scrollbar_trough': DARK_SCROLLBAR_TROUGH,
            'button_bg': '#2d2d2d',
            'button_fg': DARK_FG,
            'button_active_bg': '#3c3c3c',
            'button_active_fg': DARK_FG,
            'frame_bg': DARK_BG,
            'label_bg': DARK_BG,
            'label_fg': DARK_FG,
            'entry_bg': DARK_INSERT_BG,
            'entry_fg': DARK_INSERT_FG,
            'text_bg': DARK_INSERT_BG,
            'text_fg': DARK_INSERT_FG,
            'border': '#3c3c3c',
            'error': '#ff6b6b',
            'warning': '#ffd93d',
            'info': '#4dabf7',
            'debug': '#adb5bd'
        }
    else:
        return {
            'bg': LIGHT_BG,
            'fg': LIGHT_FG,
            'select_bg': LIGHT_SELECTION_BG,
            'select_fg': LIGHT_SELECTION_FG,
            'insert_bg': LIGHT_INSERT_BG,
            'insert_fg': LIGHT_INSERT_FG,
            'scrollbar_bg': LIGHT_SCROLLBAR_BG,
            'scrollbar_trough': LIGHT_SCROLLBAR_TROUGH,
            'button_bg': '#e0e0e0',
            'button_fg': LIGHT_FG,
            'button_active_bg': '#d0d0d0',
            'button_active_fg': LIGHT_FG,
            'frame_bg': LIGHT_BG,
            'label_bg': LIGHT_BG,
            'label_fg': LIGHT_FG,
            'entry_bg': LIGHT_INSERT_BG,
            'entry_fg': LIGHT_INSERT_FG,
            'text_bg': LIGHT_INSERT_BG,
            'text_fg': LIGHT_INSERT_FG,
            'border': '#c0c0c0',
            'error': '#dc3545',
            'warning': '#ffc107',
            'info': '#0d6efd',
            'debug': '#6c757d'
        }

class LogHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record):
        log_entry = self.format(record)
        self.log_queue.put((record.name, record.levelname, log_entry))

class LogViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("MOMOKA ログビューア")
        self.root.geometry("1200x800")
        
        # テーマカラーを取得
        self.theme = get_theme_colors()
        
        # メインウィンドウの背景色を設定
        self.root.configure(bg=self.theme['bg'])
        
        # 設定ファイルの読み込み
        self.config_file = "data/log_viewer_config.json"
        self.load_config()
        
        # スタイルの設定を初期化（self.styleとして保存）
        self.style = ttk.Style()
        self.setup_styles()
        
        # ログキュー
        self.log_queue = queue.Queue()
        
        # メニューバーの作成
        self.create_menu()
        
        # ロガーの設定
        self.setup_logging()
        
        # GUIの作成
        self.setup_gui()
        
        # キューを定期的にチェック
        self.poll_log_queue()
        
        # ウィンドウクローズ時の処理
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
        # Windowsのダークモード設定を適用
        self.apply_windows_dark_mode()
    
    def apply_windows_dark_mode(self):
        """Windowsのダークモード設定を適用"""
        if DARK_THEME and platform.system() == 'Windows':
            try:
                DWMWA_USE_IMMERSIVE_DARK_MODE = 20
                hwnd = ctypes.windll.user32.GetForegroundWindow()
                value = 1
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, 
                    DWMWA_USE_IMMERSIVE_DARK_MODE,
                    ctypes.byref(ctypes.c_int(value)),
                    ctypes.sizeof(ctypes.c_int(value))
                )
            except Exception as e:
                print(f"ダークモードの適用中にエラーが発生しました: {e}")
    
    def create_menu(self):
        """メニューバーの作成"""
        self.menubar = tk.Menu(self.root, 
                             bg=self.theme['bg'], 
                             fg=self.theme['fg'],
                             activebackground=self.theme['select_bg'],
                             activeforeground=self.theme['select_fg'],
                             relief='flat',
                             bd=0)
        
        # ファイルメニュー
        file_menu = tk.Menu(self.menubar, 
                          tearoff=0, 
                          bg=self.theme['bg'], 
                          fg=self.theme['fg'],
                          activebackground=self.theme['select_bg'],
                          activeforeground=self.theme['select_fg'],
                          bd=1,
                          relief='solid')
        file_menu.add_command(label="終了", 
                            command=self.root.quit,
                            activebackground=self.theme['select_bg'],
                            activeforeground=self.theme['select_fg'])
        self.menubar.add_cascade(label="ファイル", menu=file_menu)
        
        # 表示メニュー
        view_menu = tk.Menu(self.menubar, 
                          tearoff=0,
                          bg=self.theme['bg'],
                          fg=self.theme['fg'],
                          activebackground=self.theme['select_bg'],
                          activeforeground=self.theme['select_fg'],
                          bd=1,
                          relief='solid')
        
        # 自動スクロールの状態変数を初期化
        self.auto_scroll_var = tk.BooleanVar(value=self.config.get("auto_scroll", True))
        view_menu.add_checkbutton(label="自動スクロール", 
                                variable=self.auto_scroll_var,
                                command=self.toggle_auto_scroll,
                                activebackground=self.theme['select_bg'],
                                activeforeground=self.theme['select_fg'])
        
        self.menubar.add_cascade(label="表示", menu=view_menu)
        
        # ヘルプメニュー
        help_menu = tk.Menu(self.menubar, 
                           tearoff=0,
                           bg=self.theme['bg'],
                           fg=self.theme['fg'],
                           activebackground=self.theme['select_bg'],
                           activeforeground=self.theme['select_fg'],
                           bd=1,
                           relief='solid')
        help_menu.add_command(label="バージョン情報",
                            command=self.show_about,
                            activebackground=self.theme['select_bg'],
                            activeforeground=self.theme['select_fg'])
        self.menubar.add_cascade(label="ヘルプ", menu=help_menu)
        
        self.root.config(menu=self.menubar)
        
        # メニューのスタイルオプション
        self.root.option_add('*Menu*background', self.theme['bg'])
        self.root.option_add('*Menu*foreground', self.theme['fg'])
        self.root.option_add('*Menu*activeBackground', self.theme['select_bg'])
        self.root.option_add('*Menu*activeForeground', self.theme['select_fg'])
    
    def setup_styles(self):
        """スタイルの初期化のみを行う"""
        # テーマの設定
        self.style.theme_use('clam')
        
        # フレームのスタイル
        self.style.configure('TFrame', 
                      background=self.theme['bg'],
                      borderwidth=0)
        
        # ラベルのスタイル
        self.style.configure('TLabel', 
                      background=self.theme['bg'], 
                      foreground=self.theme['fg'],
                      font=('Meiryo UI', 9),
                      padding=2)
        
        # ボタンのスタイル
        self.style.configure('TButton',
                      background=self.theme['button_bg'],
                      foreground=self.theme['button_fg'],
                      borderwidth=1,
                      relief='raised',
                      padding=5)
        
        self.style.map('TButton',
                 background=[('active', self.theme['button_active_bg']),
                           ('pressed', self.theme['select_bg'])],
                 foreground=[('active', self.theme['button_active_fg']),
                           ('pressed', self.theme['select_fg'])],
                 relief=[('pressed', 'sunken'), ('!pressed', 'raised')])
        
        # エントリーのスタイル
        self.style.configure('TEntry',
                      fieldbackground=self.theme['entry_bg'],
                      foreground=self.theme['entry_fg'],
                      insertcolor=self.theme['insert_fg'],
                      borderwidth=1,
                      relief='solid')
        
        # コンボボックスのスタイル
        self.style.configure('TCombobox',
                      fieldbackground=self.theme['entry_bg'],
                      background=self.theme['entry_bg'],
                      foreground=self.theme['entry_fg'],
                      selectbackground=self.theme['select_bg'],
                      selectforeground=self.theme['select_fg'],
                      arrowcolor=self.theme['fg'],
                      borderwidth=1,
                      relief='solid')
        
        self.style.map('TCombobox',
                      fieldbackground=[('readonly', self.theme['entry_bg'])],
                      selectbackground=[('readonly', self.theme['select_bg'])],
                      selectforeground=[('readonly', self.theme['select_fg'])])
        
        # スクロールバーのスタイル
        self.style.configure('Vertical.TScrollbar',
                      background=self.theme['scrollbar_bg'],
                      troughcolor=self.theme['scrollbar_trough'],
                      arrowcolor=self.theme['fg'],
                      bordercolor=self.theme['bg'],
                      darkcolor=self.theme['bg'],
                      lightcolor=self.theme['bg'],
                      gripcount=0,
                      arrowsize=12)
        
        self.style.configure('Horizontal.TScrollbar',
                      background=self.theme['scrollbar_bg'],
                      troughcolor=self.theme['scrollbar_trough'],
                      arrowcolor=self.theme['fg'],
                      bordercolor=self.theme['bg'],
                      darkcolor=self.theme['bg'],
                      lightcolor=self.theme['bg'],
                      gripcount=0,
                      arrowsize=12)
        
        self.style.map('Vertical.TScrollbar',
                      background=[('active', self.theme['scrollbar_bg'])])
        
        # ラベルフレームのスタイル
        self.style.configure('TLabelframe',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'],
                      relief='groove',
                      borderwidth=2)
        
        self.style.configure('TLabelframe.Label',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'])
                      
        # チェックボタンのスタイル
        self.style.configure('TCheckbutton',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'],
                      indicatorbackground=self.theme['bg'],
                      indicatorcolor=self.theme['fg'],
                      selectcolor=self.theme['bg'])
        
        self.style.map('TCheckbutton',
                 background=[('active', self.theme['bg'])],
                 foreground=[('active', self.theme['fg'])])
        
        # ラジオボタンのスタイル
        self.style.configure('TRadiobutton',
                      background=self.theme['bg'],
                      foreground=self.theme['fg'],
                      indicatorbackground=self.theme['bg'],
                      indicatorcolor=self.theme['fg'],
                      selectcolor=self.theme['bg'])
        
        self.style.map('TRadiobutton',
                 background=[('active', self.theme['bg'])],
                 foreground=[('active', self.theme['fg'])])
        
        # メニューボタンのスタイル
        self.style.configure('TMenubutton',
                           borderwidth=2)
    
    def load_config(self):
        """設定ファイルの読み込み"""
        self.config = {
            "font": ("Meiryo UI", 9),
            "max_lines": 1000,
            "auto_scroll": True,
            "log_levels": {
                "general": "INFO",
                "llm": "INFO",
                "tts": "INFO",
                "error": "WARNING"
            }
        }
        
        try:
            if os.path.exists(self.config_file):
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    saved_config = json.load(f)
                    self.config.update(saved_config)
        except Exception as e:
            print(f"設定ファイルの読み込み中にエラーが発生しました: {e}")
    
    def save_config(self):
        """設定ファイルの保存"""
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"設定ファイルの保存中にエラーが発生しました: {e}")
        
    def setup_gui(self):
        """GUIの作成"""
        # メインフレーム
        main_frame = ttk.Frame(self.root, padding="5", style='TFrame')
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # コントロールフレーム
        control_frame = ttk.Frame(main_frame, padding="5", style='TFrame')
        control_frame.pack(fill=tk.X, padx=5, pady=5)
        
        # ログレベル選択
        log_level_frame = ttk.LabelFrame(control_frame, text="ログレベル", padding=5)
        log_level_frame.pack(side=tk.LEFT, padx=5, pady=5)
        
        # ログレベル用の変数を初期化
        self.general_level_var = tk.StringVar(value=self.config["log_levels"].get("general", "INFO"))
        self.llm_level_var = tk.StringVar(value=self.config["log_levels"].get("llm", "INFO"))
        self.tts_level_var = tk.StringVar(value=self.config["log_levels"].get("tts", "INFO"))
        self.error_level_var = tk.StringVar(value=self.config["log_levels"].get("error", "WARNING"))
        
        # 一般ログレベル
        ttk.Label(log_level_frame, text="一般:").grid(row=0, column=0, padx=2, pady=2, sticky=tk.W)
        general_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.general_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        general_level.grid(row=0, column=1, padx=2, pady=2)
        general_level.bind("<<ComboboxSelected>>", 
                          lambda e: self.update_log_level("general", self.general_level_var.get()))
        
        # LLMログレベル
        ttk.Label(log_level_frame, text="LLM:").grid(row=0, column=2, padx=2, pady=2, sticky=tk.W)
        llm_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.llm_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        llm_level.grid(row=0, column=3, padx=2, pady=2)
        llm_level.bind("<<ComboboxSelected>>", 
                      lambda e: self.update_log_level("llm", self.llm_level_var.get()))
        
        # TTSログレベル
        ttk.Label(log_level_frame, text="TTS:").grid(row=0, column=4, padx=2, pady=2, sticky=tk.W)
        tts_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.tts_level_var,
            values=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        tts_level.grid(row=0, column=5, padx=2, pady=2)
        tts_level.bind("<<ComboboxSelected>>", 
                      lambda e: self.update_log_level("tts", self.tts_level_var.get()))
        
        # エラーログレベル
        ttk.Label(log_level_frame, text="エラー:").grid(row=0, column=6, padx=2, pady=2, sticky=tk.W)
        error_level = ttk.Combobox(
            log_level_frame,
            textvariable=self.error_level_var,
            values=["WARNING", "ERROR", "CRITICAL"],
            state="readonly",
            width=10
        )
        error_level.grid(row=0, column=7, padx=2, pady=2)
        error_level.bind("<<ComboboxSelected>>", 
                        lambda e: self.update_log_level("error", self.error_level_var.get()))
        
        # ボタンフレーム
        button_frame = ttk.Frame(control_frame, style='TFrame')
        button_frame.pack(side=tk.RIGHT, padx=5, pady=5)
        
        # クリアボタン
        clear_button = ttk.Button(
            button_frame,
            text="ログをクリア",
            command=self.clear_all_logs
        )
        clear_button.pack(side=tk.LEFT, padx=2)
        
        # 自動スクロールチェックボタン
        auto_scroll = ttk.Checkbutton(
            button_frame,
            text="自動スクロール",
            variable=self.auto_scroll_var,
            command=self.toggle_auto_scroll
        )
        auto_scroll.pack(side=tk.LEFT, padx=2)
        
        # ログ表示エリアのフレーム
        log_frame = ttk.Frame(main_frame, style='TFrame')
        log_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        # グリッドの設定
        log_frame.columnconfigure(0, weight=1)
        log_frame.columnconfigure(1, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        
        # 左上: 一般ログ
        general_frame = ttk.LabelFrame(log_frame, text="一般ログ", padding="2", style='TLabelframe')
        general_frame.grid(row=0, column=0, padx=2, pady=2, sticky="nsew")
        self.general_log = scrolledtext.ScrolledText(
            general_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['text_fg'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.general_log.pack(fill=tk.BOTH, expand=True)
        
        # 右上: LLMログ
        llm_frame = ttk.LabelFrame(log_frame, text="LLMログ", padding="2", style='TLabelframe')
        llm_frame.grid(row=0, column=1, padx=2, pady=2, sticky="nsew")
        self.llm_log = scrolledtext.ScrolledText(
            llm_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['text_fg'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.llm_log.pack(fill=tk.BOTH, expand=True)
        
        # 左下: TTSログ
        tts_frame = ttk.LabelFrame(log_frame, text="TTSログ", padding="2", style='TLabelframe')
        tts_frame.grid(row=1, column=0, padx=2, pady=2, sticky="nsew")
        self.tts_log = scrolledtext.ScrolledText(
            tts_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['text_fg'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.tts_log.pack(fill=tk.BOTH, expand=True)
        
        # 右下: エラーログ
        error_frame = ttk.LabelFrame(log_frame, text="エラーログ", padding="2", style='TLabelframe')
        error_frame.grid(row=1, column=1, padx=2, pady=2, sticky="nsew")
        self.error_log = scrolledtext.ScrolledText(
            error_frame, 
            wrap=tk.WORD, 
            width=60, 
            height=15,
            font=self.config["font"],
            bg=self.theme['text_bg'],
            fg=self.theme['error'],
            insertbackground=self.theme['fg'],
            selectbackground=self.theme['select_bg'],
            selectforeground=self.theme['select_fg'],
            relief='flat'
        )
        self.error_log.pack(fill=tk.BOTH, expand=True)
        
        # ステータスバー
        self.status_var = tk.StringVar()
        self.status_var.set("準備完了")
        status_bar = ttk.Label(
            self.root, 
            textvariable=self.status_var, 
            relief=tk.SUNKEN, 
            anchor=tk.W,
            style='TLabel'
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, ipady=2)
        
        # コンテキストメニューの設定
        self.setup_context_menu(self.general_log)
        self.setup_context_menu(self.llm_log)
        self.setup_context_menu(self.tts_log)
        self.setup_context_menu(self.error_log)
        
        # キーバインドの設定
        self.root.bind_all("<Control-c>", lambda e: self.copy_text(self.root.focus_get()))
        self.root.bind_all("<Control-a>", lambda e: self.select_all(self.root.focus_get()))
        
        # 初期フォーカスを設定
        self.general_log.focus_set()
    
    def setup_context_menu(self, widget):
        """コンテキストメニューの設定"""
        def show_menu(event):
            menu = tk.Menu(self.root, tearoff=0,
                         bg=self.theme['bg'],
                         fg=self.theme['fg'],
                         activebackground=self.theme['select_bg'],
                         activeforeground=self.theme['select_fg'])
            menu.add_command(label="コピー", command=lambda: self.copy_text(widget))
            menu.add_separator()
            menu.add_command(label="すべて選択", command=lambda: self.select_all(widget))
            menu.add_command(label="クリア", command=lambda: self.clear_log(widget))
            
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
        
        widget.bind("<Button-3>", show_menu)
    
    def copy_text(self, widget):
        """選択されたテキストをコピー"""
        try:
            selected_text = widget.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected_text)
            self.status_var.set("選択されたテキストをクリップボードにコピーしました")
        except tk.TclError:
            self.status_var.set("コピーするテキストが選択されていません")
    
    def select_all(self, widget):
        """すべてのテキストを選択"""
        widget.tag_add(tk.SEL, "1.0", tk.END)
        widget.mark_set(tk.INSERT, "1.0")
        widget.see(tk.INSERT)
        return 'break'
    
    def clear_log(self, widget):
        """指定されたウィジェットのログをクリア"""
        widget.config(state='normal')
        widget.delete(1.0, tk.END)
        widget.config(state='disabled')
        self.status_var.set("ログをクリアしました")
    
    def clear_all_logs(self):
        """すべてのログをクリア"""
        for widget in [self.general_log, self.llm_log, self.tts_log, self.error_log]:
            self.clear_log(widget)
        self.status_var.set("すべてのログをクリアしました")
    
    def toggle_auto_scroll(self):
        """自動スクロールの切り替え"""
        self.config["auto_scroll"] = self.auto_scroll_var.get()
        self.save_config()
        status = "有効" if self.auto_scroll_var.get() else "無効"
        self.status_var.set(f"自動スクロールを{status}にしました")
    
    def update_log_level(self, log_type, level):
        """ログレベルの更新"""
        self.config["log_levels"][log_type] = level
        self.save_config()
        self.status_var.set(f"{log_type}のログレベルを{level}に設定しました")
    
    def setup_logging(self):
        """ロガーの設定"""
        # ルートロガーの設定
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG)
        
        # 既存のハンドラをクリア
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        
        # カスタムハンドラを追加
        custom_handler = LogHandler(self.log_queue)
        custom_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
        root_logger.addHandler(custom_handler)
        
    def poll_log_queue(self):
        """ログキューを定期的にチェック"""
        try:
            while True:
                name, level, log_entry = self.log_queue.get_nowait()
                self.process_log_entry(name, level, log_entry)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.poll_log_queue)
    
    def process_log_entry(self, name, level, log_entry):
        """ログエントリを処理"""
        # ログレベルに基づいてフィルタリング
        log_levels = {
            "DEBUG": 10,
            "INFO": 20,
            "WARNING": 30,
            "ERROR": 40,
            "CRITICAL": 50
        }
        
        # ログの種類を判定
        if "MOMOKA.llm" in name:
            log_type = "llm"
            widget = self.llm_log
            min_level = log_levels.get(self.llm_level_var.get(), 20)
        elif "MOMOKA.tts" in name:
            log_type = "tts"
            widget = self.tts_log
            min_level = log_levels.get(self.tts_level_var.get(), 20)
        elif level in ["ERROR", "CRITICAL"]:
            log_type = "error"
            widget = self.error_log
            min_level = log_levels.get(self.error_level_var.get(), 30)
        else:
            log_type = "general"
            widget = self.general_log
            min_level = log_levels.get(self.general_level_var.get(), 20)
        
        # ログレベルが閾値以上の場合のみ表示
        if log_levels.get(level, 0) >= min_level:
            self.append_to_log(widget, log_entry, level)
    
    def append_to_log(self, text_widget, message, level=None):
        """ログをテキストウィジェットに追加"""
        text_widget.config(state='normal')
        
        # 行数制限
        lines = int(text_widget.index('end-1c').split('.')[0])
        if lines > self.config["max_lines"]:
            text_widget.delete(1.0, f"{lines - self.config['max_lines']}.0")
        
        # レベルに応じた色付け
        if level == "ERROR" or level == "CRITICAL":
            text_widget.tag_config("error", foreground=self.theme['error'])
            text_widget.insert(tk.END, message + "\n", "error")
        elif level == "WARNING":
            text_widget.tag_config("warning", foreground=self.theme['warning'])
            text_widget.insert(tk.END, message + "\n", "warning")
        elif level == "INFO":
            text_widget.tag_config("info", foreground=self.theme['info'])
            text_widget.insert(tk.END, message + "\n", "info")
        elif level == "DEBUG":
            text_widget.tag_config("debug", foreground=self.theme['debug'])
            text_widget.insert(tk.END, message + "\n", "debug")
        else:
            text_widget.insert(tk.END, message + "\n")
        
        # 自動スクロール
        if self.config["auto_scroll"]:
            text_widget.see(tk.END)
        
        text_widget.config(state='disabled')
    
    def show_about(self):
        """バージョン情報を表示"""
        about_window = tk.Toplevel(self.root)
        about_window.title("バージョン情報")
        about_window.transient(self.root)
        about_window.resizable(False, False)
        about_window.configure(bg=self.theme['bg'])
        
        # 中央に配置
        window_width = 300
        window_height = 150
        screen_width = about_window.winfo_screenwidth()
        screen_height = about_window.winfo_screenheight()
        x = (screen_width // 2) - (window_width // 2)
        y = (screen_height // 2) - (window_height // 2)
        about_window.geometry(f'{window_width}x{window_height}+{x}+{y}')
        
        # バージョン情報
        version_label = ttk.Label(
            about_window,
            text="MOMOKA ログビューア\nバージョン 1.0.0\n\n© 2025 MOMOKA Project",
            justify=tk.CENTER,
            style='TLabel'
        )
        version_label.pack(expand=True, padx=20, pady=20)
        
        # OKボタン
        ok_button = ttk.Button(
            about_window,
            text="OK",
            command=about_window.destroy,
            style='TButton'
        )
        ok_button.pack(pady=(0, 20))
        
        # モーダルダイアログとして表示
        about_window.grab_set()
        about_window.focus_set()
        about_window.wait_window()
    
    def on_closing(self):
        """ウィンドウを閉じる時の処理"""
        # 設定を保存
        self.save_config()
        self.root.destroy()

def main():
    """メイン関数"""
    try:
        print("ログビューアを起動しています...")
        root = tk.Tk()
        print("Tkインスタンスを作成しました")
        app = LogViewerApp(root)
        print("LogViewerAppの初期化が完了しました")
        print("メインループを開始します...")
        root.mainloop()
        print("メインループが終了しました")
    except Exception as e:
        print(f"ログビューアでエラーが発生しました: {str(e)}")
        import traceback
        traceback.print_exc()
        # エラーが発生した場合、ユーザーがエラーメッセージを確認できるように一時停止
        input("エラーが発生しました。エラーメッセージを確認するにはEnterキーを押してください...")

if __name__ == "__main__":
    main()