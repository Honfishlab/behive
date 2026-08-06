"""Local, masked GUI for configuring BeHive API credentials."""

from __future__ import annotations

import os
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

PROVIDERS = {
    "OpenAI": {"key": "OPENAI_API_KEY", "model": "openai/gpt-4o-mini"},
    "Anthropic": {"key": "ANTHROPIC_API_KEY", "model": "anthropic/claude-sonnet-4-20250514"},
}

def find_env_file() -> Path:
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"]
    return next((path for path in candidates if path.exists()), candidates[-1])

def upsert_env(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining, output = dict(updates), []
    for line in lines:
        stripped = line.lstrip()
        candidate = stripped[1:].lstrip() if stripped.startswith("#") else stripped
        name = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
        output.append(f"{name}={remaining.pop(name)}" if name in remaining else line)
    if remaining:
        if output and output[-1].strip(): output.append("")
        output.extend(f"{name}={value}" for name, value in remaining.items())
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, path)

class KeySetup(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("BeHive API Key Setup")
        self.geometry("560x520")
        self.minsize(520, 480)
        self.resizable(True, True)
        self.env_path = find_env_file()
        frame = ttk.Frame(self, padding=28); frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="BeHive API Key Setup", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="The key is written only to your local .env file. It is masked and never logged.", wraplength=450).pack(anchor="w", pady=(4, 20))
        ttk.Label(frame, text="Provider").pack(anchor="w")
        self.provider = tk.StringVar(value="OpenAI")
        box = ttk.Combobox(frame, textvariable=self.provider, values=list(PROVIDERS), state="readonly"); box.pack(fill="x", pady=(4, 12)); box.bind("<<ComboboxSelected>>", self.provider_changed)
        ttk.Label(frame, text="API key").pack(anchor="w")
        self.key = tk.StringVar(); entry = ttk.Entry(frame, textvariable=self.key, show="●"); entry.pack(fill="x", pady=(4, 4)); entry.focus_set()
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame, text="Show key", variable=self.show_key, command=lambda: entry.configure(show="" if self.show_key.get() else "●")).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text="Model").pack(anchor="w")
        self.model = tk.StringVar(value=PROVIDERS["OpenAI"]["model"]); ttk.Entry(frame, textvariable=self.model).pack(fill="x", pady=(4, 16))
        ttk.Label(frame, text=f"Saving to: {self.env_path}").pack(anchor="w", pady=(0, 16))
        ttk.Button(frame, text="Save configuration", command=self.save).pack(fill="x")

    def provider_changed(self, _event=None) -> None:
        self.model.set(PROVIDERS[self.provider.get()]["model"])

    def save(self) -> None:
        key, model = self.key.get().strip(), self.model.get().strip()
        if len(key) < 20: messagebox.showerror("Invalid key", "Enter the complete API key."); return
        if not model: messagebox.showerror("Missing model", "Enter a model name."); return
        provider = PROVIDERS[self.provider.get()]
        try: upsert_env(self.env_path, {provider["key"]: key, "BEHIVE_MODEL": model, "LLM_PROVIDER": self.provider.get().lower()})
        except OSError as exc: messagebox.showerror("Could not save", str(exc)); return
        self.key.set("")
        messagebox.showinfo("Configuration saved", "Saved locally. Restart BeHive to load the key.")
        self.destroy()

if __name__ == "__main__":
    KeySetup().mainloop()
