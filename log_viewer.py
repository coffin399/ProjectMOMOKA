import tkinter as tk
from tkinter import scrolledtext, ttk
import logging
import queue
import threading
from datetime import datetime
import os
import json

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
        
        # 設定ファイルの読み込み
        self.config_file = "data/log_viewer_config.json"
        self.load_config()
        
        # ログキュー
        self.log_queue = queue.Queue()
        
        # ロガーの設定
        self.setup_logging()
        
        # GUIの作成
        self.setup_gui()
        
        # キューを定期的にチェック
        self.poll_log_queue()
        
        # ウィンドウクローズ時の処理
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
        
    def load_config(self):
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
        try:
            os.makedirs(os.path.dirname(self.config_file), exist_ok=True)
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"設定ファイルの保存中にエラーが発生しました: {e}")
        
    def setup_gui(self):
        # メインフレーム
        main_frame = ttk.Frame(self.root, padding="5")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
        # 上部フレーム（コントロールパネル）
        control_frame = ttk.LabelFrame(main_frame, text="コントロール", padding="5 2 5 5")
        control_frame.pack(fill=tk.X, pady=(0, 5))
        
        # クリアボタン
        ttk.Button(control_frame, text="全クリア", command=self.clear_all_logs).pack(side=tk.LEFT, padx=2)
        
        # 自動スクロールチェックボックス
        self.auto_scroll_var = tk.BooleanVar(value=self.config["auto_scroll"])
        ttk.Checkbutton(control_frame, text="自動スクロール", variable=self.auto_scroll_var,
                       command=self.toggle_auto_scroll).pack(side=tk.LEFT, padx=5)
        
        # ログレベル選択
        ttk.Label(control_frame, text="ログレベル:").pack(side=tk.LEFT, padx=(10, 2))
        
        log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        
        self.general_level = tk.StringVar(value=self.config["log_levels"]["general"])
        ttk.OptionMenu(control_frame, self.general_level, self.general_level.get(), *log_levels,
                      command=lambda x: self.update_log_level("general", x)).pack(side=tk.LEFT, padx=2)
        
        self.llm_level = tk.StringVar(value=self.config["log_levels"]["llm"])
        ttk.OptionMenu(control_frame, self.llm_level, self.llm_level.get(), *log_levels,
                      command=lambda x: self.update_log_level("llm", x)).pack(side=tk.LEFT, padx=2)
        
        self.tts_level = tk.StringVar(value=self.config["log_levels"]["tts"])
        ttk.OptionMenu(control_frame, self.tts_level, self.tts_level.get(), *log_levels,
                      command=lambda x: self.update_log_level("tts", x)).pack(side=tk.LEFT, padx=2)
        
        self.error_level = tk.StringVar(value=self.config["log_levels"]["error"])
        ttk.OptionMenu(control_frame, self.error_level, self.error_level.get(), *log_levels,
                      command=lambda x: self.update_log_level("error", x)).pack(side=tk.LEFT, padx=2)
        
        # ログ表示エリア
        log_frame = ttk.Frame(main_frame)
        log_frame.pack(fill=tk.BOTH, expand=True)
        
        # 左上: 一般ログ
        general_frame = ttk.LabelFrame(log_frame, text="一般ログ", padding="2")
        general_frame.grid(row=0, column=0, padx=2, pady=2, sticky="nsew")
        self.general_log = scrolledtext.ScrolledText(
            general_frame, wrap=tk.WORD, width=60, height=15,
            font=self.config["font"], bg='white', fg='black'
        )
        self.general_log.pack(fill=tk.BOTH, expand=True)
        
        # 右上: LLMログ
        llm_frame = ttk.LabelFrame(log_frame, text="LLMログ", padding="2")
        llm_frame.grid(row=0, column=1, padx=2, pady=2, sticky="nsew")
        self.llm_log = scrolledtext.ScrolledText(
            llm_frame, wrap=tk.WORD, width=60, height=15,
            font=self.config["font"], bg='#f0f8ff', fg='black'
        )
        self.llm_log.pack(fill=tk.BOTH, expand=True)
        
        # 左下: TTSログ
        tts_frame = ttk.LabelFrame(log_frame, text="TTSログ", padding="2")
        tts_frame.grid(row=1, column=0, padx=2, pady=2, sticky="nsew")
        self.tts_log = scrolledtext.ScrolledText(
            tts_frame, wrap=tk.WORD, width=60, height=15,
            font=self.config["font"], bg='#f0fff0', fg='black'
        )
        self.tts_log.pack(fill=tk.BOTH, expand=True)
        
        # 右下: エラーログ
        error_frame = ttk.LabelFrame(log_frame, text="エラーログ", padding="2")
        error_frame.grid(row=1, column=1, padx=2, pady=2, sticky="nsew")
        self.error_log = scrolledtext.ScrolledText(
            error_frame, wrap=tk.WORD, width=60, height=15,
            font=self.config["font"], bg='#fff0f0', fg='red'
        )
        self.error_log.pack(fill=tk.BOTH, expand=True)
        
        # グリッドの設定
        log_frame.columnconfigure(0, weight=1)
        log_frame.columnconfigure(1, weight=1)
        log_frame.rowconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        
        # ステータスバー
        self.status_var = tk.StringVar()
        self.status_var.set("準備完了")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(fill=tk.X, side=tk.BOTTOM, ipady=2)
        
        # コンテキストメニュー
        self.setup_context_menu()
        
    def setup_context_menu(self):
        # 各テキストウィジェットにコンテキストメニューを追加
        for widget in [self.general_log, self.llm_log, self.tts_log, self.error_log]:
            menu = tk.Menu(widget, tearoff=0)
            menu.add_command(label="コピー", command=lambda w=widget: self.copy_text(w))
            menu.add_command(label="すべて選択", command=lambda w=widget: self.select_all(w))
            menu.add_separator()
            menu.add_command(label="クリア", command=lambda w=widget: self.clear_log(w))
            
            widget.bind("<Button-3>", lambda e, m=menu: self.show_context_menu(e, m))
    
    def show_context_menu(self, event, menu):
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
    
    def copy_text(self, widget):
        try:
            selected_text = widget.get("sel.first", "sel.last")
            self.root.clipboard_clear()
            self.root.clipboard_append(selected_text)
            self.status_var.set("選択されたテキストをクリップボードにコピーしました")
        except tk.TclError:
            self.status_var.set("コピーするテキストが選択されていません")
    
    def select_all(self, widget):
        widget.tag_add(tk.SEL, "1.0", tk.END)
        widget.mark_set(tk.INSERT, "1.0")
        widget.see(tk.INSERT)
        return 'break'
    
    def clear_log(self, widget):
        widget.config(state='normal')
        widget.delete(1.0, tk.END)
        widget.config(state='disabled')
        self.status_var.set("ログをクリアしました")
    
    def clear_all_logs(self):
        for widget in [self.general_log, self.llm_log, self.tts_log, self.error_log]:
            self.clear_log(widget)
        self.status_var.set("すべてのログをクリアしました")
    
    def toggle_auto_scroll(self):
        self.config["auto_scroll"] = self.auto_scroll_var.get()
        self.save_config()
    
    def update_log_level(self, log_type, level):
        self.config["log_levels"][log_type] = level
        self.save_config()
        self.status_var.set(f"{log_type}のログレベルを{level}に設定しました")
    
    def setup_logging(self):
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
        try:
            while True:
                name, level, log_entry = self.log_queue.get_nowait()
                self.process_log_entry(name, level, log_entry)
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self.poll_log_queue)
    
    def process_log_entry(self, name, level, log_entry):
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
            min_level = log_levels.get(self.llm_level.get(), 20)  # デフォルトはINFO
        elif "MOMOKA.tts" in name:
            log_type = "tts"
            widget = self.tts_log
            min_level = log_levels.get(self.tts_level.get(), 20)  # デフォルトはINFO
        elif level in ["ERROR", "CRITICAL"]:
            log_type = "error"
            widget = self.error_log
            min_level = log_levels.get(self.error_level.get(), 30)  # デフォルトはWARNING
        else:
            log_type = "general"
            widget = self.general_log
            min_level = log_levels.get(self.general_level.get(), 20)  # デフォルトはINFO
        
        # ログレベルが閾値以上の場合のみ表示
        if log_levels.get(level, 0) >= min_level:
            self.append_to_log(widget, log_entry, level)
    
    def append_to_log(self, text_widget, message, level=None):
        text_widget.config(state='normal')
        
        # 行数制限
        lines = int(text_widget.index('end-1c').split('.')[0])
        if lines > self.config["max_lines"]:
            text_widget.delete(1.0, f"{lines - self.config['max_lines']}.0")
        
        # レベルに応じた色付け
        if level == "ERROR" or level == "CRITICAL":
            text_widget.tag_config("error", foreground="red")
            text_widget.insert(tk.END, message + "\n", "error")
        elif level == "WARNING":
            text_widget.tag_config("warning", foreground="orange")
            text_widget.insert(tk.END, message + "\n", "warning")
        else:
            text_widget.insert(tk.END, message + "\n")
        
        # 自動スクロール
        if self.config["auto_scroll"]:
            text_widget.see(tk.END)
        
        text_widget.config(state='disabled')
    
    def on_closing(self):
        # 設定を保存
        self.save_config()
        self.root.destroy()

def main():
    root = tk.Tk()
    app = LogViewerApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
