"""Simple desktop control panel for the Telegram-to-X service.

The panel intentionally uses only the Python standard library (Tkinter). It
starts the service as a detached subprocess, controls graceful shutdown through
``storage/stop.request``, edits the publication interval in ``.env`` and can
clear the pending queue without touching Telegram/X sessions or publication
history.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable
from tkinter import BOTH, END, LEFT, RIGHT, X, Button, Entry, Frame, Label, StringVar, Text, Tk
from tkinter import messagebox, ttk

PROJECT_ROOT = Path(__file__).resolve().parent
ENV_PATH = PROJECT_ROOT / ".env"
STORAGE_DIR = PROJECT_ROOT / "storage"
PENDING_PATH = STORAGE_DIR / "pending.json"
RATE_LIMIT_PATH = STORAGE_DIR / "rate_limit.json"
STOP_REQUEST_PATH = STORAGE_DIR / "stop.request"
PID_PATH = STORAGE_DIR / "bot.pid"
MEDIA_DIR = STORAGE_DIR / "media"
LOG_PATH = STORAGE_DIR / "logs" / "telegram_to_x.log"
APP_PATH = PROJECT_ROOT / "app.py"

ENV_LINE_RE = re.compile(r"^(?P<prefix>\s*PUBLISH_INTERVAL_SECONDS\s*=).*$", re.MULTILINE)


def bot_python_executable() -> Path:
    """Return python.exe for the worker even when the panel uses pythonw.exe.

    Launching the background worker with pythonw.exe can make Playwright's
    driver create a small ownerless/ghost window on Windows. The console
    interpreter combined with CREATE_NO_WINDOW avoids that window while the
    Tkinter panel itself remains console-free.
    """

    executable = Path(sys.executable)
    if os.name == "nt" and executable.name.lower() == "pythonw.exe":
        console_executable = executable.with_name("python.exe")
        if console_executable.exists():
            return console_executable
    return executable


def read_interval_seconds(default: int = 600) -> int:
    """Read the current interval from .env without loading credentials."""

    try:
        content = ENV_PATH.read_text(encoding="utf-8")
    except OSError:
        return default

    match = re.search(
        r"^\s*PUBLISH_INTERVAL_SECONDS\s*=\s*(\d+)\s*$",
        content,
        flags=re.MULTILINE,
    )
    if not match:
        return default
    return max(0, int(match.group(1)))


def write_interval_seconds(seconds: int) -> None:
    """Update only PUBLISH_INTERVAL_SECONDS while preserving the rest of .env."""

    if seconds < 0:
        raise ValueError("O intervalo não pode ser negativo")

    if ENV_PATH.exists():
        content = ENV_PATH.read_text(encoding="utf-8")
    else:
        example = PROJECT_ROOT / ".env.example"
        content = example.read_text(encoding="utf-8") if example.exists() else ""

    replacement = f"PUBLISH_INTERVAL_SECONDS={seconds}"
    if ENV_LINE_RE.search(content):
        content = ENV_LINE_RE.sub(replacement, content, count=1)
    else:
        if content and not content.endswith("\n"):
            content += "\n"
        content += replacement + "\n"

    temp_path = ENV_PATH.with_suffix(".env.tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(temp_path, ENV_PATH)


def read_pid() -> int | None:
    """Return the persisted bot PID when valid."""

    try:
        return int(PID_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def process_is_running(pid: int | None) -> bool:
    """Check whether a process exists on Windows or POSIX."""

    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        return str(pid) in result.stdout and "No tasks" not in result.stdout
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def pending_count() -> int:
    """Return the current number of durable pending jobs."""

    try:
        data = json.loads(PENDING_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    jobs = data.get("jobs", {}) if isinstance(data, dict) else {}
    return len(jobs) if isinstance(jobs, dict) else 0


def clear_queue_files() -> int:
    """Clear pending jobs, temporary media and the persisted rate-limit slot."""

    count = pending_count()
    STORAGE_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_PATH.write_text(
        json.dumps({"version": 1, "jobs": {}}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    RATE_LIMIT_PATH.write_text(
        json.dumps(
            {"version": 1, "next_allowed_at": None},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if MEDIA_DIR.exists():
        for child in MEDIA_DIR.iterdir():
            if child.name == ".gitkeep":
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except OSError:
                pass

    STOP_REQUEST_PATH.unlink(missing_ok=True)
    return count


class ControlPanel:
    """Tkinter interface for starting, stopping and resetting the bot."""

    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("Telegram → X | Painel de Controle")
        self.root.geometry("860x620")
        self.root.minsize(760, 540)

        self.status_var = StringVar(value="Verificando...")
        self.pid_var = StringVar(value="PID: —")
        self.queue_var = StringVar(value="Fila: 0")
        self.interval_var = StringVar(value=self._format_minutes(read_interval_seconds()))
        self.action_in_progress = False
        self._last_log_signature: tuple[int, int] | None = None

        self._build_ui()
        self._refresh_status()
        self._refresh_logs()

    @staticmethod
    def _format_minutes(seconds: int) -> str:
        minutes = seconds / 60
        return str(int(minutes)) if minutes.is_integer() else f"{minutes:.2f}".rstrip("0")

    def _build_ui(self) -> None:
        header = Frame(self.root, padx=18, pady=14)
        header.pack(fill=X)

        Label(
            header,
            text="Bot Telegram → X",
            font=("Segoe UI", 18, "bold"),
        ).pack(anchor="w")
        Label(
            header,
            text="Inicie, interrompa, altere o intervalo e zere a fila com segurança.",
            font=("Segoe UI", 10),
        ).pack(anchor="w", pady=(2, 0))

        status_frame = ttk.LabelFrame(self.root, text="Status", padding=12)
        status_frame.pack(fill=X, padx=18, pady=(0, 10))

        Label(status_frame, textvariable=self.status_var, font=("Segoe UI", 11, "bold")).pack(side=LEFT)
        Label(status_frame, textvariable=self.pid_var).pack(side=LEFT, padx=(24, 0))
        Label(status_frame, textvariable=self.queue_var).pack(side=RIGHT)

        controls = ttk.LabelFrame(self.root, text="Controles", padding=12)
        controls.pack(fill=X, padx=18, pady=(0, 10))

        first_row = Frame(controls)
        first_row.pack(fill=X)

        self.start_button = Button(
            first_row,
            text="▶ Iniciar bot",
            width=18,
            command=self.start_bot,
        )
        self.start_button.pack(side=LEFT, padx=(0, 8))

        self.stop_button = Button(
            first_row,
            text="■ Parar bot",
            width=18,
            command=self.stop_bot,
        )
        self.stop_button.pack(side=LEFT, padx=(0, 8))

        self.reset_button = Button(
            first_row,
            text="Zerar fila e reiniciar",
            width=22,
            command=self.reset_queue_and_restart,
        )
        self.reset_button.pack(side=LEFT)

        second_row = Frame(controls)
        second_row.pack(fill=X, pady=(14, 0))

        Label(second_row, text="Intervalo entre posts (minutos):").pack(side=LEFT)
        self.interval_entry = Entry(
            second_row,
            width=10,
            textvariable=self.interval_var,
            justify="center",
        )
        self.interval_entry.pack(side=LEFT, padx=(8, 8))
        Button(
            second_row,
            text="Aplicar intervalo",
            command=self.apply_interval,
        ).pack(side=LEFT)
        Label(
            second_row,
            text="A alteração reinicia o bot quando ele estiver em execução.",
        ).pack(side=LEFT, padx=(12, 0))

        logs_frame = ttk.LabelFrame(self.root, text="Últimas atividades", padding=8)
        logs_frame.pack(fill=BOTH, expand=True, padx=18, pady=(0, 12))

        self.logs_text = Text(
            logs_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
        )
        scrollbar = ttk.Scrollbar(logs_frame, command=self.logs_text.yview)
        self.logs_text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=RIGHT, fill="y")
        self.logs_text.pack(side=LEFT, fill=BOTH, expand=True)

        footer = Frame(self.root, padx=18, pady=6)
        footer.pack(fill=X)
        Label(
            footer,
            text="Zerar a fila não apaga os logins nem o histórico de mensagens já publicadas.",
            font=("Segoe UI", 9),
        ).pack(side=LEFT)
        Button(footer, text="Abrir pasta de logs", command=self.open_logs_folder).pack(side=RIGHT)

    def start_bot(self) -> None:
        if self.action_in_progress:
            return
        pid = read_pid()
        if process_is_running(pid):
            messagebox.showinfo("Bot em execução", f"O bot já está rodando no PID {pid}.")
            return
        if not ENV_PATH.exists():
            messagebox.showerror("Configuração ausente", "O arquivo .env não foi encontrado.")
            return

        STOP_REQUEST_PATH.unlink(missing_ok=True)
        PID_PATH.unlink(missing_ok=True)

        creationflags = 0
        startupinfo = None
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
            startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)

        child_env = os.environ.copy()
        child_env["PYTHONUNBUFFERED"] = "1"
        child_env["PUBLISH_INTERVAL_SECONDS"] = str(read_interval_seconds())
        child_env["X_HEADLESS"] = "true"

        try:
            subprocess.Popen(
                [str(bot_python_executable()), "-u", str(APP_PATH)],
                cwd=str(PROJECT_ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=child_env,
                creationflags=creationflags,
                startupinfo=startupinfo,
                close_fds=os.name != "nt",
            )
        except OSError as exc:
            messagebox.showerror("Falha ao iniciar", str(exc))
            return

        self.status_var.set("Iniciando...")
        self.root.after(1200, self._refresh_status)

    def stop_bot(self) -> None:
        if self.action_in_progress:
            return
        if not process_is_running(read_pid()):
            self.status_var.set("Parado")
            return
        self._request_stop(on_stopped=None)

    def apply_interval(self) -> None:
        if self.action_in_progress:
            return
        try:
            raw = self.interval_var.get().strip().replace(",", ".")
            minutes = float(raw)
            if minutes <= 0:
                raise ValueError
            seconds = int(round(minutes * 60))
            if seconds < 10:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Intervalo inválido",
                "Informe um número maior que zero. O mínimo é 0,17 minuto (10 segundos).",
            )
            return

        write_interval_seconds(seconds)
        self.interval_var.set(self._format_minutes(seconds))

        if process_is_running(read_pid()):
            self._request_stop(on_stopped=self.start_bot)
        else:
            messagebox.showinfo(
                "Intervalo salvo",
                f"Novo intervalo: {self._format_minutes(seconds)} minuto(s).",
            )

    def reset_queue_and_restart(self) -> None:
        if self.action_in_progress:
            return
        confirmed = messagebox.askyesno(
            "Zerar fila",
            "Deseja parar o bot, apagar todas as mensagens pendentes e iniciar uma fila nova?\n\n"
            "O histórico de publicações e as sessões do Telegram/X serão preservados.",
        )
        if not confirmed:
            return

        def clear_and_start() -> None:
            removed = clear_queue_files()
            self._append_local_log(f"Fila zerada manualmente | itens removidos={removed}")
            self.start_bot()

        if process_is_running(read_pid()):
            self._request_stop(on_stopped=clear_and_start)
        else:
            clear_and_start()

    def _request_stop(self, on_stopped: Callable[[], None] | None) -> None:
        self.action_in_progress = True
        STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        STOP_REQUEST_PATH.write_text(str(time.time()), encoding="utf-8")
        self.status_var.set("Parando com segurança...")
        deadline = time.monotonic() + 30
        self._wait_for_stop(deadline, on_stopped)

    def _wait_for_stop(
        self, deadline: float, on_stopped: Callable[[], None] | None
    ) -> None:
        pid = read_pid()
        if not process_is_running(pid):
            PID_PATH.unlink(missing_ok=True)
            STOP_REQUEST_PATH.unlink(missing_ok=True)
            self.action_in_progress = False
            self.status_var.set("Parado")
            if on_stopped is not None:
                self.root.after(100, on_stopped)
            return

        if time.monotonic() >= deadline:
            self.action_in_progress = False
            self.status_var.set("Não foi possível parar automaticamente")
            force = messagebox.askyesno(
                "Tempo esgotado",
                "O bot não encerrou em 30 segundos. Deseja forçar o encerramento?",
            )
            if force and pid:
                self._force_kill(pid)
                PID_PATH.unlink(missing_ok=True)
                STOP_REQUEST_PATH.unlink(missing_ok=True)
                if on_stopped is not None:
                    self.root.after(500, on_stopped)
            return

        self.root.after(500, lambda: self._wait_for_stop(deadline, on_stopped))

    @staticmethod
    def _force_kill(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:
            try:
                os.kill(pid, 9)
            except OSError:
                pass

    def _refresh_status(self) -> None:
        pid = read_pid()
        running = process_is_running(pid)
        self.status_var.set("Em execução" if running else "Parado")
        self.pid_var.set(f"PID: {pid}" if running and pid else "PID: —")
        self.queue_var.set(f"Fila: {pending_count()}")
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.root.after(1200, self._refresh_status)

    def _refresh_logs(self) -> None:
        try:
            stat = LOG_PATH.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            signature = None

        if signature and signature != self._last_log_signature:
            self._last_log_signature = signature
            try:
                with LOG_PATH.open("rb") as file:
                    file.seek(max(0, stat.st_size - 80_000))
                    raw = file.read()
                text = raw.decode("utf-8", errors="replace")
                lines = text.splitlines()[-220:]
                self.logs_text.configure(state="normal")
                self.logs_text.delete("1.0", END)
                self.logs_text.insert(END, "\n".join(lines))
                self.logs_text.see(END)
                self.logs_text.configure(state="disabled")
            except OSError:
                pass

        self.root.after(1200, self._refresh_logs)

    def _append_local_log(self, message: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.logs_text.configure(state="normal")
        self.logs_text.insert(END, f"\n{timestamp} | PAINEL | {message}\n")
        self.logs_text.see(END)
        self.logs_text.configure(state="disabled")

    @staticmethod
    def open_logs_folder() -> None:
        (STORAGE_DIR / "logs").mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(STORAGE_DIR / "logs")  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(STORAGE_DIR / "logs")])
        else:
            subprocess.Popen(["xdg-open", str(STORAGE_DIR / "logs")])


def main() -> None:
    """Launch the desktop control panel."""

    root = Tk()
    try:
        ttk.Style().theme_use("vista" if os.name == "nt" else "clam")
    except Exception:
        pass
    ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    main()

