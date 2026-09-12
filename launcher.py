"""Keep imports inside the startup handler so config errors are visible."""
import argparse
import json
from pathlib import Path
import sys
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", metavar="RESULT_JSON")
    args = parser.parse_args()
    try:
        import hrm_app
        import runtime
        if args.smoke_test:
            import tempfile
            import tkinter as tk
            import storage
            from hr_ble import parse_hr_measurement
            with tempfile.TemporaryDirectory() as folder:
                root = tk.Tk()
                root.withdraw()
                import config
                config.DB_PATH = Path(folder) / "ui.sqlite3"
                config.SETTINGS_PATH = Path(folder) / "settings.json"
                app = hrm_app.HRApp(root, start_ble=False)
                root.update()
                if not app.store.close():
                    raise RuntimeError("Packaged UI storage initialization failed")
                app.destroyed = True
                root.destroy()
                store = storage.AsyncSessionStore(Path(folder) / "smoke.sqlite3")
                store.start_session(1, "smoke", None)
                store.insert_sample(1, "2026-01-01T00:00:00+00:00", 72)
                store.end_session(1)
                if not store.close() or storage.list_sessions(store.db_path)[0][5] != 1:
                    raise RuntimeError("Packaged storage smoke test failed")
                if parse_hr_measurement(bytearray([0, 72]))[0] != 72:
                    raise RuntimeError("Packaged parser smoke test failed")
            Path(args.smoke_test).write_text(json.dumps({"ok": True, **runtime.diagnostics()}), encoding="utf-8")
        else:
            hrm_app.main()
    except Exception as exc:
        if args.smoke_test:
            Path(args.smoke_test).write_text(json.dumps({"ok": False, "error": str(exc)}), encoding="utf-8")
        else:
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror("Ticker could not start", str(exc), parent=root)
                root.destroy()
            except Exception:
                if sys.stderr is not None:
                    traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
