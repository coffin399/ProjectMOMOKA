# ログビューアをデーモンスレッドで起動する

import threading
import tkinter as tk
import traceback


def run_log_viewer_thread(log_queue) -> threading.Thread:
    """ログビューアを別スレッドで起動し、Thread を返す。"""

    def run_gui() -> None:
        """Tk メインループをこのスレッドで回す。"""
        try:
            # ルートウィンドウを作る
            root = tk.Tk()
            # 遅延 import で循環参照を避ける
            from MOMOKA.GUI.log_viewer import LogViewerApp

            # アプリを構築する
            LogViewerApp(root, log_queue)
            # イベントループを開始する
            root.mainloop()
        except Exception as e:
            # GUI 失敗でも Bot 本体は止めない
            print(f"ログビューアでエラーが発生しました: {e}")
            # スタックをコンソールへ出す
            traceback.print_exc()

    # デーモンスレッドとして起動する（メイン終了で一緒に終わる）
    thread = threading.Thread(target=run_gui, daemon=True)
    # スレッドを開始する
    thread.start()
    # 呼び出し側が参照できるよう返す
    return thread
